"""SQLite control ledger for the ZSXQ scraper module (schema v4).

This is the single state owner / Unit of Work for the module. Schema v4 owns seven
application tables: ``schema_version``, ``runs``, ``active_lease``,
``health_observations``, ``health_episodes`` and ``scraper_outbox``. Gate 2B/B0
adds an observation-owned reason code to the historical v2 shape; v4 adds the
artifact-bound ``capture_ingests`` recovery ledger. Malformed or non-v4 ledgers
fail closed during ordinary opens.

Invariants owned by this module:

- ``runs.run_id`` is globally unique across processes. It is built from a
  database-assigned sequence (SQLite AUTOINCREMENT ``seq``) plus a random
  UUID4 — never an in-process counter and never ``uuid.uuid7`` (absent on
  Python 3.13).
- At most one row exists in ``active_lease`` (enforced by ``CHECK(id = 1)``),
  so at most one owner — a run OR a probe — holds the lease at any time, even
  across processes.
- Acquisition runs inside ``BEGIN IMMEDIATE`` so two processes serialize: the
  loser observes the winner's lease and coalesces instead of creating a second
  run. A stale *run* lease is reclaimed by marking its owning run
  ``interrupted`` (persisted) before the fresh run/lease is created. A stale
  *probe* lease is only deleted/replaced — it owns no run, so recovery never
  fabricates or interrupts a run.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from .cdp_diagnostics import CdpProbeControlFailureCode
from .contracts import (
    FAILURE_REASON_ALLOWLIST,
    SCHEMA_VERSION,
    TERMINAL_RUN_STATUSES,
    ZsxqRunIntent,
    ZsxqRunStatus,
    ZsxqRunTrigger,
)
from .page_assessment import PageState

_RUNNING = "running"
_NORMAL_BUSY_TIMEOUT_MS = 5_000
_WAL_SETUP_TIMEOUT_SECONDS = 5.0
_WAL_SETUP_BUSY_SLICE_MS = 100
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6
_PERSISTABLE_REASON_CODES = frozenset(state.value for state in PageState) | frozenset(
    code.value for code in CdpProbeControlFailureCode
)

#: Lease owner kinds. A ``run`` lease owns a row in ``runs``; a ``probe`` lease
#: owns no run (``run_id`` is NULL) and is used by the health probe path.
_OWNER_RUN = "run"
_OWNER_PROBE = "probe"

_CAPTURE_CLAIMED = "CLAIMED"
_CAPTURE_BUSINESS_TERMINAL = "BUSINESS_TERMINAL"
_CAPTURE_PUBLICATION_PREPARED = "PUBLICATION_PREPARED"
_CAPTURE_COMPLETE = "COMPLETE"
_CAPTURE_BUSINESS_TERMINAL_STATUSES = frozenset(
    {
        ZsxqRunStatus.SUCCEEDED.value,
        ZsxqRunStatus.NO_CHANGE.value,
        ZsxqRunStatus.FAILED.value,
        ZsxqRunStatus.DEADLINE_EXCEEDED.value,
    }
)
CAPTURE_RECOVERY_COMPLETION_SCHEMA_VERSION = "fin.zsxq-capture-recovery-completion/v1"
_CAPTURE_INGEST_WIRE_SCHEMA_VERSION = "fin.zsxq-capture-ingest/v1"
_CAPTURE_CONSUMED_RECEIPT_SCHEMA_VERSION = "fin.zsxq-capture-consumed/v1"
_CAPTURE_REJECTED_RECEIPT_SCHEMA_VERSION = "fin.zsxq-capture-rejected/v1"
_CAPTURE_PHASES = frozenset(
    {
        _CAPTURE_CLAIMED,
        _CAPTURE_BUSINESS_TERMINAL,
        _CAPTURE_PUBLICATION_PREPARED,
        _CAPTURE_COMPLETE,
    }
)
_MAX_CAPTURE_STATE_JSON_BYTES = 2 * 1024 * 1024
_CAPTURE_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Frozen base DDL for the deployed v3 operator input. V3 replaces the
#: observation table below with an observation-owned ``reason_code`` column.
_V2_TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        id      INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        seq           INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id        TEXT UNIQUE,
        intent        TEXT NOT NULL,
        trigger       TEXT NOT NULL,
        status        TEXT NOT NULL,
        attempt       INTEGER NOT NULL DEFAULT 1,
        changed_count INTEGER NOT NULL DEFAULT 0,
        started_at    TEXT NOT NULL,
        finished_at   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS active_lease (
        id           INTEGER PRIMARY KEY CHECK (id = 1),
        owner_kind   TEXT NOT NULL CHECK (owner_kind IN ('run', 'probe')),
        run_id       TEXT,
        owner_token  TEXT NOT NULL,
        acquired_at  TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        deadline_at  TEXT NOT NULL,
        CHECK ((owner_kind = 'run' AND run_id IS NOT NULL) OR (owner_kind = 'probe' AND run_id IS NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS health_observations (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        intent       TEXT NOT NULL,
        surface      TEXT NOT NULL,
        state        TEXT NOT NULL,
        observed_at  TEXT NOT NULL,
        episode_id   TEXT,
        evidence_ref TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS health_episodes (
        episode_id  TEXT PRIMARY KEY,
        intent      TEXT NOT NULL,
        surface     TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        status      TEXT NOT NULL,
        opened_at   TEXT NOT NULL,
        closed_at   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scraper_outbox (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key   TEXT NOT NULL UNIQUE,
        kind         TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        subject_id   TEXT NOT NULL,
        reason_code  TEXT NOT NULL,
        action_code  TEXT NOT NULL,
        evidence_ref TEXT,
        occurred_at  TEXT NOT NULL,
        delivered_at TEXT
    )
    """,
)

# Schema v2 above is a frozen historical input oracle. Schema v3 changes only
# health_observations so each observation owns its exact sanitized reason.
_V3_HEALTH_OBSERVATIONS_DDL = """
    CREATE TABLE IF NOT EXISTS health_observations (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        intent       TEXT NOT NULL,
        surface      TEXT NOT NULL,
        state        TEXT NOT NULL,
        reason_code  TEXT NOT NULL,
        observed_at  TEXT NOT NULL,
        episode_id   TEXT,
        evidence_ref TEXT
    )
    """
_V3_TABLE_DDL: tuple[str, ...] = (
    *_V2_TABLE_DDL[:3],
    _V3_HEALTH_OBSERVATIONS_DDL,
    *_V2_TABLE_DDL[4:],
)

_CAPTURE_INGESTS_DDL = """
    CREATE TABLE IF NOT EXISTS capture_ingests (
        artifact_run_id       TEXT NOT NULL PRIMARY KEY,
        content_sha256        TEXT NOT NULL,
        phase                 TEXT NOT NULL CHECK (
            phase IN ('CLAIMED', 'BUSINESS_TERMINAL', 'PUBLICATION_PREPARED', 'COMPLETE')
        ),
        ingest_run_id         TEXT UNIQUE,
        prior_g_json          TEXT NOT NULL,
        business_json         TEXT,
        publication_plan_json TEXT,
        completion_json       TEXT,
        FOREIGN KEY (ingest_run_id) REFERENCES runs(run_id),
        CHECK (
            (phase = 'CLAIMED' AND ingest_run_id IS NULL AND business_json IS NULL
             AND publication_plan_json IS NULL AND completion_json IS NULL)
            OR
            (phase = 'BUSINESS_TERMINAL' AND ingest_run_id IS NOT NULL
             AND business_json IS NOT NULL AND publication_plan_json IS NULL
             AND completion_json IS NULL)
            OR
            (phase = 'PUBLICATION_PREPARED' AND ingest_run_id IS NOT NULL
             AND business_json IS NOT NULL AND publication_plan_json IS NOT NULL
             AND completion_json IS NULL)
            OR
            (phase = 'COMPLETE' AND ingest_run_id IS NOT NULL
             AND business_json IS NOT NULL AND completion_json IS NOT NULL)
        )
    )
    """

_V4_TABLE_DDL: tuple[str, ...] = (*_V3_TABLE_DDL, _CAPTURE_INGESTS_DDL)


def _normalize_sql(sql: str) -> str:
    """Collapse whitespace runs to single spaces so SQL text compares whitespace-tolerantly.

    The approved v2 contract is the *logical* schema, not the exact ``sqlite_master``
    byte string: a canonical table re-created with whitespace-only differences must
    still reopen. Raw byte equality is therefore forbidden as an oracle.
    """
    return re.sub(r"\s+", " ", sql).strip()


def _table_fingerprint(conn: sqlite3.Connection, table: str) -> dict:
    """A cross-database logical fingerprint of one table's enforced shape.

    Captures the ordered full ``PRAGMA table_xinfo`` tuple
    ``(cid, name, type, notnull, dflt_value, pk, hidden)`` and a name-independent
    semantic index signature: for every index its ``PRAGMA index_list``
    ``(unique, origin, partial)`` plus the complete ``PRAGMA index_xinfo`` rows,
    collected into a stable sorted tuple. Raw ``sqlite_autoindex`` names and their
    order are deliberately excluded so the signature is a valid cross-database oracle.
    """
    xinfo = tuple(tuple(row) for row in conn.execute(f"PRAGMA table_xinfo({table})").fetchall())
    index_sigs = []
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        idx_name, unique, origin, partial = idx[1], idx[2], idx[3], idx[4]
        idx_cols = tuple(
            tuple(col) for col in conn.execute(f"PRAGMA index_xinfo('{idx_name}')").fetchall()
        )
        index_sigs.append((unique, origin, partial, idx_cols))
    return {"xinfo": xinfo, "indexes": tuple(sorted(index_sigs, key=repr))}


def _build_canonical(table_ddl: tuple[str, ...]) -> dict:
    conn = sqlite3.connect(":memory:")
    try:
        for ddl in table_ddl:
            conn.execute(ddl)
        objects = frozenset(
            (row[0], row[1])
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
            ).fetchall()
        )
        tables: dict[str, dict] = {}
        for _type, name in objects:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()[0]
            fingerprint = _table_fingerprint(conn, name)
            fingerprint["sql"] = _normalize_sql(sql)
            tables[name] = fingerprint
        return {"objects": objects, "tables": tables}
    finally:
        conn.close()


@lru_cache(maxsize=1)
def _v3_canonical() -> dict:
    """Frozen deployed-v3 fingerprint accepted only by operator migration."""
    return _build_canonical(_V3_TABLE_DDL)


@lru_cache(maxsize=1)
def _v4_canonical() -> dict:
    """Canonical current-schema fingerprint built from the exact v4 DDL."""
    return _build_canonical(_V4_TABLE_DDL)


class LeaseLostError(RuntimeError):
    """Raised when an ``owner_token`` no longer owns the single active lease.

    ``heartbeat``, ``finish_run`` and ``release_probe_lease`` are fenced by
    ``owner_token``: once a stale lease is reclaimed (its run marked
    ``interrupted`` and the lease handed to a new owner, or a probe lease
    deleted/replaced), the old owner is fenced and every canonical write fails
    visibly instead of overwriting the reclaimed run or the new owner's lease.
    """


class SchemaVersionError(RuntimeError):
    """Raised when the on-disk ledger is not an exact, supported schema shape.

    Ordinary opens accept exact v4 only. The explicit operator seam additionally
    accepts the frozen exact v3 input; every other version or shape is refused
    unchanged.
    """


class OperatorMigrationFinalizationError(RuntimeError):
    """Migration committed or may have committed, but finalization was not clean."""

    def __init__(self, message: str, *, wrote: bool | None) -> None:
        super().__init__(message)
        self.wrote = wrote


class CaptureIdentityConflictError(RuntimeError):
    """The artifact run id is already bound to a different content hash."""


@dataclass(frozen=True)
class CaptureIngestRecord:
    artifact_run_id: str
    content_sha256: str
    phase: str
    ingest_run_id: str | None
    prior_g_json: str
    business_json: str | None
    publication_plan_json: str | None
    completion_json: str | None


@dataclass
class LeaseAcquisition:
    """Outcome of :meth:`ScraperRuntimeRepository.acquire_or_coalesce`."""

    acquired: bool
    run_id: str = ""
    owner_token: str = ""
    attempt: int = 1
    started_at: str = ""
    #: When coalesced onto a *run* lease, the run id of the already-active run.
    #: A coalesce onto a *probe* lease carries no run id (``None``).
    active_run_id: str | None = ""
    #: When stale recovery ran, the run id that was marked ``interrupted``.
    recovered_run_id: str = ""


@dataclass
class ProbeLeaseAcquisition:
    """Outcome of :meth:`ScraperRuntimeRepository.acquire_probe_lease`."""

    acquired: bool
    owner_token: str = ""
    #: When coalesced, the active owner's run id (``None`` when a probe owns it).
    active_run_id: str | None = ""
    #: When a stale *run* lease was reclaimed, the run id marked ``interrupted``.
    recovered_run_id: str = ""


class ScraperRuntimeRepository:
    """The module's canonical SQLite state owner (schema v4)."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        try:
            self._apply_connection_pragmas()
            self._migrate()
            self._enable_wal()
        except BaseException:
            # Fail-closed opens (e.g. unknown future version) must not leak the
            # connection / hold the WAL file open.
            self._conn.close()
            raise

    # ── setup ────────────────────────────────────────────────────

    def _apply_connection_pragmas(self) -> None:
        """Configure connection-local behavior before inspecting the ledger."""
        preflight_timeout_ms = max(
            1,
            min(_WAL_SETUP_BUSY_SLICE_MS, int(_WAL_SETUP_TIMEOUT_SECONDS * 1_000)),
        )
        self._conn.execute(f"PRAGMA busy_timeout={preflight_timeout_ms}")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def _enable_wal(self) -> None:
        """Enable persistent WAL mode only after the schema is accepted."""
        cur = self._conn.cursor()
        try:
            deadline = time.monotonic() + _WAL_SETUP_TIMEOUT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining < 0.001:
                    raise RuntimeError("SQLite WAL setup did not converge within 5 seconds")
                # A PRAGMA call may itself block for busy_timeout.  Bound each
                # attempt by the smaller of one short slice and the total
                # remaining setup budget; never install the normal 5-second
                # timeout until WAL has actually succeeded.
                attempt_timeout_ms = max(
                    1,
                    min(_WAL_SETUP_BUSY_SLICE_MS, int(remaining * 1_000)),
                )
                cur.execute(f"PRAGMA busy_timeout={attempt_timeout_ms}")
                try:
                    row = cur.execute("PRAGMA journal_mode=WAL").fetchone()
                except sqlite3.OperationalError as exc:
                    if _sqlite_base_error_code(exc) not in (
                        _SQLITE_BUSY,
                        _SQLITE_LOCKED,
                    ):
                        raise
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "SQLite WAL setup did not converge within 5 seconds"
                        ) from None
                else:
                    if row is not None and str(row[0]).lower() == "wal":
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("SQLite WAL setup did not converge within 5 seconds")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("SQLite WAL setup did not converge within 5 seconds")
                time.sleep(min(0.01, remaining))
            cur.execute(f"PRAGMA busy_timeout={_NORMAL_BUSY_TIMEOUT_MS}")
        finally:
            cur.close()

    @classmethod
    def operator_migrate_v3_to_v4(cls, db_path: str | Path) -> str:
        """Explicitly cut one stopped, exact v3 ledger over to exact v4."""
        path = Path(db_path)
        expected_identity = _validate_operator_runtime_db(path)
        if _inspect_operator_runtime_read_only(path, expected_identity) == SCHEMA_VERSION:
            return "ALREADY_CURRENT"
        from .scheduler_handoff_lock import (
            HandoffLockMode,
            hold_scheduler_handoff_lock,
            scheduler_handoff_lock_path,
        )

        lock_path = scheduler_handoff_lock_path(path)
        commit_state = "PRECOMMIT"
        try:
            with hold_scheduler_handoff_lock(lock_path, mode=HandoffLockMode.EXCLUSIVE):
                if _validate_operator_runtime_db(path) != expected_identity:
                    raise ValueError("runtime_db_identity_drifted")
                _require_no_capture_owner_sidecars(path)
                uri = f"file:{quote(str(path), safe='/')}?mode=rw"
                conn = sqlite3.connect(uri, uri=True, isolation_level=None)
                conn.row_factory = sqlite3.Row
                owner = cls.__new__(cls)
                owner._db_path = str(path)
                owner._conn = conn
                try:
                    if _validate_operator_runtime_db(path) != expected_identity:
                        raise ValueError("runtime_db_identity_drifted")
                    conn.execute(f"PRAGMA busy_timeout={_NORMAL_BUSY_TIMEOUT_MS}")
                    conn.execute("PRAGMA foreign_keys=ON")
                    cur = conn.cursor()
                    cur.execute("BEGIN IMMEDIATE")
                    try:
                        owner._validate_singleton(cur)
                        version = owner._read_version(cur)
                        if version == SCHEMA_VERSION:
                            owner._validate_v4_exact(cur)
                            disposition = "ALREADY_CURRENT"
                        elif version == 3:
                            owner._validate_v3_exact(cur)
                            if cur.execute("SELECT 1 FROM active_lease LIMIT 1").fetchone():
                                raise SchemaVersionError(
                                    "v3 ledger has an active lease; operator migration refused"
                                )
                            cur.execute(_CAPTURE_INGESTS_DDL)
                            cur.execute(
                                "UPDATE schema_version SET version = ? "
                                "WHERE id = 1 AND version = 3",
                                (SCHEMA_VERSION,),
                            )
                            if cur.rowcount != 1:
                                raise SchemaVersionError("v3 to v4 schema-version CAS failed")
                            owner._v4_operator_migration_fault_hook(cur)
                            owner._validate_v4_exact(cur)
                            disposition = "MIGRATED"
                        else:
                            raise SchemaVersionError(
                                f"ledger schema version {version} is not an exact v3/v4 "
                                "operator input"
                            )
                        quick_check = cur.execute("PRAGMA quick_check").fetchone()
                        if quick_check is None or quick_check[0] != "ok":
                            raise SchemaVersionError("v4 ledger quick_check failed")
                        if cur.execute("PRAGMA foreign_key_check").fetchone() is not None:
                            raise SchemaVersionError("v4 ledger foreign_key_check failed")
                        if _validate_operator_runtime_db(path) != expected_identity:
                            raise ValueError("runtime_db_identity_drifted")
                        if disposition == "MIGRATED":
                            commit_state = "COMMITTING"
                        cur.execute("COMMIT")
                        if disposition == "MIGRATED":
                            commit_state = "COMMITTED"
                    except BaseException:
                        if conn.in_transaction:
                            cur.execute("ROLLBACK")
                        raise
                    finally:
                        cur.close()
                finally:
                    conn.close()
                if _validate_operator_runtime_db(path) != expected_identity:
                    raise ValueError("runtime_db_identity_drifted")
        except OperatorMigrationFinalizationError:
            raise
        except Exception as error:
            if commit_state == "COMMITTING":
                raise OperatorMigrationFinalizationError(
                    "runtime_db_commit_outcome_unknown",
                    wrote=None,
                ) from error
            if commit_state == "COMMITTED":
                raise OperatorMigrationFinalizationError(
                    "runtime_db_post_commit_finalization_failed",
                    wrote=True,
                ) from error
            raise
        return disposition

    @staticmethod
    def _v4_operator_migration_fault_hook(cur: sqlite3.Cursor) -> None:
        """No-op fault seam proving the v3→v4 transaction rolls back."""

    def _migrate(self) -> None:
        """Open the ledger fail-closed: inspect an existing schema BEFORE any DDL.

        A fresh database creates the v4 schema and seeds the singleton version
        row inside one ``BEGIN IMMEDIATE`` transaction, so concurrent fresh opens
        serialize and converge on exactly one row. An existing exact v4 database
        reopens in place. Historical or future versions, corrupted singletons and
        shape mismatches are rejected before any schema side effect can commit;
        the sole supported v3→v4 cutover is the explicit operator entry point.
        """
        cur = self._conn.cursor()
        deadline = time.monotonic() + _WAL_SETUP_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining < 0.001:
                cur.close()
                raise RuntimeError(
                    "SQLite schema preflight did not converge within the startup bound"
                )
            attempt_timeout_ms = max(
                1,
                min(_WAL_SETUP_BUSY_SLICE_MS, int(remaining * 1_000)),
            )
            cur.execute(f"PRAGMA busy_timeout={attempt_timeout_ms}")
            try:
                cur.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if _sqlite_base_error_code(exc) not in (_SQLITE_BUSY, _SQLITE_LOCKED):
                    cur.close()
                    raise
                if time.monotonic() >= deadline:
                    cur.close()
                    raise RuntimeError(
                        "SQLite schema preflight did not converge within the startup bound"
                    ) from None
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                continue
            break
        try:
            has_version_table = (
                cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
                ).fetchone()
                is not None
            )
            if not has_version_table:
                # Fresh only when the database carries no user schema at all. A
                # missing schema_version alongside any other non-internal object
                # (table/index/view/trigger) is a foreign/partial ledger, not a
                # fresh one; refuse it here — before any DDL — so the rollback
                # leaves the on-disk schema and data logically unchanged.
                preexisting = cur.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
                ).fetchone()
                if preexisting is not None:
                    raise SchemaVersionError(
                        "fresh open requires an empty database, but found pre-existing "
                        f"object {preexisting['name']!r} without schema_version (fail-closed)"
                    )
                # Fresh database → create v4 and seed the singleton version row.
                for ddl in _V4_TABLE_DDL:
                    cur.execute(ddl)
                cur.execute(
                    "INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, ?)",
                    (SCHEMA_VERSION,),
                )
                self._validate_version(cur, SCHEMA_VERSION)
                self._validate_v4_exact(cur)
            else:
                # Inspect + reject/upgrade before touching the schema shape.
                self._validate_singleton(cur)
                version = self._read_version(cur)
                if version == SCHEMA_VERSION:
                    self._validate_v4_exact(cur)
                    for ddl in _V4_TABLE_DDL:  # idempotent no-ops on the exact shape
                        cur.execute(ddl)
                else:
                    raise SchemaVersionError(
                        f"ledger schema version {version} requires an explicit operator "
                        f"migration to v{SCHEMA_VERSION} (fail-closed)"
                    )
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    @staticmethod
    def _read_version(cur: sqlite3.Cursor) -> int:
        rows = cur.execute("SELECT version FROM schema_version").fetchall()
        if len(rows) != 1:
            raise SchemaVersionError(
                f"schema_version must hold exactly one row; found {len(rows)} (fail-closed)"
            )
        return int(rows[0]["version"])

    @staticmethod
    def _validate_version(cur: sqlite3.Cursor, expected: int) -> None:
        """Fail closed unless ``schema_version`` holds exactly one ``expected`` row."""
        version = ScraperRuntimeRepository._read_version(cur)
        if version != expected:
            raise SchemaVersionError(
                f"ledger schema version {version} is not supported v{expected} (fail-closed)"
            )

    def _validate_v3_exact(self, cur: sqlite3.Cursor) -> None:
        """Validate the frozen historical v3 operator input exactly."""
        self._validate_exact(cur, canonical=_v3_canonical(), version=3)

    def _validate_v4_exact(self, cur: sqlite3.Cursor) -> None:
        """Validate the current v4 schema exactly before open or commit."""
        self._validate_exact(cur, canonical=_v4_canonical(), version=4)

    def _validate_exact(self, cur: sqlite3.Cursor, *, canonical: dict, version: int) -> None:
        actual_objects = frozenset(
            (row["type"], row["name"])
            for row in cur.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
            ).fetchall()
        )
        if actual_objects != canonical["objects"]:
            canonical_names = {name for _type, name in canonical["objects"]}
            actual_names = {name for _type, name in actual_objects}
            raise SchemaVersionError(
                f"v{version} ledger object inventory is not the exact approved tables "
                f"(unexpected={sorted(actual_names - canonical_names)}, "
                f"missing={sorted(canonical_names - actual_names)}); fail-closed"
            )

        for name, want in canonical["tables"].items():
            got = _table_fingerprint(self._conn, name)
            if got["xinfo"] != want["xinfo"]:
                raise SchemaVersionError(
                    f"v{version} ledger table {name!r} column shape "
                    "(table_xinfo) mismatch; fail-closed"
                )
            if got["indexes"] != want["indexes"]:
                raise SchemaVersionError(
                    f"v{version} ledger table {name!r} index shape mismatch; fail-closed"
                )
            sql_row = cur.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            if sql_row is None or _normalize_sql(sql_row["sql"]) != want["sql"]:
                raise SchemaVersionError(
                    f"v{version} ledger table {name!r} definition "
                    "(CHECK/UNIQUE/AUTOINCREMENT) mismatch; fail-closed"
                )

        rows = [
            tuple(row)
            for row in cur.execute("SELECT id, version FROM schema_version ORDER BY id").fetchall()
        ]
        if rows != [(1, version)]:
            raise SchemaVersionError(
                f"v{version} ledger schema_version rows must be exactly [(1, {version})]; "
                "fail-closed"
            )

    @staticmethod
    def _validate_singleton(cur: sqlite3.Cursor) -> None:
        """Fail closed unless ``schema_version`` is a DB-enforced ``CHECK (id = 1)`` singleton.

        Probe by inserting a second row (id = 2) inside a SAVEPOINT that is always
        rolled back and released. Only a ``CHECK`` constraint violation proves the
        on-disk table carries the singleton guard; a successful insert, a different
        constraint (PK/UNIQUE/trigger), a missing ``id`` column or any other
        database error means the table is not the enforced shape and the ledger
        is refused. This never parses ``sqlite_master`` SQL.
        """
        ok = False
        cur.execute("SAVEPOINT singleton_probe")
        try:
            cur.execute("INSERT INTO schema_version (id, version) VALUES (2, ?)", (SCHEMA_VERSION,))
        except sqlite3.IntegrityError as exc:
            ok = exc.sqlite_errorname == "SQLITE_CONSTRAINT_CHECK"
        except sqlite3.Error:
            ok = False
        finally:
            cur.execute("ROLLBACK TO singleton_probe")
            cur.execute("RELEASE singleton_probe")
        if not ok:
            raise SchemaVersionError(
                "schema_version is not a DB-enforced singleton (CHECK (id = 1)); fail-closed"
            )

    # ── capture ingest recovery ──────────────────────────────────

    def claim_capture_ingest(
        self,
        *,
        artifact_run_id: str,
        content_sha256: str,
        prior_g_json: str,
    ) -> CaptureIngestRecord:
        """Create or read the one durable claim for an immutable artifact identity."""
        _validate_capture_identity(artifact_run_id, content_sha256)
        _validate_capture_state_json(prior_g_json, label="prior_g_json")
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT * FROM capture_ingests WHERE artifact_run_id = ?",
                (artifact_run_id,),
            ).fetchone()
            if row is not None:
                if row["content_sha256"] != content_sha256:
                    raise CaptureIdentityConflictError(
                        "artifact run id is already bound to different content"
                    )
                cur.execute("COMMIT")
                return _capture_record(self._conn, row)
            cur.execute(
                "INSERT INTO capture_ingests "
                "(artifact_run_id, content_sha256, phase, prior_g_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    artifact_run_id,
                    content_sha256,
                    _CAPTURE_CLAIMED,
                    prior_g_json,
                ),
            )
            row = cur.execute(
                "SELECT * FROM capture_ingests WHERE artifact_run_id = ?",
                (artifact_run_id,),
            ).fetchone()
            assert row is not None
            cur.execute("COMMIT")
            return _capture_record(self._conn, row)
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    def read_capture_ingest(
        self, *, artifact_run_id: str, content_sha256: str
    ) -> CaptureIngestRecord | None:
        """Read one claim, rejecting reuse of its artifact id with another hash."""
        _validate_capture_identity(artifact_run_id, content_sha256)
        row = self._conn.execute(
            "SELECT * FROM capture_ingests WHERE artifact_run_id = ?",
            (artifact_run_id,),
        ).fetchone()
        if row is None:
            return None
        if row["content_sha256"] != content_sha256:
            raise CaptureIdentityConflictError(
                "artifact run id is already bound to different content"
            )
        return _capture_record(self._conn, row)

    def complete_capture_ingest(
        self,
        *,
        artifact_run_id: str,
        content_sha256: str,
        ingest_run_id: str,
        business_json: str,
        completion_json: str,
        audit_json: str,
        knowledge_base_root: str | Path | None = None,
    ) -> CaptureIngestRecord:
        """Persist the canonical final projection before filesystem publication."""
        _validate_capture_identity(artifact_run_id, content_sha256)
        if not ingest_run_id or len(ingest_run_id) > 128 or not ingest_run_id.isprintable():
            raise ValueError("ingest_run_id is invalid")
        _validate_capture_state_json(business_json, label="business_json")
        _validate_capture_state_json(completion_json, label="completion_json")
        _validate_capture_state_json(audit_json, label="audit_json")
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT * FROM capture_ingests WHERE artifact_run_id = ?",
                (artifact_run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("capture ingest claim is missing")
            if row["content_sha256"] != content_sha256:
                raise CaptureIdentityConflictError(
                    "artifact run id is already bound to different content"
                )
            if row["phase"] == _CAPTURE_COMPLETE:
                decode_capture_completion_projection(
                    completion_json,
                    artifact_run_id=artifact_run_id,
                    content_sha256=content_sha256,
                    ingest_run_id=ingest_run_id,
                    business_json=business_json,
                    prior_g_json=str(row["prior_g_json"]),
                    publication_plan_json=(
                        str(row["publication_plan_json"])
                        if row["publication_plan_json"] is not None
                        else None
                    ),
                    expected_audit_json=audit_json,
                )
                if (
                    row["ingest_run_id"] != ingest_run_id
                    or row["business_json"] != business_json
                    or row["completion_json"] != completion_json
                ):
                    raise RuntimeError("completed capture ingest projection conflicts")
                cur.execute("COMMIT")
                return _capture_record(self._conn, row)
            if row["phase"] not in {
                _CAPTURE_BUSINESS_TERMINAL,
                _CAPTURE_PUBLICATION_PREPARED,
            }:
                raise RuntimeError("capture ingest phase is invalid")
            if row["ingest_run_id"] != ingest_run_id or row["business_json"] != business_json:
                raise RuntimeError("capture business projection conflicts")
            current_phase = str(row["phase"])
            run_row = cur.execute(
                "SELECT status FROM runs WHERE run_id = ?",
                (ingest_run_id,),
            ).fetchone()
            if run_row is None or run_row["status"] not in TERMINAL_RUN_STATUSES:
                raise RuntimeError("capture terminal run is invalid")
            if (
                run_row["status"] in {ZsxqRunStatus.SUCCEEDED.value, ZsxqRunStatus.NO_CHANGE.value}
                and current_phase != _CAPTURE_PUBLICATION_PREPARED
            ):
                raise RuntimeError("capture publication plan is required")
            if (
                run_row["status"]
                not in {ZsxqRunStatus.SUCCEEDED.value, ZsxqRunStatus.NO_CHANGE.value}
                and current_phase != _CAPTURE_BUSINESS_TERMINAL
            ):
                raise RuntimeError("failed capture cannot own a publication plan")
            decoded_completion = decode_capture_completion_projection(
                completion_json,
                artifact_run_id=artifact_run_id,
                content_sha256=content_sha256,
                ingest_run_id=ingest_run_id,
                business_json=business_json,
                prior_g_json=str(row["prior_g_json"]),
                publication_plan_json=(
                    str(row["publication_plan_json"])
                    if row["publication_plan_json"] is not None
                    else None
                ),
                expected_audit_json=audit_json,
            )
            if current_phase == _CAPTURE_PUBLICATION_PREPARED:
                _validate_capture_g_publication_owner(
                    decoded_completion,
                    publication_plan_json=str(row["publication_plan_json"]),
                    knowledge_base_root=knowledge_base_root,
                )
            cur.execute(
                "UPDATE capture_ingests SET phase = ?, completion_json = ? "
                "WHERE artifact_run_id = ? AND content_sha256 = ? AND phase = ? "
                "AND ingest_run_id = ? AND business_json = ?",
                (
                    _CAPTURE_COMPLETE,
                    completion_json,
                    artifact_run_id,
                    content_sha256,
                    current_phase,
                    ingest_run_id,
                    business_json,
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("capture ingest completion CAS failed")
            row = cur.execute(
                "SELECT * FROM capture_ingests WHERE artifact_run_id = ?",
                (artifact_run_id,),
            ).fetchone()
            assert row is not None
            cur.execute("COMMIT")
            return _capture_record(self._conn, row)
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    def prepare_capture_publication(
        self,
        *,
        artifact_run_id: str,
        content_sha256: str,
        ingest_run_id: str,
        publication_plan_json: str,
    ) -> CaptureIngestRecord:
        """Persist the sole frozen G plan before any manifest publication."""
        _validate_capture_identity(artifact_run_id, content_sha256)
        if not ingest_run_id or len(ingest_run_id) > 128 or not ingest_run_id.isprintable():
            raise ValueError("ingest_run_id is invalid")
        _validate_capture_state_json(publication_plan_json, label="publication_plan_json")
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT * FROM capture_ingests WHERE artifact_run_id = ?",
                (artifact_run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("capture ingest claim is missing")
            if row["content_sha256"] != content_sha256:
                raise CaptureIdentityConflictError(
                    "artifact run id is already bound to different content"
                )
            run_row = cur.execute(
                "SELECT status FROM runs WHERE run_id = ?",
                (ingest_run_id,),
            ).fetchone()
            if run_row is None or run_row["status"] not in {
                ZsxqRunStatus.SUCCEEDED.value,
                ZsxqRunStatus.NO_CHANGE.value,
            }:
                raise RuntimeError("capture publication requires a successful terminal run")
            _validate_capture_publication_plan_json(publication_plan_json)
            if row["phase"] in {_CAPTURE_PUBLICATION_PREPARED, _CAPTURE_COMPLETE}:
                if (
                    row["ingest_run_id"] != ingest_run_id
                    or row["publication_plan_json"] != publication_plan_json
                ):
                    raise RuntimeError("capture publication plan conflicts")
                cur.execute("COMMIT")
                return _capture_record(self._conn, row)
            if (
                row["phase"] != _CAPTURE_BUSINESS_TERMINAL
                or row["ingest_run_id"] != ingest_run_id
                or row["business_json"] is None
            ):
                raise RuntimeError("capture business terminal is incomplete")
            cur.execute(
                "UPDATE capture_ingests SET phase = ?, publication_plan_json = ? "
                "WHERE artifact_run_id = ? AND phase = ?",
                (
                    _CAPTURE_PUBLICATION_PREPARED,
                    publication_plan_json,
                    artifact_run_id,
                    _CAPTURE_BUSINESS_TERMINAL,
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("capture publication-plan CAS failed")
            row = cur.execute(
                "SELECT * FROM capture_ingests WHERE artifact_run_id = ?",
                (artifact_run_id,),
            ).fetchone()
            assert row is not None
            cur.execute("COMMIT")
            return _capture_record(self._conn, row)
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    # ── lease acquisition / coalescing / stale recovery ──────────

    @staticmethod
    def _reclaim_stale_run(cur: sqlite3.Cursor, run_id: str, finished_at: str) -> None:
        """Mark a stale run-owned lease's run as ``interrupted`` atomically.

        Executes one conditional UPDATE that must match exactly one row.
        A rowcount other than 1 means the run relation is malformed (missing,
        already terminal, or non-running-unfinished) — the caller's surrounding
        ``BEGIN IMMEDIATE`` transaction is rolled back and a ``RuntimeError`` is
        raised.
        """
        cur.execute(
            "UPDATE runs SET status = 'interrupted', finished_at = ? "
            "WHERE run_id = ? AND status = 'running' AND finished_at IS NULL",
            (finished_at, run_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"Stale run reclaim: expected exactly 1 running/unfinished row for "
                f"run_id {run_id!r}, but UPDATE matched {cur.rowcount} row(s)"
            )

    def acquire_or_coalesce(
        self,
        *,
        intent: str,
        trigger: str,
        now: datetime,
        deadline_at: datetime,
        stale_before: datetime,
    ) -> LeaseAcquisition:
        """Acquire the single lease for a run, or coalesce onto the active owner.

        Serialized across processes via ``BEGIN IMMEDIATE``. If an active lease
        exists and is fresh, the caller coalesces: onto a *run* lease it reports
        that run's id; onto a *probe* lease it reports no run id (``None``) and
        creates no run. If the lease is stale it is reclaimed — a stale *run*
        lease marks its owning run ``interrupted`` (persisted) before a fresh
        run/lease is created; a stale *probe* lease is only deleted and never
        interrupts or fabricates a run.
        """
        now_iso = _iso(now)
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            lease = cur.execute("SELECT * FROM active_lease WHERE id = 1").fetchone()
            recovered_run_id = ""
            if lease is not None:
                fresh_heartbeat = _iso_ge(lease["heartbeat_at"], _iso(stale_before))
                within_deadline = _iso_lt(_iso(now), lease["deadline_at"])
                if fresh_heartbeat and within_deadline:
                    # A live owner still holds a renewable, un-expired lease →
                    # coalesce, create no run/history. A probe owner has run_id
                    # NULL, so the coalesced result carries no active run id.
                    cur.execute("COMMIT")
                    return LeaseAcquisition(
                        acquired=False,
                        active_run_id=lease["run_id"],
                    )
                # Stale heartbeat OR the total deadline has passed: the owner has
                # lost ownership. Reclaim it. Only a *run* owner has a run to mark
                # interrupted — a probe owner owns nothing, so we just drop it.
                if lease["owner_kind"] == _OWNER_RUN and lease["run_id"] is not None:
                    recovered_run_id = lease["run_id"]
                    self._reclaim_stale_run(cur, recovered_run_id, now_iso)
                cur.execute("DELETE FROM active_lease WHERE id = 1")

            # Create the fresh run: DB sequence + UUID4 identity.
            cur.execute(
                "INSERT INTO runs (run_id, intent, trigger, status, attempt, started_at) "
                "VALUES (NULL, ?, ?, ?, 1, ?)",
                (intent, trigger, _RUNNING, now_iso),
            )
            seq = cur.lastrowid
            assert seq is not None  # AUTOINCREMENT always assigns a rowid on INSERT
            run_id = f"r{seq:08d}-{uuid4().hex}"
            owner_token = uuid4().hex
            cur.execute("UPDATE runs SET run_id = ? WHERE seq = ?", (run_id, seq))
            cur.execute(
                "INSERT INTO active_lease "
                "(id, owner_kind, run_id, owner_token, acquired_at, heartbeat_at, deadline_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?)",
                (_OWNER_RUN, run_id, owner_token, now_iso, now_iso, _iso(deadline_at)),
            )
            cur.execute("COMMIT")
            return LeaseAcquisition(
                acquired=True,
                run_id=run_id,
                owner_token=owner_token,
                attempt=1,
                started_at=now_iso,
                recovered_run_id=recovered_run_id,
            )
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    def acquire_probe_lease(
        self,
        *,
        now: datetime,
        deadline_at: datetime,
        stale_before: datetime,
    ) -> ProbeLeaseAcquisition:
        """Acquire the single lease for a health probe (owns no run), or coalesce.

        Mirrors :meth:`acquire_or_coalesce`'s cross-process serialization and
        stale-recovery rules, but the acquired lease has ``owner_kind='probe'``
        and ``run_id`` NULL: a probe never creates a run. Reclaiming a stale
        *run* lease still marks that run ``interrupted``; reclaiming a stale
        *probe* lease only deletes it. This is the smallest fenced seam the later
        health checkpoint needs — B0 records no observation/episode/outbox.
        """
        now_iso = _iso(now)
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            lease = cur.execute("SELECT * FROM active_lease WHERE id = 1").fetchone()
            recovered_run_id = ""
            if lease is not None:
                fresh_heartbeat = _iso_ge(lease["heartbeat_at"], _iso(stale_before))
                within_deadline = _iso_lt(_iso(now), lease["deadline_at"])
                if fresh_heartbeat and within_deadline:
                    cur.execute("COMMIT")
                    return ProbeLeaseAcquisition(
                        acquired=False,
                        active_run_id=lease["run_id"],
                    )
                if lease["owner_kind"] == _OWNER_RUN and lease["run_id"] is not None:
                    recovered_run_id = lease["run_id"]
                    self._reclaim_stale_run(cur, recovered_run_id, now_iso)
                cur.execute("DELETE FROM active_lease WHERE id = 1")

            owner_token = uuid4().hex
            cur.execute(
                "INSERT INTO active_lease "
                "(id, owner_kind, run_id, owner_token, acquired_at, heartbeat_at, deadline_at) "
                "VALUES (1, ?, NULL, ?, ?, ?, ?)",
                (_OWNER_PROBE, owner_token, now_iso, now_iso, _iso(deadline_at)),
            )
            cur.execute("COMMIT")
            return ProbeLeaseAcquisition(
                acquired=True,
                owner_token=owner_token,
                recovered_run_id=recovered_run_id,
            )
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    def release_probe_lease(self, *, owner_token: str) -> None:
        """Release the probe lease, fenced by ``owner_token``.

        Raises :class:`LeaseLostError` unless the single lease is a probe lease
        owned by ``owner_token`` — a fenced/stale probe owner can neither delete
        a reclaimer's replacement lease nor another owner's run lease.
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            lease = cur.execute(
                "SELECT owner_kind, owner_token FROM active_lease WHERE id = 1"
            ).fetchone()
            if (
                lease is None
                or lease["owner_kind"] != _OWNER_PROBE
                or lease["owner_token"] != owner_token
            ):
                raise LeaseLostError("release_probe_lease: this token no longer owns a probe lease")
            cur.execute("DELETE FROM active_lease WHERE id = 1")
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    # ── health checkpoint ──────────────────────────────────────

    def record_probe_observation(
        self,
        *,
        owner_token: str,
        intent: str,
        surface: str,
        state: PageState,
        reason_code: str,
        evidence_ref: str | None,
        observed_at: datetime,
        recorded_at: datetime,
    ) -> None:
        """Record a health probe observation inside one ``BEGIN IMMEDIATE`` UoW.

        Fenced by the persisted singleton probe lease; raises
        :class:`LeaseLostError` unless the lease exists, has
        ``owner_kind='probe'``, exactly matches ``owner_token`` and satisfies
        ``recorded_at < deadline_at``.  The lease is read but never mutated.

        *LOGIN_REQUIRED* / *CHALLENGE* (requires_user) creates or reuses an
        open episode and writes exactly one ``requires_user`` outbox row per
        *new* episode.  *READY* closes a matching open episode (no outbox).
        All other states are observation-only.
        """
        observed_at_iso = _iso(observed_at)
        recorded_at_iso = _iso(recorded_at)
        state_value = state.value
        persisted_reason_code = _persistable_reason_code(reason_code, fallback=state_value)

        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            # ── lease fence ─────────────────────────────────────
            lease = cur.execute(
                "SELECT owner_kind, owner_token, deadline_at FROM active_lease WHERE id = 1"
            ).fetchone()
            if (
                lease is None
                or lease["owner_kind"] != _OWNER_PROBE
                or lease["owner_token"] != owner_token
            ):
                raise LeaseLostError(
                    "record_probe_observation: this token no longer owns a probe lease"
                )
            if _iso_ge(recorded_at_iso, lease["deadline_at"]):
                raise LeaseLostError(
                    "record_probe_observation: recorded_at at or after lease deadline"
                )

            requires_user = state in (PageState.login_required, PageState.challenge)
            is_ready = state is PageState.ready

            open_episode_id: str | None = None

            if requires_user or is_ready:
                # ── corrupt state: at most one open episode ─────
                # Bounded query (LIMIT 2) run *before* any health write:
                # finding two open episodes means the ledger is corrupt;
                # raise before inserting any row.
                open_eps = cur.execute(
                    "SELECT episode_id FROM health_episodes "
                    "WHERE intent = ? AND surface = ? AND status = 'open' "
                    "LIMIT 2",
                    (intent, surface),
                ).fetchall()
                if len(open_eps) > 1:
                    raise RuntimeError(
                        f"Corrupt state: {len(open_eps)} open episodes for "
                        f"intent={intent!r} surface={surface!r}"
                    )
                open_episode_id = open_eps[0]["episode_id"] if len(open_eps) == 1 else None

            # ── insert observation (episode_id NULL until linked) ──
            cur.execute(
                "INSERT INTO health_observations "
                "(intent, surface, state, reason_code, observed_at, episode_id, evidence_ref) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (
                    intent,
                    surface,
                    state_value,
                    persisted_reason_code,
                    observed_at_iso,
                    evidence_ref,
                ),
            )
            obs_seq: int = cur.lastrowid  # type: ignore[assignment]

            if requires_user or is_ready:
                if requires_user:
                    if open_episode_id is not None:
                        # Reuse existing open episode — no new outbox.
                        cur.execute(
                            "UPDATE health_observations SET episode_id = ? WHERE seq = ?",
                            (open_episode_id, obs_seq),
                        )
                    else:
                        # New episode: deterministic id from observation sequence.
                        episode_id = f"ep{obs_seq:08d}"
                        cur.execute(
                            "UPDATE health_observations SET episode_id = ? WHERE seq = ?",
                            (episode_id, obs_seq),
                        )
                        cur.execute(
                            "INSERT INTO health_episodes "
                            "(episode_id, intent, surface, reason_code, status, "
                            "opened_at, closed_at) "
                            "VALUES (?, ?, ?, ?, 'open', ?, NULL)",
                            (
                                episode_id,
                                intent,
                                surface,
                                persisted_reason_code,
                                observed_at_iso,
                            ),
                        )
                        # Outbox — plain INSERT (not OR IGNORE/REPLACE).
                        action_code = (
                            "maintain_chrome_login"
                            if state is PageState.login_required
                            else "resolve_challenge"
                        )
                        dedupe_key = f"requires_user:health_episode:{episode_id}"
                        cur.execute(
                            "INSERT INTO scraper_outbox "
                            "(dedupe_key, kind, subject_type, subject_id, reason_code, "
                            "action_code, evidence_ref, occurred_at, delivered_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                            (
                                dedupe_key,
                                "requires_user",
                                "health_episode",
                                episode_id,
                                persisted_reason_code,
                                action_code,
                                evidence_ref,
                                observed_at_iso,
                            ),
                        )

                elif is_ready and open_episode_id is not None:
                    # Link to the open episode and close it — no outbox.
                    cur.execute(
                        "UPDATE health_observations SET episode_id = ? WHERE seq = ?",
                        (open_episode_id, obs_seq),
                    )
                    cur.execute(
                        "UPDATE health_episodes SET status = 'closed', closed_at = ? "
                        "WHERE episode_id = ?",
                        (observed_at_iso, open_episode_id),
                    )
                # READY with no open episode: observation-only (episode_id stays NULL).

            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    def heartbeat(self, *, run_id: str, owner_token: str, at: datetime) -> None:
        """Renew the lease. Fenced by ``owner_token`` AND the persisted deadline.

        Raises :class:`LeaseLostError` when this ``(run_id, owner_token)`` no
        longer owns the single active lease, or when ``at`` is at/after the
        lease's persisted ``deadline_at`` — an expired owner has lost ownership
        and must not renew, even with a matching token.
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            deadline_at = self._owned_deadline_at(cur, run_id, owner_token, op="heartbeat")
            if _iso_ge(_iso(at), deadline_at):
                raise LeaseLostError(
                    f"heartbeat: run {run_id!r} lease deadline has passed; renewal fenced"
                )
            cur.execute("UPDATE active_lease SET heartbeat_at = ? WHERE id = 1", (_iso(at),))
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    def finish_run(
        self,
        *,
        run_id: str,
        owner_token: str,
        status: str,
        changed_count: int,
        finished_at: datetime,
    ) -> None:
        """Persist a terminal run status and release its lease atomically.

        Fenced by ``owner_token``: a fenced (reclaimed) owner cannot overwrite
        its ``interrupted`` run nor delete the new owner's lease. Fenced by the
        persisted deadline too: once ``finished_at`` is at/after the lease's
        ``deadline_at``, the only terminalization allowed is the truthful
        ``DEADLINE_EXCEEDED`` — an expired owner may not claim
        SUCCEEDED/NO_CHANGE/FAILED/PARTIAL. Every rejection raises
        :class:`LeaseLostError` before any write.
        """
        self._finish_run(
            run_id=run_id,
            owner_token=owner_token,
            status=status,
            changed_count=changed_count,
            finished_at=finished_at,
            capture=None,
            op="finish_run",
        )

    def finish_capture_business(
        self,
        *,
        run_id: str,
        owner_token: str,
        status: str,
        changed_count: int,
        finished_at: datetime,
        artifact_run_id: str,
        content_sha256: str,
        business_json: str,
    ) -> None:
        """Terminalize one run and bind its capture business result atomically."""
        _validate_capture_identity(artifact_run_id, content_sha256)
        if status not in _CAPTURE_BUSINESS_TERMINAL_STATUSES:
            raise ValueError(f"unsupported capture terminal status: {status!r}")
        _validate_capture_state_json(business_json, label="business_json")
        self._finish_run(
            run_id=run_id,
            owner_token=owner_token,
            status=status,
            changed_count=changed_count,
            finished_at=finished_at,
            capture=(artifact_run_id, content_sha256, business_json),
            op="finish_capture_business",
        )

    def _finish_run(
        self,
        *,
        run_id: str,
        owner_token: str,
        status: str,
        changed_count: int,
        finished_at: datetime,
        capture: tuple[str, str, str] | None,
        op: str,
    ) -> None:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"finish_run requires a terminal status, got {status!r}")
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            deadline_at = self._owned_deadline_at(cur, run_id, owner_token, op=op)
            if (
                _iso_ge(_iso(finished_at), deadline_at)
                and status != ZsxqRunStatus.DEADLINE_EXCEEDED.value
            ):
                raise LeaseLostError(
                    f"finish_run: run {run_id!r} lease deadline has passed; only "
                    f"DEADLINE_EXCEEDED may terminalize, refusing {status!r}"
                )
            run_row = cur.execute(
                "SELECT intent, trigger, attempt, started_at FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise RuntimeError("capture terminal run is missing")
            if capture is not None:
                _validate_capture_business_terminal_json(
                    capture[2],
                    run_id=run_id,
                    status=status,
                    changed_count=changed_count,
                    finished_at=_iso(finished_at),
                    intent=str(run_row["intent"]),
                    trigger=str(run_row["trigger"]),
                    attempt=int(run_row["attempt"]),
                    started_at=str(run_row["started_at"]),
                )
            cur.execute(
                "UPDATE runs SET status = ?, changed_count = ?, finished_at = ? WHERE run_id = ?",
                (status, changed_count, _iso(finished_at), run_id),
            )
            if capture is not None:
                artifact_run_id, content_sha256, business_json = capture
                cur.execute(
                    "UPDATE capture_ingests SET phase = ?, ingest_run_id = ?, "
                    "business_json = ? "
                    "WHERE artifact_run_id = ? AND content_sha256 = ? AND phase = ? "
                    "AND (ingest_run_id IS NULL OR ingest_run_id = ?)",
                    (
                        _CAPTURE_BUSINESS_TERMINAL,
                        run_id,
                        business_json,
                        artifact_run_id,
                        content_sha256,
                        _CAPTURE_CLAIMED,
                        run_id,
                    ),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("capture business-terminal CAS failed")
            cur.execute("DELETE FROM active_lease WHERE id = 1")
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    @staticmethod
    def _owned_deadline_at(cur: sqlite3.Cursor, run_id: str, owner_token: str, *, op: str) -> str:
        """Return the lease's persisted ``deadline_at`` if owned, else fence.

        Raises :class:`LeaseLostError` when no active lease matches this
        ``run_id``/``owner_token``, so callers fail visibly on lease loss.
        """
        lease = cur.execute(
            "SELECT run_id, owner_token, deadline_at FROM active_lease WHERE id = 1"
        ).fetchone()
        if lease is None or lease["run_id"] != run_id or lease["owner_token"] != owner_token:
            raise LeaseLostError(f"{op}: run {run_id!r} no longer owns the lease")
        return str(lease["deadline_at"])

    # ── reads ────────────────────────────────────────────────────

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_active_lease(self) -> dict | None:
        row = self._conn.execute("SELECT * FROM active_lease WHERE id = 1").fetchone()
        return dict(row) if row is not None else None

    def latest_terminal_run(self) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def latest_actual_observation(self, *, intent: str | None = None) -> dict | None:
        """Return the most recent health observation with its reason_code.

        Ordered by ``observed_at DESC, seq DESC`` so the latest chronologically
        observed row wins; ties break on insertion order. Schema v3 stores the
        sanitized diagnostic reason on the observation itself, so a reused or
        closed episode cannot overwrite the latest observation's reason.

        When ``intent`` is supplied, only that intent participates in the same
        ordering.  This lets the facade preserve sync truth without adding a
        second health state owner or changing the storage schema.

        This is a pure read — no write, no time substitution, no schema change.
        """
        where = "WHERE o.intent = ? " if intent is not None else ""
        params = (intent,) if intent is not None else ()
        query = (
            "SELECT o.seq, o.intent, o.surface, o.state, o.observed_at, "
            "o.episode_id, o.evidence_ref, o.reason_code "
            "FROM health_observations o "
        )
        query += where
        query += "ORDER BY o.observed_at DESC, o.seq DESC LIMIT 1"
        row = self._conn.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def health_observation_count(self) -> int:
        """Return the committed observation count for bounded live-proof auditing."""
        row = self._conn.execute("SELECT COUNT(*) AS count FROM health_observations").fetchone()
        return int(row["count"])

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return int(row["version"]) if row is not None else 0

    def close(self) -> None:
        self._conn.close()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _validate_operator_runtime_db(path: Path) -> tuple[int, int]:
    if not path.is_absolute():
        raise ValueError("runtime_db_must_be_absolute")
    try:
        if path.resolve(strict=True) != path:
            raise ValueError("runtime_db_must_be_canonical")
        parent = path.parent.stat(follow_symlinks=False)
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise ValueError("runtime_db_must_exist") from None
    except OSError as error:
        raise ValueError("runtime_db_identity_unavailable") from error
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise ValueError("runtime_db_parent_unsafe")
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("runtime_db_unsafe")
    return info.st_dev, info.st_ino


def _inspect_operator_runtime_read_only(
    path: Path,
    expected_identity: tuple[int, int],
) -> int:
    """Validate an exact stopped v3/v4 input without filesystem writes."""
    _require_no_capture_owner_sidecars(path)
    before = path.stat(follow_symlinks=False)
    uri = f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    owner = ScraperRuntimeRepository.__new__(ScraperRuntimeRepository)
    owner._conn = conn
    try:
        conn.execute("PRAGMA query_only=ON")
        cur = conn.cursor()
        try:
            version = owner._read_version(cur)
            if version == SCHEMA_VERSION:
                owner._validate_v4_exact(cur)
            elif version == 3:
                owner._validate_v3_exact(cur)
                if cur.execute("SELECT 1 FROM active_lease LIMIT 1").fetchone():
                    raise SchemaVersionError(
                        "v3 ledger has an active lease; operator migration refused"
                    )
            else:
                raise SchemaVersionError(
                    f"ledger schema version {version} is not an exact v3/v4 operator input"
                )
            quick_check = cur.execute("PRAGMA quick_check").fetchone()
            if quick_check is None or quick_check[0] != "ok":
                raise SchemaVersionError(f"v{version} ledger quick_check failed")
            if cur.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise SchemaVersionError(f"v{version} ledger foreign_key_check failed")
        finally:
            cur.close()
    finally:
        conn.close()
    _require_no_capture_owner_sidecars(path)
    if _validate_operator_runtime_db(path) != expected_identity:
        raise ValueError("runtime_db_identity_drifted")
    after = path.stat(follow_symlinks=False)
    before_snapshot = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_snapshot = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if (after.st_dev, after.st_ino) != expected_identity or after_snapshot != before_snapshot:
        raise ValueError("runtime_db_identity_drifted")
    return version


def _validate_capture_identity(artifact_run_id: str, content_sha256: str) -> None:
    if not _CAPTURE_RUN_ID_RE.fullmatch(artifact_run_id):
        raise ValueError("artifact_run_id is invalid")
    if not _SHA256_RE.fullmatch(content_sha256):
        raise ValueError("content_sha256 is invalid")


def _validate_capture_state_json(value: str, *, label: str) -> None:
    _decode_capture_state_json(value, label=label)


def _decode_capture_state_json(value: str, *, label: str) -> dict[str, object]:
    raw = value.encode("utf-8")
    if len(raw) > _MAX_CAPTURE_STATE_JSON_BYTES:
        raise ValueError(f"{label} is oversized")
    try:
        decoded = json.loads(value, parse_constant=_reject_nonfinite_json)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} is invalid")
    canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if canonical != value:
        raise ValueError(f"{label} is not canonical")
    return decoded


def _validate_capture_business_terminal_json(
    value: str,
    *,
    run_id: str,
    status: str,
    changed_count: int,
    finished_at: str,
    intent: str,
    trigger: str,
    attempt: int,
    started_at: str,
) -> None:
    decoded = decode_capture_business_projection(value, ingest_run_id=run_id)

    if (
        decoded.get("status") != status
        or decoded.get("intent") != intent
        or decoded.get("trigger") != trigger
        or decoded["changed_count"] != changed_count
        or decoded["attempt"] != attempt
        or decoded.get("started_at") != started_at
        or decoded.get("finished_at") != finished_at
    ):
        raise ValueError(
            "business_json terminal facts conflict: incomplete or mismatched complete run projection"
        )


def decode_capture_business_projection(
    value: str,
    *,
    ingest_run_id: str,
) -> dict[str, object]:
    """Decode the complete terminal run projection used by capture recovery."""
    decoded = _decode_capture_state_json(value, label="business_json")
    required = {
        "status",
        "request_id",
        "intent",
        "trigger",
        "coalesced",
        "run_id",
        "changed_count",
        "attempt",
        "started_at",
        "finished_at",
    }
    failure_reason = decoded.get("failure_reason")
    request_id = decoded.get("request_id")
    changed_count = decoded.get("changed_count")
    attempt = decoded.get("attempt")
    started_at = decoded.get("started_at")
    finished_at = decoded.get("finished_at")
    status = decoded.get("status")
    parsed_started_at = _capture_utc_timestamp(started_at)
    parsed_finished_at = _capture_utc_timestamp(finished_at)
    if (
        not (set(decoded) == required or set(decoded) == required | {"failure_reason"})
        or decoded.get("run_id") != ingest_run_id
        or status not in _CAPTURE_BUSINESS_TERMINAL_STATUSES
        or not isinstance(request_id, str)
        or len(request_id) > 256
        or not request_id.isprintable()
        or decoded.get("intent") not in {item.value for item in ZsxqRunIntent}
        or decoded.get("trigger") not in {item.value for item in ZsxqRunTrigger}
        or not isinstance(changed_count, int)
        or isinstance(changed_count, bool)
        or changed_count < 0
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or attempt < 1
        or not isinstance(started_at, str)
        or not started_at
        or not isinstance(finished_at, str)
        or not finished_at
        or parsed_started_at is None
        or parsed_finished_at is None
        or parsed_started_at > parsed_finished_at
        or decoded.get("coalesced") is not False
        or (status == ZsxqRunStatus.SUCCEEDED.value and changed_count <= 0)
        or (status != ZsxqRunStatus.SUCCEEDED.value and changed_count != 0)
        or (
            status == ZsxqRunStatus.FAILED.value
            and (
                "failure_reason" not in decoded
                or type(failure_reason) is not str
                or failure_reason not in FAILURE_REASON_ALLOWLIST
            )
        )
        or (
            status != ZsxqRunStatus.FAILED.value
            and "failure_reason" in decoded
        )
    ):
        raise ValueError(
            "business_json terminal facts conflict: incomplete or mismatched complete run projection"
        )
    return decoded


def _capture_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or parsed.isoformat() != value
    ):
        return None
    return parsed


def _validate_capture_publication_plan_json(value: str) -> None:
    decoded = _decode_capture_state_json(value, label="publication_plan_json")
    from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetPublicationPlan

    GWorkingSetPublicationPlan.from_dict(decoded)


def decode_capture_completion_projection(
    value: str,
    *,
    artifact_run_id: str,
    content_sha256: str,
    ingest_run_id: str,
    business_json: str,
    prior_g_json: str,
    publication_plan_json: str | None,
    expected_audit_json: str | None = None,
) -> dict[str, object]:
    """Decode the one durable completion shape that filesystem recovery can replay."""
    decoded = _decode_capture_state_json(value, label="completion_json")
    business = decode_capture_business_projection(
        business_json,
        ingest_run_id=ingest_run_id,
    )
    outer_fields = {
        "schema_version",
        "artifact_run_id",
        "content_sha256",
        "exit_code",
        "payload",
        "archive_disposition",
        "receipt",
    }
    if (
        set(decoded) != outer_fields
        or decoded.get("schema_version") != CAPTURE_RECOVERY_COMPLETION_SCHEMA_VERSION
        or decoded.get("artifact_run_id") != artifact_run_id
        or decoded.get("content_sha256") != content_sha256
        or not isinstance(decoded.get("payload"), dict)
        or not isinstance(decoded.get("receipt"), dict)
    ):
        raise ValueError("completion_json recovery projection is invalid")
    payload = decoded["payload"]
    receipt = decoded["receipt"]
    assert isinstance(payload, dict)
    assert isinstance(receipt, dict)
    status_value = business.get("status")
    completion_status_value = payload.get("completion_status")
    disposition_value = decoded.get("archive_disposition")
    exit_code = decoded.get("exit_code")
    if (
        not isinstance(status_value, str)
        or not isinstance(completion_status_value, str)
        or not isinstance(disposition_value, str)
        or type(exit_code) is not int
    ):
        raise ValueError("completion_json recovery projection is invalid")
    status = status_value
    completion_status = completion_status_value
    disposition = disposition_value
    expected_outcomes = {
        ZsxqRunStatus.FAILED.value: ("failed", 1, "rejected"),
        ZsxqRunStatus.DEADLINE_EXCEEDED.value: ("failed", 2, "rejected"),
    }
    outcome_is_valid = (completion_status, exit_code, disposition) in {
        ("ready", 0, "consumed"),
        ("partial", 4, "consumed"),
        ("failed", 1, "rejected"),
    }
    if status in expected_outcomes:
        outcome_is_valid = (completion_status, exit_code, disposition) == expected_outcomes[status]
    elif status not in {ZsxqRunStatus.SUCCEEDED.value, ZsxqRunStatus.NO_CHANGE.value}:
        outcome_is_valid = False
    receipt_schema = (
        _CAPTURE_CONSUMED_RECEIPT_SCHEMA_VERSION
        if disposition == "consumed"
        else _CAPTURE_REJECTED_RECEIPT_SCHEMA_VERSION
    )
    payload_required = {
        "schema_version",
        "status",
        "completion_status",
        "completion_data_gaps",
        "artifact",
        "intent",
        "trigger",
        "coalesced",
        "run_id",
        "changed_count",
        "attempt",
        "started_at",
        "finished_at",
        "capture_audit",
    }
    payload_allowed = payload_required | {"failure_reason", "g_working_set"}
    artifact = payload.get("artifact")
    artifact_file = artifact.get("file") if isinstance(artifact, dict) else None
    completion_gaps = payload.get("completion_data_gaps")
    capture_audit = payload.get("capture_audit")
    raw_g_working_set = payload.get("g_working_set") if "g_working_set" in payload else None
    business_failure = business.get("failure_reason")
    if (
        not payload_required.issubset(payload)
        or not set(payload).issubset(payload_allowed)
        or payload.get("schema_version") != _CAPTURE_INGEST_WIRE_SCHEMA_VERSION
        or payload.get("status") != status
        or payload.get("intent") != business.get("intent")
        or payload.get("trigger") != business.get("trigger")
        or payload.get("run_id") != ingest_run_id
        or payload.get("coalesced") is not False
        or payload.get("changed_count") != business.get("changed_count")
        or payload.get("attempt") != business.get("attempt")
        or payload.get("started_at") != business.get("started_at")
        or payload.get("finished_at") != business.get("finished_at")
        or ("failure_reason" in payload) != ("failure_reason" in business)
        or payload.get("failure_reason") != business_failure
        or not isinstance(completion_gaps, list)
        or len(completion_gaps) > 64
        or any(
            not isinstance(gap, str) or not gap or len(gap) > 256
            for gap in completion_gaps
        )
        or len(completion_gaps) != len(set(completion_gaps))
        or ("g_working_set" in payload and not isinstance(payload["g_working_set"], dict))
        or not isinstance(artifact, dict)
        or set(artifact) != {"run_id", "file", "content_sha256"}
        or artifact.get("run_id") != artifact_run_id
        or artifact.get("content_sha256") != content_sha256
        or not isinstance(artifact_file, str)
        or not artifact_file
        or len(artifact_file) > 255
        or not artifact_file.isprintable()
        or not isinstance(capture_audit, dict)
        or not outcome_is_valid
        or set(receipt)
        != {
            "schema_version",
            "run_id",
            "content_sha256",
            "ingested_at",
            "ingest_run_id",
            "status",
            "completion_status",
            "audit",
        }
        or receipt.get("schema_version") != receipt_schema
        or receipt.get("run_id") != artifact_run_id
        or receipt.get("content_sha256") != content_sha256
        or receipt.get("ingest_run_id") != ingest_run_id
        or receipt.get("status") != status
        or receipt.get("completion_status") != completion_status
        or not isinstance(receipt.get("audit"), dict)
    ):
        raise ValueError("completion_json recovery projection is invalid")
    if expected_audit_json is not None:
        expected_audit = _decode_capture_state_json(
            expected_audit_json,
            label="audit_json",
        )
        if receipt["audit"] != expected_audit:
            raise ValueError("completion_json recovery projection is invalid")

    from .cdp_runtime import validate_persisted_capture_completion

    assert isinstance(completion_gaps, list)
    g_receipt, g_ready = validate_persisted_capture_completion(
        business=business,
        g_working_set=raw_g_working_set,
        completion_status=completion_status,
        completion_data_gaps=tuple(completion_gaps),
        prior_g_json=prior_g_json,
        publication_plan_json=publication_plan_json,
    )

    ingested_at = receipt.get("ingested_at")
    if not isinstance(ingested_at, str):
        raise ValueError("completion_json recovery projection is invalid")
    try:
        parsed_ingested_at = datetime.fromisoformat(ingested_at)
    except ValueError as error:
        raise ValueError("completion_json recovery projection is invalid") from error
    if (
        parsed_ingested_at.tzinfo is None
        or parsed_ingested_at.utcoffset() != timedelta(0)
        or parsed_ingested_at.isoformat() != ingested_at
    ):
        raise ValueError("completion_json recovery projection is invalid")

    from .zsxq_stability import validate_capture_ingest_audit

    audit = validate_capture_ingest_audit(
        receipt["audit"],
        artifact_run_id=artifact_run_id,
        content_sha256=content_sha256,
        ingest_status=status,
        completion_status=completion_status,
        g_working_set=(g_receipt.to_dict() if g_receipt is not None else None),
        g_ready=g_ready,
    )
    chain = audit["chain"]
    denominator = audit["denominator"]
    assert isinstance(chain, dict)
    assert isinstance(denominator, dict)
    expected_capture_audit = {
        "integrity_status": audit["integrity_status"],
        "chain_ready": chain["ready"],
        "denominator_status": denominator["status"],
        "data_gaps": audit["data_gaps"],
    }
    if capture_audit != expected_capture_audit:
        raise ValueError("completion_json recovery projection is invalid")
    return decoded


def _validate_capture_g_publication_owner(
    completion: Mapping[str, object],
    *,
    publication_plan_json: str,
    knowledge_base_root: str | Path | None,
) -> None:
    """Require the first COMPLETE CAS to observe its bound G owner."""
    if knowledge_base_root is None:
        raise ValueError("capture G publication owner is missing")
    payload = completion.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("capture G publication owner evidence is invalid")
    raw_receipt = payload.get("g_working_set")
    if raw_receipt is not None and not isinstance(raw_receipt, Mapping):
        raise ValueError("capture G publication owner evidence is invalid")
    try:
        from fin_analyse.guo_teacher_research.g_working_set import (
            GWorkingSetPublicationPlan,
            GWorkingSetService,
        )

        from .cdp_runtime import GWorkingSetPublicationReceipt

        plan = GWorkingSetPublicationPlan.from_dict(json.loads(publication_plan_json))
        service = GWorkingSetService(kb_root=Path(knowledge_base_root))
        if plan.expected_owner_id != service.owner_id:
            raise ValueError("capture G publication owner drifted")
        if raw_receipt is None:
            return
        assert isinstance(raw_receipt, Mapping)
        receipt = GWorkingSetPublicationReceipt.from_dict(raw_receipt)
        if not receipt.published:
            return
        evidence = service.verify_published_plan(plan)
        if (
            receipt.status != evidence.status.value
            or receipt.generation != evidence.generation
            or receipt.evaluated_at != evidence.evaluated_at
            or receipt.source_refs != evidence.source_refs
            or receipt.data_gaps != evidence.data_gaps
            or receipt.source_coverage_sha256 != evidence.source_coverage_sha256
        ):
            raise ValueError("capture G publication owner evidence differs")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("capture G publication owner evidence is invalid") from error


def validate_archived_capture_receipt(
    runtime_db_path: str | Path,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    """Bind one archived marker to its immutable COMPLETE ledger projection."""
    return validate_archived_capture_receipts(runtime_db_path, (receipt,))[0]


def validate_archived_capture_receipts(
    runtime_db_path: str | Path,
    receipts: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Validate archived markers against one frozen, side-effect-free DB view.

    The SQLite owner is opened in read-only/query-only mode.  No recovery,
    migration, schema creation, or filesystem publication is attempted here.
    """
    path = Path(runtime_db_path)
    try:
        identities = [_archived_capture_receipt_identity(receipt) for receipt in receipts]
        if not identities:
            raise ValueError("archived receipts are missing")
        expected_owner = _archived_capture_owner_snapshot(path)
        _require_no_capture_owner_sidecars(path)
        uri = f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            schema_validator = ScraperRuntimeRepository.__new__(
                ScraperRuntimeRepository
            )
            schema_validator._conn = conn
            schema_cursor = conn.cursor()
            try:
                schema_validator._validate_v4_exact(schema_cursor)
            finally:
                schema_cursor.close()
            results = [
                _validate_archived_capture_receipt_in_snapshot(
                    conn,
                    receipt=receipt,
                    artifact_run_id=artifact_run_id,
                    content_sha256=content_sha256,
                )
                for receipt, (artifact_run_id, content_sha256) in zip(
                    receipts, identities, strict=True
                )
            ]
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()
        _require_no_capture_owner_sidecars(path)
        if _archived_capture_owner_snapshot(path) != expected_owner:
            raise ValueError("capture ledger identity changed")
        return results
    except (KeyError, RuntimeError, sqlite3.Error, TypeError, ValueError) as error:
        raise ValueError("capture receipt owner binding is invalid") from error


def _archived_capture_receipt_identity(
    receipt: Mapping[str, object],
) -> tuple[str, str]:
    outer_fields = {
        "schema_version",
        "run_id",
        "content_sha256",
        "ingested_at",
        "ingest_run_id",
        "status",
        "completion_status",
        "audit",
    }
    if (
        not isinstance(receipt, Mapping)
        or set(receipt) != outer_fields
        or receipt.get("schema_version") != _CAPTURE_CONSUMED_RECEIPT_SCHEMA_VERSION
        or not isinstance(receipt.get("run_id"), str)
        or not isinstance(receipt.get("content_sha256"), str)
    ):
        raise ValueError("invalid archived receipt boundary")
    artifact_run_id = str(receipt["run_id"])
    content_sha256 = str(receipt["content_sha256"])
    _validate_capture_identity(artifact_run_id, content_sha256)
    return artifact_run_id, content_sha256


def _validate_archived_capture_receipt_in_snapshot(
    conn: sqlite3.Connection,
    *,
    receipt: Mapping[str, object],
    artifact_run_id: str,
    content_sha256: str,
) -> dict[str, object]:
    row = conn.execute(
        "SELECT * FROM capture_ingests WHERE artifact_run_id = ?",
        (artifact_run_id,),
    ).fetchone()
    if row is None or row["content_sha256"] != content_sha256:
        raise ValueError("capture ledger identity is missing")
    record = _capture_record(conn, row)
    if record.phase != _CAPTURE_COMPLETE or record.completion_json is None:
        raise ValueError("capture ledger is not complete")
    decoded = decode_capture_completion_projection(
        record.completion_json,
        artifact_run_id=record.artifact_run_id,
        content_sha256=record.content_sha256,
        ingest_run_id=record.ingest_run_id or "",
        business_json=record.business_json or "",
        prior_g_json=record.prior_g_json,
        publication_plan_json=record.publication_plan_json,
    )
    stored_receipt = decoded.get("receipt")
    if not isinstance(stored_receipt, dict) or stored_receipt != dict(receipt):
        raise ValueError("archived receipt differs from capture ledger")
    audit = stored_receipt.get("audit")
    if not isinstance(audit, dict):
        raise ValueError("capture ledger audit is missing")
    return dict(audit)


def _archived_capture_owner_snapshot(path: Path) -> tuple[int, int, int, int, int]:
    identity = _validate_operator_runtime_db(path)
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ValueError("capture ledger identity is unavailable") from error
    if (info.st_dev, info.st_ino) != identity:
        raise ValueError("capture ledger identity changed")
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _require_no_capture_owner_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(f"{path.name}{suffix}")
        try:
            sidecar.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError("capture ledger sidecar identity is unavailable") from error
        raise ValueError("capture ledger has an active sidecar")


def _reject_nonfinite_json(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def _capture_record(conn: sqlite3.Connection, row: sqlite3.Row) -> CaptureIngestRecord:
    phase = str(row["phase"])
    if phase not in _CAPTURE_PHASES:
        raise RuntimeError("capture ingest phase is invalid")
    for label in ("prior_g_json", "business_json", "publication_plan_json", "completion_json"):
        value = row[label]
        if value is not None:
            _validate_capture_state_json(str(value), label=label)
    if row["publication_plan_json"] is not None:
        _validate_capture_publication_plan_json(str(row["publication_plan_json"]))
    if phase == _CAPTURE_COMPLETE:
        decode_capture_completion_projection(
            str(row["completion_json"]),
            artifact_run_id=str(row["artifact_run_id"]),
            content_sha256=str(row["content_sha256"]),
            ingest_run_id=str(row["ingest_run_id"]),
            business_json=str(row["business_json"]),
            prior_g_json=str(row["prior_g_json"]),
            publication_plan_json=(
                str(row["publication_plan_json"])
                if row["publication_plan_json"] is not None
                else None
            ),
        )
    if phase != _CAPTURE_CLAIMED:
        run_row = conn.execute(
            "SELECT intent, trigger, status, attempt, changed_count, started_at, finished_at "
            "FROM runs WHERE run_id = ?",
            (row["ingest_run_id"],),
        ).fetchone()
        if run_row is None or row["business_json"] is None:
            raise RuntimeError("capture terminal run projection is missing")
        _validate_capture_business_terminal_json(
            str(row["business_json"]),
            run_id=str(row["ingest_run_id"]),
            status=str(run_row["status"]),
            changed_count=int(run_row["changed_count"]),
            finished_at=str(run_row["finished_at"]),
            intent=str(run_row["intent"]),
            trigger=str(run_row["trigger"]),
            attempt=int(run_row["attempt"]),
            started_at=str(run_row["started_at"]),
        )
    return CaptureIngestRecord(
        artifact_run_id=str(row["artifact_run_id"]),
        content_sha256=str(row["content_sha256"]),
        phase=phase,
        ingest_run_id=str(row["ingest_run_id"]) if row["ingest_run_id"] is not None else None,
        prior_g_json=str(row["prior_g_json"]),
        business_json=str(row["business_json"]) if row["business_json"] is not None else None,
        publication_plan_json=(
            str(row["publication_plan_json"]) if row["publication_plan_json"] is not None else None
        ),
        completion_json=(
            str(row["completion_json"]) if row["completion_json"] is not None else None
        ),
    )


def _persistable_reason_code(reason_code: object, *, fallback: str) -> str:
    """Persist only FIN-owned formal codes; all other values fall back closed."""
    if isinstance(reason_code, str) and reason_code in _PERSISTABLE_REASON_CODES:
        return reason_code
    if fallback in _PERSISTABLE_REASON_CODES:
        return fallback
    return PageState.control_failure.value


def _sqlite_base_error_code(exc: sqlite3.Error) -> int | None:
    """Normalize SQLite primary and extended result codes to the base code."""
    code = getattr(exc, "sqlite_errorcode", None)
    return (code & 0xFF) if type(code) is int else None


def _iso_ge(a: str, b: str) -> bool:
    """Compare two ISO-8601 timestamps: True when ``a >= b``.

    Both are produced by :func:`_iso` from tz-aware datetimes, so lexical
    comparison of the parsed datetimes is exact.
    """
    return datetime.fromisoformat(a) >= datetime.fromisoformat(b)


def _iso_lt(a: str, b: str) -> bool:
    """Compare two ISO-8601 timestamps: True when ``a < b``."""
    return datetime.fromisoformat(a) < datetime.fromisoformat(b)
