"""Reference-only event and signal context providers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fin_analyse.context.models import ExternalContextRecord
from fin_analyse.market.eastmoney_client import eastmoney_datacenter


def _date10(value: Any) -> str:
    return str(value or "")[:10]


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MarketEventProvider:
    """T1 event/signal context provider.

    Records are reference-only context for apprentice cognition and do not drive
    trading decisions directly.
    """

    def get_dragon_tiger(self, ticker: str, days: int = 30) -> list[ExternalContextRecord]:
        start = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = eastmoney_datacenter(
            report_name="RPT_DAILYBILLBOARD_DETAILS",
            filter_str=f"(SECURITY_CODE=\"{ticker}\")(TRADE_DATE>='{start}')",
            page_size=50,
            sort_columns="TRADE_DATE",
            sort_types="-1",
        )
        return [self.parse_dragon_tiger(row) for row in rows]

    def get_lockup_releases(
        self, ticker: str, days_forward: int = 90
    ) -> list[ExternalContextRecord]:
        rows = eastmoney_datacenter(
            report_name="RPT_LIFT_STAGE",
            filter_str=f'(SECURITY_CODE="{ticker}")',
            page_size=50,
            sort_columns="FREE_DATE",
            sort_types="1",
        )
        return [self.parse_lockup_release(row) for row in rows]

    def get_block_trades(self, ticker: str, days: int = 30) -> list[ExternalContextRecord]:
        rows = eastmoney_datacenter(
            report_name="RPT_BLOCKTRADE_DET",
            filter_str=f'(SECURITY_CODE="{ticker}")',
            page_size=50,
            sort_columns="TRADE_DATE",
            sort_types="-1",
        )
        return [self.parse_block_trade(row) for row in rows]

    def get_shareholder_count(self, ticker: str) -> list[ExternalContextRecord]:
        rows = eastmoney_datacenter(
            report_name="RPT_HOLDERNUMLATEST",
            filter_str=f'(SECURITY_CODE="{ticker}")',
            page_size=20,
            sort_columns="END_DATE",
            sort_types="-1",
        )
        return [self.parse_shareholder_count(row) for row in rows]

    def get_hot_themes(self, date: str | None = None) -> list[ExternalContextRecord]:
        return []

    def parse_dragon_tiger(self, row: dict[str, Any]) -> ExternalContextRecord:
        ticker = str(row.get("SECURITY_CODE", ""))
        name = row.get("SECURITY_NAME_ABBR") or ticker
        date = _date10(row.get("TRADE_DATE"))
        reason = row.get("EXPLANATION") or "龙虎榜上榜"
        net_buy = _safe_float(row.get("NET_BUY_AMT"))
        return ExternalContextRecord(
            record_id=f"dragon_tiger:{ticker}:{date}",
            source="eastmoney_datacenter",
            category="event",
            ticker=ticker,
            title=f"{name} 龙虎榜",
            summary=f"{date} {name} 龙虎榜：{reason}，净买入={net_buy}",
            occurred_at=date,
            url="https://data.eastmoney.com/stock/lhb.html",
            importance=0.7,
            metadata={"reason": reason, "net_buy_amount": net_buy},
            raw=row,
        )

    def parse_lockup_release(self, row: dict[str, Any]) -> ExternalContextRecord:
        ticker = str(row.get("SECURITY_CODE", ""))
        name = row.get("SECURITY_NAME_ABBR") or ticker
        date = _date10(row.get("FREE_DATE"))
        shares = _safe_float(row.get("FREE_SHARES"))
        return ExternalContextRecord(
            f"lockup:{ticker}:{date}",
            "eastmoney_datacenter",
            "event",
            ticker,
            f"{name} 限售解禁",
            f"{date} {name} 限售解禁，解禁数量={shares}",
            date,
            metadata={"free_shares": shares},
            raw=row,
        )

    def parse_block_trade(self, row: dict[str, Any]) -> ExternalContextRecord:
        ticker = str(row.get("SECURITY_CODE", ""))
        name = row.get("SECURITY_NAME_ABBR") or ticker
        date = _date10(row.get("TRADE_DATE"))
        price = _safe_float(row.get("DEAL_PRICE"))
        premium = _safe_float(row.get("PREMIUM_RATIO"))
        return ExternalContextRecord(
            f"block_trade:{ticker}:{date}",
            "eastmoney_datacenter",
            "event",
            ticker,
            f"{name} 大宗交易",
            f"{date} {name} 大宗交易，成交价={price}，溢价率={premium}%",
            date,
            metadata={"deal_price": price, "premium_ratio": premium},
            raw=row,
        )

    def parse_shareholder_count(self, row: dict[str, Any]) -> ExternalContextRecord:
        ticker = str(row.get("SECURITY_CODE", ""))
        name = row.get("SECURITY_NAME_ABBR") or ticker
        date = _date10(row.get("END_DATE"))
        holder_num = _safe_float(row.get("HOLDER_NUM"))
        return ExternalContextRecord(
            f"shareholder:{ticker}:{date}",
            "eastmoney_datacenter",
            "shareholder",
            ticker,
            f"{name} 股东户数",
            f"{date} {name} 股东户数={holder_num}",
            date,
            metadata={"holder_num": holder_num},
            raw=row,
        )

    def parse_hot_theme(self, row: dict[str, Any]) -> ExternalContextRecord:
        ticker = str(row.get("code", ""))
        name = row.get("name") or ticker
        date = _date10(row.get("date"))
        reason = row.get("reason") or "热点题材"
        return ExternalContextRecord(
            f"hot_theme:{ticker}:{date}:{reason}",
            "ths_hot_theme",
            "theme",
            ticker,
            f"{name} 热点题材",
            f"{date} {name} 热点题材：{reason}",
            date,
            importance=0.6,
            metadata={"reason": reason},
            raw=row,
        )
