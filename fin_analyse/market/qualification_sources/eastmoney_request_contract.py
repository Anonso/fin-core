"""Canonical Eastmoney request contracts shared by primary and fallback transports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlencode

EASTMONEY_QUOTE_MAX_RAW_BYTES = 64 * 1024
EASTMONEY_DAILY_BAR_MAX_RAW_BYTES = 4 * 1024 * 1024
EASTMONEY_THIRTY_MINUTE_BAR_MAX_RAW_BYTES = 4 * 1024 * 1024

# 2026-08-02：push2/push2his（东财实时行情 CDN 出口）在当前网络不可达
# （TLS 被干扰；主站/其他端点正常），push2delay（延迟行情，~15 分钟）数据
# 一致且与 FIN 的 reference_only 延迟参考语义完全匹配——实时报价切到
# push2delay；日线（push2his）无等价替代，保留原端点（不可达时降级）。
_QUOTE_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/stock/get"
_DAILY_BAR_ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_QUOTE_FIELDS = "f43,f47,f48,f51,f52,f57,f58,f59,f86,f107,f292"
_DAILY_FIELDS1 = "f1,f2,f3,f4,f5,f6"
_DAILY_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
_DAILY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
_MARKET_BY_VENUE = {"sh": "1", "sz": "0"}


@dataclass(frozen=True, slots=True)
class EastmoneyHttpRequest:
    """One immutable, fully specified Eastmoney HTTP GET."""

    kind: Literal["quote", "daily_bars", "thirty_minute_bars"]
    endpoint: str
    query: tuple[tuple[str, str], ...]
    headers: tuple[tuple[str, str], ...]
    maximum_payload_bytes: int
    response_content_type: str = "application/json"
    response_charset: str = "UTF-8"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Revalidate at the transport boundary, including after hostile mutation."""

        _validate_contract(self)

    @property
    def canonical_url(self) -> str:
        return f"{self.endpoint}?{urlencode(self.query)}"

    def params_dict(self) -> dict[str, str]:
        return dict(self.query)

    def headers_dict(self) -> dict[str, str]:
        return dict(self.headers)


def eastmoney_quote_request(*, symbol: str, venue: str) -> EastmoneyHttpRequest:
    symbol, venue, secid = _instrument(symbol=symbol, venue=venue)
    return EastmoneyHttpRequest(
        kind="quote",
        endpoint=_QUOTE_ENDPOINT,
        query=(
            ("fields", _QUOTE_FIELDS),
            ("fltt", "1"),
            ("invt", "2"),
            ("secid", secid),
        ),
        headers=(
            ("Referer", f"https://quote.eastmoney.com/{venue}{symbol}.html"),
            ("User-Agent", "fin-analyse-market-data-qualification/1"),
        ),
        maximum_payload_bytes=EASTMONEY_QUOTE_MAX_RAW_BYTES,
    )


def eastmoney_daily_bar_request(
    *,
    symbol: str,
    venue: str,
    completed_through: str,
) -> EastmoneyHttpRequest:
    symbol, venue, secid = _instrument(symbol=symbol, venue=venue)
    if re.fullmatch(r"[0-9]{8}", completed_through) is None:
        raise ValueError("Eastmoney completed_through must be YYYYMMDD")
    return EastmoneyHttpRequest(
        kind="daily_bars",
        endpoint=_DAILY_BAR_ENDPOINT,
        query=(
            ("beg", "0"),
            ("end", completed_through),
            ("fields1", _DAILY_FIELDS1),
            ("fields2", _DAILY_FIELDS2),
            ("fqt", "1"),
            ("klt", "101"),
            ("lmt", "120"),
            ("secid", secid),
            ("ut", _DAILY_UT),
        ),
        headers=(
            ("Referer", f"https://quote.eastmoney.com/{venue}{symbol}.html"),
            ("User-Agent", "fin-analyse-qualified-daily-bars/1"),
        ),
        maximum_payload_bytes=EASTMONEY_DAILY_BAR_MAX_RAW_BYTES,
    )


def eastmoney_thirty_minute_bar_request(
    *,
    symbol: str,
    venue: str,
    trade_date: str,
) -> EastmoneyHttpRequest:
    symbol, venue, secid = _instrument(symbol=symbol, venue=venue)
    if re.fullmatch(r"[0-9]{8}", trade_date) is None:
        raise ValueError("Eastmoney 30-minute trade_date must be YYYYMMDD")
    return EastmoneyHttpRequest(
        kind="thirty_minute_bars",
        endpoint=_DAILY_BAR_ENDPOINT,
        query=(
            ("beg", "0"),
            ("end", trade_date),
            ("fields1", _DAILY_FIELDS1),
            ("fields2", _DAILY_FIELDS2),
            ("fqt", "1"),
            ("klt", "30"),
            ("lmt", "800"),
            ("secid", secid),
            ("ut", _DAILY_UT),
        ),
        headers=(
            ("Referer", f"https://quote.eastmoney.com/{venue}{symbol}.html"),
            ("User-Agent", "fin-analyse-qualified-thirty-minute-bars/1"),
        ),
        maximum_payload_bytes=EASTMONEY_THIRTY_MINUTE_BAR_MAX_RAW_BYTES,
    )


def _instrument(*, symbol: str, venue: str) -> tuple[str, str, str]:
    if re.fullmatch(r"[0-9]{6}", symbol) is None or venue not in _MARKET_BY_VENUE:
        raise ValueError("Eastmoney instrument must use six digits and venue sh/sz")
    return symbol, venue, f"{_MARKET_BY_VENUE[venue]}.{symbol}"


def _validate_contract(request: EastmoneyHttpRequest) -> None:
    if (
        request.response_content_type != "application/json"
        or request.response_charset != "UTF-8"
        or not isinstance(request.query, tuple)
        or not isinstance(request.headers, tuple)
        or len(dict(request.query)) != len(request.query)
        or len(dict(request.headers)) != len(request.headers)
    ):
        raise ValueError("invalid Eastmoney HTTP request contract")
    query = dict(request.query)
    secid = query.get("secid")
    match = re.fullmatch(r"(?P<market>[01])\.(?P<symbol>[0-9]{6})", secid or "")
    if match is None:
        raise ValueError("invalid Eastmoney HTTP request contract")
    canonical_secid = match.group(0)
    venue = "sh" if match.group("market") == "1" else "sz"
    symbol = match.group("symbol")
    expected_query: tuple[tuple[str, str], ...]
    if request.kind == "quote":
        expected_query = (
            ("fields", _QUOTE_FIELDS),
            ("fltt", "1"),
            ("invt", "2"),
            ("secid", canonical_secid),
        )
        expected_endpoint = _QUOTE_ENDPOINT
        expected_user_agent = "fin-analyse-market-data-qualification/1"
        expected_maximum = EASTMONEY_QUOTE_MAX_RAW_BYTES
    elif request.kind in {"daily_bars", "thirty_minute_bars"}:
        end = query.get("end")
        if not isinstance(end, str):
            raise ValueError("invalid Eastmoney HTTP request contract")
        try:
            parsed_end = datetime.strptime(end, "%Y%m%d")
        except ValueError as error:
            raise ValueError("invalid Eastmoney HTTP request contract") from error
        if parsed_end.strftime("%Y%m%d") != end:
            raise ValueError("invalid Eastmoney HTTP request contract")
        interval = "101" if request.kind == "daily_bars" else "30"
        limit = "120" if request.kind == "daily_bars" else "800"
        expected_query = (
            ("beg", "0"),
            ("end", end),
            ("fields1", _DAILY_FIELDS1),
            ("fields2", _DAILY_FIELDS2),
            ("fqt", "1"),
            ("klt", interval),
            ("lmt", limit),
            ("secid", canonical_secid),
            ("ut", _DAILY_UT),
        )
        expected_endpoint = _DAILY_BAR_ENDPOINT
        expected_user_agent = (
            "fin-analyse-qualified-daily-bars/1"
            if request.kind == "daily_bars"
            else "fin-analyse-qualified-thirty-minute-bars/1"
        )
        expected_maximum = (
            EASTMONEY_DAILY_BAR_MAX_RAW_BYTES
            if request.kind == "daily_bars"
            else EASTMONEY_THIRTY_MINUTE_BAR_MAX_RAW_BYTES
        )
    else:
        raise ValueError("invalid Eastmoney HTTP request contract")
    expected_headers = (
        ("Referer", f"https://quote.eastmoney.com/{venue}{symbol}.html"),
        ("User-Agent", expected_user_agent),
    )
    if (
        request.endpoint != expected_endpoint
        or request.query != expected_query
        or request.headers != expected_headers
        or request.maximum_payload_bytes != expected_maximum
    ):
        raise ValueError("invalid Eastmoney HTTP request contract")


__all__ = [
    "EASTMONEY_DAILY_BAR_MAX_RAW_BYTES",
    "EASTMONEY_QUOTE_MAX_RAW_BYTES",
    "EASTMONEY_THIRTY_MINUTE_BAR_MAX_RAW_BYTES",
    "EastmoneyHttpRequest",
    "eastmoney_daily_bar_request",
    "eastmoney_quote_request",
    "eastmoney_thirty_minute_bar_request",
]
