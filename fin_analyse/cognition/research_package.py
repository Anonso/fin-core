"""Unified Guo-teacher research judgment package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fin_analyse.cognition.models import PersonaAnalysis
from fin_analyse.cognition.research_enricher import ResearchReferenceEnricher
from fin_analyse.context.models import ExternalContextBundle
from fin_analyse.utils.ids import stable_id


@dataclass(frozen=True)
class ResearchPackageSubject:
    company: str | None = None
    ticker: str | None = None
    topic: str | None = None
    source_type: str = "conversation"

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "ticker": self.ticker,
            "topic": self.topic,
            "source_type": self.source_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResearchPackageSubject:
        return cls(
            company=data.get("company"),
            ticker=data.get("ticker"),
            topic=data.get("topic"),
            source_type=data.get("source_type", "conversation"),
        )


@dataclass(frozen=True)
class GuoTeacherResearchPackage:
    package_id: str
    teacher_id: str
    analysis_id: str | None
    subject: ResearchPackageSubject
    topic_priority: str
    industry_chain_position: str
    expectation_gap: str
    realization_tempo: str
    risk_brake: list[str]
    next_verification_actions: list[str]
    review_hooks: list[str]
    source_classification: dict[str, Any]
    evidence_gap: dict[str, Any]
    confidence_boundary: dict[str, Any]
    confidence: float | None
    quality_mode: str = "standard"
    moa_audit: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    reference_context_used: list[dict[str, Any]] = field(default_factory=list)
    advisory_only: bool = True
    execution_allowed: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        # S-024: Explicit source boundary contract
        external_used = bool(self.reference_context_used)
        has_direct = bool(
            self.source_classification.get("direct_coverage", {}).get("available")
            or self.source_classification.get("trace_count", 0) > 0
        )
        source_boundary = {
            "primary_evidence_policy": "星大派文章/老师原文 ReasoningTrace 为唯一主证据",
            "external_data_policy": "S-010 外部感官数据仅 reference-only，不覆盖老师 stance，不写入 persona/pattern",
            "has_teacher_direct_evidence": has_direct,
            "external_context_used": external_used,
            "methodology_transfer_note": (
                "无老师直接证据，基于方法论框架推断" if not has_direct else "有老师直接证据支撑"
            ),
        }

        return {
            "package_id": self.package_id,
            "teacher_id": self.teacher_id,
            "analysis_id": self.analysis_id,
            "subject": self.subject.to_dict(),
            "topic_priority": self.topic_priority,
            "industry_chain_position": self.industry_chain_position,
            "expectation_gap": self.expectation_gap,
            "realization_tempo": self.realization_tempo,
            "risk_brake": list(self.risk_brake),
            "next_verification_actions": list(self.next_verification_actions),
            "review_hooks": list(self.review_hooks),
            "source_classification": dict(self.source_classification),
            "evidence_gap": dict(self.evidence_gap),
            "confidence_boundary": dict(self.confidence_boundary),
            "source_boundary": source_boundary,
            "confidence": self.confidence,
            "quality_mode": self.quality_mode,
            "moa_audit": dict(self.moa_audit) if self.moa_audit else None,
            "warnings": list(self.warnings),
            "needs_human_review": self.needs_human_review,
            "reference_context_used": list(self.reference_context_used),
            "advisory_only": self.advisory_only,
            "execution_allowed": self.execution_allowed,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuoTeacherResearchPackage:
        moa_audit = data.get("moa_audit")
        return cls(
            package_id=data["package_id"],
            teacher_id=data.get("teacher_id", "guo"),
            analysis_id=data.get("analysis_id"),
            subject=ResearchPackageSubject.from_dict(data.get("subject", {})),
            topic_priority=data.get("topic_priority", "需要结合证据优先级继续观察。"),
            industry_chain_position=data.get("industry_chain_position", "产业链位置需要补充验证。"),
            expectation_gap=data.get("expectation_gap", "预期差需要补充验证。"),
            realization_tempo=data.get("realization_tempo", "兑现节奏需要补充验证。"),
            risk_brake=list(data.get("risk_brake", [])),
            next_verification_actions=list(data.get("next_verification_actions", [])),
            review_hooks=list(data.get("review_hooks", [])),
            source_classification=dict(data.get("source_classification", {})),
            evidence_gap=dict(data.get("evidence_gap", {})),
            confidence_boundary=dict(data.get("confidence_boundary", {})),
            confidence=data.get("confidence"),
            quality_mode=data.get("quality_mode", "standard"),
            moa_audit=dict(moa_audit) if moa_audit else None,
            warnings=list(data.get("warnings", [])),
            needs_human_review=bool(data.get("needs_human_review", False)),
            reference_context_used=list(data.get("reference_context_used", [])),
            advisory_only=bool(data.get("advisory_only", True)),
            execution_allowed=bool(data.get("execution_allowed", False)),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
        )


class ResearchPackageBuilder:
    """Build a structured research package from existing cognition analysis."""

    def build_from_persona_analysis(
        self,
        analysis: PersonaAnalysis,
        *,
        subject: ResearchPackageSubject | None = None,
        reference_context_used: list[dict[str, Any]] | None = None,
        external_context_bundle: ExternalContextBundle | None = None,
    ) -> GuoTeacherResearchPackage:
        subject = subject or ResearchPackageSubject(
            company=analysis.company,
            ticker=analysis.ticker,
            source_type="conversation",
        )
        source_classification = self._source_classification(analysis)
        evidence_gap = self._evidence_gap(analysis)
        confidence_boundary = self._confidence_boundary(analysis)
        warnings = self._warnings(analysis, confidence_boundary)
        needs_human_review = analysis.confidence < 0.5 or bool(analysis.unsupported_claims)
        if confidence_boundary.get("level") == "low":
            needs_human_review = True

        package_id = stable_id(
            analysis.analysis_id,
            subject.company or "",
            subject.ticker or "",
            subject.source_type,
            prefix="grp:",
        )

        package = GuoTeacherResearchPackage(
            package_id=package_id,
            teacher_id=(analysis.metadata.get("teacher_id", "guo") if analysis.metadata else "guo"),
            analysis_id=analysis.analysis_id,
            subject=subject,
            topic_priority=self._topic_priority(analysis),
            industry_chain_position=self._industry_chain_position(analysis),
            expectation_gap=self._expectation_gap(analysis),
            realization_tempo=self._realization_tempo(analysis),
            risk_brake=list(analysis.invalidation_conditions)
            or ["缺少明确风险刹车条件，需人工补充。"],
            next_verification_actions=list(analysis.suggested_followups)
            or ["补充直接证据或外部事实验证。"],
            review_hooks=self._review_hooks(analysis),
            source_classification=source_classification,
            evidence_gap=evidence_gap,
            confidence_boundary=confidence_boundary,
            confidence=analysis.confidence,
            quality_mode=(
                analysis.metadata.get("quality_mode", "standard")
                if analysis.metadata
                else "standard"
            ),
            moa_audit=(analysis.metadata.get("moa_audit") if analysis.metadata else None),
            warnings=warnings,
            needs_human_review=needs_human_review,
            reference_context_used=reference_context_used or [],
            advisory_only=True,
        )

        if external_context_bundle is not None:
            package = ResearchReferenceEnricher().enrich(package, external_context_bundle)

        return package

    def _source_classification(self, analysis: PersonaAnalysis) -> dict[str, Any]:
        existing = analysis.metadata.get("source_classification") if analysis.metadata else None
        if existing:
            return dict(existing)
        return {
            "direct_knowledge": {
                "available": bool(analysis.activated_trace_ids or analysis.evidence_ids),
                "trace_ids": list(analysis.activated_trace_ids),
                "evidence_ids": list(analysis.evidence_ids),
            },
            "methodology_transfer": {
                "available": bool(
                    analysis.activated_pattern_ids and not analysis.activated_trace_ids
                ),
                "pattern_ids": list(analysis.activated_pattern_ids),
                "basis": list(analysis.reasoning_steps[:3]),
            },
            "external_observation": {"available": False, "note": "外部上下文仅供参考"},
        }

    def _evidence_gap(self, analysis: PersonaAnalysis) -> dict[str, Any]:
        existing = analysis.metadata.get("evidence_gap") if analysis.metadata else None
        if existing:
            return dict(existing)
        trace_count = len(analysis.activated_trace_ids)
        evidence_count = len(analysis.evidence_ids)
        if trace_count or evidence_count:
            message = "存在老师直接 trace/evidence，但仍需外部事实交叉验证。"
        else:
            message = "当前标的缺少老师直接 trace/evidence，只能按方法论迁移或外部观察低置信处理。"
        return {
            "direct_trace_count": trace_count,
            "direct_evidence_count": evidence_count,
            "message": message,
        }

    def _confidence_boundary(self, analysis: PersonaAnalysis) -> dict[str, Any]:
        existing = analysis.metadata.get("confidence_boundary") if analysis.metadata else None
        if existing:
            return dict(existing)
        # 老师原创 trace 仅作参考，不提升置信度边界。
        return {
            "level": "low",
            "reason": "老师原创 trace 与方法论迁移均仅作参考，未经验证不得形成高置信结论。",
        }

    def _warnings(
        self, analysis: PersonaAnalysis, confidence_boundary: dict[str, Any]
    ) -> list[str]:
        warnings = (
            list(analysis.uncertainty)
            + list(analysis.contradictions)
            + list(analysis.unsupported_claims)
        )
        if confidence_boundary.get("level") == "low":
            warnings.append("低置信或缺少直接证据，需要人工复核。")
        return warnings

    def _topic_priority(self, analysis: PersonaAnalysis) -> str:
        target = analysis.company or analysis.ticker or "该主题"
        return f"{target} 当前结论为 {analysis.stance}：{analysis.conclusion}"

    def _industry_chain_position(self, analysis: PersonaAnalysis) -> str:
        target = analysis.company or analysis.ticker or "该主题"
        company = analysis.company or ""
        ticker = analysis.ticker or ""
        if not company:
            return f"{target} 的产业链位置需要结合老师框架与外部事实继续验证。"
        try:
            from pathlib import Path

            from fin_analyse.analysis.industry_chain import IndustryChainAnalyzer
            from fin_analyse.claims import RuleBasedClaimExtractor
            from fin_analyse.ingestion.zsxq_adapter import ZsxqMarkdownAdapter
            from fin_analyse.knowledge.store import KnowledgeStore
            from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

            kb_root = str(default_knowledge_base_root())
            store = KnowledgeStore.from_adapter(
                ZsxqMarkdownAdapter(root=Path(kb_root)),
                RuleBasedClaimExtractor(),
            )
            analyzer = IndustryChainAnalyzer(store)
            result = analyzer.analyze(company, ticker=ticker)
            if result.chain_segment and result.industry:
                return (
                    f"{company} 处于 **{result.industry}** 产业链的 **{result.chain_segment}** 环节"
                    f"（战略重要度 {result.strategic_importance:.0f}/10，替代难度 {result.substitution_difficulty}）"
                    f"。{result.moat_summary}"
                )
            return f"{target} 的产业链位置需要结合老师框架与外部事实继续验证。"
        except Exception:
            return f"{target} 的产业链位置需要结合老师框架与外部事实继续验证。"

    def _expectation_gap(self, analysis: PersonaAnalysis) -> str:
        if analysis.reasoning_steps:
            return "；".join(analysis.reasoning_steps[:2])
        return "预期差需要补充老师直接证据或外部事实验证。"

    def _realization_tempo(self, analysis: PersonaAnalysis) -> str:
        if analysis.suggested_followups:
            return f"观察窗口围绕：{'；'.join(analysis.suggested_followups[:2])}"
        return "兑现节奏需要补充催化、公告、价格或后续老师观点验证。"

    def _review_hooks(self, analysis: PersonaAnalysis) -> list[str]:
        hooks = [f"复盘 thesis：{analysis.conclusion}"]
        hooks.extend(f"观察失效条件：{item}" for item in analysis.invalidation_conditions[:3])
        hooks.extend(f"后续验证：{item}" for item in analysis.suggested_followups[:3])
        return hooks
