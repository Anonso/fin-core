"""FIN-owned bounded capability broker for the replaceable Agent runtime.

The broker is deliberately transport-neutral.  Runtime adapters receive an
opaque, per-run grant; only this module owns authorization, call accounting and
source/effect enforcement.  Registered capabilities are trusted FIN functions,
not another generic Agent loop.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from fin_analyse.moa.engine import MoAEngine
from fin_analyse.moa.models import MoAReferenceRole, MoARequest
from fin_analyse.read_capabilities.types import (
    CapabilitySource,
    SourceKind,
    SourceTrust,
)

__all__ = [
    "CapabilitySource",
    "SourceKind",
    "SourceTrust",
]


class CapabilityEffect(StrEnum):
    READ = "read"
    COMPUTE = "compute"


@dataclass(frozen=True)
class CapabilityBoundary:
    """Declared effects checked before a registered implementation can run."""

    effect: CapabilityEffect
    invokes_agent_runtime: bool = False
    writes_cognition: bool = False
    writes_portfolio: bool = False
    writes_orders: bool = False
    owns_continuation: bool = False
    emits_final_product: bool = False


@dataclass(frozen=True)
class CapabilityOutput:
    """Internal implementation output before broker validation."""

    value: object
    sources: tuple[CapabilitySource, ...] = ()
    data_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityInvocationContext:
    """Broker-owned context which a runtime caller cannot override."""

    run_id: str
    capability: str
    call_index: int
    expires_at: datetime
    sources: tuple[CapabilitySource, ...]
    policy: object | None


CapabilityHandler = Callable[[object], CapabilityOutput]
ContextCapabilityHandler = Callable[[CapabilityInvocationContext, object], CapabilityOutput]


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    input_type: type[object]
    output_type: type[object]
    handler: CapabilityHandler | None
    boundary: CapabilityBoundary
    context_handler: ContextCapabilityHandler | None = None
    policy_type: type[object] | None = None
    default_policy: object | None = None
    allowed_source_kinds: frozenset[SourceKind] = field(
        default_factory=lambda: frozenset(SourceKind)
    )


@dataclass(frozen=True)
class CapabilityGrantHandle:
    """Opaque handle passed only to the configured runtime bridge."""

    token: str
    run_id: str
    expires_at: datetime


@dataclass(frozen=True)
class CapabilityCall:
    grant_token: str
    run_id: str
    capability: str
    source_scope_token: str
    payload: object
    sources: tuple[CapabilitySource, ...] = ()


@dataclass(frozen=True)
class CapabilityTrace:
    """Sanitized trace: never includes grant/scope tokens or raw payloads."""

    capability: str
    run_ref: str
    call_index: int
    source_kinds: tuple[SourceKind, ...]
    status: str = "ok"


@dataclass(frozen=True)
class CapabilityCallResult:
    value: object
    sources: tuple[CapabilitySource, ...]
    data_gaps: tuple[str, ...]
    trace: CapabilityTrace


class AgentCapabilityPort(Protocol):
    """Transport-neutral seam used by the single configured Agent runtime."""

    def invoke(self, call: CapabilityCall) -> CapabilityCallResult:
        """Authorize and execute one bounded capability call."""
        ...


class CapabilityErrorCode(StrEnum):
    UNKNOWN_GRANT = "capability_grant_unknown"
    CROSS_RUN = "capability_grant_cross_run"
    SOURCE_SCOPE = "capability_source_scope_invalid"
    EXPIRED = "capability_grant_expired"
    UNAUTHORIZED = "capability_not_allowed"
    COUNT_EXCEEDED = "capability_call_count_exceeded"
    INPUT_INVALID = "capability_input_invalid"
    OUTPUT_INVALID = "capability_output_invalid"
    BOUNDARY_VIOLATION = "capability_boundary_violation"


class CapabilityRejectedError(RuntimeError):
    def __init__(self, code: CapabilityErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value if not detail else f"{code.value}: {detail}")


@dataclass
class _GrantState:
    run_id: str
    capabilities: frozenset[str]
    source_scope_token: str
    source_scope: frozenset[CapabilitySource]
    allowed_source_kinds: frozenset[SourceKind]
    max_calls: int
    expires_at: datetime
    policies: dict[str, object | None]
    used_calls: int = 0


class InMemoryAgentCapabilityBroker(AgentCapabilityPort):
    """Per-process registry with atomic, per-run grant accounting."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._definitions: dict[str, CapabilityDefinition] = {}
        self._grants: dict[str, _GrantState] = {}
        self._lock = threading.Lock()

    def register(self, definition: CapabilityDefinition) -> None:
        if not definition.name.strip():
            raise ValueError("capability name must not be empty")
        if definition.name in self._definitions:
            raise ValueError(f"capability already registered: {definition.name}")
        if (definition.handler is None) == (definition.context_handler is None):
            raise ValueError("define exactly one capability handler")
        if (
            definition.policy_type is not None
            and definition.default_policy is not None
            and not isinstance(definition.default_policy, definition.policy_type)
        ):
            raise ValueError("default policy does not match policy_type")
        self._definitions[definition.name] = definition

    def issue_grant(
        self,
        *,
        run_id: str,
        capabilities: Collection[str],
        source_scope_token: str,
        source_scope: Collection[CapabilitySource],
        allowed_source_kinds: Collection[SourceKind],
        max_calls: int,
        expires_at: datetime,
        policies: Mapping[str, object] | None = None,
    ) -> CapabilityGrantHandle:
        if not run_id or not source_scope_token:
            raise ValueError("run_id and source_scope_token are required")
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        requested = frozenset(capabilities)
        missing = requested.difference(self._definitions)
        if missing:
            raise ValueError(f"unregistered capabilities: {sorted(missing)}")
        supplied_policies = dict(policies or {})
        unknown_policies = supplied_policies.keys() - requested
        if unknown_policies:
            raise ValueError(f"policies for capabilities outside grant: {sorted(unknown_policies)}")
        resolved_policies: dict[str, object | None] = {}
        for name in requested:
            definition = self._definitions[name]
            policy = supplied_policies.get(name, definition.default_policy)
            if definition.policy_type is not None and not isinstance(
                policy, definition.policy_type
            ):
                raise ValueError(f"missing or invalid policy for capability: {name}")
            resolved_policies[name] = policy
        token = secrets.token_urlsafe(32)
        state = _GrantState(
            run_id=run_id,
            capabilities=requested,
            source_scope_token=source_scope_token,
            source_scope=frozenset(source_scope),
            allowed_source_kinds=frozenset(allowed_source_kinds),
            max_calls=max_calls,
            expires_at=expires_at,
            policies=resolved_policies,
        )
        with self._lock:
            self._grants[token] = state
        return CapabilityGrantHandle(token=token, run_id=run_id, expires_at=expires_at)

    def invoke(self, call: CapabilityCall) -> CapabilityCallResult:
        with self._lock:
            state = self._grants.get(call.grant_token)
            if state is None:
                raise CapabilityRejectedError(CapabilityErrorCode.UNKNOWN_GRANT)
            if call.run_id != state.run_id:
                raise CapabilityRejectedError(CapabilityErrorCode.CROSS_RUN)
            if not secrets.compare_digest(call.source_scope_token, state.source_scope_token):
                raise CapabilityRejectedError(CapabilityErrorCode.SOURCE_SCOPE)
            if self._clock() >= state.expires_at:
                raise CapabilityRejectedError(CapabilityErrorCode.EXPIRED)
            if call.capability not in state.capabilities:
                raise CapabilityRejectedError(CapabilityErrorCode.UNAUTHORIZED)
            definition = self._definitions[call.capability]
            self._validate_boundary(definition.boundary)
            self._validate_sources(
                call.sources,
                grant_source_scope=state.source_scope,
                grant_allowed=state.allowed_source_kinds,
                definition_allowed=definition.allowed_source_kinds,
            )
            if not isinstance(call.payload, definition.input_type):
                raise CapabilityRejectedError(CapabilityErrorCode.INPUT_INVALID)
            if state.used_calls >= state.max_calls:
                raise CapabilityRejectedError(CapabilityErrorCode.COUNT_EXCEEDED)
            state.used_calls += 1
            call_index = state.used_calls

        if definition.context_handler is not None:
            invocation = CapabilityInvocationContext(
                run_id=call.run_id,
                capability=call.capability,
                call_index=call_index,
                expires_at=state.expires_at,
                sources=call.sources,
                policy=state.policies[call.capability],
            )
            output = definition.context_handler(invocation, call.payload)
        else:
            if definition.handler is None:  # pragma: no cover - register() invariant
                raise RuntimeError("capability handler missing")
            output = definition.handler(call.payload)
        if self._clock() >= state.expires_at:
            raise CapabilityRejectedError(CapabilityErrorCode.EXPIRED)
        if not isinstance(output, CapabilityOutput):
            raise CapabilityRejectedError(CapabilityErrorCode.OUTPUT_INVALID)
        if not isinstance(output.value, definition.output_type):
            raise CapabilityRejectedError(CapabilityErrorCode.OUTPUT_INVALID)
        self._validate_sources(
            output.sources,
            grant_source_scope=state.source_scope,
            grant_allowed=state.allowed_source_kinds,
            definition_allowed=definition.allowed_source_kinds,
        )
        if self._contains_forbidden_product_field(output.value):
            raise CapabilityRejectedError(
                CapabilityErrorCode.BOUNDARY_VIOLATION,
                "capability output contains a final-product field",
            )

        sources = output.sources or call.sources
        trace = CapabilityTrace(
            capability=call.capability,
            run_ref=hashlib.sha256(call.run_id.encode("utf-8")).hexdigest()[:12],
            call_index=call_index,
            source_kinds=tuple(dict.fromkeys(source.kind for source in sources)),
        )
        return CapabilityCallResult(
            value=output.value,
            sources=sources,
            data_gaps=output.data_gaps,
            trace=trace,
        )

    @staticmethod
    def _validate_boundary(boundary: CapabilityBoundary) -> None:
        forbidden = (
            boundary.invokes_agent_runtime
            or boundary.writes_cognition
            or boundary.writes_portfolio
            or boundary.writes_orders
            or boundary.owns_continuation
            or boundary.emits_final_product
        )
        if forbidden:
            raise CapabilityRejectedError(CapabilityErrorCode.BOUNDARY_VIOLATION)

    @staticmethod
    def _validate_sources(
        sources: tuple[CapabilitySource, ...],
        *,
        grant_source_scope: frozenset[CapabilitySource],
        grant_allowed: frozenset[SourceKind],
        definition_allowed: frozenset[SourceKind],
    ) -> None:
        for source in sources:
            if not source.ref.strip():
                raise CapabilityRejectedError(CapabilityErrorCode.SOURCE_SCOPE)
            if source not in grant_source_scope:
                raise CapabilityRejectedError(CapabilityErrorCode.SOURCE_SCOPE)
            if source.kind not in grant_allowed or source.kind not in definition_allowed:
                raise CapabilityRejectedError(CapabilityErrorCode.SOURCE_SCOPE)
            if source.kind is SourceKind.G:
                if source.trust is not SourceTrust.FIN_TRUSTED_G:
                    raise CapabilityRejectedError(CapabilityErrorCode.SOURCE_SCOPE)
            elif source.trust is not SourceTrust.NON_G:
                raise CapabilityRejectedError(CapabilityErrorCode.SOURCE_SCOPE)

    @classmethod
    def _contains_forbidden_product_field(cls, value: object) -> bool:
        forbidden = {"answer", "final_answer", "public_product", "current_advice"}
        if isinstance(value, Mapping):
            if forbidden.intersection(str(key) for key in value):
                return True
            return any(cls._contains_forbidden_product_field(item) for item in value.values())
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(cls._contains_forbidden_product_field(item) for item in value)
        return False


INDEPENDENT_DELIBERATION_CAPABILITY = "fin.independent_deliberation"
DEFAULT_INDEPENDENT_DELIBERATION_RECIPE_ID = "balanced-v1"


class DeliberationPolicy(StrEnum):
    DISABLED = "disabled"
    ALLOWED = "allowed"
    REQUIRED = "required"


@dataclass(frozen=True)
class IndependentDeliberationGrantPolicy:
    """FIN-owned policy; the Agent cannot select an internal MoA recipe."""

    mode: DeliberationPolicy
    recipe_id: str = DEFAULT_INDEPENDENT_DELIBERATION_RECIPE_ID

    def __post_init__(self) -> None:
        if not isinstance(self.mode, DeliberationPolicy):
            raise ValueError("independent deliberation mode invalid")
        if not self.recipe_id.strip() or len(self.recipe_id) > 128:
            raise ValueError("independent deliberation recipe id invalid")


class DeliberationOutcome(StrEnum):
    ANSWER = "answer"
    RESEARCH = "research"


class DeliberationStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class DeliberationEvidence:
    source: CapabilitySource
    content: str


@dataclass(frozen=True)
class IndependentDeliberationRequest:
    task_id: str
    outcome: DeliberationOutcome
    question: str
    evidence: tuple[DeliberationEvidence, ...]
    trigger_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliberationRoleRecipe:
    name: str
    backend_name: str
    instruction: str


@dataclass(frozen=True)
class IndependentDeliberationRecipe:
    recipe_id: str
    roles: tuple[DeliberationRoleRecipe, ...]
    aggregator_instruction: str
    reference_timeout_seconds: float
    aggregator_timeout_seconds: float
    min_reference_success: int = 2
    partial_allowed: bool = True
    max_evidence_chars: int = 16_000


@dataclass(frozen=True)
class IndependentDeliberationFindings:
    """Sanitized findings only; deliberately has no final-answer field."""

    status: DeliberationStatus
    consensus: tuple[str, ...] = ()
    material_disagreements: tuple[str, ...] = ()
    counterarguments: tuple[str, ...] = ()
    blind_spots: tuple[str, ...] = ()
    unresolved_gaps: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    confidence_boundary: str = "not_established"


class IndependentDeliberationCapability:
    """Bounded FIN-owned MoA recipe, not an independently planning Agent."""

    _ANSWER_REFERENCE_LIMIT = 2
    _RESEARCH_REFERENCE_LIMIT = 3
    _MAX_EVIDENCE_ITEMS = 12
    _MAX_FINDING_CHARS = 600

    def __init__(
        self,
        *,
        engine: MoAEngine,
        recipes: Collection[IndependentDeliberationRecipe],
    ) -> None:
        self._engine = engine
        recipe_items = tuple(recipes)
        self._recipes = {recipe.recipe_id: recipe for recipe in recipe_items}
        if len(self._recipes) != len(recipe_items):
            raise ValueError("duplicate independent-deliberation recipe id")
        for recipe in recipe_items:
            if len(recipe.roles) < 2 or recipe.min_reference_success < 2:
                raise ValueError("independent deliberation requires at least two references")
            if recipe.max_evidence_chars < 1:
                raise ValueError("max_evidence_chars must be positive")
            if (
                not math.isfinite(recipe.reference_timeout_seconds)
                or recipe.reference_timeout_seconds <= 0
                or not math.isfinite(recipe.aggregator_timeout_seconds)
                or recipe.aggregator_timeout_seconds <= 0
            ):
                raise ValueError("independent deliberation timeouts must be finite and positive")

    def definition(self) -> CapabilityDefinition:
        return CapabilityDefinition(
            name=INDEPENDENT_DELIBERATION_CAPABILITY,
            input_type=IndependentDeliberationRequest,
            output_type=IndependentDeliberationFindings,
            handler=None,
            context_handler=self._invoke,
            policy_type=IndependentDeliberationGrantPolicy,
            boundary=CapabilityBoundary(effect=CapabilityEffect.COMPUTE),
            allowed_source_kinds=frozenset(SourceKind),
        )

    def _invoke(
        self,
        context: CapabilityInvocationContext,
        payload: object,
    ) -> CapabilityOutput:
        if not isinstance(payload, IndependentDeliberationRequest):
            raise CapabilityRejectedError(CapabilityErrorCode.INPUT_INVALID)
        if not isinstance(context.policy, IndependentDeliberationGrantPolicy):
            raise CapabilityRejectedError(CapabilityErrorCode.BOUNDARY_VIOLATION)
        policy = context.policy.mode
        evidence_refs = tuple(item.source.ref for item in payload.evidence)
        sources = tuple(item.source for item in payload.evidence)
        self._validate_request(payload, context)

        if policy is DeliberationPolicy.DISABLED:
            return self._output(
                DeliberationStatus.SKIPPED,
                evidence_refs=evidence_refs,
                sources=sources,
                gaps=("independent_deliberation_disabled",),
            )
        if policy is DeliberationPolicy.ALLOWED and not payload.trigger_reasons:
            return self._output(
                DeliberationStatus.SKIPPED,
                evidence_refs=evidence_refs,
                sources=sources,
                gaps=("independent_deliberation_not_triggered",),
            )

        recipe = self._recipes.get(context.policy.recipe_id)
        if recipe is None:
            return self._output(
                DeliberationStatus.UNAVAILABLE,
                evidence_refs=evidence_refs,
                sources=sources,
                gaps=("independent_deliberation_recipe_unavailable",),
            )

        reference_limit = (
            self._ANSWER_REFERENCE_LIMIT
            if payload.outcome is DeliberationOutcome.ANSWER
            else self._RESEARCH_REFERENCE_LIMIT
        )
        selected_roles = recipe.roles[:reference_limit]
        bounded_evidence = self._bounded_evidence(payload.evidence, recipe.max_evidence_chars)
        question = payload.question.strip()
        reference_roles = [
            MoAReferenceRole(
                name=role.name,
                backend_name=role.backend_name,
                prompt=(
                    f"{role.instruction}\n\n"
                    f"Question:\n{question}\n\n"
                    f"Bounded evidence:\n{bounded_evidence}\n\n"
                    "Return only bounded analytical findings; do not produce a final user answer."
                ),
            )
            for role in selected_roles
        ]
        request = MoARequest(
            task_id=payload.task_id,
            task_type="fin.independent_deliberation",
            context={
                "question": question,
                "evidence_refs": evidence_refs,
                "source_kinds": tuple(source.kind.value for source in sources),
            },
            aggregator_prompt=(
                f"{recipe.aggregator_instruction}\n"
                "Synthesize findings only. Never emit an answer, public product, continuation, "
                "or tool plan."
            ),
            reference_timeout_seconds=recipe.reference_timeout_seconds,
            aggregator_timeout_seconds=recipe.aggregator_timeout_seconds,
            reference_roles=reference_roles,
            expected_schema={
                "required": [
                    "consensus",
                    "material_disagreements",
                    "counterarguments",
                    "blind_spots",
                    "unresolved_gaps",
                    "confidence_boundary",
                ]
            },
            min_reference_success=min(max(2, recipe.min_reference_success), len(reference_roles)),
            metadata={
                "independent_deliberation_recipe": recipe.recipe_id,
                "bounded_reference_limit": reference_limit,
            },
        )
        result = self._engine.deliberate(request)
        if result.status != "ok":
            successful = [
                item for item in result.reference_outputs if item.ok and item.content.strip()
            ]
            partial_ok = bool(successful) and recipe.partial_allowed
            status = DeliberationStatus.PARTIAL if partial_ok else DeliberationStatus.UNAVAILABLE
            counterarguments = tuple(self._sanitize_finding(item.content) for item in successful)
            counterarguments = tuple(item for item in counterarguments if item)
            gaps = tuple(dict.fromkeys([*result.data_gaps, result.fallback_reason]))
            gaps = tuple(item for item in gaps if item)
            findings = IndependentDeliberationFindings(
                status=status,
                counterarguments=counterarguments if partial_ok else (),
                unresolved_gaps=gaps or ("independent_deliberation_unavailable",),
                evidence_refs=evidence_refs,
                confidence_boundary="aggregation_not_established",
            )
            return CapabilityOutput(
                value=findings,
                sources=sources,
                data_gaps=findings.unresolved_gaps,
            )

        final = result.final
        if InMemoryAgentCapabilityBroker._contains_forbidden_product_field(final):
            findings = IndependentDeliberationFindings(
                status=DeliberationStatus.UNAVAILABLE,
                unresolved_gaps=("independent_deliberation_final_product_rejected",),
                evidence_refs=evidence_refs,
            )
            return CapabilityOutput(
                value=findings,
                sources=sources,
                data_gaps=findings.unresolved_gaps,
            )
        findings = IndependentDeliberationFindings(
            status=DeliberationStatus.COMPLETED,
            consensus=self._strings(final.get("consensus")),
            material_disagreements=self._strings(final.get("material_disagreements")),
            counterarguments=self._strings(final.get("counterarguments")),
            blind_spots=self._strings(final.get("blind_spots")),
            unresolved_gaps=self._strings(final.get("unresolved_gaps")),
            evidence_refs=evidence_refs,
            confidence_boundary=self._sanitize_finding(final.get("confidence_boundary"))
            or "not_established",
        )
        return CapabilityOutput(
            value=findings,
            sources=sources,
            data_gaps=findings.unresolved_gaps,
        )

    @staticmethod
    def _validate_request(
        payload: IndependentDeliberationRequest,
        context: CapabilityInvocationContext,
    ) -> None:
        if not payload.task_id.strip() or not payload.question.strip():
            raise CapabilityRejectedError(CapabilityErrorCode.INPUT_INVALID)
        if len(payload.evidence) > IndependentDeliberationCapability._MAX_EVIDENCE_ITEMS:
            raise CapabilityRejectedError(CapabilityErrorCode.INPUT_INVALID)
        call_sources = {(source.ref, source.kind, source.trust) for source in context.sources}
        evidence_sources = {
            (item.source.ref, item.source.kind, item.source.trust) for item in payload.evidence
        }
        if evidence_sources != call_sources:
            raise CapabilityRejectedError(CapabilityErrorCode.SOURCE_SCOPE)

    @staticmethod
    def _bounded_evidence(
        evidence: tuple[DeliberationEvidence, ...],
        max_chars: int,
    ) -> str:
        parts: list[str] = []
        remaining = max(0, max_chars)
        for item in evidence:
            if remaining <= 0:
                break
            content = " ".join(item.content.split())[:remaining]
            rendered = f"[{item.source.ref}|{item.source.kind.value}] {content}"
            parts.append(rendered)
            remaining -= len(rendered)
        return "\n".join(parts)

    @classmethod
    def _strings(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in (cls._sanitize_finding(raw) for raw in value) if item)

    @classmethod
    def _sanitize_finding(cls, value: object) -> str:
        return " ".join(str(value or "").replace("```", "").split())[: cls._MAX_FINDING_CHARS]

    @staticmethod
    def _output(
        status: DeliberationStatus,
        *,
        evidence_refs: tuple[str, ...],
        sources: tuple[CapabilitySource, ...],
        gaps: tuple[str, ...],
    ) -> CapabilityOutput:
        findings = IndependentDeliberationFindings(
            status=status,
            unresolved_gaps=gaps,
            evidence_refs=evidence_refs,
        )
        return CapabilityOutput(value=findings, sources=sources, data_gaps=gaps)
