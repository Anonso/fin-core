"""Gate 2B / B2a — Health observation + episode + outbox UoW, RED tests only.

These tests establish the repository-level contract for the already-approved,
already-present schema-v2 health tables. The future production seam under test
is ``ScraperRuntimeRepository.record_probe_observation``, which does not exist
yet — every honest RED is ``AttributeError``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from fin_analyse.scraper.page_assessment import PageState
from fin_analyse.scraper.runtime_repository import (
    LeaseLostError,
    ScraperRuntimeRepository,
)

# A fixed deterministic base wall-clock so no test depends on real time.
_BASE = datetime(2026, 7, 12, 9, 0, 0, tzinfo=UTC)


def _probe_lease(repo, *, now=None, deadline_seconds=300):
    """Acquire a fresh probe lease and return its ``ProbeLeaseAcquisition``."""
    now = now or _BASE
    return repo.acquire_probe_lease(
        now=now,
        deadline_at=now + timedelta(seconds=deadline_seconds),
        stale_before=now - timedelta(seconds=120),
    )


def _read_rows(db_path, sql, params=()):
    """Open a separate connection, set row_factory, execute SELECT, close in finally."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ── Test 1 ────────────────────────────────────────────────────────────────


def test_requires_user_probe_atomically_writes_observation_episode_and_outbox(
    tmp_path,
):
    """Loop over LOGIN_REQUIRED and CHALLENGE; for each isolated temporary v2
    ledger acquire a probe lease, record once, and after GREEN assert one
    observation, one open episode and one outbox row with exact state/reason,
    kind='requires_user', subject_type='health_episode', subject matching
    episode, action login->maintain_chrome_login / challenge->resolve_challenge,
    dedupe requires_user:health_episode:<episode_id>, source observed_at
    preserved, sanitized fingerprint preserved, zero runs, and the same probe
    lease still held."""

    cases = [
        (PageState.login_required, "maintain_chrome_login"),
        (PageState.challenge, "resolve_challenge"),
    ]

    for idx, (state, expected_action) in enumerate(cases):
        db_path = str(tmp_path / f"ledger_1_{idx}.sqlite3")
        repo = ScraperRuntimeRepository(db_path)
        now = _BASE + timedelta(seconds=idx)
        evidence_ref = "abcdef1234567890abcdef1234567890abcdef12"

        lease = _probe_lease(repo, now=now)
        assert lease.acquired is True

        before_lease = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])

        # The call under test — does not exist yet (honest RED).
        repo.record_probe_observation(
            owner_token=lease.owner_token,
            intent="sync",
            surface="timeline",
            state=state,
            reason_code=state.value,
            evidence_ref=evidence_ref,
            observed_at=now,
            recorded_at=now,
        )

        # ── After-GREEN assertions (unreachable in RED) ──────────────────

        after_lease = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])
        assert after_lease == before_lease

        # One observation row with correct fields.
        obs_rows = _read_rows(db_path, "SELECT * FROM health_observations ORDER BY seq")
        assert len(obs_rows) == 1
        obs = dict(obs_rows[0])
        assert obs["intent"] == "sync"
        assert obs["surface"] == "timeline"
        assert obs["state"] == state.value
        assert obs["observed_at"] == now.isoformat()
        assert obs["evidence_ref"] == evidence_ref
        episode_id = obs["episode_id"]
        assert episode_id is not None
        assert len(episode_id) > 0

        # One open episode.
        ep_rows = _read_rows(db_path, "SELECT * FROM health_episodes ORDER BY opened_at")
        assert len(ep_rows) == 1
        ep = dict(ep_rows[0])
        assert ep["episode_id"] == episode_id
        assert ep["intent"] == "sync"
        assert ep["surface"] == "timeline"
        assert ep["reason_code"] == state.value
        assert ep["status"] == "open"
        assert ep["opened_at"] == now.isoformat()
        assert ep["closed_at"] is None

        # One outbox row.
        out_rows = _read_rows(db_path, "SELECT * FROM scraper_outbox ORDER BY seq")
        assert len(out_rows) == 1
        out = dict(out_rows[0])
        assert out["kind"] == "requires_user"
        assert out["subject_type"] == "health_episode"
        assert out["subject_id"] == episode_id
        assert out["reason_code"] == state.value
        assert out["action_code"] == expected_action
        assert out["evidence_ref"] == evidence_ref
        assert out["occurred_at"] == now.isoformat()
        assert out["delivered_at"] is None
        expected_dedupe = f"requires_user:health_episode:{episode_id}"
        assert out["dedupe_key"] == expected_dedupe

        # Zero runs (probe creates no run).
        run_count = _read_rows(db_path, "SELECT COUNT(*) FROM runs")[0][0]
        assert run_count == 0

        repo.close()


# ── Test 2 ────────────────────────────────────────────────────────────────


def test_repeated_requires_user_probe_reuses_open_episode_and_dedupes_outbox(
    tmp_path,
):
    """Two acquire->record->explicit-release cycles for the same LOGIN_REQUIRED
    (intent='sync', surface='timeline'); assert through persisted SQLite rows
    that both observations share one stable open episode and the second call
    creates no additional outbox; do not assert or prescribe a return value in
    this RED slice."""

    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    evidence_ref = "bbbb2222cccc3333dddd4444eeee5555ffff6666"

    # -- Cycle 1 --
    lease1 = _probe_lease(repo)
    assert lease1.acquired is True

    repo.record_probe_observation(
        owner_token=lease1.owner_token,
        intent="sync",
        surface="timeline",
        state=PageState.login_required,
        reason_code=PageState.login_required.value,
        evidence_ref=evidence_ref,
        observed_at=_BASE,
        recorded_at=_BASE,
    )

    # After GREEN: explicitly release the first lease.
    repo.release_probe_lease(owner_token=lease1.owner_token)

    # -- Cycle 2 --
    second_now = _BASE + timedelta(seconds=10)
    lease2 = _probe_lease(repo, now=second_now)
    assert lease2.acquired is True

    repo.record_probe_observation(
        owner_token=lease2.owner_token,
        intent="sync",
        surface="timeline",
        state=PageState.login_required,
        reason_code=PageState.login_required.value,
        evidence_ref=evidence_ref,
        observed_at=second_now,
        recorded_at=second_now,
    )

    # After GREEN: explicitly release the second lease.
    repo.release_probe_lease(owner_token=lease2.owner_token)

    # ── After-GREEN assertions (unreachable in RED) ──────────────────────

    # Two observations.
    obs_rows = _read_rows(db_path, "SELECT * FROM health_observations ORDER BY seq")
    assert len(obs_rows) == 2

    # Both observations share one stable open episode.
    ep_rows = _read_rows(db_path, "SELECT * FROM health_episodes ORDER BY opened_at")
    assert len(ep_rows) == 1
    ep = dict(ep_rows[0])
    assert ep["status"] == "open"
    assert obs_rows[0]["episode_id"] == ep["episode_id"]
    assert obs_rows[1]["episode_id"] == ep["episode_id"]

    # Only one outbox row (second call deduped).
    out_rows = _read_rows(db_path, "SELECT * FROM scraper_outbox ORDER BY seq")
    assert len(out_rows) == 1

    # The single outbox row references the shared episode.
    out = dict(out_rows[0])
    assert out["subject_id"] == ep["episode_id"]
    assert out["kind"] == "requires_user"

    repo.close()


# ── Test 3 ────────────────────────────────────────────────────────────────


def test_ready_closes_open_episode_and_later_requires_user_opens_new_episode(
    tmp_path,
):
    """LOGIN_REQUIRED -> READY -> LOGIN_REQUIRED in three explicit probe leases;
    READY associates/closes the first episode at READY observed_at without a
    second outbox, later abnormal opens a different episode, final totals
    observations=3, episodes=2, outbox=2."""

    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    evidence_ref = "cafe0000cafe0000cafe0000cafe0000cafe0000"

    t0 = _BASE
    t1 = t0 + timedelta(seconds=30)
    t2 = t0 + timedelta(seconds=60)

    # -- Step 1: LOGIN_REQUIRED --
    lease1 = _probe_lease(repo, now=t0)
    assert lease1.acquired is True

    repo.record_probe_observation(
        owner_token=lease1.owner_token,
        intent="sync",
        surface="timeline",
        state=PageState.login_required,
        reason_code=PageState.login_required.value,
        evidence_ref=evidence_ref,
        observed_at=t0,
        recorded_at=t0,
    )

    repo.release_probe_lease(owner_token=lease1.owner_token)

    # -- Step 2: READY --
    lease2 = _probe_lease(repo, now=t1)
    assert lease2.acquired is True

    repo.record_probe_observation(
        owner_token=lease2.owner_token,
        intent="sync",
        surface="timeline",
        state=PageState.ready,
        reason_code=PageState.ready.value,
        evidence_ref=evidence_ref,
        observed_at=t1,
        recorded_at=t1,
    )

    repo.release_probe_lease(owner_token=lease2.owner_token)

    # -- Step 3: LOGIN_REQUIRED again --
    lease3 = _probe_lease(repo, now=t2)
    assert lease3.acquired is True

    repo.record_probe_observation(
        owner_token=lease3.owner_token,
        intent="sync",
        surface="timeline",
        state=PageState.login_required,
        reason_code=PageState.login_required.value,
        evidence_ref=evidence_ref,
        observed_at=t2,
        recorded_at=t2,
    )

    repo.release_probe_lease(owner_token=lease3.owner_token)

    # ── After-GREEN assertions (unreachable in RED) ──────────────────────

    # Three observations.
    obs_rows = _read_rows(db_path, "SELECT * FROM health_observations ORDER BY seq")
    assert len(obs_rows) == 3

    # Two episodes.
    ep_rows = _read_rows(db_path, "SELECT * FROM health_episodes ORDER BY opened_at")
    assert len(ep_rows) == 2

    ep1 = dict(ep_rows[0])
    ep2 = dict(ep_rows[1])

    # First episode: opened at t0 by LOGIN_REQUIRED, closed at t1 by READY.
    assert ep1["status"] == "closed"
    assert ep1["opened_at"] == t0.isoformat()
    assert ep1["closed_at"] == t1.isoformat()
    assert ep1["reason_code"] == PageState.login_required.value

    # First episode id is non-empty; second episode id is different.
    assert len(ep1["episode_id"]) > 0
    assert len(ep2["episode_id"]) > 0
    assert ep2["episode_id"] != ep1["episode_id"]

    # Second episode: opened at t2, still open.
    assert ep2["status"] == "open"
    assert ep2["opened_at"] == t2.isoformat()
    assert ep2["closed_at"] is None
    assert ep2["reason_code"] == PageState.login_required.value

    # Observations link to correct episodes.
    assert obs_rows[0]["episode_id"] == ep1["episode_id"]
    assert obs_rows[0]["state"] == PageState.login_required.value
    assert obs_rows[1]["episode_id"] == ep1["episode_id"]
    assert obs_rows[1]["state"] == PageState.ready.value
    assert obs_rows[2]["episode_id"] == ep2["episode_id"]
    assert obs_rows[2]["state"] == PageState.login_required.value

    # Two outbox rows (one per requires_user episode, none for READY).
    out_rows = _read_rows(db_path, "SELECT * FROM scraper_outbox ORDER BY seq")
    assert len(out_rows) == 2
    ep1_out = dict(out_rows[0])
    ep2_out = dict(out_rows[1])

    # Subject-id set is exactly the two episode IDs.
    assert {ep1_out["subject_id"], ep2_out["subject_id"]} == {
        ep1["episode_id"],
        ep2["episode_id"],
    }

    # ep1_out binds episode 1 and t0.
    assert ep1_out["kind"] == "requires_user"
    assert ep1_out["subject_type"] == "health_episode"
    assert ep1_out["subject_id"] == ep1["episode_id"]
    assert ep1_out["dedupe_key"] == f"requires_user:health_episode:{ep1['episode_id']}"
    assert ep1_out["reason_code"] == PageState.login_required.value
    assert ep1_out["action_code"] == "maintain_chrome_login"
    assert ep1_out["occurred_at"] == t0.isoformat()
    assert ep1_out["delivered_at"] is None

    # ep2_out binds the distinct episode 2 and t2.
    assert ep2_out["kind"] == "requires_user"
    assert ep2_out["subject_type"] == "health_episode"
    assert ep2_out["subject_id"] == ep2["episode_id"]
    assert ep2_out["dedupe_key"] == f"requires_user:health_episode:{ep2['episode_id']}"
    assert ep2_out["reason_code"] == PageState.login_required.value
    assert ep2_out["action_code"] == "maintain_chrome_login"
    assert ep2_out["occurred_at"] == t2.isoformat()
    assert ep2_out["delivered_at"] is None

    repo.close()


# ── Test 4 ────────────────────────────────────────────────────────────────


def test_non_requires_user_probe_records_observation_without_episode_or_outbox(
    tmp_path,
):
    """Loop over READY, RATE_LIMITED, LOADING_TIMEOUT, DOM_CHANGED, WRONG_PAGE
    and CONTROL_FAILURE, each with explicit acquire->record->release; assert
    only six observations and no episode/outbox/run."""

    non_requires_states = [
        PageState.ready,
        PageState.rate_limited,
        PageState.loading_timeout,
        PageState.dom_changed,
        PageState.wrong_page,
        PageState.control_failure,
    ]

    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)

    for idx, state in enumerate(non_requires_states):
        now = _BASE + timedelta(seconds=idx * 10)
        evidence_ref = f"e{idx:04d}" + "ff" * 30

        lease = _probe_lease(repo, now=now)
        assert lease.acquired is True

        repo.record_probe_observation(
            owner_token=lease.owner_token,
            intent="watch",
            surface="digest",
            state=state,
            reason_code=state.value,
            evidence_ref=evidence_ref,
            observed_at=now,
            recorded_at=now,
        )

        repo.release_probe_lease(owner_token=lease.owner_token)

    # ── After-GREEN assertions (unreachable in RED) ──────────────────────

    # Six observations, one per state.
    obs_rows = _read_rows(db_path, "SELECT * FROM health_observations ORDER BY seq")
    assert len(obs_rows) == 6
    for i, state in enumerate(non_requires_states):
        assert obs_rows[i]["state"] == state.value
        assert obs_rows[i]["intent"] == "watch"
        assert obs_rows[i]["surface"] == "digest"

    # No episodes.
    ep_count = _read_rows(db_path, "SELECT COUNT(*) FROM health_episodes")[0][0]
    assert ep_count == 0

    # No outbox.
    out_count = _read_rows(db_path, "SELECT COUNT(*) FROM scraper_outbox")[0][0]
    assert out_count == 0

    # No runs.
    run_count = _read_rows(db_path, "SELECT COUNT(*) FROM runs")[0][0]
    assert run_count == 0

    repo.close()


# ── Test 5 ────────────────────────────────────────────────────────────────


def test_probe_health_uow_rolls_back_all_rows_when_outbox_insert_fails(
    tmp_path,
):
    """After repository open/acquire, install a TEMP BEFORE INSERT trigger on
    scraper_outbox using only the repository's SQLite connection and
    RAISE(ABORT, 'injected outbox failure'); expect sqlite3.IntegrityError
    from LOGIN_REQUIRED; assert observation/episode/outbox all remain zero and
    the original probe lease/token remains intact, then drop the TEMP trigger
    and explicitly release."""

    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    now = _BASE
    evidence_ref = "fail0000fail0000fail0000fail0000fail0000fail00"

    lease = _probe_lease(repo, now=now)
    assert lease.acquired is True
    original_token = lease.owner_token

    # Install TEMP BEFORE INSERT trigger that raises on any outbox insert.
    repo._conn.execute(
        "CREATE TEMP TRIGGER tr_inject_outbox_failure "
        "BEFORE INSERT ON scraper_outbox "
        "BEGIN "
        "    SELECT RAISE(ABORT, 'injected outbox failure'); "
        "END"
    )

    before_lease = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])

    # LOGIN_REQUIRED must raise IntegrityError because the UoW rolls back.
    with pytest.raises(sqlite3.IntegrityError):
        repo.record_probe_observation(
            owner_token=original_token,
            intent="sync",
            surface="timeline",
            state=PageState.login_required,
            reason_code=PageState.login_required.value,
            evidence_ref=evidence_ref,
            observed_at=now,
            recorded_at=now,
        )

    # ── After-GREEN assertions (partially reachable: trigger is installed,
    #    but record_probe_observation doesn't exist yet → AttributeError,
    #    not IntegrityError. The full path exercises after GREEN.) ────────

    after_lease = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])
    assert after_lease == before_lease

    # Zero health rows — the UoW rolled back everything.
    obs_count = _read_rows(db_path, "SELECT COUNT(*) FROM health_observations")[0][0]
    assert obs_count == 0

    ep_count = _read_rows(db_path, "SELECT COUNT(*) FROM health_episodes")[0][0]
    assert ep_count == 0

    out_count = _read_rows(db_path, "SELECT COUNT(*) FROM scraper_outbox")[0][0]
    assert out_count == 0

    # Drop the TEMP trigger and explicitly release.
    repo._conn.execute("DROP TRIGGER IF EXISTS tr_inject_outbox_failure")
    repo.release_probe_lease(owner_token=original_token)
    assert _read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1") == []

    repo.close()


# ── Test 6 ────────────────────────────────────────────────────────────────


def test_reclaimed_probe_owner_cannot_record_observation_or_change_new_lease(
    tmp_path,
):
    """Let owner B reclaim expired owner A, then A's record call must raise
    existing LeaseLostError, write zero health rows, and leave B's exact lease
    unchanged."""

    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)

    t0 = _BASE
    evidence_ref = "aabbccddaabbccddaabbccddaabbccddaabbccdd"

    # Owner A acquires a probe lease.
    lease_a = _probe_lease(repo, now=t0, deadline_seconds=10)
    assert lease_a.acquired is True
    token_a = lease_a.owner_token

    # Time advances past the stale window: owner A's lease is now stale.
    t1 = t0 + timedelta(seconds=200)

    # Owner B reclaims the stale lease.
    lease_b = _probe_lease(repo, now=t1)
    assert lease_b.acquired is True
    token_b = lease_b.owner_token
    assert token_b != token_a

    before_lease = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])

    # Owner A's record call must raise LeaseLostError.
    with pytest.raises(LeaseLostError):
        repo.record_probe_observation(
            owner_token=token_a,
            intent="sync",
            surface="timeline",
            state=PageState.login_required,
            reason_code=PageState.login_required.value,
            evidence_ref=evidence_ref,
            observed_at=t0,
            recorded_at=t0,
        )

    # ── After-GREEN assertions (unreachable in RED) ──────────────────────

    after_lease = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])
    assert after_lease == before_lease

    # Zero health rows.
    obs_count = _read_rows(db_path, "SELECT COUNT(*) FROM health_observations")[0][0]
    assert obs_count == 0

    ep_count = _read_rows(db_path, "SELECT COUNT(*) FROM health_episodes")[0][0]
    assert ep_count == 0

    out_count = _read_rows(db_path, "SELECT COUNT(*) FROM scraper_outbox")[0][0]
    assert out_count == 0

    repo.release_probe_lease(owner_token=token_b)
    assert _read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1") == []
    repo.close()


# ── Test 7 ────────────────────────────────────────────────────────────────


def test_probe_observation_at_or_after_deadline_is_fenced_without_writes(
    tmp_path,
):
    """With a physically matching token, recorded_at == persisted deadline_at
    must raise LeaseLostError, write zero health rows, preserve the lease, and
    allow explicit release."""

    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    evidence_ref = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    deadline_seconds = 120
    now = _BASE
    lease = _probe_lease(repo, now=now, deadline_seconds=deadline_seconds)
    assert lease.acquired is True
    token = lease.owner_token

    deadline_at = now + timedelta(seconds=deadline_seconds)

    before_lease = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])

    # recorded_at == deadline_at must be fenced.
    with pytest.raises(LeaseLostError):
        repo.record_probe_observation(
            owner_token=token,
            intent="sync",
            surface="timeline",
            state=PageState.login_required,
            reason_code=PageState.login_required.value,
            evidence_ref=evidence_ref,
            observed_at=deadline_at,
            recorded_at=deadline_at,
        )

    # ── After-GREEN assertions (unreachable in RED) ──────────────────────

    after_lease = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])
    assert after_lease == before_lease

    # Zero health rows.
    obs_count = _read_rows(db_path, "SELECT COUNT(*) FROM health_observations")[0][0]
    assert obs_count == 0

    ep_count = _read_rows(db_path, "SELECT COUNT(*) FROM health_episodes")[0][0]
    assert ep_count == 0

    out_count = _read_rows(db_path, "SELECT COUNT(*) FROM scraper_outbox")[0][0]
    assert out_count == 0

    # Explicit release still works.
    repo.release_probe_lease(owner_token=token)
    assert _read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1") == []
    repo.close()


# ── Test 8 ────────────────────────────────────────────────────────────────


def test_probe_observation_commit_keeps_lease_until_explicit_fenced_release(
    tmp_path,
):
    """A READY commit keeps the same lease, explicit existing fenced release
    removes it, and the committed observation remains."""

    db_path = str(tmp_path / "ledger.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    now = _BASE
    evidence_ref = "c0ffee11c0ffee11c0ffee11c0ffee11c0ffee11"

    lease = _probe_lease(repo, now=now)
    assert lease.acquired is True
    token = lease.owner_token

    before_lease = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])

    # READY observation commit.
    repo.record_probe_observation(
        owner_token=token,
        intent="sync",
        surface="timeline",
        state=PageState.ready,
        reason_code=PageState.ready.value,
        evidence_ref=evidence_ref,
        observed_at=now,
        recorded_at=now,
    )

    # ── After-GREEN assertions (unreachable in RED) ──────────────────────

    after_lease = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])
    assert after_lease == before_lease

    # Explicit fenced release removes the lease.
    repo.release_probe_lease(owner_token=token)
    assert _read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1") == []

    # Committed observation remains.
    obs_rows = _read_rows(db_path, "SELECT * FROM health_observations ORDER BY seq")
    assert len(obs_rows) == 1
    obs = dict(obs_rows[0])
    assert obs["state"] == PageState.ready.value
    assert obs["intent"] == "sync"
    assert obs["surface"] == "timeline"
    assert obs["evidence_ref"] == evidence_ref
    assert obs["observed_at"] == now.isoformat()

    # No episode or outbox for READY.
    ep_count = _read_rows(db_path, "SELECT COUNT(*) FROM health_episodes")[0][0]
    assert ep_count == 0

    out_count = _read_rows(db_path, "SELECT COUNT(*) FROM scraper_outbox")[0][0]
    assert out_count == 0

    repo.close()


# ── Test 9 ────────────────────────────────────────────────────────────────


def test_corrupt_multiple_open_episodes_fail_before_health_writes(
    tmp_path,
):
    """Seed exactly two open health_episodes for the same (intent='sync',
    surface='timeline') through an independent SQLite connection, capture the
    repository connection SQL trace, call record_probe_observation with
    LOGIN_REQUIRED and the valid token, and expect RuntimeError containing
    'Corrupt state'. Assert the trace proves the bounded open-episode SELECT
    contains LIMIT 2 and happens before any health-table write; in this
    corruption path no INSERT INTO health_observations or INSERT INTO
    scraper_outbox may be attempted. Also assert zero observations/outbox, the
    exact two seeded episodes and complete probe lease remain byte-for-byte
    unchanged, then clear the trace callback and explicitly release the lease.
    Do not use source/AST introspection."""

    db_path = str(tmp_path / "ledger_corrupt.sqlite3")
    repo = ScraperRuntimeRepository(db_path)
    now = _BASE
    evidence_ref = "corrupt0corrupt0corrupt0corrupt0corrupt0corr"

    # Acquire a probe lease.
    lease = _probe_lease(repo, now=now)
    assert lease.acquired is True
    token = lease.owner_token

    # Seed exactly two open episodes via an independent connection.
    import uuid

    ep1_id = f"ep{uuid.uuid4().hex[:16]}"
    ep2_id = f"ep{uuid.uuid4().hex[:16]}"

    seed_conn = sqlite3.connect(db_path)
    try:
        seed_conn.execute(
            "INSERT INTO health_episodes "
            "(episode_id, intent, surface, reason_code, status, opened_at, closed_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, NULL)",
            (ep1_id, "sync", "timeline", "login_required", now.isoformat()),
        )
        seed_conn.execute(
            "INSERT INTO health_episodes "
            "(episode_id, intent, surface, reason_code, status, opened_at, closed_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, NULL)",
            (ep2_id, "sync", "timeline", "login_required", now.isoformat()),
        )
        seed_conn.commit()
    finally:
        seed_conn.close()

    # Capture the repository connection's SQL trace.
    trace_statements: list[str] = []

    def _trace_callback(stmt: str) -> None:
        trace_statements.append(stmt)

    repo._conn.set_trace_callback(_trace_callback)

    # Snapshot the seeded episodes and lease before the call.
    ep_rows_before = _read_rows(db_path, "SELECT * FROM health_episodes ORDER BY episode_id")
    lease_before = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])

    # Call must raise RuntimeError with 'Corrupt state'.
    with pytest.raises(RuntimeError, match="Corrupt state"):
        repo.record_probe_observation(
            owner_token=token,
            intent="sync",
            surface="timeline",
            state=PageState.login_required,
            reason_code=PageState.login_required.value,
            evidence_ref=evidence_ref,
            observed_at=now,
            recorded_at=now,
        )

    # ── After-GREEN assertions ──────────────────────────────────────────

    # 1. The trace proves the bounded open-episode SELECT contains LIMIT 2
    #    and references health_episodes.
    bounded_selects = [s for s in trace_statements if "LIMIT 2" in s and "health_episodes" in s]
    assert len(bounded_selects) >= 1, (
        f"No bounded LIMIT 2 SELECT on health_episodes found in trace: {trace_statements}"
    )

    # 2. The bounded SELECT happens before any health-table write.
    first_bounded_idx = min(
        i for i, s in enumerate(trace_statements) if "LIMIT 2" in s and "health_episodes" in s
    )
    for i in range(first_bounded_idx):
        assert "INSERT INTO health_observations" not in trace_statements[i], (
            f"health_observations INSERT found before bounded SELECT at index {i}"
        )

    # 3. No INSERT INTO health_observations or INSERT INTO scraper_outbox
    #    anywhere in the trace — the corruption path writes nothing.
    obs_inserts = [s for s in trace_statements if "INSERT INTO health_observations" in s]
    outbox_inserts = [s for s in trace_statements if "INSERT INTO scraper_outbox" in s]
    assert len(obs_inserts) == 0, f"Found health_observations INSERT: {obs_inserts}"
    assert len(outbox_inserts) == 0, f"Found scraper_outbox INSERT: {outbox_inserts}"

    # 4. Zero observations and outbox in the database.
    obs_count = _read_rows(db_path, "SELECT COUNT(*) FROM health_observations")[0][0]
    assert obs_count == 0
    out_count = _read_rows(db_path, "SELECT COUNT(*) FROM scraper_outbox")[0][0]
    assert out_count == 0

    # 5. The exact two seeded episodes remain byte-for-byte unchanged.
    ep_rows_after = _read_rows(db_path, "SELECT * FROM health_episodes ORDER BY episode_id")
    assert len(ep_rows_after) == 2
    for i in range(2):
        assert dict(ep_rows_after[i]) == dict(ep_rows_before[i])

    # 6. The probe lease remains byte-for-byte unchanged.
    lease_after = dict(_read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1")[0])
    assert lease_after == lease_before

    # Clear the trace callback and explicitly release the lease.
    repo._conn.set_trace_callback(None)
    repo.release_probe_lease(owner_token=token)
    assert _read_rows(db_path, "SELECT * FROM active_lease WHERE id = 1") == []

    repo.close()
