"""Source-only Eastmoney quote adapter that preserves upstream response bytes."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic_ns as _system_monotonic_ns
from typing import Protocol
from zoneinfo import ZoneInfo

import requests

from fin_analyse.market.data_qualification import (
    ObservationEvidenceOrigin,
    QualificationNormalizedRecord,
    QualificationSample,
    QualificationSourceCapture,
    TradingStatus,
)
from fin_analyse.market.qualification_sources.eastmoney_http_transport import (
    _EastmoneyOnDemandHttpGet,
    _is_production_on_demand_http_get,
)
from fin_analyse.market.qualification_sources.eastmoney_request_contract import (
    EastmoneyHttpRequest,
    eastmoney_quote_request,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MARKET_BY_VENUE = {"sh": 1, "sz": 0}
_VENUE_BY_MARKET = {market: venue for venue, market in _MARKET_BY_VENUE.items()}
# Verified 2026-07-18 against Eastmoney's official quote frontend contract:
# https://quote.eastmoney.com/newstatic/build/vendor.js
# Its trading-status field binds f292 and names 2=交易中, 6=停牌, 14=盘中停牌.
_TRADING_STATUS_CODE = 2
_SUSPENDED_STATUS_CODES = frozenset({6, 14})


class _HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


class EastmoneyHttpGet(Protocol):
    """Minimal requests-compatible HTTP seam used by the raw adapter."""

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
        raise RuntimeError("Eastmoney raw source is already initialized")


class _ImmutableSourceConfiguration:
    """Reject ordinary source mutation; constructors write slots directly."""

    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Eastmoney raw source configuration is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Eastmoney raw source configuration is immutable")


class EastmoneyRawQualificationSource(_ImmutableSourceConfiguration):
    """Capture exactly one Eastmoney secid without list selection or fallback."""

    __slots__ = (
        "_clock",
        "_evidence_origin",
        "_http_get",
        "_monotonic_ns",
        "_timeout_seconds",
    )

    _clock: Callable[[], datetime]
    _evidence_origin: ObservationEvidenceOrigin
    _http_get: EastmoneyHttpGet
    _monotonic_ns: Callable[[], int]
    _timeout_seconds: float

    source_id = "eastmoney_raw"
    adapter_version = "eastmoney_raw_qualification.v1"

    def __init__(
        self,
        *,
        http_get: EastmoneyHttpGet | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
        evidence_origin: ObservationEvidenceOrigin = ObservationEvidenceOrigin.TEST_ONLY,
        timeout_seconds: float = 10.0,
    ) -> None:
        _reject_reinitialization(self, "_evidence_origin")
        resolved_http_get = requests.get if http_get is None else http_get
        resolved_clock = _utc_now if clock is None else clock
        resolved_monotonic_ns = _system_monotonic_ns if monotonic_ns is None else monotonic_ns
        if type(evidence_origin) is not ObservationEvidenceOrigin:
            raise TypeError("evidence_origin must be ObservationEvidenceOrigin")
        if http_get is not None and evidence_origin is not ObservationEvidenceOrigin.TEST_ONLY:
            raise ValueError("injected Eastmoney transport must use TEST_ONLY evidence origin")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be finite and positive")
        normalized_timeout_seconds = float(timeout_seconds)
        if not math.isfinite(normalized_timeout_seconds) or normalized_timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        object.__setattr__(self, "_http_get", resolved_http_get)
        object.__setattr__(self, "_clock", resolved_clock)
        object.__setattr__(self, "_monotonic_ns", resolved_monotonic_ns)
        object.__setattr__(self, "_evidence_origin", evidence_origin)
        object.__setattr__(self, "_timeout_seconds", normalized_timeout_seconds)

    @property
    def evidence_origin(self) -> ObservationEvidenceOrigin:
        return self._evidence_origin

    def capture(
        self,
        sample: QualificationSample,
        *,
        timeout_seconds: float | None = None,
    ) -> QualificationSourceCapture:
        """Fetch one explicit secid and retain response content before decoding."""
        _validate_sample(sample)
        request = eastmoney_quote_request(symbol=sample.symbol, venue=sample.venue)
        started_ns = self._monotonic_ns()
        requested_at = self._clock()
        response = self._fetch_request(
            request,
            timeout=_effective_timeout(self._timeout_seconds, timeout_seconds),
        )
        received_at = self._clock()
        fetch_duration_ms = (self._monotonic_ns() - started_ns) // 1_000_000
        status_code = getattr(response, "status_code", None)
        raw_payload = getattr(response, "content", None)
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not isinstance(raw_payload, bytes)
            or len(raw_payload) > request.maximum_payload_bytes
        ):
            raise ValueError("invalid Eastmoney HTTP response")
        if status_code != 200:
            return _failed_capture(
                sample,
                requested_at=requested_at,
                received_at=received_at,
                fetch_duration_ms=fetch_duration_ms,
                raw_payload=raw_payload,
                data_gap=f"http_status_{status_code}",
            )
        try:
            normalized = self.replay_normalize(sample, raw_payload)
        except (UnicodeError, ValueError, TypeError):
            return _failed_capture(
                sample,
                requested_at=requested_at,
                received_at=received_at,
                fetch_duration_ms=fetch_duration_ms,
                raw_payload=raw_payload,
                data_gap="source_payload_parse_failed",
            )
        return QualificationSourceCapture(
            symbol=normalized.symbol,
            venue=normalized.venue,
            requested_at=requested_at,
            received_at=received_at,
            fetch_duration_ms=fetch_duration_ms,
            source_event_at=normalized.source_event_at,
            price=normalized.price,
            trading_status=normalized.trading_status,
            upper_limit_price=normalized.upper_limit_price,
            lower_limit_price=normalized.lower_limit_price,
            raw_payload=raw_payload,
            raw_payload_kind="upstream_http_response",
            volume=normalized.volume,
            turnover=normalized.turnover,
        )

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

    def replay_normalize(
        self,
        sample: QualificationSample,
        raw_payload: bytes,
    ) -> QualificationNormalizedRecord:
        """Normalize only supplied UTF-8 JSON bytes with explicit field scaling."""
        _validate_sample(sample)
        data = _response_data(raw_payload)
        symbol = _required_symbol(data.get("f57"))
        market = _required_market(data.get("f107"))
        scale = _price_scale(data.get("f59"), data)
        return QualificationNormalizedRecord(
            symbol=symbol,
            venue=_VENUE_BY_MARKET[market],
            source_event_at=_source_event_at(data.get("f86")),
            price=_optional_scaled_price("last price", data.get("f43"), scale),
            trading_status=_trading_status(data.get("f292")),
            upper_limit_price=_optional_scaled_price("upper limit", data.get("f51"), scale),
            lower_limit_price=_optional_scaled_price("lower limit", data.get("f52"), scale),
            volume=_optional_nonnegative_quantity("volume", data.get("f47")),
            turnover=_optional_nonnegative_quantity("turnover", data.get("f48")),
        )


class _OnDemandEastmoneyRawQualificationSource(EastmoneyRawQualificationSource):
    """Composition-owned LIVE source; formal callers cannot inject its transport."""

    __slots__ = ("__on_demand_transport",)

    __on_demand_transport: _EastmoneyOnDemandHttpGet

    def __init__(
        self,
        *,
        transport: _EastmoneyOnDemandHttpGet,
        clock: Callable[[], datetime] | None,
        timeout_seconds: float,
    ) -> None:
        _reject_reinitialization(
            self,
            "_evidence_origin",
            "_OnDemandEastmoneyRawQualificationSource__on_demand_transport",
        )
        if not _is_production_on_demand_http_get(transport):
            raise TypeError("on-demand Eastmoney raw source requires production transport")
        super().__init__(
            clock=clock,
            evidence_origin=ObservationEvidenceOrigin.LIVE_CAPTURE,
            timeout_seconds=timeout_seconds,
        )
        object.__setattr__(
            self,
            "_OnDemandEastmoneyRawQualificationSource__on_demand_transport",
            transport,
        )

    def _fetch_request(
        self,
        request: EastmoneyHttpRequest,
        *,
        timeout: float,
    ) -> _HttpResponse:
        return self.__on_demand_transport.fetch(request, timeout=timeout)


def _build_on_demand_eastmoney_raw_source(
    *,
    transport: _EastmoneyOnDemandHttpGet,
    clock: Callable[[], datetime] | None,
    timeout_seconds: float,
) -> EastmoneyRawQualificationSource:
    return _OnDemandEastmoneyRawQualificationSource(
        transport=transport,
        clock=clock,
        timeout_seconds=timeout_seconds,
    )


def _effective_timeout(configured: float, requested: float | None) -> float:
    if requested is None:
        return configured
    if (
        isinstance(requested, bool)
        or not isinstance(requested, (int, float))
        or not math.isfinite(float(requested))
        or requested <= 0
    ):
        raise ValueError("timeout_seconds must be finite and positive")
    return min(configured, float(requested))


def _response_data(raw_payload: bytes) -> Mapping[str, object]:
    text = raw_payload.decode("utf-8", errors="strict")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("unexpected Eastmoney quote response envelope")
    response_code = payload.get("rc")
    if isinstance(response_code, bool) or not isinstance(response_code, int) or response_code != 0:
        raise ValueError("unexpected Eastmoney quote response envelope")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Eastmoney quote response data must be an object")
    return data


def _required_symbol(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{6}", value) is None:
        raise ValueError("invalid Eastmoney response symbol")
    return value


def _required_market(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in _VENUE_BY_MARKET:
        raise ValueError("invalid Eastmoney response market")
    return value


def _price_scale(value: object, data: Mapping[str, object]) -> int:
    has_price = any(not _is_missing(data.get(field)) for field in ("f43", "f51", "f52"))
    if not has_price and _is_missing(value):
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
        raise ValueError("invalid Eastmoney price scale")
    return value


def _optional_scaled_price(field_name: str, value: object, scale: int) -> str | None:
    if _is_missing(value):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Eastmoney {field_name} is malformed")
    return format(Decimal(value).scaleb(-scale), f".{scale}f")


def _optional_nonnegative_quantity(field_name: str, value: object) -> str | None:
    if _is_missing(value):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Eastmoney {field_name} is malformed")
    return str(value)


def _source_event_at(value: object) -> datetime | None:
    if _is_missing(value):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("invalid Eastmoney source event time")
    try:
        return datetime.fromtimestamp(value, tz=UTC).astimezone(_SHANGHAI)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("invalid Eastmoney source event time") from exc


def _trading_status(value: object) -> TradingStatus:
    if _is_missing(value):
        return TradingStatus.UNKNOWN
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid Eastmoney trading status")
    if value == _TRADING_STATUS_CODE:
        return TradingStatus.TRADING
    if value in _SUSPENDED_STATUS_CODES:
        return TradingStatus.SUSPENDED
    return TradingStatus.UNKNOWN


def _is_missing(value: object) -> bool:
    return value is None or value == "-"


def _validate_sample(sample: QualificationSample) -> int:
    if re.fullmatch(r"[0-9]{6}", sample.symbol) is None or sample.venue not in _MARKET_BY_VENUE:
        raise ValueError("Eastmoney sample must use six digits and venue sh/sz")
    return _MARKET_BY_VENUE[sample.venue]


def _failed_capture(
    sample: QualificationSample,
    *,
    requested_at: datetime,
    received_at: datetime,
    fetch_duration_ms: int,
    raw_payload: bytes,
    data_gap: str,
) -> QualificationSourceCapture:
    return QualificationSourceCapture(
        symbol=sample.symbol,
        venue=None,
        requested_at=requested_at,
        received_at=received_at,
        fetch_duration_ms=fetch_duration_ms,
        source_event_at=None,
        price=None,
        trading_status=TradingStatus.UNKNOWN,
        upper_limit_price=None,
        lower_limit_price=None,
        raw_payload=raw_payload,
        raw_payload_kind="upstream_http_response",
        data_gaps=(data_gap,),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = ["EastmoneyHttpGet", "EastmoneyRawQualificationSource"]
