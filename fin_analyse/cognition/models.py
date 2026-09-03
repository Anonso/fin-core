"""Data models for the investment cognition layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceLabel:
    label: str
    teacher_id: str | None
    confidence: float
    reasons: list[str] = field(default_factory=list)
    human_override: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceLabel:
        return cls(
            label=str(data["label"]),
            teacher_id=data.get("teacher_id"),
            confidence=float(data.get("confidence", 0.0)),
            reasons=list(data.get("reasons", [])),
            human_override=bool(data.get("human_override", False)),
        )


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    source_type: str
    source_id: str
    title: str
    content: str
    author: str | None
    published_at: str | None
    collected_at: str
    companies: list[str]
    topics: list[str]
    source_label: SourceLabel
    reliability: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_label"] = self.source_label.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceItem:
        return cls(
            evidence_id=str(data["evidence_id"]),
            source_type=str(data["source_type"]),
            source_id=str(data["source_id"]),
            title=str(data.get("title", "")),
            content=str(data.get("content", "")),
            author=data.get("author"),
            published_at=data.get("published_at"),
            collected_at=str(data["collected_at"]),
            companies=list(data.get("companies", [])),
            topics=list(data.get("topics", [])),
            source_label=SourceLabel.from_dict(data["source_label"]),
            reliability=float(data.get("reliability", 0.0)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class ReasoningTrace:
    trace_id: str
    teacher_id: str
    source_evidence_id: str
    topic: str
    companies: list[str]
    premises: list[str]
    observed_variables: list[str]
    inferred_relationships: list[str]
    conclusion: str
    stance: str
    time_horizon: str
    risk_boundaries: list[str]
    invalidation_conditions: list[str]
    action_implications: list[str]
    extraction_confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReasoningTrace:
        return cls(**data)


@dataclass(frozen=True)
class CognitivePattern:
    pattern_id: str
    teacher_id: str
    name: str
    description: str
    trigger_conditions: list[str]
    typical_variables: list[str]
    typical_reasoning_shape: str
    supporting_trace_ids: list[str]
    counterexamples: list[str]
    confidence: float
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CognitivePattern:
        return cls(**data)


@dataclass(frozen=True)
class TeacherPersona:
    persona_id: str
    teacher_id: str
    display_name: str
    active_version: str
    style_summary: str
    core_pattern_ids: list[str]
    explicit_rules: list[str]
    known_blind_spots: list[str]
    evidence_policy: dict[str, Any]
    last_built_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TeacherPersona:
        return cls(**data)


@dataclass(frozen=True)
class TraceabilityReport:
    analysis_id: str
    supported_steps: list[str]
    weakly_supported_steps: list[str]
    unsupported_steps: list[str]
    factual_conflicts: list[str]
    cognition_conflicts: list[str]
    confidence_adjustment: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceabilityReport:
        return cls(**data)


@dataclass(frozen=True)
class TraceVerification:
    verification_id: str
    trace_id: str
    source_evidence_id: str
    teacher_id: str
    verdict: str
    verified_confidence: float
    confidence_adjustment: float
    issues: list[str]
    suggested_revision: dict[str, Any]
    reason: str
    verifier_backend: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceVerification:
        return cls(
            verification_id=str(data["verification_id"]),
            trace_id=str(data["trace_id"]),
            source_evidence_id=str(data["source_evidence_id"]),
            teacher_id=str(data["teacher_id"]),
            verdict=str(data["verdict"]),
            verified_confidence=float(data.get("verified_confidence", 0.0)),
            confidence_adjustment=float(data.get("confidence_adjustment", 0.0)),
            issues=list(data.get("issues", [])),
            suggested_revision=dict(data.get("suggested_revision", {})),
            reason=str(data.get("reason", "")),
            verifier_backend=str(data.get("verifier_backend", "unknown")),
            created_at=str(data["created_at"]),
        )


@dataclass(frozen=True)
class UsagePolicy:
    allowed_usage: list[str] = field(default_factory=list)
    forbidden_usage: list[str] = field(default_factory=list)

    @classmethod
    def default_research_policy(cls) -> UsagePolicy:
        return cls(
            allowed_usage=["research_tracking", "daily_key_interpretation"],
            forbidden_usage=[
                "direct_buy_signal",
                "automatic_position_change",
                "bypass_risk_guard",
                "persona_rebuild_without_review",
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsagePolicy:
        return cls(
            allowed_usage=list(data.get("allowed_usage", [])),
            forbidden_usage=list(data.get("forbidden_usage", [])),
        )


@dataclass(frozen=True)
class ZsxqCognitionSource:
    source_id: str
    article_path: str
    article_id: str | None
    topic_id: str | None
    published_at: str
    column: str
    title: str
    content: str
    image_descriptions: list[str]
    image_ocr: list[str]
    source_rank: str
    completeness: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZsxqCognitionSource:
        return cls(
            source_id=str(data["source_id"]),
            article_path=str(data["article_path"]),
            article_id=data.get("article_id"),
            topic_id=data.get("topic_id"),
            published_at=str(data.get("published_at", "")),
            column=str(data.get("column", "")),
            title=str(data.get("title", "")),
            content=str(data.get("content", "")),
            image_descriptions=list(data.get("image_descriptions", [])),
            image_ocr=list(data.get("image_ocr", [])),
            source_rank=str(data.get("source_rank", "unknown")),
            completeness=str(data.get("completeness", "unknown")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class InformationUnit:
    unit_id: str
    source_id: str
    teacher_id: str
    unit_type: str
    title: str
    thesis: str
    original_evidence: list[str]
    apprentice_interpretation: str
    confidence: float
    related_companies: list[str]
    related_topics: list[str]
    theme_cluster_ids: list[str]
    usage_policy: UsagePolicy
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["usage_policy"] = self.usage_policy.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InformationUnit:
        return cls(
            unit_id=str(data["unit_id"]),
            source_id=str(data["source_id"]),
            teacher_id=str(data.get("teacher_id", "guo")),
            unit_type=str(data["unit_type"]),
            title=str(data.get("title", "")),
            thesis=str(data.get("thesis", "")),
            original_evidence=list(data.get("original_evidence", [])),
            apprentice_interpretation=str(data.get("apprentice_interpretation", "")),
            confidence=float(data.get("confidence", 0.0)),
            related_companies=list(data.get("related_companies", [])),
            related_topics=list(data.get("related_topics", [])),
            theme_cluster_ids=list(data.get("theme_cluster_ids", [])),
            usage_policy=UsagePolicy.from_dict(data.get("usage_policy", {})),
            created_at=str(data.get("created_at", "")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class ValidationSignal:
    signal_id: str
    unit_id: str
    signal_type: str
    description: str
    source_ref: str
    observed_at: str
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ValidationSignal:
        return cls(
            signal_id=str(data["signal_id"]),
            unit_id=str(data["unit_id"]),
            signal_type=str(data["signal_type"]),
            description=str(data.get("description", "")),
            source_ref=str(data.get("source_ref", "")),
            observed_at=str(data.get("observed_at", "")),
            confidence=float(data.get("confidence", 0.0)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class EvidenceChain:
    chain_id: str
    unit_id: str
    original_claims: list[str]
    original_source_refs: list[str]
    apprentice_inferences: list[str]
    inference_confidence: float
    external_validations: list[ValidationSignal]
    counter_evidence: list[ValidationSignal]
    source_boundary_notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["external_validations"] = [item.to_dict() for item in self.external_validations]
        data["counter_evidence"] = [item.to_dict() for item in self.counter_evidence]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceChain:
        return cls(
            chain_id=str(data["chain_id"]),
            unit_id=str(data["unit_id"]),
            original_claims=list(data.get("original_claims", [])),
            original_source_refs=list(data.get("original_source_refs", [])),
            apprentice_inferences=list(data.get("apprentice_inferences", [])),
            inference_confidence=float(data.get("inference_confidence", 0.0)),
            external_validations=[
                ValidationSignal.from_dict(item) for item in data.get("external_validations", [])
            ],
            counter_evidence=[
                ValidationSignal.from_dict(item) for item in data.get("counter_evidence", [])
            ],
            source_boundary_notes=list(data.get("source_boundary_notes", [])),
        )


@dataclass(frozen=True)
class ThemeCluster:
    cluster_id: str
    name: str
    description: str
    teacher_id: str
    unit_ids: list[str]
    source_ids: list[str]
    core_theses: list[str]
    active_status: str
    priority: float
    last_reinforced_at: str
    tracking_indicators: list[str]
    risks: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThemeCluster:
        return cls(
            cluster_id=str(data["cluster_id"]),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            teacher_id=str(data.get("teacher_id", "guo")),
            unit_ids=list(data.get("unit_ids", [])),
            source_ids=list(data.get("source_ids", [])),
            core_theses=list(data.get("core_theses", [])),
            active_status=str(data.get("active_status", "new")),
            priority=float(data.get("priority", 0.0)),
            last_reinforced_at=str(data.get("last_reinforced_at", "")),
            tracking_indicators=list(data.get("tracking_indicators", [])),
            risks=list(data.get("risks", [])),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class DynamicClock:
    unit_id: str
    state: str
    observed_at: str
    base_half_life_days: float
    effective_until: str | None
    freshness_score: float
    upgrade_triggers: list[str]
    downgrade_triggers: list[str]
    reset_triggers: list[str]
    last_evaluated_at: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DynamicClock:
        return cls(
            unit_id=str(data["unit_id"]),
            state=str(data.get("state", "fresh")),
            observed_at=str(data.get("observed_at", "")),
            base_half_life_days=float(data.get("base_half_life_days", 0.0)),
            effective_until=data.get("effective_until"),
            freshness_score=float(data.get("freshness_score", 0.0)),
            upgrade_triggers=list(data.get("upgrade_triggers", [])),
            downgrade_triggers=list(data.get("downgrade_triggers", [])),
            reset_triggers=list(data.get("reset_triggers", [])),
            last_evaluated_at=str(data.get("last_evaluated_at", "")),
            reason=str(data.get("reason", "")),
        )


@dataclass(frozen=True)
class InvestmentResearchSuggestion:
    suggestion_id: str
    unit_id: str
    suggestion_level: str
    summary: str
    upgrade_conditions: list[str]
    downgrade_conditions: list[str]
    tracking_indicators: list[str]
    risk_boundaries: list[str]
    allowed_usage: list[str]
    forbidden_usage: list[str]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvestmentResearchSuggestion:
        return cls(
            suggestion_id=str(data["suggestion_id"]),
            unit_id=str(data["unit_id"]),
            suggestion_level=str(data.get("suggestion_level", "observation")),
            summary=str(data.get("summary", "")),
            upgrade_conditions=list(data.get("upgrade_conditions", [])),
            downgrade_conditions=list(data.get("downgrade_conditions", [])),
            tracking_indicators=list(data.get("tracking_indicators", [])),
            risk_boundaries=list(data.get("risk_boundaries", [])),
            allowed_usage=list(data.get("allowed_usage", [])),
            forbidden_usage=list(data.get("forbidden_usage", [])),
            confidence=float(data.get("confidence", 0.0)),
        )


@dataclass(frozen=True)
class ZsxqApprenticeResult:
    source: ZsxqCognitionSource
    units: list[InformationUnit]
    evidence_chains: list[EvidenceChain]
    theme_clusters: list[ThemeCluster]
    clocks: list[DynamicClock]
    suggestions: list[InvestmentResearchSuggestion]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "units": [item.to_dict() for item in self.units],
            "evidence_chains": [item.to_dict() for item in self.evidence_chains],
            "theme_clusters": [item.to_dict() for item in self.theme_clusters],
            "clocks": [item.to_dict() for item in self.clocks],
            "suggestions": [item.to_dict() for item in self.suggestions],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZsxqApprenticeResult:
        return cls(
            source=ZsxqCognitionSource.from_dict(data["source"]),
            units=[InformationUnit.from_dict(item) for item in data.get("units", [])],
            evidence_chains=[
                EvidenceChain.from_dict(item) for item in data.get("evidence_chains", [])
            ],
            theme_clusters=[
                ThemeCluster.from_dict(item) for item in data.get("theme_clusters", [])
            ],
            clocks=[DynamicClock.from_dict(item) for item in data.get("clocks", [])],
            suggestions=[
                InvestmentResearchSuggestion.from_dict(item) for item in data.get("suggestions", [])
            ],
            warnings=list(data.get("warnings", [])),
        )
