"""Reference-only research enrichment for GuoTeacherResearchPackage.

Enrichment fills fact-oriented fields using external research/news/market context.
It must never alter teacher attribution, direct-knowledge status, confidence,
action implications, position sizing, or risk guardrails.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fin_analyse.context.models import ExternalContextBundle, ExternalContextRecord

if TYPE_CHECKING:
    from fin_analyse.cognition.research_package import GuoTeacherResearchPackage


class ResearchReferenceEnricher:
    """Enrich a research package with reference-only external context."""

    def enrich(
        self,
        package: GuoTeacherResearchPackage,
        bundle: ExternalContextBundle | None,
    ) -> GuoTeacherResearchPackage:
        if bundle is None or not bundle.records:
            return package

        records = bundle.records[:5]
        reference_context_used = self._records_to_reference_context(records)

        industry_chain_position = self._industry_chain_text(package.subject, records)
        expectation_gap = self._expectation_gap_text(records)
        realization_tempo = self._realization_tempo_text(records)
        external_verification_actions = self._verification_actions(records)
        external_risks = self._external_risks(records)

        warnings = list(package.warnings)
        warnings.extend(bundle.warnings)
        warnings.append("外部上下文仅用于事实性补充，不构成老师观点，也不改变来源边界。")
        for record in records:
            if record.is_decision_factor:
                warnings.append(f"外部记录 [{record.title}] 被标记为决策因素，需警惕过度归因。")

        next_verification_actions = list(package.next_verification_actions) + list(
            external_verification_actions
        )
        review_hooks = list(package.review_hooks) + [
            f"外部风险/反证：{risk}" for risk in external_risks
        ]

        return replace(
            package,
            industry_chain_position=industry_chain_position,
            expectation_gap=expectation_gap,
            realization_tempo=realization_tempo,
            next_verification_actions=next_verification_actions,
            review_hooks=review_hooks,
            reference_context_used=reference_context_used,
            warnings=warnings,
            advisory_only=True,
        )

    @staticmethod
    def _records_to_reference_context(records: list[ExternalContextRecord]) -> list[dict[str, Any]]:
        return [
            {
                "record_id": record.record_id,
                "source": record.source,
                "category": record.category,
                "ticker": record.ticker,
                "title": record.title,
                "reference_only": True,
            }
            for record in records
        ]

    def _industry_chain_text(self, subject, records: list[ExternalContextRecord]) -> str:
        target = subject.company or subject.ticker or "该主题"
        summaries = "；".join(
            f"[{record.category}] {record.title}: {record.summary[:80]}" for record in records
        )
        return (
            f"{target} 的产业链/事实位置（外部参考）：{summaries}。"
            "以上仅作事实参考，不替代老师认知。"
        )

    def _expectation_gap_text(self, records: list[ExternalContextRecord]) -> str:
        points = [f"- [{record.category}] {record.title}" for record in records]
        return (
            "外部观察到的预期差/事实缺口：\n" + "\n".join(points)
            if points
            else "预期差需要结合老师直接证据与外部事实继续验证。"
        )

    def _realization_tempo_text(self, records: list[ExternalContextRecord]) -> str:
        near_term = [
            record.title for record in records if record.category in {"announcement", "event"}
        ]
        if near_term:
            return f"外部催化/兑现窗口参考：{'；'.join(near_term[:3])}。需以老师框架重新评估。"
        return "兑现节奏需要结合催化、公告、价格或后续老师观点验证。"

    def _verification_actions(self, records: list[ExternalContextRecord]) -> list[str]:
        actions = []
        categories = {record.category for record in records}
        if "financial" in categories or "report" in categories:
            actions.append("核对财报与一致性预期")
        if "announcement" in categories:
            actions.append("跟踪公司公告")
        if "news" in categories or "market" in categories:
            actions.append("观察新闻与价格验证")
        if not actions:
            actions.append("交叉验证外部信息来源")
        return actions

    def _external_risks(self, records: list[ExternalContextRecord]) -> list[str]:
        risks: list[str] = []
        for record in records:
            if record.is_decision_factor:
                risks.append(f"{record.title} 被标记为决策因素，需警惕过度归因")
            if record.category in {"risk", "bear_case"}:
                risks.append(record.summary[:100])
        if not risks:
            risks.append("外部信息未覆盖完整风险场景")
        return risks
