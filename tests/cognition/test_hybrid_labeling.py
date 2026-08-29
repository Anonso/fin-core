"""Tests for LLM and hybrid source labelers."""

from fin_analyse.cognition.labeling import (
    HybridSourceLabeler,
    LLMSourceLabeler,
    SourceLabeler,
)
from fin_analyse.cognition.llm import CognitionLLM


class FakeBackend:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


# ---------------------------------------------------------------------------
# LLMSourceLabeler
# ---------------------------------------------------------------------------


def test_llm_labeler_classifies_teacher_original():
    llm = CognitionLLM(
        backend=FakeBackend(
            '{"label": "teacher_original", "confidence": 0.85, "reasons": ["first-person reasoning", "subjective judgement"]}'
        )
    )
    labeler = LLMSourceLabeler(llm, default_teacher_id="guo")

    label = labeler.label(
        title="我对液冷的最新判断", content="我认为液冷是刚需...", author="郭老师"
    )

    assert label.label == "teacher_original"
    assert label.teacher_id == "guo"
    assert label.confidence == 0.85
    assert "first-person reasoning" in label.reasons


def test_llm_labeler_classifies_research_report():
    llm = CognitionLLM(
        backend=FakeBackend(
            '{"label": "research_report", "confidence": 0.9, "reasons": ["third-party research", "price target present"]}'
        )
    )
    labeler = LLMSourceLabeler(llm, default_teacher_id="guo")

    label = labeler.label(
        title="券商研报：XX公司目标价上调", content="公司营收增长...", author="某券商"
    )

    assert label.label == "research_report"
    assert label.teacher_id == "guo"


def test_llm_labeler_classifies_ai_assisted():
    llm = CognitionLLM(
        backend=FakeBackend(
            '{"label": "ai_assisted", "confidence": 0.95, "reasons": ["explicit AI marker"]}'
        )
    )
    labeler = LLMSourceLabeler(llm, default_teacher_id="guo")

    label = labeler.label(title="AI分析摘要", content="由AI整理...", author="郭老师")

    assert label.label == "ai_assisted"


def test_llm_labeler_rejects_unparseable_json_with_unknown():
    """When JSON parse fails, return unknown with low confidence."""
    llm = CognitionLLM(backend=FakeBackend("not json"))
    labeler = LLMSourceLabeler(llm, default_teacher_id="guo")

    label = labeler.label(title="某文章", content="...", author="郭老师")

    assert label.label == "unknown"
    assert label.confidence < 0.7


def test_llm_labeler_returns_unknown_when_llm_unavailable():
    llm = CognitionLLM(backend=None)
    labeler = LLMSourceLabeler(llm, default_teacher_id="guo")

    label = labeler.label(title="某文章", content="...", author="郭老师")

    assert label.label == "unknown"
    assert label.confidence == 0.5


# ---------------------------------------------------------------------------
# HybridSourceLabeler
# ---------------------------------------------------------------------------


def test_hybrid_labeler_uses_rule_when_high_confidence():
    """When rule labeler returns high-confidence non-unknown, skip LLM."""
    llm_backend = FakeBackend("SHOULD NOT BE CALLED")
    llm = CognitionLLM(backend=llm_backend)
    rule = SourceLabeler(default_teacher_id="guo", min_original_chars=30)
    llm_labeler = LLMSourceLabeler(llm, default_teacher_id="guo")
    hybrid = HybridSourceLabeler(rule, llm_labeler, rule_confidence_threshold=0.8)

    label = hybrid.label(title="AI分析：研报摘要", content="AI分析某行业..." * 20, author="郭老师")

    # Rule should catch AI_MARKERS at high confidence — LLM never invoked.
    assert label.label == "ai_assisted"
    assert label.confidence >= 0.8
    assert not llm_backend.prompts  # but never called


def test_hybrid_labeler_falls_back_to_llm_when_rule_uncertain():
    """When rule labeler is uncertain, consult LLM."""
    llm_backend = FakeBackend(
        '{"label": "teacher_original", "confidence": 0.82, "reasons": ["reasoning detected"]}'
    )
    llm = CognitionLLM(backend=llm_backend)
    llm_labeler = LLMSourceLabeler(llm, default_teacher_id="guo")
    rule = SourceLabeler(default_teacher_id="guo", min_original_chars=30)
    hybrid = HybridSourceLabeler(rule, llm_labeler, rule_confidence_threshold=0.8)

    # Short content with no markers → rule outputs unknown (low confidence).
    label = hybrid.label(
        title="某观察",
        content="真正值得关注的不是xx本身，而是背后的逻辑变化..." * 3,
        author="郭老师",
    )

    # LLM should have been called.
    assert label.label == "teacher_original"
    assert label.confidence == 0.82
    assert llm_backend.prompts


def test_hybrid_labeler_does_not_override_teacher_id_from_rule():
    llm = CognitionLLM(
        backend=FakeBackend('{"label": "research_report", "confidence": 0.9, "reasons": []}')
    )
    llm_labeler = LLMSourceLabeler(llm, default_teacher_id="guo")
    rule = SourceLabeler(default_teacher_id="guo", min_original_chars=30)
    hybrid = HybridSourceLabeler(rule, llm_labeler, rule_confidence_threshold=0.8)

    label = hybrid.label(title="短消息", content="关注。", author="somebody")

    # Rule says unknown because too short → LLM consulted
    assert label.teacher_id == "guo"


def test_hybrid_labeler_prevents_research_report_labeled_as_teacher():
    """Critical: an article with research-report markers must not be labeled teacher_original."""
    llm = CognitionLLM(
        backend=FakeBackend(
            '{"label": "teacher_original", "confidence": 0.88, "reasons": ["has reasoning"]}'
        )
    )
    llm_labeler = LLMSourceLabeler(llm, default_teacher_id="guo")
    rule = SourceLabeler(default_teacher_id="guo", min_original_chars=30)
    hybrid = HybridSourceLabeler(rule, llm_labeler, rule_confidence_threshold=0.8)

    label = hybrid.label(
        title="研报摘要",
        content="研报显示该公司目标价上调，给予买入评级...盈利预测...",
        author="某券商",
    )

    # Rule should catch it (报告 markers) at high confidence → skip LLM entirely.
    assert label.label in ("research_report", "unknown")
    assert label.label != "teacher_original"
