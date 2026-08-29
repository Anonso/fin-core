"""Tencent completed 30-minute bars for advisory intraday evidence.

Replaces the unreachable Eastmoney 30-minute path (push2his TLS interference;
push2delay returns empty klines) with the Tencent ``ifzq.gtimg.cn`` mkline
endpoint, verified reachable and returning real data on 2026-08-12.  Only
bars completed at or before the decision cutoff are returned; provenance and
cutoff semantics match the provider-neutral 30-minute contract.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast
from urllib.parse import urlencode

from fin_analyse.market.providers.base import OHLCV
from fin_analyse.market.qualified_intraday_bars import (
    QualifiedThirtyMinuteBarReadRequest,
    QualifiedThirtyMinuteBarSeries,
)

_PROVIDER_ID = "tencent_intraday_bars"
_BASE_PROVIDER_VERSION = "tencent_completed_thirty_minute_bars.v1"
_KLINE_ENDPOINT = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
_MAX_ROWS = 800
_BAR_MINUTES = 30
_TIMESTAMP_FORMAT = "%Y%m%d%H%M"
_CN_TZ = timezone(timedelta(hours=8))


class TencentIntradayBarHttpError(RuntimeError):
    """Stable fail-closed Tencent intraday source error."""


class TencentIntradayBarHttpGet(Protocol):
    def __call__(
        self,
        url: str,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any: ...


class TencentIntradayBarReader:
    """Direct-request Tencent 30-minute bar reader honoring the decision cutoff."""

    __slots__ = ("_http_get", "_timeout_seconds")

    _http_get: TencentIntradayBarHttpGet
    _timeout_seconds: float

    provider_id = _PROVIDER_ID

    def __init__(
        self,
        *,
        http_get: TencentIntradayBarHttpGet | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        import requests

        resolved_http_get = (
            cast(TencentIntradayBarHttpGet, requests.get) if http_get is None else http_get
        )
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be finite and positive")
        normalized = float(timeout_seconds)
        if not math.isfinite(normalized) or normalized <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self._http_get = resolved_http_get
        self._timeout_seconds = normalized

    def read(
        self,
        request: QualifiedThirtyMinuteBarReadRequest,
    ) -> QualifiedThirtyMinuteBarSeries:
        """Return completed 30-minute bars strictly before the decision cutoff."""
        payload = self._capture(request)
        bars = _parse_bars(payload, symbol=request.symbol)
        completed = _filter_cutoff(bars, cutoff_at=request.decision_cutoff_at)
        if len(completed) < request.minimum_completed_bars:
            raise TencentIntradayBarHttpError("TENCENT_INTRADAY_BAR_INSUFFICIENT_BARS")
        return QualifiedThirtyMinuteBarSeries(
            symbol=request.symbol,
            provider_id=_PROVIDER_ID,
            provider_version=_BASE_PROVIDER_VERSION,
            completed_bars=tuple(completed),
            adjustment="UNKNOWN",
            source_revision=hashlib.sha256(payload).hexdigest(),
        )

    def _capture(self, request: QualifiedThirtyMinuteBarReadRequest) -> bytes:
        code, _, venue = request.symbol.partition(".")
        venue = venue.lower()
        if not code or venue not in ("sh", "sz"):
            raise TencentIntradayBarHttpError("TENCENT_INTRADAY_BAR_SYMBOL_INVALID")
        param = f"{venue}{code},m30,,{_MAX_ROWS}"
        url = f"{_KLINE_ENDPOINT}?{urlencode({'param': param})}"
        try:
            response = self._http_get(url, params={}, timeout=self._timeout_seconds)
        except Exception:
            raise TencentIntradayBarHttpError("TENCENT_INTRADAY_BAR_UPSTREAM_UNAVAILABLE") from None
        if response.status_code != 200:
            raise TencentIntradayBarHttpError("TENCENT_INTRADAY_BAR_HTTP_STATUS_ERROR")
        return cast(bytes, response.content)


def _parse_bars(payload: bytes, *, symbol: str) -> list[OHLCV]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TencentIntradayBarHttpError("TENCENT_INTRADAY_BAR_DOCUMENT_INVALID") from None
    if not isinstance(document, dict) or document.get("code") != 0:
        raise TencentIntradayBarHttpError("TENCENT_INTRADAY_BAR_DOCUMENT_INVALID")
    data = document.get("data")
    if not isinstance(data, dict):
        raise TencentIntradayBarHttpError("TENCENT_INTRADAY_BAR_DOCUMENT_INVALID")
    code, _, venue = symbol.partition(".")
    series_key = f"{venue.lower()}{code}"
    entry = data.get(series_key)
    if not isinstance(entry, dict):
        raise TencentIntradayBarHttpError("TENCENT_INTRADAY_BAR_SYMBOL_MISSING")
    raw = entry.get("m30")
    if not isinstance(raw, list):
        raise TencentIntradayBarHttpError("TENCENT_INTRADAY_BAR_SERIES_MISSING")
    bars: list[OHLCV] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        timestamp = str(item[0])
        try:
            started_at = datetime.strptime(timestamp, _TIMESTAMP_FORMAT).replace(tzinfo=_CN_TZ)
        except ValueError:
            continue
        try:
            values = [float(item[i]) for i in range(1, 5)]
            volume = int(float(item[5]))
        except (TypeError, ValueError):
            continue
        if any(not math.isfinite(v) for v in values):
            continue
        bars.append(
            OHLCV(
                date=started_at.isoformat(),
                open=values[0],
                close=values[1],
                high=values[2],
                low=values[3],
                volume=volume,
            )
        )
    return bars


def _filter_cutoff(bars: Sequence[OHLCV], *, cutoff_at: datetime) -> list[OHLCV]:
    """30m bar 完成时刻 = 起始时刻 + 30 分钟,必须不晚于 decision cutoff。"""
    completed: list[OHLCV] = []
    for bar in bars:
        try:
            started = datetime.fromisoformat(bar.date)
        except ValueError:
            continue
        if started + timedelta(minutes=_BAR_MINUTES) <= cutoff_at:
            completed.append(bar)
    return completed


def _build_on_demand_tencent_intraday_bar_reader(
    *,
    timeout_seconds: float = 8.0,
) -> TencentIntradayBarReader:
    return TencentIntradayBarReader(timeout_seconds=timeout_seconds)


__all__ = [
    "TencentIntradayBarReader",
    "TencentIntradayBarHttpError",
    "_build_on_demand_tencent_intraday_bar_reader",
]
