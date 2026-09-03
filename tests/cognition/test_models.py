"""Test cognition data model round-trips."""

from fin_analyse.cognition.models import (
    EvidenceItem,
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
