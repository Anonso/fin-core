"""Public contracts for the ZSXQ scraper module (Gate 1).

Only run/health contracts live here in Gate 1. Notification, cursor, coverage,
artifact, projection and committed-reader contracts are deliberately NOT
pre-built — they are added in later gates when a failing test requires them.

All contracts are JSON-safe (``to_dict`` returns primitives / lists / dicts)
and the enums are stable so external consumers can depend on the string values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 4

#: Health wire contract version — independent from SQLite storage schema version.
HEALTH_CONTRACT_VERSION = 1


#: The frozen narrow failure-reason allowlist carried on a FAILED
#: :class:`ZsxqRunResult`. Classified kinds outside this closed set degrade to
#: ``"unknown"`` at the module boundary, so the wire contract never leaks a
#: new un-contracted value.
FAILURE_REASON_ALLOWLIST: frozenset[str] = frozenset(
    {
        "bridge_start_failed",
        "target_invalid",
        "transport_unavailable",
        "login_required",
        "content_insufficient",
        "window_coverage_incomplete",
        "unknown",
    }
)


class ZsxqRunStatus(StrEnum):
    """Stable terminal / control statuses for a scraper run.

    ``COALESCED`` is not a run of its own — it reports that a concurrent
    trigger joined the already-active run. ``INTERRUPTED`` marks a run whose
    owner died and was reclaimed by stale recovery.
    """

    NO_CHANGE = "no_change"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    COALESCED = "coalesced"
    INTERRUPTED = "interrupted"


#: Statuses that mean a run has reached a terminal state (lease released).
TERMINAL_RUN_STATUSES = frozenset(
    {
        ZsxqRunStatus.NO_CHANGE.value,
        ZsxqRunStatus.SUCCEEDED.value,
        ZsxqRunStatus.PARTIAL.value,
        ZsxqRunStatus.FAILED.value,
        ZsxqRunStatus.DEADLINE_EXCEEDED.value,
        ZsxqRunStatus.INTERRUPTED.value,
    }
)


class ZsxqHealthState(StrEnum):
    """Coarse health state derived from the latest persisted observation.

    UNKNOWN / IDLE / BUSY are retained for backward compatibility with
    run-only callers.  The four new states are projected from PageState
    observations: ready → healthy, login_required / challenge →
    requires_user, rate_limited / loading_timeout / dom_changed /
    wrong_page → degraded, control_failure → unavailable.
    """

    UNKNOWN = "unknown"
    IDLE = "idle"
    BUSY = "busy"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    REQUIRES_USER = "requires_user"
    UNAVAILABLE = "unavailable"


class ZsxqRunIntent(StrEnum):
    """What kind of reconciliation a run performs.

    ``intent`` alone selects the adapter surface: ``sync`` drives the rolling
    3-day incremental group scan; ``watch`` drives the lightweight priority scan.
    It is deliberately independent of ``trigger`` (who/why started the run), so
    routing can never be smuggled through a caller-chosen mode string.
    """

    SYNC = "sync"
    WATCH = "watch"


class ZsxqRunTrigger(StrEnum):
    """Who/why a run was started, recorded separately from ``intent``.

    ``schedule`` is an automated timer, ``manual`` a human/operator request and
    ``recovery`` a reconciliation kicked off by stale-lease recovery. Triggers
    never influence adapter routing.
    """

    SCHEDULE = "schedule"
    MANUAL = "manual"
    RECOVERY = "recovery"


_VALID_INTENTS = frozenset(i.value for i in ZsxqRunIntent)
_VALID_TRIGGERS = frozenset(t.value for t in ZsxqRunTrigger)


@dataclass
class ZsxqRunRequest:
    """A request to run the scraper.

    ``intent`` (``sync`` | ``watch``) selects the adapter surface and is the ONLY
    routing signal. ``trigger`` (``schedule`` | ``manual`` | ``recovery``) records
    who/why the run started and is persisted separately; it never affects routing.

    There is deliberately no combined/legacy trigger encoding, no arbitrary mode,
    and NO 30-day or window-override field: the production rolling 3-day
    incremental policy is owned by the adapter and must never be overridable
    through the public request. Invalid values fail closed at construction rather
    than being silently coerced to a default.
    """

    intent: str = ZsxqRunIntent.SYNC.value
    trigger: str = ZsxqRunTrigger.SCHEDULE.value
    deadline_seconds: float = 120.0
    request_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if self.intent not in _VALID_INTENTS:
            raise ValueError(
                f"ZsxqRunRequest.intent must be one of {sorted(_VALID_INTENTS)}, "
                f"got {self.intent!r}"
            )
        if self.trigger not in _VALID_TRIGGERS:
            raise ValueError(
                f"ZsxqRunRequest.trigger must be one of {sorted(_VALID_TRIGGERS)}, "
                f"got {self.trigger!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "trigger": self.trigger,
            "deadline_seconds": self.deadline_seconds,
            "request_id": self.request_id,
        }


@dataclass
class ZsxqRunResult:
    """The outcome of :meth:`ZsxqScraperModule.run`.

    Run-scoped fields are ``None`` when they do not apply, and ``to_dict`` omits
    any ``None`` field so the wire form never carries pseudo-values. A terminal
    run carries ``run_id`` but no ``active_run_id``; a COALESCED result carries
    only ``active_run_id`` (the run it joined) and fabricates no run id,
    timestamps, attempt or changed-count history.
    """

    status: str
    request_id: str = ""
    intent: str = ""
    trigger: str = ""
    coalesced: bool = False
    run_id: str | None = None
    active_run_id: str | None = None
    changed_count: int | None = None
    attempt: int | None = None
    started_at: str | None = None
    finished_at: str | None = None

    #: Narrow classified cause of a FAILED run (wire-only, never persisted):
    #: ``bridge_start_failed`` / ``target_invalid`` / ``transport_unavailable`` /
    #: ``login_required`` / ``content_insufficient`` / ``window_coverage_incomplete`` /
    #: ``unknown``. ``None`` when the run did not fail on an adapter exception
    #: (e.g. NO_CHANGE / SUCCEEDED / DEADLINE_EXCEEDED / COALESCED), in which case
    #: the wire form omits it.
    failure_reason: str | None = None

    #: Run-scoped fields omitted from the wire form when unset (None).
    _OPTIONAL_FIELDS = (
        "run_id",
        "active_run_id",
        "changed_count",
        "attempt",
        "started_at",
        "finished_at",
        "failure_reason",
    )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.status,
            "request_id": self.request_id,
            "intent": self.intent,
            "trigger": self.trigger,
            "coalesced": self.coalesced,
        }
        for name in self._OPTIONAL_FIELDS:
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data


@dataclass
class ZsxqHealthRequest:
    """A request to read scraper health.

    ``probe=False`` reads only the ledger and never touches Chrome or creates a
    run. The live probe path (``probe=True``) acquires a probe lease, calls the
    adapter's ``probe_page`` and records a health observation.

    ``deadline_at`` carries an exact absolute probe deadline when supplied by a
    production entrypoint. Otherwise ``deadline_seconds`` caps the wall-clock
    budget relative to the module clock (default 30 s).
    """

    probe: bool = False
    deadline_seconds: float = 30.0
    deadline_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "probe": self.probe,
            "deadline_seconds": self.deadline_seconds,
        }
        if self.deadline_at is not None:
            data["deadline_at"] = self.deadline_at.isoformat()
        return data


@dataclass
class ZsxqHealth:
    """Health snapshot projected from the latest persisted observation.

    Existing run-only fields are retained.  The observation-derived fields
    (``page_state``, ``reason_code``, ``health_episode_id``) come from the
    latest ``health_observations`` row; ``observed_at`` is the source
    observation timestamp and ``evaluated_at`` is the health query time.
    """

    state: str = ZsxqHealthState.UNKNOWN.value
    # ── retained run-level fields ──────────────────────────────────
    last_run_id: str = ""
    last_status: str = ""
    last_finished_at: str = ""
    # ── observation projection ─────────────────────────────────────
    observed_at: str = ""
    evaluated_at: str = ""
    page_state: str = ""
    reason_code: str = ""
    requires_user_action: bool = False
    active_run_id: str | None = None
    health_episode_id: str | None = None
    # ── versioning ─────────────────────────────────────────────────
    schema_version: int = HEALTH_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "last_run_id": self.last_run_id,
            "last_status": self.last_status,
            "last_finished_at": self.last_finished_at,
            "observed_at": self.observed_at,
            "evaluated_at": self.evaluated_at,
            "page_state": self.page_state,
            "reason_code": self.reason_code,
            "requires_user_action": self.requires_user_action,
            "active_run_id": self.active_run_id,
            "health_episode_id": self.health_episode_id,
            "schema_version": self.schema_version,
        }


@dataclass
class ReconcileOutcome:
    """Result of one reconciliation pass returned by the injected adapter.

    Gate 1 uses an empty reconciliation (``changed_count=0``) from a fake
    adapter. The real CDP adapter (Gate 2) returns the same shape.
    """

    changed_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_count": self.changed_count,
            "warnings": list(self.warnings),
        }
