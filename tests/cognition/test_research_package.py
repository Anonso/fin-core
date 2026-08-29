from fin_analyse.cognition.models import PersonaAnalysis
from fin_analyse.cognition.research_package import (
    ResearchPackageBuilder,
    ResearchPackageSubject,
)


def _analysis(metadata=None, confidence=0.55, traces=None, patterns=None, evidence=None):
    return PersonaAnalysis(
        analysis_id="pa-1",
        persona_id="persona-guo",
        question="帮我看广晟有色",
        company="广晟有色",
        ticker="600259",
        activated_trace_ids=traces if traces is not None else ["trace-1"],
        activated_pattern_ids=patterns if patterns is not None else ["pattern-1"],
        evidence_ids=evidence if evidence is not None else ["evidence-1"],
        reasoning_steps=["先判断资源位置", "再看兑现节奏"],
        conclusion="关注但不追高",
        stance="watch",
        confidence=confidence,
        uncertainty=["外部价格验证不足"],
        contradictions=[],
        unsupported_claims=["老师直接 trace 仅作为参考材料，不构成确定性证据。"],
        invalidation_conditions=["价格跌破关键支撑", "政策兑现低于预期"],
        suggested_followups=["核对公告", "跟踪价格与成交"],
        created_at="2026-06-27T00:00:00+00:00",
        metadata=metadata or {},
    )


def test_research_package_preserves_source_boundaries_and_required_fields():
    metadata = {
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
            "message": "存在老师直接证据，但仍需外部验证。",
        },
        "confidence_boundary": {"level": "low", "reason": "老师原创 trace 仅作参考材料。"},
        "quality_mode": "moa",
        "moa_audit": {"roles": ["direct_trace_reader", "aggregator"], "verdict": "accepted"},
    }
    subject = ResearchPackageSubject(
        company="广晟有色", ticker="600259", source_type="conversation"
    )

    package = ResearchPackageBuilder().build_from_persona_analysis(
        _analysis(metadata), subject=subject
    )
    data = package.to_dict()

    for key in (
        "topic_priority",
        "industry_chain_position",
        "expectation_gap",
        "realization_tempo",
        "risk_brake",
        "next_verification_actions",
        "review_hooks",
        "source_classification",
        "evidence_gap",
        "confidence_boundary",
        "quality_mode",
        "moa_audit",
    ):
        assert key in data, f"missing key: {key}"

    assert data["source_classification"] == metadata["source_classification"]
    assert data["confidence_boundary"] == metadata["confidence_boundary"]
    assert data["evidence_gap"] == metadata["evidence_gap"]
    assert data["quality_mode"] == "moa"
    assert data["moa_audit"] == metadata["moa_audit"]
    assert data["risk_brake"] == ["价格跌破关键支撑", "政策兑现低于预期"]
    assert data["next_verification_actions"] == ["核对公告", "跟踪价格与成交"]
    assert data["advisory_only"] is True
    assert data["execution_allowed"] is False
    assert data["subject"]["source_type"] == "conversation"


def test_research_package_round_trips_dict_shape():
    subject = ResearchPackageSubject(
        company="协鑫能科", ticker="002015", source_type="real_holding"
    )

    package = ResearchPackageBuilder().build_from_persona_analysis(_analysis(), subject=subject)
    restored = type(package).from_dict(package.to_dict())

    assert restored.to_dict() == package.to_dict()
    assert restored.subject.company == "协鑫能科"
    assert restored.subject.source_type == "real_holding"


def test_research_package_uses_conservative_defaults_without_metadata():
    subject = ResearchPackageSubject(company="万科A", ticker="000002", source_type="real_holding")

    package = ResearchPackageBuilder().build_from_persona_analysis(
        _analysis(
            metadata={}, confidence=0.42, traces=[], patterns=["pattern-transfer"], evidence=[]
        ),
        subject=subject,
    )
    data = package.to_dict()

    assert data["source_classification"]["direct_knowledge"]["available"] is False
    assert data["source_classification"]["methodology_transfer"]["available"] is True
    assert data["source_classification"]["methodology_transfer"]["pattern_ids"] == [
        "pattern-transfer"
    ]
    assert data["evidence_gap"]["direct_trace_count"] == 0
    assert data["evidence_gap"]["direct_evidence_count"] == 0
    assert data["confidence_boundary"]["level"] == "low"
    assert data["needs_human_review"] is True
    assert any("缺少" in warning or "低置信" in warning for warning in data["warnings"])
