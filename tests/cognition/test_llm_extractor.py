"""Tests for LLM and hybrid reasoning extractors."""

from fin_analyse.cognition.extractor import (
    HybridReasoningExtractor,
    LLMReasoningExtractor,
    RuleBasedReasoningExtractor,
)
from fin_analyse.cognition.llm import CognitionLLM
from fin_analyse.cognition.models import EvidenceItem, SourceLabel


def _make_evidence(label="teacher_original", teacher_id="guo", confidence=0.85):
    return EvidenceItem(
        evidence_id="ev-001",
        source_type="zsxq_article",
        source_id="art_42",
        title="我对液冷的最新判断",
        content="我认为液冷是刚需，主要原因有三...",
        author="郭老师",
        published_at="2026-01-15",
        collected_at="2026-01-16",
        companies=["英维克"],
        topics=["液冷"],
        source_label=SourceLabel(label, teacher_id, confidence, []),
        reliability=0.8,
    )


class FakeBackend:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


# ---------------------------------------------------------------------------
# LLMReasoningExtractor
# ---------------------------------------------------------------------------


def test_llm_extractor_returns_traces_from_teacher_original():
    backend = FakeBackend(
        '[{"topic": "液冷", "companies": ["英维克"], "premises": ["AI算力推动液冷需求"],'
        '"observed_variables": ["订单增速", "利润率"],'
        '"inferred_relationships": ["订单增长→利润改善"],'
        '"conclusion": "液冷确定性高，但需等价格确认", "stance": "watch",'
        '"time_horizon": "mid", "risk_boundaries": ["估值过高"],'
        '"invalidation_conditions": ["订单增速放缓"],'
        '"action_implications": ["跟踪订单数据"], "extraction_confidence": 0.82}]'
    )
    llm = CognitionLLM(backend=backend)
    extractor = LLMReasoningExtractor(llm)
    evidence = _make_evidence()

    traces = extractor.extract(evidence)

    assert len(traces) == 1
    t = traces[0]
    assert t.topic == "液冷"
    assert t.stance == "watch"
    assert t.extraction_confidence == 0.82
    assert t.teacher_id == "guo"
    assert t.source_evidence_id == "ev-001"
    # System-assigned trace_id, not LLM-assigned
    assert t.trace_id and not t.trace_id.startswith("llm-")


def test_llm_extractor_skips_non_teacher_original():
    backend = FakeBackend("SHOULD NOT BE CALLED")
    llm = CognitionLLM(backend=backend)
    extractor = LLMReasoningExtractor(llm)
    evidence = _make_evidence(label="research_report", confidence=0.9)

    traces = extractor.extract(evidence)

    assert traces == []
    assert not backend.prompts


def test_llm_extractor_handles_unparseable_json():
    backend = FakeBackend("not json")
    llm = CognitionLLM(backend=backend)
    extractor = LLMReasoningExtractor(llm)
    evidence = _make_evidence()

    traces = extractor.extract(evidence)

    assert traces == []


def test_llm_extractor_caps_confidence_by_source_label():
    """extraction_confidence must not exceed source_label.confidence."""
    backend = FakeBackend(
        '[{"topic": "x", "companies": [], "premises": [], "observed_variables": [],'
        '"inferred_relationships": [], "conclusion": "ok", "stance": "watch",'
        '"time_horizon": "short", "risk_boundaries": [],'
        '"invalidation_conditions": [], "action_implications": [],'
        '"extraction_confidence": 0.99}]'
    )
    llm = CognitionLLM(backend=backend)
    extractor = LLMReasoningExtractor(llm)
    evidence = _make_evidence(confidence=0.6)

    traces = extractor.extract(evidence)

    assert traces[0].extraction_confidence <= 0.6


# ---------------------------------------------------------------------------
# HybridReasoningExtractor
# ---------------------------------------------------------------------------


def test_hybrid_extractor_skips_rule_when_llm_fails():
    """When LLM fails, do NOT fall back to rule extractor — flag the failure."""
    backend = FakeBackend("not json")
    llm = CognitionLLM(backend=backend)
    llm_ext = LLMReasoningExtractor(llm)
    rule_ext = RuleBasedReasoningExtractor()
    hybrid = HybridReasoningExtractor(llm_ext, rule_ext)
    # Use content that would trigger rule extractor markers (订单, 利润率, 价格)
    evidence = EvidenceItem(
        evidence_id="ev-002",
        source_type="zsxq_article",
        source_id="art_42",
        title="我对液冷的最新判断",
        content="我认为液冷是刚需，订单数据支持，利润率在改善，价格可能上涨，政策也支持...",
        author="郭老师",
        published_at="2026-01-15",
        collected_at="2026-01-16",
        companies=["英维克"],
        topics=["液冷"],
        source_label=SourceLabel("teacher_original", "guo", 0.85, []),
        reliability=0.8,
    )

    traces = hybrid.extract(evidence)

    # LLM failed → no rule fallback, empty result, failure flagged
    assert len(traces) == 0
    assert llm_ext.last_extraction_failed is True


def test_hybrid_extractor_prefers_llm_when_available():
    """When LLM succeeds, use its output."""
    backend = FakeBackend(
        '[{"topic": "液冷", "companies": ["英维克"], "premises": ["p1"],'
        '"observed_variables": ["v1"], "inferred_relationships": ["r1"],'
        '"conclusion": "c1", "stance": "watch", "time_horizon": "mid",'
        '"risk_boundaries": ["r1"], "invalidation_conditions": ["i1"],'
        '"action_implications": ["a1"], "extraction_confidence": 0.8}]'
    )
    llm = CognitionLLM(backend=backend)
    llm_ext = LLMReasoningExtractor(llm)
    rule_ext = RuleBasedReasoningExtractor()
    hybrid = HybridReasoningExtractor(llm_ext, rule_ext)
    evidence = _make_evidence()

    traces = hybrid.extract(evidence)

    assert len(traces) == 1
    assert traces[0].topic == "液冷"
