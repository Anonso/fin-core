"""Auxiliary external context providers for news, dividends, and fund flow."""

from __future__ import annotations

from typing import Any

from fin_analyse.context.models import ExternalContextRecord
from fin_analyse.market.eastmoney_client import eastmoney_datacenter


def _date10(value: Any) -> str:
    return str(value or "")[:10]


class AuxiliaryDataProvider:
    """T3 auxiliary context provider; records are reference-only."""

    def get_stock_news(self, ticker: str, limit: int = 20) -> list[ExternalContextRecord]:
        return []

    def get_dividends(self, ticker: str) -> list[ExternalContextRecord]:
        rows = eastmoney_datacenter(
            report_name="RPT_SHAREBONUS_DET",
            filter_str=f'(SECURITY_CODE="{ticker}")',
            page_size=50,
            sort_columns="REPORT_DATE",
            sort_types="-1",
        )
        return [self.parse_dividend(row) for row in rows]

    def get_fund_flow(self, ticker: str, period: str = "daily") -> list[ExternalContextRecord]:
        rows = eastmoney_datacenter(
            report_name="RPT_STOCK_FUND_FLOW",
            filter_str=f'(SECURITY_CODE="{ticker}")',
            page_size=120,
            sort_columns="TRADE_DATE",
            sort_types="-1",
        )
        return [self.parse_fund_flow(row) for row in rows]

    def parse_stock_news(self, row: dict[str, Any], ticker: str) -> ExternalContextRecord:
        title = str(row.get("title") or row.get("TITLE") or "个股新闻")
        date = _date10(row.get("showTime") or row.get("SHOW_TIME") or row.get("date"))
        summary = str(row.get("summary") or title)
        return ExternalContextRecord(
            record_id=f"news:{ticker}:{date}:{title}",
            source="eastmoney_news",
            category="news",
            ticker=ticker,
            title=title,
            summary=f"{date} 新闻：{summary}。新闻为外部参考，不写入老师认知。",
            occurred_at=date,
            url=str(row.get("url") or row.get("URL") or ""),
            importance=0.4,
            raw=row,
        )

    def parse_dividend(self, row: dict[str, Any]) -> ExternalContextRecord:
        ticker = str(row.get("SECURITY_CODE", ""))
        name = row.get("SECURITY_NAME_ABBR") or ticker
        date = _date10(row.get("REPORT_DATE"))
        desc = row.get("ASSIGNDSCRPT") or row.get("IMPL_PLAN_PROFILE") or "分红送转"
        return ExternalContextRecord(
            record_id=f"dividend:{ticker}:{date}",
            source="eastmoney_datacenter",
            category="dividend",
            ticker=ticker,
            title=f"{name} 分红送转",
            summary=f"{date} {name} 分红送转：{desc}",
            occurred_at=date,
            importance=0.4,
            metadata={"dividend_desc": desc},
            raw=row,
        )

    def parse_fund_flow(self, row: dict[str, Any]) -> ExternalContextRecord:
        ticker = str(row.get("SECURITY_CODE", ""))
        name = row.get("SECURITY_NAME_ABBR") or ticker
        date = _date10(row.get("TRADE_DATE"))
        main_net = row.get("MAIN_NET_INFLOW")
        return ExternalContextRecord(
            record_id=f"fund_flow:{ticker}:{date}",
            source="eastmoney_datacenter",
            category="capital",
            ticker=ticker,
            title=f"{name} 资金流",
            summary=f"{date} {name} 主力净流入={main_net}",
            occurred_at=date,
            importance=0.5,
            metadata={"main_net_inflow": main_net},
            raw=row,
        )
