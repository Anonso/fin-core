"""AKShare-based market data provider."""

import os

from .base import OHLCV, BaseMarketProvider, CapitalFlow, QuoteResult

os.environ["AKSHARE_DISABLE_PROGRESS"] = "1"


class AKShareProvider(BaseMarketProvider):
    """Market data from akshare (free, broad A-share coverage)."""

    _stock_df = None  # class-level cache for stock list

    @property
    def name(self) -> str:
        return "akshare"

    @property
    def priority(self) -> int:
        return 30  # 最全但最不稳定 — HTTP 爬虫，常超时

    # ── Stock search ──────────────────────────────────────

    def search_stock(self, name_or_ticker: str) -> dict:
        # ── Fast path: local offline maps (manual + A-share, O(1), no network) ──
        # lookup_company() covers manual entries (US/HK/unlisted, e.g. 华为) AND
        # the offline A-share snapshot — a superset of search_a_share().
        try:
            from fin_analyse.market.company_map import (
                _load_a_share_map,
                fuzzy_search_a_share,
                lookup_company,
            )

            result = lookup_company(name_or_ticker)
            if result:
                return {
                    "name": result.get("name", name_or_ticker),
                    "ticker": result.get("ticker") or name_or_ticker,
                    "market": result.get("market", "unknown"),
                }

            # Partial/substring match against the offline snapshot, mirroring the
            # legacy live-path contains() behaviour (e.g. 茅台 → 贵州茅台) — still
            # O(n) in-memory, no network.
            fuzzy = fuzzy_search_a_share(name_or_ticker)
            if fuzzy:
                return {
                    "name": fuzzy.get("name", name_or_ticker),
                    "ticker": fuzzy.get("ticker") or name_or_ticker,
                    "market": fuzzy.get("market", "unknown"),
                }

            # The offline A-share snapshot is a full copy of what
            # ``stock_info_a_code_name()`` returns.  When it is present a miss is
            # authoritative: the name is not a resolvable A-share, so skip the
            # slow live download (5,528 stocks) and degrade fast.  Live akshare
            # is only worth trying when the snapshot is unavailable.
            if _load_a_share_map():
                return {"name": name_or_ticker, "ticker": name_or_ticker, "market": "A股"}
        except Exception:
            pass

        # ── Slow path: akshare network call (only when offline snapshot missing) ──
        try:
            import akshare as ak

            if AKShareProvider._stock_df is None:
                AKShareProvider._stock_df = ak.stock_info_a_code_name()
            df = AKShareProvider._stock_df
            match = df[df["name"].str.contains(name_or_ticker, na=False)]
            if match.empty:
                match = df[df["code"].str.contains(name_or_ticker, na=False)]
            if match.empty:
                return {"name": name_or_ticker, "ticker": name_or_ticker, "market": "unknown"}
            row = match.iloc[0]
            return {"name": str(row["name"]), "ticker": str(row["code"]), "market": "A股"}
        except Exception:
            return {"name": name_or_ticker, "ticker": name_or_ticker, "market": "A股"}

    # ── Quote ─────────────────────────────────────────────

    def get_quote(self, ticker: str) -> QuoteResult:
        try:
            import akshare as ak

            df = ak.stock_zh_a_spot_em()
            row = df[df["代码"] == ticker]
            if row.empty:
                return QuoteResult(ticker=ticker, name=ticker)
            r = row.iloc[0]
            return QuoteResult(
                ticker=ticker,
                name=str(r.get("名称", ticker)),
                price=float(r.get("最新价", 0)),
                change_pct=float(r.get("涨跌幅", 0)),
                volume=float(r.get("成交量", 0)),
                turnover=float(r.get("成交额", 0)),
            )
        except Exception:
            return QuoteResult(ticker=ticker, name=ticker)

    # ── History ───────────────────────────────────────────

    def get_history(self, ticker: str, days: int = 120) -> list[OHLCV]:
        # Try yfinance first (handles A-share suffix conversion)
        try:
            import yfinance as yf

            symbol = ticker
            if ticker.isdigit() and len(ticker) == 6:
                prefix = "0" if ticker.startswith(("6", "9")) else "1"
                symbol = f"{ticker}.{'SS' if prefix == '0' else 'SZ'}"
            tf = yf.Ticker(symbol)
            df = tf.history(period=f"{min(days, 360)}d")
            results = []
            for idx, row in df.iterrows():
                results.append(
                    OHLCV(
                        date=idx.strftime("%Y-%m-%d"),
                        open=float(row.get("Open", 0)),
                        close=float(row.get("Close", 0)),
                        high=float(row.get("High", 0)),
                        low=float(row.get("Low", 0)),
                        volume=float(row.get("Volume", 0)),
                    )
                )
            return results
        except Exception:
            pass

        # Fallback to akshare
        try:
            import akshare as ak

            df = ak.stock_zh_a_hist(symbol=ticker, period="daily", adjust="qfq")
            results = []
            for _, row in df.tail(days).iterrows():
                results.append(
                    OHLCV(
                        date=str(row.get("日期", "")),
                        open=float(row.get("开盘", 0)),
                        close=float(row.get("收盘", 0)),
                        high=float(row.get("最高", 0)),
                        low=float(row.get("最低", 0)),
                        volume=float(row.get("成交量", 0)),
                    )
                )
            return results
        except Exception:
            return []

    # ── Financials ────────────────────────────────────────

    def get_financials(self, ticker: str) -> dict:
        """获取最新一期财务数据。字段值自动处理单位（亿/万/%）。

        Returns dict with ``latest`` containing revenue, net_profit, eps, roe,
        bps, debt_ratio, net_margin, cf_per_share, revenue_yoy, net_profit_yoy.
        """
        try:
            import akshare as ak

            from fin_analyse.market.valuation import _safe_float

            df = ak.stock_financial_abstract_ths(symbol=ticker, indicator="按报告期")
            latest: dict = {}
            if not df.empty:
                row = df.iloc[-1]
                latest = {
                    "date": str(row.get("报告期", "")),
                    "revenue": _safe_float(row.get("营业总收入")),
                    "revenue_yoy": _safe_float(row.get("营业总收入同比增长率")),
                    "net_profit": _safe_float(row.get("净利润")),
                    "net_profit_yoy": _safe_float(row.get("净利润同比增长率")),
                    "eps": _safe_float(row.get("基本每股收益")),
                    "bps": _safe_float(row.get("每股净资产")),
                    "cf_per_share": _safe_float(row.get("每股经营现金流")),
                    "roe": _safe_float(row.get("净资产收益率")),
                    "net_margin": _safe_float(row.get("销售净利率")),
                    "debt_ratio": _safe_float(row.get("资产负债率")),
                }
            return {"ticker": ticker, "name": ticker, "latest": latest}
        except Exception:
            return {"ticker": ticker, "name": ticker, "latest": {}}

    # ── Capital flow ─────────────────────────────────────

    def get_capital_flow(self, ticker: str, days: int = 60) -> list[CapitalFlow]:
        """获取个股资金流向（融资融券+北向）。"""
        try:
            from fin_analyse.market.capital_flow import get_margin_detail, get_northbound_detail

            margin = get_margin_detail(ticker)
            north = get_northbound_detail(ticker)
            date = margin.get("date") or north.get("date") or ""
            return [
                CapitalFlow(
                    date=date,
                    northbound_net=north.get("daily_change_value"),
                    main_net=None,  # 主力资金需 eastmoney（当前被代理拦截）
                    margin_balance=margin.get("margin_balance"),
                    short_balance=margin.get("short_sell"),
                )
            ]
        except Exception:
            return []
