"""MooTDX-based market data provider — 通达信 TCP 协议，免费、不封 IP。

用于替代被代理拦截的 eastmoney 接口。
"""

from .base import OHLCV, BaseMarketProvider, CapitalFlow, QuoteResult


class MootdxProvider(BaseMarketProvider):
    """通达信 TCP 协议行情数据，免费、稳定、无需 API Key."""

    @property
    def name(self) -> str:
        return "mootdx"

    @property
    def priority(self) -> int:
        return 15  # TCP 稳定但 K 线近似报价，速度不及 HTTP 实时源

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from fin_analyse.market.tdx_client import tdx_client

            self._client = tdx_client()
        return self._client

    def search_stock(self, name_or_ticker: str) -> dict:
        """mootdx 不支持名称搜索，委托给 AKShareProvider。"""
        from .akshare import AKShareProvider

        return AKShareProvider().search_stock(name_or_ticker)

    def get_quote(self, ticker: str) -> QuoteResult:
        """通过最新 K 线获取近似报价。"""
        try:
            df = self.client.bars(symbol=ticker, frequency=9, offset=2)
            if df.empty:
                return QuoteResult(ticker=ticker, name=ticker)
            row = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else row
            change_pct = None
            if prev["close"] and prev["close"] > 0:
                change_pct = round(
                    (float(row["close"]) - float(prev["close"])) / float(prev["close"]) * 100, 2
                )
            return QuoteResult(
                ticker=ticker,
                name=ticker,
                price=float(row["close"]),
                change_pct=change_pct,
                volume=float(row.get("volume", 0)),
                turnover=float(row.get("amount", 0)) if "amount" in row else None,
            )
        except Exception:
            return QuoteResult(ticker=ticker, name=ticker)

    def get_history(self, ticker: str, days: int = 120) -> list[OHLCV]:
        """获取日 K 线历史数据。"""
        try:
            df = self.client.bars(symbol=ticker, frequency=9, offset=min(days, 800))
            if df.empty:
                return []
            results = []
            for idx, row in df.iterrows():
                results.append(
                    OHLCV(
                        date=idx.strftime("%Y-%m-%d")
                        if hasattr(idx, "strftime")
                        else str(idx)[:10],
                        open=float(row.get("open", 0)),
                        close=float(row.get("close", 0)),
                        high=float(row.get("high", 0)),
                        low=float(row.get("low", 0)),
                        volume=float(row.get("volume", 0)),
                        turnover=float(row.get("amount", 0)) if "amount" in row else None,
                    )
                )
            return results
        except Exception:
            return []

    def get_financials(self, ticker: str) -> dict:
        """mootdx 不支持财务数据。"""
        raise NotImplementedError("mootdx 不支持财务数据")

    def get_capital_flow(self, ticker: str, days: int = 60) -> list[CapitalFlow]:
        raise NotImplementedError("mootdx 不支持资金流向")
