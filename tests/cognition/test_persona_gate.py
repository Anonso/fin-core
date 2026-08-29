"""Tests for strict Persona ingestion eligibility."""

from fin_analyse.cognition.models import EvidenceItem, SourceLabel
from fin_analyse.cognition.persona_gate import (
    PersonaGateDecision,
    PersonaIngestionGate,
    apply_persona_gate,
    decision_from_metadata,
)


def _evidence(
    *,
    title: str = "测试文章",
    content: str = "我认为关键变量需要观察，产业链逻辑和风险边界都要验证。",
    column: str = "普通",
    label: str = "teacher_original",
    source_type: str = "zsxq_article",
    score: object = None,
    is_qa: bool = False,
    evidence_type: str | None = None,
) -> EvidenceItem:
    metadata: dict[str, object] = {
        "column": column,
        "score": score,
        "is_qa": is_qa,
    }
    if evidence_type is not None:
        metadata["evidence_type"] = evidence_type
    return EvidenceItem(
        evidence_id="ev-test",
        source_type=source_type,
        source_id="article-test",
        title=title,
        content=content,
        author=column,
        published_at="2026-06-27",
        collected_at="2026-06-27",
        companies=["测试公司"],
        topics=["测试主题"],
        source_label=SourceLabel(label, "guo", 0.82, ["fixture"]),
        reliability=0.8,
        metadata=metadata,
    )


def test_star_article_is_persona_eligible_even_when_rule_label_is_unknown():
    evidence = _evidence(
        column="星大派锐评",
        label="unknown",
        content="关键不在情绪，而在订单、价格和利润率是否兑现；需要观察风险边界。",
    )

    decision = PersonaIngestionGate().evaluate(evidence)

    assert decision.allows_persona is True
    assert decision.category == "star_teacher_original"
    assert decision.source_classification == "teacher_original"
    assert decision.half_life_class in {"short_signal", "medium_logic", "long_methodology"}
    assert any("星大派" in reason for reason in decision.reasons)


def test_research_report_is_never_persona_eligible_even_with_high_score():
    evidence = _evidence(
        column="普通",
        label="research_report",
        score="9.3",
        content="某券商研报给予买入评级，盈利预测上调，目标价提高。",
    )

    decision = PersonaIngestionGate().evaluate(evidence)

    assert decision.allows_persona is False
    assert decision.category == "research_reference_only"
    assert decision.source_classification == "research_reference"
    assert decision.half_life_class == "medium_logic"


def test_ai_assisted_content_is_never_persona_eligible():
    evidence = _evidence(
        label="ai_assisted",
        content="AI分析：根据多方资料整理，本段内容为模型辅助生成。",
    )

    decision = PersonaIngestionGate().evaluate(evidence)

    assert decision.allows_persona is False
    assert decision.category == "ai_reference_only"
    assert decision.source_classification == "ai_assisted_reference"


def test_non_star_good_question_with_methodology_markers_is_candidate():
    evidence = _evidence(
        title="好问题：如何理解板块预期差",
        column="问题回答",
        is_qa=True,
        label="teacher_original",
        content=(
            "这个问题的关键是先拆产业链位置，再看预期差是否存在。"
            "方法不是追热点，而是观察变量兑现节奏和风险边界。"
        ),
    )

    decision = PersonaIngestionGate().evaluate(evidence)

    assert decision.allows_persona is True
    assert decision.category == "teacher_methodology_candidate"
    assert decision.source_classification == "teacher_methodology"
    assert decision.half_life_class == "long_methodology"


def test_non_star_good_question_without_methodology_markers_is_rejected_for_persona():
    evidence = _evidence(
        title="好问题：今天这个票涨了怎么办",
        column="问题回答",
        is_qa=True,
        label="teacher_original",
        content="今天涨得比较多，短线注意波动，具体还要看市场情绪。",
    )

    decision = PersonaIngestionGate().evaluate(evidence)

    assert decision.allows_persona is False
    assert decision.category == "time_sensitive_signal_only"
    assert decision.source_classification == "market_observation"


def test_external_context_metadata_is_rejected_before_other_rules():
    evidence = _evidence(
        source_type="external_context",
        evidence_type="external_context",
        column="星大派特刊",
        label="teacher_original",
        content="即使文字里有关键变量和方法论，external_context 也不能进 Persona。",
    )

    decision = PersonaIngestionGate().evaluate(evidence)

    assert decision.allows_persona is False
    assert decision.category == "external_context_only"
    assert decision.source_classification == "external_context"


def test_apply_persona_gate_persists_metadata_and_safe_label_upgrade():
    evidence = _evidence(
        column="星大派特刊",
        label="unknown",
        content="关键不在题材热度，而在兑现节奏、风险边界和复盘条件。",
    )

    updated = apply_persona_gate(evidence)

    assert updated.metadata["persona_eligible"] is True
    assert updated.metadata["source_classification"] == "teacher_original"
    assert updated.source_label.label == "teacher_original"
    assert updated.source_label.confidence >= evidence.source_label.confidence
    stored = decision_from_metadata(updated.metadata)
    assert isinstance(stored, PersonaGateDecision)
    assert stored.allows_persona is True
    assert stored.category == "star_teacher_original"


def test_apply_persona_gate_downgrades_report_mislabeled_teacher_original():
    evidence = _evidence(
        label="teacher_original",
        score="9.4",
        content="券商研报显示，给予买入评级，盈利预测和目标价均上调。",
    )

    updated = apply_persona_gate(evidence)

    assert updated.metadata["persona_eligible"] is False
    assert updated.metadata["source_classification"] == "research_reference"
    assert updated.source_label.label == "unknown"
    assert any("persona gate rejected" in reason for reason in updated.source_label.reasons)
