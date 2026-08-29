"""Cognition memory store — FIN-owned seam for cognition JSONL repository ownership.

Provides scoped read/write isolation for teacher cognition, external evidence,
shared reference, and agent private memory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fin_analyse.cognition.evidence_store import (
    JsonlRepository,
    OwnerOnlyReadJsonlRepository,
    validate_existing_owner_only_directory,
)
from fin_analyse.cognition.models import (
    CognitiveFeedback,
    CognitivePattern,
    EvidenceItem,
    PersonaAnalysis,
    ReasoningTrace,
    TeacherPersona,
    TraceVerification,
)
from fin_analyse.cognition.trace_verifier import select_low_confidence_traces

logger = logging.getLogger(__name__)
_READ_ONLY_OPERATIONS = frozenset(
    {
        "get_evidence",
        "list_traces",
        "list_patterns",
        "list_personas",
        "list_feedback",
        "list_trace_verifications",
        "select_low_confidence_traces",
    }
)
_MEMORY_KINDS = frozenset(
    {"teacher_cognition", "external_evidence", "shared_reference", "agent_private"}
)


# ── scope / request / result dataclasses ──────────────────────────────────


@dataclass(frozen=True)
class CognitionMemoryScope:
    """Machine-readable scope contract for cognition memory isolation.

    memory_kind: one of "teacher_cognition", "external_evidence",
                 "shared_reference", "agent_private".
    teacher_id:  required for teacher_cognition; must match trace/pattern/persona.
    agent_id:    required for agent_private.
    """

    memory_kind: str
    teacher_id: str = ""
    agent_id: str = ""
    source_boundary: str = ""


@dataclass
class CognitionMemoryRequest:
    """Request envelope for memory store operations."""

    operation: str
    scope: CognitionMemoryScope
    evidence: EvidenceItem | None = None
    trace: ReasoningTrace | None = None
    pattern: CognitivePattern | None = None
    persona: TeacherPersona | None = None
    analysis: PersonaAnalysis | None = None
    feedback: CognitiveFeedback | None = None
    trace_verification: TraceVerification | None = None
    evidence_id: str = ""
    threshold: float = 0.5
    limit: int = 3


@dataclass
class CognitionMemoryResult:
    """Result envelope returned by memory store operations."""

    operation: str
    status: str  # "success" or "error"
    payload: dict[str, Any] = field(default_factory=dict)
    source_boundary: str = ""
    data_gaps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    write_effect: str = ""


# ── service ────────────────────────────────────────────────────────────────


class CognitionMemoryStoreService:
    """FIN-owned cognition memory store with scope isolation.

    Owns core cognition JSONL repositories:
      - evidence_items.jsonl
      - reasoning_traces.jsonl
      - cognitive_patterns.jsonl
      - teacher_personas.jsonl
      - persona_analyses.jsonl

    Scope validation enforces that teacher_cognition writes carry a matching
    teacher_id, external_evidence cannot write cognition objects, and
    agent_private cannot write teacher persona/pattern.
    """

    def __init__(self, runtime_root: Path, *, existing_owner_only_read: bool = False) -> None:
        root = Path(runtime_root)
        repository_type: type[JsonlRepository[Any]]
        if existing_owner_only_read:
            validate_existing_owner_only_directory(root)
            repository_type = OwnerOnlyReadJsonlRepository
        else:
            root.mkdir(parents=True, exist_ok=True)
            repository_type = JsonlRepository
        self.runtime_root = root
        self._read_only = existing_owner_only_read
        self.evidence_repo: JsonlRepository[EvidenceItem] = repository_type(
            root / "evidence_items.jsonl", EvidenceItem, "evidence_id"
        )
        self.trace_repo: JsonlRepository[ReasoningTrace] = repository_type(
            root / "reasoning_traces.jsonl", ReasoningTrace, "trace_id"
        )
        self.pattern_repo: JsonlRepository[CognitivePattern] = repository_type(
            root / "cognitive_patterns.jsonl", CognitivePattern, "pattern_id"
        )
        self.persona_repo: JsonlRepository[TeacherPersona] = repository_type(
            root / "teacher_personas.jsonl", TeacherPersona, "persona_id"
        )
        self.analysis_repo: JsonlRepository[PersonaAnalysis] = repository_type(
            root / "persona_analyses.jsonl", PersonaAnalysis, "analysis_id"
        )
        self.feedback_repo: JsonlRepository[CognitiveFeedback] = repository_type(
            root / "feedback.jsonl", CognitiveFeedback, "feedback_id"
        )
        self.trace_verification_repo: JsonlRepository[TraceVerification] = repository_type(
            root / "trace_verifications.jsonl", TraceVerification, "verification_id"
        )

    @classmethod
    def open_existing_owner_only_read(cls, runtime_root: Path) -> CognitionMemoryStoreService:
        """Open an existing production store as a stable, non-mutating read view."""

        return cls(runtime_root, existing_owner_only_read=True)

    # ── handle dispatcher ───────────────────────────────────────────────

    def handle(self, request: CognitionMemoryRequest) -> CognitionMemoryResult:
        scope = request.scope
        op = request.operation
        if not isinstance(scope.memory_kind, str) or scope.memory_kind not in _MEMORY_KINDS:
            return CognitionMemoryResult(
                operation=op,
                status="error",
                source_boundary="unknown",
                payload={
                    "error_code": "UNKNOWN_MEMORY_KIND",
                    "detail": "Cognition memory scope is not recognized.",
                },
                write_effect="none",
            )
        if self._read_only and op not in _READ_ONLY_OPERATIONS:
            return CognitionMemoryResult(
                operation=op,
                status="error",
                source_boundary=scope.memory_kind,
                data_gaps=["cognition_memory_read_only"],
                write_effect="none",
            )

        # ── save_evidence ────────────────────────────────────────────
        if op == "save_evidence":
            return self._validate_and_save_evidence(request, scope)

        # ── get_evidence ─────────────────────────────────────────────
        if op == "get_evidence":
            return self._validate_and_get_evidence(request, scope)

        # ── upsert_trace ─────────────────────────────────────────────
        if op == "upsert_trace":
            return self._validate_and_upsert_trace(request, scope)

        # ── list_traces ──────────────────────────────────────────────
        if op == "list_traces":
            return self._validate_and_list_traces(scope)

        # ── upsert_pattern ───────────────────────────────────────────
        if op == "upsert_pattern":
            return self._validate_and_upsert_pattern(request, scope)

        # ── list_patterns ────────────────────────────────────────────
        if op == "list_patterns":
            return self._validate_and_list_patterns(scope)

        # ── upsert_persona ───────────────────────────────────────────
        if op == "upsert_persona":
            return self._validate_and_upsert_persona(request, scope)

        # ── list_personas ────────────────────────────────────────────
        if op == "list_personas":
            return self._validate_and_list_personas(scope)

        # ── upsert_analysis ──────────────────────────────────────────
        if op == "upsert_analysis":
            return self._validate_and_upsert_analysis(request, scope)

        # ── record_feedback ─────────────────────────────────────────
        if op == "record_feedback":
            return self._validate_and_record_feedback(request, scope)

        # ── list_feedback ───────────────────────────────────────────
        if op == "list_feedback":
            return self._validate_and_list_feedback(scope)

        # ── upsert_trace_verification ───────────────────────────────
        if op == "upsert_trace_verification":
            return self._validate_and_upsert_trace_verification(request, scope)

        # ── list_trace_verifications ────────────────────────────────
        if op == "list_trace_verifications":
            return self._validate_and_list_trace_verifications(scope)

        # ── select_low_confidence_traces ────────────────────────────
        if op == "select_low_confidence_traces":
            return self._validate_and_select_low_confidence_traces(request, scope)

        return CognitionMemoryResult(
            operation=op,
            status="error",
            source_boundary=scope.memory_kind,
            payload={"error_code": "UNKNOWN_OPERATION", "detail": f"Unknown operation: {op}"},
        )

    # ── scope validation helpers ────────────────────────────────────────

    def _resolve_boundary(self, scope: CognitionMemoryScope) -> str:
        return scope.memory_kind

    def _missing_teacher_result(
        self,
        *,
        operation: str,
        entity_label: str,
    ) -> CognitionMemoryResult:
        return CognitionMemoryResult(
            operation=operation,
            status="error",
            source_boundary="teacher_cognition",
            payload={
                "error_code": "MISSING_TEACHER_ID",
                "detail": f"teacher_cognition scope requires teacher_id for {entity_label}.",
            },
        )

    def _check_teacher_cognition_write(
        self, scope: CognitionMemoryScope, entity_label: str = "trace"
    ) -> CognitionMemoryResult | None:
        """Return error result if scope blocks cognition writes; None if ok."""
        if scope.memory_kind == "teacher_cognition" and not scope.teacher_id:
            return self._missing_teacher_result(
                operation="",
                entity_label=entity_label,
            )
        if scope.memory_kind == "external_evidence":
            return CognitionMemoryResult(
                operation="",
                status="error",
                source_boundary="external_evidence",
                payload={
                    "error_code": "EXTERNAL_SCOPE_CANNOT_WRITE_COGNITION",
                    "detail": f"External evidence scope cannot write {entity_label}.",
                },
            )
        if scope.memory_kind == "shared_reference":
            return CognitionMemoryResult(
                operation="",
                status="error",
                source_boundary="shared_reference",
                payload={
                    "error_code": "SHARED_REFERENCE_NOT_COGNITION_MEMORY",
                    "detail": f"Shared reference scope cannot write {entity_label}.",
                },
            )
        if scope.memory_kind == "agent_private":
            return CognitionMemoryResult(
                operation="",
                status="error",
                source_boundary="agent_private",
                payload={
                    "error_code": "AGENT_PRIVATE_CANNOT_WRITE_TEACHER_PERSONA",
                    "detail": f"Agent private scope cannot write {entity_label}.",
                },
            )
        return None

    def _check_teacher_cognition_read(
        self, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult | None:
        """Return error result if scope cannot read teacher cognition memory."""
        if scope.memory_kind == "teacher_cognition" and not scope.teacher_id:
            return self._missing_teacher_result(
                operation="",
                entity_label="read",
            )
        if scope.memory_kind == "external_evidence":
            return CognitionMemoryResult(
                operation="",
                status="error",
                source_boundary="external_evidence",
                payload={
                    "error_code": "EXTERNAL_SCOPE_CANNOT_READ_COGNITION",
                    "detail": "External evidence scope cannot read teacher cognition memory.",
                },
            )
        if scope.memory_kind == "shared_reference":
            return CognitionMemoryResult(
                operation="",
                status="error",
                source_boundary="shared_reference",
                payload={
                    "error_code": "SHARED_REFERENCE_NOT_COGNITION_MEMORY",
                    "detail": "Shared reference scope cannot read cognition memory.",
                },
            )
        if scope.memory_kind == "agent_private":
            if not scope.agent_id:
                return CognitionMemoryResult(
                    operation="",
                    status="error",
                    source_boundary="agent_private",
                    payload={
                        "error_code": "MISSING_AGENT_ID",
                        "detail": "agent_private scope requires agent_id.",
                    },
                )
            return CognitionMemoryResult(
                operation="",
                status="error",
                source_boundary="agent_private",
                payload={
                    "error_code": "AGENT_PRIVATE_CANNOT_READ_TEACHER_COGNITION",
                    "detail": "Agent private scope cannot read teacher cognition memory.",
                },
            )
        return None

    # ── operation implementations ────────────────────────────────────────

    def _validate_and_save_evidence(
        self, request: CognitionMemoryRequest, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult:
        if scope.memory_kind == "agent_private" and not scope.agent_id:
            return CognitionMemoryResult(
                operation="save_evidence",
                status="error",
                source_boundary="agent_private",
                payload={
                    "error_code": "MISSING_AGENT_ID",
                    "detail": "agent_private scope requires agent_id.",
                },
            )
        if scope.memory_kind == "shared_reference":
            return CognitionMemoryResult(
                operation="save_evidence",
                status="error",
                source_boundary="shared_reference",
                payload={
                    "error_code": "SHARED_REFERENCE_NOT_COGNITION_MEMORY",
                    "detail": "Shared reference scope cannot write cognition evidence.",
                },
            )
        if scope.memory_kind == "teacher_cognition" and not scope.teacher_id:
            return self._missing_teacher_result(
                operation="save_evidence",
                entity_label="evidence",
            )
        if request.evidence is None:
            return CognitionMemoryResult(
                operation="save_evidence",
                status="error",
                source_boundary=scope.memory_kind,
                payload={"error_code": "MISSING_EVIDENCE", "detail": "No evidence provided."},
            )
        if scope.memory_kind == "teacher_cognition":
            evidence_teacher = request.evidence.source_label.teacher_id or ""
            if evidence_teacher != scope.teacher_id:
                return CognitionMemoryResult(
                    operation="save_evidence",
                    status="error",
                    source_boundary=scope.memory_kind,
                    payload={
                        "error_code": "TEACHER_ID_MISMATCH",
                        "detail": (
                            f"Evidence teacher_id '{evidence_teacher}' "
                            f"does not match scope teacher_id '{scope.teacher_id}'."
                        ),
                    },
                )
        self.evidence_repo.upsert(request.evidence)
        return CognitionMemoryResult(
            operation="save_evidence",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            write_effect="evidence_saved",
            payload={"evidence_id": request.evidence.evidence_id},
        )

    def _validate_and_get_evidence(
        self, request: CognitionMemoryRequest, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult:
        if scope.memory_kind == "agent_private" and not scope.agent_id:
            return CognitionMemoryResult(
                operation="get_evidence",
                status="error",
                source_boundary="agent_private",
                payload={
                    "error_code": "MISSING_AGENT_ID",
                    "detail": "agent_private scope requires agent_id.",
                },
            )
        if request.evidence_id == "":
            return CognitionMemoryResult(
                operation="get_evidence",
                status="error",
                source_boundary=scope.memory_kind,
                payload={"error_code": "MISSING_EVIDENCE_ID", "detail": "No evidence_id provided."},
            )
        matches = self.evidence_repo.find(lambda item: item.evidence_id == request.evidence_id)
        if not matches:
            return CognitionMemoryResult(
                operation="get_evidence",
                status="error",
                source_boundary=scope.memory_kind,
                payload={
                    "error_code": "EVIDENCE_NOT_FOUND",
                    "detail": f"Evidence not found: {request.evidence_id}",
                    "not_found": True,
                },
            )
        return CognitionMemoryResult(
            operation="get_evidence",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            payload={"evidence": matches[-1]},
        )

    def _validate_and_upsert_trace(
        self, request: CognitionMemoryRequest, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult:
        block = self._check_teacher_cognition_write(scope, "trace")
        if block is not None:
            block.operation = "upsert_trace"
            return block
        if request.trace is None:
            return CognitionMemoryResult(
                operation="upsert_trace",
                status="error",
                source_boundary=scope.memory_kind,
                payload={"error_code": "MISSING_TRACE", "detail": "No trace provided."},
            )
        if (
            scope.memory_kind == "teacher_cognition"
            and request.trace.teacher_id != scope.teacher_id
        ):
            return CognitionMemoryResult(
                operation="upsert_trace",
                status="error",
                source_boundary=scope.memory_kind,
                payload={
                    "error_code": "TEACHER_ID_MISMATCH",
                    "detail": (
                        f"Trace teacher_id '{request.trace.teacher_id}' "
                        f"does not match scope teacher_id '{scope.teacher_id}'."
                    ),
                },
            )
        self.trace_repo.upsert(request.trace)
        return CognitionMemoryResult(
            operation="upsert_trace",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            write_effect="trace_upserted",
            payload={"trace_id": request.trace.trace_id},
        )

    def _validate_and_list_traces(self, scope: CognitionMemoryScope) -> CognitionMemoryResult:
        read_block = self._check_teacher_cognition_read(scope)
        if read_block is not None:
            read_block.operation = "list_traces"
            return read_block
        all_traces = self.trace_repo.list_all()
        if scope.memory_kind == "teacher_cognition" and scope.teacher_id:
            filtered = [t for t in all_traces if t.teacher_id == scope.teacher_id]
        else:
            filtered = all_traces
        return CognitionMemoryResult(
            operation="list_traces",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            payload={"traces": filtered},
        )

    def _validate_and_upsert_pattern(
        self, request: CognitionMemoryRequest, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult:
        block = self._check_teacher_cognition_write(scope, "pattern")
        if block is not None:
            block.operation = "upsert_pattern"
            return block
        if request.pattern is None:
            return CognitionMemoryResult(
                operation="upsert_pattern",
                status="error",
                source_boundary=scope.memory_kind,
                payload={"error_code": "MISSING_PATTERN", "detail": "No pattern provided."},
            )
        if (
            scope.memory_kind == "teacher_cognition"
            and request.pattern.teacher_id != scope.teacher_id
        ):
            return CognitionMemoryResult(
                operation="upsert_pattern",
                status="error",
                source_boundary=scope.memory_kind,
                payload={
                    "error_code": "TEACHER_ID_MISMATCH",
                    "detail": (
                        f"Pattern teacher_id '{request.pattern.teacher_id}' "
                        f"does not match scope teacher_id '{scope.teacher_id}'."
                    ),
                },
            )
        self.pattern_repo.upsert(request.pattern)
        return CognitionMemoryResult(
            operation="upsert_pattern",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            write_effect="pattern_upserted",
            payload={"pattern_id": request.pattern.pattern_id},
        )

    def _validate_and_list_patterns(self, scope: CognitionMemoryScope) -> CognitionMemoryResult:
        read_block = self._check_teacher_cognition_read(scope)
        if read_block is not None:
            read_block.operation = "list_patterns"
            return read_block
        all_patterns = self.pattern_repo.list_all()
        if scope.memory_kind == "teacher_cognition" and scope.teacher_id:
            filtered = [p for p in all_patterns if p.teacher_id == scope.teacher_id]
        else:
            filtered = all_patterns
        return CognitionMemoryResult(
            operation="list_patterns",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            payload={"patterns": filtered},
        )

    def _validate_and_upsert_persona(
        self, request: CognitionMemoryRequest, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult:
        block = self._check_teacher_cognition_write(scope, "persona")
        if block is not None:
            block.operation = "upsert_persona"
            return block
        if request.persona is None:
            return CognitionMemoryResult(
                operation="upsert_persona",
                status="error",
                source_boundary=scope.memory_kind,
                payload={"error_code": "MISSING_PERSONA", "detail": "No persona provided."},
            )
        if (
            scope.memory_kind == "teacher_cognition"
            and request.persona.teacher_id != scope.teacher_id
        ):
            return CognitionMemoryResult(
                operation="upsert_persona",
                status="error",
                source_boundary=scope.memory_kind,
                payload={
                    "error_code": "TEACHER_ID_MISMATCH",
                    "detail": (
                        f"Persona teacher_id '{request.persona.teacher_id}' "
                        f"does not match scope teacher_id '{scope.teacher_id}'."
                    ),
                },
            )
        self.persona_repo.upsert(request.persona)
        return CognitionMemoryResult(
            operation="upsert_persona",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            write_effect="persona_upserted",
            payload={"persona_id": request.persona.persona_id},
        )

    def _validate_and_list_personas(self, scope: CognitionMemoryScope) -> CognitionMemoryResult:
        read_block = self._check_teacher_cognition_read(scope)
        if read_block is not None:
            read_block.operation = "list_personas"
            return read_block
        all_personas = self.persona_repo.list_all()
        if scope.memory_kind == "teacher_cognition" and scope.teacher_id:
            filtered = [p for p in all_personas if p.teacher_id == scope.teacher_id]
        else:
            filtered = all_personas
        return CognitionMemoryResult(
            operation="list_personas",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            payload={"personas": filtered},
        )

    def _validate_and_upsert_analysis(
        self, request: CognitionMemoryRequest, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult:
        if request.analysis is None:
            return CognitionMemoryResult(
                operation="upsert_analysis",
                status="error",
                source_boundary=scope.memory_kind,
                payload={"error_code": "MISSING_ANALYSIS", "detail": "No analysis provided."},
            )
        self.analysis_repo.upsert(request.analysis)
        return CognitionMemoryResult(
            operation="upsert_analysis",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            write_effect="analysis_upserted",
            payload={"analysis_id": request.analysis.analysis_id},
        )

    # ── feedback operations ─────────────────────────────────────────────

    def _validate_and_record_feedback(
        self, request: CognitionMemoryRequest, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult:
        if request.feedback is None:
            return CognitionMemoryResult(
                operation="record_feedback",
                status="error",
                source_boundary=scope.memory_kind,
                payload={"error_code": "MISSING_FEEDBACK", "detail": "No feedback provided."},
            )
        if scope.memory_kind == "agent_private" and not scope.agent_id:
            return CognitionMemoryResult(
                operation="record_feedback",
                status="error",
                source_boundary="agent_private",
                payload={
                    "error_code": "MISSING_AGENT_ID",
                    "detail": "agent_private scope requires agent_id.",
                },
            )
        if scope.memory_kind == "teacher_cognition" and not scope.teacher_id:
            return self._missing_teacher_result(
                operation="record_feedback",
                entity_label="feedback",
            )
        scoped_feedback = replace(
            request.feedback,
            scope_kind=scope.memory_kind,
            teacher_id=scope.teacher_id,
            agent_id=scope.agent_id,
        )
        self.feedback_repo.upsert(scoped_feedback)
        return CognitionMemoryResult(
            operation="record_feedback",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            write_effect="feedback_recorded",
            payload={"feedback": scoped_feedback, "feedback_id": scoped_feedback.feedback_id},
        )

    def _validate_and_list_feedback(self, scope: CognitionMemoryScope) -> CognitionMemoryResult:
        if scope.memory_kind == "agent_private" and not scope.agent_id:
            return CognitionMemoryResult(
                operation="list_feedback",
                status="error",
                source_boundary="agent_private",
                payload={
                    "error_code": "MISSING_AGENT_ID",
                    "detail": "agent_private scope requires agent_id.",
                },
            )
        if scope.memory_kind == "teacher_cognition" and not scope.teacher_id:
            return self._missing_teacher_result(
                operation="list_feedback",
                entity_label="feedback",
            )
        if scope.memory_kind in ("external_evidence", "shared_reference"):
            return CognitionMemoryResult(
                operation="list_feedback",
                status="error",
                source_boundary=scope.memory_kind,
                payload={
                    "error_code": f"{scope.memory_kind.upper()}_CANNOT_LIST_FEEDBACK",
                    "detail": (
                        f"{scope.memory_kind} scope cannot list feedback. "
                        "Feedback is scoped to teacher_cognition or agent_private."
                    ),
                },
            )
        all_feedback = self.feedback_repo.list_all()
        if scope.memory_kind == "teacher_cognition":
            filtered = [
                f
                for f in all_feedback
                if f.scope_kind in ("", "teacher_cognition")
                and (not scope.teacher_id or f.teacher_id in ("", scope.teacher_id))
            ]
        elif scope.memory_kind == "agent_private":
            filtered = [
                f
                for f in all_feedback
                if f.scope_kind == "agent_private" and f.agent_id == scope.agent_id
            ]
        else:
            filtered = all_feedback
        return CognitionMemoryResult(
            operation="list_feedback",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            payload={"feedbacks": filtered},
        )

    # ── trace verification operations ───────────────────────────────────

    def _check_trace_verification_write(
        self, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult | None:
        """Trace verification is teacher cognition quality record — teacher scope only."""
        if scope.memory_kind != "teacher_cognition":
            return CognitionMemoryResult(
                operation="",
                status="error",
                source_boundary=scope.memory_kind,
                payload={
                    "error_code": "EXTERNAL_SCOPE_CANNOT_WRITE_TRACE_VERIFICATION",
                    "detail": (
                        f"{scope.memory_kind} scope cannot write trace verification. "
                        "Trace verification is teacher cognition quality record."
                    ),
                },
            )
        if not scope.teacher_id:
            return self._missing_teacher_result(
                operation="",
                entity_label="trace_verification",
            )
        if scope.teacher_id == "*":
            return CognitionMemoryResult(
                operation="",
                status="error",
                source_boundary="teacher_cognition",
                payload={
                    "error_code": "WILDCARD_TEACHER_ID_NOT_ALLOWED_FOR_WRITE",
                    "detail": (
                        "Wildcard teacher_id '*' is only allowed for read/select "
                        "trace quality operations, not for upsert."
                    ),
                },
            )
        return None

    def _validate_and_upsert_trace_verification(
        self, request: CognitionMemoryRequest, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult:
        block = self._check_trace_verification_write(scope)
        if block is not None:
            block.operation = "upsert_trace_verification"
            return block
        if request.trace_verification is None:
            return CognitionMemoryResult(
                operation="upsert_trace_verification",
                status="error",
                source_boundary=scope.memory_kind,
                payload={
                    "error_code": "MISSING_TRACE_VERIFICATION",
                    "detail": "No trace verification provided.",
                },
            )
        if request.trace_verification.teacher_id != scope.teacher_id:
            return CognitionMemoryResult(
                operation="upsert_trace_verification",
                status="error",
                source_boundary=scope.memory_kind,
                payload={
                    "error_code": "TEACHER_ID_MISMATCH",
                    "detail": (
                        f"Verification teacher_id '{request.trace_verification.teacher_id}' "
                        f"does not match scope teacher_id '{scope.teacher_id}'."
                    ),
                },
            )
        self.trace_verification_repo.upsert(request.trace_verification)
        return CognitionMemoryResult(
            operation="upsert_trace_verification",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            write_effect="trace_verification_upserted",
            payload={"verification_id": request.trace_verification.verification_id},
        )

    def _validate_and_list_trace_verifications(
        self, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult:
        read_block = self._check_teacher_cognition_read(scope)
        if read_block is not None:
            read_block.operation = "list_trace_verifications"
            return read_block
        all_verifications = self.trace_verification_repo.list_all()
        # "*" means all teachers — skip teacher_id filtering.
        if (
            scope.memory_kind == "teacher_cognition"
            and scope.teacher_id
            and scope.teacher_id != "*"
        ):
            filtered = [v for v in all_verifications if v.teacher_id == scope.teacher_id]
        else:
            filtered = all_verifications
        return CognitionMemoryResult(
            operation="list_trace_verifications",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            payload={"verifications": filtered},
        )

    def _validate_and_select_low_confidence_traces(
        self, request: CognitionMemoryRequest, scope: CognitionMemoryScope
    ) -> CognitionMemoryResult:
        read_block = self._check_teacher_cognition_read(scope)
        if read_block is not None:
            read_block.operation = "select_low_confidence_traces"
            return read_block
        all_traces = self.trace_repo.list_all()
        # "*" means all teachers — pass None to skip teacher_id filtering.
        effective_teacher_id: str | None = (
            None if scope.teacher_id == "*" else (scope.teacher_id or None)
        )
        selected = select_low_confidence_traces(
            all_traces,
            threshold=request.threshold,
            limit=request.limit,
            teacher_id=effective_teacher_id,
        )
        return CognitionMemoryResult(
            operation="select_low_confidence_traces",
            status="success",
            source_boundary=self._resolve_boundary(scope),
            payload={"traces": selected},
        )
