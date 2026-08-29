"""The ZSXQ scraper module public facade (Gate 1).

This is the single public seam for the scraper. Gate 1 exposes exactly two
working use cases:

- :meth:`run` — acquire the single cross-process lease (or coalesce onto the
  active run), reconcile once through the injected adapter, then persist a
  terminal status and release the lease.
- :meth:`health` — a ledger-derived health snapshot; Gate 1 never probes
  Chrome and never creates a run.

There is deliberately NO ``read_committed`` here — not even a placeholder. The
committed reader arrives in Gate 5 driven by its own failing test. Cursor,
coverage, artifact, projection, notification and paging state are likewise not
pre-built.

Synchronous reconciliation does not use a persistent job queue: a run is a
single in-process call guarded by the SQLite lease.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .cdp_diagnostics import classify_cdp_error
from .contracts import (
    FAILURE_REASON_ALLOWLIST,
    ReconcileOutcome,
    ZsxqHealth,
    ZsxqHealthRequest,
    ZsxqHealthState,
    ZsxqRunIntent,
    ZsxqRunRequest,
    ZsxqRunResult,
    ZsxqRunStatus,
)
from .page_assessment import PageAssessment, PageState
from .runtime_repository import LeaseLostError, ScraperRuntimeRepository

#: Map the eight stable PageState values to the four coarse health states.
_PAGE_STATE_TO_HEALTH: dict[str, str] = {
    "ready": ZsxqHealthState.HEALTHY.value,
    "login_required": ZsxqHealthState.REQUIRES_USER.value,
    "challenge": ZsxqHealthState.REQUIRES_USER.value,
    "rate_limited": ZsxqHealthState.DEGRADED.value,
    "loading_timeout": ZsxqHealthState.DEGRADED.value,
    "dom_changed": ZsxqHealthState.DEGRADED.value,
    "wrong_page": ZsxqHealthState.DEGRADED.value,
    "control_failure": ZsxqHealthState.UNAVAILABLE.value,
}


def _coerce_failure_reason(value: str) -> str:
    """Restrict a classified kind to the frozen wire allowlist (fallback unknown)."""
    return value if value in FAILURE_REASON_ALLOWLIST else "unknown"


def _page_state_to_health(page_state: str) -> str:
    """Project a PageState string to its coarse health status.

    Unknown values fall back to UNKNOWN so a future PageState addition
    cannot crash the health read before the mapping is updated.
    """
    return _PAGE_STATE_TO_HEALTH.get(page_state, ZsxqHealthState.UNKNOWN.value)


class _DeadlineReachedError(Exception):
    """Internal signal: a checkpoint rejected further work past the total deadline."""


class ReconcileAdapter(Protocol):
    """The reconciliation seam the module drives during a run.

    Gate 1 injects a fake. Gate 2 supplies the production Windows Chrome CDP
    adapter, which delegates to the existing rolling 3-day CDP scraper.

    ``checkpoint`` is a cooperative heartbeat/deadline callback the adapter must
    call at its existing bounded scraping checkpoints. Each call rejects an
    expired total deadline before more work (raising out of the adapter) and
    otherwise renews the lease; a fenced owner's renewal fails visibly.

    ``probe_page`` is the live health-probe seam. Gate 2B supplies a fake
    assessment; the production adapter raises ``NotImplementedError``.
    """

    def run_incremental(
        self, *, mode: str, deadline_at: datetime, checkpoint: Callable[[], None]
    ) -> ReconcileOutcome: ...

    def probe_page(self, *, deadline_at: datetime) -> PageAssessment: ...


class ZsxqScraperModule:
    """Public facade over the control ledger and reconciliation adapter."""

    def __init__(
        self,
        *,
        repository: ScraperRuntimeRepository,
        adapter: ReconcileAdapter,
        clock: Callable[[], datetime] | None = None,
        stale_after_seconds: float = 120.0,
    ) -> None:
        self._repo = repository
        self._adapter = adapter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stale_after_seconds = stale_after_seconds

    # ── public use cases ─────────────────────────────────────────

    def run(self, request: ZsxqRunRequest | None = None) -> ZsxqRunResult:
        """Run the scraper once, or coalesce onto an already-active run."""
        return self._run(request or ZsxqRunRequest(), capture_identity=None)

    def _run_capture(
        self,
        request: ZsxqRunRequest,
        *,
        artifact_run_id: str,
        content_sha256: str,
    ) -> ZsxqRunResult:
        """Package-owned capture path with atomic business-terminal binding."""
        return self._run(
            request,
            capture_identity=(artifact_run_id, content_sha256),
        )

    def _run(
        self,
        request: ZsxqRunRequest,
        *,
        capture_identity: tuple[str, str] | None,
    ) -> ZsxqRunResult:
        request = request or ZsxqRunRequest()
        now = self._clock()
        deadline_at = now + timedelta(seconds=request.deadline_seconds)
        stale_before = now - timedelta(seconds=self._stale_after_seconds)

        acquisition = self._repo.acquire_or_coalesce(
            intent=request.intent,
            trigger=request.trigger,
            now=now,
            deadline_at=deadline_at,
            stale_before=stale_before,
        )

        if not acquisition.acquired:
            # Concurrent trigger: report only the active run id (which is None when
            # the active owner is a probe lease). Fabricate no run id, timestamps,
            # attempt or changed-count history.
            return ZsxqRunResult(
                status=ZsxqRunStatus.COALESCED.value,
                request_id=request.request_id,
                intent=request.intent,
                trigger=request.trigger,
                coalesced=True,
                active_run_id=acquisition.active_run_id or None,
            )

        run_id = acquisition.run_id
        owner_token = acquisition.owner_token
        checkpoint = self._make_checkpoint(run_id, owner_token, deadline_at)
        try:
            outcome = self._adapter.run_incremental(
                mode=request.intent,
                deadline_at=deadline_at,
                checkpoint=checkpoint,
            )
        except _DeadlineReachedError:
            finished_at = self._clock()
            return self._finalize(
                request,
                acquisition,
                ZsxqRunStatus.DEADLINE_EXCEEDED.value,
                0,
                finished_at,
                capture_identity=capture_identity,
            )
        except LeaseLostError:
            # We were fenced mid-run (ownership reclaimed). Fail visibly and do
            # NOT write — the reclaimer already marked this run interrupted.
            raise
        except Exception as exc:
            # An adapter failure raised past the total deadline is a deadline
            # outcome, not a fresh FAILED — terminalize truthfully so the
            # repository's deadline fence accepts it. Sample the completion clock
            # exactly once and use that value for both the decision and the write.
            finished_at = self._clock()
            status = (
                ZsxqRunStatus.DEADLINE_EXCEEDED.value
                if finished_at >= deadline_at
                else ZsxqRunStatus.FAILED.value
            )
            # The deadline itself is the cause; only a FAILED run carries a narrow
            # classified reason (bridge_start_failed / target_invalid / ...). The
            # reason is wire-only — the ledger run row keeps just the status.
            # Kinds outside the frozen allowlist degrade to "unknown" so the
            # wire contract stays a closed set.
            failure_reason = (
                None
                if status == ZsxqRunStatus.DEADLINE_EXCEEDED.value
                else _coerce_failure_reason(classify_cdp_error(str(exc)).value)
            )
            return self._finalize(
                request,
                acquisition,
                status,
                0,
                finished_at,
                failure_reason=failure_reason,
                capture_identity=capture_identity,
            )

        finished_at = self._clock()
        status = self._terminal_status(outcome, deadline_at, finished_at)
        return self._finalize(
            request,
            acquisition,
            status,
            outcome.changed_count,
            finished_at,
            capture_identity=capture_identity,
        )

    def health(self, request: ZsxqHealthRequest | None = None) -> ZsxqHealth:
        """Project health from the latest persisted observation, or run a live probe.

        ``probe=False`` (default) is a pure ledger read — never touches the adapter
        or Chrome.  ``probe=True`` acquires a probe lease, calls the adapter's
        ``probe_page``, records a health observation and returns the projection.
        """
        request = request or ZsxqHealthRequest()
        if request.probe:
            return self._health_probe(request)
        return self._health_read()

    # ── internals ────────────────────────────────────────────────

    def _health_read(self) -> ZsxqHealth:
        """Pure ledger projection (probe=False).  Never touches the adapter or Chrome."""
        now = self._clock()
        evaluated_at = now.isoformat()

        # Always read the latest terminal run so run fields are additive
        # on every result path — an observation controls health state but
        # coexists with last_run_id / last_status / last_finished_at.
        latest_run = self._repo.latest_terminal_run()
        last_run_id = latest_run["run_id"] if latest_run else ""
        last_status = latest_run["status"] if latest_run else ""
        last_finished_at = latest_run.get("finished_at") or "" if latest_run else ""

        lease = self._repo.get_active_lease()
        active_run_id: str | None = lease["run_id"] if lease is not None else None
        lease_active = lease is not None

        latest = self._repo.latest_actual_observation()
        if (
            latest is not None
            and latest["intent"] == ZsxqRunIntent.WATCH.value
            and latest["state"] == PageState.ready.value
        ):
            latest_sync = self._repo.latest_actual_observation(intent=ZsxqRunIntent.SYNC.value)
            if latest_sync is not None and latest_sync["state"] != PageState.ready.value:
                latest = latest_sync

        if latest is not None:
            page_state = latest["state"]
            health_state = _page_state_to_health(page_state)
            reason_code = latest.get("reason_code") or ""
            requires_user = health_state == ZsxqHealthState.REQUIRES_USER.value

            # When a run or probe holds the lease, the coarse state is always
            # BUSY regardless of the observation's derived state — the system
            # is actively working.  The observation data is still projected,
            # and requires_user_action / episode are preserved.
            if lease_active:
                return ZsxqHealth(
                    state=ZsxqHealthState.BUSY.value,
                    last_run_id=last_run_id,
                    last_status=last_status,
                    last_finished_at=last_finished_at,
                    page_state=page_state,
                    reason_code=reason_code,
                    observed_at=latest["observed_at"],
                    evaluated_at=evaluated_at,
                    active_run_id=active_run_id,
                    requires_user_action=requires_user,
                    health_episode_id=latest.get("episode_id"),
                )

            return ZsxqHealth(
                state=health_state,
                last_run_id=last_run_id,
                last_status=last_status,
                last_finished_at=last_finished_at,
                page_state=page_state,
                reason_code=reason_code,
                observed_at=latest["observed_at"],
                evaluated_at=evaluated_at,
                active_run_id=None,
                requires_user_action=requires_user,
                health_episode_id=latest.get("episode_id"),
            )

        # Any active lease (run or probe) without observations → BUSY.
        if lease_active:
            return ZsxqHealth(
                state=ZsxqHealthState.BUSY.value,
                last_run_id=last_run_id,
                last_status=last_status,
                last_finished_at=last_finished_at,
                evaluated_at=evaluated_at,
                active_run_id=active_run_id,
            )

        if latest_run is None:
            return ZsxqHealth(
                state=ZsxqHealthState.UNKNOWN.value,
                last_run_id=last_run_id,
                last_status=last_status,
                last_finished_at=last_finished_at,
                evaluated_at=evaluated_at,
            )

        return ZsxqHealth(
            state=ZsxqHealthState.IDLE.value,
            last_run_id=last_run_id,
            last_status=last_status,
            last_finished_at=last_finished_at,
            evaluated_at=evaluated_at,
        )

    def _health_probe(self, request: ZsxqHealthRequest) -> ZsxqHealth:
        """Live probe lifecycle (probe=True).

        Acquires a probe lease, calls ``adapter.probe_page``, records a health
        observation through the existing UoW, releases the lease and returns the
        pure ledger projection.  A busy owner returns BUSY without touching the
        adapter.  The probe lease is always released in a ``finally`` block;
        adapter errors and repository ``LeaseLostError`` propagate after the
        release attempt without writing any observation/episode/outbox/run.
        """
        now = self._clock()
        deadline_at = request.deadline_at or now + timedelta(seconds=request.deadline_seconds)
        if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
            raise ValueError("probe deadline_at must be timezone-aware")
        if deadline_at <= now:
            raise ValueError("probe deadline_at must be in the future")
        stale_before = now - timedelta(seconds=self._stale_after_seconds)

        probe = self._repo.acquire_probe_lease(
            now=now, deadline_at=deadline_at, stale_before=stale_before
        )

        if not probe.acquired:
            # A fresh active run or probe already holds the lease → BUSY.
            # Call no adapter, write no table, release no foreign lease.
            return self._health_read()

        owner_token = probe.owner_token
        try:
            assessment = self._adapter.probe_page(deadline_at=deadline_at)
            post_clock = self._clock()
            self._repo.record_probe_observation(
                owner_token=owner_token,
                intent="watch",
                surface="timeline",
                state=assessment.state,
                reason_code=assessment.reason_code,
                evidence_ref=assessment.evidence_fingerprint,
                observed_at=post_clock,
                recorded_at=post_clock,
            )
        finally:
            self._repo.release_probe_lease(owner_token=owner_token)
        return self._health_read()

    def _make_checkpoint(
        self, run_id: str, owner_token: str, deadline_at: datetime
    ) -> Callable[[], None]:
        """Build the cooperative heartbeat/deadline callback for one run.

        Each call rejects an expired total deadline before more work, otherwise
        renews the lease. ``repo.heartbeat`` raises :class:`LeaseLostError` if this
        owner was fenced, propagating out of the adapter to fail visibly.
        """

        def checkpoint() -> None:
            now = self._clock()
            if now >= deadline_at:
                raise _DeadlineReachedError()
            self._repo.heartbeat(run_id=run_id, owner_token=owner_token, at=now)

        return checkpoint

    def _finalize(
        self,
        request: ZsxqRunRequest,
        acquisition,
        status: str,
        changed_count: int,
        finished_at: datetime,
        *,
        failure_reason: str | None = None,
        capture_identity: tuple[str, str] | None = None,
    ) -> ZsxqRunResult:
        """Persist the terminal status, release the lease, build the wire result.

        ``finished_at`` is the single completion clock sample taken by the caller;
        this method never reads the clock, so the status decision, the persisted
        row and the wire result all report the exact same instant.
        """
        result = ZsxqRunResult(
            status=status,
            request_id=request.request_id,
            intent=request.intent,
            trigger=request.trigger,
            run_id=acquisition.run_id,
            changed_count=changed_count,
            attempt=acquisition.attempt,
            started_at=acquisition.started_at,
            finished_at=finished_at.isoformat(),
            failure_reason=failure_reason,
        )
        if capture_identity is None:
            self._repo.finish_run(
                run_id=acquisition.run_id,
                owner_token=acquisition.owner_token,
                status=status,
                changed_count=changed_count,
                finished_at=finished_at,
            )
        else:
            self._repo.finish_capture_business(
                run_id=acquisition.run_id,
                owner_token=acquisition.owner_token,
                status=status,
                changed_count=changed_count,
                finished_at=finished_at,
                artifact_run_id=capture_identity[0],
                content_sha256=capture_identity[1],
                business_json=json.dumps(
                    result.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        return result

    def _terminal_status(
        self, outcome: ReconcileOutcome, deadline_at: datetime, finished_at: datetime
    ) -> str:
        if finished_at >= deadline_at:
            return ZsxqRunStatus.DEADLINE_EXCEEDED.value
        if outcome.changed_count > 0:
            return ZsxqRunStatus.SUCCEEDED.value
        return ZsxqRunStatus.NO_CHANGE.value
