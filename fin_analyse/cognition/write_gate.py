"""Cognition Write Gate Service — unified seam for persona write gate decisions.

This module provides CognitionWriteGateService as the single entry point for
deciding whether evidence may be written to teacher cognition (persona/patterns).
It replaces direct calls to apply_persona_gate() and _is_teacher_cognition_evidence()
in CognitiveService.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from fin_analyse.cognition.models import EvidenceItem, ReasoningTrace
from fin_analyse.cognition.persona_gate import (
    PersonaGateDecision,
    PersonaIngestionGate,
    apply_persona_gate,
    decision_from_metadata,
)


class CognitionWriteTarget(StrEnum):
    EVIDENCE = "evidence"
    REASONING_TRACE = "reasoning_trace"
    COGNITIVE_PATTERN = "cognitive_pattern"
    TEACHER_PERSONA = "teacher_persona"


@dataclass(frozen=True)
class CognitionWriteGateResult:
    """Result of evaluating whether evidence may be written to teacher cognition.

    Attributes:
        evidence_id: The evidence identifier.
        gate_decision: The PersonaGateDecision (allows_persona, category, etc.).
        write_target: One of "persona", "reference_only", "external_context_only".
        is_teacher_cognition: True when write_target is "persona" and the
            evidence source_label is "teacher_original".
        gated_evidence: The evidence with persona gate metadata applied.
    """

    evidence_id: str
    gate_decision: PersonaGateDecision
    write_target: str
    is_teacher_cognition: bool
    gated_evidence: EvidenceItem
    target: str = CognitionWriteTarget.REASONING_TRACE.value
    allowed: bool = False
    source_classification: str = ""
    category: str = ""
    confidence: float = 0.0
    half_life_class: str = ""
    source_boundary: str = "teacher_cognition_write_gate"
    data_gaps: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


class CognitionWriteGateService:
    """Unified write gate for cognition/persona evidence.

    Evaluates whether evidence is eligible for teacher cognition write,
    applies persona gate metadata to the evidence, and returns a
    CognitionWriteGateResult with the decision and gated evidence.
    """

    def __init__(self, gate: PersonaIngestionGate | None = None) -> None:
        self._gate = gate or PersonaIngestionGate()

    def evaluate_evidence(
        self,
        evidence: EvidenceItem,
        target: CognitionWriteTarget | str = CognitionWriteTarget.REASONING_TRACE,
    ) -> CognitionWriteGateResult:
        """Evaluate evidence and return write gate result.

        Checks for cached decision in metadata first, evaluates gate,
        applies gate to evidence, and determines write_target.
        """
        decision = decision_from_metadata(evidence.metadata)
        if decision is None:
            decision = self._gate.evaluate(evidence)

        gated = apply_persona_gate(evidence, decision)

        write_target = self._determine_write_target(gated, decision)
        normalized_target = (
            target.value if isinstance(target, CognitionWriteTarget) else str(target)
        )

        is_teacher = (
            write_target == "persona"
            and gated.source_label.label == "teacher_original"
        )
        data_gaps = _data_gaps_for(decision, is_teacher)

        return CognitionWriteGateResult(
            evidence_id=evidence.evidence_id,
            gate_decision=decision,
            write_target=write_target,
            is_teacher_cognition=is_teacher,
            gated_evidence=gated,
            target=normalized_target,
            allowed=is_teacher,
            source_classification=decision.source_classification,
            category=decision.category,
            confidence=decision.confidence,
            half_life_class=decision.half_life_class,
            data_gaps=data_gaps,
            reasons=tuple(decision.reasons),
        )

    def prepare_evidence(
        self,
        evidence: EvidenceItem,
        target: CognitionWriteTarget | str = CognitionWriteTarget.REASONING_TRACE,
    ) -> CognitionWriteGateResult:
        """Return the write decision plus evidence with gate metadata applied."""
        return self.evaluate_evidence(evidence, target=target)

    def select_traces(
        self,
        traces: list[ReasoningTrace],
        evidence_lookup: dict[str, EvidenceItem],
        *,
        teacher_id: str | None = None,
    ) -> list[ReasoningTrace]:
        """Select traces whose source evidence is eligible for teacher cognition."""
        selected: list[ReasoningTrace] = []
        for trace in traces:
            if teacher_id is not None and trace.teacher_id != teacher_id:
                continue
            evidence = evidence_lookup.get(trace.source_evidence_id)
            if evidence is None:
                continue
            result = self.evaluate_evidence(
                evidence,
                target=CognitionWriteTarget.TEACHER_PERSONA,
            )
            if result.allowed:
                selected.append(trace)
        return selected

    @staticmethod
    def _determine_write_target(
        evidence: EvidenceItem,
        decision: PersonaGateDecision,
    ) -> str:
        """Determine the write target for evidence based on gate decision."""
        if decision.category == "external_context_only":
            return "external_context_only"
        if not decision.allows_persona:
            return "reference_only"
        # teacher_original label + persona allowed → persona write target
        if evidence.source_label.label == "teacher_original":
            return "persona"
        return "reference_only"


def _data_gaps_for(
    decision: PersonaGateDecision,
    is_teacher_cognition: bool,
) -> tuple[str, ...]:
    if is_teacher_cognition:
        return ()
    if decision.source_classification == "external_context":
        return ("external_context_rejected_for_cognition",)
    if decision.source_classification == "research_reference":
        return ("research_reference_rejected_for_cognition",)
    if decision.source_classification == "ai_assisted_reference":
        return ("ai_reference_rejected_for_cognition",)
    return ("persona_gate_rejected_for_cognition",)
