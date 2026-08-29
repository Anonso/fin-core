"""Tests for ZsxqScraperModule.health() — Gate 2B / B2b-read projection and B2b-probe-fake lifecycle."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from fin_analyse.scraper.contracts import (
    HEALTH_CONTRACT_VERSION,
    SCHEMA_VERSION,
    ZsxqHealthRequest,
    ZsxqHealthState,
    ZsxqRunIntent,
    ZsxqRunStatus,
    ZsxqRunTrigger,
)
from fin_analyse.scraper.module import ZsxqScraperModule
from fin_analyse.scraper.page_assessment import PageAssessment, PageState
from fin_analyse.scraper.runtime_repository import LeaseLostError, ScraperRuntimeRepository

_APP_TABLES = [
    "schema_version",
    "runs",
    "active_lease",
    "health_observations",
    "health_episodes",
    "scraper_outbox",
    "capture_ingests",
]


def _snapshot_all(db_path: str) -> dict[str, list[tuple]]:
    """Return every row of every application table sorted by PK."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result: dict[str, list[tuple]] = {}
    for table in _APP_TABLES:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        result[table] = [tuple(row) for row in rows]
    conn.close()
    return result


class _FakeAdapter:
    """Adapter that fails if *any* method is called — proves no adapter access."""

    def __init__(self) -> None:
        self.call_count = 0

    def run_incremental(self, **kwargs: object) -> None:
        self.call_count += 1
        raise AssertionError("Adapter.run_incremental must not be called during health read")

    def __getattr__(self, name: str) -> object:
        def _fail(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"Adapter.{name} must not be called during health read")

        return _fail


class _FakeProbeAdapter:
    """Fake adapter with ``probe_page`` for probe lifecycle tests.

    Returns a controlled ``PageAssessment`` or raises a controlled error.
    Tracks every ``probe_page`` call for assertions.  An optional ``on_probe``
    callback is invoked inside ``probe_page`` with the ``deadline_at`` argument
    so tests can inspect repository state while the probe lease is held.
    """

    def __init__(
        self,
        assessment: PageAssessment | None = None,
        error: BaseException | None = None,
        on_probe: Callable[[datetime], None] | None = None,
    ) -> None:
        self.assessment = assessment
        self.error = error
        self._on_probe = on_probe
        self.probe_calls: list[datetime] = []

    def run_incremental(self, **kwargs: object) -> None:
        raise AssertionError("Adapter.run_incremental must not be called during health probe")

    def probe_page(self, *, deadline_at: datetime) -> PageAssessment:
        self.probe_calls.append(deadline_at)
        if self._on_probe is not None:
            self._on_probe(deadline_at)
        if self.error is not None:
            raise self.error
        assert self.assessment is not None, "probe_page called but no assessment configured"
        return self.assessment


# ── mapping table ──────────────────────────────────────────────────

_PAGE_STATE_HEALTH_MAP: dict[PageState, str] = {
    PageState.ready: "healthy",
    PageState.login_required: "requires_user",
    PageState.challenge: "requires_user",
    PageState.rate_limited: "degraded",
    PageState.loading_timeout: "degraded",
    PageState.dom_changed: "degraded",
    PageState.wrong_page: "degraded",
    PageState.control_failure: "unavailable",
}

_PAGE_STATE_HAS_EPISODE: set[PageState] = {PageState.login_required, PageState.challenge}


class _TestBaseException(BaseException):
    """A BaseException subclass for proving cleanup is not limited to ``except Exception``."""


# ═══════════════════════════════════════════════════════════════════
# Test 1 — parameterized observation read projection
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("page_state", list(PageState))
def test_health_read_projects_latest_observation_without_changing_ledger(
    tmp_path, page_state: PageState
) -> None:
    """Health(probe=False) is a pure read projection over the latest observation.

    For every PageState value a single observation is seeded through the real
    repository probe-lease UoW, the lease is released, all application
    tables are snapshotted, and the clock is advanced before calling health.
    The test proves:

    * zero adapter calls
    * no SQL write / row change in any application table
    * zero new runs
    * source ``observed_at`` is preserved (not the query time)
    * ``evaluated_at`` is the later query time
    * page_state / reason_code / health_episode_id are projected correctly
    * the eight PageState values map to the four coarse health states
    * health wire contract version is independent v1, not storage v4
    """
    expected_health = _PAGE_STATE_HEALTH_MAP[page_state]
    has_episode = page_state in _PAGE_STATE_HAS_EPISODE
    requires_user_action = expected_health == "requires_user"

    # ── seed one observation through the real probe-lease UoW ──────
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    source_clock = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    stale_before = source_clock - timedelta(seconds=120)
    deadline_at = source_clock + timedelta(seconds=30)

    probe = repo.acquire_probe_lease(
        now=source_clock,
        deadline_at=deadline_at,
        stale_before=stale_before,
    )
    assert probe.acquired

    reason_code = page_state.value
    repo.record_probe_observation(
        owner_token=probe.owner_token,
        intent=ZsxqRunIntent.SYNC.value,
        surface="zsxq",
        state=page_state,
        reason_code=reason_code,
        evidence_ref=None,
        observed_at=source_clock,
        recorded_at=source_clock,
    )

    repo.release_probe_lease(owner_token=probe.owner_token)

    # ── snapshot *after* seeding, before health ────────────────────
    before = _snapshot_all(str(db_path))

    # ── advance clock so evaluated_at ≠ observed_at ───────────────
    eval_time = source_clock + timedelta(seconds=42)
    fake_adapter = _FakeAdapter()

    module = ZsxqScraperModule(
        repository=repo,
        adapter=fake_adapter,  # type: ignore[arg-type]
        clock=lambda: eval_time,
    )

    # ── call health ────────────────────────────────────────────────
    result = module.health(ZsxqHealthRequest(probe=False))

    after = _snapshot_all(str(db_path))

    # ── assertions ─────────────────────────────────────────────────
    # Zero adapter calls
    assert fake_adapter.call_count == 0

    # Zero new runs
    assert len(after["runs"]) == 0

    # No SQL write — byte-for-byte equal
    for table in _APP_TABLES:
        assert before[table] == after[table], f"Table {table} changed during health read"

    # State mapping
    assert result.state == expected_health

    # Timestamps: source observed_at is preserved, evaluated_at is later
    assert result.observed_at == source_clock.isoformat()
    assert result.evaluated_at == eval_time.isoformat()
    assert result.evaluated_at != result.observed_at

    # Page state projection
    assert result.page_state == page_state.value

    # Every observation owns its exact persisted diagnostic reason in schema v4.
    assert result.reason_code == reason_code

    # Episode projection
    if has_episode:
        assert result.health_episode_id is not None
        assert result.health_episode_id != ""

    # requires_user_action
    assert result.requires_user_action is requires_user_action

    # No active run (lease was released)
    assert result.active_run_id is None

    # Health wire contract version is independent v1, not storage v4.
    assert result.schema_version == HEALTH_CONTRACT_VERSION
    assert repo.schema_version() == SCHEMA_VERSION

    repo.close()


# ═══════════════════════════════════════════════════════════════════
# Test 2 — BUSY retains observation and run id, preserves requires_user
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "page_state",
    [
        PageState.ready,
        PageState.login_required,
        PageState.challenge,
    ],
)
def test_health_read_busy_retains_latest_observation_without_side_effects(
    tmp_path, page_state: PageState
) -> None:
    """When a run holds the lease, health returns BUSY with the latest
    persisted observation and active run id — still read-only, no adapter call.

    For LOGIN_REQUIRED / CHALLENGE observations BUSY does not erase the
    requires_user truth: ``requires_user_action`` stays True and the episode
    id / reason are preserved.
    """
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    requires_user = page_state in (PageState.login_required, PageState.challenge)
    source_clock = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    stale_before = source_clock - timedelta(seconds=120)
    deadline_at = source_clock + timedelta(seconds=30)

    # 1. Seed one observation via probe lease
    probe = repo.acquire_probe_lease(
        now=source_clock, deadline_at=deadline_at, stale_before=stale_before
    )
    assert probe.acquired
    reason_code = page_state.value
    repo.record_probe_observation(
        owner_token=probe.owner_token,
        intent=ZsxqRunIntent.SYNC.value,
        surface="zsxq",
        state=page_state,
        reason_code=reason_code,
        evidence_ref=None,
        observed_at=source_clock,
        recorded_at=source_clock,
    )
    repo.release_probe_lease(owner_token=probe.owner_token)

    # 2. Acquire a run lease — leave it running (do NOT finish)
    run_clock = source_clock + timedelta(seconds=10)
    run_deadline = run_clock + timedelta(seconds=60)
    acquisition = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.SCHEDULE.value,
        now=run_clock,
        deadline_at=run_deadline,
        stale_before=run_clock - timedelta(seconds=120),
    )
    assert acquisition.acquired
    active_run_id = acquisition.run_id

    # 3. Snapshot before health
    before = _snapshot_all(str(db_path))

    # 4. Health with advanced clock + fake adapter
    eval_time = run_clock + timedelta(seconds=5)
    fake_adapter = _FakeAdapter()
    module = ZsxqScraperModule(
        repository=repo,
        adapter=fake_adapter,  # type: ignore[arg-type]
        clock=lambda: eval_time,
    )
    result = module.health(ZsxqHealthRequest(probe=False))

    after = _snapshot_all(str(db_path))
    repo.close()

    # ── assertions ─────────────────────────────────────────────────
    assert fake_adapter.call_count == 0
    for table in _APP_TABLES:
        assert before[table] == after[table], f"Table {table} changed during health read"

    # BUSY with observation data
    assert result.state == ZsxqHealthState.BUSY.value
    assert result.active_run_id == active_run_id
    assert result.page_state == page_state.value
    assert result.observed_at == source_clock.isoformat()
    assert result.evaluated_at == eval_time.isoformat()
    assert result.observed_at != result.evaluated_at

    # requires_user_action is preserved even under BUSY
    assert result.requires_user_action is requires_user
    assert result.reason_code == reason_code
    if requires_user:
        assert result.health_episode_id is not None
        assert result.health_episode_id != ""

    # Health wire contract version
    assert result.schema_version == HEALTH_CONTRACT_VERSION


# ═══════════════════════════════════════════════════════════════════
# Test 3 — no observation never fabricates observed_at
# ═══════════════════════════════════════════════════════════════════


def test_health_read_without_observation_never_fabricates_observed_at(
    tmp_path,
) -> None:
    """Without any page observation, observed_at is empty; evaluated_at is
    the query time.  No observed_at is ever fabricated from run timestamps.
    """
    # ── sub-case A: fresh repo, no observations, no runs ────────────
    db_path_a = tmp_path / "fresh.db"
    repo_a = ScraperRuntimeRepository(str(db_path_a))
    eval_a = datetime(2026, 7, 12, 11, 0, 0, tzinfo=UTC)
    fake_a = _FakeAdapter()
    module_a = ZsxqScraperModule(
        repository=repo_a,
        adapter=fake_a,  # type: ignore[arg-type]
        clock=lambda: eval_a,
    )
    before_a = _snapshot_all(str(db_path_a))
    result_a = module_a.health(ZsxqHealthRequest(probe=False))
    after_a = _snapshot_all(str(db_path_a))

    assert fake_a.call_count == 0
    for table in _APP_TABLES:
        assert before_a[table] == after_a[table], f"Table {table} changed"

    assert result_a.state == ZsxqHealthState.UNKNOWN.value
    assert result_a.observed_at == ""
    assert result_a.evaluated_at == eval_a.isoformat()
    assert result_a.page_state == ""
    assert result_a.schema_version == HEALTH_CONTRACT_VERSION
    repo_a.close()

    # ── sub-case B: terminal run exists, no observations ────────────
    db_path_b = tmp_path / "run_only.db"
    repo_b = ScraperRuntimeRepository(str(db_path_b))
    run_clock = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    run_deadline = run_clock + timedelta(seconds=60)
    acq = repo_b.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.SCHEDULE.value,
        now=run_clock,
        deadline_at=run_deadline,
        stale_before=run_clock - timedelta(seconds=120),
    )
    assert acq.acquired
    finished_at = run_clock + timedelta(seconds=3)
    repo_b.finish_run(
        run_id=acq.run_id,
        owner_token=acq.owner_token,
        status=ZsxqRunStatus.SUCCEEDED.value,
        changed_count=0,
        finished_at=finished_at,
    )

    eval_b = datetime(2026, 7, 12, 11, 30, 0, tzinfo=UTC)
    fake_b = _FakeAdapter()
    module_b = ZsxqScraperModule(
        repository=repo_b,
        adapter=fake_b,  # type: ignore[arg-type]
        clock=lambda: eval_b,
    )
    before_b = _snapshot_all(str(db_path_b))
    result_b = module_b.health(ZsxqHealthRequest(probe=False))
    after_b = _snapshot_all(str(db_path_b))

    assert fake_b.call_count == 0
    for table in _APP_TABLES:
        assert before_b[table] == after_b[table], f"Table {table} changed"

    assert result_b.state == ZsxqHealthState.IDLE.value
    assert result_b.observed_at == ""
    assert result_b.evaluated_at == eval_b.isoformat()
    assert result_b.page_state == ""
    assert result_b.last_run_id == acq.run_id
    assert result_b.last_status == ZsxqRunStatus.SUCCEEDED.value
    assert result_b.schema_version == HEALTH_CONTRACT_VERSION
    repo_b.close()


# ═══════════════════════════════════════════════════════════════════
# Test 4 — probe=True success lifecycle
# ═══════════════════════════════════════════════════════════════════


def test_live_probe_success_acquires_probe_lease_records_observation_releases_without_run(
    tmp_path,
) -> None:
    """probe=True acquires a probe lease, calls adapter.probe_page, records
    a health observation with intent='watch'/surface='timeline', releases the
    lease, and returns the pure ledger projection.

    No run is created and the adapter's run_incremental is never called.
    """
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    # Seed a terminal run so run fields are populated in the projection.
    run_clock = datetime(2026, 7, 12, 9, 0, 0, tzinfo=UTC)
    run_deadline = run_clock + timedelta(seconds=60)
    acq = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.SCHEDULE.value,
        now=run_clock,
        deadline_at=run_deadline,
        stale_before=run_clock - timedelta(seconds=120),
    )
    assert acq.acquired
    finished_at = run_clock + timedelta(seconds=5)
    repo.finish_run(
        run_id=acq.run_id,
        owner_token=acq.owner_token,
        status=ZsxqRunStatus.SUCCEEDED.value,
        changed_count=3,
        finished_at=finished_at,
    )

    # ── fake adapter returns a ready assessment ────────────────────
    assessment = PageAssessment(
        state=PageState.ready,
        reason_code=PageState.ready.value,
        evidence_fingerprint="fp_deadbeef",
    )

    # ── controlled clock ───────────────────────────────────────────
    probe_start = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    post_probe = datetime(2026, 7, 12, 10, 0, 3, tzinfo=UTC)

    # Verify inside the adapter callback that the probe lease is held
    # with the correct shape: owner_kind='probe', run_id=NULL.
    captured_deadline: list[datetime] = []

    def _on_probe(deadline_at: datetime) -> None:
        captured_deadline.append(deadline_at)
        lease = repo.get_active_lease()
        assert lease is not None, "probe lease must be held during adapter callback"
        assert lease["owner_kind"] == "probe"
        assert lease["run_id"] is None

    fake_adapter = _FakeProbeAdapter(assessment=assessment, on_probe=_on_probe)

    # _health_probe samples twice (now + post_clock), then _health_read samples once.
    clocks = iter([probe_start, post_probe, post_probe])

    module = ZsxqScraperModule(
        repository=repo,
        adapter=fake_adapter,  # type: ignore[arg-type]
        clock=lambda: next(clocks),
    )

    # ── call health(probe=True) ────────────────────────────────────
    result = module.health(ZsxqHealthRequest(probe=True))
    after = _snapshot_all(str(db_path))
    repo.close()

    # ── adapter was called exactly once ────────────────────────────
    assert len(fake_adapter.probe_calls) == 1
    assert len(captured_deadline) == 1
    deadline_arg = fake_adapter.probe_calls[0]
    expected_deadline = probe_start + timedelta(seconds=30.0)  # default 30s
    assert deadline_arg == expected_deadline
    assert captured_deadline[0] == expected_deadline

    # ── request wire contract ──────────────────────────────────────
    assert ZsxqHealthRequest(probe=True).to_dict()["deadline_seconds"] == 30.0

    # ── no run created ─────────────────────────────────────────────
    assert len(after["runs"]) == 1  # only the pre-seeded terminal run
    assert after["runs"][0][1] == acq.run_id  # run_id unchanged

    # ── lease released ─────────────────────────────────────────────
    assert len(after["active_lease"]) == 0

    # ── one observation recorded ───────────────────────────────────
    assert len(after["health_observations"]) == 1
    obs = after["health_observations"][0]
    # intent='watch', surface='timeline'
    assert obs[1] == "watch"  # intent
    assert obs[2] == "timeline"  # surface
    assert obs[3] == PageState.ready.value  # state
    assert obs[4] == PageState.ready.value  # reason_code
    assert obs[5] == post_probe.isoformat()  # observed_at
    assert obs[7] == "fp_deadbeef"  # evidence_ref

    # ── projection ─────────────────────────────────────────────────
    assert result.state == ZsxqHealthState.HEALTHY.value
    assert result.page_state == PageState.ready.value
    assert result.observed_at == post_probe.isoformat()
    assert result.evaluated_at == post_probe.isoformat()

    # Run fields from the pre-seeded terminal run are projected.
    assert result.last_run_id == acq.run_id
    assert result.last_status == ZsxqRunStatus.SUCCEEDED.value

    # No active run — probe lease was released.
    assert result.active_run_id is None

    assert result.schema_version == HEALTH_CONTRACT_VERSION


# ═══════════════════════════════════════════════════════════════════
# Test 5 — probe=True requires_user reuses episode and outbox
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("page_state", [PageState.login_required, PageState.challenge])
def test_live_probe_requires_user_reuses_episode_and_outbox_without_run(
    tmp_path, page_state: PageState
) -> None:
    """Two consecutive probe=True calls with the same requires-user assessment
    produce exactly two observations, one reused open episode, and one
    deduplicated requires_user outbox row — zero runs, no Hermes delivery.
    Each state (LOGIN_REQUIRED / CHALLENGE) is probed independently.
    """
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    assessment = PageAssessment(
        state=page_state,
        reason_code=page_state.value,
        evidence_fingerprint="fp_auth_1",
    )

    # ── first probe ────────────────────────────────────────────────
    clock1 = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    post1 = datetime(2026, 7, 12, 10, 0, 1, tzinfo=UTC)
    clocks1 = iter([clock1, post1, post1])

    adapter1 = _FakeProbeAdapter(assessment=assessment)
    module1 = ZsxqScraperModule(
        repository=repo,
        adapter=adapter1,  # type: ignore[arg-type]
        clock=lambda: next(clocks1),
    )
    result1 = module1.health(ZsxqHealthRequest(probe=True))

    after_first = _snapshot_all(str(db_path))

    # First probe: 1 observation, 1 episode, 1 outbox row, 0 runs.
    assert len(after_first["health_observations"]) == 1
    assert len(after_first["health_episodes"]) == 1
    assert len(after_first["scraper_outbox"]) == 1
    assert len(after_first["runs"]) == 0
    # No active lease after call.
    assert len(after_first["active_lease"]) == 0

    episode_id = after_first["health_episodes"][0][0]
    outbox_dedupe_key = after_first["scraper_outbox"][0][1]

    assert result1.state == ZsxqHealthState.REQUIRES_USER.value
    assert result1.requires_user_action is True

    # ── second probe — same state ──────────────────────────────────
    assessment2 = PageAssessment(
        state=page_state,
        reason_code=page_state.value,
        evidence_fingerprint="fp_auth_2",
    )
    clock2 = datetime(2026, 7, 12, 10, 5, 0, tzinfo=UTC)
    post2 = datetime(2026, 7, 12, 10, 5, 1, tzinfo=UTC)
    clocks2 = iter([clock2, post2, post2])

    adapter2 = _FakeProbeAdapter(assessment=assessment2)
    module2 = ZsxqScraperModule(
        repository=repo,
        adapter=adapter2,  # type: ignore[arg-type]
        clock=lambda: next(clocks2),
    )
    result2 = module2.health(ZsxqHealthRequest(probe=True))

    after_second = _snapshot_all(str(db_path))
    repo.close()

    # Second probe: 2 observations, 1 episode (reused), 1 outbox (deduped), 0 runs.
    assert len(after_second["health_observations"]) == 2
    assert len(after_second["health_episodes"]) == 1
    assert len(after_second["scraper_outbox"]) == 1
    assert len(after_second["runs"]) == 0
    assert len(after_second["active_lease"]) == 0

    # Both observations linked to the same episode.
    obs1_ep = after_second["health_observations"][0][6]
    obs2_ep = after_second["health_observations"][1][6]
    assert obs1_ep == episode_id
    assert obs2_ep == episode_id

    # Episode still open.
    assert after_second["health_episodes"][0][4] == "open"

    # Outbox unchanged — dedupe key preserved.
    assert after_second["scraper_outbox"][0][1] == outbox_dedupe_key

    assert result2.state == ZsxqHealthState.REQUIRES_USER.value
    assert result2.requires_user_action is True
    assert result2.reason_code == page_state.value


# ═══════════════════════════════════════════════════════════════════
# Test 6 — probe=True BUSY owner returns without adapter or ledger side effects
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("owner_type", ["run", "probe"])
def test_live_probe_busy_owner_returns_without_adapter_or_ledger_side_effects(
    tmp_path, owner_type: str
) -> None:
    """When a fresh active owner already holds the lease, probe=True returns
    BUSY without calling the adapter, writing any row, or releasing a foreign
    lease.  Requires-user truth (page_state, reason, episode, requires_user_action)
    is preserved regardless of whether the owning lease is a run or probe.
    """
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    # Seed a requires-user observation — LOGIN_REQUIRED for run, CHALLENGE for probe.
    page_state = PageState.login_required if owner_type == "run" else PageState.challenge
    seed_clock = datetime(2026, 7, 12, 9, 0, 0, tzinfo=UTC)
    probe_lease = repo.acquire_probe_lease(
        now=seed_clock,
        deadline_at=seed_clock + timedelta(seconds=30),
        stale_before=seed_clock - timedelta(seconds=120),
    )
    assert probe_lease.acquired
    reason_code = page_state.value
    repo.record_probe_observation(
        owner_token=probe_lease.owner_token,
        intent=ZsxqRunIntent.SYNC.value,
        surface="zsxq",
        state=page_state,
        reason_code=reason_code,
        evidence_ref=None,
        observed_at=seed_clock,
        recorded_at=seed_clock,
    )
    repo.release_probe_lease(owner_token=probe_lease.owner_token)

    # Acquire the foreign lease (run or probe) — leave it active.
    busy_clock = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    busy_deadline = busy_clock + timedelta(seconds=60)
    if owner_type == "run":
        acq = repo.acquire_or_coalesce(
            intent=ZsxqRunIntent.SYNC.value,
            trigger=ZsxqRunTrigger.SCHEDULE.value,
            now=busy_clock,
            deadline_at=busy_deadline,
            stale_before=busy_clock - timedelta(seconds=120),
        )
        assert acq.acquired
        active_run_id: str | None = acq.run_id
        foreign_owner_token = acq.owner_token
    else:
        probe_acq = repo.acquire_probe_lease(
            now=busy_clock,
            deadline_at=busy_deadline,
            stale_before=busy_clock - timedelta(seconds=120),
        )
        assert probe_acq.acquired
        active_run_id = None  # probe lease owns no run
        foreign_owner_token = probe_acq.owner_token

    # Snapshot before probe.
    before = _snapshot_all(str(db_path))

    # Fake adapter that MUST NOT be called.
    fake_adapter = _FakeProbeAdapter(
        assessment=PageAssessment(
            state=PageState.ready,
            reason_code=PageState.ready.value,
            evidence_fingerprint="x",
        )
    )

    eval_time = busy_clock + timedelta(seconds=5)
    module = ZsxqScraperModule(
        repository=repo,
        adapter=fake_adapter,  # type: ignore[arg-type]
        clock=lambda: eval_time,
    )

    result = module.health(ZsxqHealthRequest(probe=True))
    after = _snapshot_all(str(db_path))

    # Clean up the foreign lease using its own token.
    if owner_type == "run":
        repo.finish_run(
            run_id=acq.run_id,
            owner_token=foreign_owner_token,
            status=ZsxqRunStatus.NO_CHANGE.value,
            changed_count=0,
            finished_at=eval_time,
        )
    else:
        repo.release_probe_lease(owner_token=foreign_owner_token)
    repo.close()

    # Adapter was never called.
    assert len(fake_adapter.probe_calls) == 0

    # Zero writes — all tables unchanged.
    for table in _APP_TABLES:
        assert before[table] == after[table], f"Table {table} changed during probe=True BUSY"

    # BUSY with requires-user observation and correct active owner id.
    assert result.state == ZsxqHealthState.BUSY.value
    assert result.active_run_id == active_run_id
    assert result.page_state == page_state.value
    assert result.observed_at == seed_clock.isoformat()
    assert result.requires_user_action is True
    assert result.health_episode_id is not None
    assert result.health_episode_id != ""
    assert result.reason_code == reason_code


# ═══════════════════════════════════════════════════════════════════
# Test 7 — probe=True adapter failure releases lease without observation
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: RuntimeError("CDP bridge unreachable"),
        lambda: _TestBaseException("fatal signal"),
    ],
)
def test_live_probe_adapter_failure_releases_lease_without_observation(
    tmp_path, error_factory: Callable[[], BaseException]
) -> None:
    """When adapter.probe_page raises any exception (including BaseException),
    the module releases the probe lease in ``finally`` and propagates the
    exception.  No observation, episode, outbox or run is written.

    This proves cleanup is not limited to ``except Exception`` — a
    ``BaseException`` that is NOT a subclass of ``Exception`` is also handled.
    """
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    adapter_error = error_factory()
    fake_adapter = _FakeProbeAdapter(error=adapter_error)

    clock = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    module = ZsxqScraperModule(
        repository=repo,
        adapter=fake_adapter,  # type: ignore[arg-type]
        clock=lambda: clock,
    )

    with pytest.raises(type(adapter_error)):
        module.health(ZsxqHealthRequest(probe=True))

    after = _snapshot_all(str(db_path))
    repo.close()

    # Adapter was called.
    assert len(fake_adapter.probe_calls) == 1

    # Lease released.
    assert len(after["active_lease"]) == 0

    # Zero observations / episodes / outbox / runs.
    assert len(after["health_observations"]) == 0
    assert len(after["health_episodes"]) == 0
    assert len(after["scraper_outbox"]) == 0
    assert len(after["runs"]) == 0


# ═══════════════════════════════════════════════════════════════════
# Test 8 — probe=True deadline fence releases lease without observation
# ═══════════════════════════════════════════════════════════════════


def test_live_probe_deadline_fence_releases_lease_without_observation(
    tmp_path,
) -> None:
    """When the post-probe clock reaches the exact lease deadline,
    record_probe_observation raises LeaseLostError.  The module releases
    the still-owned probe lease and propagates the exception — zero health
    writes, zero runs, no synthetic observation.
    """
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    assessment = PageAssessment(
        state=PageState.ready,
        reason_code=PageState.ready.value,
        evidence_fingerprint="fp_deadbeef",
    )
    fake_adapter = _FakeProbeAdapter(assessment=assessment)

    # Post-probe clock is exactly 30s later — at the deadline boundary.
    probe_start = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    post_probe = probe_start + timedelta(seconds=30)
    clocks = iter([probe_start, post_probe])

    module = ZsxqScraperModule(
        repository=repo,
        adapter=fake_adapter,  # type: ignore[arg-type]
        clock=lambda: next(clocks),
    )

    with pytest.raises(LeaseLostError, match="recorded_at"):
        module.health(ZsxqHealthRequest(probe=True))

    after = _snapshot_all(str(db_path))
    repo.close()

    # Adapter was called.
    assert len(fake_adapter.probe_calls) == 1

    # Lease released.
    assert len(after["active_lease"]) == 0

    # Zero observations / episodes / outbox / runs.
    assert len(after["health_observations"]) == 0
    assert len(after["health_episodes"]) == 0
    assert len(after["scraper_outbox"]) == 0
    assert len(after["runs"]) == 0


# ═══════════════════════════════════════════════════════════════════
# Test 9 — production WindowsChromeCdpAdapter.probe_page wiring
# ═══════════════════════════════════════════════════════════════════


def test_production_probe_uses_probe_bridge_and_records_observation_without_run(
    tmp_path, monkeypatch
) -> None:
    """Production wiring uses one read-only probe client and never builds a scraper."""
    from fin_analyse.scraper import opencli_bridge_client
    from fin_analyse.scraper.cdp_runtime import WindowsChromeCdpAdapter

    built: list[dict[str, object]] = []

    class _OfflineProbeBridge:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.inventory_calls = 0
            self.close_calls = 0
            built.append(kwargs)

        def start(self) -> bool:
            return True

        def get_browser_tab_inventory(self) -> list[dict[str, object]]:
            self.inventory_calls += 1
            return [
                {
                    "tabId": 22,
                    "windowId": 42,
                    "url": "https://wx.zsxq.com/group/15522441811252",
                    "active": True,
                }
            ]

        def collect_page_evidence_on_tab(self, tab_id: int | str) -> dict[str, object]:
            assert str(tab_id) == "22"
            return {
                "schema_version": 1,
                "observed_origin": "https://wx.zsxq.com",
                "observed_url_path": "/group/15522441811252",
                "url_query_present": False,
                "url_fragment_present": False,
                "observed_native_identity": "zsxq-group-timeline",
                "document_ready_state": "complete",
                "loading_surface_stable": False,
                "challenge_present": False,
                "login_surface_present": False,
                "qr_scan_surface_present": False,
                "rate_limit_present": False,
                "retry_after_seconds": None,
            }

        def close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(opencli_bridge_client, "OpenCliBridgeClient", _OfflineProbeBridge)

    # ── sub-case A: direct adapter call ────────────────────────────
    adapter = WindowsChromeCdpAdapter()
    assert adapter.scraper_builds == 0
    direct_deadline = datetime(2026, 7, 12, 10, 0, 30, tzinfo=UTC)

    assessment = adapter.probe_page(deadline_at=direct_deadline)

    assert assessment.state is PageState.ready
    assert adapter.scraper_builds == 0
    assert built == [
        {
            "startup_wait": 35.0,
            "max_retries": 0,
            "purpose": "probe",
            "deadline_at": direct_deadline,
        }
    ]

    # ── sub-case B: module health(probe=True) with production adapter ─
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    clock = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    module = ZsxqScraperModule(
        repository=repo,
        adapter=adapter,
        clock=lambda: clock,
    )

    result = module.health(ZsxqHealthRequest(probe=True))

    after = _snapshot_all(str(db_path))
    repo.close()

    assert result.state == ZsxqHealthState.HEALTHY.value
    assert built == [
        {
            "startup_wait": 35.0,
            "max_retries": 0,
            "purpose": "probe",
            "deadline_at": direct_deadline,
        },
        {
            "startup_wait": 35.0,
            "max_retries": 0,
            "purpose": "probe",
            "deadline_at": clock + timedelta(seconds=30),
        },
    ]
    assert len(after["active_lease"]) == 0
    assert len(after["health_observations"]) == 1
    assert len(after["health_episodes"]) == 0
    assert len(after["scraper_outbox"]) == 0
    assert len(after["runs"]) == 0


def test_production_probe_records_typed_bridge_control_failure_without_run(
    tmp_path, monkeypatch
) -> None:
    """A recognized production bridge failure is an observed page state."""
    from fin_analyse.scraper import opencli_bridge_client
    from fin_analyse.scraper.cdp_diagnostics import CdpProbeControlFailureCode
    from fin_analyse.scraper.cdp_runtime import WindowsChromeCdpAdapter

    class _OfflineControlFailureBridge:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.close_calls = 0
            instances.append(self)

        def start(self) -> bool:
            return False

        def probe_control_failure_code(self) -> CdpProbeControlFailureCode:
            return CdpProbeControlFailureCode.EXTENSION_DISCONNECTED

        def close(self) -> None:
            self.close_calls += 1

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"control failure must not collect page data: {name}")

    instances: list[_OfflineControlFailureBridge] = []

    monkeypatch.setattr(opencli_bridge_client, "OpenCliBridgeClient", _OfflineControlFailureBridge)

    adapter = WindowsChromeCdpAdapter()
    direct_deadline = datetime(2026, 7, 12, 10, 0, 30, tzinfo=UTC)
    assessment = adapter.probe_page(deadline_at=direct_deadline)

    assert assessment.state is PageState.control_failure
    assert assessment.reason_code == "extension_disconnected"
    assert instances[0].kwargs == {
        "startup_wait": 35.0,
        "max_retries": 0,
        "purpose": "probe",
        "deadline_at": direct_deadline,
    }
    assert instances[0].close_calls == 1

    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))
    clock = datetime(2026, 7, 12, 10, 1, 0, tzinfo=UTC)
    module = ZsxqScraperModule(repository=repo, adapter=adapter, clock=lambda: clock)

    result = module.health(ZsxqHealthRequest(probe=True))

    after = _snapshot_all(str(db_path))
    repo.close()

    assert result.state == ZsxqHealthState.UNAVAILABLE.value
    assert result.page_state == PageState.control_failure.value
    assert result.reason_code == "extension_disconnected"
    assert len(after["health_observations"]) == 1
    assert after["health_observations"][0][3] == PageState.control_failure.value
    assert after["health_observations"][0][4] == "extension_disconnected"
    assert len(after["active_lease"]) == 0
    assert len(after["health_episodes"]) == 0
    assert len(after["scraper_outbox"]) == 0
    assert len(after["runs"]) == 0
    assert len(instances) == 2
    assert instances[1].close_calls == 1


@pytest.mark.parametrize("page_state", [PageState.control_failure, PageState.login_required])
def test_repository_does_not_persist_raw_error_as_observation_reason(
    tmp_path, page_state: PageState
) -> None:
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))
    observed_at = datetime(2026, 7, 12, 10, 2, 0, tzinfo=UTC)
    probe = repo.acquire_probe_lease(
        now=observed_at,
        deadline_at=observed_at + timedelta(seconds=30),
        stale_before=observed_at - timedelta(seconds=120),
    )
    assert probe.acquired

    repo.record_probe_observation(
        owner_token=probe.owner_token,
        intent=ZsxqRunIntent.WATCH.value,
        surface="timeline",
        state=page_state,
        reason_code=(
            "Runtime failed at https://private.invalid/?access_token=secret for tab 735041166"
        ),
        evidence_ref=None,
        observed_at=observed_at,
        recorded_at=observed_at,
    )
    repo.release_probe_lease(owner_token=probe.owner_token)

    latest = repo.latest_actual_observation()
    module = ZsxqScraperModule(
        repository=repo,
        adapter=_FakeAdapter(),  # type: ignore[arg-type]
        clock=lambda: observed_at + timedelta(seconds=1),
    )
    health = module.health(ZsxqHealthRequest(probe=False))
    snapshot = _snapshot_all(str(db_path))
    retained = repr(snapshot)
    repo.close()

    assert latest is not None
    assert latest["reason_code"] == page_state.value
    assert health.reason_code == page_state.value
    if page_state is PageState.login_required:
        assert snapshot["health_episodes"][0][3] == page_state.value
        assert snapshot["scraper_outbox"][0][5] == page_state.value
    assert "access_token" not in retained
    assert "735041166" not in retained


@pytest.mark.parametrize("unowned_reason", ["arbitrary_snake_case", "access_token_secret"])
def test_repository_reason_code_is_closed_to_fin_owned_codes(tmp_path, unowned_reason: str) -> None:
    db_path = tmp_path / "closed-reason-ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))
    observed_at = datetime(2026, 7, 12, 10, 3, 0, tzinfo=UTC)
    probe = repo.acquire_probe_lease(
        now=observed_at,
        deadline_at=observed_at + timedelta(seconds=30),
        stale_before=observed_at - timedelta(seconds=120),
    )
    assert probe.acquired

    repo.record_probe_observation(
        owner_token=probe.owner_token,
        intent=ZsxqRunIntent.WATCH.value,
        surface="timeline",
        state=PageState.ready,
        reason_code=unowned_reason,
        evidence_ref=None,
        observed_at=observed_at,
        recorded_at=observed_at,
    )
    repo.release_probe_lease(owner_token=probe.owner_token)

    latest = repo.latest_actual_observation()
    snapshot = _snapshot_all(str(db_path))
    repo.close()

    assert latest is not None
    assert latest["reason_code"] == PageState.ready.value
    assert unowned_reason not in repr(snapshot)


# ═══════════════════════════════════════════════════════════════════
# Test 10 — terminal run fields coexist with observation projection
# ═══════════════════════════════════════════════════════════════════


def test_health_read_retains_terminal_run_fields_with_observation(
    tmp_path,
) -> None:
    """When both a terminal run and an observation exist, the observation
    controls the health state while run fields (last_run_id, last_status,
    last_finished_at) remain populated in every result path.
    """
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    # 1. Create and finish a run
    run_clock = datetime(2026, 7, 12, 9, 0, 0, tzinfo=UTC)
    run_deadline = run_clock + timedelta(seconds=60)
    acq = repo.acquire_or_coalesce(
        intent=ZsxqRunIntent.SYNC.value,
        trigger=ZsxqRunTrigger.SCHEDULE.value,
        now=run_clock,
        deadline_at=run_deadline,
        stale_before=run_clock - timedelta(seconds=120),
    )
    assert acq.acquired
    finished_at = run_clock + timedelta(seconds=5)
    repo.finish_run(
        run_id=acq.run_id,
        owner_token=acq.owner_token,
        status=ZsxqRunStatus.SUCCEEDED.value,
        changed_count=5,
        finished_at=finished_at,
    )

    # 2. Seed an observation (rate_limited → degraded) — this should
    #    control the health state while run fields remain.
    obs_clock = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    probe = repo.acquire_probe_lease(
        now=obs_clock,
        deadline_at=obs_clock + timedelta(seconds=30),
        stale_before=obs_clock - timedelta(seconds=120),
    )
    assert probe.acquired
    repo.record_probe_observation(
        owner_token=probe.owner_token,
        intent=ZsxqRunIntent.SYNC.value,
        surface="zsxq",
        state=PageState.rate_limited,
        reason_code=PageState.rate_limited.value,
        evidence_ref=None,
        observed_at=obs_clock,
        recorded_at=obs_clock,
    )
    repo.release_probe_lease(owner_token=probe.owner_token)

    # 3. Health
    eval_time = obs_clock + timedelta(seconds=30)
    fake_adapter = _FakeAdapter()
    module = ZsxqScraperModule(
        repository=repo,
        adapter=fake_adapter,  # type: ignore[arg-type]
        clock=lambda: eval_time,
    )
    result = module.health(ZsxqHealthRequest(probe=False))
    repo.close()

    # ── observation controls health state ──────────────────────────
    assert result.state == ZsxqHealthState.DEGRADED.value
    assert result.page_state == PageState.rate_limited.value
    assert result.observed_at == obs_clock.isoformat()
    assert result.evaluated_at == eval_time.isoformat()

    # ── run fields are additive and populated ──────────────────────
    assert result.last_run_id == acq.run_id
    assert result.last_status == ZsxqRunStatus.SUCCEEDED.value
    assert result.last_finished_at == finished_at.isoformat()

    assert result.reason_code == PageState.rate_limited.value

    assert result.schema_version == HEALTH_CONTRACT_VERSION


# ═══════════════════════════════════════════════════════════════════
# Test 11 — active probe lease is BUSY without creating a run
# ═══════════════════════════════════════════════════════════════════


def test_active_probe_lease_without_observation_is_busy_without_run(
    tmp_path,
) -> None:
    """An active probe lease (owner_kind=probe, run_id=NULL) with no
    observation still returns BUSY with active_run_id=None.  No run is
    created and the read is side-effect-free.
    """
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    # Acquire a probe lease — do NOT release it
    now = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
    probe = repo.acquire_probe_lease(
        now=now,
        deadline_at=now + timedelta(seconds=30),
        stale_before=now - timedelta(seconds=120),
    )
    assert probe.acquired
    assert not probe.active_run_id  # probe owns no run (NULL → "")

    before = _snapshot_all(str(db_path))
    fake_adapter = _FakeAdapter()
    eval_time = now + timedelta(seconds=5)

    module = ZsxqScraperModule(
        repository=repo,
        adapter=fake_adapter,  # type: ignore[arg-type]
        clock=lambda: eval_time,
    )
    result = module.health(ZsxqHealthRequest(probe=False))

    after = _snapshot_all(str(db_path))

    # Release the probe lease so we can cleanly close the repo
    repo.release_probe_lease(owner_token=probe.owner_token)
    repo.close()

    # ── assertions ─────────────────────────────────────────────────
    assert fake_adapter.call_count == 0
    for table in _APP_TABLES:
        assert before[table] == after[table], f"Table {table} changed during health read"

    assert result.state == ZsxqHealthState.BUSY.value
    assert result.active_run_id is None  # probe lease, not a run
    assert result.evaluated_at == eval_time.isoformat()
    assert result.observed_at == ""
    assert result.page_state == ""
    assert result.schema_version == HEALTH_CONTRACT_VERSION

    # Zero runs created
    assert len(after["runs"]) == 0


# ═══════════════════════════════════════════════════════════════════
# Test 12 — latest observation ordering
# ═══════════════════════════════════════════════════════════════════


def test_latest_observation_orders_by_observed_at_then_sequence(
    tmp_path,
) -> None:
    """The health projection selects the latest observation by
    ``observed_at DESC, seq DESC`` — even when insertion order differs
    from chronological order, and equal-time ties break on sequence.
    """
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))

    base = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)

    # Insert three observations in *reverse* chronological order.
    # obs-C: latest time, inserted first → should win
    # obs-B: middle time, inserted second
    # obs-A: earliest time, inserted last

    def _seed(state: PageState, obs_time: datetime) -> None:
        probe = repo.acquire_probe_lease(
            now=obs_time,
            deadline_at=obs_time + timedelta(seconds=10),
            stale_before=obs_time - timedelta(seconds=120),
        )
        assert probe.acquired
        repo.record_probe_observation(
            owner_token=probe.owner_token,
            intent=ZsxqRunIntent.SYNC.value,
            surface="zsxq",
            state=state,
            reason_code=state.value,
            evidence_ref=None,
            observed_at=obs_time,
            recorded_at=obs_time,
        )
        repo.release_probe_lease(owner_token=probe.owner_token)

    # Insert C first (latest time, will have lowest seq)
    _seed(PageState.ready, base + timedelta(seconds=20))
    # Insert B second (middle time)
    _seed(PageState.rate_limited, base + timedelta(seconds=10))
    # Insert A last (earliest time, will have highest seq)
    _seed(PageState.control_failure, base)

    eval_time = base + timedelta(seconds=30)
    fake_adapter = _FakeAdapter()
    module = ZsxqScraperModule(
        repository=repo,
        adapter=fake_adapter,  # type: ignore[arg-type]
        clock=lambda: eval_time,
    )
    before = _snapshot_all(str(db_path))
    result = module.health(ZsxqHealthRequest(probe=False))
    after = _snapshot_all(str(db_path))
    repo.close()

    assert fake_adapter.call_count == 0
    for table in _APP_TABLES:
        assert before[table] == after[table], f"Table {table} changed during health read"

    # obs-C has the latest observed_at → should be selected
    assert result.page_state == PageState.ready.value
    assert result.state == ZsxqHealthState.HEALTHY.value
    assert result.observed_at == (base + timedelta(seconds=20)).isoformat()

    # ── equal-time tie-break on seq ────────────────────────────────
    db_path2 = tmp_path / "tiebreak.db"
    repo2 = ScraperRuntimeRepository(str(db_path2))

    same_time = datetime(2026, 7, 12, 11, 0, 0, tzinfo=UTC)

    # Insert first at same_time → lower seq
    _seed2(repo2, PageState.ready, same_time)
    # Insert second at same_time → higher seq → should win tie-break
    _seed2(repo2, PageState.control_failure, same_time)

    eval2 = same_time + timedelta(seconds=10)
    module2 = ZsxqScraperModule(
        repository=repo2,
        adapter=_FakeAdapter(),  # type: ignore[arg-type]
        clock=lambda: eval2,
    )
    result2 = module2.health(ZsxqHealthRequest(probe=False))
    repo2.close()

    # Higher seq wins when observed_at ties
    assert result2.page_state == PageState.control_failure.value
    assert result2.state == ZsxqHealthState.UNAVAILABLE.value


def _seed2(repo: ScraperRuntimeRepository, state: PageState, obs_time: datetime) -> None:
    probe = repo.acquire_probe_lease(
        now=obs_time,
        deadline_at=obs_time + timedelta(seconds=10),
        stale_before=obs_time - timedelta(seconds=120),
    )
    assert probe.acquired
    repo.record_probe_observation(
        owner_token=probe.owner_token,
        intent=ZsxqRunIntent.SYNC.value,
        surface="zsxq",
        state=state,
        reason_code=state.value,
        evidence_ref=None,
        observed_at=obs_time,
        recorded_at=obs_time,
    )
    repo.release_probe_lease(owner_token=probe.owner_token)


# ═══════════════════════════════════════════════════════════════════
# Test 13 — watch READY must not mask sync failure
# ═══════════════════════════════════════════════════════════════════


def test_health_read_watch_ready_does_not_mask_sync_failure(tmp_path) -> None:
    """A newer watch/probe success cannot hide the latest sync failure."""
    db_path = tmp_path / "ledger.db"
    repo = ScraperRuntimeRepository(str(db_path))
    base = datetime(2026, 7, 13, 9, 0, 0, tzinfo=UTC)

    def _record(intent: str, state: PageState, observed_at: datetime) -> None:
        probe = repo.acquire_probe_lease(
            now=observed_at,
            deadline_at=observed_at + timedelta(seconds=30),
            stale_before=observed_at - timedelta(seconds=120),
        )
        assert probe.acquired
        repo.record_probe_observation(
            owner_token=probe.owner_token,
            intent=intent,
            surface="timeline",
            state=state,
            reason_code=state.value,
            evidence_ref=None,
            observed_at=observed_at,
            recorded_at=observed_at,
        )
        repo.release_probe_lease(owner_token=probe.owner_token)

    sync_failure_at = base
    _record(ZsxqRunIntent.SYNC.value, PageState.control_failure, sync_failure_at)
    _record(ZsxqRunIntent.WATCH.value, PageState.ready, base + timedelta(seconds=10))

    before = _snapshot_all(str(db_path))
    adapter = _FakeAdapter()
    module = ZsxqScraperModule(
        repository=repo,
        adapter=adapter,  # type: ignore[arg-type]
        clock=lambda: base + timedelta(seconds=20),
    )

    result = module.health(ZsxqHealthRequest(probe=False))
    after = _snapshot_all(str(db_path))

    assert adapter.call_count == 0
    for table in _APP_TABLES:
        assert before[table] == after[table], f"Table {table} changed during health read"

    assert result.state == ZsxqHealthState.UNAVAILABLE.value
    assert result.page_state == PageState.control_failure.value
    assert result.reason_code == PageState.control_failure.value
    assert result.observed_at == sync_failure_at.isoformat()

    sync_recovered_at = base + timedelta(seconds=30)
    latest_watch_at = base + timedelta(seconds=40)
    _record(ZsxqRunIntent.SYNC.value, PageState.ready, sync_recovered_at)
    _record(ZsxqRunIntent.WATCH.value, PageState.ready, latest_watch_at)

    recovered_before = _snapshot_all(str(db_path))
    recovered_module = ZsxqScraperModule(
        repository=repo,
        adapter=adapter,  # type: ignore[arg-type]
        clock=lambda: base + timedelta(seconds=50),
    )
    recovered = recovered_module.health(ZsxqHealthRequest(probe=False))
    recovered_after = _snapshot_all(str(db_path))
    repo.close()

    for table in _APP_TABLES:
        assert recovered_before[table] == recovered_after[table]
    assert adapter.call_count == 0
    assert recovered.state == ZsxqHealthState.HEALTHY.value
    assert recovered.page_state == PageState.ready.value
    assert recovered.observed_at == latest_watch_at.isoformat()
