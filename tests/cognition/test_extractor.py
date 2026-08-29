"""Test reasoning trace extraction."""

from fin_analyse.cognition.extractor import RuleBasedReasoningExtractor
from fin_analyse.cognition.models import EvidenceItem, SourceLabel


def make_evidence(label: str = "teacher_original") -> EvidenceItem:
    return EvidenceItem(
        evidence_id="ev-1",
        source_type="zsxq_article",
        source_id="doc-1",
        title="政策变化后的行业判断",
        content=(
            "我认为这次政策的关键不在口号，而在利润分配。"
            "真正值得观察的是订单和利润率。如果只是情绪刺激，追高意义不大。"
        ),
        author="郭老师",
        published_at="2026-06-21",
        collected_at="2026-06-21T00:00:00Z",
        companies=["测试公司"],
        topics=["政策"],
        source_label=SourceLabel(
            label=label, teacher_id="guo", confidence=0.9, reasons=["fixture"]
        ),
        reliability=0.8,
        metadata={},
    )


def test_extracts_trace_from_teacher_original_evidence():
    extractor = RuleBasedReasoningExtractor()

    traces = extractor.extract(make_evidence())

    assert len(traces) == 1
    trace = traces[0]
    assert trace.teacher_id == "guo"
    assert trace.source_evidence_id == "ev-1"
    assert "利润分配" in trace.observed_variables
    assert trace.stance == "watch"
    assert trace.extraction_confidence > 0
    assert len(trace.risk_boundaries) > 0
    assert len(trace.invalidation_conditions) > 0


def test_skips_non_teacher_original_evidence():
    extractor = RuleBasedReasoningExtractor()

    traces = extractor.extract(make_evidence(label="ai_assisted"))

    assert traces == []
