"""Immutable Eastmoney raw captures for the R1-3 margin-evidence reader."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

import requests

from fin_analyse.margin.evidence import (
    MarginEvidenceRequest,
    MarginEvidenceSourceResult,
    MarginObservation,
)
from fin_analyse.market.data_qualification import ObservationEvidenceOrigin

_MARGIN_ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_KLINE_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_QUOTE_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/stock/get"
_MAX_RAW_BYTES = 4 * 1024 * 1024
_ARTIFACT_SCHEMA = "eastmoney_margin_evidence_artifact.v1"
_SOURCE_ID = "eastmoney"
_SOURCE_VERSION = "eastmoney_margin_evidence.v1"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class EastmoneyMarginEvidenceError(RuntimeError):
    """A safe, typed failure from the source-native margin adapter."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


class EastmoneyMarginHttpGet(Protocol):
    def __call__(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _HttpResponse: ...


@dataclass(frozen=True, slots=True)
class _RawCapture:
    raw_payload: bytes
    revision: str
    captured_at: datetime
    stale_cache: bool = False
    #: 回放原因码（MARGIN_EVIDENCE_HTTP_RESPONSE_INVALID / TRANSPORT_UNAVAILABLE），
    #: 由 read() 与 STALE_CACHE 叠加成两个 gap，保留故障类型归因（BUG-040）。
    stale_reason: str = ""


class EastmoneyMarginEvidenceSource:
    """One source-native adapter: capture raw data, then project typed records.

    Every network response is content-addressed under the owner-only artifact
    root. A changed source response creates a new revision; a transport failure
    may only replay the latest verified raw capture for that exact scope.
    """

    def __init__(
        self,
        *,
        artifact_root: Path,
        http_get: EastmoneyMarginHttpGet | None = None,
        evidence_origin: ObservationEvidenceOrigin = ObservationEvidenceOrigin.TEST_ONLY,
        timeout_seconds: float = 12.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(artifact_root, Path):
            raise TypeError("margin artifact root must be a pathlib.Path")
        if type(evidence_origin) is not ObservationEvidenceOrigin:
            raise TypeError("margin evidence origin is invalid")
        if http_get is not None and evidence_origin is not ObservationEvidenceOrigin.TEST_ONLY:
            raise EastmoneyMarginEvidenceError(
                "EASTMONEY_MARGIN_INJECTED_TRANSPORT_MUST_BE_TEST_ONLY"
            )
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("margin timeout must be finite and positive")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("margin timeout must be finite and positive")
        self._store = _MarginArtifactStore(artifact_root)
        self._http_get = requests.get if http_get is None else http_get
        self._evidence_origin = evidence_origin
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def provider_id(self) -> str:
        return _SOURCE_ID

    @property
    def provider_version(self) -> str:
        return _SOURCE_VERSION

    def read(self, request: MarginEvidenceRequest) -> MarginEvidenceSourceResult:
        _validate_request(request)
        markets: dict[str, tuple[MarginObservation, ...]] = {}
        instruments: dict[str, tuple[MarginObservation, ...]] = {}
        gaps: list[str] = []
        for venue in ("SH", "SZ"):
            try:
                capture = self._capture_market(venue, request=request)
                markets[venue] = _parse_market(capture)
                if capture.stale_cache:
                    if capture.stale_reason:
                        gaps.append(f"{capture.stale_reason}:{venue}")
                    gaps.append(f"MARGIN_EVIDENCE_STALE_CACHE:{venue}")
            except EastmoneyMarginEvidenceError as error:
                gaps.append(f"{error.code}:{venue}")
        for symbol in request.instruments:
            if symbol.endswith(".BJ"):
                gaps.append(f"MARGIN_EVIDENCE_BJ_NOT_COVERED:{symbol}")
                continue
            if _symbol_parts(symbol) is None:
                gaps.append(f"MARGIN_EVIDENCE_SYMBOL_UNSUPPORTED:{symbol}")
                continue
            try:
                margin_capture = self._capture_stock_margin(symbol, request=request)
                points = _parse_stock_margin(margin_capture, symbol=symbol)
            except EastmoneyMarginEvidenceError as error:
                gaps.append(f"{error.code}:{symbol}")
                continue
            try:
                kline_capture = self._capture_stock_kline(symbol, request=request)
                denominators = _parse_stock_kline(kline_capture)
                if kline_capture.stale_cache:
                    if kline_capture.stale_reason:
                        gaps.append(f"{kline_capture.stale_reason}:{symbol}")
                    gaps.append(f"MARGIN_EVIDENCE_STALE_CACHE:{symbol}")
            except EastmoneyMarginEvidenceError as error:
                denominators = {}
                gaps.append(f"{error.code}:{symbol}")
            instruments[symbol] = _merge_stock_denominators(points, denominators)
            # 行情能力扩展验收 3:当日自由流通市值(stock/get f117)合并到最新点,
            # 只对当日(latest trade_date)有效,不冒充历史日市值。
            try:
                quote_capture = self._capture_stock_quote(symbol, request=request)
                cap = _parse_stock_quote_cap(quote_capture)
                if cap is not None:
                    instruments[symbol] = _merge_latest_cap(instruments[symbol], cap)
            except EastmoneyMarginEvidenceError as error:
                gaps.append(f"{error.code}:{symbol}")
            if margin_capture.stale_cache:
                if margin_capture.stale_reason:
                    gaps.append(f"{margin_capture.stale_reason}:{symbol}")
                gaps.append(f"MARGIN_EVIDENCE_STALE_CACHE:{symbol}")
        return MarginEvidenceSourceResult(
            markets=markets,
            instruments=instruments,
            data_gaps=tuple(dict.fromkeys(gaps)),
        )

    def _capture_market(self, venue: str, *, request: MarginEvidenceRequest) -> _RawCapture:
        code = {"SH": "007", "SZ": "001"}[venue]
        return self._capture(
            scope=("market", venue),
            url=_MARGIN_ENDPOINT,
            params={
                "reportName": "RPTA_WEB_RZRQ_LSSH",
                "columns": "ALL",
                "source": "WEB",
                "sortColumns": "DIM_DATE",
                "sortTypes": "-1",
                "pageNumber": "1",
                "pageSize": "500",
                "filter": f"(SCDM={code})",
            },
            headers=_margin_headers(),
            request=request,
        )

    def _capture_stock_margin(
        self,
        symbol: str,
        *,
        request: MarginEvidenceRequest,
    ) -> _RawCapture:
        code, _venue = _require_symbol(symbol)
        return self._capture(
            scope=("stock-margin", symbol),
            url=_MARGIN_ENDPOINT,
            params={
                "reportName": "RPTA_WEB_RZRQ_GGMX",
                "columns": "ALL",
                "source": "WEB",
                "sortColumns": "DATE",
                "sortTypes": "-1",
                "pageNumber": "1",
                "pageSize": "500",
                "filter": f"(SCODE={code})",
            },
            headers=_margin_headers(),
            request=request,
        )

    def _capture_stock_quote(
        self,
        symbol: str,
        *,
        request: MarginEvidenceRequest,
    ) -> _RawCapture:
        code, venue = _require_symbol(symbol)
        market = "1" if venue == "SH" else "0"
        return self._capture(
            scope=("stock-quote", symbol),
            url=_QUOTE_ENDPOINT,
            params={
                "fields": "f116,f117",
                "secid": f"{market}.{code}",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            },
            headers={
                "Referer": f"https://quote.eastmoney.com/{venue.lower()}{code}.html",
                "User-Agent": "fin-analyse-margin-evidence/1",
            },
            request=request,
        )

    def _capture_stock_kline(
        self,
        symbol: str,
        *,
        request: MarginEvidenceRequest,
    ) -> _RawCapture:
        code, venue = _require_symbol(symbol)
        market = "1" if venue == "SH" else "0"
        return self._capture(
            scope=("stock-kline", symbol),
            url=_KLINE_ENDPOINT,
            params={
                "beg": "0",
                "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "fqt": "0",
                "klt": "101",
                "lmt": "500",
                "secid": f"{market}.{code}",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            },
            headers={
                "Referer": f"https://quote.eastmoney.com/{venue.lower()}{code}.html",
                "User-Agent": "fin-analyse-margin-evidence/1",
            },
            request=request,
        )

    def _capture(
        self,
        *,
        scope: tuple[str, str],
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        request: MarginEvidenceRequest,
    ) -> _RawCapture:
        now = _aware_now(self._clock)
        timeout = self._timeout_seconds
        if request.deadline_at is not None:
            remaining = (request.deadline_at - now).total_seconds()
            if remaining <= 0:
                raise EastmoneyMarginEvidenceError("MARGIN_EVIDENCE_DEADLINE_REACHED")
            timeout = min(timeout, remaining)
        try:
            response = self._http_get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
            raw_payload = response.content
            if (
                response.status_code != 200
                or not isinstance(raw_payload, bytes)
                or len(raw_payload) > _MAX_RAW_BYTES
            ):
                # BUG-040：HTTP 非 200（恰是反爬 403/5xx 形态）与传输异常同走
                # stale 回放；失败原因码经 stale_reason 保留，回放时与
                # STALE_CACHE 叠加成两个 gap，不再随故障类型漂移。
                return self._replay_or_raise(
                    scope, reason="MARGIN_EVIDENCE_HTTP_RESPONSE_INVALID"
                )
            return self._store.capture(
                scope, raw_payload=raw_payload, captured_at=_aware_now(self._clock)
            )
        except EastmoneyMarginEvidenceError:
            raise
        except Exception as error:
            return self._replay_or_raise(
                scope,
                reason="MARGIN_EVIDENCE_TRANSPORT_UNAVAILABLE",
                cause=error,
            )

    def _replay_or_raise(
        self,
        scope: tuple[str, str],
        *,
        reason: str,
        cause: BaseException | None = None,
    ) -> _RawCapture:
        cached = self._store.load_latest(scope)
        if cached is not None:
            return _RawCapture(
                raw_payload=cached.raw_payload,
                revision=cached.revision,
                captured_at=cached.captured_at,
                stale_cache=True,
                stale_reason=reason,
            )
        if cause is not None:
            raise EastmoneyMarginEvidenceError(reason) from cause
        raise EastmoneyMarginEvidenceError(reason)


def _margin_headers() -> dict[str, str]:
    return {
        "Referer": "https://data.eastmoney.com/rzrq/",
        "User-Agent": "fin-analyse-margin-evidence/1",
    }


def _parse_market(capture: _RawCapture) -> tuple[MarginObservation, ...]:
    points = _parse_margin_rows(capture, date_key="DIM_DATE")
    if not points:
        raise EastmoneyMarginEvidenceError("MARGIN_EVIDENCE_PAYLOAD_INVALID")
    return points


def _parse_stock_margin(
    capture: _RawCapture,
    *,
    symbol: str,
) -> tuple[MarginObservation, ...]:
    code, _venue = _require_symbol(symbol)
    points: list[MarginObservation] = []
    for row in _rows(capture.raw_payload):
        if _text(row.get("SCODE")) != code:
            continue
        point = _margin_observation(row, date_key="DATE", capture=capture)
        if point is None:
            continue
        ratio = _decimal(row.get("RZYEZB"))
        free_float_market_cap = (
            point.financing_balance / (ratio / Decimal("100"))
            if ratio is not None and ratio > 0
            else None
        )
        points.append(
            MarginObservation(
                trade_date=point.trade_date,
                financing_balance=point.financing_balance,
                short_balance=point.short_balance,
                total_balance=point.total_balance,
                source_id=point.source_id,
                source_revision=point.source_revision,
                captured_at=point.captured_at,
                free_float_market_cap=free_float_market_cap,
                close=_decimal(row.get("SPJ")),
            )
        )
    if not points:
        raise EastmoneyMarginEvidenceError("MARGIN_EVIDENCE_PAYLOAD_INVALID")
    return tuple(sorted(points, key=lambda point: point.trade_date))


def _parse_margin_rows(
    capture: _RawCapture,
    *,
    date_key: str,
) -> tuple[MarginObservation, ...]:
    points = tuple(
        point
        for row in _rows(capture.raw_payload)
        if (point := _margin_observation(row, date_key=date_key, capture=capture)) is not None
    )
    return tuple(sorted(points, key=lambda point: point.trade_date))


def _margin_observation(
    row: Mapping[str, object],
    *,
    date_key: str,
    capture: _RawCapture,
) -> MarginObservation | None:
    trade_date = _date_value(row.get(date_key))
    financing = _decimal(row.get("RZYE"))
    short = _decimal(row.get("RQYE"))
    total = _decimal(row.get("RZRQYE"))
    if trade_date is None or financing is None or short is None or total is None:
        return None
    if financing < 0 or short < 0 or total < 0:
        return None
    return MarginObservation(
        trade_date=trade_date,
        financing_balance=financing,
        short_balance=short,
        total_balance=total,
        source_id=_SOURCE_ID,
        source_revision=capture.revision,
        captured_at=capture.captured_at,
    )


def _parse_stock_kline(
    capture: _RawCapture,
) -> dict[date, tuple[Decimal, Decimal, Decimal, str, datetime]]:
    try:
        payload = json.loads(capture.raw_payload)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        rows = data.get("klines") if isinstance(data, Mapping) else None
    except (TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        rows = None
    if not isinstance(rows, list):
        raise EastmoneyMarginEvidenceError("MARGIN_EVIDENCE_PAYLOAD_INVALID")
    result: dict[date, tuple[Decimal, Decimal, Decimal, str, datetime]] = {}
    for row in rows:
        if not isinstance(row, str):
            continue
        fields = row.split(",")
        if len(fields) < 7:
            continue
        trade_date = _date_value(fields[0])
        close = _decimal(fields[2])
        volume = _decimal(fields[5])
        turnover = _decimal(fields[6])
        if (
            trade_date is None
            or close is None
            or volume is None
            or turnover is None
            or close < 0
            or volume < 0
            or turnover < 0
        ):
            continue
        result[trade_date] = (
            close,
            volume,
            turnover,
            capture.revision,
            capture.captured_at,
        )
    if not result:
        raise EastmoneyMarginEvidenceError("MARGIN_EVIDENCE_PAYLOAD_INVALID")
    return result


def _parse_stock_quote_cap(capture: _RawCapture) -> Decimal | None:
    """从 stock/get 解析当日自由流通市值(f117);缺失/畸形 → None。"""
    try:
        document = json.loads(capture.raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict):
        return None
    value = data.get("f117")
    if value in (None, "-", ""):
        return None
    try:
        cap = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return cap if cap >= 0 else None


def _merge_latest_cap(
    points: tuple[MarginObservation, ...],
    cap: Decimal,
) -> tuple[MarginObservation, ...]:
    """当日市值只合并到最新 trade_date 的点(不冒充历史日市值)。"""
    if not points:
        return points
    latest_date = max(point.trade_date for point in points)
    merged: list[MarginObservation] = []
    for point in points:
        # 官方自由流通市值(f117)优先于 RZYEZB 反推估算,无条件覆盖最新点
        if point.trade_date == latest_date:
            point = MarginObservation(
                trade_date=point.trade_date,
                financing_balance=point.financing_balance,
                short_balance=point.short_balance,
                total_balance=point.total_balance,
                source_id=point.source_id,
                source_revision=point.source_revision,
                captured_at=point.captured_at,
                free_float_market_cap=cap,
                turnover=point.turnover,
                close=point.close,
                volume=point.volume,
                denominator_trade_date=point.denominator_trade_date,
                denominator_source_id=point.denominator_source_id,
                denominator_source_revision=point.denominator_source_revision,
                denominator_captured_at=point.denominator_captured_at,
            )
        merged.append(point)
    return tuple(merged)


def _merge_stock_denominators(
    points: tuple[MarginObservation, ...],
    denominators: Mapping[date, tuple[Decimal, Decimal, Decimal, str, datetime]],
) -> tuple[MarginObservation, ...]:
    merged: list[MarginObservation] = []
    for point in points:
        denominator = denominators.get(point.trade_date)
        if denominator is None:
            merged.append(point)
            continue
        close, volume, turnover, revision, captured_at = denominator
        merged.append(
            MarginObservation(
                trade_date=point.trade_date,
                financing_balance=point.financing_balance,
                short_balance=point.short_balance,
                total_balance=point.total_balance,
                source_id=point.source_id,
                source_revision=point.source_revision,
                captured_at=point.captured_at,
                free_float_market_cap=point.free_float_market_cap,
                turnover=turnover,
                close=close,
                volume=volume,
                denominator_trade_date=point.trade_date,
                denominator_source_id=_SOURCE_ID,
                denominator_source_revision=revision,
                denominator_captured_at=captured_at,
            )
        )
    return tuple(merged)


class _MarginArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def capture(
        self,
        scope: tuple[str, str],
        *,
        raw_payload: bytes,
        captured_at: datetime,
    ) -> _RawCapture:
        _ensure_root(self._root)
        key = _scope_key(scope)
        revision = hashlib.sha256(raw_payload).hexdigest()
        with _artifact_lock(self._root, key):
            artifact = self._root / "artifacts" / key / revision
            if not artifact.exists():
                _ensure_directory(artifact)
                _write_atomic(artifact, "raw.bin", raw_payload)
                _write_atomic(
                    artifact,
                    "manifest.json",
                    _json_bytes(
                        {
                            "schema_version": _ARTIFACT_SCHEMA,
                            "scope": list(scope),
                            "revision": revision,
                            "raw_payload_sha256": revision,
                        }
                    ),
                )
            _write_atomic(
                self._root / "latest",
                f"{key}.json",
                _json_bytes(
                    {
                        "schema_version": _ARTIFACT_SCHEMA,
                        "scope": list(scope),
                        "revision": revision,
                        "captured_at": captured_at.isoformat(),
                    }
                ),
            )
        return _RawCapture(raw_payload=raw_payload, revision=revision, captured_at=captured_at)

    def load_latest(self, scope: tuple[str, str]) -> _RawCapture | None:
        try:
            _ensure_root(self._root)
            key = _scope_key(scope)
            payload = _read_regular(self._root / "latest" / f"{key}.json")
            manifest = json.loads(payload)
            if (
                not isinstance(manifest, Mapping)
                or manifest.get("schema_version") != _ARTIFACT_SCHEMA
                or manifest.get("scope") != list(scope)
                or not isinstance(manifest.get("revision"), str)
                or not isinstance(manifest.get("captured_at"), str)
            ):
                return None
            revision = manifest["revision"]
            captured_at = datetime.fromisoformat(manifest["captured_at"])
            if captured_at.tzinfo is None or captured_at.utcoffset() is None:
                return None
            raw_payload = _read_regular(self._root / "artifacts" / key / revision / "raw.bin")
            if hashlib.sha256(raw_payload).hexdigest() != revision:
                return None
            return _RawCapture(raw_payload=raw_payload, revision=revision, captured_at=captured_at)
        except (OSError, RuntimeError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None


def _ensure_root(root: Path) -> None:
    _ensure_directory(root)
    _ensure_directory(root / "artifacts")
    _ensure_directory(root / "latest")
    _ensure_directory(root / "locks")


def _ensure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise EastmoneyMarginEvidenceError("MARGIN_EVIDENCE_ARTIFACT_ROOT_INVALID")


@contextmanager
def _artifact_lock(root: Path, key: str) -> Iterator[None]:
    path = root / "locks" / f"{key}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_atomic(directory: Path, name: str, payload: bytes) -> None:
    _ensure_directory(directory)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / name)
    except Exception:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _read_regular(path: Path) -> bytes:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError("margin artifact is not a regular file")
    with path.open("rb") as handle:
        return handle.read()


def _scope_key(scope: tuple[str, str]) -> str:
    return hashlib.sha256(_json_bytes(list(scope))).hexdigest()


def _rows(raw_payload: bytes) -> tuple[Mapping[str, object], ...]:
    try:
        payload = json.loads(raw_payload)
        result = payload.get("result") if isinstance(payload, Mapping) else None
        rows = result.get("data") if isinstance(result, Mapping) else None
    except (TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        rows = None
    if not isinstance(rows, list):
        raise EastmoneyMarginEvidenceError("MARGIN_EVIDENCE_PAYLOAD_INVALID")
    return tuple(row for row in rows if isinstance(row, Mapping))


def _date_value(value: object) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _text(value: object) -> str | None:
    if isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _symbol_parts(symbol: object) -> tuple[str, str] | None:
    if not isinstance(symbol, str) or len(symbol) != 9 or symbol[6] != ".":
        return None
    code, venue = symbol[:6], symbol[7:]
    if not code.isdigit() or venue not in {"SH", "SZ"}:
        return None
    return code, venue


def _require_symbol(symbol: str) -> tuple[str, str]:
    parts = _symbol_parts(symbol)
    if parts is None:
        raise EastmoneyMarginEvidenceError("MARGIN_EVIDENCE_SYMBOL_UNSUPPORTED")
    return parts


def _validate_request(request: MarginEvidenceRequest) -> None:
    if not isinstance(request, MarginEvidenceRequest):
        raise TypeError("margin evidence request is invalid")
    if request.as_of.tzinfo is None or request.as_of.utcoffset() is None:
        raise ValueError("margin evidence as_of must be timezone-aware")
    if request.deadline_at is not None and (
        request.deadline_at.tzinfo is None or request.deadline_at.utcoffset() is None
    ):
        raise ValueError("margin evidence deadline must be timezone-aware")


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise EastmoneyMarginEvidenceError("MARGIN_EVIDENCE_CLOCK_INVALID")
    return now


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


__all__ = ["EastmoneyMarginEvidenceError", "EastmoneyMarginEvidenceSource"]
