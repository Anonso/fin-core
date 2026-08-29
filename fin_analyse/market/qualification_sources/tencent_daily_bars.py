"""Tencent qfq daily-bar reader — fallback for unreachable Eastmoney kline.

Eastmoney ``push2his`` (daily kline CDN export) was unreachable on this
network since 2026-08-02 (TLS interference), while Tencent's
``ifzq.gtimg.cn/appstock/app/fqkline/get`` serves the same qfq daily bars
with identical OHLCV semantics.  This reader implements the same
``QualifiedDailyBarReader`` protocol with a lightweight direct-request
replay (no artifact cache), so ``on_demand_tactical_context`` can chain it
as a fallback behind the Eastmoney reader.

Row format (Tencent qfqday): ``[date, open, close, high, low, volume]`` —
note open/close/high/low order differs from Eastmoney's open/close/high/low.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

from fin_analyse.market.data_qualification import ObservationEvidenceOrigin
from fin_analyse.market.providers.base import OHLCV
from fin_analyse.market.qualified_daily_bars import (
    QualifiedDailyBarReadRequest,
    QualifiedDailyBarSeries,
)

_PROVIDER_ID = "tencent_daily_bars"
_BASE_PROVIDER_VERSION = "tencent_qfq_daily_bars.v1"
_KLINE_ENDPOINT = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
_MAX_ROWS = 120
_LOOKBACK_DAYS = 240  # 120 bars + 周末/节假日余量
_VENUES = frozenset({"sh", "sz"})
_SYMBOL_PATTERN = re.compile(r"^(?P<code>[0-9]{6})(?:\.(?P<venue>SH|SZ))?$")
_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class TencentDailyBarSourceError(RuntimeError):
    """Tencent daily-bar read failed closed (invalid input, upstream, or parse)."""


class _HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


class TencentDailyBarHttpGet(Protocol):
    def __call__(
        self,
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
    ) -> _HttpResponse: ...


def _reject_reinitialization(instance: object, *slot_names: str) -> None:
    for slot_name in slot_names:
        if getattr(instance, slot_name, None) is not None:
            raise RuntimeError("tencent daily bar source is immutable")


class _ImmutableSourceConfiguration:
    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("tencent daily bar source is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("tencent daily bar source is immutable")


class TencentDailyBarReader(_ImmutableSourceConfiguration):
    """Direct-request qfq daily-bar reader honoring point-in-time semantics."""

    __slots__ = ("_http_get", "_clock", "_evidence_origin", "_timeout_seconds")

    _http_get: TencentDailyBarHttpGet
    _clock: Callable[[], datetime]
    _evidence_origin: ObservationEvidenceOrigin
    _timeout_seconds: float

    provider_id = _PROVIDER_ID
    adapter_version = _BASE_PROVIDER_VERSION

    def __init__(
        self,
        *,
        http_get: TencentDailyBarHttpGet | None = None,
        clock: Callable[[], datetime] | None = None,
        evidence_origin: ObservationEvidenceOrigin = ObservationEvidenceOrigin.TEST_ONLY,
        timeout_seconds: float = 10.0,
    ) -> None:
        _reject_reinitialization(self, "_evidence_origin")
        import requests

        resolved_http_get = requests.get if http_get is None else http_get
        resolved_clock = (lambda: datetime.now(UTC)) if clock is None else clock
        if type(evidence_origin) is not ObservationEvidenceOrigin:
            raise TypeError("evidence_origin must be ObservationEvidenceOrigin")
        if http_get is not None and evidence_origin is not ObservationEvidenceOrigin.TEST_ONLY:
            raise TencentDailyBarSourceError(
                "TENCENT_DAILY_BAR_INJECTED_TRANSPORT_MUST_BE_TEST_ONLY"
            )
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be finite and positive")
        normalized_timeout_seconds = float(timeout_seconds)
        if not math.isfinite(normalized_timeout_seconds) or normalized_timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        object.__setattr__(self, "_http_get", resolved_http_get)
        object.__setattr__(self, "_clock", resolved_clock)
        object.__setattr__(self, "_evidence_origin", evidence_origin)
        object.__setattr__(self, "_timeout_seconds", normalized_timeout_seconds)

    @property
    def evidence_origin(self) -> ObservationEvidenceOrigin:
        return self._evidence_origin

    @property
    def provider_version(self) -> str:
        suffix = (
            "live" if self.evidence_origin is ObservationEvidenceOrigin.LIVE_CAPTURE else "test"
        )
        return f"{_BASE_PROVIDER_VERSION}.{suffix}"

    def read(self, request: QualifiedDailyBarReadRequest) -> QualifiedDailyBarSeries:
        """Return completed qfq bars strictly before the decision cutoff."""

        scope = _validate_request(request)
        if (
            request.deadline_at is not None
            and _deadline_remaining_seconds(request.deadline_at) <= 0
        ):
            raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_DEADLINE_REACHED")
        payload = self._capture(scope, deadline_at=request.deadline_at)
        bars = _parse_bars(payload, scope=scope)
        completed = _filter_cutoff(bars, cutoff=scope.cutoff_date)
        if len(completed) < request.minimum_completed_bars:
            raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_INSUFFICIENT_BARS")
        return QualifiedDailyBarSeries(
            symbol=scope.symbol,
            provider_id=_PROVIDER_ID,
            provider_version=self.provider_version,
            completed_bars=tuple(completed),
            adjustment="FORWARD_ADJUSTED_QFQ",
            source_revision=hashlib.sha256(payload).hexdigest(),
        )

    def _capture(
        self,
        scope: _ReadScope,
        *,
        deadline_at: datetime | None,
    ) -> bytes:
        start = (scope.cutoff_date - timedelta(days=_LOOKBACK_DAYS)).isoformat()
        end = scope.cutoff_date.isoformat()
        param = f"{scope.venue}{scope.symbol},day,{start},{end},{_MAX_ROWS},qfq"
        url = f"{_KLINE_ENDPOINT}?{urlencode({'param': param})}"
        remaining = _deadline_remaining_seconds(deadline_at) if deadline_at is not None else None
        timeout = (
            min(self._timeout_seconds, remaining)
            if remaining is not None and remaining > 0
            else self._timeout_seconds
        )
        if timeout <= 0:
            raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_DEADLINE_REACHED")
        try:
            response = self._http_get(url, params={}, timeout=timeout)
        except Exception:
            raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_UPSTREAM_UNAVAILABLE") from None
        if response.status_code != 200:
            raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_HTTP_STATUS_ERROR")
        return response.content


def _build_on_demand_tencent_daily_bar_reader(
    *,
    timeout_seconds: float = 10.0,
) -> TencentDailyBarReader:
    return TencentDailyBarReader(
        evidence_origin=ObservationEvidenceOrigin.LIVE_CAPTURE,
        timeout_seconds=timeout_seconds,
    )


class _ReadScope:
    __slots__ = ("symbol", "venue", "cutoff_date")

    def __init__(self, *, symbol: str, venue: str, cutoff_date: date) -> None:
        self.symbol = symbol
        self.venue = venue
        self.cutoff_date = cutoff_date


def _validate_request(request: QualifiedDailyBarReadRequest) -> _ReadScope:
    symbol = request.symbol
    if not isinstance(symbol, str):
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_SYMBOL_INVALID")
    code, venue = _parse_symbol(symbol)
    if not isinstance(request.decision_cutoff_at, datetime) or (
        request.decision_cutoff_at.tzinfo is None or request.decision_cutoff_at.utcoffset() is None
    ):
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_CUTOFF_INVALID")
    cutoff_date = request.decision_cutoff_at.astimezone(UTC).date()
    if not isinstance(request.trade_date, date) or isinstance(request.trade_date, datetime):
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_TRADE_DATE_INVALID")
    if isinstance(request.minimum_completed_bars, bool) or not isinstance(
        request.minimum_completed_bars, int
    ):
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_MINIMUM_INVALID")
    if request.minimum_completed_bars < 0:
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_MINIMUM_INVALID")
    return _ReadScope(symbol=code, venue=venue, cutoff_date=cutoff_date)


def _venue_for_symbol(symbol: str) -> str:
    # 6xx/68x = 上交所 sh；其余深市 sz（腾讯接口按交易所前缀拼接）。
    return "sh" if symbol.startswith(("6", "68")) else "sz"


def _parse_symbol(symbol: str) -> tuple[str, str]:
    """Return (code, venue) from a bare or canonical (``600519.SH``) symbol.

    The canonical suffix carries the authoritative venue; bare symbols fall
    back to the prefix rule so the reader accepts both FIN canonical
    ``600519.SH`` instruments (what on-demand collection passes) and the
    legacy bare form.
    """

    match = _SYMBOL_PATTERN.fullmatch(symbol)
    if match is None:
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_SYMBOL_INVALID")
    code = match.group("code")
    venue = match.group("venue")
    return code, venue.lower() if venue is not None else _venue_for_symbol(code)


def _deadline_remaining_seconds(deadline_at: datetime | None) -> float:
    if deadline_at is None:
        return math.inf
    return (deadline_at - datetime.now(UTC)).total_seconds()


def _parse_bars(raw_payload: bytes, *, scope: _ReadScope) -> list[Any]:
    try:
        payload = json.loads(raw_payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_PAYLOAD_INVALID") from None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_PAYLOAD_INVALID")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_PAYLOAD_INVALID")
    stock = data.get(f"{scope.venue}{scope.symbol}")
    if not isinstance(stock, dict):
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_PAYLOAD_INVALID")
    rows = stock.get("qfqday")
    if not isinstance(rows, list) or not rows:
        raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_PAYLOAD_INVALID")
    bars: list[Any] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_PAYLOAD_INVALID")
        try:
            bars.append(
                {
                    "date": row[0],
                    "open": float(row[1]),
                    "close": float(row[2]),
                    "high": float(row[3]),
                    "low": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        except (TypeError, ValueError):
            raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_PAYLOAD_INVALID") from None
    return bars


def _filter_cutoff(bars: list[Any], *, cutoff: date) -> list[Any]:
    completed: list[Any] = []
    for bar in bars:
        raw_date = bar["date"]
        if not isinstance(raw_date, str) or _DATE_PATTERN.fullmatch(raw_date) is None:
            raise TencentDailyBarSourceError("TENCENT_DAILY_BAR_PAYLOAD_INVALID")
        bar_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        if bar_date >= cutoff:
            continue
        completed.append(
            OHLCV(
                date=raw_date,
                open=bar["open"],
                high=bar["high"],
                low=bar["low"],
                close=bar["close"],
                volume=bar["volume"],
            )
        )
    return completed


__all__ = [
    "TencentDailyBarReader",
    "TencentDailyBarSourceError",
    "_build_on_demand_tencent_daily_bar_reader",
]
