"""EasyQuotation-based market data provider — 新浪/腾讯实时行情，秒级延迟。

整合双源（腾讯优先，新浪兜底），轻量无冗余依赖。
纯实时报价，不支持历史K线、财务数据、股票搜索。
"""

from __future__ import annotations

import logging

from .base import OHLCV, BaseMarketProvider, CapitalFlow, QuoteResult

logger = logging.getLogger(__name__)


class EasyQuotationProvider(BaseMarketProvider):
    """实时行情提供者 — 新浪财经 + 腾讯财经双数据源。

    EasyQuotation 是轻量实时行情库，秒级延迟，免费无需注册。
    腾讯源数据更丰富（含涨跌幅、PE/PB、市值等），作为主源；
    新浪源作为 fallback。
    """

    @property
    def name(self) -> str:
        return "easyquotation"

    @property
    def priority(self) -> int:
        return 5  # 秒级实时行情，腾讯+新浪双源，速度最快

    def __init__(self) -> None:
        self._tencent = None
        self._sina = None

    def _get_tencent(self):
        if self._tencent is None:
            import easyquotation

            self._tencent = easyquotation.use("tencent")
        return self._tencent

    def _get_sina(self):
        if self._sina is None:
            import easyquotation

            self._sina = easyquotation.use("sina")
        return self._sina

    # ── Quote (primary capability) ──────────────────────────

    def get_quote(self, ticker: str) -> QuoteResult:
        """获取实时行情 — 腾讯优先，新浪 fallback。"""
        # Try tencent first (richer data: PE/PB/市值/涨跌幅)
        try:
            eq = self._get_tencent()
            data = eq.stocks([ticker])
            if ticker in data:
                return self._parse_tencent(ticker, data[ticker])
        except Exception:
            logger.debug("[easyquotation] tencent source failed for %s, trying sina", ticker)

        # Fallback to sina
        try:
            eq = self._get_sina()
            data = eq.stocks([ticker])
            if ticker in data:
                return self._parse_sina(ticker, data[ticker])
        except Exception:
            logger.debug("[easyquotation] sina source also failed for %s", ticker)

        return QuoteResult(ticker=ticker, name=ticker)

    @staticmethod
    def _parse_tencent(ticker: str, row: dict) -> QuoteResult:
        """Parse tencent source response into QuoteResult."""
        price = row.get("now")
        close = row.get("close")
        change_pct = row.get("涨跌(%)")  # tencent provides this directly
        if change_pct is None and price is not None and close and close > 0:
            change_pct = round((float(price) - float(close)) / float(close) * 100, 2)

        return QuoteResult(
            ticker=ticker,
            name=str(row.get("name", ticker)),
            price=float(price) if price is not None else None,
            change_pct=float(change_pct) if change_pct is not None else None,
            volume=float(row.get("volume", 0)),
            turnover=float(row.get("成交额(万)", 0)) if "成交额(万)" in row else None,
        )

    @staticmethod
    def _parse_sina(ticker: str, row: dict) -> QuoteResult:
        """Parse sina source response into QuoteResult.

        Sina doesn't provide change_pct directly — compute from now vs close.
        """
        price = row.get("now")
        close = row.get("close")
        change_pct = None
        if price is not None and close and close > 0:
            change_pct = round((float(price) - float(close)) / float(close) * 100, 2)

        return QuoteResult(
            ticker=ticker,
            name=str(row.get("name", ticker)),
            price=float(price) if price is not None else None,
            change_pct=change_pct,
            volume=float(row.get("volume", 0)),
            turnover=float(row.get("turnover", 0)) if row.get("turnover") else None,
        )

    # ── Unsupported operations ──────────────────────────────

    def search_stock(self, name_or_ticker: str) -> dict:
        """EasyQuotation 不支持股票搜索，委托给 AKShareProvider。"""
        from .akshare import AKShareProvider

        return AKShareProvider().search_stock(name_or_ticker)

    def get_history(self, ticker: str, days: int = 120) -> list[OHLCV]:
        """EasyQuotation 不支持历史K线。"""
        raise NotImplementedError("EasyQuotation 不支持历史K线")

    def get_financials(self, ticker: str) -> dict:
        """EasyQuotation 不支持财务数据。"""
        raise NotImplementedError("EasyQuotation 不支持财务数据")

    def get_capital_flow(self, ticker: str, days: int = 60) -> list[CapitalFlow]:
        """EasyQuotation 不支持资金流向。"""
        raise NotImplementedError("EasyQuotation 不支持资金流向")

    def health_check(self) -> bool:
        """快速探测腾讯源可用性。"""
        try:
            eq = self._get_tencent()
            result = eq.stocks(["600519"])
            return "600519" in result and result["600519"].get("now") is not None
        except Exception:
            return False
