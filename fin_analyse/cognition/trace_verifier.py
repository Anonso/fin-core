"""Selective LLM verification for low-confidence reasoning traces."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fin_analyse.cognition.models import EvidenceItem, ReasoningTrace, TraceVerification

_VALID_VERDICTS = frozenset({"keep", "revise", "reject"})


class TraceVerificationError(Exception):
    """Raised when a trace cannot be safely verified."""


@dataclass
class TraceVerificationReport:
    selected_count: int = 0
    verified_count: int = 0
    keep_count: int = 0
    revise_count: int = 0
    reject_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors_sample: list[str] = field(default_factory=list)
    verification_ids: list[str] = field(default_factory=list)


def select_low_confidence_traces(
    traces: list[ReasoningTrace],
    *,
    threshold: float = 0.5,
    limit: int = 3,
    teacher_id: str | None = None,
) -> list[ReasoningTrace]:
    """Return low-confidence traces sorted from lowest confidence upward."""
    selected = [t for t in traces if t.extraction_confidence <= threshold]
    if teacher_id is not None:
        selected = [t for t in selected if t.teacher_id == teacher_id]
    selected.sort(key=lambda t: (t.extraction_confidence, t.trace_id))
    return selected[:limit]


class SelectiveTraceVerifier:
    """LLM-backed verifier for one ReasoningTrace and its source evidence."""

    _PROMPT = """Verify whether this extracted investment reasoning trace is faithful to the source article.

Return ONLY a JSON object with these keys:
- verdict: one of keep, revise, reject
- verified_confidence: float 0-1, your confidence in the verified trace quality
- confidence_adjustment: float -1 to 1, suggested adjustment from the original extraction confidence
- issues: list of strings, concrete problems found; empty if none
- suggested_revision: object, suggested corrected trace fields; empty object if no revision needed
- reason: string, short Chinese explanation

Verdict rules:
- keep: the trace is faithful to the source; small wording differences are fine.
- revise: the trace direction is supported, but fields are overstated, incomplete, or need correction.
- reject: the trace is unsupported by the source or contradicts it.

Distinguish direct support, reasonable inference, and unsupported extraction. Do not introduce outside facts.

Source article:
Title: {title}
Author: {author}
Content:
{content}

Extracted trace:
trace_id: {trace_id}
topic: {topic}
companies: {companies}
premises: {premises}
observed_variables: {observed_variables}
inferred_relationships: {inferred_relationships}
conclusion: {conclusion}
stance: {stance}
time_horizon: {time_horizon}
risk_boundaries: {risk_boundaries}
invalidation_conditions: {invalidation_conditions}
action_implications: {action_implications}
extraction_confidence: {extraction_confidence}
"""

    def __init__(self, llm, *, verifier_backend: str = "unknown") -> None:
        self.llm = llm
        self.verifier_backend = verifier_backend

    def verify(self, trace: ReasoningTrace, evidence: EvidenceItem) -> TraceVerification:
        if trace.source_evidence_id != evidence.evidence_id:
            raise TraceVerificationError(
                f"Trace {trace.trace_id} does not belong to evidence {evidence.evidence_id}"
            )

        prompt = self._build_prompt(trace, evidence)
        result = self.llm.complete_json(prompt, expected_type="trace_verification")
        if not result.ok or not isinstance(result.data, dict):
            raise TraceVerificationError(result.error or "LLM verification failed")

        data = result.data
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict not in _VALID_VERDICTS:
            raise TraceVerificationError(f"Invalid verdict: {verdict}")

        created_at = datetime.now(UTC).isoformat()
        return TraceVerification(
            verification_id="tv-" + uuid4().hex[:12],
            trace_id=trace.trace_id,
            source_evidence_id=trace.source_evidence_id,
            teacher_id=trace.teacher_id,
            verdict=verdict,
            verified_confidence=_clamp_float(data.get("verified_confidence", 0.0), 0.0, 1.0),
            confidence_adjustment=_clamp_float(data.get("confidence_adjustment", 0.0), -1.0, 1.0),
            issues=_list_str(data.get("issues", [])),
            suggested_revision=_dict_value(data.get("suggested_revision", {})),
            reason=str(data.get("reason", "")),
            verifier_backend=self.verifier_backend,
            created_at=created_at,
        )

    def _build_prompt(self, trace: ReasoningTrace, evidence: EvidenceItem) -> str:
        return self._PROMPT.format(
            title=evidence.title,
            author=evidence.author or "unknown",
            content=evidence.content[:5000],
            trace_id=trace.trace_id,
            topic=trace.topic,
            companies=trace.companies,
            premises=trace.premises,
            observed_variables=trace.observed_variables,
            inferred_relationships=trace.inferred_relationships,
            conclusion=trace.conclusion,
            stance=trace.stance,
            time_horizon=trace.time_horizon,
            risk_boundaries=trace.risk_boundaries,
            invalidation_conditions=trace.invalidation_conditions,
            action_implications=trace.action_implications,
            extraction_confidence=trace.extraction_confidence,
        )


def _clamp_float(value: object, low: float, high: float) -> float:
    if not isinstance(value, (int, float, str)):
        return low
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return min(max(number, low), high)


def _list_str(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _dict_value(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}
