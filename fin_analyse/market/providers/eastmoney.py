"""EastMoney direct HTTP API provider — 东方财富网免费行情接口.

No library dependency, pure requests + JSONP parsing.
Reference: push2.eastmoney.com / push2his.eastmoney.com
"""

from __future__ import annotations

import json
import logging
import re
import time

from fin_analyse.market.eastmoney_client import em_get, strip_jsonp

from .base import OHLCV, BaseMarketProvider, CapitalFlow, QuoteResult

logger = logging.getLogger(__name__)

# ── API endpoints ──────────────────────────────────────────────
# 2026-08-02：push2/push2his（实时 CDN 出口）不可达（TLS 干扰），切
# push2delay（延迟行情，与 reference_only 语义匹配）；kline 无替代保留。
_QUOTE_API = "https://push2delay.eastmoney.com/api/qt/clist/get"
_KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_SEARCH_API = "https://searchapi.eastmoney.com/api/suggest/get"

# Stock market codes for East Money secid
_SH_MARKETS = {"1", "sh", "SH"}
_SZ_MARKETS = {"0", "sz", "SZ", "3"}  # 3 = 创业板

# JSONP token (fixed, from East Money web app)
_UT = "bd1d9ddb04089700cf9c27f6f7426281"

# Quote fields we care about
_QUOTE_FIELDS = "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f20,f21"

_KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
_KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def _secid(ticker: str) -> str:
    """Convert ticker to East Money secid format (market.code)."""
    if ticker.startswith("6") or ticker.startswith("68"):
        return f"1.{ticker}"  # 上交所
    return f"0.{ticker}"  # 深交所


def _parse_kline_row(row: str) -> OHLCV:
    """Parse a single K-line row from East Money response.

    Format: 日期,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率
    """
    parts = row.split(",")
    return OHLCV(
        date=parts[0],
        open=float(parts[1]),
        close=float(parts[2]),
        high=float(parts[3]),
        low=float(parts[4]),
        volume=float(parts[5]),
        turnover=float(parts[6]) if len(parts) > 6 else None,
    )


class EastMoneyProvider(BaseMarketProvider):
    """东方财富网行情数据提供者。

    直接调用东方财富 HTTP API，无需 akshare 等第三方库。
    接口稳定度高，返回速度快（通常 <500ms）。
    """

    @property
    def name(self) -> str:
        return "eastmoney"

    @property
    def priority(self) -> int:
        return 10  # HTTP 直连 <500ms，实时行情快，含历史K线+前复权

    def search_stock(self, name_or_ticker: str) -> dict:
        """Search for stock by name or ticker."""
        params = {
            "input": name_or_ticker,
            "type": "14",
            "token": "D43BF722C8E33BDC906FB84D85E326E8",
        }
        resp = em_get(_SEARCH_API, params=params, timeout=10)
        resp.raise_for_status()
        data = json.loads(strip_jsonp(resp.text))

        stocks = data.get("QuotationCodeTable", {}).get("Data", [])
        if not stocks:
            raise ValueError(f"未找到股票: {name_or_ticker}")

        s = stocks[0]
        code = s.get("Code", "")
        market = s.get("MktNum", "")
        return {
            "name": s.get("Name", name_or_ticker),
            "ticker": code,
            "market": "sh" if market == "1" else "sz",
        }

    def get_quote(self, ticker: str) -> QuoteResult:
        """Get real-time quote via East Money batch API."""
        params = {
            "pn": "1",
            "pz": "1",
            "po": "1",
            "np": "1",
            "ut": _UT,
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:0 t:6,m:0 t:13,m:0 t:80",
            "fields": "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18",
            "_": str(int(time.time() * 1000)),
        }
        # If ticker is known, filter to just that stock
        if re.match(r"^[0-9]{6}$", ticker):
            params["fs"] = "m:0 t:6,m:0 t:13,m:0 t:80,m:1 t:2,m:1 t:23"

        resp = em_get(_QUOTE_API, params=params, timeout=10)
        resp.raise_for_status()
        data = json.loads(strip_jsonp(resp.text))

        items = data.get("data", {}).get("diff", [])
        # Find exact ticker match
        item = None
        for i in items:
            if i.get("f12") == ticker:
                item = i
                break
        if item is None and items:
            item = items[0]

        if item is None:
            raise ValueError(f"未找到报价: {ticker}")

        return QuoteResult(
            ticker=ticker,
            name=item.get("f14", ""),
            price=item.get("f2"),
            change_pct=item.get("f3"),
            volume=item.get("f5"),
            turnover=item.get("f6"),
        )

    def get_history(self, ticker: str, days: int = 120) -> list[OHLCV]:
        """Get historical daily K-line data."""
        sid = _secid(ticker)
        import datetime

        end = datetime.date.today().strftime("%Y%m%d")
        beg = (datetime.date.today() - datetime.timedelta(days=days + 10)).strftime("%Y%m%d")
        params = {
            "secid": sid,
            "klt": "101",  # daily
            "fqt": "1",  # 前复权
            "beg": beg,
            "end": end,
            "lmt": str(days),
            "fields1": _KLINE_FIELDS1,
            "fields2": _KLINE_FIELDS2,
            "ut": _UT,
        }
        resp = em_get(_KLINE_API, params=params, timeout=15)
        resp.raise_for_status()
        data = json.loads(strip_jsonp(resp.text))

        klines_raw = data.get("data", {}).get("klines", [])
        return [_parse_kline_row(row) for row in klines_raw]

    def get_financials(self, ticker: str) -> dict:
        """Financial data — not available via simple East Money HTTP."""
        raise NotImplementedError("EastMoney 不支持财务数据")

    def get_capital_flow(self, ticker: str, days: int = 60) -> list[CapitalFlow]:
        """Capital flow — not implemented for East Money provider."""
        raise NotImplementedError("EastMoney 不支持资金流向")

    def health_check(self) -> bool:
        """Probe East Money API availability."""
        try:
            params = {
                "pn": "1",
                "pz": "1",
                "ut": _UT,
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": "m:0 t:6,m:0 t:13",
                "fields": "f12",
            }
            resp = em_get(_QUOTE_API, params=params, timeout=5)
            resp.raise_for_status()
            data = json.loads(strip_jsonp(resp.text))
            return "data" in data
        except Exception:
            return False
