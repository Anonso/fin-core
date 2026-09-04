"""Source-only Tencent quote adapter that preserves upstream response bytes."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime
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

_TENCENT_QUOTE_API = "https://qt.gtimg.cn/q="
_HEADERS = {
    "Referer": "https://gu.qq.com/",
    "User-Agent": "fin-analyse-market-data-qualification/1",
}
_RESPONSE = re.compile(r'v_(?P<venue>sh|sz)(?P<symbol>[0-9]{6})="(?P<payload>[^"\r\n]*)";\r?\n?')
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SYMBOL_INDEX = 2
_LAST_PRICE_INDEX = 3
_SOURCE_EVENT_TIME_INDEX = 30
_UPPER_LIMIT_INDEX = 47
_LOWER_LIMIT_INDEX = 48
_MINIMUM_FIELD_COUNT = _LOWER_LIMIT_INDEX + 1


class _HttpResponse(Protocol):
    status_code: int
    content: bytes


class TencentHttpGet(Protocol):
    """Minimal requests-compatible HTTP seam used by the raw adapter."""

    def __call__(
        self,
        url: str,
        *,
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
        raise RuntimeError("Tencent raw source is already initialized")


class _ImmutableSourceConfiguration:
    """Reject ordinary source mutation; constructors write slots directly."""

    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Tencent raw source configuration is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Tencent raw source configuration is immutable")


class TencentRawQualificationSource(_ImmutableSourceConfiguration):
    """Capture one Tencent HTTP body without fallback or pre-normalization loss."""

    __slots__ = (
        "_clock",
        "_evidence_origin",
        "_http_get",
        "_monotonic_ns",
        "_timeout_seconds",
    )

    _clock: Callable[[], datetime]
    _evidence_origin: ObservationEvidenceOrigin
    _http_get: TencentHttpGet
    _monotonic_ns: Callable[[], int]
    _timeout_seconds: float

    source_id = "tencent_raw"
    adapter_version = "tencent_raw_qualification.v1"

    def __init__(
        self,
        *,
        http_get: TencentHttpGet | None = None,
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
            raise ValueError("injected Tencent transport must use TEST_ONLY evidence origin")
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
        """Fetch exactly one symbol and retain the response content before decoding."""
        _validate_sample(sample)
        started_ns = self._monotonic_ns()
        requested_at = self._clock()
        response = self._http_get(
            f"{_TENCENT_QUOTE_API}{sample.venue}{sample.symbol}",
            headers=_HEADERS,
            timeout=_effective_timeout(self._timeout_seconds, timeout_seconds),
            allow_redirects=False,
        )
        received_at = self._clock()
        fetch_duration_ms = (self._monotonic_ns() - started_ns) // 1_000_000
        raw_payload = response.content
        if response.status_code != 200:
            return _failed_capture(
                sample,
                requested_at=requested_at,
                received_at=received_at,
                fetch_duration_ms=fetch_duration_ms,
                raw_payload=raw_payload,
                data_gap=f"http_status_{response.status_code}",
            )
        try:
            normalized = self.replay_normalize(sample, raw_payload)
        except (UnicodeError, ValueError):
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
        )

    def replay_normalize(
        self,
        sample: QualificationSample,
        raw_payload: bytes,
    ) -> QualificationNormalizedRecord:
        """Normalize only the supplied bytes using Tencent's fixed GB18030 encoding."""
        _validate_sample(sample)
        text = _decode_response(raw_payload)
        match = _RESPONSE.fullmatch(text)
        if match is None:
            raise ValueError("unexpected Tencent quote response envelope")
        fields = match.group("payload").split("~")
        if len(fields) < _MINIMUM_FIELD_COUNT:
            raise ValueError("truncated Tencent quote response")
        symbol = match.group("symbol")
        if fields[_SYMBOL_INDEX] != symbol:
            raise ValueError("Tencent quote envelope and payload symbols disagree")
        return QualificationNormalizedRecord(
            symbol=symbol,
            venue=match.group("venue"),
            source_event_at=_source_event_at(fields[_SOURCE_EVENT_TIME_INDEX]),
            price=_optional_positive_decimal("last price", fields[_LAST_PRICE_INDEX]),
            trading_status=TradingStatus.UNKNOWN,
            upper_limit_price=_optional_limit_price("upper limit", fields[_UPPER_LIMIT_INDEX]),
            lower_limit_price=_optional_limit_price("lower limit", fields[_LOWER_LIMIT_INDEX]),
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


def _validate_sample(sample: QualificationSample) -> None:
    if re.fullmatch(r"[0-9]{6}", sample.symbol) is None or sample.venue not in {"sh", "sz"}:
        raise ValueError("Tencent sample must use six digits and venue sh/sz")


def _source_event_at(value: str) -> datetime | None:
    if not value:
        return None
    if re.fullmatch(r"[0-9]{14}", value) is None:
        raise ValueError("invalid Tencent source event time")
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise ValueError("invalid Tencent source event time") from exc
    return parsed.replace(tzinfo=_SHANGHAI)


def _decode_response(raw_payload: bytes) -> str:
    if any(byte >= 0x80 for byte in raw_payload):
        try:
            raw_payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            pass
        else:
            raise ValueError("Tencent response unexpectedly uses UTF-8")
    return raw_payload.decode("gb18030", errors="strict")


def _optional_positive_decimal(field_name: str, value: str) -> str | None:
    if not value:
        return None
    if re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) is None:
        raise ValueError(f"Tencent {field_name} is malformed")
    if not any(character != "0" and character != "." for character in value):
        raise ValueError(f"Tencent {field_name} is not positive")
    return value


def _optional_limit_price(field_name: str, value: str) -> str | None:
    # 指数行涨跌停位为 "-1" 哨兵（实测 sh000688/sh600519 同 88 字段对照）：
    # 指数无涨跌停是语义事实，按缺失处理而非坏数据；价格位校验不受此影响。
    if value in {"", "-1", "0"}:
        return None
    return _optional_positive_decimal(field_name, value)


def _utc_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


__all__ = ["TencentHttpGet", "TencentRawQualificationSource"]
