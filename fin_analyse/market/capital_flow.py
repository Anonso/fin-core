"""Capital flow data — 融资融券 + 北向资金，每日更新。

数据源:
- stock_margin_detail_sse/szse: 沪深两市融资融券明细 (akshare, 非 eastmoney)
- stock_hsgt_individual_em: 北向资金个股持股 (akshare)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 北京时间
TZ = timezone(timedelta(hours=8))


def _latest_trading_day() -> str:
    """Get the latest available trading day (T-1 or earlier)."""
    today = datetime.now(TZ)
    # Try today, yesterday, and up to 5 days back
    for days_back in range(5):
        d = today - timedelta(days=days_back)
        # Skip weekends
        if d.weekday() >= 5:
            continue
        date_str = d.strftime("%Y%m%d")
        # Quick check: try SSE margin to see if data exists
        try:
            import akshare as ak

            df = ak.stock_margin_detail_sse(date=date_str)
            if len(df) > 0:
                return date_str
        except Exception:
            continue
    return today.strftime("%Y%m%d")


def _is_sse(ticker: str) -> bool:
    """判断是否上交所股票（6开头=SSE, 0/3开头=SZSE）。"""
    return ticker.startswith("6")


def get_margin_detail(ticker: str) -> dict:
    """获取单只股票的融资融券数据。自动回溯最近交易日。

    Returns {date, margin_balance, margin_buy, short_volume, short_sell, total_balance}
    """
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz)

    for days_back in range(7):
        d = today - timedelta(days=days_back)
        if d.weekday() >= 5:
            continue
        date_str = d.strftime("%Y%m%d")
        try:
            import akshare as ak

            if _is_sse(ticker):
                df = ak.stock_margin_detail_sse(date=date_str)
                code_col = "标的证券代码"
            else:
                df = ak.stock_margin_detail_szse(date=date_str)
                code_col = "证券代码" if "证券代码" in df.columns else "标的证券代码"

            if df.empty:
                continue

            row = df[df[code_col] == ticker]
            if row.empty:
                continue

            r = row.iloc[0]
            return {
                "date": date_str,
                "margin_balance": _safe_float(r.get("融资余额")),
                "margin_buy": _safe_float(r.get("融资买入额")),
                "short_volume": _safe_float(r.get("融券余量")),
                "short_sell": _safe_float(r.get("融券卖出量")),
                "total_balance": _safe_float(r.get("融资融券余额")),
            }
        except Exception:
            continue

    return _empty_margin()


def get_northbound_detail(ticker: str) -> dict:
    """获取单只股票的北向资金持股数据。

    Returns {date, shares_held, market_value, pct_of_float, daily_change_shares, daily_change_value}
    """
    try:
        import akshare as ak

        df = ak.stock_hsgt_individual_em(symbol=ticker)
        if df.empty:
            return _empty_northbound()

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else latest

        shares_held = _safe_float(latest.get("持股数量"))
        shares_prev = _safe_float(prev.get("持股数量"))
        daily_change = (
            round(shares_held - shares_prev, 0)
            if shares_held is not None and shares_prev is not None
            else None
        )

        return {
            "date": str(latest.get("持股日期", "")),
            "shares_held": shares_held,
            "market_value": _safe_float(latest.get("持股市值")),
            "pct_of_float": _safe_float(latest.get("持股数量占A股百分比")),
            "daily_change_shares": daily_change,
            "daily_change_value": _safe_float(latest.get("今日增持资金")),
        }
    except Exception as exc:
        logger.debug("Northbound detail failed for %s: %s", ticker, exc)
        return _empty_northbound()


def get_capital_flow_summary(ticker: str) -> dict:
    """获取资金面综合摘要 — 融资融券 + 北向。

    Returns dict with margin, northbound, and combined flow_score (0-100).
    """
    margin = get_margin_detail(ticker)
    north = get_northbound_detail(ticker)

    # Flow score: positive for buying, negative for selling
    flow_score = 50.0  # neutral

    # Margin: increasing = bullish
    if margin.get("margin_buy") and margin.get("margin_balance"):
        margin_buy = float(margin["margin_buy"])
        margin_bal = float(margin["margin_balance"])
        if margin_bal > 0 and margin_buy > 0:
            ratio = margin_buy / margin_bal
            if ratio > 0.05:
                flow_score += 10  # significant margin buying
            elif ratio > 0.02:
                flow_score += 5

    # Northbound: increasing holdings = bullish
    if north.get("daily_change_shares"):
        change = float(north["daily_change_shares"])
        if change > 0:
            flow_score += 15  # northbound buying
        elif change < 0:
            flow_score -= 10  # northbound selling

    # Northbound: high ownership % = institutional confidence
    if north.get("pct_of_float"):
        pct = float(north["pct_of_float"])
        if pct > 5:
            flow_score += 10
        elif pct > 2:
            flow_score += 5

    return {
        "ticker": ticker,
        "margin": margin,
        "northbound": north,
        "flow_score": round(min(100.0, max(0.0, flow_score)), 1),
    }


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _empty_margin() -> dict:
    return {
        "date": "",
        "margin_balance": None,
        "margin_buy": None,
        "short_volume": None,
        "short_sell": None,
        "total_balance": None,
    }


def _empty_northbound() -> dict:
    return {
        "date": "",
        "shares_held": None,
        "market_value": None,
        "pct_of_float": None,
        "daily_change_shares": None,
        "daily_change_value": None,
    }
