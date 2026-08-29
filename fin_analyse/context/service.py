"""External context collection service for apprentice agents."""

from __future__ import annotations

import logging
from collections.abc import Callable

from .models import ContextRequestScope, ExternalContextBundle, ExternalContextRecord

logger = logging.getLogger(__name__)

_DEFAULT_INCLUDE = ["dragon_tiger", "research", "announcement", "dividend", "fund_flow"]
_DISABLED_SOURCE_WARNINGS = {
    "news": "news: 数据源暂未启用，当前仅支持 parser 测试",
    "hot_theme": "hot_theme: 数据源暂未启用，当前仅支持 parser 测试",
}


class ExternalContextService:
    """Collect reference-only context for a ticker.

    Provider imports are lazy to avoid circular imports: market/events and
    market/auxiliary both import from context/models, so context/service must
    not import them at module level.
    """

    def __init__(self, events=None, reports=None, auxiliary=None, announcements=None):
        if events is None:
            from fin_analyse.market.events import MarketEventProvider

            events = MarketEventProvider()
        if reports is None:
            from fin_analyse.researcher.providers.eastmoney_reports import ResearchReportProvider

            reports = ResearchReportProvider()
        if auxiliary is None:
            from fin_analyse.market.auxiliary import AuxiliaryDataProvider

            auxiliary = AuxiliaryDataProvider()
        if announcements is None:
            from fin_analyse.researcher.providers.cninfo import AnnouncementProvider

            announcements = AnnouncementProvider()
        self.events = events
        self.reports = reports
        self.auxiliary = auxiliary
        self.announcements = announcements

    def collect_for_ticker(
        self,
        ticker: str,
        *,
        include: list[str] | None = None,
        scope: ContextRequestScope | None = None,
    ) -> ExternalContextBundle:
        request_scope = scope or ContextRequestScope()
        include_set = set(include or _DEFAULT_INCLUDE)
        records: list[ExternalContextRecord] = []
        warnings: list[str] = []

        def collect(name: str, fn: Callable[[], list[ExternalContextRecord]]) -> None:
            if name not in include_set:
                return
            try:
                records.extend(fn())
            except Exception as exc:
                logger.warning(
                    "Provider %s failed for ticker %s: %s",
                    name,
                    ticker,
                    exc,
                    exc_info=True,
                )
                warnings.append(f"{name}: 数据暂时不可用")

        collect("dragon_tiger", lambda: self.events.get_dragon_tiger(ticker))
        collect("lockup", lambda: self.events.get_lockup_releases(ticker))
        collect("block_trade", lambda: self.events.get_block_trades(ticker))
        collect("shareholder", lambda: self.events.get_shareholder_count(ticker))
        collect("research", lambda: self.reports.get_stock_reports(ticker, max_pages=2))
        collect("announcement", lambda: self.announcements.get_announcements(ticker))
        collect("dividend", lambda: self.auxiliary.get_dividends(ticker))
        collect("fund_flow", lambda: self.auxiliary.get_fund_flow(ticker))

        for name, warning in _DISABLED_SOURCE_WARNINGS.items():
            if name in include_set:
                warnings.append(warning)

        records.sort(key=lambda r: (r.importance, r.occurred_at), reverse=True)
        return ExternalContextBundle(
            ticker=ticker,
            records=records,
            warnings=warnings,
            scope=request_scope,
        )

    def to_prompt_context(self, bundle: ExternalContextBundle, max_items: int = 20) -> str:
        lines = ["以下为外部市场上下文，仅供参考，不代表老师认知，不构成交易建议："]
        for record in bundle.records[:max_items]:
            lines.append(
                f"- [{record.category}] {record.occurred_at} {record.title}: {record.summary}"
            )
        if bundle.warnings:
            lines.append("外部上下文抓取警告：" + "; ".join(bundle.warnings))
        return "\n".join(lines)
