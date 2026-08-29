"""Typed, bounded user investment-memory values.

The SQLite owner remains :class:`ResearchStateRepository`.  These immutable
values deliberately carry references and user-stated facts only; they never
own an analysis body, account facts, Hermes transcript, or Agent session.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

InvestmentMemoryEventKind = Literal[
    "USER_DECISION",
    "USER_REPORTED_EXECUTION",
    "OUTCOME_OBSERVATION",
    "OUTCOME_JUDGMENT",
]
InvestmentMemoryDecision = Literal["ACCEPT", "REJECT", "WAIT", "CHANGE_PLAN"]
InvestmentMemoryEventState = Literal["ACTIVE", "SUPERSEDED", "TOMBSTONED", "PURGED"]

_EVENT_KINDS = frozenset(
    {
        "USER_DECISION",
        "USER_REPORTED_EXECUTION",
        "OUTCOME_OBSERVATION",
        "OUTCOME_JUDGMENT",
    }
)
_DECISIONS = frozenset({"ACCEPT", "REJECT", "WAIT", "CHANGE_PLAN"})
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVENT_ID = re.compile(r"^[0-9a-f]{32}$")
_ACTUAL_ADVISORY_SNAPSHOT_REF = re.compile(r"^actual-advisory-snapshot-[0-9a-f]{16}$")


def _text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{field} must be non-empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return normalized


def _event_ids(value: object, *, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field} must be a tuple")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} references")
    if any(not isinstance(item, str) or _EVENT_ID.fullmatch(item) is None for item in value):
        raise ValueError(f"{field} contains an invalid event_id")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must not repeat an event_id")
    return value


@dataclass(frozen=True, slots=True)
class AnalysisReference:
    """Direct pointer to an existing FIN product; no analysis body is copied."""

    chain_id: str
    product_version: int
    artifact_hash: str

    def __post_init__(self) -> None:
        _text(self.chain_id, field="analysis chain_id", maximum=128)
        if (
            not isinstance(self.product_version, int)
            or isinstance(self.product_version, bool)
            or self.product_version < 1
        ):
            raise ValueError("analysis product_version must be positive")
        if not isinstance(self.artifact_hash, str) or _SHA256.fullmatch(self.artifact_hash) is None:
            raise ValueError("analysis artifact_hash must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class AccountReference:
    """Direct pointer to a published actual-advisory account snapshot only."""

    snapshot_ref: str
    revision: str
    as_of: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot_ref, str)
            or _ACTUAL_ADVISORY_SNAPSHOT_REF.fullmatch(self.snapshot_ref) is None
        ):
            raise ValueError("account snapshot_ref must reference actual advisory state")
        if not isinstance(self.revision, str) or _SHA256.fullmatch(self.revision) is None:
            raise ValueError("account revision must be a SHA-256 digest")
        if (
            not isinstance(self.as_of, (int, float))
            or isinstance(self.as_of, bool)
            or not math.isfinite(self.as_of)
        ):
            raise ValueError("account as_of must be finite")


@dataclass(frozen=True, slots=True)
class InvestmentMemoryEventInput:
    """A user-explicit, bounded fact suitable for the one event journal."""

    kind: InvestmentMemoryEventKind
    statement: str
    decision: InvestmentMemoryDecision | None = None
    supersedes_event_id: str | None = None
    related_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _EVENT_KINDS:
            raise ValueError("investment memory event kind is invalid")
        object.__setattr__(
            self,
            "statement",
            _text(self.statement, field="investment memory statement", maximum=1_000),
        )
        if self.kind == "USER_DECISION":
            if self.decision not in _DECISIONS:
                raise ValueError("user decision event requires a decision")
        elif self.decision is not None:
            raise ValueError("only user decision events may carry a decision")
        if self.supersedes_event_id is not None and (
            not isinstance(self.supersedes_event_id, str)
            or _EVENT_ID.fullmatch(self.supersedes_event_id) is None
        ):
            raise ValueError("supersedes_event_id is invalid")
        related_event_ids = _event_ids(
            self.related_event_ids,
            field="related_event_ids",
            maximum=3,
        )
        if self.kind not in {"OUTCOME_OBSERVATION", "OUTCOME_JUDGMENT"} and related_event_ids:
            raise ValueError("only outcome events may reference prior journal events")


@dataclass(frozen=True, slots=True)
class InvestmentMemoryEvent:
    """One journal projection; statement is absent after retention purge."""

    event_id: str
    kind: InvestmentMemoryEventKind
    statement: str | None
    analysis_ref: AnalysisReference | None
    account_ref: AccountReference | None
    created_at: float
    decision: InvestmentMemoryDecision | None = None
    state: InvestmentMemoryEventState = "ACTIVE"
    related_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _EVENT_ID.fullmatch(self.event_id) is None:
            raise ValueError("investment memory event_id is invalid")
        if self.kind not in _EVENT_KINDS:
            raise ValueError("investment memory event kind is invalid")
        if self.statement is not None:
            object.__setattr__(
                self,
                "statement",
                _text(self.statement, field="investment memory statement", maximum=1_000),
            )
        elif self.state not in {"TOMBSTONED", "PURGED"}:
            raise ValueError("only deleted investment memory may omit a statement")
        if self.kind == "USER_DECISION":
            if self.decision not in _DECISIONS:
                raise ValueError("user decision event requires a decision")
        elif self.decision is not None:
            raise ValueError("only user decision events may carry a decision")
        if (
            not isinstance(self.created_at, (int, float))
            or isinstance(self.created_at, bool)
            or not math.isfinite(self.created_at)
        ):
            raise ValueError("investment memory created_at must be finite")
        if self.state not in {"ACTIVE", "SUPERSEDED", "TOMBSTONED", "PURGED"}:
            raise ValueError("investment memory event state is invalid")
        related_event_ids = _event_ids(
            self.related_event_ids,
            field="related_event_ids",
            maximum=3,
        )
        if self.kind not in {"OUTCOME_OBSERVATION", "OUTCOME_JUDGMENT"} and related_event_ids:
            raise ValueError("only outcome events may reference prior journal events")


@dataclass(frozen=True, slots=True)
class InvestmentMemoryReceipt:
    """Idempotent append result; state exposes an intentional tombstone replay."""

    event: InvestmentMemoryEvent
    state: InvestmentMemoryEventState

    @property
    def analysis_ref(self) -> AnalysisReference | None:
        return self.event.analysis_ref

    @property
    def account_ref(self) -> AccountReference | None:
        return self.event.account_ref


@dataclass(frozen=True, slots=True)
class InvestmentMemoryRecall:
    """Bounded non-evidence projection passed to a fresh consultation only."""

    schema_version: Literal["fin.investment-memory-recall/v1"]
    classification: Literal["investment_memory_not_evidence"]
    unresolved_decisions: tuple[InvestmentMemoryEvent, ...] = ()
    reported_execution: tuple[InvestmentMemoryEvent, ...] = ()
    outcomes: tuple[InvestmentMemoryEvent, ...] = ()
    account_refs: tuple[AccountReference, ...] = ()
    recent_analyses: tuple[AnalysisReference, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != "fin.investment-memory-recall/v1":
            raise ValueError("investment memory recall schema_version is invalid")
        if self.classification != "investment_memory_not_evidence":
            raise ValueError("investment memory recall classification is invalid")
        groups = (
            (self.unresolved_decisions, {"USER_DECISION"}),
            (self.reported_execution, {"USER_REPORTED_EXECUTION"}),
            (self.outcomes, {"OUTCOME_OBSERVATION", "OUTCOME_JUDGMENT"}),
        )
        event_ids: set[str] = set()
        for events, allowed_kinds in groups:
            if not isinstance(events, tuple):
                raise ValueError("investment memory recall events must be tuples")
            for event in events:
                if not isinstance(event, InvestmentMemoryEvent) or event.kind not in allowed_kinds:
                    raise ValueError("investment memory recall event group is invalid")
                if event.state != "ACTIVE":
                    raise ValueError("investment memory recall must contain active events")
                if event.event_id in event_ids:
                    raise ValueError("investment memory recall repeats an event")
                event_ids.add(event.event_id)
        if any(event.decision not in {"WAIT", "CHANGE_PLAN"} for event in self.unresolved_decisions):
            raise ValueError("investment memory recall unresolved decisions are invalid")
        event_count = (
            len(self.unresolved_decisions)
            + len(self.reported_execution)
            + len(self.outcomes)
        )
        if event_count > 8:
            raise ValueError("investment memory recall exceeds eight events")
        if len(self.account_refs) > 3 or len(self.recent_analyses) > 8:
            raise ValueError("investment memory recall is not bounded")
        if (
            not isinstance(self.account_refs, tuple)
            or not all(isinstance(item, AccountReference) for item in self.account_refs)
            or len(set(self.account_refs)) != len(self.account_refs)
        ):
            raise ValueError("investment memory recall account references are invalid")
        if (
            not isinstance(self.recent_analyses, tuple)
            or not all(isinstance(item, AnalysisReference) for item in self.recent_analyses)
            or len(set(self.recent_analyses)) != len(self.recent_analyses)
        ):
            raise ValueError("investment memory recall analysis references are invalid")

    @property
    def is_empty(self) -> bool:
        return not (
            self.unresolved_decisions
            or self.reported_execution
            or self.outcomes
            or self.account_refs
            or self.recent_analyses
        )


__all__ = [
    "AccountReference",
    "AnalysisReference",
    "InvestmentMemoryDecision",
    "InvestmentMemoryEvent",
    "InvestmentMemoryEventInput",
    "InvestmentMemoryEventKind",
    "InvestmentMemoryEventState",
    "InvestmentMemoryRecall",
    "InvestmentMemoryReceipt",
]
