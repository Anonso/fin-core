"""Strict Persona ingestion gate for teacher cognition learning."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from fin_analyse.cognition.models import EvidenceItem, SourceLabel

STAR_COLUMNS = ("星大派特刊", "星大派锐评", "星大派好问题", "凤仙郡小故事", "星大派")
GOOD_QUESTION_COLUMNS = ("星大派好问题", "好问题", "问题回答", "回答问题")

RESEARCH_MARKERS = (
    "研报",
    "券商",
    "给予买入评级",
    "盈利预测",
    "目标价",
    "机构上调",
)
AI_MARKERS = (
    "AI分析",
    "AI整理",
    "AI 辅助",
    "以下为AI",
    "由AI",
    "模型整理",
)
METHODOLOGY_MARKERS = (
    "方法",
    "方法论",
    "逻辑",
    "框架",
    "关键变量",
    "变量",
    "产业链",
    "板块逻辑",
    "预期差",
    "兑现",
    "节奏",
    "风险边界",
    "失效条件",
    "验证",
    "复盘",
    "思路",
)
TIME_SENSITIVE_MARKERS = (
    "今天",
    "短线",
    "情绪",
    "涨停",
    "异动",
    "催化",
    "启动",
    "回调",
)


@dataclass(frozen=True)
class PersonaGateDecision:
    evidence_id: str
    allows_persona: bool
    category: str
    source_classification: str
    confidence: float
    half_life_class: str
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonaGateDecision:
        return cls(
            evidence_id=str(data["evidence_id"]),
            allows_persona=bool(data["allows_persona"]),
            category=str(data["category"]),
            source_classification=str(data["source_classification"]),
            confidence=float(data.get("confidence", 0.0)),
            half_life_class=str(data.get("half_life_class", "medium_logic")),
            reasons=[str(reason) for reason in data.get("reasons", [])],
        )


class PersonaIngestionGate:
    """Decide whether evidence may train teacher Persona/patterns."""

    def evaluate(self, evidence: EvidenceItem) -> PersonaGateDecision:
        text = f"{evidence.title}\n{evidence.content}"
        column = _column(evidence)
        label = evidence.source_label.label

        if _is_external_context(evidence):
            return _decision(
                evidence,
                allows=False,
                category="external_context_only",
                source="external_context",
                confidence=0.98,
                half_life="medium_logic",
                reasons=["external_context is never eligible for Persona"],
            )

        if label == "ai_assisted" or _contains_any(text, AI_MARKERS):
            return _decision(
                evidence,
                allows=False,
                category="ai_reference_only",
                source="ai_assisted_reference",
                confidence=0.96,
                half_life="medium_logic",
                reasons=["AI-assisted content is reference-only"],
            )

        if label == "research_report" or _looks_like_research_report(text):
            return _decision(
                evidence,
                allows=False,
                category="research_reference_only",
                source="research_reference",
                confidence=0.95,
                half_life="medium_logic",
                reasons=["research-report markers are reference-only"],
            )

        if _is_star_column(column):
            return _decision(
                evidence,
                allows=True,
                category="star_teacher_original",
                source="teacher_original",
                confidence=max(evidence.source_label.confidence, 0.88),
                half_life=_half_life_for_text(text),
                reasons=[f"星大派 source column: {column}"],
            )

        if _is_good_question(evidence, column):
            methodology_hits = _marker_hits(text, METHODOLOGY_MARKERS)
            if label == "teacher_original" and len(methodology_hits) >= 2:
                return _decision(
                    evidence,
                    allows=True,
                    category="teacher_methodology_candidate",
                    source="teacher_methodology",
                    confidence=min(max(evidence.source_label.confidence, 0.72), 0.9),
                    half_life="long_methodology",
                    reasons=[
                        "non-star good question contains teacher methodology markers",
                        "markers: " + ", ".join(methodology_hits[:5]),
                    ],
                )
            return _decision(
                evidence,
                allows=False,
                category="time_sensitive_signal_only",
                source="market_observation",
                confidence=0.72,
                half_life="short_signal",
                reasons=["good question lacks enough methodology markers for Persona"],
            )

        if label == "teacher_original":
            methodology_hits = _marker_hits(text, METHODOLOGY_MARKERS)
            if len(methodology_hits) >= 3:
                return _decision(
                    evidence,
                    allows=True,
                    category="teacher_methodology_candidate",
                    source="teacher_methodology",
                    confidence=min(evidence.source_label.confidence, 0.78),
                    half_life="long_methodology",
                    reasons=["teacher_original label with strong methodology markers"],
                )

        return _decision(
            evidence,
            allows=False,
            category="reject_for_persona",
            source="unknown_reference",
            confidence=0.65,
            half_life=_half_life_for_text(text),
            reasons=["not 星大派 and not a clear teacher methodology candidate"],
        )


def apply_persona_gate(
    evidence: EvidenceItem,
    decision: PersonaGateDecision | None = None,
) -> EvidenceItem:
    gate_decision = decision or PersonaIngestionGate().evaluate(evidence)
    metadata = dict(evidence.metadata)
    metadata["persona_eligible"] = gate_decision.allows_persona
    metadata["persona_gate"] = gate_decision.to_dict()
    metadata["source_classification"] = gate_decision.source_classification
    metadata["half_life_class"] = gate_decision.half_life_class

    label = evidence.source_label
    if gate_decision.allows_persona and label.label == "unknown":
        label = SourceLabel(
            "teacher_original",
            label.teacher_id,
            max(label.confidence, gate_decision.confidence),
            [*label.reasons, *gate_decision.reasons],
            label.human_override,
        )
    elif not gate_decision.allows_persona and label.label == "teacher_original":
        label = SourceLabel(
            "unknown",
            label.teacher_id,
            min(label.confidence, gate_decision.confidence),
            [*label.reasons, "persona gate rejected teacher_original", *gate_decision.reasons],
            label.human_override,
        )

    return EvidenceItem.from_dict(
        {
            **evidence.to_dict(),
            "source_label": label.to_dict(),
            "metadata": metadata,
        }
    )


def decision_from_metadata(metadata: dict[str, object]) -> PersonaGateDecision | None:
    raw = metadata.get("persona_gate")
    if not isinstance(raw, dict):
        return None
    try:
        return PersonaGateDecision.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return None


def _decision(
    evidence: EvidenceItem,
    *,
    allows: bool,
    category: str,
    source: str,
    confidence: float,
    half_life: str,
    reasons: list[str],
) -> PersonaGateDecision:
    return PersonaGateDecision(
        evidence_id=evidence.evidence_id,
        allows_persona=allows,
        category=category,
        source_classification=source,
        confidence=max(0.0, min(confidence, 1.0)),
        half_life_class=half_life,
        reasons=reasons,
    )


def _column(evidence: EvidenceItem) -> str:
    value = evidence.metadata.get("column") or evidence.author or ""
    return str(value)


def _is_external_context(evidence: EvidenceItem) -> bool:
    return (
        evidence.source_type == "external_context"
        or evidence.metadata.get("evidence_type") == "external_context"
        or evidence.source_label.label == "external_context"
    )


def _is_star_column(column: str) -> bool:
    return any(marker in column for marker in STAR_COLUMNS)


def _is_good_question(evidence: EvidenceItem, column: str) -> bool:
    title = evidence.title
    return (
        bool(evidence.metadata.get("is_qa"))
        or any(marker in column for marker in GOOD_QUESTION_COLUMNS)
        or "好问题" in title
        or "问题" in title
        or "提问" in title  # S-028: detect Q&A articles with "提问" in title
    )


def _looks_like_research_report(text: str) -> bool:
    return len(_marker_hits(text, RESEARCH_MARKERS)) >= 2


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _marker_hits(text: str, markers: tuple[str, ...]) -> list[str]:
    return [marker for marker in markers if marker in text]


def _half_life_for_text(text: str) -> str:
    methodology_hits = len(_marker_hits(text, METHODOLOGY_MARKERS))
    time_hits = len(_marker_hits(text, TIME_SENSITIVE_MARKERS))
    if methodology_hits >= 3:
        return "long_methodology"
    if time_hits >= 2:
        return "short_signal"
    return "medium_logic"
