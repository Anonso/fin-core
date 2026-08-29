"""Tests for MoA PersonaAnalysis adapter."""

from fin_analyse.cognition.models import PersonaAnalysis, ReasoningTrace, TeacherPersona
from fin_analyse.cognition.persona_moa import MoAPersonaAnalyzer, PersonaMoAAdapter
from fin_analyse.moa.models import MoAReferenceOutput, MoARequest, MoAResult


def _trace(trace_id: str, evidence_id: str = "ev-1") -> ReasoningTrace:
    return ReasoningTrace(
        trace_id=trace_id,
        teacher_id="guo",
        source_evidence_id=evidence_id,
        topic="测试主题",
        companies=["广晟有色"],
        premises=["p"],
        observed_variables=["v"],
        inferred_relationships=["r"],
        conclusion="c",
        stance="watch",
        time_horizon="mid",
        risk_boundaries=["risk"],
        invalidation_conditions=["价格跌破支撑"],
        action_implications=["核对公告"],
        extraction_confidence=0.7,
    )


def _persona() -> TeacherPersona:
    return TeacherPersona(
        persona_id="guo:v0",
        teacher_id="guo",
        display_name="郭老师",
        active_version="v0",
        style_summary="先看关键变量是否兑现。",
        core_pattern_ids=[],
        explicit_rules=["不追高"],
        known_blind_spots=["样本不足"],
        evidence_policy={"teacher_original_only_for_cognition": True},
        last_built_at="2026-06-27",
    )


def test_build_request_has_required_roles_and_expected_schema():
    request = PersonaMoAAdapter.build_request(
        persona=_persona(),
        question="帮我看广晟有色",
        traces=[_trace("trace-1")],
        patterns=[],
        company="广晟有色",
        ticker="600259",
    )

    assert isinstance(request, MoARequest)
    role_names = {role.name for role in request.reference_roles}
    assert role_names >= {
        "core_reasoning",
        "cross_view_risk",
        "boundary_schema_guard",
    }
    assert request.task_type == "persona_analysis"
    assert request.expected_schema is not None
    assert "source_classification" in request.expected_schema
    assert "evidence_gap" in request.expected_schema
    assert "confidence_boundary" in request.expected_schema
    assert "moa_audit" in request.expected_schema


def test_to_analysis_populates_required_metadata_and_validates_trace_ids():
    result = MoAResult(
        task_id="persona:gsys",
        task_type="persona_analysis",
        status="ok",
        final={
            "conclusion": "关注但不追高",
            "stance": "watch",
            "confidence": 0.62,
            "reasoning_steps": ["直接 trace 显示关键变量待验证"],
            "activated_trace_ids": ["trace-1", "trace-fake"],
            "invalidation_conditions": ["价格跌破支撑"],
            "suggested_followups": ["核对公告"],
            "source_classification": {
                "direct_knowledge": {
                    "available": True,
                    "trace_ids": ["trace-1"],
                    "evidence_ids": ["ev-1"],
                },
                "methodology_transfer": {"available": False, "pattern_ids": [], "basis": []},
                "external_observation": {"available": False, "note": "外部上下文仅供参考"},
            },
            "evidence_gap": {
                "direct_trace_count": 1,
                "direct_evidence_count": 1,
                "message": "存在直接证据。",
                "severity": "low",
            },
            "unsupported_claims": [],
            "confidence_boundary": {"level": "medium", "reason": "有 direct trace 但仍需验证。"},
            "moa_audit": {
                "roles": ["core_reasoning", "aggregator"],
                "verdict": "accepted",
            },
            "warnings": [],
            "needs_human_review": False,
        },
        reference_outputs=[
            MoAReferenceOutput(
                role="core_reasoning", backend_name="t0", content="ok", ok=True
            )
        ],
        consensus=[],
        disagreements=[],
        blind_spots=[],
        confidence=0.62,
        warnings=[],
    )

    analyzer = MoAPersonaAnalyzer()
    analysis = analyzer.to_analysis(
        result=result,
        persona=_persona(),
        question="帮我看广晟有色",
        traces=[_trace("trace-1")],
        patterns=[],
        company="广晟有色",
        ticker="600259",
    )

    assert isinstance(analysis, PersonaAnalysis)
    assert analysis.metadata["quality_mode"] == "moa"
    assert analysis.metadata["source_classification"]["direct_knowledge"]["trace_ids"] == [
        "trace-1"
    ]
    assert analysis.metadata["evidence_gap"]["direct_trace_count"] == 1
    assert analysis.metadata["confidence_boundary"]["level"] == "low"
    assert analysis.metadata["moa_audit"]["verdict"] == "accepted"
    assert "trace-fake" not in analysis.activated_trace_ids
    assert analysis.activated_trace_ids == ["trace-1"]
    assert analysis.evidence_ids == ["ev-1"]


def test_to_analysis_returns_none_on_failed_result():
    result = MoAResult(
        task_id="persona:fail",
        task_type="persona_analysis",
        status="fallback",
        final={},
        reference_outputs=[],
        consensus=[],
        disagreements=[],
        blind_spots=[],
        confidence=0.0,
        warnings=["no backends"],
        fallback_reason="aggregator unavailable",
    )

    analyzer = MoAPersonaAnalyzer()
    assert (
        analyzer.to_analysis(
            result=result, persona=_persona(), question="q", traces=[], patterns=[]
        )
        is None
    )


def test_build_request_uses_capability_slots_not_persona_business_roles():
    """Regression: old persona business-role names must not appear."""
    request = PersonaMoAAdapter.build_request(
        persona=_persona(),
        question="帮我看广晟有色",
        traces=[_trace("trace-1")],
        patterns=[],
        company="广晟有色",
        ticker="600259",
    )

    forbidden = {
        "teacher_trace_strict_reader",
        "methodology_transfer_reader",
        "evidence_gap_checker",
        "source_boundary_guard",
        "risk_boundary_checker",
    }
    role_names = {role.name for role in request.reference_roles}
    assert not role_names & forbidden, f"Business role names found: {role_names & forbidden}"

    assert request.metadata.get("moa_topology") == "capability_slots_v1"
    slots = request.metadata.get("capability_slots", [])
    assert len(slots) == 3
