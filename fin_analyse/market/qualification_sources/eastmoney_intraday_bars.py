"""Frozen Eastmoney completed 30-minute bars for advisory technical evidence."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import requests

from fin_analyse.market.data_qualification import ObservationEvidenceOrigin
from fin_analyse.market.providers.base import OHLCV
from fin_analyse.market.qualification_sources.eastmoney_daily_bars import (
    EastmoneyDailyBarSourceError,
    _artifact_lock,
    _canonical_bytes,
    _deadline_remaining_seconds,
    _ensure_artifact_root,
    _publish_artifact,
    _read_secure_file,
    _require_secure_directory,
    _sha256,
    _strict_json_loads,
)
from fin_analyse.market.qualification_sources.eastmoney_http_transport import (
    _EastmoneyOnDemandHttpGet,
    _is_production_on_demand_http_get,
)
from fin_analyse.market.qualification_sources.eastmoney_request_contract import (
    EASTMONEY_THIRTY_MINUTE_BAR_MAX_RAW_BYTES,
    EastmoneyHttpRequest,
    eastmoney_thirty_minute_bar_request,
)
from fin_analyse.market.qualified_intraday_bars import (
    QualifiedThirtyMinuteBarReadRequest,
    QualifiedThirtyMinuteBarSeries,
)

_BASE_PROVIDER_VERSION = "eastmoney_completed_thirty_minute_bars.qfq.v1"
_PROVIDER_ID = "eastmoney_thirty_minute_bars"
_SCHEMA_VERSION = "eastmoney_completed_thirty_minute_bars_artifact.v1"
_CN_TZ = ZoneInfo("Asia/Shanghai")
_SYMBOL = re.compile(r"^(?P<code>[0-9]{6})\.(?P<venue>SH|SZ)$")
_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MARKET_BY_VENUE = {"SH": 1, "SZ": 0}
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_BARS = 800
_SESSION_STARTS = frozenset(
    {
        time(9, 30),
        time(10, 0),
        time(10, 30),
        time(11, 0),
        time(13, 0),
        time(13, 30),
        time(14, 0),
        time(14, 30),
    }
)
_COMPLETION_TIMES = (
    time(10, 0),
    time(10, 30),
    time(11, 0),
    time(11, 30),
    time(13, 30),
    time(14, 0),
    time(14, 30),
    time(15, 0),
)


class EastmoneyThirtyMinuteBarSourceError(RuntimeError):
    """Stable fail-closed 30-minute source or artifact error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


class EastmoneyThirtyMinuteBarHttpGet(Protocol):
    def __call__(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _HttpResponse: ...


@dataclass(frozen=True)
class _ReadScope:
    symbol: str
    code: str
    venue: str
    market: int
    trade_date: date
    decision_cutoff_at: datetime
    completed_through_at: datetime | None
    minimum_completed_bars: int


class EastmoneyThirtyMinuteBarReader:
    """Capture one qfq 30-minute response and replay only completed bars."""

    __slots__ = ("_artifact_root", "_evidence_origin", "_http_get", "_timeout_seconds")

    _artifact_root: Path
    _evidence_origin: ObservationEvidenceOrigin
    _http_get: EastmoneyThirtyMinuteBarHttpGet
    _timeout_seconds: float

    provider_id = _PROVIDER_ID
    adapter_version = _BASE_PROVIDER_VERSION

    def __init__(
        self,
        *,
        artifact_root: Path,
        http_get: EastmoneyThirtyMinuteBarHttpGet | None = None,
        evidence_origin: ObservationEvidenceOrigin = ObservationEvidenceOrigin.TEST_ONLY,
        timeout_seconds: float = 15.0,
    ) -> None:
        if hasattr(self, "_evidence_origin"):
            raise RuntimeError("Eastmoney 30-minute source is already initialized")
        if not isinstance(artifact_root, Path):
            raise TypeError("artifact_root must be pathlib.Path")
        if type(evidence_origin) is not ObservationEvidenceOrigin:
            raise TypeError("evidence_origin must be ObservationEvidenceOrigin")
        if http_get is not None and evidence_origin is not ObservationEvidenceOrigin.TEST_ONLY:
            raise EastmoneyThirtyMinuteBarSourceError(
                "EASTMONEY_THIRTY_MINUTE_INJECTED_TRANSPORT_MUST_BE_TEST_ONLY"
            )
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be finite and positive")
        normalized_timeout = float(timeout_seconds)
        if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        object.__setattr__(self, "_artifact_root", artifact_root)
        object.__setattr__(self, "_http_get", requests.get if http_get is None else http_get)
        object.__setattr__(self, "_evidence_origin", evidence_origin)
        object.__setattr__(self, "_timeout_seconds", normalized_timeout)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Eastmoney 30-minute source configuration is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Eastmoney 30-minute source configuration is immutable")

    @property
    def evidence_origin(self) -> ObservationEvidenceOrigin:
        return self._evidence_origin

    @property
    def provider_version(self) -> str:
        suffix = (
            "test_only"
            if self.evidence_origin is ObservationEvidenceOrigin.TEST_ONLY
            else "live_capture"
        )
        return f"{_BASE_PROVIDER_VERSION}.{suffix}"

    def read(
        self,
        request: QualifiedThirtyMinuteBarReadRequest,
    ) -> QualifiedThirtyMinuteBarSeries:
        scope = _validate_request(request)
        _require_remaining_deadline(request.deadline_at)
        try:
            _ensure_artifact_root(self._artifact_root)
            key = _artifact_key(scope, provider_version=self.provider_version)
            artifact_path = self._artifact_root / "artifacts" / key
            with _artifact_lock(self._artifact_root, key, deadline_at=request.deadline_at):
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
                    _require_remaining_deadline(request.deadline_at)
                    manifest = _manifest(
                        scope,
                        provider_version=self.provider_version,
                        evidence_origin=self.evidence_origin,
                        status_code=status_code,
                        raw_payload=raw_payload,
                    )
                    _publish_artifact(artifact_path, manifest=manifest, raw_payload=raw_payload)
        except EastmoneyThirtyMinuteBarSourceError:
            raise
        except EastmoneyDailyBarSourceError as error:
            raise EastmoneyThirtyMinuteBarSourceError(_storage_error_code(error.code)) from error
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
        timeout = self._timeout_seconds
        if deadline_at is not None:
            remaining = _deadline_remaining_seconds(deadline_at)
            if remaining <= 0:
                raise EastmoneyThirtyMinuteBarSourceError(
                    "EASTMONEY_THIRTY_MINUTE_DEADLINE_REACHED"
                )
            timeout = min(timeout, remaining)
        spec = _request_spec(scope)
        try:
            response = self._fetch_request(spec, timeout=timeout)
        except Exception as error:
            raise EastmoneyThirtyMinuteBarSourceError(
                "EASTMONEY_THIRTY_MINUTE_TRANSPORT_UNAVAILABLE"
            ) from error
        status_code = getattr(response, "status_code", None)
        raw_payload = getattr(response, "content", None)
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not isinstance(raw_payload, bytes)
            or len(raw_payload) > spec.maximum_payload_bytes
        ):
            raise EastmoneyThirtyMinuteBarSourceError(
                "EASTMONEY_THIRTY_MINUTE_HTTP_RESPONSE_INVALID"
            )
        return status_code, raw_payload

    def _fetch_request(self, spec: EastmoneyHttpRequest, *, timeout: float) -> _HttpResponse:
        return self._http_get(
            spec.endpoint,
            params=spec.params_dict(),
            headers=spec.headers_dict(),
            timeout=timeout,
            allow_redirects=False,
        )


class _OnDemandEastmoneyThirtyMinuteBarReader(EastmoneyThirtyMinuteBarReader):
    __slots__ = ("__transport",)

    __transport: _EastmoneyOnDemandHttpGet

    def __init__(
        self,
        *,
        artifact_root: Path,
        transport: _EastmoneyOnDemandHttpGet,
        timeout_seconds: float,
    ) -> None:
        if not _is_production_on_demand_http_get(transport):
            raise TypeError("on-demand Eastmoney 30-minute reader requires production transport")
        super().__init__(
            artifact_root=artifact_root,
            evidence_origin=ObservationEvidenceOrigin.LIVE_CAPTURE,
            timeout_seconds=timeout_seconds,
        )
        object.__setattr__(self, "_OnDemandEastmoneyThirtyMinuteBarReader__transport", transport)

    def _fetch_request(self, spec: EastmoneyHttpRequest, *, timeout: float) -> _HttpResponse:
        return self.__transport.fetch(spec, timeout=timeout)


def _build_on_demand_eastmoney_thirty_minute_bar_reader(
    *,
    artifact_root: Path,
    transport: _EastmoneyOnDemandHttpGet,
    timeout_seconds: float,
) -> EastmoneyThirtyMinuteBarReader:
    return _OnDemandEastmoneyThirtyMinuteBarReader(
        artifact_root=artifact_root,
        transport=transport,
        timeout_seconds=timeout_seconds,
    )


def _validate_request(request: QualifiedThirtyMinuteBarReadRequest) -> _ReadScope:
    if not isinstance(request, QualifiedThirtyMinuteBarReadRequest):
        raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_REQUEST_INVALID")
    match = _SYMBOL.fullmatch(request.symbol) if isinstance(request.symbol, str) else None
    cutoff = request.decision_cutoff_at
    if (
        match is None
        or not isinstance(request.trade_date, date)
        or not isinstance(cutoff, datetime)
        or cutoff.tzinfo is None
        or cutoff.utcoffset() is None
    ):
        raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_REQUEST_INVALID")
    if request.deadline_at is not None and (
        not isinstance(request.deadline_at, datetime)
        or request.deadline_at.tzinfo is None
        or request.deadline_at.utcoffset() is None
    ):
        raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_REQUEST_INVALID")
    local_cutoff = cutoff.astimezone(_CN_TZ)
    minimum = request.minimum_completed_bars
    if (
        local_cutoff.date() != request.trade_date
        or isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or not 1 <= minimum <= _MAX_BARS
    ):
        raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_REQUEST_INVALID")
    return _ReadScope(
        symbol=request.symbol,
        code=match.group("code"),
        venue=match.group("venue"),
        market=_MARKET_BY_VENUE[match.group("venue")],
        trade_date=request.trade_date,
        decision_cutoff_at=local_cutoff,
        completed_through_at=_completed_through_at(local_cutoff),
        minimum_completed_bars=minimum,
    )


def _require_remaining_deadline(deadline_at: datetime | None) -> None:
    if deadline_at is not None and _deadline_remaining_seconds(deadline_at) <= 0:
        raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_DEADLINE_REACHED")


def _completed_through_at(cutoff: datetime) -> datetime | None:
    for completed_at in reversed(_COMPLETION_TIMES):
        if cutoff.time() >= completed_at:
            return datetime.combine(cutoff.date(), completed_at, tzinfo=_CN_TZ)
    return None


def _request_spec(scope: _ReadScope) -> EastmoneyHttpRequest:
    return eastmoney_thirty_minute_bar_request(
        symbol=scope.code,
        venue=scope.venue.lower(),
        trade_date=scope.trade_date.strftime("%Y%m%d"),
    )


def _artifact_key(scope: _ReadScope, *, provider_version: str) -> str:
    return _sha256(
        _canonical_bytes(
            {
                "provider_version": provider_version,
                "symbol": scope.symbol,
                "trade_date": scope.trade_date.isoformat(),
                "completed_through_at": (
                    scope.completed_through_at.isoformat()
                    if scope.completed_through_at is not None
                    else None
                ),
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
    spec = _request_spec(scope)
    manifest: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "artifact_key": _artifact_key(scope, provider_version=provider_version),
        "provider_id": _PROVIDER_ID,
        "provider_version": provider_version,
        "evidence_origin": evidence_origin.value,
        "adjustment": "FORWARD_ADJUSTED_QFQ",
        "request": {
            "symbol": scope.symbol,
            "trade_date": scope.trade_date.isoformat(),
            "completed_through_at": (
                scope.completed_through_at.isoformat()
                if scope.completed_through_at is not None
                else None
            ),
        },
        "http": {
            "endpoint": spec.endpoint,
            "params": spec.params_dict(),
            "allow_redirects": False,
            "status_code": status_code,
        },
        "response_sha256": _sha256(raw_payload),
        "response_size": len(raw_payload),
    }
    manifest["manifest_sha256"] = _sha256(_canonical_bytes(manifest))
    return manifest


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
            EASTMONEY_THIRTY_MINUTE_BAR_MAX_RAW_BYTES,
        )
        manifest = _strict_json_loads(
            _read_secure_file(artifact_path / "manifest.json", _MAX_MANIFEST_BYTES)
        )
    except Exception as error:
        raise EastmoneyThirtyMinuteBarSourceError(
            "EASTMONEY_THIRTY_MINUTE_ARTIFACT_INVALID"
        ) from error
    if artifact_path.name != _artifact_key(
        scope, provider_version=provider_version
    ) or not _manifest_matches(
        manifest,
        scope=scope,
        provider_version=provider_version,
        evidence_origin=evidence_origin,
        raw_payload=raw_payload,
    ):
        raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_ARTIFACT_INVALID")
    return manifest, raw_payload


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
    http = manifest.get("http")
    status_code = http.get("status_code") if isinstance(http, dict) else None
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        return False
    return manifest == _manifest(
        scope,
        provider_version=provider_version,
        evidence_origin=evidence_origin,
        status_code=status_code,
        raw_payload=raw_payload,
    )


def _replay_series(
    scope: _ReadScope,
    *,
    provider_version: str,
    manifest: Mapping[str, object],
    raw_payload: bytes,
) -> QualifiedThirtyMinuteBarSeries:
    http = manifest.get("http")
    if not isinstance(http, dict) or http.get("status_code") != 200:
        raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_HTTP_STATUS_ERROR")
    try:
        payload = _strict_json_loads(raw_payload)
        data = payload.get("data")
        if payload.get("rc") != 0 or not isinstance(data, dict):
            raise ValueError("invalid response envelope")
        if data.get("code") != scope.code or data.get("market") != scope.market:
            raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_SECURITY_MISMATCH")
        rows = data.get("klines")
        if not isinstance(rows, list) or not rows:
            raise ValueError("30-minute bars missing")
        completed = _parse_completed_rows(rows, scope=scope)
    except EastmoneyThirtyMinuteBarSourceError:
        raise
    except (UnicodeError, ValueError, TypeError) as error:
        raise EastmoneyThirtyMinuteBarSourceError(
            "EASTMONEY_THIRTY_MINUTE_PAYLOAD_INVALID"
        ) from error
    if len(completed) < scope.minimum_completed_bars:
        raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_HISTORY_INSUFFICIENT")
    revision = manifest.get("response_sha256")
    if not isinstance(revision, str) or _SHA256.fullmatch(revision) is None:
        raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_ARTIFACT_INVALID")
    return QualifiedThirtyMinuteBarSeries(
        symbol=scope.symbol,
        provider_id=_PROVIDER_ID,
        provider_version=provider_version,
        adjustment="FORWARD_ADJUSTED_QFQ",
        source_revision=revision,
        completed_bars=completed,
    )


def _parse_completed_rows(
    rows: list[object],
    *,
    scope: _ReadScope,
) -> tuple[OHLCV, ...]:
    completed: list[OHLCV] = []
    previous: datetime | None = None
    for raw_row in rows:
        if not isinstance(raw_row, str):
            raise ValueError("30-minute bar row must be text")
        fields = raw_row.split(",")
        if len(fields) != 11:
            raise ValueError("30-minute bar row is truncated")
        start = datetime.strptime(fields[0], "%Y-%m-%d %H:%M").replace(tzinfo=_CN_TZ)
        if fields[0] != start.strftime("%Y-%m-%d %H:%M") or start.time() not in _SESSION_STARTS:
            raise ValueError("30-minute bar timestamp is invalid")
        if previous is not None and start <= previous:
            raise EastmoneyThirtyMinuteBarSourceError(
                "EASTMONEY_THIRTY_MINUTE_TIMESTAMPS_UNORDERED_OR_DUPLICATE"
            )
        previous = start
        if start.date() > scope.trade_date:
            raise EastmoneyThirtyMinuteBarSourceError("EASTMONEY_THIRTY_MINUTE_FUTURE_BAR")
        if start + timedelta(minutes=30) > scope.decision_cutoff_at:
            continue
        open_price = _number(fields[1], positive=True)
        close = _number(fields[2], positive=True)
        high = _number(fields[3], positive=True)
        low = _number(fields[4], positive=True)
        volume = _number(fields[5], positive=False)
        turnover = _number(fields[6], positive=False)
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise ValueError("30-minute bar OHLC relationship is invalid")
        completed.append(
            OHLCV(
                date=start.isoformat(),
                open=float(open_price),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume),
                turnover=float(turnover),
            )
        )
    return tuple(completed)


def _number(value: str, *, positive: bool) -> Decimal:
    if _DECIMAL.fullmatch(value) is None:
        raise ValueError("30-minute bar numeric field is invalid")
    try:
        parsed = Decimal(value)
        as_float = float(parsed)
    except (InvalidOperation, OverflowError, ValueError) as error:
        raise ValueError("30-minute bar numeric field is invalid") from error
    if (
        not parsed.is_finite()
        or not math.isfinite(as_float)
        or parsed < 0
        or (positive and parsed == 0)
    ):
        raise ValueError("30-minute bar numeric field is outside its domain")
    return parsed


def _storage_error_code(code: str) -> str:
    if code.endswith("LOCK_TIMEOUT"):
        return "EASTMONEY_THIRTY_MINUTE_ARTIFACT_LOCK_TIMEOUT"
    if "DEADLINE" in code:
        return "EASTMONEY_THIRTY_MINUTE_DEADLINE_REACHED"
    return "EASTMONEY_THIRTY_MINUTE_ARTIFACT_INVALID"


__all__ = [
    "EastmoneyThirtyMinuteBarReader",
    "EastmoneyThirtyMinuteBarSourceError",
]
