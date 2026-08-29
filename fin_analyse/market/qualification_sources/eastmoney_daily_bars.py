"""Frozen Eastmoney completed daily bars for formal technical evidence."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time as time_module
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import requests

from fin_analyse.market.data_qualification import ObservationEvidenceOrigin
from fin_analyse.market.providers.base import OHLCV
from fin_analyse.market.qualification_sources.eastmoney_http_transport import (
    _EastmoneyOnDemandHttpGet,
    _is_production_on_demand_http_get,
)
from fin_analyse.market.qualification_sources.eastmoney_request_contract import (
    EASTMONEY_DAILY_BAR_MAX_RAW_BYTES,
    EastmoneyHttpRequest,
    eastmoney_daily_bar_request,
)
from fin_analyse.market.qualified_daily_bars import (
    QualifiedDailyBarReadRequest,
    QualifiedDailyBarSeries,
)

_FETCH_LIMIT = 120
_A_SHARE_CLOSE_TIME = time(15, 0)
_SCHEMA_VERSION = "eastmoney_completed_daily_bars_artifact.v3"
_BASE_PROVIDER_VERSION = "eastmoney_completed_daily_bars.qfq.v2"
_PROVIDER_ID = "eastmoney_daily_bars"
_MARKET_BY_VENUE = {"SH": 1, "SZ": 0}
_SYMBOL = re.compile(r"^(?P<code>[0-9]{6})\.(?P<venue>SH|SZ)$")
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CN_TZ = ZoneInfo("Asia/Shanghai")
_MAX_MANIFEST_BYTES = 64 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class EastmoneyDailyBarSourceError(RuntimeError):
    """A stable fail-closed source or frozen-artifact error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


class EastmoneyDailyBarHttpGet(Protocol):
    """Minimal requests-compatible transport for one no-redirect GET."""

    def __call__(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _HttpResponse: ...


def _reject_reinitialization(instance: object, *slot_names: str) -> None:
    for slot_name in slot_names:
        try:
            object.__getattribute__(instance, slot_name)
        except AttributeError:
            continue
        raise RuntimeError("Eastmoney daily-bar source is already initialized")


class _ImmutableSourceConfiguration:
    """Reject ordinary source mutation; constructors write slots directly."""

    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Eastmoney daily-bar source configuration is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Eastmoney daily-bar source configuration is immutable")


@dataclass(frozen=True)
class _ReadScope:
    symbol: str
    code: str
    venue: str
    market: int
    trade_date: date
    completed_through_date: date
    decision_cutoff_at: datetime
    minimum_completed_bars: int


class EastmoneyDailyBarReader(_ImmutableSourceConfiguration):
    """Capture once, then replay one immutable Eastmoney daily-bar response."""

    __slots__ = ("_artifact_root", "_evidence_origin", "_http_get", "_timeout_seconds")

    _artifact_root: Path
    _evidence_origin: ObservationEvidenceOrigin
    _http_get: EastmoneyDailyBarHttpGet
    _timeout_seconds: float

    provider_id = _PROVIDER_ID
    adapter_version = _BASE_PROVIDER_VERSION

    def __init__(
        self,
        *,
        artifact_root: Path,
        http_get: EastmoneyDailyBarHttpGet | None = None,
        evidence_origin: ObservationEvidenceOrigin = ObservationEvidenceOrigin.TEST_ONLY,
        timeout_seconds: float = 15.0,
    ) -> None:
        _reject_reinitialization(self, "_evidence_origin")
        resolved_http_get = requests.get if http_get is None else http_get
        if not isinstance(artifact_root, Path):
            raise TypeError("artifact_root must be pathlib.Path")
        if type(evidence_origin) is not ObservationEvidenceOrigin:
            raise TypeError("evidence_origin must be ObservationEvidenceOrigin")
        if http_get is not None and evidence_origin is not ObservationEvidenceOrigin.TEST_ONLY:
            raise EastmoneyDailyBarSourceError(
                "EASTMONEY_DAILY_BAR_INJECTED_TRANSPORT_MUST_BE_TEST_ONLY"
            )
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be finite and positive")
        normalized_timeout_seconds = float(timeout_seconds)
        if not math.isfinite(normalized_timeout_seconds) or normalized_timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        object.__setattr__(self, "_artifact_root", artifact_root)
        object.__setattr__(self, "_http_get", resolved_http_get)
        object.__setattr__(self, "_evidence_origin", evidence_origin)
        object.__setattr__(self, "_timeout_seconds", normalized_timeout_seconds)

    @property
    def evidence_origin(self) -> ObservationEvidenceOrigin:
        return self._evidence_origin

    @property
    def provider_version(self) -> str:
        return _provider_version(self.evidence_origin)

    def read(self, request: QualifiedDailyBarReadRequest) -> QualifiedDailyBarSeries:
        """Return deterministic completed bars from a frozen point-in-time response."""

        scope = _validate_request(request)
        if (
            request.deadline_at is not None
            and _deadline_remaining_seconds(request.deadline_at) <= 0
        ):
            raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_DEADLINE_REACHED")
        _ensure_artifact_root(self._artifact_root)
        key = _artifact_key(scope, provider_version=self.provider_version)
        artifact_path = self._artifact_root / "artifacts" / key
        with _artifact_lock(
            self._artifact_root,
            key,
            deadline_at=request.deadline_at,
        ):
            if artifact_path.exists():
                manifest, raw_payload = _load_artifact(
                    artifact_path,
                    scope=scope,
                    provider_version=self.provider_version,
                    evidence_origin=self.evidence_origin,
                )
            else:
                status_code, raw_payload = self._capture(
                    scope,
                    deadline_at=request.deadline_at,
                )
                if (
                    request.deadline_at is not None
                    and _deadline_remaining_seconds(request.deadline_at) <= 0
                ):
                    raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_DEADLINE_REACHED")
                manifest = _manifest(
                    scope,
                    provider_version=self.provider_version,
                    evidence_origin=self.evidence_origin,
                    status_code=status_code,
                    raw_payload=raw_payload,
                )
                _publish_artifact(artifact_path, manifest=manifest, raw_payload=raw_payload)
        return _replay_series(
            scope,
            provider_version=self.provider_version,
            manifest=manifest,
            raw_payload=raw_payload,
        )

    def _capture(
        self,
        scope: _ReadScope,
        *,
        deadline_at: datetime | None,
    ) -> tuple[int, bytes]:
        timeout_seconds = self._timeout_seconds
        if deadline_at is not None:
            remaining = _deadline_remaining_seconds(deadline_at)
            if remaining <= 0:
                raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_DEADLINE_REACHED")
            timeout_seconds = min(timeout_seconds, remaining)
        request = _request_spec(scope)
        try:
            response = self._fetch_request(
                request,
                timeout=timeout_seconds,
            )
        except Exception as error:
            raise EastmoneyDailyBarSourceError(
                "EASTMONEY_DAILY_BAR_TRANSPORT_UNAVAILABLE"
            ) from error
        status_code = getattr(response, "status_code", None)
        raw_payload = getattr(response, "content", None)
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not isinstance(raw_payload, bytes)
            or len(raw_payload) > request.maximum_payload_bytes
        ):
            raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_HTTP_RESPONSE_INVALID")
        return status_code, raw_payload

    def _fetch_request(
        self,
        request: EastmoneyHttpRequest,
        *,
        timeout: float,
    ) -> _HttpResponse:
        return self._http_get(
            request.endpoint,
            params=request.params_dict(),
            headers=request.headers_dict(),
            timeout=timeout,
            allow_redirects=False,
        )


class _OnDemandEastmoneyDailyBarReader(EastmoneyDailyBarReader):
    """Composition-owned LIVE daily reader with the bounded fallback."""

    __slots__ = ("__on_demand_transport",)

    __on_demand_transport: _EastmoneyOnDemandHttpGet

    def __init__(
        self,
        *,
        artifact_root: Path,
        transport: _EastmoneyOnDemandHttpGet,
        timeout_seconds: float,
    ) -> None:
        _reject_reinitialization(
            self,
            "_evidence_origin",
            "_OnDemandEastmoneyDailyBarReader__on_demand_transport",
        )
        if not _is_production_on_demand_http_get(transport):
            raise TypeError("on-demand Eastmoney daily reader requires production transport")
        super().__init__(
            artifact_root=artifact_root,
            evidence_origin=ObservationEvidenceOrigin.LIVE_CAPTURE,
            timeout_seconds=timeout_seconds,
        )
        object.__setattr__(
            self,
            "_OnDemandEastmoneyDailyBarReader__on_demand_transport",
            transport,
        )

    def _fetch_request(
        self,
        request: EastmoneyHttpRequest,
        *,
        timeout: float,
    ) -> _HttpResponse:
        return self.__on_demand_transport.fetch(request, timeout=timeout)


def _build_on_demand_eastmoney_daily_bar_reader(
    *,
    artifact_root: Path,
    transport: _EastmoneyOnDemandHttpGet,
    timeout_seconds: float,
) -> EastmoneyDailyBarReader:
    return _OnDemandEastmoneyDailyBarReader(
        artifact_root=artifact_root,
        transport=transport,
        timeout_seconds=timeout_seconds,
    )


class EastmoneyDailyBarReplayReader(_ImmutableSourceConfiguration):
    """Read one already-frozen daily-bar artifact without any write or network."""

    __slots__ = ("_artifact_root", "_evidence_origin")

    _artifact_root: Path
    _evidence_origin: ObservationEvidenceOrigin

    provider_id = _PROVIDER_ID
    adapter_version = _BASE_PROVIDER_VERSION

    def __init__(
        self,
        *,
        artifact_root: Path,
        evidence_origin: ObservationEvidenceOrigin = ObservationEvidenceOrigin.TEST_ONLY,
    ) -> None:
        _reject_reinitialization(self, "_evidence_origin")
        if not isinstance(artifact_root, Path):
            raise TypeError("artifact_root must be pathlib.Path")
        if type(evidence_origin) is not ObservationEvidenceOrigin:
            raise TypeError("evidence_origin must be ObservationEvidenceOrigin")
        object.__setattr__(self, "_artifact_root", artifact_root)
        object.__setattr__(self, "_evidence_origin", evidence_origin)

    @property
    def evidence_origin(self) -> ObservationEvidenceOrigin:
        return self._evidence_origin

    @property
    def provider_version(self) -> str:
        return _provider_version(self.evidence_origin)

    def read(self, request: QualifiedDailyBarReadRequest) -> QualifiedDailyBarSeries:
        """Replay an exact same-day raw artifact without creating or repairing state."""

        scope = _validate_request(request)
        _require_replay_artifact_root(self._artifact_root)
        key = _artifact_key(scope, provider_version=self.provider_version)
        artifact_path = self._artifact_root / "artifacts" / key
        _require_replay_artifact_present(artifact_path)
        manifest, raw_payload = _load_artifact(
            artifact_path,
            scope=scope,
            provider_version=self.provider_version,
            evidence_origin=self.evidence_origin,
        )
        return _replay_series(
            scope,
            provider_version=self.provider_version,
            manifest=manifest,
            raw_payload=raw_payload,
        )


def _provider_version(evidence_origin: ObservationEvidenceOrigin) -> str:
    suffix = (
        "test_only" if evidence_origin is ObservationEvidenceOrigin.TEST_ONLY else "live_capture"
    )
    return f"{_BASE_PROVIDER_VERSION}.{suffix}"


def _validate_request(request: QualifiedDailyBarReadRequest) -> _ReadScope:
    if not isinstance(request, QualifiedDailyBarReadRequest):
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_REQUEST_INVALID")
    match = _SYMBOL.fullmatch(request.symbol) if isinstance(request.symbol, str) else None
    if match is None or not isinstance(request.trade_date, date):
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_REQUEST_INVALID")
    cutoff = request.decision_cutoff_at
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_REQUEST_INVALID")
    deadline_at = request.deadline_at
    if deadline_at is not None and (
        not isinstance(deadline_at, datetime)
        or deadline_at.tzinfo is None
        or deadline_at.utcoffset() is None
    ):
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_REQUEST_INVALID")
    local_cutoff = cutoff.astimezone(_CN_TZ)
    if local_cutoff.date() != request.trade_date:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_REQUEST_INVALID")
    qualified_completed_through_date = (
        request.trade_date
        if local_cutoff.time() >= _A_SHARE_CLOSE_TIME
        else request.trade_date - timedelta(days=1)
    )
    minimum = request.minimum_completed_bars
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or not 1 <= minimum <= _FETCH_LIMIT
    ):
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_REQUEST_INVALID")
    venue = match.group("venue")
    return _ReadScope(
        symbol=request.symbol,
        code=match.group("code"),
        venue=venue,
        market=_MARKET_BY_VENUE[venue],
        trade_date=request.trade_date,
        completed_through_date=qualified_completed_through_date,
        decision_cutoff_at=local_cutoff,
        minimum_completed_bars=minimum,
    )


def _request_spec(scope: _ReadScope) -> EastmoneyHttpRequest:
    return eastmoney_daily_bar_request(
        symbol=scope.code,
        venue=scope.venue.lower(),
        completed_through=scope.completed_through_date.strftime("%Y%m%d"),
    )


def _artifact_key(scope: _ReadScope, *, provider_version: str) -> str:
    return _sha256(
        _canonical_bytes(
            {
                "provider_version": provider_version,
                "symbol": scope.symbol,
                "trade_date": scope.trade_date.isoformat(),
                "completed_through_date": scope.completed_through_date.isoformat(),
            }
        )
    )


def _manifest(
    scope: _ReadScope,
    *,
    provider_version: str,
    evidence_origin: ObservationEvidenceOrigin,
    status_code: int,
    raw_payload: bytes,
) -> dict[str, object]:
    key = _artifact_key(scope, provider_version=provider_version)
    request = _request_spec(scope)
    manifest: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "artifact_key": key,
        "provider_id": _PROVIDER_ID,
        "provider_version": provider_version,
        "evidence_origin": evidence_origin.value,
        "adjustment": "FORWARD_ADJUSTED_QFQ",
        "request": {
            "symbol": scope.symbol,
            "trade_date": scope.trade_date.isoformat(),
            "completed_through_date": scope.completed_through_date.isoformat(),
        },
        "http": {
            "endpoint": request.endpoint,
            "params": request.params_dict(),
            "allow_redirects": False,
            "status_code": status_code,
        },
        "response_sha256": _sha256(raw_payload),
        "response_size": len(raw_payload),
    }
    manifest["manifest_sha256"] = _sha256(_canonical_bytes(manifest))
    return manifest


def _replay_series(
    scope: _ReadScope,
    *,
    provider_version: str,
    manifest: Mapping[str, object],
    raw_payload: bytes,
) -> QualifiedDailyBarSeries:
    http = manifest.get("http")
    if not isinstance(http, dict) or http.get("status_code") != 200:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_HTTP_STATUS_ERROR")
    try:
        payload = _strict_json_loads(raw_payload)
        data = payload.get("data")
        response_code = payload.get("rc")
        if (
            isinstance(response_code, bool)
            or not isinstance(response_code, int)
            or response_code != 0
            or not isinstance(data, dict)
        ):
            raise ValueError("invalid response envelope")
        response_symbol = data.get("code")
        response_market = data.get("market")
        if (
            not isinstance(response_symbol, str)
            or _SYMBOL.fullmatch(f"{response_symbol}.{scope.venue}") is None
            or response_symbol != scope.code
            or isinstance(response_market, bool)
            or not isinstance(response_market, int)
            or response_market != scope.market
        ):
            raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_SECURITY_MISMATCH")
        rows = data.get("klines")
        if not isinstance(rows, list) or not rows:
            raise ValueError("daily bars missing")
        completed = _parse_completed_rows(
            rows,
            trade_date=scope.trade_date,
            completed_through_date=scope.completed_through_date,
        )
    except EastmoneyDailyBarSourceError:
        raise
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_PAYLOAD_INVALID") from error
    if len(completed) < scope.minimum_completed_bars:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_HISTORY_INSUFFICIENT")
    source_revision = manifest.get("response_sha256")
    if not isinstance(source_revision, str) or _SHA256.fullmatch(source_revision) is None:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_INVALID")
    return QualifiedDailyBarSeries(
        symbol=scope.symbol,
        provider_id=_PROVIDER_ID,
        provider_version=provider_version,
        completed_bars=completed,
        adjustment="FORWARD_ADJUSTED_QFQ",
        source_revision=source_revision,
    )


def _strict_json_loads(raw_payload: bytes) -> dict[str, object]:
    text = raw_payload.decode("utf-8", errors="strict")

    def reject_constant(value: str) -> object:
        raise ValueError(f"invalid JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    parsed = json.loads(
        text,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
    if not isinstance(parsed, dict):
        raise ValueError("response envelope must be an object")
    return parsed


def _parse_completed_rows(
    rows: list[object],
    *,
    trade_date: date,
    completed_through_date: date,
) -> tuple[OHLCV, ...]:
    completed: list[OHLCV] = []
    previous_date: date | None = None
    for raw_row in rows:
        if not isinstance(raw_row, str):
            raise ValueError("daily bar row must be text")
        fields = raw_row.split(",")
        if len(fields) != 11:
            raise ValueError("daily bar row is truncated")
        parsed_date = date.fromisoformat(fields[0])
        if fields[0] != parsed_date.isoformat():
            raise ValueError("daily bar date is not canonical")
        if previous_date is not None and parsed_date <= previous_date:
            raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_DATES_UNORDERED_OR_DUPLICATE")
        previous_date = parsed_date
        if parsed_date > trade_date:
            raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_FUTURE_DATE")
        if parsed_date > completed_through_date:
            continue
        open_price = _number("open", fields[1], positive=True)
        close = _number("close", fields[2], positive=True)
        high = _number("high", fields[3], positive=True)
        low = _number("low", fields[4], positive=True)
        volume = _number("volume", fields[5], positive=False)
        turnover = _number("turnover", fields[6], positive=False)
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise ValueError("daily bar OHLC relationship is invalid")
        completed.append(
            OHLCV(
                date=parsed_date.isoformat(),
                open=float(open_price),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume),
                turnover=float(turnover),
            )
        )
    return tuple(completed)


def _number(field_name: str, value: str, *, positive: bool) -> Decimal:
    if _DECIMAL.fullmatch(value) is None:
        raise ValueError(f"daily bar {field_name} is not numeric")
    try:
        parsed = Decimal(value)
        as_float = float(parsed)
    except (InvalidOperation, OverflowError, ValueError) as error:
        raise ValueError(f"daily bar {field_name} is not numeric") from error
    if (
        not parsed.is_finite()
        or not math.isfinite(as_float)
        or parsed < 0
        or (positive and parsed == 0)
    ):
        raise ValueError(f"daily bar {field_name} is outside its domain")
    return parsed


def _ensure_artifact_root(root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_secure_directory(root)
        for child in (root / "artifacts", root / "locks"):
            child.mkdir(mode=0o700, exist_ok=True)
            _require_secure_directory(child)
    except EastmoneyDailyBarSourceError:
        raise
    except OSError as error:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_PATH_INVALID") from error


def _require_secure_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_PATH_INVALID")


def _require_replay_artifact_root(root: Path) -> None:
    try:
        _require_secure_directory(root)
        _require_secure_directory(root / "artifacts")
    except FileNotFoundError as error:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_NOT_FOUND") from error
    except EastmoneyDailyBarSourceError:
        raise
    except OSError as error:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_PATH_INVALID") from error


def _require_replay_artifact_present(artifact_path: Path) -> None:
    try:
        _require_secure_directory(artifact_path)
    except FileNotFoundError as error:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_NOT_FOUND") from error
    except EastmoneyDailyBarSourceError:
        raise
    except OSError as error:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_INVALID") from error


@contextmanager
def _artifact_lock(
    root: Path,
    key: str,
    *,
    deadline_at: datetime | None,
) -> Iterator[None]:
    lock_path = root / "locks" / f"{key}.lock"
    descriptor = -1
    acquired = False
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | _CLOEXEC | _NOFOLLOW,
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_PATH_INVALID")
        os.fchmod(descriptor, 0o600)
        if deadline_at is None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:
            while True:
                remaining = _deadline_remaining_seconds(deadline_at)
                if remaining <= 0:
                    raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_LOCK_TIMEOUT")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time_module.sleep(min(0.01, remaining))
        acquired = True
        yield
    except EastmoneyDailyBarSourceError:
        raise
    except OSError as error:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_LOCK_FAILED") from error
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _deadline_remaining_seconds(deadline_at: datetime) -> float:
    return max(
        0.0,
        (deadline_at - datetime.now(tz=deadline_at.tzinfo)).total_seconds(),
    )


def _publish_artifact(
    artifact_path: Path,
    *,
    manifest: Mapping[str, object],
    raw_payload: bytes,
) -> None:
    parent = artifact_path.parent
    temporary = Path(tempfile.mkdtemp(prefix=f".{artifact_path.name}.", dir=parent))
    published = False
    try:
        os.chmod(temporary, 0o700)
        _write_new_file(temporary / "response.bin", raw_payload)
        _write_new_file(temporary / "manifest.json", _canonical_bytes(manifest) + b"\n")
        _fsync_directory(temporary)
        if artifact_path.exists():
            raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_CONFLICT")
        os.rename(temporary, artifact_path)
        published = True
        _fsync_directory(parent)
    except EastmoneyDailyBarSourceError:
        raise
    except OSError as error:
        code = (
            "EASTMONEY_DAILY_BAR_ARTIFACT_WRITE_OUTCOME_UNKNOWN"
            if published
            else "EASTMONEY_DAILY_BAR_ARTIFACT_WRITE_FAILED"
        )
        raise EastmoneyDailyBarSourceError(code) from error
    finally:
        if not published:
            with contextlib.suppress(OSError):
                shutil.rmtree(temporary)


def _write_new_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_artifact(
    artifact_path: Path,
    *,
    scope: _ReadScope,
    provider_version: str,
    evidence_origin: ObservationEvidenceOrigin,
) -> tuple[dict[str, object], bytes]:
    try:
        _require_secure_directory(artifact_path)
        raw_payload = _read_secure_file(
            artifact_path / "response.bin",
            EASTMONEY_DAILY_BAR_MAX_RAW_BYTES,
        )
        manifest_bytes = _read_secure_file(
            artifact_path / "manifest.json",
            _MAX_MANIFEST_BYTES,
        )
        manifest = _strict_json_loads(manifest_bytes)
    except EastmoneyDailyBarSourceError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_INVALID") from error
    expected_key = _artifact_key(scope, provider_version=provider_version)
    if artifact_path.name != expected_key or not _manifest_matches(
        manifest,
        scope=scope,
        provider_version=provider_version,
        evidence_origin=evidence_origin,
        raw_payload=raw_payload,
    ):
        raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_INVALID")
    return manifest, raw_payload


def _read_secure_file(path: Path, maximum_size: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | _CLOEXEC | _NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > maximum_size
        ):
            raise EastmoneyDailyBarSourceError("EASTMONEY_DAILY_BAR_ARTIFACT_INVALID")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read(maximum_size + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _manifest_matches(
    manifest: Mapping[str, object],
    *,
    scope: _ReadScope,
    provider_version: str,
    evidence_origin: ObservationEvidenceOrigin,
    raw_payload: bytes,
) -> bool:
    manifest_without_hash = dict(manifest)
    manifest_sha256 = manifest_without_hash.pop("manifest_sha256", None)
    if (
        not isinstance(manifest_sha256, str)
        or _SHA256.fullmatch(manifest_sha256) is None
        or manifest_sha256 != _sha256(_canonical_bytes(manifest_without_hash))
    ):
        return False
    expected = _manifest(
        scope,
        provider_version=provider_version,
        evidence_origin=evidence_origin,
        status_code=_manifest_status_code(manifest),
        raw_payload=raw_payload,
    )
    response_sha256 = manifest.get("response_sha256")
    return (
        manifest == expected
        and isinstance(response_sha256, str)
        and _SHA256.fullmatch(response_sha256) is not None
    )


def _manifest_status_code(manifest: Mapping[str, object]) -> int:
    http = manifest.get("http")
    if not isinstance(http, dict):
        return -1
    value = http.get("status_code")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | _CLOEXEC | _NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "EastmoneyDailyBarHttpGet",
    "EastmoneyDailyBarReader",
    "EastmoneyDailyBarReplayReader",
    "EastmoneyDailyBarSourceError",
]
