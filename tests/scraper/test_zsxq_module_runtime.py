"""Gate 1 runtime tests for the ZsxqScraperModule control ledger.

These tests exercise the module's public facade (run/health) and the SQLite
control ledger (schema_version/runs/active_lease). A fake adapter is injected so
reconciliation is deterministic.

The cross-process invariants — sequential unique run ids, concurrent
one-owner/one-coalesced with exactly one run history, and owner crash followed by
stale recovery — are proven with GENUINE spawned OS processes over a single
on-disk SQLite database. They must NOT be faked with two connections in the same
interpreter. ``multiprocessing.get_context("spawn")`` starts a fresh interpreter,
so each worker is an independent process contending for the same lease file.

The heartbeat/deadline checkpoint, owner-token fencing, wire-contract and schema
invariants are deterministic and run in-process against the same on-disk ledger.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import pytest

from fin_analyse.scraper.contracts import (
    SCHEMA_VERSION,
    ReconcileOutcome,
    ZsxqRunIntent,
    ZsxqRunRequest,
    ZsxqRunStatus,
    ZsxqRunTrigger,
)
from fin_analyse.scraper.module import ZsxqScraperModule
from fin_analyse.scraper.runtime_repository import ScraperRuntimeRepository

#: A fixed, deterministic base wall-clock shared across processes so staleness
#: never depends on real time.
_BASE_ISO = "2026-07-11T09:00:00+00:00"


def _terminal_business_json(acquired, *, status: str, changed_count: int, finished_at) -> str:
    projection = {
        "attempt": acquired.attempt,
        "changed_count": changed_count,
        "coalesced": False,
        "finished_at": finished_at.isoformat(),
        "intent": ZsxqRunIntent.SYNC.value,
        "request_id": "",
        "run_id": acquired.run_id,
        "started_at": acquired.started_at,
        "status": status,
        "trigger": ZsxqRunTrigger.MANUAL.value,
    }
    if status == ZsxqRunStatus.FAILED.value:
        projection["failure_reason"] = "unknown"
    return json.dumps(projection, sort_keys=True, separators=(",", ":"))


def test_wal_setup_contention_is_bounded_and_restores_normal_busy_timeout(
    tmp_path, monkeypatch
) -> None:
    """A foreign exclusive lock cannot stretch repository startup indefinitely."""
    import fin_analyse.scraper.runtime_repository as rr

    db_path = str(tmp_path / "wal-contention.sqlite3")
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("PRAGMA journal_mode=DELETE")
    blocker.execute("CREATE TABLE contention_guard (id INTEGER PRIMARY KEY)")
    blocker.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(rr, "_WAL_SETUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(rr, "_WAL_SETUP_BUSY_SLICE_MS", 10)

    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="schema preflight did not converge"):
            ScraperRuntimeRepository(db_path)
    finally:
        elapsed = time.monotonic() - started
        blocker.rollback()
        blocker.close()

    assert elapsed < 0.25

    # A normal fresh open still gets the repository's regular 5-second
    # transaction contention policy after WAL setup has succeeded.
    clean_path = str(tmp_path / "normal-wal.sqlite3")
    repo = ScraperRuntimeRepository(clean_path)
    assert repo._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    repo.close()


def test_sqlite_extended_busy_code_is_normalized_to_primary_result_code() -> None:
    import fin_analyse.scraper.runtime_repository as rr

    error = sqlite3.OperationalError("database is busy")
    error.sqlite_errorcode = 5 | (5 << 8)

    assert rr._sqlite_base_error_code(error) == rr._SQLITE_BUSY == 5


def test_schema_preflight_retries_a_short_lived_writer(tmp_path, monkeypatch) -> None:
    """A valid writer inside the startup budget must not make repository open fail."""
    import fin_analyse.scraper.runtime_repository as rr

    db_path = str(tmp_path / "short-writer.sqlite3")
    ScraperRuntimeRepository(db_path).close()
    blocker = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(rr, "_WAL_SETUP_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(rr, "_WAL_SETUP_BUSY_SLICE_MS", 25)
    released = threading.Event()

    def release() -> None:
        blocker.rollback()
        released.set()

    timer = threading.Timer(0.15, release)
    timer.start()
    try:
        repo = ScraperRuntimeRepository(db_path)
        repo.close()
    finally:
        timer.join()
        blocker.close()

    assert released.is_set()


# ── deterministic clock ──────────────────────────────────────────────


class _MutableClock:
    """A deterministic, advanceable wall clock returning tz-aware datetimes."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime.fromisoformat(_BASE_ISO)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class _ScriptedClock:
    """A strict scripted clock returning a fixed sequence of datetimes, one per call.

    Each call returns the next scripted value; a call beyond the script raises
    ``AssertionError``. This pins down exactly how many times the module reads the
    wall clock during a run, so a forbidden second completion-time sample (the
    pre-fix bug) is caught rather than silently tolerated.
    """

    def __init__(self, values: list[datetime]) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> datetime:
        assert self.calls < len(self._values), (
            f"clock read #{self.calls + 1} exceeds the {len(self._values)} scripted values"
        )
        value = self._values[self.calls]
        self.calls += 1
        return value


# ── fake adapters (all accept the cooperative checkpoint seam) ────────


class _FakeAdapter:
    """Fake reconciliation seam that renews the lease once via ``checkpoint``."""

    def __init__(self, changed_count: int = 0) -> None:
        self.changed_count = changed_count
        self.calls = 0

    def run_incremental(self, *, mode, deadline_at, checkpoint) -> ReconcileOutcome:
        self.calls += 1
        checkpoint()
        return ReconcileOutcome(changed_count=self.changed_count)


class _CheckpointingLongAdapter:
    """A long run that heartbeats at each bounded checkpoint.

    Advances the injected clock past the stale window while renewing the lease
    every step, then invokes ``during`` (a concurrent acquirer) once.
    """

    def __init__(self, clock, *, steps, step_seconds, during_at, during) -> None:
        self._clock = clock
        self._steps = steps
        self._step_seconds = step_seconds
        self._during_at = during_at
        self._during = during

    def run_incremental(self, *, mode, deadline_at, checkpoint) -> ReconcileOutcome:
        for i in range(self._steps):
            self._clock.advance(self._step_seconds)
            checkpoint()  # renew lease; deadline not yet exceeded
            if i == self._during_at:
                self._during()
        return ReconcileOutcome(changed_count=0)


class _DeadlineOverrunAdapter:
    """Adapter that burns past the total deadline and expects the checkpoint to stop it."""

    def __init__(self, clock, *, overrun_seconds) -> None:
        self._clock = clock
        self._overrun = overrun_seconds
        self.reached_more_work = False

    def run_incremental(self, *, mode, deadline_at, checkpoint) -> ReconcileOutcome:
        checkpoint()  # first checkpoint: still within the deadline
        self._clock.advance(self._overrun)  # overrun the total deadline
        checkpoint()  # must reject before more work
        self.reached_more_work = True  # unreachable if the deadline is honoured
        return ReconcileOutcome(changed_count=5)


class _RaisingAdapter:
    """Adapter that raises mid-reconcile to prove FAILED is durable."""

    def __init__(self, message: str = "adapter blew up") -> None:
        self._message = message

    def run_incremental(self, *, mode, deadline_at, checkpoint) -> ReconcileOutcome:
        checkpoint()
        raise RuntimeError(self._message)


class _DeadlineThenRaiseAdapter:
    """Adapter that overruns the total deadline, then raises.

    It never checkpoints, so the deadline is only observable from the clock.
    Terminalization must still close as DEADLINE_EXCEEDED — the truthful state —
    not escape or claim FAILED.
    """

    def __init__(self, clock, *, overrun_seconds) -> None:
        self._clock = clock
        self._overrun = overrun_seconds

    def run_incremental(self, *, mode, deadline_at, checkpoint) -> ReconcileOutcome:
        self._clock.advance(self._overrun)  # burn past the deadline without renewing
        raise RuntimeError("adapter blew up after the deadline")


class _BlockingInProcessAdapter:
    """Runs a concurrent trigger in-process while still holding the lease."""

    def __init__(self, during) -> None:
        self._during = during
        self.concurrent_result = None

    def run_incremental(self, *, mode, deadline_at, checkpoint) -> ReconcileOutcome:
        checkpoint()
        self.concurrent_result = self._during()
        return ReconcileOutcome(changed_count=0)


class _CoordinatingAdapter:
    """Cross-process adapter: publish that we hold the lease, wait for release."""

    def __init__(self, holding_evt, release_evt, *, timeout=25.0) -> None:
        self._holding = holding_evt
        self._release = release_evt
        self._timeout = timeout

    def run_incremental(self, *, mode, deadline_at, checkpoint) -> ReconcileOutcome:
        checkpoint()
        self._holding.set()
        self._release.wait(self._timeout)
        return ReconcileOutcome(changed_count=0)


def _build_module(db_path, *, adapter=None, clock=None, stale_after_seconds=120.0):
    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(
        repository=repo,
        adapter=adapter or _FakeAdapter(),
        clock=clock or _MutableClock(),
        stale_after_seconds=stale_after_seconds,
    )
    return module, repo


# ── spawned worker entrypoints (top-level for spawn picklability) ─────


def _worker_run_once(db_path, intent, result_q):
    """Run one full module.run() in a fresh process; report status + run id."""
    try:
        clock = _MutableClock()
        module, repo = _build_module(db_path, clock=clock)
        result = module.run(ZsxqRunRequest(intent=intent, trigger=ZsxqRunTrigger.SCHEDULE.value))
        repo.close()
        result_q.put({"status": result.status, "run_id": result.run_id})
    except BaseException as exc:  # pragma: no cover - surfaced to parent as failure
        result_q.put({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        result_q.close()
        result_q.join_thread()


def _worker_owner(db_path, holding_evt, release_evt, result_q):
    """Owner process: acquire the lease and hold it while a peer contends."""
    try:
        clock = _MutableClock()
        repo = ScraperRuntimeRepository(db_path)
        adapter = _CoordinatingAdapter(holding_evt, release_evt)
        module = ZsxqScraperModule(repository=repo, adapter=adapter, clock=clock)
        result = module.run(
            ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value, trigger=ZsxqRunTrigger.SCHEDULE.value)
        )
        repo.close()
        result_q.put({"role": "owner", "status": result.status, "run_id": result.run_id})
    except BaseException as exc:  # pragma: no cover
        result_q.put({"role": "owner", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        result_q.close()
        result_q.join_thread()


def _worker_coalescer(db_path, holding_evt, release_evt, result_q):
    """Peer process: attempt a run while the owner holds the lease -> coalesce."""
    try:
        holding_evt.wait(25.0)  # wait until the owner genuinely holds the lease
        clock = _MutableClock()
        module, repo = _build_module(db_path, clock=clock)
        result = module.run(
            ZsxqRunRequest(intent=ZsxqRunIntent.WATCH.value, trigger=ZsxqRunTrigger.MANUAL.value)
        )
        repo.close()
        result_q.put(
            {
                "role": "coalescer",
                "status": result.status,
                "run_id": result.run_id,
                "active_run_id": result.active_run_id,
            }
        )
    except BaseException as exc:  # pragma: no cover
        result_q.put({"role": "coalescer", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        release_evt.set()  # let the owner finish regardless of outcome
        result_q.close()
        result_q.join_thread()


def _worker_acquire_then_crash(db_path, deadline_iso, info_q):
    """Acquire the lease in a fresh process, publish tokens, then hard-exit.

    ``os._exit`` skips all cleanup so the committed lease row is left behind with
    a never-renewed heartbeat, exactly like a crashed owner.
    """
    base = datetime.fromisoformat(_BASE_ISO)
    repo = ScraperRuntimeRepository(db_path)
    acq = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.SCHEDULE.value,
        now=base,
        deadline_at=datetime.fromisoformat(deadline_iso),
        stale_before=base,  # nothing is stale yet
    )
    info_q.put({"run_id": acq.run_id, "owner_token": acq.owner_token, "acquired": acq.acquired})
    info_q.close()
    info_q.join_thread()
    os._exit(0)


def _drain(result_q, count, timeout=30.0):
    items = []
    for _ in range(count):
        items.append(result_q.get(timeout=timeout))
    return items


def _worker_open_repo(db_path, ready_evt, result_q):
    """Open the repo in a fresh process once released; report the version-row count.

    All workers are released simultaneously so several race to initialize the
    same brand-new on-disk ledger, exercising the concurrent fresh-open path.
    """
    try:
        ready_evt.wait(25.0)
        repo = ScraperRuntimeRepository(db_path)
        rows = repo._conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        repo.close()
        result_q.put({"rows": int(rows)})
    except BaseException as exc:  # pragma: no cover - surfaced to parent as failure
        result_q.put({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        result_q.close()
        result_q.join_thread()


# ── in-process terminal / lease basics ───────────────────────────────


def test_terminal_no_change_persists_terminal_and_releases_lease(tmp_path):
    """A terminal NO_CHANGE run persists a terminal record and releases the lease."""
    db_path = str(tmp_path / "ledger.sqlite3")
    module, repo = _build_module(db_path)

    result = module.run(
        ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value, trigger=ZsxqRunTrigger.SCHEDULE.value)
    )

    assert result.status == ZsxqRunStatus.NO_CHANGE.value
    assert result.run_id
    assert result.finished_at

    stored = repo.get_run(result.run_id)
    assert stored is not None
    assert stored["status"] == ZsxqRunStatus.NO_CHANGE.value
    assert repo.get_active_lease() is None


# ── GENUINE cross-process invariants (spawned OS processes) ───────────


def test_cross_process_sequential_runs_have_unique_ids(tmp_path):
    """Two sequential runs from independent OS processes get unique run ids."""
    db_path = str(tmp_path / "ledger.sqlite3")
    ctx = mp.get_context("spawn")

    def run_in_child(intent):
        q = ctx.Queue()
        p = ctx.Process(target=_worker_run_once, args=(db_path, intent, q))
        p.start()
        (item,) = _drain(q, 1)
        p.join(30)
        assert p.exitcode == 0
        assert "error" not in item, item.get("error")
        return item

    first = run_in_child(ZsxqRunIntent.SYNC.value)
    second = run_in_child(ZsxqRunIntent.SYNC.value)

    assert first["status"] == ZsxqRunStatus.NO_CHANGE.value
    assert second["status"] == ZsxqRunStatus.NO_CHANGE.value
    assert first["run_id"] != second["run_id"]

    repo = ScraperRuntimeRepository(db_path)
    stored = {r["run_id"] for r in repo._conn.execute("SELECT run_id FROM runs").fetchall()}
    assert stored == {first["run_id"], second["run_id"]}
    repo.close()


@pytest.fixture(scope="module")
def concurrent_outcome(tmp_path_factory):
    """Run the concurrent-contention scenario once with two spawned processes."""
    db_path = str(tmp_path_factory.mktemp("concurrent") / "ledger.sqlite3")
    ctx = mp.get_context("spawn")
    holding = ctx.Event()
    release = ctx.Event()
    q = ctx.Queue()

    owner = ctx.Process(target=_worker_owner, args=(db_path, holding, release, q))
    peer = ctx.Process(target=_worker_coalescer, args=(db_path, holding, release, q))
    owner.start()
    peer.start()
    items = _drain(q, 2)
    owner.join(30)
    peer.join(30)

    # Both contending processes must have genuinely exited cleanly — no orphan
    # survives and neither aborted.
    assert not owner.is_alive()
    assert not peer.is_alive()
    assert owner.exitcode == 0
    assert peer.exitcode == 0

    by_role = {item["role"]: item for item in items}
    return {"db_path": db_path, "owner": by_role["owner"], "coalescer": by_role["coalescer"]}


def test_concurrent_trigger_coalesces_to_active_run(concurrent_outcome):
    """One process owns the run; the concurrent process coalesces onto it."""
    owner = concurrent_outcome["owner"]
    coalescer = concurrent_outcome["coalescer"]

    assert "error" not in owner, owner.get("error")
    assert "error" not in coalescer, coalescer.get("error")

    assert owner["status"] == ZsxqRunStatus.NO_CHANGE.value
    assert coalescer["status"] == ZsxqRunStatus.COALESCED.value
    assert coalescer["active_run_id"] == owner["run_id"]


def test_coalesced_request_creates_no_fake_run_history(concurrent_outcome):
    """The coalesced peer fabricates no run id; the ledger holds exactly one run."""
    coalescer = concurrent_outcome["coalescer"]
    owner = concurrent_outcome["owner"]

    # The coalesced result carries no fabricated run id.
    assert coalescer["run_id"] in (None, "")

    repo = ScraperRuntimeRepository(concurrent_outcome["db_path"])
    run_ids = [r["run_id"] for r in repo._conn.execute("SELECT run_id FROM runs").fetchall()]
    assert run_ids == [owner["run_id"]]
    assert repo.get_active_lease() is None
    repo.close()


def test_stale_run_is_marked_interrupted_before_recovery(tmp_path):
    """A crashed owner's run is marked interrupted before a fresh run is created."""
    db_path = str(tmp_path / "ledger.sqlite3")
    ctx = mp.get_context("spawn")
    info_q = ctx.Queue()

    deadline_iso = (datetime.fromisoformat(_BASE_ISO) + timedelta(hours=3)).isoformat()
    crashed = ctx.Process(target=_worker_acquire_then_crash, args=(db_path, deadline_iso, info_q))
    crashed.start()
    (info,) = _drain(info_q, 1)
    crashed.join(30)
    assert crashed.exitcode == 0
    assert info["acquired"] is True
    dead_run_id = info["run_id"]

    # A new process runs well past the stale threshold and reclaims the lease.
    later = datetime.fromisoformat(_BASE_ISO) + timedelta(seconds=10_000)
    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(
        repository=repo,
        adapter=_FakeAdapter(),
        clock=lambda: later,
        stale_after_seconds=120.0,
    )
    result = module.run(
        ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value, trigger=ZsxqRunTrigger.SCHEDULE.value)
    )

    abandoned = repo.get_run(dead_run_id)
    assert abandoned is not None
    assert abandoned["status"] == ZsxqRunStatus.INTERRUPTED.value
    assert abandoned["finished_at"]

    assert result.run_id != dead_run_id
    assert result.status == ZsxqRunStatus.NO_CHANGE.value
    assert repo.get_active_lease() is None
    repo.close()


# ── heartbeat / deadline cooperative checkpoint ───────────────────────


def test_live_run_heartbeat_prevents_stale_recovery(tmp_path):
    """A long run that checkpoints keeps ownership; a concurrent trigger coalesces."""
    db_path = str(tmp_path / "ledger.sqlite3")
    clock = _MutableClock()
    concurrent: dict = {}

    def during_long_run():
        # The owner has heartbeated recently, though far more than the stale
        # window has elapsed since acquisition. A peer must coalesce, not reclaim.
        peer = ScraperRuntimeRepository(db_path)
        now = clock()
        acq = peer.acquire_or_coalesce(
            intent=ZsxqRunIntent.WATCH.value,
            trigger=ZsxqRunTrigger.MANUAL.value,
            now=now,
            deadline_at=now + timedelta(seconds=120),
            stale_before=now - timedelta(seconds=120),
        )
        concurrent["acquired"] = acq.acquired
        concurrent["active_run_id"] = acq.active_run_id
        peer.close()

    adapter = _CheckpointingLongAdapter(
        clock, steps=5, step_seconds=60, during_at=2, during=during_long_run
    )
    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(
        repository=repo, adapter=adapter, clock=clock, stale_after_seconds=120.0
    )

    result = module.run(
        ZsxqRunRequest(
            intent=ZsxqRunIntent.SYNC.value,
            trigger=ZsxqRunTrigger.SCHEDULE.value,
            deadline_seconds=100_000,
        )
    )

    # The concurrent trigger observed a fresh (heartbeated) lease and coalesced.
    assert concurrent["acquired"] is False
    assert concurrent["active_run_id"] == result.run_id
    # The long run kept ownership through to a clean terminal (never interrupted).
    assert result.status == ZsxqRunStatus.NO_CHANGE.value
    assert repo.get_run(result.run_id)["status"] == ZsxqRunStatus.NO_CHANGE.value
    assert repo.get_active_lease() is None
    repo.close()


def test_deadline_persists_terminal_and_releases_lease(tmp_path):
    """A checkpoint past the total deadline stops work and persists DEADLINE_EXCEEDED."""
    db_path = str(tmp_path / "ledger.sqlite3")
    clock = _MutableClock()
    adapter = _DeadlineOverrunAdapter(clock, overrun_seconds=9_999)
    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(repository=repo, adapter=adapter, clock=clock)

    result = module.run(
        ZsxqRunRequest(
            intent=ZsxqRunIntent.SYNC.value,
            trigger=ZsxqRunTrigger.SCHEDULE.value,
            deadline_seconds=1.0,
        )
    )

    assert result.status == ZsxqRunStatus.DEADLINE_EXCEEDED.value
    # The checkpoint rejected the overrun BEFORE the adapter did any more work.
    assert adapter.reached_more_work is False
    stored = repo.get_run(result.run_id)
    assert stored["status"] == ZsxqRunStatus.DEADLINE_EXCEEDED.value
    assert stored["finished_at"]
    assert repo.get_active_lease() is None
    repo.close()


# ── owner completion samples the clock exactly once (FIX A) ───────────

# Both the fake (NO_CHANGE) and raising (FAILED) adapters call ``checkpoint``
# once, so a run reads the clock for acquisition, once for the single checkpoint,
# then exactly once at completion — three reads total.
_COMPLETION_ADAPTERS = [
    pytest.param(
        lambda: _FakeAdapter(changed_count=0), ZsxqRunStatus.NO_CHANGE.value, id="no_change"
    ),
    pytest.param(lambda: _RaisingAdapter(), ZsxqRunStatus.FAILED.value, id="failed"),
]

_COMPLETION_ADAPTER_FACTORIES = [
    pytest.param(lambda: _FakeAdapter(changed_count=0), id="no_change"),
    pytest.param(lambda: _RaisingAdapter(), id="failed"),
]


@pytest.mark.parametrize("make_adapter, expected_status", _COMPLETION_ADAPTERS)
def test_completion_samples_clock_once_just_before_deadline(
    tmp_path, make_adapter, expected_status
):
    """A1: the single completion sample lands just before the deadline; no second read.

    The pre-fix code read the clock a second time in ``_finalize``. With the clock
    at the deadline on that read, ``finish_run``'s deadline fence rejected the write
    with ``LeaseLostError`` and left the lease open. The fix samples once
    (deadline - 1µs), so the status decision and ``finished_at`` share that value
    and the lease closes cleanly.
    """
    db_path = str(tmp_path / "ledger.sqlite3")
    t0 = datetime.fromisoformat(_BASE_ISO)
    deadline = t0 + timedelta(seconds=1.0)
    deadline_minus = deadline - timedelta(microseconds=1)
    # 4th value is the trap the pre-fix second read would consume (=> LeaseLostError).
    clock = _ScriptedClock([t0, t0, deadline_minus, deadline])

    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(repository=repo, adapter=make_adapter(), clock=clock)

    result = module.run(
        ZsxqRunRequest(
            intent=ZsxqRunIntent.SYNC.value,
            trigger=ZsxqRunTrigger.SCHEDULE.value,
            deadline_seconds=1.0,
        )
    )

    assert result.status == expected_status
    assert result.finished_at == deadline_minus.isoformat()
    stored = repo.get_run(result.run_id)
    assert stored["status"] == expected_status
    assert stored["finished_at"] == deadline_minus.isoformat()
    assert repo.get_active_lease() is None
    repo.close()


@pytest.mark.parametrize("make_adapter", _COMPLETION_ADAPTER_FACTORIES)
def test_completion_sample_at_deadline_closes_as_deadline_exceeded(tmp_path, make_adapter):
    """A2: a completion sample exactly at the deadline closes as DEADLINE_EXCEEDED once.

    The scripted clock forbids a fourth read, proving completion samples the clock
    exactly once. Both adapters must terminalize as DEADLINE_EXCEEDED (the truthful
    state at the deadline), with ``finished_at`` equal to the deadline and the lease
    released.
    """
    db_path = str(tmp_path / "ledger.sqlite3")
    t0 = datetime.fromisoformat(_BASE_ISO)
    deadline = t0 + timedelta(seconds=1.0)
    # Only three reads are allowed; a fourth (the pre-fix second sample) raises.
    clock = _ScriptedClock([t0, t0, deadline])

    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(repository=repo, adapter=make_adapter(), clock=clock)

    result = module.run(
        ZsxqRunRequest(
            intent=ZsxqRunIntent.SYNC.value,
            trigger=ZsxqRunTrigger.SCHEDULE.value,
            deadline_seconds=1.0,
        )
    )

    assert result.status == ZsxqRunStatus.DEADLINE_EXCEEDED.value
    assert result.finished_at == deadline.isoformat()
    stored = repo.get_run(result.run_id)
    assert stored["status"] == ZsxqRunStatus.DEADLINE_EXCEEDED.value
    assert stored["finished_at"] == deadline.isoformat()
    assert repo.get_active_lease() is None
    repo.close()


# ── repository-level deadline fencing (persisted deadline_at) ─────────


def test_heartbeat_after_persisted_deadline_is_fenced_even_with_matching_token(tmp_path):
    """A heartbeat at/after the persisted deadline is fenced even with the right token."""
    from fin_analyse.scraper.runtime_repository import LeaseLostError

    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    base = datetime.fromisoformat(_BASE_ISO)
    deadline = base + timedelta(seconds=120)
    acq = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.SCHEDULE.value,
        now=base,
        deadline_at=deadline,
        stale_before=base,
    )
    assert acq.acquired is True

    # Exactly at the deadline — and beyond — renewal is fenced by the ledger.
    with pytest.raises(LeaseLostError):
        repo.heartbeat(run_id=acq.run_id, owner_token=acq.owner_token, at=deadline)
    with pytest.raises(LeaseLostError):
        repo.heartbeat(
            run_id=acq.run_id, owner_token=acq.owner_token, at=deadline + timedelta(seconds=1)
        )

    # A heartbeat strictly before the deadline still renews the lease.
    repo.heartbeat(run_id=acq.run_id, owner_token=acq.owner_token, at=base + timedelta(seconds=1))
    lease = repo.get_active_lease()
    assert lease is not None
    assert lease["heartbeat_at"] == (base + timedelta(seconds=1)).isoformat()
    repo.close()


def test_expired_owner_finish_rejects_non_deadline_terminal_but_deadline_exceeded_closes(tmp_path):
    """Past the persisted deadline only DEADLINE_EXCEEDED may terminalize and release."""
    from fin_analyse.scraper.runtime_repository import LeaseLostError

    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    base = datetime.fromisoformat(_BASE_ISO)
    deadline = base + timedelta(seconds=120)
    acq = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.SCHEDULE.value,
        now=base,
        deadline_at=deadline,
        stale_before=base,
    )
    after = deadline + timedelta(seconds=5)

    for bad in (
        ZsxqRunStatus.SUCCEEDED.value,
        ZsxqRunStatus.NO_CHANGE.value,
        ZsxqRunStatus.FAILED.value,
        ZsxqRunStatus.PARTIAL.value,
    ):
        with pytest.raises(LeaseLostError):
            repo.finish_run(
                run_id=acq.run_id,
                owner_token=acq.owner_token,
                status=bad,
                changed_count=0,
                finished_at=after,
            )
        # The rejected write leaves the run open and the lease held (no partial close).
        assert repo.get_active_lease() is not None
        assert repo.get_run(acq.run_id)["finished_at"] is None

    # The one truthful terminalization closes the control record and releases the lease.
    repo.finish_run(
        run_id=acq.run_id,
        owner_token=acq.owner_token,
        status=ZsxqRunStatus.DEADLINE_EXCEEDED.value,
        changed_count=0,
        finished_at=after,
    )
    assert repo.get_active_lease() is None
    stored = repo.get_run(acq.run_id)
    assert stored["status"] == ZsxqRunStatus.DEADLINE_EXCEEDED.value
    assert stored["finished_at"]
    repo.close()


def test_adapter_exception_after_deadline_closes_as_deadline_exceeded(tmp_path):
    """An adapter exception raised past the deadline closes as DEADLINE_EXCEEDED, not FAILED."""
    db_path = str(tmp_path / "ledger.sqlite3")
    clock = _MutableClock()
    adapter = _DeadlineThenRaiseAdapter(clock, overrun_seconds=9_999)
    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(repository=repo, adapter=adapter, clock=clock)

    result = module.run(
        ZsxqRunRequest(
            intent=ZsxqRunIntent.SYNC.value,
            trigger=ZsxqRunTrigger.SCHEDULE.value,
            deadline_seconds=1.0,
        )
    )

    assert result.status == ZsxqRunStatus.DEADLINE_EXCEEDED.value
    stored = repo.get_run(result.run_id)
    assert stored["status"] == ZsxqRunStatus.DEADLINE_EXCEEDED.value
    assert stored["finished_at"]
    assert repo.get_active_lease() is None
    repo.close()


# ── owner-token fencing after recovery ────────────────────────────────


@pytest.fixture(scope="module")
def crashed_and_reclaimed(tmp_path_factory):
    """Genuine crash + reclaim: a spawned owner dies, a new owner holds the lease."""
    db_path = str(tmp_path_factory.mktemp("fencing") / "ledger.sqlite3")
    ctx = mp.get_context("spawn")
    info_q = ctx.Queue()

    deadline_iso = (datetime.fromisoformat(_BASE_ISO) + timedelta(hours=3)).isoformat()
    crashed = ctx.Process(target=_worker_acquire_then_crash, args=(db_path, deadline_iso, info_q))
    crashed.start()
    (info,) = _drain(info_q, 1)
    crashed.join(30)
    assert crashed.exitcode == 0

    later = datetime.fromisoformat(_BASE_ISO) + timedelta(seconds=10_000)
    reclaimer = ScraperRuntimeRepository(db_path)
    new_acq = reclaimer.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.SCHEDULE.value,
        now=later,
        deadline_at=later + timedelta(hours=1),
        stale_before=later - timedelta(seconds=120),
    )
    assert new_acq.acquired is True
    assert new_acq.recovered_run_id == info["run_id"]
    reclaimer.close()

    return {
        "db_path": db_path,
        "dead_run_id": info["run_id"],
        "dead_owner_token": info["owner_token"],
        "new_run_id": new_acq.run_id,
        "later": later,
    }


def test_reclaimed_owner_cannot_finish_or_overwrite_interrupted_run(crashed_and_reclaimed):
    """The reclaimed owner cannot finish its run or overwrite its INTERRUPTED record."""
    from fin_analyse.scraper.runtime_repository import LeaseLostError

    fixture = crashed_and_reclaimed
    repo = ScraperRuntimeRepository(fixture["db_path"])
    assert repo.get_run(fixture["dead_run_id"])["status"] == ZsxqRunStatus.INTERRUPTED.value

    with pytest.raises(LeaseLostError):
        repo.finish_run(
            run_id=fixture["dead_run_id"],
            owner_token=fixture["dead_owner_token"],
            status=ZsxqRunStatus.SUCCEEDED.value,
            changed_count=99,
            finished_at=fixture["later"],
        )

    # INTERRUPTED is preserved and the new owner's lease is intact.
    assert repo.get_run(fixture["dead_run_id"])["status"] == ZsxqRunStatus.INTERRUPTED.value
    lease = repo.get_active_lease()
    assert lease is not None
    assert lease["run_id"] == fixture["new_run_id"]
    repo.close()


def test_lost_owner_cannot_heartbeat_or_release_new_owner_lease(crashed_and_reclaimed):
    """The fenced owner cannot heartbeat, nor delete the new owner's lease."""
    from fin_analyse.scraper.runtime_repository import LeaseLostError

    fixture = crashed_and_reclaimed
    repo = ScraperRuntimeRepository(fixture["db_path"])

    with pytest.raises(LeaseLostError):
        repo.heartbeat(
            run_id=fixture["dead_run_id"],
            owner_token=fixture["dead_owner_token"],
            at=fixture["later"],
        )

    # finish_run is the only lease-delete path; it too is fenced by owner token.
    with pytest.raises(LeaseLostError):
        repo.finish_run(
            run_id=fixture["dead_run_id"],
            owner_token=fixture["dead_owner_token"],
            status=ZsxqRunStatus.FAILED.value,
            changed_count=0,
            finished_at=fixture["later"],
        )

    lease = repo.get_active_lease()
    assert lease is not None
    assert lease["run_id"] == fixture["new_run_id"]
    repo.close()


# ── adapter-exception durability ──────────────────────────────────────


def test_adapter_failure_persists_failed_and_releases_lease(tmp_path):
    """An adapter exception yields a durable FAILED terminal with the lease released."""
    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(repository=repo, adapter=_RaisingAdapter(), clock=_MutableClock())

    result = module.run(
        ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value, trigger=ZsxqRunTrigger.SCHEDULE.value)
    )

    assert result.status == ZsxqRunStatus.FAILED.value
    stored = repo.get_run(result.run_id)
    assert stored is not None
    assert stored["status"] == ZsxqRunStatus.FAILED.value
    assert stored["finished_at"]
    assert repo.get_active_lease() is None
    repo.close()


def test_adapter_failure_carries_narrow_failure_reason(tmp_path):
    """A FAILED run keeps the classified narrow cause on the wire result."""
    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(
        repository=repo,
        adapter=_RaisingAdapter("CDP Bridge 连接失败 [target_invalid]: OPENCLI_TARGET_INVALID"),
        clock=_MutableClock(),
    )

    result = module.run(
        ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value, trigger=ZsxqRunTrigger.SCHEDULE.value)
    )

    assert result.status == ZsxqRunStatus.FAILED.value
    assert result.failure_reason == "target_invalid"
    assert result.to_dict()["failure_reason"] == "target_invalid"
    repo.close()


def test_deadline_exceeded_failure_reason_is_none(tmp_path):
    """The deadline itself is the cause; no narrow failure reason is fabricated."""
    db_path = str(tmp_path / "ledger.sqlite3")
    clock = _MutableClock()
    adapter = _DeadlineThenRaiseAdapter(clock, overrun_seconds=9_999)
    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(repository=repo, adapter=adapter, clock=clock)

    result = module.run(
        ZsxqRunRequest(
            intent=ZsxqRunIntent.SYNC.value,
            trigger=ZsxqRunTrigger.SCHEDULE.value,
            deadline_seconds=1.0,
        )
    )

    assert result.status == ZsxqRunStatus.DEADLINE_EXCEEDED.value
    assert result.failure_reason is None
    assert "failure_reason" not in result.to_dict()
    repo.close()


def test_out_of_allowlist_failure_reason_coerces_to_unknown(tmp_path):
    """A classified kind outside the frozen allowlist (e.g. legacy
    extension_disconnected) degrades to 'unknown' instead of leaking a
    new un-contracted wire value."""
    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(
        repository=repo,
        adapter=_RaisingAdapter("McpError: Connection closed"),
        clock=_MutableClock(),
    )

    result = module.run(
        ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value, trigger=ZsxqRunTrigger.SCHEDULE.value)
    )

    assert result.status == ZsxqRunStatus.FAILED.value
    assert result.failure_reason == "unknown"
    assert result.to_dict()["failure_reason"] == "unknown"
    repo.close()


# ── wire contract truth ───────────────────────────────────────────────


def test_coalesced_wire_contract_has_no_run_or_timestamp_fields(tmp_path):
    """The coalesced wire dict serializes no run id, timestamps or history fields."""
    db_path = str(tmp_path / "ledger.sqlite3")

    module_b, repo_b = _build_module(db_path)

    def concurrent_trigger():
        return module_b.run(
            ZsxqRunRequest(intent=ZsxqRunIntent.WATCH.value, trigger=ZsxqRunTrigger.MANUAL.value)
        )

    adapter = _BlockingInProcessAdapter(during=concurrent_trigger)
    repo_a = ScraperRuntimeRepository(db_path)
    module_a = ZsxqScraperModule(repository=repo_a, adapter=adapter, clock=_MutableClock())

    result_a = module_a.run(
        ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value, trigger=ZsxqRunTrigger.SCHEDULE.value)
    )
    coalesced = adapter.concurrent_result

    assert coalesced.status == ZsxqRunStatus.COALESCED.value
    assert coalesced.run_id is None

    wire = coalesced.to_dict()
    for absent in ("run_id", "started_at", "finished_at", "changed_count", "attempt"):
        assert absent not in wire, f"coalesced wire must not carry {absent!r}"
    assert wire["active_run_id"] == result_a.run_id
    assert wire["coalesced"] is True
    repo_a.close()
    repo_b.close()


def test_terminal_result_has_no_active_run_id(tmp_path):
    """A terminal run result carries a run id but no active_run_id field."""
    db_path = str(tmp_path / "ledger.sqlite3")
    module, repo = _build_module(db_path)

    result = module.run(
        ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value, trigger=ZsxqRunTrigger.SCHEDULE.value)
    )

    assert result.status == ZsxqRunStatus.NO_CHANGE.value
    assert result.run_id
    assert result.active_run_id is None
    assert "active_run_id" not in result.to_dict()
    repo.close()


# ── schema v1 exactness / reopen / fail-closed ────────────────────────


def _app_tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


#: The exact v2 application-table set the fresh schema must create.
_V2_APP_TABLES = {
    "schema_version",
    "runs",
    "active_lease",
    "health_observations",
    "health_episodes",
    "scraper_outbox",
}


#: The exact historical Gate-2A v1 three-table DDL, as a TEST-LOCAL oracle (this
#: is deliberately NOT imported from production ``_V1_TABLE_DDL`` so the contract is
#: pinned independently). ``_make_exact_v1_ledger`` consumes it; the B0a3 exact-v1
#: input tests mutate exactly one fragment of one table's DDL.
_EXACT_V1_TABLE_DDL: dict[str, str] = {
    "schema_version": (
        "CREATE TABLE schema_version (\n"
        "    id INTEGER PRIMARY KEY CHECK (id = 1),\n"
        "    version INTEGER NOT NULL\n"
        ")"
    ),
    "runs": (
        "CREATE TABLE runs (\n"
        "    seq INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    run_id TEXT UNIQUE,\n"
        "    trigger TEXT NOT NULL,\n"
        "    status TEXT NOT NULL,\n"
        "    attempt INTEGER NOT NULL DEFAULT 1,\n"
        "    changed_count INTEGER NOT NULL DEFAULT 0,\n"
        "    started_at TEXT NOT NULL,\n"
        "    finished_at TEXT\n"
        ")"
    ),
    "active_lease": (
        "CREATE TABLE active_lease (\n"
        "    id INTEGER PRIMARY KEY CHECK (id = 1),\n"
        "    run_id TEXT NOT NULL,\n"
        "    owner_token TEXT NOT NULL,\n"
        "    acquired_at TEXT NOT NULL,\n"
        "    heartbeat_at TEXT NOT NULL,\n"
        "    deadline_at TEXT NOT NULL\n"
        ")"
    ),
}


# Historical schema v2 is pinned independently from production so migration tests
# cannot silently follow the current schema forward.
_EXACT_V2_TABLE_DDL: dict[str, str] = {
    "schema_version": (
        "CREATE TABLE IF NOT EXISTS schema_version (\n"
        "    id      INTEGER PRIMARY KEY CHECK (id = 1),\n"
        "    version INTEGER NOT NULL\n"
        ")"
    ),
    "runs": (
        "CREATE TABLE IF NOT EXISTS runs (\n"
        "    seq           INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    run_id        TEXT UNIQUE,\n"
        "    intent        TEXT NOT NULL,\n"
        "    trigger       TEXT NOT NULL,\n"
        "    status        TEXT NOT NULL,\n"
        "    attempt       INTEGER NOT NULL DEFAULT 1,\n"
        "    changed_count INTEGER NOT NULL DEFAULT 0,\n"
        "    started_at    TEXT NOT NULL,\n"
        "    finished_at   TEXT\n"
        ")"
    ),
    "active_lease": (
        "CREATE TABLE IF NOT EXISTS active_lease (\n"
        "    id           INTEGER PRIMARY KEY CHECK (id = 1),\n"
        "    owner_kind   TEXT NOT NULL CHECK (owner_kind IN ('run', 'probe')),\n"
        "    run_id       TEXT,\n"
        "    owner_token  TEXT NOT NULL,\n"
        "    acquired_at  TEXT NOT NULL,\n"
        "    heartbeat_at TEXT NOT NULL,\n"
        "    deadline_at  TEXT NOT NULL,\n"
        "    CHECK ((owner_kind = 'run' AND run_id IS NOT NULL) OR "
        "(owner_kind = 'probe' AND run_id IS NULL))\n"
        ")"
    ),
    "health_observations": (
        "CREATE TABLE IF NOT EXISTS health_observations (\n"
        "    seq          INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    intent       TEXT NOT NULL,\n"
        "    surface      TEXT NOT NULL,\n"
        "    state        TEXT NOT NULL,\n"
        "    observed_at  TEXT NOT NULL,\n"
        "    episode_id   TEXT,\n"
        "    evidence_ref TEXT\n"
        ")"
    ),
    "health_episodes": (
        "CREATE TABLE IF NOT EXISTS health_episodes (\n"
        "    episode_id  TEXT PRIMARY KEY,\n"
        "    intent      TEXT NOT NULL,\n"
        "    surface     TEXT NOT NULL,\n"
        "    reason_code TEXT NOT NULL,\n"
        "    status      TEXT NOT NULL,\n"
        "    opened_at   TEXT NOT NULL,\n"
        "    closed_at   TEXT\n"
        ")"
    ),
    "scraper_outbox": (
        "CREATE TABLE IF NOT EXISTS scraper_outbox (\n"
        "    seq          INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    dedupe_key   TEXT NOT NULL UNIQUE,\n"
        "    kind         TEXT NOT NULL,\n"
        "    subject_type TEXT NOT NULL,\n"
        "    subject_id   TEXT NOT NULL,\n"
        "    reason_code  TEXT NOT NULL,\n"
        "    action_code  TEXT NOT NULL,\n"
        "    evidence_ref TEXT,\n"
        "    occurred_at  TEXT NOT NULL,\n"
        "    delivered_at TEXT\n"
        ")"
    ),
}


def _make_v1_ledger_from_ddl(db_path: str, ddl_by_table: dict[str, str]) -> None:
    """Create a version-1 ledger from the given per-table DDL and seed ``(1, 1)``.

    Executes each ``CREATE TABLE`` verbatim (so whitespace/mutations are preserved
    on disk) and seeds the singleton ``schema_version`` row at version 1.
    """
    conn = sqlite3.connect(db_path)
    try:
        for ddl in ddl_by_table.values():
            conn.execute(ddl)
        conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()


def _make_exact_v1_ledger(db_path: str) -> None:
    """Materialise a database with the EXACT known Gate 2A v1 shape (version 1).

    This is the byte-shape the v1→v2 migration is allowed to recognise: three
    application tables with the v1 columns, the runs UNIQUE autoindex, and a
    single ``schema_version`` row holding version 1. No health/outbox tables.
    """
    _make_v1_ledger_from_ddl(db_path, _EXACT_V1_TABLE_DDL)


def _make_exact_v2_ledger(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for ddl in _EXACT_V2_TABLE_DDL.values():
            conn.execute(ddl)
        conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 2)")
        conn.commit()
    finally:
        conn.close()


def _make_exact_v3_ledger(db_path: str) -> None:
    import fin_analyse.scraper.runtime_repository as rr

    conn = sqlite3.connect(db_path)
    try:
        for ddl in rr._V3_TABLE_DDL:
            conn.execute(ddl)
        conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 3)")
        conn.commit()
    finally:
        conn.close()
    os.chmod(db_path, 0o600)


def _make_mutated_v1_ledger(db_path: str, *, table: str, pattern: str, repl: str) -> None:
    """Create a v1 ledger with exactly one enforced-shape fragment mutated.

    Only ``table``'s DDL is changed — via ``_sub_once`` (exactly one replacement,
    result must differ); every other table's DDL is copied verbatim, so the mutation
    is single-cause.
    """
    ddl = dict(_EXACT_V1_TABLE_DDL)
    ddl[table] = _sub_once(ddl[table], pattern, repl)
    _make_v1_ledger_from_ddl(db_path, ddl)


def _normalize_v1_sql(sql: str) -> str:
    """TEST-LOCAL whitespace normalization (independent of production normalization)."""
    return re.sub(r"\s+", " ", sql).strip()


def _v1_fingerprint(db_path: str) -> dict:
    """A complete TEST-LOCAL logical fingerprint of a v1 ledger's on-disk state.

    Inventories every non-internal object with literal-prefix semantics
    (``name NOT GLOB 'sqlite_*'`` — never ``LIKE``, whose ``_`` is a wildcard) and
    records: raw ``sqlite_master`` SQL (for same-database unchanged assertions),
    ordered full ``PRAGMA table_xinfo``, name-independent ``index_list`` +
    ``index_xinfo`` signatures, the exact ``schema_version`` rows, ``runs`` and
    ``active_lease`` row counts, and ``PRAGMA integrity_check``.
    """
    conn = sqlite3.connect(db_path)
    try:
        master = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name, tbl_name"
        ).fetchall()
        tables = [name for (typ, name, *_rest) in master if typ == "table"]
        semantic: dict[str, dict] = {}
        for table in tables:
            xinfo = [tuple(r) for r in conn.execute(f"PRAGMA table_xinfo({table})").fetchall()]
            index_sigs = []
            for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
                idx_name, unique, origin, partial = idx[1], idx[2], idx[3], idx[4]
                idx_cols = tuple(
                    tuple(c) for c in conn.execute(f"PRAGMA index_xinfo('{idx_name}')").fetchall()
                )
                # Name-independent: the autoindex name is NOT part of the signature.
                index_sigs.append((unique, origin, partial, idx_cols))
            semantic[table] = {"xinfo": xinfo, "indexes": sorted(index_sigs, key=repr)}
        schema_version_rows = [
            tuple(r)
            for r in conn.execute("SELECT id, version FROM schema_version ORDER BY id").fetchall()
        ]
        runs_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        lease_count = conn.execute("SELECT COUNT(*) FROM active_lease").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    return {
        "master": [tuple(row) for row in master],
        "semantic": semantic,
        "schema_version_rows": schema_version_rows,
        "runs_count": runs_count,
        "lease_count": lease_count,
        "integrity": integrity,
    }


def test_operator_migrates_exact_v3_while_ordinary_open_refuses_it(tmp_path):
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    tmp_path.chmod(0o700)
    db_path = str(tmp_path / "legacy-v3.sqlite3")
    _make_exact_v3_ledger(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO runs "
        "(run_id, intent, trigger, status, changed_count, started_at, finished_at) "
        "VALUES ('r00000001-preserved', 'sync', 'manual', 'succeeded', 2, ?, ?)",
        (_BASE_ISO, _BASE_ISO),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    finally:
        conn.close()

    with pytest.raises(SchemaVersionError, match="explicit operator migration"):
        ScraperRuntimeRepository(db_path)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()

    disposition = ScraperRuntimeRepository.operator_migrate_v3_to_v4(db_path)

    assert disposition == "MIGRATED"
    repo = ScraperRuntimeRepository(db_path)
    try:
        assert repo.schema_version() == 4
        assert repo.get_run("r00000001-preserved")["changed_count"] == 2
        assert "capture_ingests" in _app_tables(repo._conn)
    finally:
        repo.close()


def test_operator_exact_v4_replay_is_filesystem_zero_write(tmp_path):
    tmp_path.chmod(0o700)
    db_path = tmp_path / "current-v4.sqlite3"
    repo = ScraperRuntimeRepository(db_path)
    repo.close()
    db_path.chmod(0o600)
    before_bytes = db_path.read_bytes()
    fixed_ns = 1_700_000_000_000_000_000
    os.utime(tmp_path, ns=(fixed_ns, fixed_ns))
    parent_before = tmp_path.stat()

    disposition = ScraperRuntimeRepository.operator_migrate_v3_to_v4(db_path)

    assert disposition == "ALREADY_CURRENT"
    assert db_path.read_bytes() == before_bytes
    assert sorted(path.name for path in tmp_path.iterdir()) == [db_path.name]
    parent_after = tmp_path.stat()
    assert (parent_after.st_mtime_ns, parent_after.st_ctime_ns) == (
        parent_before.st_mtime_ns,
        parent_before.st_ctime_ns,
    )


def test_operator_v4_replay_rejects_parent_mode_drift(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    db_path = tmp_path / "current-v4.sqlite3"
    repo = ScraperRuntimeRepository(str(db_path))
    repo.close()
    db_path.chmod(0o600)
    validate_v4_exact = ScraperRuntimeRepository._validate_v4_exact

    def drift_parent_mode(self, cursor):
        validate_v4_exact(self, cursor)
        tmp_path.chmod(0o755)

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "_validate_v4_exact",
        drift_parent_mode,
    )

    with pytest.raises(ValueError, match="runtime_db_parent_unsafe"):
        ScraperRuntimeRepository.operator_migrate_v3_to_v4(db_path)

    assert not (tmp_path / "scheduler-handoff.lock").exists()


def test_operator_v3_migration_rejects_active_lease_unchanged(tmp_path):
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    tmp_path.chmod(0o700)
    db_path = str(tmp_path / "active-v3.sqlite3")
    _make_exact_v3_ledger(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO runs (run_id, intent, trigger, status, started_at) "
        "VALUES ('r00000001-active', 'sync', 'manual', 'running', ?)",
        (_BASE_ISO,),
    )
    conn.execute(
        "INSERT INTO active_lease "
        "(id, owner_kind, run_id, owner_token, acquired_at, heartbeat_at, deadline_at) "
        "VALUES (1, 'run', 'r00000001-active', 'owner', ?, ?, ?)",
        (_BASE_ISO, _BASE_ISO, "2026-07-11T09:02:00+00:00"),
    )
    conn.commit()
    conn.close()
    fixed_ns = 1_700_000_000_000_000_000
    os.utime(tmp_path, ns=(fixed_ns, fixed_ns))
    parent_before = tmp_path.stat()

    with pytest.raises(SchemaVersionError, match="active lease"):
        ScraperRuntimeRepository.operator_migrate_v3_to_v4(db_path)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (3,)
        assert conn.execute("SELECT owner_token FROM active_lease").fetchone() == ("owner",)
        assert (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='capture_ingests'").fetchone()
            is None
        )
    finally:
        conn.close()
    assert not (tmp_path / "scheduler-handoff.lock").exists()
    parent_after = tmp_path.stat()
    assert (parent_after.st_mtime_ns, parent_after.st_ctime_ns) == (
        parent_before.st_mtime_ns,
        parent_before.st_ctime_ns,
    )


def test_operator_v3_migration_fault_rolls_back(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    db_path = str(tmp_path / "fault-v3.sqlite3")
    _make_exact_v3_ledger(db_path)

    def fail(_cursor):
        raise RuntimeError("injected migration fault")

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "_v4_operator_migration_fault_hook",
        staticmethod(fail),
    )
    with pytest.raises(RuntimeError, match="injected migration fault"):
        ScraperRuntimeRepository.operator_migrate_v3_to_v4(db_path)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (3,)
        assert (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='capture_ingests'").fetchone()
            is None
        )
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        conn.close()


def test_operator_post_commit_verification_failure_reports_write(tmp_path, monkeypatch):
    import fin_analyse.scraper.runtime_repository as repository

    tmp_path.chmod(0o700)
    db_path = tmp_path / "post-commit-v3.sqlite3"
    _make_exact_v3_ledger(str(db_path))
    validate_runtime_db = repository._validate_operator_runtime_db

    def fail_after_committed(path):
        identity = validate_runtime_db(path)
        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            connection.close()
        if version == 4:
            raise ValueError("injected post-commit verification failure")
        return identity

    monkeypatch.setattr(repository, "_validate_operator_runtime_db", fail_after_committed)

    with pytest.raises(RuntimeError) as raised:
        ScraperRuntimeRepository.operator_migrate_v3_to_v4(db_path)

    assert getattr(raised.value, "wrote", None) is True
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (4,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='capture_ingests'"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_operator_lock_exit_failure_after_commit_reports_write(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import fin_analyse.scraper.scheduler_handoff_lock as handoff_lock

    tmp_path.chmod(0o700)
    db_path = tmp_path / "lock-exit-v3.sqlite3"
    _make_exact_v3_ledger(str(db_path))
    hold_lock = handoff_lock.hold_scheduler_handoff_lock

    @contextmanager
    def drift_parent_after_body(path, *, mode):
        with hold_lock(path, mode=mode):
            yield
            tmp_path.chmod(0o755)

    monkeypatch.setattr(handoff_lock, "hold_scheduler_handoff_lock", drift_parent_after_body)

    with pytest.raises(RuntimeError) as raised:
        ScraperRuntimeRepository.operator_migrate_v3_to_v4(db_path)

    assert getattr(raised.value, "wrote", None) is True
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (4,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='capture_ingests'"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_operator_v4_replay_rejects_foreign_key_corruption(tmp_path):
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    tmp_path.chmod(0o700)
    db_path = tmp_path / "corrupt-v4.sqlite3"
    repo = ScraperRuntimeRepository(str(db_path))
    repo.close()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO capture_ingests "
            "(artifact_run_id, content_sha256, phase, ingest_run_id, prior_g_json, "
            "business_json) VALUES (?, ?, 'BUSINESS_TERMINAL', ?, '{}', '{}')",
            (
                "77777777-7777-4777-8777-777777777777",
                "2" * 64,
                "missing-run",
            ),
        )
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is not None
    finally:
        connection.close()
    db_path.chmod(0o600)

    with pytest.raises(SchemaVersionError, match="foreign_key_check"):
        ScraperRuntimeRepository.operator_migrate_v3_to_v4(db_path)


def test_fresh_database_is_exact_schema_v4(tmp_path):
    """A fresh database creates exact v4 including capture recovery state."""
    db_path = str(tmp_path / "ledger.sqlite3")

    repo = ScraperRuntimeRepository(db_path)
    assert SCHEMA_VERSION == 4
    assert _app_tables(repo._conn) == _V2_APP_TABLES | {"capture_ingests"}
    versions = [r[0] for r in repo._conn.execute("SELECT version FROM schema_version").fetchall()]
    assert versions == [SCHEMA_VERSION]
    # active_lease carries the v2 owner_kind column (run|probe).
    lease_cols = {
        row[1] for row in repo._conn.execute("PRAGMA table_info(active_lease)").fetchall()
    }
    assert "owner_kind" in lease_cols
    # runs carries intent and trigger as separate columns.
    run_cols = {row[1] for row in repo._conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert {"intent", "trigger"} <= run_cols
    observation_columns = {
        row[1]: tuple(row)
        for row in repo._conn.execute("PRAGMA table_info(health_observations)").fetchall()
    }
    assert observation_columns["reason_code"][2:4] == ("TEXT", 1)
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute(
            "INSERT INTO health_observations "
            "(intent, surface, state, observed_at) VALUES ('watch', 'timeline', 'ready', ?)",
            (_BASE_ISO,),
        )
    repo.close()

    # The complete logical fingerprint of the canonical fresh v2 ledger, captured
    # while closed, must survive a successful reopen unchanged (positive control
    # for the fail-closed reopen tests below, which use the same fingerprint).
    fresh_fingerprint = _v2_shape_fingerprint(db_path)

    # Idempotent reopen: no extra tables, singleton schema-version row preserved.
    repo2 = ScraperRuntimeRepository(db_path)
    assert _app_tables(repo2._conn) == _V2_APP_TABLES | {"capture_ingests"}
    count = repo2._conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == 1
    repo2.close()

    # A canonical fresh v2 ledger closes and reopens successfully with the exact
    # same complete logical fingerprint (schema + enforced shape) before/after.
    assert _v2_shape_fingerprint(db_path) == fresh_fingerprint

    # An unknown future version must fail closed on open (no silent downgrade).
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 999,))
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)


def test_capture_completion_cannot_skip_business_terminal(tmp_path):
    """COMPLETE is reachable only after the business result is durable."""
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))
    artifact_run_id = "11111111-1111-4111-8111-111111111111"
    content_sha256 = "a" * 64
    repo.claim_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        prior_g_json="{}",
    )
    now = datetime.fromisoformat(_BASE_ISO)
    acquired = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.MANUAL.value,
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )

    with pytest.raises(RuntimeError, match="phase is invalid"):
        repo.complete_capture_ingest(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            ingest_run_id=acquired.run_id,
            business_json="{}",
            completion_json="{}",
            audit_json="{}",
        )

    assert (
        repo.read_capture_ingest(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
        ).phase
        == "CLAIMED"
    )
    repo.close()


def test_capture_completion_cannot_rewrite_frozen_business_result(tmp_path):
    """The completion projection may not splice in different business state."""
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))
    artifact_run_id = "22222222-2222-4222-8222-222222222222"
    content_sha256 = "b" * 64
    repo.claim_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        prior_g_json="{}",
    )
    now = datetime.fromisoformat(_BASE_ISO)
    acquired = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.MANUAL.value,
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )
    original_business = _terminal_business_json(
        acquired,
        status=ZsxqRunStatus.FAILED.value,
        changed_count=0,
        finished_at=now + timedelta(seconds=1),
    )
    repo.finish_capture_business(
        run_id=acquired.run_id,
        owner_token=acquired.owner_token,
        status=ZsxqRunStatus.FAILED.value,
        changed_count=0,
        finished_at=now + timedelta(seconds=1),
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        business_json=original_business,
    )

    with pytest.raises(RuntimeError, match="business projection conflicts"):
        repo.complete_capture_ingest(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            ingest_run_id=acquired.run_id,
            business_json='{"status":"succeeded"}',
            completion_json="{}",
            audit_json="{}",
        )

    record = repo.read_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
    )
    assert record.phase == "BUSINESS_TERMINAL"
    assert record.business_json == original_business
    repo.close()


def test_capture_completion_rejects_unreadable_projection_without_terminalizing(tmp_path):
    """A malformed recovery projection cannot poison the immutable COMPLETE state."""
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))
    artifact_run_id = "77777777-7777-4777-8777-777777777777"
    content_sha256 = "7" * 64
    repo.claim_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        prior_g_json="{}",
    )
    now = datetime.fromisoformat(_BASE_ISO)
    acquired = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.MANUAL.value,
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )
    business_json = _terminal_business_json(
        acquired,
        status=ZsxqRunStatus.FAILED.value,
        changed_count=0,
        finished_at=now + timedelta(seconds=1),
    )
    repo.finish_capture_business(
        run_id=acquired.run_id,
        owner_token=acquired.owner_token,
        status=ZsxqRunStatus.FAILED.value,
        changed_count=0,
        finished_at=now + timedelta(seconds=1),
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        business_json=business_json,
    )

    with pytest.raises(ValueError, match="recovery projection is invalid"):
        repo.complete_capture_ingest(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            ingest_run_id=acquired.run_id,
            business_json=business_json,
            completion_json="{}",
            audit_json="{}",
        )

    assert (
        repo.read_capture_ingest(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
        ).phase
        == "BUSINESS_TERMINAL"
    )
    repo.close()


def test_successful_capture_requires_prepared_publication_before_completion(tmp_path):
    """A successful business run cannot bypass its durable G publication plan."""
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))
    artifact_run_id = "33333333-3333-4333-8333-333333333333"
    content_sha256 = "c" * 64
    repo.claim_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        prior_g_json="{}",
    )
    now = datetime.fromisoformat(_BASE_ISO)
    acquired = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.MANUAL.value,
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )
    business_json = _terminal_business_json(
        acquired,
        status=ZsxqRunStatus.SUCCEEDED.value,
        changed_count=1,
        finished_at=now + timedelta(seconds=1),
    )
    repo.finish_capture_business(
        run_id=acquired.run_id,
        owner_token=acquired.owner_token,
        status=ZsxqRunStatus.SUCCEEDED.value,
        changed_count=1,
        finished_at=now + timedelta(seconds=1),
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        business_json=business_json,
    )

    with pytest.raises(RuntimeError, match="publication plan is required"):
        repo.complete_capture_ingest(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            ingest_run_id=acquired.run_id,
            business_json=business_json,
            completion_json="{}",
            audit_json="{}",
        )

    assert (
        repo.read_capture_ingest(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
        ).phase
        == "BUSINESS_TERMINAL"
    )
    repo.close()


def test_capture_publication_rejects_unreadable_plan_without_freezing(tmp_path):
    """An invalid G plan cannot strand a successful capture in PREPARED."""
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))
    artifact_run_id = "88888888-8888-4888-8888-888888888888"
    content_sha256 = "8" * 64
    repo.claim_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        prior_g_json="{}",
    )
    now = datetime.fromisoformat(_BASE_ISO)
    acquired = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.MANUAL.value,
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )
    business_json = _terminal_business_json(
        acquired,
        status=ZsxqRunStatus.SUCCEEDED.value,
        changed_count=1,
        finished_at=now + timedelta(seconds=1),
    )
    repo.finish_capture_business(
        run_id=acquired.run_id,
        owner_token=acquired.owner_token,
        status=ZsxqRunStatus.SUCCEEDED.value,
        changed_count=1,
        finished_at=now + timedelta(seconds=1),
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        business_json=business_json,
    )

    with pytest.raises(ValueError, match="publication plan is invalid"):
        repo.prepare_capture_publication(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            ingest_run_id=acquired.run_id,
            publication_plan_json="{}",
        )

    record = repo.read_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
    )
    assert record.phase == "BUSINESS_TERMINAL"
    assert record.publication_plan_json is None
    repo.close()


def test_failed_capture_cannot_prepare_g_publication(tmp_path):
    """Only successful/no-change business runs may enter G publication."""
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))
    artifact_run_id = "44444444-4444-4444-8444-444444444444"
    content_sha256 = "d" * 64
    repo.claim_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        prior_g_json="{}",
    )
    now = datetime.fromisoformat(_BASE_ISO)
    acquired = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.MANUAL.value,
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )
    business_json = _terminal_business_json(
        acquired,
        status=ZsxqRunStatus.FAILED.value,
        changed_count=0,
        finished_at=now + timedelta(seconds=1),
    )
    repo.finish_capture_business(
        run_id=acquired.run_id,
        owner_token=acquired.owner_token,
        status=ZsxqRunStatus.FAILED.value,
        changed_count=0,
        finished_at=now + timedelta(seconds=1),
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        business_json=business_json,
    )

    with pytest.raises(RuntimeError, match="successful terminal run"):
        repo.prepare_capture_publication(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            ingest_run_id=acquired.run_id,
            publication_plan_json="{}",
        )

    assert (
        repo.read_capture_ingest(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
        ).phase
        == "BUSINESS_TERMINAL"
    )
    repo.close()


def test_capture_identity_is_not_nullable_in_storage(tmp_path):
    """SQLite TEXT PRIMARY KEY alone must not admit an identity-less claim."""
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))

    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute(
            "INSERT INTO capture_ingests "
            "(artifact_run_id, content_sha256, phase, prior_g_json) "
            "VALUES (NULL, ?, 'CLAIMED', '{}')",
            ("e" * 64,),
        )

    repo.close()


@pytest.mark.parametrize("state_json", ("[]", '{"value":NaN}'))
def test_capture_state_requires_a_strict_json_object(tmp_path, state_json):
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))

    with pytest.raises(ValueError, match="prior_g_json is invalid"):
        repo.claim_capture_ingest(
            artifact_run_id="55555555-5555-4555-8555-555555555555",
            content_sha256="f" * 64,
            prior_g_json=state_json,
        )

    assert repo._conn.execute("SELECT COUNT(*) FROM capture_ingests").fetchone()[0] == 0
    repo.close()


def test_capture_business_json_must_match_terminal_facts(tmp_path):
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))
    artifact_run_id = "66666666-6666-4666-8666-666666666666"
    content_sha256 = "1" * 64
    repo.claim_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        prior_g_json="{}",
    )
    now = datetime.fromisoformat(_BASE_ISO)
    acquired = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.MANUAL.value,
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )
    mismatched = json.dumps(
        {
            "changed_count": 0,
            "coalesced": False,
            "finished_at": (now + timedelta(seconds=1)).isoformat(),
            "run_id": acquired.run_id,
            "status": "succeeded",
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(ValueError, match="business_json terminal facts conflict"):
        repo.finish_capture_business(
            run_id=acquired.run_id,
            owner_token=acquired.owner_token,
            status=ZsxqRunStatus.FAILED.value,
            changed_count=0,
            finished_at=now + timedelta(seconds=1),
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            business_json=mismatched,
        )

    assert repo.get_run(acquired.run_id)["status"] == "running"
    repo.close()


def test_capture_business_json_must_bind_the_complete_run_projection(tmp_path):
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))
    artifact_run_id = "99999999-9999-4999-8999-999999999999"
    content_sha256 = "9" * 64
    repo.claim_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        prior_g_json="{}",
    )
    now = datetime.fromisoformat(_BASE_ISO)
    acquired = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.MANUAL.value,
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )
    incomplete = json.dumps(
        {
            "changed_count": 0,
            "coalesced": False,
            "finished_at": (now + timedelta(seconds=1)).isoformat(),
            "run_id": acquired.run_id,
            "status": ZsxqRunStatus.FAILED.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(ValueError, match="complete run projection"):
        repo.finish_capture_business(
            run_id=acquired.run_id,
            owner_token=acquired.owner_token,
            status=ZsxqRunStatus.FAILED.value,
            changed_count=0,
            finished_at=now + timedelta(seconds=1),
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            business_json=incomplete,
        )

    assert repo.get_run(acquired.run_id)["status"] == "running"
    assert (
        repo.read_capture_ingest(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
        ).phase
        == "CLAIMED"
    )
    repo.close()


def test_capture_business_failure_reason_is_bound_to_failed_status(tmp_path):
    repo = ScraperRuntimeRepository(str(tmp_path / "ledger.sqlite3"))
    artifact_run_id = "91919191-9191-4919-8919-919191919191"
    content_sha256 = "9" * 64
    repo.claim_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        prior_g_json="{}",
    )
    now = datetime.fromisoformat(_BASE_ISO)
    acquired = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.MANUAL.value,
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )
    projection = json.loads(
        _terminal_business_json(
            acquired,
            status=ZsxqRunStatus.SUCCEEDED.value,
            changed_count=1,
            finished_at=now + timedelta(seconds=1),
        )
    )
    projection["failure_reason"] = "unknown"

    with pytest.raises(ValueError, match="complete run projection"):
        repo.finish_capture_business(
            run_id=acquired.run_id,
            owner_token=acquired.owner_token,
            status=ZsxqRunStatus.SUCCEEDED.value,
            changed_count=1,
            finished_at=now + timedelta(seconds=1),
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            business_json=json.dumps(projection, sort_keys=True, separators=(",", ":")),
        )

    failed_without_reason = json.loads(
        _terminal_business_json(
            acquired,
            status=ZsxqRunStatus.FAILED.value,
            changed_count=0,
            finished_at=now + timedelta(seconds=1),
        )
    )
    failed_without_reason.pop("failure_reason")
    with pytest.raises(ValueError, match="complete run projection"):
        repo.finish_capture_business(
            run_id=acquired.run_id,
            owner_token=acquired.owner_token,
            status=ZsxqRunStatus.FAILED.value,
            changed_count=0,
            finished_at=now + timedelta(seconds=1),
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            business_json=json.dumps(
                failed_without_reason,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    assert repo.get_run(acquired.run_id)["status"] == "running"
    repo.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "succeeded", "changed_count": 0},
        {"status": "no_change", "changed_count": 1},
        {"status": "failed", "changed_count": 1, "failure_reason": "unknown"},
        {
            "status": "succeeded",
            "changed_count": 1,
            "started_at": "2026-07-11T09:00:02+00:00",
            "finished_at": "2026-07-11T09:00:01+00:00",
        },
        {"status": "succeeded", "changed_count": 1, "finished_at": "not-a-time"},
    ],
)
def test_capture_business_projection_rejects_impossible_terminal_facts(overrides):
    from fin_analyse.scraper.runtime_repository import decode_capture_business_projection

    run_id = "r00000001-0123456789abcdef0123456789abcdef"
    projection = {
        "attempt": 1,
        "changed_count": 1,
        "coalesced": False,
        "finished_at": "2026-07-11T09:00:01+00:00",
        "intent": "sync",
        "request_id": "request-1",
        "run_id": run_id,
        "started_at": "2026-07-11T09:00:00+00:00",
        "status": "succeeded",
        "trigger": "manual",
    }
    projection.update(overrides)

    with pytest.raises(ValueError, match="complete run projection"):
        decode_capture_business_projection(
            json.dumps(projection, sort_keys=True, separators=(",", ":")),
            ingest_run_id=run_id,
        )


@pytest.mark.parametrize(
    "status",
    [ZsxqRunStatus.PARTIAL.value, ZsxqRunStatus.INTERRUPTED.value],
)
def test_capture_business_refuses_unrecoverable_terminal_status(tmp_path, status):
    repo = ScraperRuntimeRepository(str(tmp_path / f"{status}.sqlite3"))
    artifact_run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    content_sha256 = "a" * 64
    repo.claim_capture_ingest(
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        prior_g_json="{}",
    )
    now = datetime.fromisoformat(_BASE_ISO)
    acquired = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.MANUAL.value,
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )
    business_json = _terminal_business_json(
        acquired,
        status=status,
        changed_count=0,
        finished_at=now + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="supported capture terminal status"):
        repo.finish_capture_business(
            run_id=acquired.run_id,
            owner_token=acquired.owner_token,
            status=status,
            changed_count=0,
            finished_at=now + timedelta(seconds=1),
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            business_json=business_json,
        )

    assert repo.get_run(acquired.run_id)["status"] == "running"
    assert repo.get_active_lease()["run_id"] == acquired.run_id
    assert (
        repo.read_capture_ingest(
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
        ).phase
        == "CLAIMED"
    )
    repo.close()


@pytest.mark.parametrize(
    ("version", "factory"),
    [(1, _make_exact_v1_ledger), (2, _make_exact_v2_ledger)],
)
def test_exact_legacy_ordinary_open_is_refused_unchanged(tmp_path, version, factory):
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / f"exact-v{version}.sqlite3")
    factory(db_path)
    before = _v1_fingerprint(db_path) if version == 1 else _b0b_ledger_snapshot(db_path)

    with pytest.raises(SchemaVersionError, match="explicit operator migration"):
        ScraperRuntimeRepository(db_path)

    after = _v1_fingerprint(db_path) if version == 1 else _b0b_ledger_snapshot(db_path)
    assert after == before


def test_v2_input_requires_explicit_operator_and_stays_unchanged(tmp_path):
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "v2-mutated.sqlite3")
    _make_exact_v2_ledger(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE health_observations ADD COLUMN reason_code TEXT")
    conn.commit()
    before = _b0b_ledger_snapshot(db_path, conn)
    conn.close()

    with pytest.raises(SchemaVersionError, match="explicit operator migration"):
        ScraperRuntimeRepository(db_path)

    assert _b0b_ledger_snapshot(db_path) == before


def test_nonempty_v1_fails_closed_and_unchanged(tmp_path):
    """A v1 ledger with an existing run must NOT migrate; it fails closed unchanged."""
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    _make_exact_v1_ledger(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO runs (run_id, trigger, status, started_at) VALUES (?, ?, ?, ?)",
        ("r1", "incremental", "succeeded", _BASE_ISO),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    # Logically unchanged: still v1 tables and version 1 with the run intact.
    conn = sqlite3.connect(db_path)
    assert _app_tables(conn) == {"schema_version", "runs", "active_lease"}
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_active_v1_lease_fails_closed_and_unchanged(tmp_path):
    """A v1 ledger holding an active lease must NOT migrate; it fails closed unchanged."""
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    _make_exact_v1_ledger(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO active_lease "
        "(id, run_id, owner_token, acquired_at, heartbeat_at, deadline_at) "
        "VALUES (1, ?, ?, ?, ?, ?)",
        ("r1", "tok", _BASE_ISO, _BASE_ISO, _BASE_ISO),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    conn = sqlite3.connect(db_path)
    assert _app_tables(conn) == {"schema_version", "runs", "active_lease"}
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM active_lease").fetchone()[0] == 1
    conn.close()


def test_malformed_v1_shape_fails_closed_and_unchanged(tmp_path):
    """A v1-version ledger with an unexpected column shape fails closed unchanged.

    The version row says 1, but ``runs`` is missing the ``trigger`` column, so the
    shape is not the exact known v1 shape and must not migrate.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_version ("
        " id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE runs (seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE active_lease ("
        " id INTEGER PRIMARY KEY CHECK (id = 1),"
        " run_id TEXT NOT NULL, owner_token TEXT NOT NULL,"
        " acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, deadline_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
    conn.commit()
    before_tables = _app_tables(conn)
    conn.close()

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    conn = sqlite3.connect(db_path)
    assert _app_tables(conn) == before_tables
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    conn.close()


def test_unexpected_extra_table_v1_shape_fails_closed(tmp_path):
    """A v1-version ledger carrying an unexpected extra table fails closed unchanged."""
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    _make_exact_v1_ledger(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE surprise (x INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    conn = sqlite3.connect(db_path)
    assert "surprise" in _app_tables(conn)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    conn.close()


def test_existing_v2_is_exact_shape_validated_fail_closed(tmp_path):
    """A version-2 ledger missing the v2 tables fails closed (exact-shape validation)."""
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    # Claims version 2 but only carries the v1-era tables — not the exact v2 shape.
    _make_exact_v1_ledger(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE schema_version SET version = 2")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)


def test_schema_v4_exact_tables_reopen_and_future_version_fails_closed(tmp_path):
    """Schema v4 reopens idempotently and rejects a future version."""
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")

    repo = ScraperRuntimeRepository(db_path)
    expected_tables = _V2_APP_TABLES | {"capture_ingests"}
    assert _app_tables(repo._conn) == expected_tables
    versions = [r[0] for r in repo._conn.execute("SELECT version FROM schema_version").fetchall()]
    assert versions == [SCHEMA_VERSION]
    repo.close()

    # Idempotent reopen: no extra tables, singleton schema-version row preserved.
    repo2 = ScraperRuntimeRepository(db_path)
    assert _app_tables(repo2._conn) == expected_tables
    count = repo2._conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == 1
    repo2.close()

    # An unknown future version must fail closed on open (no silent downgrade).
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 999,))
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)


def test_mixed_multi_row_schema_version_fails_closed(tmp_path):
    """A corrupted multi-row version table fails closed instead of picking one via LIMIT 1."""
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)


def test_open_future_schema_rejects_before_v1_ddl_and_leaves_state_unchanged(tmp_path):
    """Opening an unknown future schema rejects before any v1 DDL runs; state is untouched."""
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    # A future-schema ledger that has ONLY the version table — no v1 runs/lease tables.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO schema_version (id, version) VALUES (1, ?)", (SCHEMA_VERSION + 999,))
    conn.commit()
    before_tables = _app_tables(conn)
    before_versions = [r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()]
    conn.close()

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    conn = sqlite3.connect(db_path)
    after_tables = _app_tables(conn)
    after_versions = [r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()]
    conn.close()

    # No v1 DDL ran: the application-table set and version rows are logically unchanged.
    assert before_tables == {"schema_version"}
    assert after_tables == before_tables
    assert after_versions == before_versions == [SCHEMA_VERSION + 999]


def test_weak_singleton_schema_version_fails_closed_before_v1_ddl(tmp_path):
    """A schema_version lacking the DB-enforced singleton CHECK fails closed pre-DDL.

    The version row is correct, so the version check alone would accept it and the
    IF NOT EXISTS v1 DDL would silently keep the guardless table. The singleton
    probe (INSERT id=2 rolled back inside a SAVEPOINT) must reject it before any
    runs/active_lease DDL, leaving the weak table and its row untouched.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version (id, version) VALUES (1, ?)", (SCHEMA_VERSION,))
    conn.commit()
    before_tables = _app_tables(conn)
    before_versions = [r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()]
    conn.close()

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    conn = sqlite3.connect(db_path)
    after_tables = _app_tables(conn)
    after_versions = [r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()]
    # The probe INSERT (id=2) was rolled back, so a genuine id=2 insert still
    # succeeds — proof the weak table is unchanged and no id=2 row leaked.
    conn.execute("INSERT INTO schema_version (id, version) VALUES (2, ?)", (SCHEMA_VERSION,))
    conn.commit()
    conn.close()

    assert before_tables == {"schema_version"}
    assert after_tables == before_tables
    assert after_versions == before_versions == [SCHEMA_VERSION]


def test_schema_version_row_is_database_enforced_singleton(tmp_path):
    """A second version row (id=2) is rejected by the CHECK (id = 1) constraint itself."""
    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)

    # A second logical version row (id=2) must be refused by the singleton CHECK
    # constraint — the guard is DB-enforced, not merely observed as one row.
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        repo._conn.execute(
            "INSERT INTO schema_version (id, version) VALUES (2, ?)", (SCHEMA_VERSION,)
        )
    assert excinfo.value.sqlite_errorname == "SQLITE_CONSTRAINT_CHECK"
    assert repo._conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    repo.close()


def test_concurrent_fresh_open_initializes_single_version_row(tmp_path):
    """Concurrent fresh OS-process opens converge on exactly one version row; all exit 0."""
    db_path = str(tmp_path / "ledger.sqlite3")
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    q = ctx.Queue()
    procs = [ctx.Process(target=_worker_open_repo, args=(db_path, ready, q)) for _ in range(6)]
    for p in procs:
        p.start()
    ready.set()  # release every open at once to force the initialization race
    items = _drain(q, len(procs))
    for p in procs:
        p.join(30)
        assert not p.is_alive()
        assert p.exitcode == 0

    for item in items:
        assert "error" not in item, item.get("error")
        assert item["rows"] == 1

    # Ground truth: the database holds exactly one version row.
    repo = ScraperRuntimeRepository(db_path)
    rows = repo._conn.execute("SELECT version FROM schema_version").fetchall()
    assert len(rows) == 1
    assert int(rows[0][0]) == SCHEMA_VERSION
    repo.close()


def test_repository_is_not_a_public_package_seam():
    """ScraperRuntimeRepository stays a private implementation, not a package export."""
    import fin_analyse.scraper as pkg

    assert "ScraperRuntimeRepository" not in pkg.__all__
    assert not hasattr(pkg, "ScraperRuntimeRepository")
    # The module facade and stable contracts remain public.
    assert "ZsxqScraperModule" in pkg.__all__


# ── B0a1: fail-closed fresh-database preflight ────────────────────────


def _logical_ledger_snapshot(db_path: str, sentinel_table: str | None = None):
    """Snapshot the ledger's logical state: ordered sqlite_master rows + sentinel rows.

    Captures every non-internal schema object (deterministically ordered) and, when
    given, the rows of a sentinel table, so a fail-closed open can be proven to leave
    the on-disk database logically identical (nothing created, nothing dropped, no
    data touched).
    """
    conn = sqlite3.connect(db_path)
    try:
        master = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        ).fetchall()
        sentinel = (
            conn.execute(f"SELECT * FROM {sentinel_table} ORDER BY rowid").fetchall()
            if sentinel_table is not None
            else []
        )
    finally:
        conn.close()
    return [tuple(row) for row in master], [tuple(row) for row in sentinel]


def _seed_preexisting_table(conn: sqlite3.Connection) -> str:
    conn.execute("CREATE TABLE legacy_notes (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO legacy_notes (id, note) VALUES (1, 'sentinel')")
    return "legacy_notes"


def _seed_preexisting_index(conn: sqlite3.Connection) -> str:
    _seed_preexisting_table(conn)  # an index requires a table to attach to
    conn.execute("CREATE INDEX legacy_notes_note_idx ON legacy_notes (note)")
    return "legacy_notes"


def _seed_preexisting_view(conn: sqlite3.Connection) -> str:
    _seed_preexisting_table(conn)  # a view requires an underlying table
    conn.execute("CREATE VIEW legacy_notes_view AS SELECT note FROM legacy_notes")
    return "legacy_notes"


def _seed_preexisting_trigger(conn: sqlite3.Connection) -> str:
    _seed_preexisting_table(conn)  # a trigger requires a table to fire on
    conn.execute("CREATE TABLE legacy_audit (seq INTEGER PRIMARY KEY, note TEXT)")
    conn.execute(
        "CREATE TRIGGER legacy_notes_ai AFTER INSERT ON legacy_notes "
        "BEGIN INSERT INTO legacy_audit (note) VALUES (NEW.note); END"
    )
    return "legacy_notes"


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(_seed_preexisting_table, id="table"),
        pytest.param(_seed_preexisting_index, id="index"),
        pytest.param(_seed_preexisting_view, id="view"),
        pytest.param(_seed_preexisting_trigger, id="trigger"),
    ],
)
def test_fresh_preflight_rejects_preexisting_user_object_unchanged(tmp_path, seed):
    """A database with no schema_version but a pre-existing user object is NOT fresh.

    The fresh-database branch keys solely off the presence of a ``schema_version``
    table, so a database carrying any other user object (table/index/view/trigger)
    but no version table is wrongly treated as fresh and has the v2 schema layered
    on top. A fail-closed preflight must instead refuse the open with
    ``SchemaVersionError`` and leave the on-disk state logically unchanged, adding
    none of the six v2 application tables.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    conn = sqlite3.connect(db_path)
    sentinel_table = seed(conn)
    conn.commit()
    conn.close()

    before = _logical_ledger_snapshot(db_path, sentinel_table)

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _logical_ledger_snapshot(db_path, sentinel_table)
    assert after == before
    after_names = {name for (_type, name, *_rest) in after[0]}
    assert not (_V2_APP_TABLES & after_names)


def test_fresh_preflight_rejects_empty_schema_version_unchanged(tmp_path):
    """A schema_version table holding zero rows must fail closed, unchanged.

    An empty version table is a corrupt/partial ledger, not a fresh database. The
    open must raise ``SchemaVersionError`` and leave the on-disk state logically
    identical, creating none of the other five v2 application tables.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_version ("
        " id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()

    before = _logical_ledger_snapshot(db_path)

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _logical_ledger_snapshot(db_path)
    assert after == before
    after_names = {name for (_type, name, *_rest) in after[0]}
    assert not (_V2_APP_TABLES - {"schema_version"}) & after_names


# ── B0: intent/trigger request contract ───────────────────────────────


def test_request_defaults_to_a_valid_intent_and_trigger():
    """The default request is a valid sync/schedule request and is JSON-safe."""
    req = ZsxqRunRequest()
    assert req.intent == ZsxqRunIntent.SYNC.value
    assert req.trigger == ZsxqRunTrigger.SCHEDULE.value
    wire = req.to_dict()
    # intent and trigger are carried as separate JSON-safe primitives.
    assert wire["intent"] == ZsxqRunIntent.SYNC.value
    assert wire["trigger"] == ZsxqRunTrigger.SCHEDULE.value
    import json

    assert json.loads(json.dumps(wire)) == wire
    # There is no combined/legacy trigger encoding and no time-window override.
    assert "mode" not in wire
    assert "window_days" not in wire


@pytest.mark.parametrize(
    "intent, trigger",
    [
        ("incremental", "schedule"),  # legacy combined trigger value as intent
        ("priority", "manual"),  # legacy combined trigger value as intent
        ("keepalive", "schedule"),  # arbitrary mode
        ("sync", "incremental"),  # legacy value in the trigger slot
        ("sync", "keepalive"),  # arbitrary trigger
        ("", "schedule"),  # empty intent
        ("sync", ""),  # empty trigger
    ],
)
def test_request_rejects_legacy_or_invalid_intent_or_trigger(intent, trigger):
    """Legacy combined triggers and arbitrary intent/trigger values are rejected."""
    with pytest.raises(ValueError):
        ZsxqRunRequest(intent=intent, trigger=trigger)


def test_request_persists_intent_and_trigger_separately(tmp_path):
    """A run persists intent and trigger as two separate ledger columns."""
    db_path = str(tmp_path / "ledger.sqlite3")
    module, repo = _build_module(db_path)

    result = module.run(
        ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value, trigger=ZsxqRunTrigger.MANUAL.value)
    )

    stored = repo.get_run(result.run_id)
    assert stored["intent"] == ZsxqRunIntent.SYNC.value
    assert stored["trigger"] == ZsxqRunTrigger.MANUAL.value
    # The result echoes both separately too.
    assert result.intent == ZsxqRunIntent.SYNC.value
    assert result.trigger == ZsxqRunTrigger.MANUAL.value
    repo.close()


# ── B0: adapter routing is by intent only ─────────────────────────────


class _ModeRecordingAdapter:
    """Records the ``mode`` the module hands the adapter for each run."""

    def __init__(self) -> None:
        self.modes: list[str] = []

    def run_incremental(self, *, mode, deadline_at, checkpoint) -> ReconcileOutcome:
        self.modes.append(mode)
        checkpoint()
        return ReconcileOutcome(changed_count=0)


@pytest.mark.parametrize(
    "intent",
    [ZsxqRunIntent.SYNC.value, ZsxqRunIntent.WATCH.value],
)
def test_sync_and_watch_route_solely_by_intent(tmp_path, intent):
    """The adapter mode is the request intent, independent of the trigger value."""
    db_path = str(tmp_path / "ledger.sqlite3")
    adapter = _ModeRecordingAdapter()
    repo = ScraperRuntimeRepository(db_path)
    module = ZsxqScraperModule(repository=repo, adapter=adapter, clock=_MutableClock())

    # The trigger is deliberately varied to prove it does NOT influence routing.
    for trig in (ZsxqRunTrigger.SCHEDULE.value, ZsxqRunTrigger.MANUAL.value):
        module.run(ZsxqRunRequest(intent=intent, trigger=trig))

    assert adapter.modes == [intent, intent]
    repo.close()


# ── B0: v2 probe lease (owner_kind = run | probe) ─────────────────────


def test_run_coalesces_on_probe_lease_without_creating_a_run(tmp_path):
    """A run meeting a fresh probe lease returns sparse COALESCED and creates no run."""
    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    base = datetime.fromisoformat(_BASE_ISO)

    # A live probe holds the single lease (owner_kind=probe, run_id NULL).
    probe = repo.acquire_probe_lease(
        now=base,
        deadline_at=base + timedelta(seconds=120),
        stale_before=base - timedelta(seconds=120),
    )
    assert probe.acquired is True

    module = ZsxqScraperModule(repository=repo, adapter=_FakeAdapter(), clock=lambda: base)
    result = module.run(ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value))

    assert result.status == ZsxqRunStatus.COALESCED.value
    # Sparse: no fabricated active_run_id when the owner is a probe, no run created.
    assert result.active_run_id in (None, "")
    assert "active_run_id" not in result.to_dict()
    assert repo._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    repo.close()


def test_stale_probe_reclaim_creates_no_interrupted_or_fake_run(tmp_path):
    """Reclaiming a stale probe lease replaces the lease; it never creates/interrupts a run."""
    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    base = datetime.fromisoformat(_BASE_ISO)

    # A stale probe lease left behind (heartbeat far in the past).
    probe = repo.acquire_probe_lease(
        now=base,
        deadline_at=base + timedelta(seconds=120),
        stale_before=base - timedelta(seconds=120),
    )
    assert probe.acquired is True

    # A real run arrives well past the stale window and reclaims the probe lease.
    later = base + timedelta(seconds=10_000)
    module = ZsxqScraperModule(
        repository=repo, adapter=_FakeAdapter(), clock=lambda: later, stale_after_seconds=120.0
    )
    result = module.run(ZsxqRunRequest(intent=ZsxqRunIntent.SYNC.value))

    # The run acquired cleanly (no coalesce onto a dead probe).
    assert result.status == ZsxqRunStatus.NO_CHANGE.value
    assert result.run_id
    # Exactly one run exists, and it is NOT interrupted — no fake run was fabricated
    # for the reclaimed probe.
    rows = repo._conn.execute("SELECT run_id, status FROM runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == ZsxqRunStatus.NO_CHANGE.value
    assert repo.get_active_lease() is None
    repo.close()


def test_fenced_probe_release_cannot_delete_another_owners_lease(tmp_path):
    """A probe release is fenced by its owner token; it cannot delete another lease."""
    from fin_analyse.scraper.runtime_repository import LeaseLostError

    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    base = datetime.fromisoformat(_BASE_ISO)

    probe = repo.acquire_probe_lease(
        now=base,
        deadline_at=base + timedelta(seconds=120),
        stale_before=base - timedelta(seconds=120),
    )
    assert probe.acquired is True

    # A stale foreign token must not delete the current probe's lease.
    with pytest.raises(LeaseLostError):
        repo.release_probe_lease(owner_token="not-the-owner")

    lease = repo.get_active_lease()
    assert lease is not None
    assert lease["owner_kind"] == "probe"

    # The genuine owner releases cleanly.
    repo.release_probe_lease(owner_token=probe.owner_token)
    assert repo.get_active_lease() is None
    repo.close()


# ── B0a2: exact-v2 SQLite shape + DB-enforced critical constraints ────
#
# B0a1 proved fail-closed handling of a *missing/partial* fresh ledger. B0a2
# tightens the reopen path: an existing (or freshly created) version-2 ledger
# must carry the EXACT approved SQLite shape AND its critical constraints must be
# enforced by SQLite itself, not merely asserted in Python. The current
# ``_validate_shape`` only compares the application-table set and each table's
# *column names*; it never inspects column type/NOT NULL/default/PK semantics,
# UNIQUE/AUTOINCREMENT, extra user objects, or CHECK text. These RED tests pin
# every such gap by mutating a single fragment of the canonical v2 DDL and
# proving the reopen is refused fail-closed and unchanged.
#
# To avoid duplicating the full six-table DDL, the canonical table SQL is derived
# from a throwaway fresh v2 ledger; each test mutates exactly one fragment.


def _canonical_v2_table_sql(tmp_path) -> dict[str, str]:
    """Return the canonical v2 ``CREATE TABLE`` SQL per table from a fresh ledger.

    A throwaway v2 ledger is created and its ``sqlite_master`` table SQL is read
    back verbatim, so the tests never re-spell the production DDL — they mutate a
    single fragment of the real thing.
    """
    seed_path = str(tmp_path / "canonical_v2.sqlite3")
    ScraperRuntimeRepository(seed_path).close()
    conn = sqlite3.connect(seed_path)
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        conn.close()
    return dict(rows)


def _v2_shape_fingerprint(db_path: str):
    """A complete logical fingerprint of a v2 ledger's schema + enforced shape.

    For each application table it captures:

    * the ordered, *full* ``PRAGMA table_xinfo`` rows
      ``(cid, name, type, notnull, dflt_value, pk, hidden)`` — every column
      attribute, not just the name;
    * a name-independent semantic index signature: for every index a tuple of its
      ``PRAGMA index_list`` ``(unique, origin, partial)`` together with the
      complete ``PRAGMA index_xinfo`` rows, collected into a stable sorted set.
      Raw autoindex names/order are deliberately excluded from this semantic
      signature so it is a valid *cross-database* oracle (item 6); the ordered
      ``sqlite_master`` objects (which do carry names) are retained only for
      before/after snapshots of the *same* database.

    It also records the ordered ``schema_version`` ``(id, version)`` rows.

    Any structural mutation — type, NOT NULL, default, PK, UNIQUE, AUTOINCREMENT,
    an added/removed index (implicit or explicit), an extra view/trigger, altered
    CHECK text, or a changed singleton row — changes the fingerprint. Two
    fingerprints comparing equal proves a fail-closed open left the on-disk schema
    logically untouched.
    """
    conn = sqlite3.connect(db_path)
    try:
        master = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        ).fetchall()
        tables = [name for (typ, name, *_rest) in master if typ == "table"]
        semantic: dict[str, dict] = {}
        for table in tables:
            xinfo = [tuple(r) for r in conn.execute(f"PRAGMA table_xinfo({table})")]
            index_sigs = []
            for idx in conn.execute(f"PRAGMA index_list({table})"):
                idx_name, unique, origin, partial = idx[1], idx[2], idx[3], idx[4]
                idx_cols = tuple(
                    tuple(c) for c in conn.execute(f"PRAGMA index_xinfo('{idx_name}')")
                )
                # Name-independent: the autoindex name is NOT part of the signature.
                index_sigs.append((unique, origin, partial, idx_cols))
            semantic[table] = {
                "xinfo": xinfo,
                "indexes": sorted(index_sigs, key=repr),
            }
        schema_version_rows = [
            tuple(r) for r in conn.execute("SELECT id, version FROM schema_version ORDER BY id")
        ]
    finally:
        conn.close()
    return {
        "master": [tuple(row) for row in master],
        "schema_version_rows": schema_version_rows,
        "semantic": semantic,
    }


def _sub_once(sql: str, pattern: str, repl: str) -> str:
    """Replace the single occurrence of ``pattern`` in ``sql`` (else assert).

    Substitutes ALL matches (no ``count`` limit) and asserts the total replacement
    count is exactly one, so the claim of a unique source fragment is genuinely
    verified: a pattern that matches twice fails loudly instead of silently editing
    only the first. The result must also differ from the input.
    """
    new_sql, count = re.subn(pattern, repl, sql)
    assert count == 1, f"fragment {pattern!r} matched {count} times, expected exactly 1"
    assert new_sql != sql, f"fragment {pattern!r} produced no change"
    return new_sql


def _rebuild_v2_with_mutated_table(
    tmp_path,
    *,
    table: str,
    pattern: str,
    repl: str,
    extra_object_sql: str | None = None,
) -> str:
    """Build an otherwise-exact v2 ledger with one table's DDL mutated in place.

    A fresh v2 ledger is created, then the single target table is dropped and
    recreated from its canonical ``sqlite_master`` SQL with exactly one fragment
    mutated. The ``schema_version`` row is preserved/reseeded to version 2 so the
    version check passes and the *shape* validation is what the reopen must catch.
    ``extra_object_sql`` optionally adds one unexpected user object.
    """
    canonical = _canonical_v2_table_sql(tmp_path)
    mutated_sql = _sub_once(canonical[table], pattern, repl)

    db_path = str(tmp_path / "mutated_v2.sqlite3")
    ScraperRuntimeRepository(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"DROP TABLE {table}")
        conn.execute(mutated_sql)
        # Re-seed the singleton version row dropped with the table (if any).
        if table == "schema_version":
            conn.execute(
                "INSERT INTO schema_version (id, version) VALUES (1, ?)", (SCHEMA_VERSION,)
            )
        if extra_object_sql is not None:
            conn.execute(extra_object_sql)
        conn.commit()
    finally:
        conn.close()

    # Single-cause guard: prove the ledger differs from canonical ONLY in the
    # target table's DDL. Rather than requiring exactly one fingerprint leaf to
    # change (a single UNIQUE/PK edit legitimately alters an implicit autoindex
    # too), assert that every *other* invariant is untouched:
    #   * schema_version rows equal the canonical singleton, and
    #   * every non-target table's name-independent semantic signature is equal,
    #   * while the target table's stored sqlite_master SQL equals the exactly-once
    #     mutation result (so the change is precisely the intended fragment).
    if extra_object_sql is None:
        canonical_ref_path = str(tmp_path / "canonical_ref_v2.sqlite3")
        ScraperRuntimeRepository(canonical_ref_path).close()
        canonical_fp = _v2_shape_fingerprint(canonical_ref_path)
        mutated_fp = _v2_shape_fingerprint(db_path)
        assert mutated_fp["schema_version_rows"] == canonical_fp["schema_version_rows"]
        for name, sig in canonical_fp["semantic"].items():
            if name == table:
                continue
            assert mutated_fp["semantic"][name] == sig, f"non-target table {name!r} changed"
        target_sql = next(
            sql for (typ, nm, _tbl, sql) in mutated_fp["master"] if typ == "table" and nm == table
        )
        assert target_sql == mutated_sql, "target sqlite_master SQL != mutation result"
    return db_path


def _add_extra_object_to_fresh_v2(tmp_path, extra_object_sql: str) -> str:
    """Create an exact fresh v2 ledger and add exactly one unexpected user object."""
    db_path = str(tmp_path / "extra_object_v2.sqlite3")
    ScraperRuntimeRepository(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(extra_object_sql)
        conn.commit()
    finally:
        conn.close()
    return db_path


# (1) same table/column names but mutated table_xinfo semantics.
_MUTATED_XINFO_CASES = [
    pytest.param("runs", r"attempt(\s+)INTEGER", r"attempt\1TEXT", id="runs_attempt_type"),
    pytest.param(
        "runs", r"intent(\s+)TEXT NOT NULL", r"intent\1TEXT", id="runs_intent_not_null_dropped"
    ),
    pytest.param("runs", r"DEFAULT 1", "DEFAULT 5", id="runs_attempt_default_changed"),
    pytest.param(
        "health_episodes",
        r"episode_id(\s+)TEXT PRIMARY KEY",
        r"episode_id\1TEXT",
        id="health_episodes_pk_dropped",
    ),
    pytest.param(
        "health_observations",
        r"reason_code(\s+)TEXT NOT NULL",
        r"reason_code\1TEXT",
        id="health_observations_reason_not_null_dropped",
    ),
]


@pytest.mark.parametrize("table, pattern, repl", _MUTATED_XINFO_CASES)
def test_v2_reopen_rejects_mutated_column_semantics_unchanged(tmp_path, table, pattern, repl):
    """A v2 ledger with the exact names but mutated column semantics fails closed.

    Same table/column *names* as the approved v2 shape, but a mutated
    type / NOT NULL / default / PRIMARY KEY. The current name-only shape check
    accepts these; a database-shape check must refuse the open with
    ``SchemaVersionError`` and leave the on-disk schema logically unchanged.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = _rebuild_v2_with_mutated_table(tmp_path, table=table, pattern=pattern, repl=repl)
    before = _v2_shape_fingerprint(db_path)

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _v2_shape_fingerprint(db_path)
    assert after == before


# (2) removal of a critical UNIQUE guard.
_REMOVED_UNIQUE_CASES = [
    pytest.param(
        "runs", r"run_id(\s+)TEXT UNIQUE", r"run_id\1TEXT", id="runs_run_id_unique_removed"
    ),
    pytest.param(
        "scraper_outbox",
        r"dedupe_key(\s+)TEXT NOT NULL UNIQUE",
        r"dedupe_key\1TEXT NOT NULL",
        id="outbox_dedupe_key_unique_removed",
    ),
]


@pytest.mark.parametrize("table, pattern, repl", _REMOVED_UNIQUE_CASES)
def test_v2_reopen_rejects_removed_unique_guard_unchanged(tmp_path, table, pattern, repl):
    """Dropping ``runs.run_id`` or ``scraper_outbox.dedupe_key`` UNIQUE fails closed.

    These UNIQUE guards are the module's cross-process identity/dedupe invariants.
    A v2 ledger that keeps the column names but drops the UNIQUE constraint must be
    refused, and the on-disk schema must stay logically unchanged.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = _rebuild_v2_with_mutated_table(tmp_path, table=table, pattern=pattern, repl=repl)
    before = _v2_shape_fingerprint(db_path)

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _v2_shape_fingerprint(db_path)
    assert after == before


# (3) removal of AUTOINCREMENT from an approved sequence table.
_REMOVED_AUTOINCREMENT_TABLES = [
    pytest.param("runs", id="runs_autoincrement_removed"),
    pytest.param("health_observations", id="health_observations_autoincrement_removed"),
    pytest.param("scraper_outbox", id="scraper_outbox_autoincrement_removed"),
]


@pytest.mark.parametrize("table", _REMOVED_AUTOINCREMENT_TABLES)
def test_v2_reopen_rejects_removed_autoincrement_unchanged(tmp_path, table):
    """Dropping AUTOINCREMENT from an approved sequence table fails closed.

    ``runs.seq`` in particular must be a monotonic AUTOINCREMENT sequence (run ids
    are built from it); a plain ``INTEGER PRIMARY KEY`` reuses rowids and breaks the
    invariant. Reopening such a ledger must be refused, unchanged.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = _rebuild_v2_with_mutated_table(
        tmp_path,
        table=table,
        pattern=r"PRIMARY KEY AUTOINCREMENT",
        repl="PRIMARY KEY",
    )
    before = _v2_shape_fingerprint(db_path)

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _v2_shape_fingerprint(db_path)
    assert after == before


# (4) an unexpected user index / view / trigger on an otherwise exact v2 ledger.
_UNEXPECTED_OBJECT_CASES = [
    pytest.param("CREATE INDEX extra_runs_status_idx ON runs (status)", id="unexpected_index"),
    pytest.param("CREATE VIEW extra_runs_view AS SELECT run_id FROM runs", id="unexpected_view"),
    pytest.param(
        "CREATE TRIGGER extra_runs_ai AFTER INSERT ON runs BEGIN SELECT 1; END",
        id="unexpected_trigger",
    ),
    pytest.param(
        "CREATE TABLE extra_notes (id INTEGER PRIMARY KEY, note TEXT)",
        id="unexpected_user_table",
    ),
]


@pytest.mark.parametrize("extra_object_sql", _UNEXPECTED_OBJECT_CASES)
def test_v2_reopen_rejects_unexpected_user_object_unchanged(tmp_path, extra_object_sql):
    """An otherwise-exact v2 ledger carrying an extra user object fails closed.

    The six approved tables (and their autoindexes) are the whole approved shape;
    an unexpected user index/view/trigger is a foreign object that must be refused,
    leaving the on-disk schema logically unchanged.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = _add_extra_object_to_fresh_v2(tmp_path, extra_object_sql)
    before = _v2_shape_fingerprint(db_path)

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _v2_shape_fingerprint(db_path)
    assert after == before


# (5) weakened critical CHECK text/semantics.
_WEAKENED_CHECK_CASES = [
    pytest.param(
        "schema_version",
        r"CHECK \(id = 1\)",
        "CHECK (id != 2)",
        id="schema_version_check_widened",
    ),
    pytest.param(
        "active_lease",
        r"(id\s+INTEGER PRIMARY KEY) CHECK \(id = 1\)",
        r"\1",
        id="active_lease_singleton_check_removed",
    ),
    pytest.param(
        "active_lease",
        r"owner_kind IN \('run', 'probe'\)",
        "owner_kind IN ('run', 'probe', 'ghost')",
        id="owner_kind_enumeration_widened",
    ),
    pytest.param(
        "active_lease",
        r"(owner_kind\s+TEXT NOT NULL) CHECK \(owner_kind IN \('run', 'probe'\)\)",
        r"\1",
        id="owner_kind_enumeration_removed",
    ),
]


@pytest.mark.parametrize("table, pattern, repl", _WEAKENED_CHECK_CASES)
def test_v2_reopen_rejects_weakened_check_unchanged(tmp_path, table, pattern, repl):
    """A v2 ledger with a weakened critical CHECK constraint fails closed.

    The ``schema_version``/``active_lease`` singleton guards and the
    ``owner_kind`` enumeration are database-enforced invariants. A ledger whose
    CHECK text is widened or dropped keeps the same column names but no longer
    enforces the invariant; the reopen must refuse it, unchanged.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = _rebuild_v2_with_mutated_table(tmp_path, table=table, pattern=pattern, repl=repl)
    before = _v2_shape_fingerprint(db_path)

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _v2_shape_fingerprint(db_path)
    assert after == before


# (6) an existing v2 ledger whose active_lease keeps the singleton + owner_kind
# enum guards but lacks ONLY the owner_kind/run_id relationship CHECK. The current
# production canonical DDL itself omits this relationship, so this cannot be built
# by mutating the canonical SQL (there is nothing to remove); one short explicit
# weak DDL fixture stands in for the pre-relationship shape.
_WEAK_ACTIVE_LEASE_DDL = (
    "CREATE TABLE active_lease (\n"
    "    id           INTEGER PRIMARY KEY CHECK (id = 1),\n"
    "    owner_kind   TEXT NOT NULL CHECK (owner_kind IN ('run', 'probe')),\n"
    "    run_id       TEXT,\n"
    "    owner_token  TEXT NOT NULL,\n"
    "    acquired_at  TEXT NOT NULL,\n"
    "    heartbeat_at TEXT NOT NULL,\n"
    "    deadline_at  TEXT NOT NULL\n"
    ")"
)


def _normalize_sql(sql: str) -> str:
    """Collapse whitespace runs to single spaces for whitespace-tolerant SQL compares."""
    return re.sub(r"\s+", " ", sql).strip()


def _rebuild_v2_with_weak_active_lease(tmp_path) -> str:
    """Build an otherwise-exact v2 ledger with the weak seven-column active_lease.

    A fresh v2 ledger is created, then ``active_lease`` alone is dropped and
    recreated from ``_WEAK_ACTIVE_LEASE_DDL`` — same seven columns, same ``id = 1``
    singleton and ``owner_kind`` enum guards, but no owner_kind/run_id relationship
    CHECK. Every other table and the ``schema_version`` singleton are untouched.

    Executable single-cause guards prove the fixture differs from a canonical fresh
    v2 ledger ONLY by omission of the approved owner_kind/run_id relationship CHECK,
    so future canonical DDL drift cannot make the reopen test pass for the wrong
    reason:

    * the canonical ``active_lease`` DDL, once the exact relationship CHECK the
      approved contract adds is removed (whitespace-tolerant, exactly one match, no
      SQL parser, no other CHECK touched), must be equivalent to
      ``_WEAK_ACTIVE_LEASE_DDL``. While production still omits that CHECK the
      canonical DDL is already equivalent, so no removal is required;
    * the built ledger's fingerprint must match a canonical fresh v2 on every
      non-``active_lease`` table, on the ``schema_version`` singleton rows, and on
      ``active_lease``'s columns and indexes — only the stored CHECK text may differ,
      and the stored weak ``active_lease`` SQL must equal ``_WEAK_ACTIVE_LEASE_DDL``.
    """
    weak_norm = _normalize_sql(_WEAK_ACTIVE_LEASE_DDL)

    # (1)/(2) The canonical active_lease DDL must reduce to the weak DDL by removing
    # ONLY the relationship CHECK. Pre-GREEN the canonical DDL already equals it; once
    # the approved contract adds exactly this table-level CHECK, stripping that one
    # clause (plus its attaching comma) — whitespace-tolerant, no SQL parser, no other
    # CHECK weakened — must reproduce the weak DDL, else the omission is not the only
    # difference and the fixture is refused fail-closed.
    canonical_lease_sql = _canonical_v2_table_sql(tmp_path)["active_lease"]
    if _normalize_sql(canonical_lease_sql) != weak_norm:
        relationship_check = (
            "CHECK ((owner_kind = 'run' AND run_id IS NOT NULL) "
            "OR (owner_kind = 'probe' AND run_id IS NULL))"
        )
        relationship_check_re = r",\s*" + r"\s+".join(
            re.escape(tok) for tok in relationship_check.split()
        )
        stripped, matches = re.subn(relationship_check_re, "", canonical_lease_sql)
        assert matches == 1, f"relationship CHECK matched {matches} times, expected exactly 1"
        assert _normalize_sql(stripped) == weak_norm, (
            "canonical active_lease DDL differs from the weak DDL by more than the "
            "owner_kind/run_id relationship CHECK"
        )

    # Build the weak ledger: only active_lease is dropped and recreated weak.
    db_path = str(tmp_path / "weak_lease_v2.sqlite3")
    ScraperRuntimeRepository(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE active_lease")
        conn.execute(_WEAK_ACTIVE_LEASE_DDL)
        conn.commit()
    finally:
        conn.close()

    # (3) The built ledger must differ from a canonical fresh v2 ONLY inside
    # active_lease's CHECK text: identical schema_version singleton rows, identical
    # semantic signature for every other table, identical active_lease columns and
    # indexes, and a stored active_lease SQL equivalent to the weak DDL. Any other
    # drift fails these guards.
    canonical_ref_path = str(tmp_path / "canonical_weak_ref_v2.sqlite3")
    ScraperRuntimeRepository(canonical_ref_path).close()
    canonical_fp = _v2_shape_fingerprint(canonical_ref_path)
    weak_fp = _v2_shape_fingerprint(db_path)
    assert weak_fp["schema_version_rows"] == canonical_fp["schema_version_rows"]
    for name, sig in canonical_fp["semantic"].items():
        if name == "active_lease":
            continue
        assert weak_fp["semantic"][name] == sig, f"non-target table {name!r} changed"
    weak_lease = weak_fp["semantic"]["active_lease"]
    canonical_lease = canonical_fp["semantic"]["active_lease"]
    assert weak_lease["xinfo"] == canonical_lease["xinfo"], "active_lease columns changed"
    assert weak_lease["indexes"] == canonical_lease["indexes"], "active_lease indexes changed"
    weak_lease_sql = next(
        sql for (typ, nm, _tbl, sql) in weak_fp["master"] if typ == "table" and nm == "active_lease"
    )
    assert _normalize_sql(weak_lease_sql) == weak_norm, "stored weak active_lease SQL != weak DDL"

    return db_path


def test_v2_reopen_rejects_weak_active_lease_relationship_unchanged(tmp_path):
    """A v2 ledger whose active_lease lacks only the relationship CHECK fails closed.

    The seven-column ``active_lease`` keeps the ``id = 1`` singleton and the
    ``owner_kind IN ('run','probe')`` enum, but omits the owner_kind/run_id
    relationship CHECK (a probe owns no run; a run must). Because the current
    production canonical DDL also omits that CHECK, this reopen is accepted today
    (intentional RED); once the approved v2 contract enforces the relationship, the
    same unchanged fixture must be refused with ``SchemaVersionError`` and leave the
    on-disk schema logically untouched.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = _rebuild_v2_with_weak_active_lease(tmp_path)
    before = _v2_shape_fingerprint(db_path)

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _v2_shape_fingerprint(db_path)
    assert after == before


def test_v2_reopen_accepts_whitespace_reformatted_active_lease_unchanged(tmp_path):
    """A v2 ledger whose active_lease SQL differs only in whitespace reopens intact.

    Positive control guarding against a byte-literal shape check: the approved v2
    contract is defined by the *logical* schema, not the exact ``sqlite_master`` SQL
    text. A ledger whose ``active_lease`` DDL is byte-for-byte different from the
    canonical form but semantically identical — only whitespace runs / indentation
    changed, no CHECK removed or weakened, no column altered — must still reopen
    successfully and leave the complete logical fingerprint unchanged. This stays a
    positive control before and after the owner_kind/run_id relationship CHECK is
    added, because the reformat is derived from whatever the canonical DDL is.
    """
    db_path = str(tmp_path / "reformatted_v2.sqlite3")
    ScraperRuntimeRepository(db_path).close()

    # Canonical active_lease SQL, verbatim from a fresh ledger's sqlite_master.
    canonical_sql = _canonical_v2_table_sql(tmp_path)["active_lease"]

    # Whitespace-only reformat: double every space run so the raw SQL bytes differ
    # while the token stream (and thus ``_normalize_sql``) is identical. No token,
    # CHECK or column is touched — only inter-token whitespace amount.
    reformatted_sql = re.sub(r" ", "  ", canonical_sql)
    assert reformatted_sql != canonical_sql, "reformat did not change the raw SQL"
    assert _normalize_sql(reformatted_sql) == _normalize_sql(canonical_sql), (
        "reformat changed more than whitespace"
    )

    # Drop/recreate ONLY active_lease from the whitespace-reformatted canonical SQL.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE active_lease")
        conn.execute(reformatted_sql)
        conn.commit()
    finally:
        conn.close()

    before = _v2_shape_fingerprint(db_path)

    # The reopen must accept the semantically identical ledger and not mutate it.
    ScraperRuntimeRepository(db_path).close()

    after = _v2_shape_fingerprint(db_path)
    assert after == before


def test_v2_reopen_rejects_sqlitex_internal_prefix_boundary_unchanged(tmp_path):
    """A user table named ``sqliteXuser_extra`` is NOT a true SQLite internal object.

    SQL ``LIKE`` treats ``_`` as a single-character wildcard, so a production
    object-inventory filter ``WHERE name NOT LIKE 'sqlite_%'`` hides a user table
    whose name starts with ``sqliteX`` — ``X`` matches the ``_`` pattern. The
    approved v2 contract requires a literal-prefix check: ``NOT GLOB 'sqlite_*'``
    (or an equivalent Python-level ``startswith('sqlite_')``) so that only real
    ``sqlite_``-prefixed internal objects are excluded. This test proves the
    production LIKE-wildcard blind spot fails closed as RED until the validator is
    patched.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "sqliteX_v2.sqlite3")
    ScraperRuntimeRepository(db_path).close()

    # Inject a user table whose name starts with four capital letters —
    # 'sqliteX' — but does NOT start with the literal string 'sqlite_'.
    extra_name = "sqliteXuser_extra"
    assert not extra_name.startswith("sqlite_"), "this is NOT a literal sqlite_ prefixed object"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"CREATE TABLE {extra_name} (id INTEGER PRIMARY KEY, sentinel TEXT NOT NULL)")
        conn.execute(f"INSERT INTO {extra_name} (id, sentinel) VALUES (1, 'p0-boundary-bug')")
        conn.commit()
    finally:
        conn.close()

    # Snapshot excluding ONLY literal-sqlite_-prefix objects — NEVER LIKE.
    def _snapshot_no_internals(path):
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        try:
            objects = frozenset(
                (r["type"], r["name"])
                for r in c.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
                ).fetchall()
            )
            row = c.execute(f"SELECT sentinel FROM {extra_name} WHERE id = 1").fetchone()
            # The sentinel row is part of the contract: it must survive unaltered.
            sentinel = row["sentinel"] if row is not None else None
        finally:
            c.close()
        return {"objects": objects, "sentinel": sentinel}

    before = _snapshot_no_internals(db_path)
    assert extra_name in {n for _t, n in before["objects"]}, (
        f"pre-open: {extra_name!r} must be visible to a literal-prefix snapshot"
    )
    assert "sqlite_sequence" not in {n for _t, n in before["objects"]}, (
        "sqlite_sequence must be excluded by the literal-prefix check"
    )
    assert before["sentinel"] == "p0-boundary-bug", "sentinel row must be present pre-open"

    # Reopening must reject a ledger carrying an unexpected user table. Currently
    # RED because the production ``LIKE 'sqlite_%'`` filter hides sqliteXuser_extra
    # from the object inventory — the open succeeds when it must fail.
    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _snapshot_no_internals(db_path)
    assert after == before, "fail-closed reopen must leave the on-disk schema logically unchanged"


# Direct fresh-v2 constraint enforcement — SQLite itself must reject bad rows.
def _fresh_v2_constraint_rejects(tmp_path, sql: str, params: tuple) -> str | None:
    """Attempt an INSERT on a fresh v2 ledger inside a rolled-back SAVEPOINT.

    Returns the ``sqlite_errorname`` when SQLite rejects the row with an
    ``IntegrityError``; returns ``None`` when the row is accepted (the bug). The
    probe is always rolled back so the ledger is never mutated.
    """
    db_path = str(tmp_path / "constraint_v2.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    try:
        repo._conn.execute("SAVEPOINT probe")
        try:
            repo._conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            return exc.sqlite_errorname
        finally:
            repo._conn.execute("ROLLBACK TO probe")
            repo._conn.execute("RELEASE probe")
        return None
    finally:
        repo.close()


_LEASE_COLS = "(id, owner_kind, run_id, owner_token, acquired_at, heartbeat_at, deadline_at)"
_FRESH_V2_CONSTRAINT_CASES = [
    pytest.param(
        "INSERT INTO schema_version (id, version) VALUES (2, ?)",
        (SCHEMA_VERSION,),
        id="schema_version_id_not_1",
    ),
    pytest.param(
        f"INSERT INTO active_lease {_LEASE_COLS} VALUES (1, 'run', NULL, 'tok', ?, ?, ?)",
        (_BASE_ISO, _BASE_ISO, _BASE_ISO),
        id="active_lease_owner_run_requires_run_id",
    ),
    pytest.param(
        f"INSERT INTO active_lease {_LEASE_COLS} VALUES (1, 'probe', 'r1', 'tok', ?, ?, ?)",
        (_BASE_ISO, _BASE_ISO, _BASE_ISO),
        id="active_lease_owner_probe_forbids_run_id",
    ),
    pytest.param(
        f"INSERT INTO active_lease {_LEASE_COLS} VALUES (1, 'ghost', 'r1', 'tok', ?, ?, ?)",
        (_BASE_ISO, _BASE_ISO, _BASE_ISO),
        id="active_lease_owner_kind_invalid",
    ),
    pytest.param(
        "INSERT INTO schema_version (id, version) VALUES (3, ?)",
        (SCHEMA_VERSION,),
        id="schema_version_id_3_rejected",
    ),
    pytest.param(
        f"INSERT INTO active_lease {_LEASE_COLS} VALUES (3, 'probe', NULL, 'tok', ?, ?, ?)",
        (_BASE_ISO, _BASE_ISO, _BASE_ISO),
        id="active_lease_id_3_rejected",
    ),
]


@pytest.mark.parametrize("sql, params", _FRESH_V2_CONSTRAINT_CASES)
def test_fresh_v2_database_rejects_constraint_violations(tmp_path, sql, params):
    """SQLite itself rejects rows that violate the approved v2 critical constraints.

    On a fresh, exact v2 ledger:
      * ``schema_version`` must reject any ``id != 1`` (singleton) — both ``id = 2``
        and ``id = 3`` are probed,
      * ``active_lease`` must reject any ``id != 1`` (singleton) even for an
        otherwise-valid probe row (``id = 3``, ``owner_kind='probe'``, NULL run_id),
      * ``active_lease`` must reject ``owner_kind='run'`` with a NULL ``run_id``,
      * ``active_lease`` must reject ``owner_kind='probe'`` with a non-NULL ``run_id``,
      * ``active_lease`` must reject an unknown ``owner_kind``.
    Each rejection must be a database ``IntegrityError`` (CHECK), not a Python-side
    assertion — the guard has to live in the schema.
    """
    errorname = _fresh_v2_constraint_rejects(tmp_path, sql, params)
    assert errorname == "SQLITE_CONSTRAINT_CHECK", (
        f"expected SQLite to reject the row with SQLITE_CONSTRAINT_CHECK, got {errorname!r}"
    )


# Positive controls: the *valid* owner_kind/run_id combinations must be accepted by
# SQLite itself (asserted on the database's own INSERT behavior, not Python-side
# validation). These stay green before and after the relationship CHECK is added.
_FRESH_V2_ACCEPTED_CASES = [
    pytest.param(
        f"INSERT INTO active_lease {_LEASE_COLS} VALUES (1, 'run', 'r1', 'tok', ?, ?, ?)",
        (_BASE_ISO, _BASE_ISO, _BASE_ISO),
        id="active_lease_owner_run_with_run_id_accepted",
    ),
    pytest.param(
        f"INSERT INTO active_lease {_LEASE_COLS} VALUES (1, 'probe', NULL, 'tok', ?, ?, ?)",
        (_BASE_ISO, _BASE_ISO, _BASE_ISO),
        id="active_lease_owner_probe_without_run_id_accepted",
    ),
]


@pytest.mark.parametrize("sql, params", _FRESH_V2_ACCEPTED_CASES)
def test_fresh_v2_database_accepts_valid_lease_rows(tmp_path, sql, params):
    """SQLite accepts the two *valid* lease shapes inside a rolled-back SAVEPOINT.

    ``owner_kind='run'`` with a non-NULL ``run_id`` and ``owner_kind='probe'`` with
    a NULL ``run_id`` are the legitimate leases; the database must accept both (the
    probe is always rolled back). These positive controls prove the relationship
    contract rejects only the invalid combinations, and they remain green once the
    relationship CHECK is added.
    """
    errorname = _fresh_v2_constraint_rejects(tmp_path, sql, params)
    assert errorname is None, (
        f"expected SQLite to accept the valid lease row, but it was rejected with {errorname!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 2B B0a3 — exact-v1 input fingerprint and concurrent-migration contracts
# ═══════════════════════════════════════════════════════════════════════════════

# Whitespace-only reformatted v1 DDL — logically identical to _EXACT_V1_TABLE_DDL
# but with different indentation (2-space vs 4-space).  SQLite preserves the
# original DDL text in sqlite_master, so raw stored SQL differs; after
# whitespace-normalization both are equal.
_WHITESPACE_REFORMATTED_V1_DDL: dict[str, str] = {
    "schema_version": (
        "CREATE TABLE schema_version (\n"
        "  id INTEGER PRIMARY KEY CHECK (id = 1),\n"
        "  version INTEGER NOT NULL\n"
        ")"
    ),
    "runs": (
        "CREATE TABLE runs (\n"
        "  seq INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "  run_id TEXT UNIQUE,\n"
        "  trigger TEXT NOT NULL,\n"
        "  status TEXT NOT NULL,\n"
        "  attempt INTEGER NOT NULL DEFAULT 1,\n"
        "  changed_count INTEGER NOT NULL DEFAULT 0,\n"
        "  started_at TEXT NOT NULL,\n"
        "  finished_at TEXT\n"
        ")"
    ),
    "active_lease": (
        "CREATE TABLE active_lease (\n"
        "  id INTEGER PRIMARY KEY CHECK (id = 1),\n"
        "  run_id TEXT NOT NULL,\n"
        "  owner_token TEXT NOT NULL,\n"
        "  acquired_at TEXT NOT NULL,\n"
        "  heartbeat_at TEXT NOT NULL,\n"
        "  deadline_at TEXT NOT NULL\n"
        ")"
    ),
}


def test_v1_whitespace_reformatted_input_is_still_operator_only(tmp_path):
    """Logical v1 exactness no longer authorizes an ordinary-open migration.

    The local fingerprint still proves the input is logically exact, while the
    ordinary repository open rejects it unchanged under the v4 cutover contract.
    """
    db_path = str(tmp_path / "ledger.sqlite3")

    # Build a v1 ledger from the whitespace-reformatted DDL.
    _make_v1_ledger_from_ddl(db_path, _WHITESPACE_REFORMATTED_V1_DDL)

    # Prove raw SQL differs from exact v1 (the reformatting must be a real change).
    conn = sqlite3.connect(db_path)
    try:
        raw_sqls = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='table' AND name NOT GLOB 'sqlite_*'"
            ).fetchall()
        }
    finally:
        conn.close()

    for table_name in _EXACT_V1_TABLE_DDL:
        exact_sql = _EXACT_V1_TABLE_DDL[table_name]
        actual_sql = raw_sqls[table_name]
        assert actual_sql != exact_sql, (
            f"table {table_name!r}: raw SQL must differ from exact v1 DDL; "
            "whitespace reformatting is a no-op"
        )
        assert _normalize_v1_sql(actual_sql) == _normalize_v1_sql(exact_sql), (
            f"table {table_name!r}: whitespace-normalized SQL must equal exact v1; "
            "reformatting must be whitespace-only"
        )

    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    before = _v1_fingerprint(db_path)
    with pytest.raises(SchemaVersionError, match="explicit operator migration"):
        ScraperRuntimeRepository(db_path)
    assert _v1_fingerprint(db_path) == before


# ── Parameterized enforced-shape mutations (v1 exact-input rejection) ─────

_V1_MUTATED_SHAPE_CASES = [
    pytest.param(
        "runs",
        r"attempt(\s+)INTEGER",
        r"attempt\1TEXT",
        id="runs_attempt_type",
    ),
    pytest.param(
        "runs",
        r"trigger(\s+)TEXT NOT NULL",
        r"trigger\1TEXT",
        id="runs_trigger_not_null_dropped",
    ),
    pytest.param(
        "runs",
        "DEFAULT 1",
        "DEFAULT 5",
        id="runs_attempt_default_changed",
    ),
    pytest.param(
        "runs",
        r"run_id(\s+)TEXT UNIQUE",
        r"run_id\1TEXT",
        id="runs_run_id_unique_removed",
    ),
    pytest.param(
        "runs",
        "PRIMARY KEY AUTOINCREMENT",
        "PRIMARY KEY",
        id="runs_autoincrement_removed",
    ),
    pytest.param(
        "runs",
        "seq INTEGER PRIMARY KEY AUTOINCREMENT",
        "seq INTEGER",
        id="runs_seq_primary_key_guard_removed",
    ),
    pytest.param(
        "active_lease",
        r"(id\s+INTEGER PRIMARY KEY) CHECK \(id = 1\)",
        r"\1",
        id="active_lease_singleton_check_removed",
    ),
]


@pytest.mark.parametrize("table, pattern, repl", _V1_MUTATED_SHAPE_CASES)
def test_v1_exact_input_rejects_mutated_enforced_shape_unchanged(tmp_path, table, pattern, repl):
    """Exact-v1 ledger with a single enforced-shape mutation fails closed unchanged.

    Each fixture creates a version-1 empty ledger whose table names and column
    names are unchanged but whose column type / NOT NULL / DEFAULT / UNIQUE /
    AUTOINCREMENT / PRIMARY KEY / CHECK is mutated. The open must raise
    ``SchemaVersionError`` before any migration side effect, and the complete
    on-disk logical fingerprint must equal the pre-open snapshot.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    _make_mutated_v1_ledger(db_path, table=table, pattern=pattern, repl=repl)

    # Precondition: still version 1 and empty.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM active_lease").fetchone()[0] == 0
    finally:
        conn.close()

    before = _v1_fingerprint(db_path)

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _v1_fingerprint(db_path)
    assert after == before, (
        "fail-closed open must leave the complete on-disk logical fingerprint unchanged"
    )


def test_v1_exact_input_rejects_unexpected_user_index_unchanged(tmp_path):
    """Exact empty v1 ledger with an unexpected user index fails closed unchanged.

    Creates ``CREATE INDEX extra_v1_runs_status_idx ON runs (status)`` on an
    otherwise exact empty v1 ledger. The open must raise ``SchemaVersionError``
    before any migration side effect, and the complete on-disk logical fingerprint
    must equal the pre-open snapshot.
    """
    from fin_analyse.scraper.runtime_repository import SchemaVersionError

    db_path = str(tmp_path / "ledger.sqlite3")
    _make_exact_v1_ledger(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE INDEX extra_v1_runs_status_idx ON runs (status)")
        conn.commit()
    finally:
        conn.close()

    before = _v1_fingerprint(db_path)

    with pytest.raises(SchemaVersionError):
        ScraperRuntimeRepository(db_path)

    after = _v1_fingerprint(db_path)
    assert after == before, (
        "fail-closed open must leave the complete on-disk logical fingerprint unchanged"
    )


# ── Concurrent exact-v1/v2 spawn → converge to exact-v3 ───────────────────


# ── B0b: fail-closed stale-run-relation recovery contract ─────────────────
#
# ``acquire_or_coalesce()`` and ``acquire_probe_lease()`` reclaim a stale
# ``owner_kind='run'`` lease by UPDATE-ing the referenced run to ``interrupted`` and
# deleting/replacing the lease WITHOUT proving that exactly one canonical owning run
# was transitioned. If the referenced run is missing, already terminal (non-NULL
# ``finished_at``), or a non-running status while unfinished, the ledger is malformed
# and recovery must fail closed atomically — it must not erase the evidence, invent a
# successor, or convert a logically invalid run. These RED tests pin that contract.

_B0B_APP_TABLES = (
    "schema_version",
    "runs",
    "active_lease",
    "health_observations",
    "health_episodes",
    "scraper_outbox",
)
_B0B_ORIGINAL_TOKEN = "b0b-original-owner-token"
_B0B_STALE_RUN_ID = "r00000001-b0bstalerun"


def _b0b_ledger_snapshot(db_path: str, conn: sqlite3.Connection | None = None) -> dict:
    """TEST-PRIVATE complete logical ledger snapshot (no production helper imported).

    Captures the literal non-internal ``sqlite_master`` inventory + SQL
    (``name NOT GLOB 'sqlite_*'``), every application-table row in deterministic
    ``_rowid_`` order, the ``sqlite_sequence`` rows when the table is present, and
    ``PRAGMA integrity_check``. Two equal snapshots prove the on-disk ledger is
    byte/logically identical (schema, rows, sequences, integrity).

    When *conn* is supplied the snapshot reads through that still-open connection
    and does NOT close it; otherwise the helper opens its own connection from
    *db_path* and closes it before returning.
    """
    _close = False
    if conn is None:
        conn = sqlite3.connect(db_path)
        _close = True
    try:
        master = [
            tuple(r)
            for r in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name, tbl_name"
            ).fetchall()
        ]
        rows: dict[str, list] = {}
        for table in _B0B_APP_TABLES:
            rows[table] = [
                tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY _rowid_").fetchall()
            ]
        has_sequence = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
            ).fetchone()
            is not None
        )
        sqlite_sequence = (
            [tuple(r) for r in conn.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name")]
            if has_sequence
            else None
        )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        if _close:
            conn.close()
    return {
        "master": master,
        "rows": rows,
        "sqlite_sequence": sqlite_sequence,
        "integrity": integrity,
    }


def _seed_stale_run_lease_with_relation(db_path: str, *, relation: str) -> None:
    """Seed a canonical fresh v2 ledger + a deliberately stale ``owner_kind='run'``
    lease whose referenced run is malformed per ``relation`` — direct SQLite only,
    no schema constraint weakened/rebuilt.
    """
    ScraperRuntimeRepository(db_path).close()  # canonical fresh v2 ledger

    conn = sqlite3.connect(db_path)
    try:
        if relation == "missing_run":
            pass  # the lease references a run row that does not exist
        elif relation == "terminal_finished":
            # Already-terminal run with a non-NULL finished_at.
            conn.execute(
                "INSERT INTO runs (run_id, intent, trigger, status, attempt, changed_count, "
                "started_at, finished_at) VALUES (?, 'sync', 'schedule', ?, 1, 0, ?, ?)",
                (_B0B_STALE_RUN_ID, ZsxqRunStatus.SUCCEEDED.value, _BASE_ISO, _BASE_ISO),
            )
        elif relation == "nonrunning_unfinished":
            # Non-running status while finished_at IS NULL (internally inconsistent).
            conn.execute(
                "INSERT INTO runs (run_id, intent, trigger, status, attempt, changed_count, "
                "started_at, finished_at) VALUES (?, 'sync', 'schedule', ?, 1, 0, ?, NULL)",
                (_B0B_STALE_RUN_ID, ZsxqRunStatus.SUCCEEDED.value, _BASE_ISO),
            )
        else:  # pragma: no cover - guard against a typo'd parameter
            raise AssertionError(f"unknown malformed relation {relation!r}")

        # A stale run lease: heartbeat_at at the base instant (so a later
        # stale_before makes the heartbeat stale), BUT deadline_at strictly later
        # than the reclaim now — the lease is stale only by heartbeat, not dead.
        _heartbeat = _BASE_ISO
        _deadline = (datetime.fromisoformat(_BASE_ISO) + timedelta(hours=2)).isoformat()
        conn.execute(
            "INSERT INTO active_lease (id, owner_kind, run_id, owner_token, acquired_at, "
            "heartbeat_at, deadline_at) VALUES (1, 'run', ?, ?, ?, ?, ?)",
            (_B0B_STALE_RUN_ID, _B0B_ORIGINAL_TOKEN, _BASE_ISO, _heartbeat, _deadline),
        )
        conn.commit()
    finally:
        conn.close()


_B0B_MALFORMED_CASES = [
    pytest.param("run", "missing_run", id="run-missing_run"),
    pytest.param("run", "terminal_finished", id="run-terminal_finished"),
    pytest.param("run", "nonrunning_unfinished", id="run-nonrunning_unfinished"),
    pytest.param("probe", "missing_run", id="probe-missing_run"),
    pytest.param("probe", "terminal_finished", id="probe-terminal_finished"),
    pytest.param("probe", "nonrunning_unfinished", id="probe-nonrunning_unfinished"),
]


@pytest.mark.parametrize("incoming, relation", _B0B_MALFORMED_CASES)
def test_malformed_stale_run_relation_fails_closed_atomically(tmp_path, incoming, relation):
    """A stale run lease pointing at a malformed run must fail closed, unchanged.

    Two incoming acquisition paths (``run`` → :meth:`acquire_or_coalesce`, ``probe``
    → :meth:`acquire_probe_lease`) x three malformed stale-run relations (missing
    run, already-terminal run, non-running-but-unfinished run). Recovery must raise a
    visible ``RuntimeError`` and leave the complete logical ledger snapshot identical:
    the original active-lease token intact, no successor run/probe lease invented, no
    sequence drift, and no run status/finished_at mutation.

    Current production RED: reclaim UPDATE-then-delete/replace proves nothing about
    the owning run, so it silently reclaims (and, on the run path, invents a
    successor) instead of raising — the ``pytest.raises(RuntimeError)`` is unmet.
    """
    db_path = str(tmp_path / "ledger.sqlite3")
    _seed_stale_run_lease_with_relation(db_path, relation=relation)

    base = datetime.fromisoformat(_BASE_ISO)
    now = base + timedelta(hours=1)
    stale_before = base + timedelta(minutes=30)
    deadline_at = base + timedelta(hours=2)

    before = _b0b_ledger_snapshot(db_path)

    repo = ScraperRuntimeRepository(db_path)
    try:
        # Pre-acquisition contract: persisted heartbeat is stale; deadline is in the future.
        persisted = repo._conn.execute(
            "SELECT heartbeat_at, deadline_at FROM active_lease WHERE id = 1"
        ).fetchone()
        assert persisted is not None
        persisted_heartbeat = datetime.fromisoformat(persisted[0])
        persisted_deadline = datetime.fromisoformat(persisted[1])
        assert persisted_heartbeat <= stale_before
        assert now < persisted_deadline

        with pytest.raises(RuntimeError):
            if incoming == "run":
                repo.acquire_or_coalesce(
                    intent=ZsxqRunIntent.SYNC.value,
                    trigger=ZsxqRunTrigger.SCHEDULE.value,
                    now=now,
                    deadline_at=deadline_at,
                    stale_before=stale_before,
                )
            else:
                repo.acquire_probe_lease(
                    now=now,
                    deadline_at=deadline_at,
                    stale_before=stale_before,
                )
        # After the expected RuntimeError, assert the connection is not in a
        # transaction and take the complete after snapshot through the same
        # still-open repo._conn.
        assert repo._conn.in_transaction is False
        after = _b0b_ledger_snapshot(db_path, conn=repo._conn)
    finally:
        repo.close()
    assert after == before, (
        "malformed stale-run recovery must be atomic and fail-closed: the complete "
        "ledger snapshot (schema, rows, sequences, integrity) must be unchanged"
    )
    # Make the required invariants explicit (all implied by full-snapshot equality).
    assert after["rows"]["active_lease"] == before["rows"]["active_lease"]
    assert _B0B_ORIGINAL_TOKEN in {row[3] for row in after["rows"]["active_lease"]}
    assert after["rows"]["runs"] == before["rows"]["runs"]  # no successor / no mutation
    assert after["sqlite_sequence"] == before["sqlite_sequence"]  # no sequence drift


def test_valid_stale_running_run_can_be_reclaimed_by_probe_atomically(tmp_path):
    """A stale lease over a genuine running run IS reclaimable by a probe (control).

    Honest positive control (PASSES before production changes): a canonical
    ``status='running' AND finished_at IS NULL`` owner run plus a stale run lease is
    validly recovered by :meth:`acquire_probe_lease` — that one run is atomically
    marked ``interrupted`` with a non-NULL finished_at, and the lease is replaced with
    an ``owner_kind='probe'`` lease that owns no run and carries a fresh token.
    """
    db_path = str(tmp_path / "ledger.sqlite3")
    ScraperRuntimeRepository(db_path).close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, intent, trigger, status, attempt, changed_count, "
            "started_at, finished_at) VALUES (?, 'sync', 'schedule', 'running', 1, 0, ?, NULL)",
            (_B0B_STALE_RUN_ID, _BASE_ISO),
        )
        _heartbeat = _BASE_ISO
        _deadline = (datetime.fromisoformat(_BASE_ISO) + timedelta(hours=2)).isoformat()
        conn.execute(
            "INSERT INTO active_lease (id, owner_kind, run_id, owner_token, acquired_at, "
            "heartbeat_at, deadline_at) VALUES (1, 'run', ?, ?, ?, ?, ?)",
            (_B0B_STALE_RUN_ID, _B0B_ORIGINAL_TOKEN, _BASE_ISO, _heartbeat, _deadline),
        )
        conn.commit()
    finally:
        conn.close()

    base = datetime.fromisoformat(_BASE_ISO)
    now = base + timedelta(hours=1)
    stale_before = base + timedelta(minutes=30)
    deadline_at = base + timedelta(hours=2)

    repo = ScraperRuntimeRepository(db_path)
    try:
        # Pre-acquisition contract: persisted heartbeat is stale; deadline is in the future.
        persisted = repo._conn.execute(
            "SELECT heartbeat_at, deadline_at FROM active_lease WHERE id = 1"
        ).fetchone()
        assert persisted is not None
        persisted_heartbeat = datetime.fromisoformat(persisted[0])
        persisted_deadline = datetime.fromisoformat(persisted[1])
        assert persisted_heartbeat <= stale_before
        assert now < persisted_deadline

        result = repo.acquire_probe_lease(
            now=now, deadline_at=deadline_at, stale_before=stale_before
        )
    finally:
        repo.close()

    assert result.acquired is True
    assert result.recovered_run_id == _B0B_STALE_RUN_ID

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute(
            "SELECT status, finished_at FROM runs WHERE run_id = ?", (_B0B_STALE_RUN_ID,)
        ).fetchone()
        assert run["status"] == ZsxqRunStatus.INTERRUPTED.value
        assert run["finished_at"] is not None
        # Exactly that one run — no successor invented.
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1

        leases = conn.execute("SELECT * FROM active_lease").fetchall()
        assert len(leases) == 1
        lease = leases[0]
        assert lease["owner_kind"] == "probe"
        assert lease["run_id"] is None
        assert lease["owner_token"] != _B0B_ORIGINAL_TOKEN
    finally:
        conn.close()
