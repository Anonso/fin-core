"""Tests for CognitionWriteGateService — the unified write gate seam.

CognitionWriteGateService.evaluate_evidence() replaces direct calls to
apply_persona_gate() and _is_teacher_cognition_evidence() in CognitiveService.
"""

from fin_analyse.cognition.models import EvidenceItem, ReasoningTrace, SourceLabel
from fin_analyse.cognition.persona_gate import PersonaGateDecision, PersonaIngestionGate
from fin_analyse.cognition.write_gate import (
    CognitionWriteGateResult,
    CognitionWriteGateService,
    CognitionWriteTarget,
)


def _evidence(
    *,
    title: str = "测试文章",
    content: str = "我认为关键变量需要观察，产业链逻辑和风险边界都要验证。",
    column: str = "普通",
    label: str = "teacher_original",
    source_type: str = "zsxq_article",
    evidence_type: str | None = None,
    metadata: dict | None = None,
) -> EvidenceItem:
    meta: dict[str, object] = {"column": column}
    if evidence_type is not None:
        meta["evidence_type"] = evidence_type
    if metadata:
        meta.update(metadata)
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
        metadata=meta,
    )


class TestCognitionWriteGateServiceEvaluateEvidence:
    """evaluate_evidence() returns a CognitionWriteGateResult with correct fields."""

    def test_returns_result_with_required_fields(self):
        svc = CognitionWriteGateService()
        evidence = _evidence()
        result = svc.evaluate_evidence(evidence)

        assert isinstance(result, CognitionWriteGateResult)
        assert result.evidence_id == "ev-test"
        assert isinstance(result.gate_decision, PersonaGateDecision)
        assert result.write_target in {"persona", "reference_only", "external_context_only"}
        assert isinstance(result.is_teacher_cognition, bool)
        assert isinstance(result.gated_evidence, EvidenceItem)
        assert result.source_boundary == "teacher_cognition_write_gate"

    def test_star_article_is_persona_write_target(self):
        """星大派 article with teacher_original label → write_target='persona'."""
        svc = CognitionWriteGateService()
        evidence = _evidence(
            column="星大派锐评",
            label="teacher_original",
            content="关键不在情绪，而在订单、价格和利润率是否兑现；需要观察风险边界。",
        )
        result = svc.evaluate_evidence(evidence)

        assert result.write_target == "persona"
        assert result.is_teacher_cognition is True
        assert result.allowed is True
        assert result.data_gaps == ()
        assert result.source_classification == "teacher_original"
        assert result.gate_decision.allows_persona is True

    def test_research_report_is_reference_only(self):
        """Research report → write_target='reference_only'."""
        svc = CognitionWriteGateService()
        evidence = _evidence(
            label="research_report",
            content="某券商研报给予买入评级，盈利预测上调，目标价提高。",
        )
        result = svc.evaluate_evidence(evidence)

        assert result.write_target == "reference_only"
        assert result.is_teacher_cognition is False
        assert result.allowed is False
        assert result.data_gaps == ("research_reference_rejected_for_cognition",)
        assert result.gate_decision.allows_persona is False

    def test_ai_assisted_is_reference_only(self):
        """AI-assisted content → write_target='reference_only'."""
        svc = CognitionWriteGateService()
        evidence = _evidence(
            label="ai_assisted",
            content="AI分析显示...",
        )
        result = svc.evaluate_evidence(evidence)

        assert result.write_target == "reference_only"
        assert result.is_teacher_cognition is False
        assert result.data_gaps == ("ai_reference_rejected_for_cognition",)

    def test_external_context_is_external_context_only(self):
        """External context source → write_target='external_context_only'."""
        svc = CognitionWriteGateService()
        evidence = _evidence(
            source_type="external_context",
            label="external_context",
        )
        result = svc.evaluate_evidence(evidence)

        assert result.write_target == "external_context_only"
        assert result.is_teacher_cognition is False
        assert result.data_gaps == ("external_context_rejected_for_cognition",)
        assert result.gate_decision.allows_persona is False

    def test_external_context_via_evidence_type(self):
        """Evidence with evidence_type='external_context' → write_target='external_context_only'."""
        svc = CognitionWriteGateService()
        evidence = _evidence(
            evidence_type="external_context",
            label="teacher_original",
        )
        result = svc.evaluate_evidence(evidence)

        assert result.write_target == "external_context_only"
        assert result.is_teacher_cognition is False
        assert result.data_gaps == ("external_context_rejected_for_cognition",)

    def test_unknown_label_with_star_column_is_persona(self):
        """Unknown label but star column → write_target='persona' (gate promotes label)."""
        svc = CognitionWriteGateService()
        evidence = _evidence(
            column="星大派特刊",
            label="unknown",
            content="关键变量、方法论框架、产业链逻辑都要验证。",
        )
        result = svc.evaluate_evidence(evidence)

        # Gate allows persona for star column, and apply_persona_gate promotes
        # the label to teacher_original
        assert result.gate_decision.allows_persona is True
        assert result.write_target == "persona"
        # The gated evidence should have teacher_original label
        assert result.gated_evidence.source_label.label == "teacher_original"

    def test_good_question_with_methodology_is_persona(self):
        """Good question with methodology markers → write_target='persona'."""
        svc = CognitionWriteGateService()
        evidence = _evidence(
            column="好问题",
            label="teacher_original",
            content="方法论：关键变量是产业链逻辑，框架需要风险边界验证。",
        )
        result = svc.evaluate_evidence(evidence)

        assert result.gate_decision.allows_persona is True
        assert result.write_target == "persona"
        assert result.is_teacher_cognition is True

    def test_gated_evidence_has_persona_metadata(self):
        """Gated evidence must have persona_eligible and persona_gate metadata."""
        svc = CognitionWriteGateService()
        evidence = _evidence(column="星大派锐评")
        result = svc.evaluate_evidence(evidence)

        gated = result.gated_evidence
        assert "persona_eligible" in gated.metadata
        assert "persona_gate" in gated.metadata
        assert gated.metadata["persona_eligible"] is True
        assert isinstance(gated.metadata["persona_gate"], dict)

    def test_prepare_evidence_returns_gated_evidence_and_target(self):
        svc = CognitionWriteGateService()
        evidence = _evidence(column="星大派锐评")

        result = svc.prepare_evidence(
            evidence,
            target=CognitionWriteTarget.REASONING_TRACE,
        )

        assert result.target == "reasoning_trace"
        assert result.gated_evidence.metadata["persona_eligible"] is True
        assert result.allowed is True

    def test_uses_cached_decision_from_metadata(self):
        """When evidence metadata already has persona_gate, reuse it."""
        svc = CognitionWriteGateService()
        pre_decision = PersonaGateDecision(
            evidence_id="ev-test",
            allows_persona=True,
            category="star_teacher_original",
            source_classification="teacher_original",
            confidence=0.88,
            half_life_class="medium_logic",
            reasons=["pre-cached"],
        )
        evidence = _evidence(
            column="普通",
            label="teacher_original",
            metadata={"persona_gate": pre_decision.to_dict()},
        )
        result = svc.evaluate_evidence(evidence)

        assert result.gate_decision.category == "star_teacher_original"
        assert "pre-cached" in result.gate_decision.reasons

    def test_custom_gate_instance(self):
        """Service accepts a custom PersonaIngestionGate instance."""
        custom_gate = PersonaIngestionGate()
        svc = CognitionWriteGateService(gate=custom_gate)
        evidence = _evidence(column="星大派锐评")
        result = svc.evaluate_evidence(evidence)

        assert result.gate_decision.allows_persona is True

    def test_select_traces_keeps_only_allowed_teacher_evidence(self):
        svc = CognitionWriteGateService()
        accepted_evidence = _evidence(
            column="星大派锐评",
            label="teacher_original",
            metadata={"custom": "accepted"},
        )
        rejected_evidence = _evidence(
            label="research_report",
            content="券商研报给予买入评级，盈利预测上调。",
            metadata={"custom": "rejected"},
        )
        accepted_evidence = EvidenceItem.from_dict(
            {
                **accepted_evidence.to_dict(),
                "evidence_id": "ev-accepted",
            }
        )
        rejected_evidence = EvidenceItem.from_dict(
            {
                **rejected_evidence.to_dict(),
                "evidence_id": "ev-rejected",
            }
        )
        accepted_trace = ReasoningTrace(
            trace_id="trace-accepted",
            teacher_id="guo",
            source_evidence_id="ev-accepted",
            topic="测试",
            companies=["测试公司"],
            premises=["p"],
            observed_variables=["v"],
            inferred_relationships=["r"],
            conclusion="c",
            stance="watch",
            time_horizon="mid",
            risk_boundaries=["risk"],
            invalidation_conditions=["invalid"],
            action_implications=["action"],
            extraction_confidence=0.7,
        )
        rejected_trace = ReasoningTrace(
            trace_id="trace-rejected",
            teacher_id="guo",
            source_evidence_id="ev-rejected",
            topic="测试",
            companies=["测试公司"],
            premises=["p"],
            observed_variables=["v"],
            inferred_relationships=["r"],
            conclusion="c",
            stance="watch",
            time_horizon="mid",
            risk_boundaries=["risk"],
            invalidation_conditions=["invalid"],
            action_implications=["action"],
            extraction_confidence=0.7,
        )

        selected = svc.select_traces(
            [accepted_trace, rejected_trace],
            {
                "ev-accepted": accepted_evidence,
                "ev-rejected": rejected_evidence,
            },
            teacher_id="guo",
        )

        assert selected == [accepted_trace]
