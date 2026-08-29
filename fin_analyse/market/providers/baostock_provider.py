"""BaoStock provider — 免费 A 股数据，无需注册/token。

pip install baostock (需要预先安装)
"""

from __future__ import annotations

import logging

from .base import OHLCV, BaseMarketProvider, CapitalFlow, QuoteResult

logger = logging.getLogger(__name__)


class BaoStockProvider(BaseMarketProvider):
    """BaoStock 数据提供者。

    BaoStock 完全免费，无需注册，专为 A 股设计。
    支持历史 K 线（日/周/月/5分钟/15分钟/30分钟/60分钟）和复权。
    不支持实时行情。
    """

    @property
    def name(self) -> str:
        return "baostock"

    @property
    def priority(self) -> int:
        return 20  # 历史K线最准+复权，但不支持实时行情

    def search_stock(self, name_or_ticker: str) -> dict:
        import baostock as bs

        bs.login()
        try:
            if name_or_ticker.isdigit() and len(name_or_ticker) == 6:
                code = (
                    f"sh.{name_or_ticker}"
                    if name_or_ticker.startswith(("6", "68"))
                    else f"sz.{name_or_ticker}"
                )
                rs = bs.query_stock_basic(code=code)
            else:
                rs = bs.query_stock_basic(code_name=name_or_ticker)
            rows = rs.get_data() if rs.error_code == "0" else []
            if isinstance(rows, list) or rows.empty:
                raise ValueError(f"未找到股票: {name_or_ticker}")
            row = rows.iloc[0]
            code_str = str(row["code"])
            ticker = code_str.split(".")[-1]
            return {
                "name": str(row.get("code_name", name_or_ticker)),
                "ticker": ticker,
                "market": "sh" if code_str.startswith("sh") else "sz",
            }
        finally:
            bs.logout()

    def get_quote(self, ticker: str) -> QuoteResult:
        raise NotImplementedError("BaoStock 不支持实时行情，请使用 EastMoney 或 mootdx")

    def get_history(self, ticker: str, days: int = 120) -> list[OHLCV]:
        import datetime

        import baostock as bs

        bs.login()
        try:
            bs_code = f"sh.{ticker}" if ticker.startswith(("6", "68")) else f"sz.{ticker}"
            end = datetime.date.today().strftime("%Y-%m-%d")
            beg = (datetime.date.today() - datetime.timedelta(days=days + 10)).strftime("%Y-%m-%d")
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,close,high,low,volume,amount,turn,preclose,pctChg",
                start_date=beg,
                end_date=end,
                frequency="d",
                adjustflag="2",  # 前复权
            )
            if rs.error_code != "0":
                raise ValueError(f"BaoStock error: {rs.error_msg}")
            rows = rs.get_data()
            if isinstance(rows, list) or rows.empty:
                return []
            return [
                OHLCV(
                    date=str(r["date"]),
                    open=float(r["open"]),
                    close=float(r["close"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    volume=float(r["volume"]),
                    turnover=float(r["amount"]) if r["amount"] else None,
                )
                for _, r in rows.iterrows()
            ]
        finally:
            bs.logout()

    def get_financials(self, ticker: str) -> dict:
        raise NotImplementedError("BaoStock 不支持财务数据")

    def get_capital_flow(self, ticker: str, days: int = 60) -> list[CapitalFlow]:
        raise NotImplementedError("BaoStock 不支持资金流向")

    def health_check(self) -> bool:
        try:
            import baostock as bs

            bs.login()
            rs = bs.query_stock_basic(code="sh.600519")
            ok: bool = rs.error_code == "0"
            bs.logout()
            return ok
        except Exception:
            return False
