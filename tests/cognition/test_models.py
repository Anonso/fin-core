"""Test cognition data model round-trips."""

from fin_analyse.cognition.models import (
    CognitiveFeedback,
    EvidenceItem,
    PersonaAnalysis,
    SourceLabel,
)


def test_evidence_item_round_trips_dict():
    label = SourceLabel(
        label="teacher_original",
        teacher_id="guo",
        confidence=0.92,
        reasons=["first-person reasoning", "not a report summary"],
    )
    item = EvidenceItem(
        evidence_id="ev-1",
        source_type="zsxq_article",
        source_id="doc-1",
        title="政策变化后的行业判断",
        content="我认为这次政策的关键不在口号，而在利润分配。",
        author="郭老师",
        published_at="2026-06-21",
        collected_at="2026-06-21T00:00:00Z",
        companies=["测试公司"],
        topics=["政策", "行业"],
        source_label=label,
        reliability=0.8,
        metadata={"tier": "S"},
    )

    restored = EvidenceItem.from_dict(item.to_dict())

    assert restored == item
    assert restored.source_label.label == "teacher_original"


def test_persona_analysis_tracks_support_and_uncertainty():
    analysis = PersonaAnalysis(
        analysis_id="pa-1",
        persona_id="guo:v0",
        question="测试公司怎么看？",
        company="测试公司",
        ticker="000001",
        activated_trace_ids=["trace-1"],
        activated_pattern_ids=["pattern-1"],
        evidence_ids=["ev-1"],
        reasoning_steps=["政策改变利润分配，所以先观察兑现。"],
        conclusion="关注但不追高",
        stance="watch",
        confidence=0.66,
        uncertainty=["缺少最新成交量验证"],
        contradictions=[],
        unsupported_claims=[],
        invalidation_conditions=["政策落地弱于预期"],
        suggested_followups=["验证行业价格趋势"],
        created_at="2026-06-21T00:00:00Z",
    )

    restored = PersonaAnalysis.from_dict(analysis.to_dict())

    assert restored.stance == "watch"
    assert restored.activated_trace_ids == ["trace-1"]
    assert restored.conclusion == "关注但不追高"


def test_trace_verification_round_trip():
    from fin_analyse.cognition.models import TraceVerification

    verification = TraceVerification(
        verification_id="tv-abc123",
        trace_id="trace-001",
        source_evidence_id="ev-001",
        teacher_id="guo",
        verdict="revise",
        verified_confidence=0.66,
        confidence_adjustment=0.24,
        issues=["结论比原文更强"],
        suggested_revision={"conclusion": "关注但等待验证", "stance": "watch"},
        reason="原文支持关注方向，但不支持立即看多。",
        verifier_backend="gpt5",
        created_at="2026-06-23T00:00:00+00:00",
    )

    restored = TraceVerification.from_dict(verification.to_dict())

    assert restored == verification
    assert restored.suggested_revision["stance"] == "watch"


def test_persona_analysis_round_trip_preserves_metadata():
    analysis = PersonaAnalysis(
        analysis_id="pa-1",
        persona_id="persona-guo",
        question="怎么看贵州茅台？",
        company="贵州茅台",
        ticker="600519",
        activated_trace_ids=["trace-1"],
        activated_pattern_ids=["pattern-1"],
        evidence_ids=["evidence-1"],
        reasoning_steps=["步骤"],
        conclusion="关注但不追高",
        stance="watch",
        confidence=0.62,
        uncertainty=[],
        contradictions=[],
        unsupported_claims=[],
        invalidation_conditions=[],
        suggested_followups=[],
        created_at="2026-06-23T00:00:00+00:00",
        metadata={"context_type": "conversation", "request_id": "req-1"},
    )

    restored = PersonaAnalysis.from_dict(analysis.to_dict())

    assert restored.metadata["context_type"] == "conversation"
    assert restored.metadata["request_id"] == "req-1"


def test_feedback_round_trips_dict():
    feedback = CognitiveFeedback(
        feedback_id="fb-1",
        target_type="persona_analysis",
        target_id="pa-1",
        feedback_type="not_like_teacher",
        note="这更像普通研报摘要，不像郭老师推理。",
        created_at="2026-06-21T00:00:00Z",
    )

    assert CognitiveFeedback.from_dict(feedback.to_dict()) == feedback
