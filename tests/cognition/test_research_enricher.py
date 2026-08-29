"""Tests for reference-only research enrichment."""

from fin_analyse.cognition.models import PersonaAnalysis
from fin_analyse.cognition.research_enricher import ResearchReferenceEnricher
from fin_analyse.cognition.research_package import (
    GuoTeacherResearchPackage,
    ResearchPackageBuilder,
    ResearchPackageSubject,
)
from fin_analyse.context.models import ExternalContextBundle, ExternalContextRecord


def _analysis() -> PersonaAnalysis:
    return PersonaAnalysis(
        analysis_id="pa-1",
        persona_id="persona-guo",
        question="帮我看广晟有色",
        company="广晟有色",
        ticker="600259",
        activated_trace_ids=["trace-1"],
        activated_pattern_ids=["pattern-1"],
        evidence_ids=["evidence-1"],
        reasoning_steps=["先判断资源位置"],
        conclusion="关注但不追高",
        stance="watch",
        confidence=0.55,
        uncertainty=[],
        contradictions=[],
        unsupported_claims=["老师直接 trace 仅作为参考材料，不构成确定性证据。"],
        invalidation_conditions=["价格跌破关键支撑"],
        suggested_followups=["核对公告"],
        created_at="2026-06-27T00:00:00+00:00",
        metadata={
            "source_classification": {
                "direct_knowledge": {
                    "available": True,
                    "trace_ids": ["trace-1"],
                    "evidence_ids": ["evidence-1"],
                },
                "methodology_transfer": {"available": False, "pattern_ids": [], "basis": []},
                "external_observation": {"available": False, "note": "外部上下文仅供参考"},
            },
            "evidence_gap": {
                "direct_trace_count": 1,
                "direct_evidence_count": 1,
                "message": "存在直接证据，但仍需外部验证。",
            },
            "confidence_boundary": {"level": "low", "reason": "老师原创 trace 仅作参考材料。"},
        },
    )


def _bundle(*records: ExternalContextRecord) -> ExternalContextBundle:
    return ExternalContextBundle(
        ticker="600259",
        records=list(records),
        warnings=["研报仅供参考"],
    )


def _build_package() -> GuoTeacherResearchPackage:
    return ResearchPackageBuilder().build_from_persona_analysis(
        _analysis(),
        subject=ResearchPackageSubject(
            company="广晟有色", ticker="600259", source_type="conversation"
        ),
    )


def test_enrich_with_no_bundle_returns_unchanged_package():
    package = _build_package()
    enriched = ResearchReferenceEnricher().enrich(package, None)

    assert enriched == package


def test_enrich_populates_fields_without_altering_source_boundary():
    record = ExternalContextRecord(
        record_id="r1",
        source="eastmoney_report",
        category="research",
        ticker="600259",
        title="广晟有色研报",
        summary="稀土价格预期上行，下游需求待验证",
        occurred_at="2026-06-27",
    )
    package = _build_package()
    original_source = dict(package.source_classification)
    original_confidence = dict(package.confidence_boundary)

    enriched = ResearchReferenceEnricher().enrich(package, _bundle(record))

    assert "外部参考" in enriched.industry_chain_position
    assert "广晟有色研报" in enriched.industry_chain_position
    assert "预期差/事实缺口" in enriched.expectation_gap
    assert enriched.reference_context_used == [
        {
            "record_id": "r1",
            "source": "eastmoney_report",
            "category": "research",
            "ticker": "600259",
            "title": "广晟有色研报",
            "reference_only": True,
        }
    ]
    assert any(
        "核对财报" in action or "交叉验证" in action
        for action in enriched.next_verification_actions
    )
    assert any("外部风险" in hook for hook in enriched.review_hooks)
    assert enriched.source_classification == original_source
    assert enriched.confidence_boundary == original_confidence
    assert any("不构成老师观点" in warning for warning in enriched.warnings)
    assert enriched.advisory_only is True


def test_enrich_adds_warning_when_record_marked_as_decision_factor():
    record = ExternalContextRecord(
        record_id="r2",
        source="market",
        category="market",
        ticker="600259",
        title="放量突破",
        summary="价格放量突破短期均线",
        occurred_at="2026-06-27",
        is_decision_factor=True,
    )
    package = _build_package()

    enriched = ResearchReferenceEnricher().enrich(package, _bundle(record))

    assert any("决策因素" in warning for warning in enriched.warnings)
    assert any("过度归因" in hook for hook in enriched.review_hooks)


def test_enrich_uses_announcement_category_for_tempo():
    record = ExternalContextRecord(
        record_id="r3",
        source="announcement",
        category="announcement",
        ticker="600259",
        title="半年度业绩预告",
        summary="业绩预增50%",
        occurred_at="2026-06-27",
    )
    package = _build_package()

    enriched = ResearchReferenceEnricher().enrich(package, _bundle(record))

    assert "半年度业绩预告" in enriched.realization_tempo
    assert "跟踪公司公告" in enriched.next_verification_actions
