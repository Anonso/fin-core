"""Transactional SQLite state for the semantic research lifecycle.

The repository owns the ``semantic-research-v1`` epoch.  It deliberately accepts
only JSON-serializable contract and input projections so the state boundary does
not depend on a particular public-command implementation.  Callers must inject a
trusted principal; principal identity is never read from a public payload here.

Continuations are 256-bit HMAC values bound to epoch, principal and chain.  Only
their SHA-256 hashes are persisted.  Every mutation that spans chain, job,
continuation, product or coordination facts runs under one ``BEGIN IMMEDIATE``
transaction.  SQLite is the sole durable state boundary for this lifecycle.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import math
import os
import re
import resource
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

from fin_analyse.guo_teacher_research.investment_memory import (
    AccountReference,
    AnalysisReference,
    InvestmentMemoryDecision,
    InvestmentMemoryEvent,
    InvestmentMemoryEventInput,
    InvestmentMemoryEventKind,
    InvestmentMemoryEventState,
    InvestmentMemoryRecall,
    InvestmentMemoryReceipt,
)
from fin_analyse.guo_teacher_research.semantic_contract import (
    ContractResolutionError,
    ResolvedResearchContract,
    SemanticInputSnapshot,
    load_input_snapshot,
    load_resolved_contract,
)

SCHEMA_NAME: Final = "semantic-research-v1"
SCHEMA_VERSION: Final = 8
DEFAULT_EPOCH: Final = SCHEMA_NAME
DAILY_WORKSPACE_KIND: Final = "daily_workspace"
CAPABILITY: Final = "guo.decision_guidance"
_SNAPSHOT_CHILD_MAX_OUTPUT_BYTES: Final = 16 * 1024
_SNAPSHOT_CHILD_TIMEOUT_SECONDS: Final = 10.0
_SNAPSHOT_MATERIALIZER_TIMEOUT_SECONDS: Final = 10.0
_SNAPSHOT_MATERIALIZER_MAX_ADDRESS_SPACE_BYTES: Final = 256 * 1024 * 1024
_SNAPSHOT_MATERIALIZER_MAX_OPEN_FILES: Final = 32
_SNAPSHOT_LIMIT_LAUNCHER: Final = Path("/usr/bin/prlimit")
_SNAPSHOT_SANDBOX: Final = Path("/usr/bin/bwrap")
_SNAPSHOT_STORE_PART_MAX_BYTES: Final = 64 * 1024 * 1024
_SNAPSHOT_DAILY_WORKSPACE_TIMING_SCHEMA: Final = "fin.semantic-daily-workspace-timing/v1"

_TERMINAL_STATES: Final = frozenset({"succeeded", "partial", "failed", "timed_out", "cancelled"})
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RUNTIME_HANDLE_KEYS: Final = frozenset(
    {"backend", "session_id", "identity_hash", "product_version"}
)


def _valid_uuid_text(value: str) -> bool:
    return _UUID_RE.fullmatch(value) is not None


_WORKER_TERMINAL_STATES: Final = frozenset({"succeeded", "partial", "failed", "timed_out"})
_PRODUCT_STATES: Final = frozenset({"succeeded", "partial"})
_INVESTMENT_MEMORY_EVENT_KINDS: Final = frozenset(
    {
        "USER_DECISION",
        "USER_REPORTED_EXECUTION",
        "OUTCOME_OBSERVATION",
        "OUTCOME_JUDGMENT",
    }
)
_INVESTMENT_MEMORY_RELATION_KINDS: Final = frozenset({"SUPERSEDES", "TOMBSTONE"})
_DAILY_WORKSPACE_FORBIDDEN_IDENTITY_FIELDS: Final = frozenset(
    {"chain_id", "continuation_token", "token_hash"}
)
_DAILY_WORKSPACE_CHECKPOINTS: Final = frozenset({"premarket", "morning", "close", "postmarket"})
_MAX_DAILY_WORKSPACE_TIMING_SAMPLES: Final = 40
_DAILY_WORKSPACE_TIMING_CANDIDATE_MULTIPLIER: Final = 8
_FORBIDDEN_PRODUCT_FIELDS: Final = frozenset(
    {
        "current_advice",
        "action",
        "trade_action",
        "position",
        "position_action",
        "position_size",
        "entry",
        "entry_price",
        "entry_timing",
        "exit",
        "exit_price",
        "exit_timing",
        "order",
        "order_instruction",
        "price",
        "target_price",
        "stop_loss_price",
        "buy_price",
        "sell_price",
        "recommended_position",
        "entry_window",
        "exit_window",
    }
)


class SemanticStateError(RuntimeError):
    """Stable, non-disclosing semantic-state failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ResearchAdmission:
    """Stable identity returned by an initial or continuation admission."""

    chain_id: str
    job_id: str
    continuation_token: str


@dataclass(frozen=True)
class AnswerWrite:
    """Persisted answer identity returned by create, append, or replay."""

    chain_id: str
    product_id: str
    continuation_token: str
    product_version: int
    status: str
    product: dict[str, object]
    artifact_hash: str
    as_of: float
    data_gaps: tuple[str, ...]
    provenance: dict[str, object]
    replayed: bool = False
    # A2: 该产物是否源自"带 continuation 但实际转入 fresh"的降级运行；
    # 随 response_projection 原子持久化，exact replay 后仍为 True。
    continuity_degraded: bool = False


@dataclass(frozen=True)
class DailyWorkspaceChain:
    """Stable daily workspace chain identity for one principal/trading day."""

    chain_id: str
    continuation_token: str
    workspace_ref: str
    trading_day_id: str
    created_at: float


@dataclass(frozen=True)
class DailyWorkspaceRead:
    """Latest immutable daily workspace version projection (hash-verified).

    Deliberately exposes no continuation token: the persisted token hash is
    not a usable continuation, and the read surface must not leak it.
    """

    chain_id: str
    workspace_ref: str
    trading_day_id: str
    product_version: int
    status: str
    product: dict[str, object]
    artifact_hash: str
    as_of: float
    created_at: float


@dataclass(frozen=True)
class DailyWorkspaceTimingSample:
    """One hash-verified actual-Agent timing observation for a checkpoint.

    The projection deliberately omits workspace/product content and opaque
    identities.  ``agent_runtime_invoked`` is always true for returned rows;
    it remains explicit so a later percentile policy cannot mistake the
    projection for deterministic fallback timing.
    """

    trading_day_id: str
    checkpoint: str
    target_at: datetime
    prepared_at: datetime
    generated_at: datetime
    degraded: bool
    agent_runtime_invoked: bool


@dataclass(frozen=True)
class DailyWorkspaceObligation:
    """One PENDING/CLAIMED/SETTLED delivery obligation for a workspace version.

    Uniquely bound to ``(workspace_ref, product_version)`` and carries the
    exact ``artifact_hash`` + ``presentation_hash`` the delivery must send.
    ``OUTCOME_UNKNOWN`` settlements never auto-resend.
    """

    workspace_ref: str
    product_version: int
    artifact_hash: str
    presentation_hash: str | None
    state: str
    claimed_at: float | None = None
    settled_at: float | None = None
    settlement: str | None = None
    created_at: float = 0.0


@dataclass(frozen=True)
class DailyWorkspaceFinalization:
    """Atomic product + obligation outcome of ``finalize_scheduled_checkpoint``."""

    read: DailyWorkspaceRead
    obligation: DailyWorkspaceObligation


@dataclass(frozen=True)
class DailyDeliveryClaim:
    """Fenced claim granting exactly one delivery attempt.

    ``claim_token`` must be presented to ``settle_delivery`` so a late ACK
    from a previous claim cannot settle a newer claim (claim fencing).
    """

    workspace_ref: str
    product_version: int
    presentation_hash: str
    claimed_at: float
    claim_token: str


@dataclass(frozen=True)
class StateCounts:
    """Read-only fact counts used by health checks and zero-write assertions."""

    chains: int
    continuations: int
    jobs: int
    chain_versions: int
    products: int
    feedback: int

    def as_tuple(self) -> tuple[int, int, int, int, int, int]:
        return (
            self.chains,
            self.continuations,
            self.jobs,
            self.chain_versions,
            self.products,
            self.feedback,
        )


@dataclass(frozen=True)
class TerminalReconciliationSnapshot:
    """Read-only retained-continuation coordination facts."""

    total_jobs: int
    active_jobs: int
    expired_jobs: int
    terminal_jobs: int
    uncoordinated_terminal_jobs: int
    reconciled_now: int = 0
    engineering_status_only: bool = True
    investment_evidence: bool = False
    writes_cognition: bool = False
    affects_confidence: bool = False
    trading_decision: bool = False
    execution_allowed: bool = False

    @property
    def data_gaps(self) -> tuple[str, ...]:
        gaps: list[str] = []
        if self.expired_jobs:
            gaps.append("semantic_expired_jobs_pending")
        if self.uncoordinated_terminal_jobs:
            gaps.append("semantic_terminal_reconciliation_pending")
        return tuple(gaps)

    def to_dict(self) -> dict[str, object]:
        return {
            "total_jobs": self.total_jobs,
            "active_jobs": self.active_jobs,
            "expired_jobs": self.expired_jobs,
            "terminal_jobs": self.terminal_jobs,
            "uncoordinated_terminal_jobs": self.uncoordinated_terminal_jobs,
            "reconciled_now": self.reconciled_now,
            "data_gaps": list(self.data_gaps),
            "engineering_status_only": self.engineering_status_only,
            "investment_evidence": self.investment_evidence,
            "writes_cognition": self.writes_cognition,
            "affects_confidence": self.affects_confidence,
            "trading_decision": self.trading_decision,
            "execution_allowed": self.execution_allowed,
        }


@dataclass(frozen=True)
class JobRecord:
    """Immutable job projection plus its mutable lifecycle coordinates."""

    job_id: str
    chain_id: str
    state: str
    contract_json: str
    input_json: str
    contract_hash: str
    input_hash: str
    request_hash: str
    deadline_at: float
    attempt: int
    fencing_token: int


@dataclass(frozen=True)
class JobLease:
    """Fenced lease handed to one worker attempt."""

    job_id: str
    chain_id: str
    worker_id: str
    attempt: int
    fencing_token: int
    lease_expires_at: float
    deadline_at: float
    contract_json: str
    input_json: str
    contract_hash: str
    input_hash: str


@dataclass(frozen=True)
class ResearchRead:
    """Stable runtime-free continuation projection."""

    status: str
    chain_id: str
    allowed_actions: tuple[str, ...]
    product_version: int | None = None
    product: dict[str, object] | None = None
    artifact_hash: str | None = None
    problem: str | None = None
    product_created_at: float | None = None
    runtime_handle: dict[str, object] | None = None


@dataclass(frozen=True)
class StoredGuidanceSnapshot:
    """Latest published product and its exact hash-verified invocation."""

    contract: ResolvedResearchContract
    input_snapshot: SemanticInputSnapshot
    product: dict[str, object]
    artifact_hash: str
    product_version: int


@dataclass(frozen=True)
class FeedbackReceipt:
    """Idempotent product-feedback identity."""

    feedback_id: str
    chain_id: str
    product_version: int


_TABLE_DDL: dict[str, str] = {
    "semantic_state_meta": """
    CREATE TABLE semantic_state_meta (
        id             INTEGER PRIMARY KEY CHECK (id = 1),
        schema_name    TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        epoch          TEXT NOT NULL
    )
    """,
    "chains": """
    CREATE TABLE chains (
        chain_id     TEXT PRIMARY KEY,
        principal_id TEXT NOT NULL,
        chain_kind   TEXT NOT NULL DEFAULT 'consultation',
        business_key TEXT,
        status       TEXT NOT NULL CHECK (status IN ('active', 'closed')),
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL
    )
    """,
    "daily_workspace_chain_per_principal": """
    CREATE UNIQUE INDEX daily_workspace_chain_per_principal
    ON chains(principal_id, chain_kind, business_key)
    WHERE chain_kind = 'daily_workspace'
    """,
    "jobs": """
    CREATE TABLE jobs (
        seq                    INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id                 TEXT NOT NULL UNIQUE,
        chain_id               TEXT NOT NULL REFERENCES chains(chain_id),
        principal_id           TEXT NOT NULL,
        state                  TEXT NOT NULL
                                   CHECK (state IN (
                                       'queued', 'running', 'succeeded', 'partial',
                                       'failed', 'timed_out', 'cancelled'
                                   )),
        contract_json          TEXT NOT NULL,
        input_json             TEXT NOT NULL,
        contract_hash          TEXT NOT NULL,
        input_hash             TEXT NOT NULL,
        request_hash           TEXT NOT NULL,
        deadline_at            REAL NOT NULL,
        attempt                INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
        fencing_token          INTEGER NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
        lease_owner            TEXT,
        lease_expires_at       REAL,
        heartbeat_at           REAL,
        product_json           TEXT,
        artifact_hash          TEXT,
        coordinated_version_no INTEGER,
        created_at             REAL NOT NULL,
        updated_at             REAL NOT NULL
    )
    """,
    "continuations": """
    CREATE TABLE continuations (
        token_hash          TEXT PRIMARY KEY,
        epoch               TEXT NOT NULL,
        principal_id        TEXT NOT NULL,
        chain_id            TEXT NOT NULL UNIQUE REFERENCES chains(chain_id),
        active_job_id       TEXT REFERENCES jobs(job_id),
        runtime_backend     TEXT,
        session_id          TEXT,
        identity_hash       TEXT,
        product_version     INTEGER,
        active_turn_id      TEXT,
        turn_lease_expires_at REAL,
        turn_fencing_token  INTEGER NOT NULL DEFAULT 0 CHECK (turn_fencing_token >= 0),
        created_at          REAL NOT NULL,
        updated_at          REAL NOT NULL
    )
    """,
    "runtime_session_gc": """
    CREATE TABLE runtime_session_gc (
        session_id      TEXT PRIMARY KEY,
        chain_id        TEXT NOT NULL REFERENCES chains(chain_id),
        product_version INTEGER NOT NULL,
        captured_at     REAL NOT NULL,
        bytes           INTEGER NOT NULL CHECK (bytes >= 0),
        state           TEXT NOT NULL CHECK (state IN ('active', 'retired')),
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL
    )
    """,
    "conversation_routes": """
    CREATE TABLE conversation_routes (
        route_key            TEXT PRIMARY KEY,
        active_generation    TEXT NOT NULL,
        active_chain_id      TEXT NOT NULL,
        active_revision      INTEGER NOT NULL CHECK (active_revision >= 1),
        seen_generations_json TEXT NOT NULL,
        created_at           REAL NOT NULL,
        updated_at           REAL NOT NULL
    )
    """,
    "daily_workspace_run_ledger": """
    CREATE TABLE daily_workspace_run_ledger (
        run_id           TEXT PRIMARY KEY,
        trading_day_id   TEXT NOT NULL,
        checkpoint       TEXT NOT NULL
                         CHECK (checkpoint IN ('premarket', 'morning', 'close', 'postmarket')),
        trigger          TEXT NOT NULL
                         CHECK (trigger IN ('manual', 'schedule', 'recovery')),
        started_at       REAL NOT NULL,
        completed_at     REAL NOT NULL,
        stage_statuses   TEXT NOT NULL,
        collect_identity TEXT NOT NULL,
        created_at       REAL NOT NULL,
        CHECK (started_at <= completed_at)
    )
    """,
    "daily_workspace_obligations": """
    CREATE TABLE daily_workspace_obligations (
        workspace_ref     TEXT NOT NULL,
        product_version   INTEGER NOT NULL,
        artifact_hash     TEXT NOT NULL,
        presentation_hash TEXT,
        state             TEXT NOT NULL
                              CHECK (state IN ('PENDING', 'CLAIMED', 'SETTLED')),
        claim_token       TEXT,
        claimed_at        REAL,
        settled_at        REAL,
        settlement        TEXT CHECK (settlement IN (
                               'POSITIVE_ACK', 'EXPLICIT_NOT_SENT', 'OUTCOME_UNKNOWN'
                           )),
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL,
        PRIMARY KEY (workspace_ref, product_version),
        CHECK (
            (state = 'PENDING' AND claim_token IS NULL AND claimed_at IS NULL
                AND settlement IS NULL AND settled_at IS NULL
                AND presentation_hash IS NULL)
            OR
            (state = 'CLAIMED' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL
                AND settlement IS NULL AND settled_at IS NULL
                AND presentation_hash IS NOT NULL)
            OR
            (state = 'SETTLED' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL
                AND settlement IS NOT NULL
                AND settlement IN ('POSITIVE_ACK', 'OUTCOME_UNKNOWN')
                AND settled_at IS NOT NULL
                AND presentation_hash IS NOT NULL)
        )
    )
    """,
    "idempotency": """
    CREATE TABLE idempotency (
        principal_id TEXT NOT NULL,
        capability   TEXT NOT NULL,
        key_hash     TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        chain_id     TEXT NOT NULL REFERENCES chains(chain_id),
        job_id       TEXT REFERENCES jobs(job_id),
        product_id   TEXT REFERENCES products(product_id),
        created_at   REAL NOT NULL,
        CHECK (
            (capability = 'daily_workspace' AND job_id IS NULL AND product_id IS NULL)
            OR (job_id IS NULL) != (product_id IS NULL)
        ),
        PRIMARY KEY (principal_id, capability, key_hash)
    )
    """,
    "products": """
    CREATE TABLE products (
        product_id      TEXT PRIMARY KEY,
        chain_id        TEXT NOT NULL REFERENCES chains(chain_id),
        job_id          TEXT UNIQUE REFERENCES jobs(job_id),
        product_version INTEGER NOT NULL,
        status          TEXT NOT NULL CHECK (status IN ('completed', 'partial')),
        product_json    TEXT NOT NULL,
        artifact_hash   TEXT NOT NULL,
        created_at      REAL NOT NULL,
        UNIQUE (chain_id, product_version)
    )
    """,
    "chain_versions": """
    CREATE TABLE chain_versions (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        chain_id     TEXT NOT NULL REFERENCES chains(chain_id),
        version_no   INTEGER NOT NULL,
        kind         TEXT NOT NULL
                         CHECK (kind IN (
                             'answer', 'research_pending', 'research_terminal',
                             'feedback', 'closed', 'daily_workspace'
                         )),
        job_id       TEXT REFERENCES jobs(job_id),
        product_id   TEXT REFERENCES products(product_id),
        contract_json TEXT,
        input_json    TEXT,
        contract_hash TEXT,
        input_hash    TEXT,
        payload_json TEXT NOT NULL,
        created_at   REAL NOT NULL,
        UNIQUE (chain_id, version_no)
    )
    """,
    "one_terminal_version_per_job": """
    CREATE UNIQUE INDEX one_terminal_version_per_job
    ON chain_versions(job_id)
    WHERE kind = 'research_terminal'
    """,
    "feedback": """
    CREATE TABLE feedback (
        feedback_id      TEXT PRIMARY KEY,
        chain_id         TEXT NOT NULL REFERENCES chains(chain_id),
        product_version  INTEGER NOT NULL,
        feedback_key_hash TEXT NOT NULL,
        disposition      TEXT NOT NULL,
        note             TEXT NOT NULL,
        created_at       REAL NOT NULL,
        UNIQUE (chain_id, feedback_key_hash),
        FOREIGN KEY (chain_id, product_version)
            REFERENCES products(chain_id, product_version)
    )
    """,
    "investment_memory_events": """
    CREATE TABLE investment_memory_events (
        seq                      INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id                 TEXT NOT NULL UNIQUE,
        principal_id             TEXT NOT NULL,
        event_key_hash           TEXT NOT NULL,
        kind                     TEXT NOT NULL CHECK (kind IN (
                                     'USER_DECISION', 'USER_REPORTED_EXECUTION',
                                     'OUTCOME_OBSERVATION', 'OUTCOME_JUDGMENT',
                                     'SUPERSEDES', 'TOMBSTONE'
                                 )),
        target_event_id          TEXT REFERENCES investment_memory_events(event_id),
        analysis_chain_id        TEXT,
        analysis_product_version INTEGER,
        account_snapshot_ref     TEXT,
        account_revision         TEXT,
        account_as_of            REAL,
        decision                 TEXT CHECK (decision IN (
                                     'ACCEPT', 'REJECT', 'WAIT', 'CHANGE_PLAN'
                                 )),
        payload_json             TEXT NOT NULL,
        payload_hash             TEXT NOT NULL,
        created_at               REAL NOT NULL,
        purged_at                REAL,
        UNIQUE (principal_id, event_key_hash),
        CHECK (
            (analysis_chain_id IS NULL AND analysis_product_version IS NULL)
            OR (analysis_chain_id IS NOT NULL AND analysis_product_version IS NOT NULL)
        ),
        CHECK (
            (account_snapshot_ref IS NULL AND account_revision IS NULL AND account_as_of IS NULL)
            OR (account_snapshot_ref IS NOT NULL AND account_revision IS NOT NULL AND account_as_of IS NOT NULL)
        ),
        CHECK (
            (kind = 'USER_DECISION' AND decision IS NOT NULL)
            OR (kind != 'USER_DECISION' AND decision IS NULL)
        ),
        FOREIGN KEY (analysis_chain_id, analysis_product_version)
            REFERENCES products(chain_id, product_version)
    )
    """,
    "investment_memory_recall_by_principal": """
    CREATE INDEX investment_memory_recall_by_principal
    ON investment_memory_events(principal_id, kind, seq DESC)
    """,
    "immutable_investment_memory_event": """
    CREATE TRIGGER immutable_investment_memory_event
    BEFORE UPDATE ON investment_memory_events
    WHEN
        NEW.seq != OLD.seq OR
        NEW.event_id != OLD.event_id OR
        NEW.principal_id != OLD.principal_id OR
        NEW.event_key_hash != OLD.event_key_hash OR
        NEW.kind != OLD.kind OR
        NEW.target_event_id IS NOT OLD.target_event_id OR
        NEW.analysis_chain_id IS NOT OLD.analysis_chain_id OR
        NEW.analysis_product_version IS NOT OLD.analysis_product_version OR
        NEW.account_snapshot_ref IS NOT OLD.account_snapshot_ref OR
        NEW.account_revision IS NOT OLD.account_revision OR
        NEW.account_as_of IS NOT OLD.account_as_of OR
        NEW.decision IS NOT OLD.decision OR
        NEW.payload_hash != OLD.payload_hash OR
        NEW.created_at != OLD.created_at OR
        OLD.purged_at IS NOT NULL OR
        NEW.purged_at IS NULL OR
        NEW.purged_at < OLD.created_at OR
        json_extract(NEW.payload_json, '$.schema_version') != 'fin.investment-memory-event/v1' OR
        json_extract(NEW.payload_json, '$.redacted') != 1
    BEGIN
        SELECT RAISE(ABORT, 'immutable investment memory event');
    END
    """,
    "append_only_investment_memory_event": """
    CREATE TRIGGER append_only_investment_memory_event
    BEFORE DELETE ON investment_memory_events
    BEGIN
        SELECT RAISE(ABORT, 'append-only investment memory event');
    END
    """,
    "immutable_job_contract": """
    CREATE TRIGGER immutable_job_contract
    BEFORE UPDATE OF
        job_id, chain_id, principal_id, contract_json, input_json,
        contract_hash, input_hash, request_hash, deadline_at
    ON jobs
    WHEN
        NEW.job_id != OLD.job_id OR
        NEW.chain_id != OLD.chain_id OR
        NEW.principal_id != OLD.principal_id OR
        NEW.contract_json != OLD.contract_json OR
        NEW.input_json != OLD.input_json OR
        NEW.contract_hash != OLD.contract_hash OR
        NEW.input_hash != OLD.input_hash OR
        NEW.request_hash != OLD.request_hash OR
        NEW.deadline_at != OLD.deadline_at
    BEGIN
        SELECT RAISE(ABORT, 'immutable research job contract');
    END
    """,
    "claimable_jobs": "CREATE INDEX claimable_jobs ON jobs(state, deadline_at, lease_expires_at, seq)",
    "jobs_by_chain": "CREATE INDEX jobs_by_chain ON jobs(chain_id, seq)",
}

_SCHEMA_DDL: tuple[str, ...] = tuple(_TABLE_DDL.values())


def _run_ledger_stage_closed_set_ok(stage_statuses: str) -> bool:
    """True when the stage closed set is complete and non-degraded.

    Success = COLLECT_READY/PARTIAL + PREPARED + DELIVERED/ALREADY_DELIVERED
    exactly once each, no degraded flag, no failure status.
    """

    try:
        stages = json.loads(stage_statuses)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise SemanticStateError("semantic_state_corrupt") from None
    if not isinstance(stages, list) or not stages:
        raise SemanticStateError("semantic_state_corrupt")
    counts: dict[str, int] = {}
    for stage in stages:
        if not isinstance(stage, dict):
            raise SemanticStateError("semantic_state_corrupt")
        if stage.get("degraded") is True:
            return False
        status = str(stage.get("status", ""))
        stage_name = str(stage.get("stage", ""))
        if status in {
            "COLLECT_FAILED",
            "PREPARE_FAILED",
            "DELIVER_FAILED",
            "NOT_TRADING_DAY",
            "NOT_DUE",
            "WINDOW_MISSED",
        }:
            return False
        if (stage_name, status) not in {
            ("collect", "COLLECT_READY"),
            ("collect", "COLLECT_PARTIAL"),
            ("prepare", "PREPARED"),
            ("deliver", "DELIVERED"),
            ("deliver", "ALREADY_DELIVERED"),
        }:
            return False
        counts[stage_name] = counts.get(stage_name, 0) + 1
    # S3: exactly-once——每 stage 恰好一次（重复 collect 不得判成功）。
    return counts == {"collect": 1, "prepare": 1, "deliver": 1}


_SNAPSHOT_SCHEMA_MANIFEST_VERSION: Final = "fin.semantic-snapshot-schema-manifest/v1"
# ``schema_version`` pragma deliberately excluded: it is SQLite's internal
# schema cookie, which advances on every DDL — a legitimately migrated v1
# owner can never match a fresh v2 database byte-for-byte on that value.
_SNAPSHOT_SCHEMA_PRAGMAS: Final = (
    "application_id",
    "user_version",
)


def _semantic_snapshot_schema_manifest(connection: sqlite3.Connection) -> dict[str, object]:
    """Project the owner DDL into the exact read-only snapshot schema contract."""
    table_name_query = """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT GLOB 'sqlite_*'
            ORDER BY name
        """
    table_name_rows = connection.execute(table_name_query)
    table_names = [str(row[0]) for row in table_name_rows]
    tables: list[dict[str, object]] = []
    for table_name in table_names:
        table_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if table_sql_row is None or not isinstance(table_sql_row[0], str):
            raise ValueError("invalid semantic state table definition")
        columns = [
            {
                "cid": int(row[0]),
                "name": str(row[1]),
                "type": str(row[2]),
                "notnull": int(row[3]),
                "default": row[4],
                "pk": int(row[5]),
                "hidden": int(row[6]),
            }
            for row in connection.execute(
                """
                SELECT cid, name, type, "notnull", dflt_value, pk, hidden
                FROM pragma_table_xinfo(?)
                ORDER BY cid
                """,
                (table_name,),
            )
        ]
        indexes: list[dict[str, object]] = []
        for index_row in connection.execute(
            """
            SELECT name, "unique", origin, partial
            FROM pragma_index_list(?)
            ORDER BY name
            """,
            (table_name,),
        ):
            index_name = str(index_row[0])
            index_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            indexes.append(
                {
                    "name": index_name,
                    "unique": int(index_row[1]),
                    "origin": str(index_row[2]),
                    "partial": int(index_row[3]),
                    "sql": None if index_sql_row is None else index_sql_row[0],
                    "columns": [
                        {
                            "seqno": int(column[0]),
                            "cid": int(column[1]),
                            "name": column[2],
                            "desc": int(column[3]),
                            "collation": str(column[4]),
                            "key": int(column[5]),
                        }
                        for column in connection.execute(
                            """
                            SELECT seqno, cid, name, "desc", coll, key
                            FROM pragma_index_xinfo(?)
                            ORDER BY seqno
                            """,
                            (index_name,),
                        )
                    ],
                }
            )
        foreign_keys = [
            {
                "id": int(row[0]),
                "seq": int(row[1]),
                "table": str(row[2]),
                "from": str(row[3]),
                "to": str(row[4]),
                "on_update": str(row[5]),
                "on_delete": str(row[6]),
                "match": str(row[7]),
            }
            for row in connection.execute(
                """
                SELECT id, seq, "table", "from", "to", on_update, on_delete, match
                FROM pragma_foreign_key_list(?)
                ORDER BY id, seq
                """,
                (table_name,),
            )
        ]
        tables.append(
            {
                "name": table_name,
                "sql": table_sql_row[0],
                "columns": columns,
                "indexes": indexes,
                "foreign_keys": foreign_keys,
            }
        )
    meta_query = """
            SELECT id, schema_name, schema_version, epoch
            FROM semantic_state_meta
            ORDER BY id
        """
    meta_rows = connection.execute(meta_query)
    meta = [
        {
            "id": row[0],
            "schema_name": row[1],
            "schema_version": row[2],
            "epoch": row[3],
        }
        for row in meta_rows
    ]
    schema_object_query = """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE type IN ('view', 'trigger') AND name NOT GLOB 'sqlite_*'
            ORDER BY type, name
        """
    schema_object_rows = connection.execute(schema_object_query)
    schema_objects = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": row[3],
        }
        for row in schema_object_rows
    ]
    return {
        "schema_version": _SNAPSHOT_SCHEMA_MANIFEST_VERSION,
        "pragmas": {
            name: _semantic_snapshot_pragma_int(connection, name)
            for name in _SNAPSHOT_SCHEMA_PRAGMAS
        },
        "meta": meta,
        "tables": tables,
        "schema_objects": schema_objects,
    }


def _semantic_snapshot_pragma_int(connection: sqlite3.Connection, name: str) -> int:
    if name not in _SNAPSHOT_SCHEMA_PRAGMAS:
        raise ValueError("unsupported semantic snapshot schema pragma")
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or type(row[0]) is not int:
        raise ValueError("invalid semantic snapshot schema pragma")
    return int(row[0])


def _canonical_semantic_snapshot_schema_digest(epoch: str) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for ddl in _SCHEMA_DDL:
            connection.execute(ddl)
        connection.execute(
            """
            INSERT INTO semantic_state_meta(id, schema_name, schema_version, epoch)
            VALUES (1, ?, ?, ?)
            """,
            (SCHEMA_NAME, SCHEMA_VERSION, epoch),
        )
        manifest = _semantic_snapshot_schema_manifest(connection)
    finally:
        connection.close()
    encoded = json.dumps(
        manifest,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ResearchStateRepository:
    """Canonical SQLite owner for one semantic-research epoch."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        token_secret: bytes,
        epoch: str = DEFAULT_EPOCH,
    ) -> None:
        if len(token_secret) < 32:
            raise ValueError("token_secret must contain at least 256 bits")
        if not epoch.strip():
            raise ValueError("epoch must be non-empty")
        self._db_path = str(db_path)
        self._token_secret = bytes(token_secret)
        self._epoch = epoch
        self._initialize()

    # -- setup and transaction boundary ---------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            has_meta = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_state_meta'"
            ).fetchone()
            if has_meta is None:
                foreign_object = connection.execute("""
                    SELECT name FROM sqlite_master
                    WHERE name NOT GLOB 'sqlite_*'
                    LIMIT 1
                    """).fetchone()
                if foreign_object is not None:
                    raise SemanticStateError("semantic_state_schema_invalid")
                for ddl in _SCHEMA_DDL:
                    connection.execute(ddl)
                connection.execute(
                    """
                    INSERT INTO semantic_state_meta(id, schema_name, schema_version, epoch)
                    VALUES (1, ?, ?, ?)
                    """,
                    (SCHEMA_NAME, SCHEMA_VERSION, self._epoch),
                )
            else:
                rows = connection.execute(
                    "SELECT schema_name, schema_version, epoch FROM semantic_state_meta"
                ).fetchall()
                if len(rows) != 1:
                    raise SemanticStateError("semantic_state_schema_invalid")
                row = rows[0]
                if str(row["schema_name"]) != SCHEMA_NAME:
                    raise SemanticStateError("semantic_state_schema_unsupported")
                stored_version = int(row["schema_version"])
                if stored_version < SCHEMA_VERSION:
                    # PRAGMA foreign_keys 在事务内是 no-op；迁移必须先提交当前
                    # 事务，在独立事务中关闭 FK 重建表，再重新开始只读校验事务。
                    connection.execute("COMMIT")
                    connection.execute("PRAGMA foreign_keys=OFF")
                    connection.execute("BEGIN IMMEDIATE")
                    if stored_version == 1:
                        self._migrate_v1_to_v2(connection)
                        self._migrate_v2_to_v3(connection)
                        self._migrate_v3_to_v4(connection)
                        self._migrate_v4_to_v5(connection)
                        self._migrate_v5_to_v6(connection)
                        self._migrate_v6_to_v7(connection)
                        self._migrate_v7_to_v8(connection)
                    elif stored_version == 2:
                        self._migrate_v2_to_v3(connection)
                        self._migrate_v3_to_v4(connection)
                        self._migrate_v4_to_v5(connection)
                        self._migrate_v5_to_v6(connection)
                        self._migrate_v6_to_v7(connection)
                        self._migrate_v7_to_v8(connection)
                    elif stored_version == 3:
                        self._migrate_v3_to_v4(connection)
                        self._migrate_v4_to_v5(connection)
                        self._migrate_v5_to_v6(connection)
                        self._migrate_v6_to_v7(connection)
                        self._migrate_v7_to_v8(connection)
                    elif stored_version == 4:
                        self._migrate_v4_to_v5(connection)
                        self._migrate_v5_to_v6(connection)
                        self._migrate_v6_to_v7(connection)
                        self._migrate_v7_to_v8(connection)
                    elif stored_version == 5:
                        self._migrate_v5_to_v6(connection)
                        self._migrate_v6_to_v7(connection)
                        self._migrate_v7_to_v8(connection)
                    elif stored_version == 6:
                        self._migrate_v6_to_v7(connection)
                        self._migrate_v7_to_v8(connection)
                    elif stored_version == 7:
                        self._migrate_v7_to_v8(connection)
                    else:
                        # 未识别的中间/未来 schema 不是迁移对象。
                        raise SemanticStateError("semantic_state_schema_unsupported")
                    connection.execute("COMMIT")
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("BEGIN IMMEDIATE")
                    migrated = connection.execute(
                        "SELECT schema_version FROM semantic_state_meta WHERE id = 1"
                    ).fetchone()
                    if migrated is None or int(migrated[0]) != SCHEMA_VERSION:
                        raise SemanticStateError("semantic_state_schema_unsupported")
                elif stored_version != SCHEMA_VERSION:
                    raise SemanticStateError("semantic_state_schema_unsupported")
                if str(row["epoch"]) != self._epoch:
                    raise SemanticStateError("semantic_state_epoch_unsupported")
                expected_tables = {
                    "conversation_routes",
                    "chains",
                    "jobs",
                    "continuations",
                    "idempotency",
                    "products",
                    "chain_versions",
                    "feedback",
                    "investment_memory_events",
                    "runtime_session_gc",
                    "daily_workspace_obligations",
                    "daily_workspace_run_ledger",
                }
                actual_tables = {
                    str(item["name"])
                    for item in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if not expected_tables.issubset(actual_tables):
                    raise SemanticStateError("semantic_state_schema_invalid")
                expected_triggers = {
                    "append_only_investment_memory_event",
                    "immutable_investment_memory_event",
                }
                actual_triggers = {
                    str(item["name"])
                    for item in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    ).fetchall()
                }
                if not expected_triggers.issubset(actual_triggers):
                    raise SemanticStateError("semantic_state_schema_invalid")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        """Additive v1→v2 migration: daily workspace chain identity.

        Existing chains keep their tokens and data; the new columns default to
        the consultation semantics so old rows are untouched.  Rebuilt tables
        reuse the canonical ``_TABLE_DDL`` text so the migrated database
        matches the exact snapshot schema manifest of a fresh v2 database.
        """

        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self._rebuild_v2_table(
                connection,
                "chains",
                old_columns=(
                    "chain_id",
                    "principal_id",
                    "status",
                    "created_at",
                    "updated_at",
                ),
                extra_values={"chain_kind": "'consultation'", "business_key": "NULL"},
            )
            self._rebuild_v2_table(
                connection,
                "chain_versions",
                old_columns=(
                    "seq",
                    "chain_id",
                    "version_no",
                    "kind",
                    "job_id",
                    "product_id",
                    "contract_json",
                    "input_json",
                    "contract_hash",
                    "input_hash",
                    "payload_json",
                    "created_at",
                ),
            )
            self._rebuild_v2_table(
                connection,
                "idempotency",
                old_columns=(
                    "principal_id",
                    "capability",
                    "key_hash",
                    "request_hash",
                    "chain_id",
                    "job_id",
                    "product_id",
                    "created_at",
                ),
            )
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        # 重建表会删除随表销毁的独立索引；从 canonical DDL 文本恢复，
        # 保证 sqlite_master.sql 与全新 v2 库逐字节一致。
        connection.execute(_TABLE_DDL["one_terminal_version_per_job"])
        connection.execute(_TABLE_DDL["daily_workspace_chain_per_principal"])
        connection.execute(
            "UPDATE semantic_state_meta SET schema_version = ? WHERE id = 1",
            (2,),
        )

    def _migrate_v2_to_v3(self, connection: sqlite3.Connection) -> None:
        """Additive v2→v3 migration: runtime continuation handle + turn lease.

        Existing continuations keep their tokens and data; the new columns
        (runtime_backend/session_id/identity_hash/product_version + turn lease)
        default NULL/0 so old rows are untouched.  Rebuilt tables reuse the
        canonical ``_TABLE_DDL`` text so the migrated database matches the
        exact snapshot schema manifest of a fresh v3 database.  Also creates
        the new ``runtime_session_gc`` table.
        """

        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self._rebuild_v2_table(
                connection,
                "continuations",
                old_columns=(
                    "token_hash",
                    "epoch",
                    "principal_id",
                    "chain_id",
                    "active_job_id",
                    "created_at",
                    "updated_at",
                ),
            )
            connection.execute(_TABLE_DDL["runtime_session_gc"])
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "UPDATE semantic_state_meta SET schema_version = ? WHERE id = 1",
            (3,),
        )

    def _migrate_v3_to_v4(self, connection: sqlite3.Connection) -> None:
        """v3→v4 migration: daily workspace delivery obligation.

        v3 never has an obligation table (it is introduced by v4).  The
        migration therefore REBUILDS the table from canonical DDL every time
        (like the v2 table rebuilds): a pre-existing same-named table from a
        drifted/malformed database is dropped and replaced with the exact
        canonical schema — no string-substring validation that can fail open
        (codex P1: substring checks never prove real CHECK/NOT NULL/PK).
        """

        # If the table somehow already exists WITH rows, do not silently drop
        # them (an interrupted v4 that later re-migrates must never lose
        # obligation data).  An empty or absent table is safely rebuilt.
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_workspace_obligations'"
        ).fetchone()
        if table_exists is not None:
            existing_count = connection.execute(
                "SELECT COUNT(*) FROM daily_workspace_obligations"
            ).fetchone()[0]
            if existing_count:
                raise SemanticStateError("semantic_state_schema_invalid")
        connection.execute("DROP TABLE IF EXISTS daily_workspace_obligations")
        connection.execute(_TABLE_DDL["daily_workspace_obligations"])
        connection.execute(
            "UPDATE semantic_state_meta SET schema_version = ? WHERE id = 1",
            (4,),
        )

    def _migrate_v4_to_v5(self, connection: sqlite3.Connection) -> None:
        """v4→v5 migration (A5L-2): additive conversation_routes table.

        Only stored schemas ≤ v4 enter this step; later released schemas
        proceed through their own sequential migration step.
        """
        connection.execute("DROP TABLE IF EXISTS conversation_routes")
        connection.execute(_TABLE_DDL["conversation_routes"])
        connection.execute(
            "UPDATE semantic_state_meta SET schema_version = ? WHERE id = 1",
            (5,),
        )

    def _migrate_v5_to_v6(self, connection: sqlite3.Connection) -> None:
        """v5→v6 migration (B2): additive daily_workspace_run_ledger table.

        Every stored schema ≤ v5 migrates through this step; older branches
        (v1–v4) reach v6 by running the full sequential chain.  The table is
        rebuilt from canonical DDL every time (like the v4 obligation table):
        an empty or absent table is safely replaced; a table with rows from a
        drifted/malformed database raises instead of silently dropping data.
        """
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_workspace_run_ledger'"
        ).fetchone()
        if table_exists is not None:
            existing_count = connection.execute(
                "SELECT COUNT(*) FROM daily_workspace_run_ledger"
            ).fetchone()[0]
            if existing_count:
                raise SemanticStateError("semantic_state_schema_invalid")
        connection.execute("DROP TABLE IF EXISTS daily_workspace_run_ledger")
        connection.execute(_TABLE_DDL["daily_workspace_run_ledger"])
        connection.execute(
            "UPDATE semantic_state_meta SET schema_version = ? WHERE id = 1",
            (6,),
        )

    def _migrate_v6_to_v7(self, connection: sqlite3.Connection) -> None:
        """v6→v7 (B3): 归一 obligations 表为 canonical DDL，语义保留行。

        401e1dd5 把 presentation_hash 从 NOT NULL 改为 nullable（PENDING→NULL、
        claim-time 绑定语义）但未迁移既有库——真实库仍 NOT NULL，degraded
        finalize（INSERT NULL）会抛 sqlite3.IntegrityError。本迁移把旧形状表
        重建为 canonical DDL，并做语义归一：
        - PENDING 行：废弃的预计算 presentation_hash 归一为 NULL；
        - CLAIMED/SETTLED 行：字段原样保留（claim-time 绑定 hash）；
        - SETTLED + EXPLICIT_NOT_SENT（新 CHECK 下的业务不可能态）：
          归一回 PENDING（清空 claim/settlement 字段，可重试）。
        表行数非零也不静默丢弃：staging 重建保留所有行。
        """
        connection.execute("PRAGMA foreign_keys=OFF")
        staging = "daily_workspace_obligations_v7"
        try:
            connection.execute(f"DROP TABLE IF EXISTS {staging}")
            connection.execute(
                _TABLE_DDL["daily_workspace_obligations"].replace(
                    "daily_workspace_obligations", staging, 1
                )
            )
            connection.execute(
                f"""
                INSERT INTO {staging}(
                    workspace_ref, product_version, artifact_hash, presentation_hash,
                    state, claim_token, claimed_at, settled_at, settlement,
                    created_at, updated_at
                )
                SELECT workspace_ref, product_version, artifact_hash,
                       -- 归一状态为 PENDING 的行（旧 PENDING 或 SETTLED+EXPLICIT_NOT_SENT）
                       -- hash 一律归一 NULL（新 CHECK 语义）
                       CASE
                           WHEN state = 'PENDING'
                                OR (state = 'SETTLED' AND settlement = 'EXPLICIT_NOT_SENT')
                           THEN NULL
                           ELSE presentation_hash
                       END,
                       CASE
                           WHEN state = 'SETTLED' AND settlement = 'EXPLICIT_NOT_SENT'
                           THEN 'PENDING'
                           ELSE state
                       END,
                       CASE
                           WHEN state = 'SETTLED' AND settlement = 'EXPLICIT_NOT_SENT'
                           THEN NULL
                           ELSE claim_token
                       END,
                       CASE
                           WHEN state = 'SETTLED' AND settlement = 'EXPLICIT_NOT_SENT'
                           THEN NULL
                           ELSE claimed_at
                       END,
                       CASE
                           WHEN state = 'SETTLED' AND settlement = 'EXPLICIT_NOT_SENT'
                           THEN NULL
                           ELSE settled_at
                       END,
                       CASE
                           WHEN state = 'SETTLED' AND settlement = 'EXPLICIT_NOT_SENT'
                           THEN NULL
                           ELSE settlement
                       END,
                       created_at, updated_at
                FROM daily_workspace_obligations
                """
            )
            connection.execute("DROP TABLE daily_workspace_obligations")
            connection.execute(_TABLE_DDL["daily_workspace_obligations"])
            connection.execute(
                f"""
                INSERT INTO daily_workspace_obligations(
                    workspace_ref, product_version, artifact_hash, presentation_hash,
                    state, claim_token, claimed_at, settled_at, settlement,
                    created_at, updated_at
                )
                SELECT workspace_ref, product_version, artifact_hash, presentation_hash,
                       state, claim_token, claimed_at, settled_at, settlement,
                       created_at, updated_at
                FROM {staging}
                """
            )
            connection.execute(f"DROP TABLE {staging}")
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "UPDATE semantic_state_meta SET schema_version = ? WHERE id = 1",
            (7,),
        )

    def _migrate_v7_to_v8(self, connection: sqlite3.Connection) -> None:
        """v7→v8: one append-only typed investment-memory event journal.

        This is additive.  The journal stores user-stated structured facts and
        direct references only; it deliberately does not copy product bodies,
        account facts, or Hermes/Codex transcripts into a second owner.
        """

        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='investment_memory_events'"
        ).fetchone()
        if table_exists is not None:
            existing_count = connection.execute(
                "SELECT COUNT(*) FROM investment_memory_events"
            ).fetchone()[0]
            if existing_count:
                raise SemanticStateError("semantic_state_schema_invalid")
        connection.execute("DROP TABLE IF EXISTS investment_memory_events")
        connection.execute(_TABLE_DDL["investment_memory_events"])
        connection.execute(_TABLE_DDL["investment_memory_recall_by_principal"])
        connection.execute(_TABLE_DDL["immutable_investment_memory_event"])
        connection.execute(_TABLE_DDL["append_only_investment_memory_event"])
        connection.execute(
            "UPDATE semantic_state_meta SET schema_version = ? WHERE id = 1",
            (8,),
        )

    def _rebuild_v2_table(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        *,
        old_columns: tuple[str, ...],
        extra_values: dict[str, str] | None = None,
    ) -> None:
        """Rebuild one table from the canonical v2 DDL, preserving all rows.

        SQLite cannot alter CHECK constraints or column order in place; the
        table is recreated with byte-identical ``_TABLE_DDL`` text so the
        migrated database matches the exact snapshot schema manifest.  The
        final table is created under its canonical name (``ALTER ... RENAME``
        would rewrite the stored DDL with quoted identifiers and break the
        manifest).
        """

        staging = f"{table_name}_v2"
        connection.execute(f"DROP TABLE IF EXISTS {staging}")
        connection.execute(_TABLE_DDL[table_name].replace(table_name, staging, 1))
        selection = ", ".join(old_columns)
        connection.execute(
            f"INSERT INTO {staging} ({selection}) SELECT {selection} FROM {table_name}"
        )
        if extra_values:
            connection.execute(f"""
                UPDATE {staging}
                SET {", ".join(f"{column}={value}" for column, value in extra_values.items())}
                """)
        connection.execute(f"DROP TABLE {table_name}")
        connection.execute(_TABLE_DDL[table_name])
        connection.execute(
            f"INSERT INTO {table_name} ({selection}) SELECT {selection} FROM {staging}"
        )
        if extra_values:
            connection.execute(f"""
                UPDATE {table_name}
                SET {", ".join(f"{column}={value}" for column, value in extra_values.items())}
                """)
        connection.execute(f"DROP TABLE {staging}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    # -- answer lifecycle -----------------------------------------------

    def find_answer_replay(
        self,
        *,
        principal_id: str,
        idempotency_key: str,
        contract: Mapping[str, object] | str,
        input_snapshot: object,
        continuation_token: str | None = None,
    ) -> AnswerWrite | None:
        """Return a prior explicit answer before invoking the runtime.

        A continuation lookup also verifies that the chain is active and idle.
        The mutating append path repeats those checks after runtime completion.
        """

        self._validate_identity(principal_id, idempotency_key)
        contract_json, _contract_hash = _canonical_object(contract, "contract")
        input_json, _input_hash = _canonical_object(input_snapshot, "input_snapshot")
        request_hash = _hash_text(f"{contract_json}\x00{input_json}")
        key_hash = _hash_text(idempotency_key)
        connection = self._connect()
        try:
            chain: sqlite3.Row | None = None
            if continuation_token is not None:
                chain = self._resolve_chain(
                    connection,
                    principal_id=principal_id,
                    continuation_token=continuation_token,
                )
                if str(chain["status"]) == "closed":
                    raise SemanticStateError("chain_closed")
                if chain["active_job_id"] is not None:
                    raise SemanticStateError("research_in_progress")
            replay = self._resolve_answer_idempotency(
                connection,
                principal_id=principal_id,
                key_hash=key_hash,
                request_hash=request_hash,
            )
            if (
                replay is not None
                and chain is not None
                and replay.chain_id != str(chain["chain_id"])
            ):
                raise SemanticStateError("idempotency_conflict")
            return replay
        finally:
            connection.close()

    def create_answer(
        self,
        *,
        principal_id: str,
        idempotency_key: str,
        contract: Mapping[str, object] | str,
        input_snapshot: object,
        status: str,
        product: Mapping[str, object],
        artifact_hash: str,
        now: float,
        data_gaps: tuple[str, ...] = (),
        provenance: Mapping[str, object] | None = None,
        runtime_handle: Mapping[str, object] | None = None,
        continuity_degraded: bool = False,
        route_key: str | None = None,
        route_generation: str | None = None,
        route_expected_revision: int | None = None,
    ) -> AnswerWrite:
        """Atomically create an answer chain, product and immutable version.

        Optional A5L-2 route: expected-absent create or revision CAS rotate
        lands in the SAME transaction as chain/product/exact handle, so a
        route only becomes (or stays) active when the whole success is
        durable (D-03).
        """

        self._validate_identity(principal_id, idempotency_key)
        self._validate_route_generation_fence(
            route_key=route_key,
            route_generation=route_generation,
            route_expected_revision=route_expected_revision,
        )
        self._validate_answer(status=status, product=product, artifact_hash=artifact_hash, now=now)
        contract_json, contract_hash = _canonical_object(contract, "contract")
        input_json, input_hash = _canonical_object(input_snapshot, "input_snapshot")
        product_json, product_hash = _canonical_object(product, "product")
        _require_product_artifact_hash(artifact_hash, product_hash=product_hash)
        stored_product = _load_json_object(product_json)
        response_projection = _answer_response_projection(
            as_of=now,
            data_gaps=data_gaps,
            provenance=provenance,
            continuity_degraded=continuity_degraded,
        )
        request_hash = _hash_text(f"{contract_json}\x00{input_json}")
        key_hash = _hash_text(idempotency_key)

        with self._transaction() as connection:
            replay = self._resolve_answer_idempotency(
                connection,
                principal_id=principal_id,
                key_hash=key_hash,
                request_hash=request_hash,
            )
            if replay is not None:
                if route_key is not None and route_generation is not None:
                    self._fence_answer_route_replay(
                        connection,
                        route_key=route_key,
                        route_generation=route_generation,
                        route_expected_revision=route_expected_revision,
                        replay_chain_id=replay.chain_id,
                    )
                return replay

            chain_id = secrets.token_hex(32)
            product_id = uuid4().hex
            continuation_token = self._token_for(principal_id, chain_id)
            if route_key is not None and route_generation is not None:
                if route_expected_revision is None:
                    try:
                        self._write_route_create(
                            connection,
                            route_key=route_key,
                            generation=route_generation,
                            chain_id=chain_id,
                            now=now,
                        )
                    except sqlite3.IntegrityError:
                        raise SemanticStateError("route_conflict") from None
                else:
                    self._write_route_rotate(
                        connection,
                        route_key=route_key,
                        generation=route_generation,
                        chain_id=chain_id,
                        expected_revision=route_expected_revision,
                        now=now,
                    )
            connection.execute(
                """
                INSERT INTO chains(chain_id, principal_id, status, created_at, updated_at)
                VALUES (?, ?, 'active', ?, ?)
                """,
                (chain_id, principal_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO continuations(
                    token_hash, epoch, principal_id, chain_id, active_job_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    _hash_text(continuation_token),
                    self._epoch,
                    principal_id,
                    chain_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO products(
                    product_id, chain_id, job_id, product_version, status,
                    product_json, artifact_hash, created_at
                ) VALUES (?, ?, NULL, 1, ?, ?, ?, ?)
                """,
                (product_id, chain_id, status, product_json, artifact_hash, now),
            )
            self._append_version(
                connection,
                chain_id=chain_id,
                kind="answer",
                job_id=None,
                product_id=product_id,
                contract_json=contract_json,
                input_json=input_json,
                contract_hash=contract_hash,
                input_hash=input_hash,
                payload={
                    "product_version": 1,
                    "status": status,
                    "response_projection": response_projection,
                },
                now=now,
            )
            connection.execute(
                """
                INSERT INTO idempotency(
                    principal_id, capability, key_hash, request_hash,
                    chain_id, job_id, product_id, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    principal_id,
                    CAPABILITY,
                    key_hash,
                    request_hash,
                    chain_id,
                    product_id,
                    now,
                ),
            )
            if runtime_handle is None:
                connection.execute(
                    "UPDATE continuations SET updated_at=? WHERE chain_id=?",
                    (now, chain_id),
                )
            else:
                if not isinstance(runtime_handle, Mapping):
                    raise SemanticStateError("runtime_handle_invalid")
                if set(runtime_handle) != _RUNTIME_HANDLE_KEYS:
                    raise SemanticStateError("runtime_handle_invalid")
                backend = runtime_handle.get("backend")
                session_id = runtime_handle.get("session_id")
                identity_hash = runtime_handle.get("identity_hash")
                handle_version = runtime_handle.get("product_version")
                if (
                    backend != "codex-cli"
                    or not isinstance(session_id, str)
                    or not _valid_uuid_text(session_id)
                    or not isinstance(identity_hash, str)
                    or _SHA256_RE.fullmatch(identity_hash) is None
                    or not isinstance(handle_version, int)
                    or isinstance(handle_version, bool)
                    or handle_version != 1
                ):
                    # initial 的 handle 必须指向本轮版本 1——随成功 product 版本
                    # 原子推进，追问才能 resume 同一后台 agent 会话。
                    raise SemanticStateError("runtime_handle_invalid")
                connection.execute(
                    """
                    UPDATE continuations
                    SET runtime_backend=?,
                        session_id=?,
                        identity_hash=?,
                        product_version=?,
                        updated_at=?
                    WHERE chain_id=?
                    """,
                    (backend, session_id, identity_hash, handle_version, now, chain_id),
                )
            return AnswerWrite(
                chain_id=chain_id,
                product_id=product_id,
                continuation_token=continuation_token,
                product_version=1,
                status=status,
                product=stored_product,
                artifact_hash=artifact_hash,
                as_of=now,
                data_gaps=cast(tuple[str, ...], response_projection["data_gaps"]),
                provenance=cast(dict[str, object], response_projection["provenance"]),
                continuity_degraded=cast(bool, response_projection["continuity_degraded"]),
            )

    def resolve_route(self, *, route_key: str) -> dict[str, object] | None:
        """Read-only A5L-2 route projection."""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM conversation_routes WHERE route_key=?", (route_key,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        self._validated_conversation_route(row)
        return dict(row)

    @staticmethod
    def _validated_conversation_route(
        row: sqlite3.Row,
    ) -> tuple[str, str, int, tuple[str, ...]]:
        """Validate the route state needed for generation fencing."""

        active_generation = row["active_generation"]
        active_chain_id = row["active_chain_id"]
        active_revision = row["active_revision"]
        seen_json = row["seen_generations_json"]
        if (
            not isinstance(active_generation, str)
            or not active_generation.strip()
            or not isinstance(active_chain_id, str)
            or not active_chain_id.strip()
            or not isinstance(active_revision, int)
            or isinstance(active_revision, bool)
            or active_revision < 1
            or not isinstance(seen_json, str)
        ):
            raise SemanticStateError("semantic_state_corrupt")
        try:
            seen = json.loads(seen_json)
        except (TypeError, ValueError):
            raise SemanticStateError("semantic_state_corrupt") from None
        if (
            not isinstance(seen, list)
            or not seen
            or any(not isinstance(item, str) or not item.strip() for item in seen)
            or len(seen) != len(set(seen))
            or active_generation not in seen
        ):
            raise SemanticStateError("semantic_state_corrupt")
        return active_generation, active_chain_id, active_revision, tuple(seen)

    def _load_valid_conversation_route(
        self,
        connection: sqlite3.Connection,
        *,
        route_key: str,
    ) -> tuple[str, str, int, tuple[str, ...]] | None:
        row = connection.execute(
            "SELECT * FROM conversation_routes WHERE route_key=?",
            (route_key,),
        ).fetchone()
        return None if row is None else self._validated_conversation_route(row)

    def _fence_answer_route_admission(
        self,
        connection: sqlite3.Connection,
        *,
        route_key: str,
        route_generation: str,
        route_expected_revision: int | None,
    ) -> tuple[str, str, int, tuple[str, ...]] | None:
        """Require the pre-state for one NEW or ROTATE answer write."""

        route = self._load_valid_conversation_route(connection, route_key=route_key)
        if route_expected_revision is None:
            if route is not None:
                raise SemanticStateError("route_conflict")
            return None
        if route is None:
            raise SemanticStateError("route_revision_conflict")
        active_generation, _chain_id, active_revision, seen = route
        if route_generation in seen:
            if route_generation != active_generation:
                raise SemanticStateError("continuation_not_accessible")
            raise SemanticStateError("route_revision_conflict")
        if active_revision != route_expected_revision:
            raise SemanticStateError("route_revision_conflict")
        return route

    def _fence_answer_route_replay(
        self,
        connection: sqlite3.Connection,
        *,
        route_key: str,
        route_generation: str,
        route_expected_revision: int | None,
        replay_chain_id: str,
    ) -> None:
        """Accept only the post-state produced by this exact routed answer."""

        route = self._load_valid_conversation_route(connection, route_key=route_key)
        if route is None:
            raise SemanticStateError("route_revision_conflict")
        active_generation, active_chain_id, active_revision, seen = route
        if route_generation in seen and route_generation != active_generation:
            raise SemanticStateError("continuation_not_accessible")
        expected_post_revision = (
            1 if route_expected_revision is None else route_expected_revision + 1
        )
        if (
            active_generation != route_generation
            or active_chain_id != replay_chain_id
            or active_revision != expected_post_revision
        ):
            raise SemanticStateError("route_revision_conflict")

    def chain_continuation_token(self, *, principal_id: str, chain_id: str) -> str:
        """FIN-derived continuation token for one active chain (server-side)."""
        connection = self._connect()
        try:
            owner = connection.execute(
                "SELECT principal_id FROM chains WHERE chain_id=?", (chain_id,)
            ).fetchone()
        finally:
            connection.close()
        if owner is None or str(owner["principal_id"]) != principal_id:
            raise SemanticStateError("continuation_not_accessible")
        return self._token_for(principal_id, chain_id)

    def _write_route_create(
        self,
        connection: sqlite3.Connection,
        *,
        route_key: str,
        generation: str,
        chain_id: str,
        now: float,
    ) -> None:
        """Expected-absent route create inside one answer transaction."""
        connection.execute(
            """
            INSERT INTO conversation_routes(
                route_key, active_generation, active_chain_id, active_revision,
                seen_generations_json, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (
                route_key,
                generation,
                chain_id,
                json.dumps([generation], separators=(",", ":")),
                now,
                now,
            ),
        )

    def _write_route_rotate(
        self,
        connection: sqlite3.Connection,
        *,
        route_key: str,
        generation: str,
        chain_id: str,
        expected_revision: int,
        now: float,
    ) -> None:
        """CAS rotate of the active generation (success-only switch).

        The seen set is preserved and extended: an old generation must stay
        identifiable after a newer one becomes active (D-01).
        """
        route = self._fence_answer_route_admission(
            connection,
            route_key=route_key,
            route_generation=generation,
            route_expected_revision=expected_revision,
        )
        if route is None:  # pragma: no cover - rotate requires an existing route
            raise SemanticStateError("route_revision_conflict")
        _active_generation, _chain_id, _active_revision, seen_values = route
        seen = [*seen_values, generation]
        cursor = connection.execute(
            """
            UPDATE conversation_routes
            SET active_generation=?, active_chain_id=?,
                active_revision=active_revision+1,
                seen_generations_json=?, updated_at=?
            WHERE route_key=? AND active_revision=?
            """,
            (
                generation,
                chain_id,
                json.dumps(seen, separators=(",", ":")),
                now,
                route_key,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise SemanticStateError("route_revision_conflict")

    def begin_answer_turn(
        self,
        *,
        principal_id: str,
        continuation_token: str,
        turn_id: str,
        lease_seconds: float,
        now: float,
    ) -> int:
        """Atomically acquire the single-flight turn lease for one chain.

        返回本轮 fencing token。同链并发 follow-up 只有持 lease 者能推进
        product version；lease 未到期时其他 turn 返回 retryable
        ``turn_lease_held``。lease 到期后（超时/崩溃残留）允许接管，
        fencing token 单调递增使旧 lease 的 release 失效。
        """
        _require_non_empty(turn_id, "turn_id")
        if (
            not isinstance(lease_seconds, (int, float))
            or isinstance(lease_seconds, bool)
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("turn lease duration is invalid")
        expiry = _require_finite_expiry(now, lease_seconds, "turn_lease")
        token_hash = _hash_text(continuation_token)
        with self._transaction() as connection:
            chain = self._resolve_chain(
                connection,
                principal_id=principal_id,
                continuation_token=continuation_token,
            )
            if str(chain["status"]) == "closed":
                raise SemanticStateError("chain_closed")
            cursor = connection.execute(
                """
                UPDATE continuations
                SET active_turn_id=?,
                    turn_lease_expires_at=?,
                    turn_fencing_token=turn_fencing_token+1,
                    updated_at=?
                WHERE token_hash=?
                  AND principal_id=?
                  AND (
                      active_turn_id IS NULL
                      OR turn_lease_expires_at IS NULL
                      OR turn_lease_expires_at <= ?
                  )
                RETURNING turn_fencing_token
                """,
                (
                    turn_id,
                    expiry,
                    now,
                    token_hash,
                    principal_id,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise SemanticStateError("turn_lease_held")
            fencing_token = int(row[0])
        return fencing_token

    def release_answer_turn(
        self,
        *,
        principal_id: str,
        continuation_token: str,
        turn_id: str,
        fencing_token: int,
        now: float,
    ) -> None:
        """Release the turn lease (fencing-token guarded, stale release no-op)."""
        _require_non_empty(turn_id, "turn_id")
        _require_finite(now, "now")
        if (
            not isinstance(fencing_token, int)
            or isinstance(fencing_token, bool)
            or fencing_token < 0
        ):
            raise ValueError("turn fencing token is invalid")
        token_hash = _hash_text(continuation_token)
        with self._transaction() as connection:
            _ = self._resolve_chain(
                connection,
                principal_id=principal_id,
                continuation_token=continuation_token,
            )
            connection.execute(
                """
                UPDATE continuations
                SET active_turn_id=NULL,
                    turn_lease_expires_at=NULL,
                    updated_at=?
                WHERE token_hash=?
                  AND principal_id=?
                  AND active_turn_id=?
                  AND turn_fencing_token=?
                """,
                (now, token_hash, principal_id, turn_id, fencing_token),
            )

    def append_answer(
        self,
        *,
        principal_id: str,
        continuation_token: str,
        idempotency_key: str,
        contract: Mapping[str, object] | str,
        input_snapshot: object,
        expected_parent_product_version: int,
        status: str,
        product: Mapping[str, object],
        artifact_hash: str,
        now: float,
        data_gaps: tuple[str, ...] = (),
        provenance: Mapping[str, object] | None = None,
        runtime_handle: Mapping[str, object] | None = None,
        turn_id: str | None = None,
        fencing_token: int | None = None,
        continuity_degraded: bool = False,
        route_key: str | None = None,
        route_generation: str | None = None,
        route_expected_revision: int | None = None,
    ) -> AnswerWrite:
        """Atomically append one answer product to an active idle chain.

        可选 ``runtime_handle`` 为 provider-private continuation envelope
        （backend/session_id/identity_hash/product_version，仅有界 handle，
        不含 transcript/prompt/credential），与本轮 product version CAS 同一
        事务原子推进；可选 ``turn_id``/``fencing_token`` 校验当前 turn 仍持有
        lease（防跨 turn 推进）。
        """

        self._validate_identity(principal_id, idempotency_key)
        self._validate_route_generation_fence(
            route_key=route_key,
            route_generation=route_generation,
            route_expected_revision=route_expected_revision,
        )
        if route_key is not None and route_expected_revision is None:
            raise ValueError("continuation route revision is required")
        if (
            not isinstance(expected_parent_product_version, int)
            or isinstance(expected_parent_product_version, bool)
            or expected_parent_product_version < 1
        ):
            raise ValueError("expected parent product version is invalid")
        self._validate_answer(status=status, product=product, artifact_hash=artifact_hash, now=now)
        contract_json, contract_hash = _canonical_object(contract, "contract")
        input_json, input_hash = _canonical_object(input_snapshot, "input_snapshot")
        product_json, product_hash = _canonical_object(product, "product")
        _require_product_artifact_hash(artifact_hash, product_hash=product_hash)
        stored_product = _load_json_object(product_json)
        response_projection = _answer_response_projection(
            as_of=now,
            data_gaps=data_gaps,
            provenance=provenance,
            continuity_degraded=continuity_degraded,
        )
        request_hash = _hash_text(f"{contract_json}\x00{input_json}")
        key_hash = _hash_text(idempotency_key)

        with self._transaction() as connection:
            chain = self._resolve_chain(
                connection,
                principal_id=principal_id,
                continuation_token=continuation_token,
            )
            if str(chain["status"]) == "closed":
                raise SemanticStateError("chain_closed")
            if chain["active_job_id"] is not None:
                raise SemanticStateError("research_in_progress")
            if (
                route_key is not None
                and route_generation is not None
                and route_expected_revision is not None
            ):
                self._require_active_route_generation(
                    connection,
                    route_key=route_key,
                    route_generation=route_generation,
                    route_expected_revision=route_expected_revision,
                    chain_id=str(chain["chain_id"]),
                )
            # turn/fencing 成对、类型与 live lease 校验必须先于 idempotency
            # replay——replay 路径同样受 fencing 契约约束，不能绕过。
            if (turn_id is None) != (fencing_token is None):
                # turn_id 与 fencing_token 必须成对出现，缺一即拒绝
                raise SemanticStateError("turn_fencing_required")
            if (
                turn_id is None
                and chain["active_turn_id"] is not None
                and chain["turn_lease_expires_at"] is not None
                and float(chain["turn_lease_expires_at"]) > now
            ):
                # 链上已有活动 lease（未到期）：省略 turn proof 不得推进——
                # 只有 lease holder 能推进 product version。
                raise SemanticStateError("turn_fencing_required")
            if turn_id is not None:
                if (
                    not isinstance(fencing_token, int)
                    or isinstance(fencing_token, bool)
                    or fencing_token < 0
                ):
                    raise ValueError("turn fencing token is invalid")
                # turn lease 校验：当前 turn 必须仍持有有效 lease（到期即失效）
                if (
                    chain["active_turn_id"] != turn_id
                    or chain["turn_lease_expires_at"] is None
                    or float(chain["turn_lease_expires_at"]) <= now
                ):
                    raise SemanticStateError("turn_lease_expired")
                if int(chain["turn_fencing_token"]) != fencing_token:
                    raise SemanticStateError("turn_fencing_conflict")
            replay = self._resolve_answer_idempotency(
                connection,
                principal_id=principal_id,
                key_hash=key_hash,
                request_hash=request_hash,
            )
            if replay is not None:
                if replay.chain_id != str(chain["chain_id"]):
                    raise SemanticStateError("idempotency_conflict")
                return replay

            chain_id = str(chain["chain_id"])
            current_product_version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(product_version), 0) FROM products WHERE chain_id=?",
                    (chain_id,),
                ).fetchone()[0]
            )
            if current_product_version != expected_parent_product_version:
                raise SemanticStateError("continuation_conflict")
            product_version = current_product_version + 1
            product_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO products(
                    product_id, chain_id, job_id, product_version, status,
                    product_json, artifact_hash, created_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    chain_id,
                    product_version,
                    status,
                    product_json,
                    artifact_hash,
                    now,
                ),
            )
            self._append_version(
                connection,
                chain_id=chain_id,
                kind="answer",
                job_id=None,
                product_id=product_id,
                contract_json=contract_json,
                input_json=input_json,
                contract_hash=contract_hash,
                input_hash=input_hash,
                payload={
                    "product_version": product_version,
                    "status": status,
                    "response_projection": response_projection,
                },
                now=now,
            )
            connection.execute(
                """
                INSERT INTO idempotency(
                    principal_id, capability, key_hash, request_hash,
                    chain_id, job_id, product_id, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    principal_id,
                    CAPABILITY,
                    key_hash,
                    request_hash,
                    chain_id,
                    product_id,
                    now,
                ),
            )
            connection.execute(
                "UPDATE chains SET updated_at=? WHERE chain_id=?",
                (now, chain_id),
            )
            if runtime_handle is None:
                connection.execute(
                    "UPDATE continuations SET updated_at=? WHERE chain_id=?",
                    (now, chain_id),
                )
            else:
                if not isinstance(runtime_handle, Mapping):
                    raise SemanticStateError("runtime_handle_invalid")
                if set(runtime_handle) != _RUNTIME_HANDLE_KEYS:
                    raise SemanticStateError("runtime_handle_invalid")
                backend = runtime_handle.get("backend")
                session_id = runtime_handle.get("session_id")
                identity_hash = runtime_handle.get("identity_hash")
                handle_version = runtime_handle.get("product_version")
                if (
                    backend != "codex-cli"
                    or not isinstance(session_id, str)
                    or not _valid_uuid_text(session_id)
                    or not isinstance(identity_hash, str)
                    or _SHA256_RE.fullmatch(identity_hash) is None
                    or not isinstance(handle_version, int)
                    or isinstance(handle_version, bool)
                    or handle_version != product_version
                ):
                    # handle 的 product_version 必须等于本轮成功推进的版本：
                    # 随成功 product version 原子推进，不允许指向任意版本。
                    raise SemanticStateError("runtime_handle_invalid")
                connection.execute(
                    """
                    UPDATE continuations
                    SET runtime_backend=?,
                        session_id=?,
                        identity_hash=?,
                        product_version=?,
                        updated_at=?
                    WHERE chain_id=?
                    """,
                    (backend, session_id, identity_hash, handle_version, now, chain_id),
                )
            return AnswerWrite(
                chain_id=chain_id,
                product_id=product_id,
                continuation_token=continuation_token,
                product_version=product_version,
                status=status,
                product=stored_product,
                artifact_hash=artifact_hash,
                as_of=now,
                data_gaps=cast(tuple[str, ...], response_projection["data_gaps"]),
                provenance=cast(dict[str, object], response_projection["provenance"]),
                continuity_degraded=cast(bool, response_projection["continuity_degraded"]),
            )

    # -- runtime session GC ----------------------------------------------

    def list_runtime_session_gc(self) -> list[dict[str, object]]:
        """只读列举 runtime_session_gc 行（janitor 回收决策用）。

        GC 表属于 repository 的 schema/epoch/integrity 边界；janitor 不直接
        读写 SQLite，只通过这里取数并驱动 store 删除。返回 SQLite 原始值
        （str/int/float/None），逐行类型与域校验由 janitor 承担——一条坏
        行不吞掉其余合法行。
        """
        with self._transaction() as connection:
            rows = connection.execute("""
                SELECT session_id, chain_id, product_version, captured_at, bytes,
                       state, created_at, updated_at
                FROM runtime_session_gc
                ORDER BY captured_at
                """).fetchall()
        # 返回原始 SQLite 值（str/int/float/None），不做批量强转——
        # 一条坏类型行不得吞掉其余合法行；逐行校验由 janitor 承担。
        return [
            {
                "session_id": row["session_id"],
                "chain_id": row["chain_id"],
                "product_version": row["product_version"],
                "captured_at": row["captured_at"],
                "bytes": row["bytes"],
                "state": row["state"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def retire_runtime_session(self, *, session_id: str, now: float) -> bool:
        """把 active session 标记为 retired（idle 过期）；返回是否真的变更。"""
        _require_non_empty(session_id, "session_id")
        _require_finite(now, "now")
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE runtime_session_gc SET state='retired', updated_at=? "
                "WHERE session_id=? AND state='active'",
                (now, session_id),
            )
            return cursor.rowcount > 0

    def reconcile_runtime_session_deleted(
        self,
        *,
        session_id: str,
        observed_pointer: int,
        remaining_versions: Sequence[int],
        now: float,
    ) -> bool:
        """artifact 删除后单一事务 CAS 推进 GC 指针（janitor 删除后调用）。

        ``observed_pointer`` 是 janitor 观察到的 GC 行指针，作为 expected
        generation：UPDATE/DELETE 只作用在 GC 行仍指向该值时——并发 capture
        已推进指针时本次 reconcile 是完整 no-op（不覆盖新 handle/capture、
        不碰 continuation fence）。配额回收删除的是最旧版本（指针可能仍指向
        更新版本），因此 CAS 守卫在 observed_pointer，而不是被删版本。
        remaining_versions 为空 → 先 CAS 删除 GC 行（未命中立即 no-op），
        命中后才在同一事务内清空 runtime handle；否则把指针推进到最大剩余
        版本（同样受 observed_pointer CAS 约束）。handle 清空与 tombstone
        删除同事务——clear 失败不会留下无 tombstone 的悬挂 handle。
        返回 CAS 是否命中：miss（指针已被并发推进）时调用方不得计入删除成功。
        """
        _require_non_empty(session_id, "session_id")
        _require_finite(now, "now")
        if (
            not isinstance(observed_pointer, int)
            or isinstance(observed_pointer, bool)
            or not 1 <= observed_pointer < 2**31
        ):
            raise ValueError("observed pointer is invalid")
        if not isinstance(remaining_versions, Sequence):
            raise ValueError("remaining versions is invalid")
        versions: list[int] = []
        for raw_version in remaining_versions:
            if (
                not isinstance(raw_version, int)
                or isinstance(raw_version, bool)
                or not 1 <= raw_version < 2**31
            ):
                raise ValueError("remaining version is invalid")
            versions.append(raw_version)
        with self._transaction() as connection:
            if not versions:
                # 先 CAS 删除 GC 行；未命中（指针已被并发推进）→ 完整 no-op
                cursor = connection.execute(
                    "DELETE FROM runtime_session_gc WHERE session_id=? AND product_version=?",
                    (session_id, observed_pointer),
                )
                if cursor.rowcount == 0:
                    return False
                connection.execute(
                    """
                    UPDATE continuations
                    SET runtime_backend=NULL,
                        session_id=NULL,
                        identity_hash=NULL,
                        product_version=NULL,
                        active_turn_id=NULL,
                        turn_lease_expires_at=NULL,
                        turn_fencing_token=turn_fencing_token + 1,
                        updated_at=?
                    WHERE session_id=? AND product_version=?
                    """,
                    (now, session_id, observed_pointer),
                )
                return True
            latest_version = max(versions)
            latest_bytes = connection.execute(
                """
                SELECT bytes FROM runtime_session_gc WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
            bytes_value = int(latest_bytes["bytes"]) if latest_bytes is not None else 0
            cursor = connection.execute(
                "UPDATE runtime_session_gc SET product_version=?, bytes=?, updated_at=? "
                "WHERE session_id=? AND product_version=?",
                (latest_version, bytes_value, now, session_id, observed_pointer),
            )
            # 行不存在或已被并发推进（expected pointer 不匹配）：幂等
            # reconcile，不抛错（并发 capture 已接管指针）。
            return cursor.rowcount > 0

    def upsert_runtime_session_gc(
        self,
        *,
        session_id: str,
        chain_id: str,
        product_version: int,
        captured_at: float,
        bytes_value: int,
        now: float,
    ) -> None:
        """repository-owned runtime_session_gc 写入（capture/append 后调用）。

        GC 生命周期记录由 repository 统一写入（janitor 不直写 SQLite）；
        state 固定 active，updated_at/created_at 为 now。
        """
        _require_non_empty(session_id, "session_id")
        _require_non_empty(chain_id, "chain_id")
        _require_finite(captured_at, "captured_at")
        _require_finite(now, "now")
        if (
            not isinstance(product_version, int)
            or isinstance(product_version, bool)
            or not 1 <= product_version < 2**31
        ):
            raise ValueError("product version is invalid")
        if not isinstance(bytes_value, int) or isinstance(bytes_value, bool) or bytes_value < 0:
            raise ValueError("bytes value is invalid")
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_session_gc(
                    session_id, chain_id, product_version, captured_at, bytes,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    chain_id=excluded.chain_id,
                    product_version=excluded.product_version,
                    captured_at=excluded.captured_at,
                    bytes=excluded.bytes,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    chain_id,
                    product_version,
                    captured_at,
                    bytes_value,
                    now,
                    now,
                ),
            )

    def renew_answer_turn(
        self,
        *,
        principal_id: str,
        continuation_token: str,
        turn_id: str,
        fencing_token: int,
        lease_seconds: float,
        now: float,
    ) -> int:
        """fencing-guarded turn lease 续期；返回新 fencing token（单调递增）。

        只有当前持有 lease 的 turn 可续期；过期/stale holder 不能续期。
        """
        _require_non_empty(turn_id, "turn_id")
        _require_finite(now, "now")
        if (
            not isinstance(lease_seconds, (int, float))
            or isinstance(lease_seconds, bool)
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("turn lease duration is invalid")
        if (
            not isinstance(fencing_token, int)
            or isinstance(fencing_token, bool)
            or fencing_token < 0
        ):
            raise ValueError("turn fencing token is invalid")
        expiry = _require_finite_expiry(now, lease_seconds, "turn_lease")
        token_hash = _hash_text(continuation_token)
        with self._transaction() as connection:
            chain = self._resolve_chain(
                connection,
                principal_id=principal_id,
                continuation_token=continuation_token,
            )
            if str(chain["status"]) == "closed":
                raise SemanticStateError("chain_closed")
            if (
                chain["active_turn_id"] != turn_id
                or chain["turn_lease_expires_at"] is None
                or float(chain["turn_lease_expires_at"]) <= now
                or int(chain["turn_fencing_token"]) != fencing_token
            ):
                raise SemanticStateError("turn_lease_expired")
            cursor = connection.execute(
                """
                UPDATE continuations
                SET turn_lease_expires_at=?,
                    turn_fencing_token=turn_fencing_token + 1,
                    updated_at=?
                WHERE token_hash=? AND principal_id=?
                RETURNING turn_fencing_token
                """,
                (expiry, now, token_hash, principal_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise SemanticStateError("turn_lease_expired")
            return int(row[0])

    # -- daily workspace ------------------------------------------------

    def _workspace_ref_for(self, principal_id: str, chain_id: str) -> str:
        """Opaque principal-bound workspace identity, never the chain id.

        Stable for one principal/chain; reveals nothing about the internal
        chain identity or continuation token.
        """

        digest = hmac.new(
            self._token_secret,
            f"{self._epoch}\x00daily_workspace\x00{principal_id}\x00{chain_id}".encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _resolve_daily_chain(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        trading_day_id: str,
    ) -> sqlite3.Row | None:
        _require_non_empty(trading_day_id, "trading_day_id")
        row = connection.execute(
            """
            SELECT chain_id, principal_id, status, created_at, updated_at
            FROM chains
            WHERE principal_id = ? AND chain_kind = ? AND business_key = ?
            """,
            (principal_id, DAILY_WORKSPACE_KIND, trading_day_id),
        ).fetchone()
        return cast("sqlite3.Row | None", row)

    def _resolve_daily_idempotency(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        key_hash: str,
        request_hash: str,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            """
            SELECT request_hash, chain_id, job_id, product_id
            FROM idempotency
            WHERE principal_id=? AND capability=? AND key_hash=?
            """,
            (principal_id, "daily_workspace", key_hash),
        ).fetchone()
        if row is None:
            return None
        if hmac.compare_digest(str(row["request_hash"]), request_hash):
            return cast("sqlite3.Row", row)
        if row["job_id"] is None and row["product_id"] is None:
            # Unbound checkpoint claim (acquire); the caller continues the
            # generation path and binds the product on append.
            return cast("sqlite3.Row", row)
        raise SemanticStateError("idempotency_conflict")

    def find_daily_workspace(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
    ) -> DailyWorkspaceRead | None:
        """Return the latest daily workspace version, hash-verified, or None."""

        _require_non_empty(principal_id, "principal_id")
        connection = self._connect()
        try:
            chain = self._resolve_daily_chain(
                connection,
                principal_id=principal_id,
                trading_day_id=trading_day_id,
            )
            if chain is None:
                return None
            has_product = connection.execute(
                "SELECT 1 FROM products WHERE chain_id=? LIMIT 1",
                (str(chain["chain_id"]),),
            ).fetchone()
            if has_product is None:
                return None
            return self._latest_daily_workspace(
                connection,
                principal_id=principal_id,
                trading_day_id=trading_day_id,
                chain_id=str(chain["chain_id"]),
            )
        finally:
            connection.close()

    def read_daily_workspace_timing_samples(
        self,
        *,
        principal_id: str,
        checkpoint: str,
        max_samples: int,
    ) -> tuple[DailyWorkspaceTimingSample, ...]:
        """Return newest-first, bounded actual-Agent timings for one checkpoint.

        This is intentionally a projection rather than a generic product
        history API: it reads only canonical scheduled checkpoint versions,
        verifies each artifact, and exposes only the timing facts needed for a
        future prepare-lead percentile policy.  Deterministic degraded rows
        where the Agent never ran are not samples.
        """

        _require_non_empty(principal_id, "principal_id")
        if checkpoint not in _DAILY_WORKSPACE_CHECKPOINTS:
            raise ValueError("checkpoint is invalid")
        if (
            not isinstance(max_samples, int)
            or isinstance(max_samples, bool)
            or not 1 <= max_samples <= _MAX_DAILY_WORKSPACE_TIMING_SAMPLES
        ):
            raise ValueError("max_samples is invalid")

        candidate_limit = max_samples * _DAILY_WORKSPACE_TIMING_CANDIDATE_MULTIPLIER
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT c.business_key AS trading_day_id, i.key_hash,
                       p.product_json, p.artifact_hash
                FROM chains AS c
                JOIN idempotency AS i
                  ON i.chain_id = c.chain_id AND i.product_id IS NOT NULL
                JOIN products AS p ON p.product_id = i.product_id
                WHERE c.principal_id = ?
                  AND c.chain_kind = ?
                  AND i.principal_id = ?
                  AND i.capability = 'daily_workspace'
                ORDER BY c.business_key DESC, p.product_version DESC
                LIMIT ?
                """,
                (principal_id, DAILY_WORKSPACE_KIND, principal_id, candidate_limit),
            ).fetchall()
            samples: list[DailyWorkspaceTimingSample] = []
            for row in rows:
                trading_day_id = row["trading_day_id"]
                if not isinstance(trading_day_id, str):
                    continue
                expected_key_hash = _hash_text(f"daily:{trading_day_id}:{checkpoint}")
                key_hash = row["key_hash"]
                if not isinstance(key_hash, str) or not hmac.compare_digest(
                    key_hash.encode("utf-8"), expected_key_hash.encode("utf-8")
                ):
                    continue
                product = _load_verified_product(
                    str(row["product_json"]), str(row["artifact_hash"])
                )
                sample = _daily_workspace_timing_sample(
                    product,
                    trading_day_id=trading_day_id,
                    checkpoint=checkpoint,
                )
                if sample is not None:
                    samples.append(sample)
                if len(samples) == max_samples:
                    break
            return tuple(samples)
        finally:
            connection.close()

    def create_daily_workspace_chain(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        now: float,
    ) -> DailyWorkspaceChain:
        """Atomically create the one daily workspace chain for a trading day.

        Idempotent: a second create with the same idempotency key returns the
        existing chain without writing anything; a different key for an
        existing (principal, day) raises ``daily_workspace_chain_exists``.
        """

        self._validate_identity(principal_id, idempotency_key)
        _require_non_empty(trading_day_id, "trading_day_id")
        _require_finite(now, "now")
        key_hash = _hash_text(idempotency_key)
        request_hash = _hash_text(f"daily_workspace\x00{trading_day_id}")
        with self._transaction() as connection:
            chain = self._resolve_daily_chain(
                connection,
                principal_id=principal_id,
                trading_day_id=trading_day_id,
            )
            if chain is not None:
                replay = self._resolve_daily_idempotency(
                    connection,
                    principal_id=principal_id,
                    key_hash=key_hash,
                    request_hash=request_hash,
                )
                if replay is not None:
                    chain_id = str(chain["chain_id"])
                    return DailyWorkspaceChain(
                        chain_id=chain_id,
                        continuation_token=self._token_for(principal_id, chain_id),
                        workspace_ref=self._workspace_ref_for(principal_id, chain_id),
                        trading_day_id=trading_day_id,
                        created_at=float(chain["created_at"]),
                    )
                raise SemanticStateError("daily_workspace_chain_exists")

            chain_id = secrets.token_hex(32)
            continuation_token = self._token_for(principal_id, chain_id)
            connection.execute(
                """
                INSERT INTO chains(
                    chain_id, principal_id, chain_kind, business_key,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    chain_id,
                    principal_id,
                    DAILY_WORKSPACE_KIND,
                    trading_day_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO continuations(
                    token_hash, epoch, principal_id, chain_id, active_job_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    _hash_text(continuation_token),
                    self._epoch,
                    principal_id,
                    chain_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO idempotency(
                    principal_id, capability, key_hash, request_hash,
                    chain_id, job_id, product_id, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    principal_id,
                    "daily_workspace",
                    key_hash,
                    request_hash,
                    chain_id,
                    now,
                ),
            )
            return DailyWorkspaceChain(
                chain_id=chain_id,
                continuation_token=continuation_token,
                workspace_ref=self._workspace_ref_for(principal_id, chain_id),
                trading_day_id=trading_day_id,
                created_at=now,
            )

    def append_daily_workspace_version(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        contract: Mapping[str, object] | str,
        input_snapshot: object,
        expected_parent_product_version: int,
        status: str,
        product: Mapping[str, object],
        now: float,
        data_gaps: tuple[str, ...] = (),
        provenance: Mapping[str, object] | None = None,
        create_delivery_obligation: bool = False,
    ) -> DailyWorkspaceRead:
        """Atomically append one workspace version to the daily chain.

        The parent version is enforced inside the same transaction; any drift
        raises ``continuation_conflict`` instead of forking the chain.  The
        repository binds the FIN-owned identity fields (workspace_ref,
        trading_day_id, versions) into the product and owns its artifact hash.
        """

        self._validate_identity(principal_id, idempotency_key)
        if (
            not isinstance(expected_parent_product_version, int)
            or isinstance(expected_parent_product_version, bool)
            or expected_parent_product_version < 0
        ):
            raise ValueError("expected parent product version is invalid")
        if status not in {"completed", "partial"}:
            raise ValueError("unsupported daily workspace status")
        _require_finite(now, "now")
        # JSON 可序列化校验提前失败；身份绑定与 artifact hash 在事务内完成。
        _canonical_object(product, "product")
        contract_json, contract_hash = _canonical_object(contract, "contract")
        input_json, input_hash = _canonical_object(input_snapshot, "input_snapshot")
        key_hash = _hash_text(idempotency_key)
        request_hash = _hash_text(f"{contract_json}\x00{input_json}")

        with self._transaction() as connection:
            chain = self._resolve_daily_chain(
                connection,
                principal_id=principal_id,
                trading_day_id=trading_day_id,
            )
            if chain is None:
                raise SemanticStateError("daily_workspace_chain_missing")
            if str(chain["status"]) == "closed":
                raise SemanticStateError("chain_closed")
            replay = self._resolve_daily_idempotency(
                connection,
                principal_id=principal_id,
                key_hash=key_hash,
                request_hash=request_hash,
            )
            if replay is not None and replay["product_id"] is not None:
                # Bound version replay: return the exact replayed product.
                if str(replay["chain_id"]) != str(chain["chain_id"]):
                    raise SemanticStateError("idempotency_conflict")
                return self._latest_daily_workspace(
                    connection,
                    principal_id=principal_id,
                    trading_day_id=trading_day_id,
                    chain_id=str(chain["chain_id"]),
                    product_id=str(replay["product_id"]),
                )

            chain_id = str(chain["chain_id"])
            current_product_version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(product_version), 0) FROM products WHERE chain_id=?",
                    (chain_id,),
                ).fetchone()[0]
            )
            if current_product_version != expected_parent_product_version:
                raise SemanticStateError("continuation_conflict")
            product_version = current_product_version + 1
            bound_product = _bind_daily_workspace_product(
                product,
                workspace_ref=self._workspace_ref_for(principal_id, chain_id),
                trading_day_id=trading_day_id,
                product_version=product_version,
                parent_product_version=expected_parent_product_version,
            )
            product_json, product_hash = _canonical_object(bound_product, "product")
            artifact_hash = f"sha256:{product_hash}"
            product_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO products(
                    product_id, chain_id, job_id, product_version, status,
                    product_json, artifact_hash, created_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    chain_id,
                    product_version,
                    status,
                    product_json,
                    artifact_hash,
                    now,
                ),
            )
            self._append_version(
                connection,
                chain_id=chain_id,
                kind="daily_workspace",
                job_id=None,
                product_id=product_id,
                contract_json=contract_json,
                input_json=input_json,
                contract_hash=contract_hash,
                input_hash=input_hash,
                payload={
                    "product_version": product_version,
                    "status": status,
                    "as_of": now,
                    "data_gaps": list(data_gaps),
                    "provenance": dict(provenance) if provenance is not None else None,
                },
                now=now,
            )
            connection.execute(
                """
                INSERT INTO idempotency(
                    principal_id, capability, key_hash, request_hash,
                    chain_id, job_id, product_id, created_at
                ) VALUES (?, 'daily_workspace', ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(principal_id, capability, key_hash)
                DO UPDATE SET
                    request_hash = excluded.request_hash,
                    job_id = NULL,
                    product_id = excluded.product_id
                """,
                (
                    principal_id,
                    key_hash,
                    request_hash,
                    chain_id,
                    product_id,
                    now,
                ),
            )
            connection.execute(
                "UPDATE chains SET updated_at=? WHERE chain_id=?",
                (now, chain_id),
            )
            connection.execute(
                "UPDATE continuations SET updated_at=? WHERE chain_id=?",
                (now, chain_id),
            )
            workspace_ref = self._workspace_ref_for(principal_id, chain_id)
            if create_delivery_obligation:
                # Product + delivery obligation land in the SAME transaction.
                # Only scheduled finalization creates an obligation; on-demand
                # ask versions never do (no delivery consumer — codex P1).
                # presentation_hash stays NULL in PENDING: the exact
                # rendered-message hash is bound at claim time, so the
                # obligation always matches what is actually sent.
                existing_obligation = connection.execute(
                    """
                    SELECT artifact_hash FROM daily_workspace_obligations
                    WHERE workspace_ref=? AND product_version=?
                    """,
                    (workspace_ref, product_version),
                ).fetchone()
                if existing_obligation is not None:
                    # A pre-existing obligation for the same version must bind
                    # the exact same artifact — never silently trust a drifted
                    # row (codex P1: DO NOTHING trusted wrong artifact_hash).
                    if str(existing_obligation["artifact_hash"]) != artifact_hash:
                        raise SemanticStateError("daily_workspace_obligation_conflict")
                else:
                    connection.execute(
                        """
                        INSERT INTO daily_workspace_obligations(
                            workspace_ref, product_version, artifact_hash,
                            presentation_hash, state, created_at, updated_at
                        ) VALUES (?, ?, ?, NULL, 'PENDING', ?, ?)
                        """,
                        (
                            workspace_ref,
                            product_version,
                            artifact_hash,
                            now,
                            now,
                        ),
                    )
            return self._latest_daily_workspace(
                connection,
                principal_id=principal_id,
                trading_day_id=trading_day_id,
                chain_id=chain_id,
            )

    def finalize_scheduled_checkpoint(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        checkpoint: str,
        product: Mapping[str, object],
        now: float,
        presentation_hash: str | None = None,
        expected_parent_product_version: int = 0,
    ) -> DailyWorkspaceFinalization:
        """Atomically append one scheduled workspace version AND its delivery
        obligation in a single ``BEGIN IMMEDIATE`` transaction.

        The obligation is uniquely bound to ``(workspace_ref, product_version)``
        and carries ``artifact_hash``.  A crash between product commit and
        obligation insert is impossible: both land together or neither
        (handoff 11:39 P1 finding).  ``presentation_hash`` is optional here:
        the exact rendered-message hash is bound at claim time (delivery
        renders after the product exists), so no precomputed fake hash is
        ever persisted (codex P1).
        """

        read = self.append_daily_workspace_version(
            principal_id=principal_id,
            trading_day_id=trading_day_id,
            idempotency_key=idempotency_key,
            contract={
                "schema": "fin.daily-workspace-contract/v1",
                "checkpoint": checkpoint,
            },
            input_snapshot={
                "schema": "fin.daily-workspace-input-snapshot/v1",
                "trading_day_id": trading_day_id,
                "checkpoint": checkpoint,
            },
            expected_parent_product_version=expected_parent_product_version,
            status=(
                "completed" if product.get("consultation_status") == "completed" else "partial"
            ),
            product=product,
            now=now,
            create_delivery_obligation=True,
        )
        obligation = self._ensure_obligation(
            read=read,
            presentation_hash=presentation_hash,
            now=now,
        )
        return DailyWorkspaceFinalization(read=read, obligation=obligation)

    def claim_delivery(
        self,
        *,
        workspace_ref: str,
        product_version: int,
        claimed_at: float,
        presentation_hash: str,
    ) -> DailyDeliveryClaim:
        """Transition one obligation PENDING→CLAIMED (exactly one attempt).

        Binds the exact rendered-message ``presentation_hash`` at claim time:
        delivery renders the message after the product exists, so the
        obligation always matches the message actually sent (codex P1:
        presentation binding must be the real sent message, not a precomputed
        product hash).  Generates an opaque claim token that must be presented
        to ``settle_delivery`` (claim fencing).  Raises
        ``daily_delivery_obligation_missing`` for unknown workspaces and
        ``daily_delivery_obligation_not_pending`` for already claimed/settled.
        """

        _require_non_empty(workspace_ref, "workspace_ref")
        _require_non_empty(presentation_hash, "presentation_hash")
        if (
            not isinstance(product_version, int)
            or isinstance(product_version, bool)
            or product_version < 1
        ):
            raise ValueError("product_version is invalid")
        _require_finite(claimed_at, "claimed_at")
        claim_token = secrets.token_hex(32)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT workspace_ref, product_version, artifact_hash, state
                FROM daily_workspace_obligations
                WHERE workspace_ref=? AND product_version=?
                """,
                (workspace_ref, product_version),
            ).fetchone()
            if row is None:
                raise SemanticStateError("daily_delivery_obligation_missing")
            if str(row["state"]) != "PENDING":
                raise SemanticStateError("daily_delivery_obligation_not_pending")
            connection.execute(
                """
                UPDATE daily_workspace_obligations
                SET state='CLAIMED', claim_token=?, claimed_at=?,
                    presentation_hash=?, updated_at=?
                WHERE workspace_ref=? AND product_version=?
                """,
                (
                    claim_token,
                    claimed_at,
                    presentation_hash,
                    claimed_at,
                    workspace_ref,
                    product_version,
                ),
            )
            return DailyDeliveryClaim(
                workspace_ref=workspace_ref,
                product_version=product_version,
                presentation_hash=presentation_hash,
                claimed_at=claimed_at,
                claim_token=claim_token,
            )

    def settle_delivery(
        self,
        *,
        workspace_ref: str,
        product_version: int,
        settlement: str,
        settled_at: float,
        claim_token: str,
    ) -> None:
        """Transition one obligation CLAIMED→terminal (or PENDING) with an
        explicit outcome.

        ``settlement`` ∈ {POSITIVE_ACK, EXPLICIT_NOT_SENT, OUTCOME_UNKNOWN}.
        - POSITIVE_ACK / OUTCOME_UNKNOWN: terminal SETTLED — never auto-resend.
        - EXPLICIT_NOT_SENT: obligation returns to PENDING so a later retry
          may re-claim and send the same immutable message.

        ``claim_token`` fences the settlement: a late ACK presenting a stale
        token (from a superseded claim) is rejected with
        ``daily_delivery_claim_token_mismatch``.  Replaying the exact same
        terminal settlement with the same token is idempotent, so an outbox
        can recover after its semantic write committed but before its local
        binding was finalized.
        """

        _require_non_empty(workspace_ref, "workspace_ref")
        _require_non_empty(claim_token, "claim_token")
        if settlement not in {"POSITIVE_ACK", "EXPLICIT_NOT_SENT", "OUTCOME_UNKNOWN"}:
            raise ValueError("unsupported delivery settlement")
        _require_finite(settled_at, "settled_at")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT state, claim_token, settlement FROM daily_workspace_obligations
                WHERE workspace_ref=? AND product_version=?
                """,
                (workspace_ref, product_version),
            ).fetchone()
            if row is None:
                raise SemanticStateError("daily_delivery_obligation_missing")
            state = str(row["state"])
            if state == "SETTLED":
                if not hmac.compare_digest(str(row["claim_token"]), claim_token):
                    raise SemanticStateError("daily_delivery_claim_token_mismatch")
                if str(row["settlement"]) == settlement:
                    return
                raise SemanticStateError("daily_delivery_obligation_settlement_conflict")
            if state != "CLAIMED":
                raise SemanticStateError("daily_delivery_obligation_not_claimed")
            if not hmac.compare_digest(str(row["claim_token"]), claim_token):
                raise SemanticStateError("daily_delivery_claim_token_mismatch")
            if settlement == "EXPLICIT_NOT_SENT":
                # Not sent is not a terminal outcome: allow a later retry to
                # claim again (delivery of the same immutable message).  All
                # claim/settlement fields are cleared to restore a clean
                # PENDING state (no stale settlement residue).
                connection.execute(
                    """
                    UPDATE daily_workspace_obligations
                    SET state='PENDING', claim_token=NULL, claimed_at=NULL,
                        settlement=NULL, settled_at=NULL,
                        presentation_hash=NULL, updated_at=?
                    WHERE workspace_ref=? AND product_version=?
                    """,
                    (settled_at, workspace_ref, product_version),
                )
                return
            connection.execute(
                """
                UPDATE daily_workspace_obligations
                SET state='SETTLED', settlement=?, settled_at=?, updated_at=?
                WHERE workspace_ref=? AND product_version=?
                """,
                (settlement, settled_at, settled_at, workspace_ref, product_version),
            )

    def append_run_ledger(
        self,
        *,
        run_id: str,
        trading_day_id: str,
        checkpoint: str,
        trigger: str,
        started_at: float,
        completed_at: float,
        stage_statuses: str,
        collect_identity: str,
    ) -> None:
        """B2: persist one run ledger row (idempotent on run_id).

        Same payload replay returns silently; a different payload for the same
        run_id raises ``run_ledger_conflict`` instead of silently overwriting
        (a run_id is generated once per invocation and must stay immutable).
        ``stage_statuses`` is a bounded JSON array of per-stage typed facts;
        ``collect_identity`` is the bounded upstream receipt identity — both
        are recorded verbatim, never re-derived.
        """

        _require_non_empty(run_id, "run_id")
        _require_non_empty(trading_day_id, "trading_day_id")
        if checkpoint not in {"premarket", "morning", "close", "postmarket"}:
            raise ValueError("checkpoint is invalid")
        if trigger not in {"manual", "schedule", "recovery"}:
            raise ValueError("trigger is invalid")
        _require_finite(started_at, "started_at")
        _require_finite(completed_at, "completed_at")
        if completed_at < started_at:
            raise ValueError("completed_at must not precede started_at")
        # stage_statuses 是 bounded JSON 数组（每 stage 一行 typed 事实），逐字规范化
        # 后存库；collect_identity 是 JSON 对象（house _canonical_object 语义）。
        try:
            parsed_stages = json.loads(stage_statuses)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("stage_statuses must be a valid JSON array") from exc
        if not isinstance(parsed_stages, list) or not all(
            isinstance(stage, dict) for stage in parsed_stages
        ):
            raise ValueError("stage_statuses must be a JSON array of objects")
        canonical_stages = json.dumps(
            parsed_stages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        canonical_identity, _unused = _canonical_object(collect_identity, "collect_identity")
        now = completed_at
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT trading_day_id, checkpoint, trigger, started_at, "
                "completed_at, stage_statuses, collect_identity "
                "FROM daily_workspace_run_ledger WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                # R3: 幂等 = 完整 payload 逐字段一致（run 事实不可变，任何差异都
                # 是 conflict，绝不静默覆盖）。
                same_payload = (
                    str(existing[0]) == trading_day_id
                    and str(existing[1]) == checkpoint
                    and str(existing[2]) == trigger
                    and float(existing[3]) == started_at
                    and float(existing[4]) == completed_at
                    # R3: bytes 比较——compare_digest(str) 对非 ASCII 抛 TypeError，
                    # stage/detail 可含中文 reason_code。
                    and hmac.compare_digest(
                        str(existing[5]).encode("utf-8"),
                        canonical_stages.encode("utf-8"),
                    )
                    and hmac.compare_digest(
                        str(existing[6]).encode("utf-8"),
                        canonical_identity.encode("utf-8"),
                    )
                )
                if same_payload:
                    return
                raise SemanticStateError("run_ledger_conflict")
            connection.execute(
                """
                INSERT INTO daily_workspace_run_ledger(
                    run_id, trading_day_id, checkpoint, trigger, started_at,
                    completed_at, stage_statuses, collect_identity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    trading_day_id,
                    checkpoint,
                    trigger,
                    started_at,
                    completed_at,
                    canonical_stages,
                    canonical_identity,
                    now,
                ),
            )

    def last_run_ledger_success(self, trading_day_id: str) -> str | None:
        """B2 read projection: run_id of the MOST RECENT all-ok, non-degraded run.

        Scans ALL rows of the trading day (most-recent-first; the day filter is
        the natural bound — only `recent_run_ledger` is LIMIT-bounded) until
        the first run whose stage closed set is complete (COLLECT_READY/PARTIAL
        + PREPARED + DELIVERED/ALREADY_DELIVERED) with no degraded stage — a
        later failure never erases an earlier success.
        """

        _require_non_empty(trading_day_id, "trading_day_id")
        # 冻结只对 `recent` 有界；last_success 扫当日全部行（WHERE trading_day_id
        # 过滤天然有界）——100 个失败后仍能找到最近一次成功。
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT run_id, stage_statuses
                FROM daily_workspace_run_ledger
                WHERE trading_day_id = ?
                ORDER BY completed_at DESC, run_id DESC
                """,
                (trading_day_id,),
            )
            for row in rows:
                if _run_ledger_stage_closed_set_ok(str(row["stage_statuses"])):
                    return str(row["run_id"])
        finally:
            connection.close()
        return None

    def latest_run_ledger_freshness(self, trading_day_id: str) -> str | None:
        """B2 read projection: 最新 run 的 G freshness——只在真实采集时呈现。

        仅当最新 run 的 collect identity 带有 succeeded/partial 的真实采集
        （run_status）且 receipt 声明 g_freshness 时返回该值；no_change 的
        receipt 可能携带 prior-assessment 的 FRESH（无新 capture），旧成功记录
        不代表当前新鲜——两者均返回 None，绝不因 no_change 或旧记录标记 fresh。
        """

        _require_non_empty(trading_day_id, "trading_day_id")
        snapshot = self.latest_run_ledger_snapshot(trading_day_id)
        return None if snapshot is None else snapshot[1]

    def latest_run_ledger_snapshot(
        self,
        trading_day_id: str,
    ) -> tuple[sqlite3.Row, str | None] | None:
        """B4 read projection: 最新 run 行与其 freshness 的**同一只读快照**。

        一次查询同时返回最新行与从该行 collect_identity 派生的 freshness——
        避免两次独立查询之间 B1 追加新 run 导致 stage/freshness 跨 run 混配。
        """

        _require_non_empty(trading_day_id, "trading_day_id")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT run_id, trading_day_id, checkpoint, trigger, started_at,
                       completed_at, stage_statuses, collect_identity, created_at
                FROM daily_workspace_run_ledger
                WHERE trading_day_id = ?
                ORDER BY completed_at DESC, run_id DESC
                LIMIT 1
                """,
                (trading_day_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        try:
            identity = json.loads(str(row["collect_identity"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise SemanticStateError("semantic_state_corrupt") from None
        if not isinstance(identity, dict):
            raise SemanticStateError("semantic_state_corrupt")
        freshness: str | None = None
        # B slice：capture 链已验证后，ingest 的 no_change 也是真实采集（窗口重采集 +
        # G 重发布）——纳入 allowlist；旧规则的前提「ZSXQ 未验证采集源」已由用户
        # 指令推翻。
        if str(identity.get("run_status", "")) in {"succeeded", "partial", "no_change"}:
            candidate = identity.get("g_freshness")
            if isinstance(candidate, str) and candidate:
                if candidate not in {"FRESH", "STALE", "UNKNOWN"}:
                    raise SemanticStateError("semantic_state_corrupt")
                freshness = candidate
        return row, freshness

    def recent_run_ledger(
        self,
        trading_day_id: str,
        *,
        limit: int,
    ) -> list[sqlite3.Row]:
        """B2 read projection: bounded most-recent-first run rows (read-only)."""

        _require_non_empty(trading_day_id, "trading_day_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        connection = self._connect()
        try:
            return list(
                connection.execute(
                    """
                    SELECT run_id, trading_day_id, checkpoint, trigger, started_at,
                           completed_at, stage_statuses, collect_identity, created_at
                    FROM daily_workspace_run_ledger
                    WHERE trading_day_id = ?
                    ORDER BY completed_at DESC, run_id DESC
                    LIMIT ?
                    """,
                    (trading_day_id, limit),
                )
            )
        finally:
            connection.close()

    def _ensure_obligation(
        self,
        *,
        read: DailyWorkspaceRead,
        presentation_hash: str | None,
        now: float,
    ) -> DailyWorkspaceObligation:
        """Create (or return the existing) PENDING obligation for one version.

        Runs inside the caller's transaction when called from a public seam
        that already holds ``_transaction()``; this helper is called from
        ``finalize_scheduled_checkpoint`` *after* ``append_daily_workspace_version``
        committed, so it opens its own transaction.  To keep product + obligation
        atomic, public callers must use ``finalize_scheduled_checkpoint`` (which
        calls append inside the same repository); obligation creation is
        idempotent on ``(workspace_ref, product_version)`` so a replay cannot
        duplicate it.
        """

        # PENDING obligation carries no presentation_hash: the exact rendered
        # message hash is bound at claim time (delivery renders after the
        # product exists), so the obligation always matches what is sent.
        # No product-JSON fallback hash (codex P1-2).
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT workspace_ref, product_version, artifact_hash,
                       presentation_hash, state, claimed_at, settled_at,
                       settlement, created_at
                FROM daily_workspace_obligations
                WHERE workspace_ref=? AND product_version=?
                """,
                (read.workspace_ref, read.product_version),
            ).fetchone()
            if existing is not None:
                return DailyWorkspaceObligation(
                    workspace_ref=str(existing["workspace_ref"]),
                    product_version=int(existing["product_version"]),
                    artifact_hash=str(existing["artifact_hash"]),
                    presentation_hash=(
                        str(existing["presentation_hash"])
                        if existing["presentation_hash"] is not None
                        else None
                    ),
                    state=str(existing["state"]),
                    claimed_at=(
                        float(existing["claimed_at"])
                        if existing["claimed_at"] is not None
                        else None
                    ),
                    settled_at=(
                        float(existing["settled_at"])
                        if existing["settled_at"] is not None
                        else None
                    ),
                    settlement=existing["settlement"],
                    created_at=float(existing["created_at"]),
                )
            connection.execute(
                """
                INSERT INTO daily_workspace_obligations(
                    workspace_ref, product_version, artifact_hash,
                    presentation_hash, state, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, 'PENDING', ?, ?)
                """,
                (
                    read.workspace_ref,
                    read.product_version,
                    read.artifact_hash,
                    now,
                    now,
                ),
            )
            return DailyWorkspaceObligation(
                workspace_ref=read.workspace_ref,
                product_version=read.product_version,
                artifact_hash=read.artifact_hash,
                presentation_hash=None,
                state="PENDING",
                created_at=now,
            )

    def find_daily_workspace_version_by_key(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
    ) -> DailyWorkspaceRead | None:
        """Return the exact version already admitted for one idempotency key.

        Scheduled checkpoint retries reuse the same key; this read makes the
        retry idempotent without re-running generation or drifting the input
        snapshot.
        """

        _require_non_empty(principal_id, "principal_id")
        _require_non_empty(idempotency_key, "idempotency_key")
        key_hash = _hash_text(idempotency_key)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT product_id FROM idempotency
                WHERE principal_id = ? AND capability = ? AND key_hash = ?
                """,
                (principal_id, "daily_workspace", key_hash),
            ).fetchone()
            if row is None or row["product_id"] is None:
                return None
            chain = self._resolve_daily_chain(
                connection,
                principal_id=principal_id,
                trading_day_id=trading_day_id,
            )
            if chain is None:
                return None
            return self._latest_daily_workspace(
                connection,
                principal_id=principal_id,
                trading_day_id=trading_day_id,
                chain_id=str(chain["chain_id"]),
                product_id=str(row["product_id"]),
            )
        finally:
            connection.close()

    def acquire_daily_workspace_checkpoint(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        now: float,
    ) -> bool:
        """Atomically claim generation for one checkpoint key.

        Exactly one caller wins; losers return False and must report
        generation-in-progress instead of duplicating the generator run.
        The claim is a chain-only idempotency row bound to the product on
        append, or released on generation failure.
        """

        self._validate_identity(principal_id, idempotency_key)
        _require_non_empty(trading_day_id, "trading_day_id")
        _require_finite(now, "now")
        key_hash = _hash_text(idempotency_key)
        request_hash = _hash_text(f"daily_workspace\x00{trading_day_id}\x00{idempotency_key}")
        with self._transaction() as connection:
            chain = self._resolve_daily_chain(
                connection,
                principal_id=principal_id,
                trading_day_id=trading_day_id,
            )
            if chain is None:
                raise SemanticStateError("daily_workspace_chain_missing")
            try:
                connection.execute(
                    """
                    INSERT INTO idempotency(
                        principal_id, capability, key_hash, request_hash,
                        chain_id, job_id, product_id, created_at
                    ) VALUES (?, 'daily_workspace', ?, ?, ?, NULL, NULL, ?)
                    """,
                    (principal_id, key_hash, request_hash, str(chain["chain_id"]), now),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def release_daily_workspace_checkpoint(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
    ) -> None:
        """Drop an unbound checkpoint claim after generation failure."""

        self._validate_identity(principal_id, idempotency_key)
        _require_non_empty(trading_day_id, "trading_day_id")
        key_hash = _hash_text(idempotency_key)
        with self._transaction() as connection:
            connection.execute(
                """
                DELETE FROM idempotency
                WHERE principal_id = ? AND capability = 'daily_workspace'
                  AND key_hash = ? AND job_id IS NULL AND product_id IS NULL
                """,
                (principal_id, key_hash),
            )

    def _latest_daily_workspace(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        trading_day_id: str,
        chain_id: str,
        product_id: str | None = None,
    ) -> DailyWorkspaceRead:
        """Read one version inside the current transaction (no open).

        Defaults to the latest; ``product_id`` pins the exact replayed
        version for idempotent appends.
        """

        if product_id is not None:
            row = connection.execute(
                """
                SELECT product_version, status, product_json, artifact_hash, created_at
                FROM products
                WHERE chain_id = ? AND product_id = ?
                """,
                (chain_id, product_id),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT product_version, status, product_json, artifact_hash, created_at
                FROM products
                WHERE chain_id = ?
                ORDER BY product_version DESC
                LIMIT 1
                """,
                (chain_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - version was just written
            raise SemanticStateError("daily_workspace_read_missing")
        product = _load_json_object(str(row["product_json"]))
        _, product_hash = _canonical_object(product, "product")
        if f"sha256:{product_hash}" != str(row["artifact_hash"]):
            raise SemanticStateError("daily_workspace_artifact_hash_mismatch")
        return DailyWorkspaceRead(
            chain_id=chain_id,
            workspace_ref=self._workspace_ref_for(principal_id, chain_id),
            trading_day_id=trading_day_id,
            product_version=int(row["product_version"]),
            status=str(row["status"]),
            product=product,
            artifact_hash=str(row["artifact_hash"]),
            as_of=float(row["created_at"]),
            created_at=float(row["created_at"]),
        )

    # -- research admission --------------------------------------------

    def admit_research(
        self,
        *,
        principal_id: str,
        idempotency_key: str,
        contract: Mapping[str, object] | str,
        input_snapshot: object,
        deadline_at: float,
        now: float,
    ) -> ResearchAdmission:
        """Atomically create a chain, continuation, queued job and pending version."""

        self._validate_identity(principal_id, idempotency_key)
        self._validate_time(deadline_at, now)
        contract_json, contract_hash = _canonical_object(contract, "contract")
        input_json, input_hash = _canonical_object(input_snapshot, "input_snapshot")
        request_hash = _hash_text(f"{contract_json}\x00{input_json}")
        key_hash = _hash_text(idempotency_key)

        with self._transaction() as connection:
            replay = self._resolve_idempotency(
                connection,
                principal_id=principal_id,
                key_hash=key_hash,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay

            # The continuation is derived from this random chain identity.  A
            # 32-byte seed keeps its effective entropy at the required 256 bits;
            # UUID4 would cap it below that even though HMAC-SHA256 is 32 bytes.
            chain_id = secrets.token_hex(32)
            job_id = uuid4().hex
            continuation_token = self._token_for(principal_id, chain_id)
            token_hash = _hash_text(continuation_token)
            connection.execute(
                """
                INSERT INTO chains(chain_id, principal_id, status, created_at, updated_at)
                VALUES (?, ?, 'active', ?, ?)
                """,
                (chain_id, principal_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO continuations(
                    token_hash, epoch, principal_id, chain_id, active_job_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (token_hash, self._epoch, principal_id, chain_id, now, now),
            )
            self._insert_job(
                connection,
                job_id=job_id,
                chain_id=chain_id,
                principal_id=principal_id,
                contract_json=contract_json,
                input_json=input_json,
                contract_hash=contract_hash,
                input_hash=input_hash,
                request_hash=request_hash,
                deadline_at=deadline_at,
                now=now,
            )
            connection.execute(
                "UPDATE continuations SET active_job_id=?, updated_at=? WHERE chain_id=?",
                (job_id, now, chain_id),
            )
            self._append_version(
                connection,
                chain_id=chain_id,
                kind="research_pending",
                job_id=job_id,
                product_id=None,
                contract_json=contract_json,
                input_json=input_json,
                contract_hash=contract_hash,
                input_hash=input_hash,
                payload={"status": "queued"},
                now=now,
            )
            connection.execute(
                """
                INSERT INTO idempotency(
                    principal_id, capability, key_hash, request_hash,
                    chain_id, job_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    principal_id,
                    CAPABILITY,
                    key_hash,
                    request_hash,
                    chain_id,
                    job_id,
                    now,
                ),
            )
            return ResearchAdmission(chain_id, job_id, continuation_token)

    def continue_research(
        self,
        *,
        principal_id: str,
        continuation_token: str,
        idempotency_key: str,
        contract: Mapping[str, object] | str,
        input_snapshot: object,
        deadline_at: float,
        now: float,
    ) -> ResearchAdmission:
        """Atomically queue another explicit research run on an active idle chain."""

        self._validate_identity(principal_id, idempotency_key)
        self._validate_time(deadline_at, now)
        contract_json, contract_hash = _canonical_object(contract, "contract")
        input_json, input_hash = _canonical_object(input_snapshot, "input_snapshot")
        request_hash = _hash_text(f"{contract_json}\x00{input_json}")
        key_hash = _hash_text(idempotency_key)
        with self._transaction() as connection:
            chain = self._resolve_chain(
                connection,
                principal_id=principal_id,
                continuation_token=continuation_token,
            )
            replay = self._resolve_idempotency(
                connection,
                principal_id=principal_id,
                key_hash=key_hash,
                request_hash=request_hash,
            )
            if replay is not None:
                if replay.chain_id != str(chain["chain_id"]):
                    raise SemanticStateError("idempotency_conflict")
                return replay
            if str(chain["status"]) == "closed":
                raise SemanticStateError("chain_closed")
            if chain["active_job_id"] is not None:
                raise SemanticStateError("research_in_progress")

            chain_id = str(chain["chain_id"])
            job_id = uuid4().hex
            self._insert_job(
                connection,
                job_id=job_id,
                chain_id=chain_id,
                principal_id=principal_id,
                contract_json=contract_json,
                input_json=input_json,
                contract_hash=contract_hash,
                input_hash=input_hash,
                request_hash=request_hash,
                deadline_at=deadline_at,
                now=now,
            )
            connection.execute(
                "UPDATE continuations SET active_job_id=?, updated_at=? WHERE chain_id=?",
                (job_id, now, chain_id),
            )
            connection.execute(
                "UPDATE chains SET updated_at=? WHERE chain_id=?",
                (now, chain_id),
            )
            self._append_version(
                connection,
                chain_id=chain_id,
                kind="research_pending",
                job_id=job_id,
                product_id=None,
                contract_json=contract_json,
                input_json=input_json,
                contract_hash=contract_hash,
                input_hash=input_hash,
                payload={"status": "queued"},
                now=now,
            )
            connection.execute(
                """
                INSERT INTO idempotency(
                    principal_id, capability, key_hash, request_hash,
                    chain_id, job_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    principal_id,
                    CAPABILITY,
                    key_hash,
                    request_hash,
                    chain_id,
                    job_id,
                    now,
                ),
            )
            return ResearchAdmission(chain_id, job_id, continuation_token)

    # -- worker lease lifecycle ----------------------------------------

    def claim_next(self, *, worker_id: str, now: float, lease_seconds: float) -> JobLease | None:
        """Claim one queued or expired-running job with a fresh fencing token."""

        _require_non_empty(worker_id, "worker_id")
        _require_finite(now, "now")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive and finite")
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state='timed_out', fencing_token=fencing_token + 1,
                    lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                    updated_at=?
                WHERE state IN ('queued', 'running')
                  AND deadline_at <= ?
                  AND EXISTS (
                      SELECT 1 FROM chains
                      WHERE chains.chain_id=jobs.chain_id AND chains.status='active'
                  )
                """,
                (now, now),
            )
            row = connection.execute(
                """
                SELECT jobs.*
                FROM jobs
                JOIN chains ON chains.chain_id=jobs.chain_id
                WHERE chains.status='active'
                  AND jobs.deadline_at > ?
                  AND (
                      jobs.state='queued'
                      OR (jobs.state='running' AND jobs.lease_expires_at <= ?)
                  )
                ORDER BY CASE jobs.state WHEN 'queued' THEN 0 ELSE 1 END, jobs.seq
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            lease_expires_at = min(now + lease_seconds, float(row["deadline_at"]))
            attempt = int(row["attempt"]) + 1
            fencing_token = int(row["fencing_token"]) + 1
            connection.execute(
                """
                UPDATE jobs
                SET state='running', attempt=?, fencing_token=?, lease_owner=?,
                    lease_expires_at=?, heartbeat_at=?, updated_at=?
                WHERE job_id=?
                """,
                (
                    attempt,
                    fencing_token,
                    worker_id,
                    lease_expires_at,
                    now,
                    now,
                    str(row["job_id"]),
                ),
            )
            return _lease_from_row(
                row,
                worker_id=worker_id,
                attempt=attempt,
                fencing_token=fencing_token,
                lease_expires_at=lease_expires_at,
            )

    def heartbeat(self, *, lease: JobLease, now: float, lease_seconds: float) -> JobLease:
        """Renew only the currently fenced, non-expired attempt."""

        _require_finite(now, "now")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive and finite")
        with self._transaction() as connection:
            row = self._require_live_lease(connection, lease=lease, now=now)
            lease_expires_at = min(now + lease_seconds, float(row["deadline_at"]))
            connection.execute(
                """
                UPDATE jobs
                SET lease_expires_at=?, heartbeat_at=?, updated_at=?
                WHERE job_id=?
                """,
                (lease_expires_at, now, now, lease.job_id),
            )
            return _lease_from_row(
                row,
                worker_id=lease.worker_id,
                attempt=lease.attempt,
                fencing_token=lease.fencing_token,
                lease_expires_at=lease_expires_at,
            )

    def finalize(
        self,
        *,
        lease: JobLease,
        status: str,
        product: Mapping[str, object] | None,
        artifact_hash: str | None,
        now: float,
    ) -> None:
        """Persist a fenced terminal candidate; public coordination happens on read."""

        if status not in _WORKER_TERMINAL_STATES:
            raise ValueError("unsupported terminal status")
        _require_finite(now, "now")
        product_json: str | None = None
        if status in _PRODUCT_STATES:
            if product is None or not artifact_hash:
                raise ValueError("successful or partial results require product and artifact hash")
            validate_persistable_product(product)
            product_json, product_hash = _canonical_object(product, "product")
            _require_product_artifact_hash(artifact_hash, product_hash=product_hash)
        elif product is not None or artifact_hash is not None:
            raise ValueError("failed or timed-out results cannot carry product identity")

        with self._transaction() as connection:
            self._require_live_lease(connection, lease=lease, now=now)
            connection.execute(
                """
                UPDATE jobs
                SET state=?, product_json=?, artifact_hash=?, lease_owner=NULL,
                    lease_expires_at=NULL, heartbeat_at=NULL, updated_at=?
                WHERE job_id=?
                """,
                (status, product_json, artifact_hash, now, lease.job_id),
            )

    # -- continuation command state table ------------------------------

    def read_guidance_snapshot(
        self,
        *,
        principal_id: str,
        continuation_token: str,
    ) -> StoredGuidanceSnapshot:
        """Read the latest published product and its exact immutable invocation.

        The opaque token is resolved only inside the caller's trusted principal
        scope.  Foreign and unknown tokens therefore share the same failure,
        while later failed attempts cannot replace the invocation that produced
        the last published product.
        """

        _require_non_empty(principal_id, "principal_id")
        connection = self._connect()
        try:
            chain = self._resolve_chain(
                connection,
                principal_id=principal_id,
                continuation_token=continuation_token,
            )
            product = connection.execute(
                """
                SELECT
                    product_id, job_id, product_version, status,
                    product_json, artifact_hash
                FROM products
                WHERE chain_id=?
                ORDER BY product_version DESC
                LIMIT 1
                """,
                (str(chain["chain_id"]),),
            ).fetchone()
            if product is None:
                raise SemanticStateError("semantic_state_corrupt")
            product_id = str(product["product_id"])
            job_id = product["job_id"]
            if job_id is None:
                version = connection.execute(
                    """
                    SELECT contract_json, input_json, contract_hash, input_hash
                    FROM chain_versions
                    WHERE chain_id=? AND product_id=? AND kind='answer'
                    """,
                    (str(chain["chain_id"]), product_id),
                ).fetchone()
            else:
                version = connection.execute(
                    """
                    SELECT
                        contract_json, input_json, contract_hash, input_hash,
                        state, product_json AS job_product_json,
                        artifact_hash AS job_artifact_hash
                    FROM jobs
                    WHERE chain_id=? AND job_id=?
                      AND EXISTS (
                          SELECT 1
                          FROM chain_versions
                          WHERE chain_versions.chain_id=jobs.chain_id
                            AND chain_versions.job_id=jobs.job_id
                            AND chain_versions.product_id=?
                            AND chain_versions.kind='research_terminal'
                      )
                    """,
                    (str(chain["chain_id"]), str(job_id), product_id),
                ).fetchone()
            if version is None:
                raise SemanticStateError("semantic_state_corrupt")
            try:
                contract_json, contract_hash = _canonical_object(
                    str(version["contract_json"]),
                    "stored contract",
                )
                input_json, input_hash = _canonical_object(
                    str(version["input_json"]),
                    "stored input snapshot",
                )
                if not hmac.compare_digest(contract_hash, str(version["contract_hash"])):
                    raise ValueError("stored contract hash mismatch")
                if not hmac.compare_digest(input_hash, str(version["input_hash"])):
                    raise ValueError("stored input hash mismatch")
                contract = load_resolved_contract(
                    cast(dict[str, object], json.loads(contract_json))
                )
                input_snapshot = load_input_snapshot(
                    cast(dict[str, object], json.loads(input_json))
                )
                if contract.input_snapshot_ref.snapshot_hash != input_snapshot.snapshot_hash:
                    raise ContractResolutionError("stored invocation references do not match")
                if job_id is not None and (
                    str(version["state"]) not in _PRODUCT_STATES
                    or str(product["status"]) != _public_status(str(version["state"]))
                    or version["job_product_json"] is None
                    or version["job_artifact_hash"] is None
                    or str(product["product_json"]) != str(version["job_product_json"])
                    or str(product["artifact_hash"]) != str(version["job_artifact_hash"])
                ):
                    raise ValueError("stored research product identity mismatch")
                stored_product = _load_verified_product(
                    str(product["product_json"]),
                    str(product["artifact_hash"]),
                )
            except (ContractResolutionError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise SemanticStateError("semantic_state_corrupt") from error
            return StoredGuidanceSnapshot(
                contract=contract,
                input_snapshot=input_snapshot,
                product=stored_product,
                artifact_hash=str(product["artifact_hash"]),
                product_version=int(product["product_version"]),
            )
        finally:
            connection.close()

    def read_prior_question(
        self,
        *,
        principal_id: str,
        continuation_token: str,
        now: float,
    ) -> str | None:
        """Best-effort original question of the prior turn for the guide.

        Read-only; any failure returns None and never blocks the follow-up.
        """
        try:
            _require_non_empty(principal_id, "principal_id")
            _require_finite(now, "now")
            with self._transaction() as connection:
                chain = self._resolve_chain(
                    connection,
                    principal_id=principal_id,
                    continuation_token=continuation_token,
                )
                chain_id = str(chain["chain_id"])
                # 原问题 = 链上第一条 answer/研究 job 的输入问题（非最近一轮）。
                latest = connection.execute(
                    """
                    SELECT job_id FROM chain_versions
                    WHERE chain_id=?
                      AND kind IN ('answer', 'research_pending', 'research_terminal')
                    ORDER BY version_no ASC
                    LIMIT 1
                    """,
                    (chain_id,),
                ).fetchone()
                if latest is None or latest["job_id"] is None:
                    return None
                job = connection.execute(
                    "SELECT input_json FROM jobs WHERE job_id=?",
                    (str(latest["job_id"]),),
                ).fetchone()
                if job is None:
                    return None
                payload = json.loads(str(job["input_json"]) or "{}")
                question = payload.get("question") if isinstance(payload, dict) else None
                if isinstance(question, str) and question.strip():
                    return question.strip()[:800]
                return None
        except (SemanticStateError, sqlite3.Error, json.JSONDecodeError, ValueError):
            return None

    def read(
        self,
        *,
        principal_id: str,
        continuation_token: str,
        now: float,
    ) -> ResearchRead:
        """Read and, if terminal, exactly-once coordinate a stable product version."""

        _require_non_empty(principal_id, "principal_id")
        _require_finite(now, "now")
        with self._transaction() as connection:
            chain = self._resolve_chain(
                connection,
                principal_id=principal_id,
                continuation_token=continuation_token,
            )
            chain_id = str(chain["chain_id"])
            if str(chain["status"]) == "closed":
                return self._closed_read(connection, chain_id)
            if chain["active_job_id"] is not None:
                job = connection.execute(
                    "SELECT * FROM jobs WHERE job_id=?",
                    (str(chain["active_job_id"]),),
                ).fetchone()
                if job is None:
                    raise SemanticStateError("semantic_state_corrupt")
            else:
                latest = connection.execute(
                    """
                    SELECT kind, job_id, product_id
                    FROM chain_versions
                    WHERE chain_id=?
                      AND kind IN ('answer', 'research_pending', 'research_terminal')
                    ORDER BY version_no DESC
                    LIMIT 1
                    """,
                    (chain_id,),
                ).fetchone()
                if latest is None:
                    return ResearchRead("unavailable", chain_id, ("read", "close"))
                if str(latest["kind"]) == "answer":
                    if latest["product_id"] is None:
                        raise SemanticStateError("semantic_state_corrupt")
                    return self._read_answer_product(
                        connection,
                        chain_id=chain_id,
                        product_id=str(latest["product_id"]),
                    )
                if latest["job_id"] is None:
                    raise SemanticStateError("semantic_state_corrupt")
                job = connection.execute(
                    "SELECT * FROM jobs WHERE job_id=?",
                    (str(latest["job_id"]),),
                ).fetchone()
                if job is None:
                    raise SemanticStateError("semantic_state_corrupt")
            if job is None:
                return ResearchRead("unavailable", chain_id, ("read", "close"))
            state = str(job["state"])
            if state in {"queued", "running"}:
                return ResearchRead("queued", chain_id, ("read", "close"))
            if state in _TERMINAL_STATES and job["coordinated_version_no"] is None:
                self._coordinate_terminal(connection, job=job, now=now)
                refreshed = connection.execute(
                    "SELECT * FROM jobs WHERE job_id=?", (str(job["job_id"]),)
                ).fetchone()
                if refreshed is None:  # defensive corruption boundary
                    raise SemanticStateError("semantic_state_corrupt")
                job = refreshed
            return self._read_from_job(connection, job)

    def close(
        self,
        *,
        principal_id: str,
        continuation_token: str,
        now: float,
    ) -> ResearchRead:
        """Atomically cancel pending work, invalidate fencing and close a chain."""

        _require_non_empty(principal_id, "principal_id")
        _require_finite(now, "now")
        with self._transaction() as connection:
            chain = self._resolve_chain(
                connection,
                principal_id=principal_id,
                continuation_token=continuation_token,
            )
            chain_id = str(chain["chain_id"])
            if str(chain["status"]) == "closed":
                return self._closed_read(connection, chain_id)

            active_job_id = chain["active_job_id"]
            if active_job_id is not None:
                job = connection.execute(
                    "SELECT * FROM jobs WHERE job_id=?", (str(active_job_id),)
                ).fetchone()
                if job is None:
                    raise SemanticStateError("semantic_state_corrupt")
                state = str(job["state"])
                if state in {"queued", "running"}:
                    connection.execute(
                        """
                        UPDATE jobs
                        SET state='cancelled', fencing_token=fencing_token + 1,
                            lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                            updated_at=?
                        WHERE job_id=?
                        """,
                        (now, str(active_job_id)),
                    )
                elif state in _TERMINAL_STATES and job["coordinated_version_no"] is None:
                    self._coordinate_terminal(connection, job=job, now=now)

            closed_version = self._append_version(
                connection,
                chain_id=chain_id,
                kind="closed",
                job_id=str(active_job_id) if active_job_id is not None else None,
                product_id=None,
                payload={"status": "closed"},
                now=now,
            )
            if active_job_id is not None:
                connection.execute(
                    """
                    UPDATE jobs
                    SET coordinated_version_no=COALESCE(coordinated_version_no, ?)
                    WHERE job_id=?
                    """,
                    (closed_version, str(active_job_id)),
                )
            connection.execute(
                """
                UPDATE continuations
                SET active_job_id=NULL,
                    active_turn_id=NULL,
                    turn_lease_expires_at=NULL,
                    turn_fencing_token=turn_fencing_token + 1,
                    runtime_backend=NULL,
                    session_id=NULL,
                    identity_hash=NULL,
                    product_version=NULL,
                    updated_at=?
                WHERE chain_id=?
                """,
                (now, chain_id),
            )
            connection.execute(
                "UPDATE chains SET status='closed', updated_at=? WHERE chain_id=?",
                (now, chain_id),
            )
            return self._closed_read(connection, chain_id)

    def append_feedback(
        self,
        *,
        principal_id: str,
        continuation_token: str,
        product_version: int,
        feedback_key: str,
        disposition: str,
        note: str,
        now: float,
    ) -> FeedbackReceipt:
        """Append idempotent product metadata; this repository has no cognition writer."""

        _require_non_empty(principal_id, "principal_id")
        _require_non_empty(feedback_key, "feedback_key")
        _require_non_empty(disposition, "disposition")
        if product_version <= 0:
            raise ValueError("product_version must be positive")
        if len(note) > 4_000:
            raise ValueError("feedback note exceeds 4000 characters")
        _require_finite(now, "now")
        feedback_key_hash = _hash_text(feedback_key)
        with self._transaction() as connection:
            chain = self._resolve_chain(
                connection,
                principal_id=principal_id,
                continuation_token=continuation_token,
            )
            chain_id = str(chain["chain_id"])
            product = connection.execute(
                """
                SELECT product_id, product_json, artifact_hash
                FROM products WHERE chain_id=? AND product_version=?
                """,
                (chain_id, product_version),
            ).fetchone()
            if product is None:
                raise SemanticStateError("product_version_not_found")
            _load_verified_product(
                str(product["product_json"]),
                str(product["artifact_hash"]),
            )
            existing = connection.execute(
                """
                SELECT feedback_id, disposition, note, product_version
                FROM feedback
                WHERE chain_id=? AND feedback_key_hash=?
                """,
                (chain_id, feedback_key_hash),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["disposition"]) != disposition
                    or str(existing["note"]) != note
                    or int(existing["product_version"]) != product_version
                ):
                    raise SemanticStateError("feedback_conflict")
                return FeedbackReceipt(str(existing["feedback_id"]), chain_id, product_version)

            feedback_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO feedback(
                    feedback_id, chain_id, product_version, feedback_key_hash,
                    disposition, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    chain_id,
                    product_version,
                    feedback_key_hash,
                    disposition,
                    note,
                    now,
                ),
            )
            self._append_version(
                connection,
                chain_id=chain_id,
                kind="feedback",
                job_id=None,
                product_id=str(product["product_id"]),
                payload={"disposition": disposition, "product_version": product_version},
                now=now,
            )
            return FeedbackReceipt(feedback_id, chain_id, product_version)

    # -- cross-generation investment memory ----------------------------

    def append_investment_memory_event(
        self,
        *,
        principal_id: str,
        event_key: str,
        event: InvestmentMemoryEventInput,
        account_ref: AccountReference | None = None,
        now: float,
        route_key: str | None = None,
        route_generation: str | None = None,
        route_expected_revision: int | None = None,
    ) -> InvestmentMemoryReceipt:
        """Append one explicit user fact without creating a second state owner.

        The journal is deliberately separate from generic ``feedback``: feedback
        evaluates a displayed product, while this method records only a typed,
        user-stated decision, execution report, or outcome.  The analysis
        reference is derived from the repository's own latest consultation
        product; callers cannot supply or copy an analysis body.
        """

        self._validate_identity(principal_id, event_key)
        if not isinstance(event, InvestmentMemoryEventInput):
            raise ValueError("investment memory event is invalid")
        if account_ref is not None and not isinstance(account_ref, AccountReference):
            raise ValueError("investment memory account reference is invalid")
        self._validate_route_generation_fence(
            route_key=route_key,
            route_generation=route_generation,
            route_expected_revision=route_expected_revision,
        )
        _require_finite(now, "investment memory now")
        event_key_hash = _hash_text(event_key)
        payload_hash = _investment_memory_input_hash(event)
        with self._transaction() as connection:
            if route_key is not None and route_generation is not None:
                self._fence_route_generation(
                    connection,
                    route_key=route_key,
                    route_generation=route_generation,
                    route_expected_revision=route_expected_revision,
                )
            existing = connection.execute(
                """
                SELECT * FROM investment_memory_events
                WHERE principal_id=? AND event_key_hash=?
                """,
                (principal_id, event_key_hash),
            ).fetchone()
            if existing is not None:
                if str(
                    existing["kind"]
                ) not in _INVESTMENT_MEMORY_EVENT_KINDS or not hmac.compare_digest(
                    str(existing["payload_hash"]), payload_hash
                ):
                    raise SemanticStateError("investment_memory_conflict")
                return InvestmentMemoryReceipt(
                    event=self._investment_memory_event_from_row(connection, existing),
                    state=self._investment_memory_event_state(connection, existing),
                )

            analysis_ref = self._latest_investment_memory_analysis_ref(
                connection,
                principal_id=principal_id,
            )
            self._validate_investment_memory_related_events(
                connection,
                principal_id=principal_id,
                event_ids=event.related_event_ids,
            )
            event_id = uuid4().hex
            payload_json, _unused_hash = _canonical_object(
                {
                    "schema_version": "fin.investment-memory-event/v1",
                    "statement": event.statement,
                    "related_event_ids": event.related_event_ids,
                },
                "investment memory payload",
            )
            connection.execute(
                """
                INSERT INTO investment_memory_events(
                    event_id, principal_id, event_key_hash, kind, target_event_id,
                    analysis_chain_id, analysis_product_version,
                    account_snapshot_ref, account_revision, account_as_of,
                    decision, payload_json, payload_hash, created_at, purged_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    event_id,
                    principal_id,
                    event_key_hash,
                    event.kind,
                    analysis_ref.chain_id if analysis_ref is not None else None,
                    analysis_ref.product_version if analysis_ref is not None else None,
                    account_ref.snapshot_ref if account_ref is not None else None,
                    account_ref.revision if account_ref is not None else None,
                    account_ref.as_of if account_ref is not None else None,
                    event.decision,
                    payload_json,
                    payload_hash,
                    now,
                ),
            )
            if event.supersedes_event_id is not None:
                self._append_investment_memory_relation(
                    connection,
                    principal_id=principal_id,
                    relation_key=_hash_text(f"{event_key}\x00supersedes"),
                    kind="SUPERSEDES",
                    target_event_id=event.supersedes_event_id,
                    now=now,
                )
            row = connection.execute(
                "SELECT * FROM investment_memory_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - INSERT/SELECT same transaction
                raise SemanticStateError("semantic_state_corrupt")
            stored = self._investment_memory_event_from_row(connection, row)
            return InvestmentMemoryReceipt(event=stored, state="ACTIVE")

    def tombstone_investment_memory(
        self,
        *,
        principal_id: str,
        deletion_key: str,
        target_event_id: str | None = None,
        now: float,
        route_key: str | None = None,
        route_generation: str | None = None,
        route_expected_revision: int | None = None,
    ) -> str:
        """Immediately revoke one event (or all prior events) from recall.

        ``target_event_id=None`` creates a principal-scoped tombstone for every
        event already in the journal.  It is intentionally append-only: later
        janitor work may erase the statement payload, but the tombstone/key
        identity remains so a delayed idempotent replay cannot resurrect it.
        """

        self._validate_identity(principal_id, deletion_key)
        if target_event_id is not None and not _valid_memory_event_id(target_event_id):
            raise ValueError("investment memory target_event_id is invalid")
        self._validate_route_generation_fence(
            route_key=route_key,
            route_generation=route_generation,
            route_expected_revision=route_expected_revision,
        )
        _require_finite(now, "investment memory now")
        with self._transaction() as connection:
            if route_key is not None and route_generation is not None:
                self._fence_route_generation(
                    connection,
                    route_key=route_key,
                    route_generation=route_generation,
                    route_expected_revision=route_expected_revision,
                )
            return self._append_investment_memory_relation(
                connection,
                principal_id=principal_id,
                relation_key=deletion_key,
                kind="TOMBSTONE",
                target_event_id=target_event_id,
                now=now,
            )

    @staticmethod
    def _validate_route_generation_fence(
        *,
        route_key: str | None,
        route_generation: str | None,
        route_expected_revision: int | None,
    ) -> None:
        if route_key is None:
            if route_generation is not None or route_expected_revision is not None:
                raise ValueError("route generation fence is incomplete")
            return
        _require_non_empty(route_key, "route_key")
        if route_generation is None:
            raise ValueError("route generation fence is incomplete")
        _require_non_empty(route_generation, "route_generation")
        if route_expected_revision is not None and (
            not isinstance(route_expected_revision, int)
            or isinstance(route_expected_revision, bool)
            or route_expected_revision < 1
        ):
            raise ValueError("route_expected_revision is invalid")

    def _fence_route_generation(
        self,
        connection: sqlite3.Connection,
        *,
        route_key: str,
        route_generation: str,
        route_expected_revision: int | None,
    ) -> tuple[str, str, int, tuple[str, ...]] | None:
        """Recheck a transport generation in one caller-owned transaction."""

        route = self._load_valid_conversation_route(connection, route_key=route_key)
        if route_expected_revision is None:
            if route is not None:
                raise SemanticStateError("route_revision_conflict")
            return None
        if route is None:
            raise SemanticStateError("route_revision_conflict")
        active_generation, _chain_id, active_revision, seen = route
        if route_generation in seen and route_generation != active_generation:
            raise SemanticStateError("continuation_not_accessible")
        if active_revision != route_expected_revision:
            raise SemanticStateError("route_revision_conflict")
        return active_generation, _chain_id, active_revision, seen

    def _require_active_route_generation(
        self,
        connection: sqlite3.Connection,
        *,
        route_key: str,
        route_generation: str,
        route_expected_revision: int,
        chain_id: str,
    ) -> None:
        route = self._fence_route_generation(
            connection,
            route_key=route_key,
            route_generation=route_generation,
            route_expected_revision=route_expected_revision,
        )
        if route is None:  # pragma: no cover - positive revision requires a row
            raise SemanticStateError("route_revision_conflict")
        active_generation, active_chain_id, _revision, _seen = route
        if active_generation != route_generation or active_chain_id != chain_id:
            raise SemanticStateError("route_revision_conflict")

    def recall_investment_memory(
        self,
        *,
        principal_id: str,
    ) -> InvestmentMemoryRecall:
        """Read at most eight active non-evidence user-memory records.

        There is no keyword router, vector store, transcript replay, or
        cross-chain resume here.  The Agent receives only recent structured
        projections and remains free to use, question, or ignore them.
        """

        _require_non_empty(principal_id, "principal_id")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT event.*
                FROM investment_memory_events AS event
                WHERE event.principal_id=?
                  AND event.kind IN (
                      'USER_DECISION', 'USER_REPORTED_EXECUTION',
                      'OUTCOME_OBSERVATION', 'OUTCOME_JUDGMENT'
                  )
                  AND event.purged_at IS NULL
                  AND event.seq > COALESCE((
                      SELECT MAX(scope_tombstone.seq)
                      FROM investment_memory_events AS scope_tombstone
                      WHERE scope_tombstone.principal_id=event.principal_id
                        AND scope_tombstone.kind='TOMBSTONE'
                        AND scope_tombstone.target_event_id IS NULL
                  ), 0)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM investment_memory_events AS relation
                      WHERE relation.principal_id=event.principal_id
                        AND relation.target_event_id=event.event_id
                        AND relation.kind IN ('SUPERSEDES', 'TOMBSTONE')
                  )
                ORDER BY event.seq DESC
                LIMIT 8
                """,
                (principal_id,),
            ).fetchall()
            events = tuple(self._investment_memory_event_from_row(connection, row) for row in rows)
            unresolved = tuple(
                event
                for event in events
                if event.kind == "USER_DECISION" and event.decision in {"WAIT", "CHANGE_PLAN"}
            )
            reported_execution = tuple(
                event for event in events if event.kind == "USER_REPORTED_EXECUTION"
            )
            outcomes = tuple(
                event
                for event in events
                if event.kind in {"OUTCOME_OBSERVATION", "OUTCOME_JUDGMENT"}
            )
            account_refs = _unique_account_references(event.account_ref for event in events)
            analyses = _unique_analysis_references(event.analysis_ref for event in events)
            return InvestmentMemoryRecall(
                schema_version="fin.investment-memory-recall/v1",
                classification="investment_memory_not_evidence",
                unresolved_decisions=unresolved,
                reported_execution=reported_execution,
                outcomes=outcomes,
                account_refs=account_refs,
                recent_analyses=analyses,
            )
        finally:
            connection.close()

    def purge_tombstoned_investment_memory(
        self,
        *,
        now: float,
        retention_seconds: float,
    ) -> int:
        """Erase eligible journal statements while retaining tombstone identity.

        SQLite page/WAL/backup byte erasure is deliberately outside this method;
        callers must describe and operate their store-specific retention window.
        This mutation removes the readable journal statement and preserves only
        enough identity to stop a stale replay from recreating it.
        """

        _require_finite(now, "investment memory janitor now")
        _require_finite(retention_seconds, "investment memory retention")
        if retention_seconds < 0:
            raise ValueError("investment memory retention must be non-negative")
        cutoff = now - retention_seconds
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT event.event_id, event.payload_json
                FROM investment_memory_events AS event
                WHERE event.kind IN (
                      'USER_DECISION', 'USER_REPORTED_EXECUTION',
                      'OUTCOME_OBSERVATION', 'OUTCOME_JUDGMENT'
                  )
                  AND event.purged_at IS NULL
                  AND event.created_at <= ?
                  AND (
                      event.seq <= COALESCE((
                          SELECT MAX(scope_tombstone.seq)
                          FROM investment_memory_events AS scope_tombstone
                          WHERE scope_tombstone.principal_id=event.principal_id
                            AND scope_tombstone.kind='TOMBSTONE'
                            AND scope_tombstone.target_event_id IS NULL
                      ), 0)
                      OR EXISTS (
                          SELECT 1
                          FROM investment_memory_events AS relation
                          WHERE relation.principal_id=event.principal_id
                            AND relation.target_event_id=event.event_id
                            AND relation.kind IN ('SUPERSEDES', 'TOMBSTONE')
                      )
                  )
                """,
                (cutoff,),
            ).fetchall()
            if not rows:
                return 0
            redacted_rows: list[tuple[str, float, str]] = []
            for row in rows:
                payload = _load_json_object(str(row["payload_json"]))
                related_event_ids = _investment_memory_related_event_ids_from_payload(payload)
                redacted_json, _unused_hash = _canonical_object(
                    {
                        "schema_version": "fin.investment-memory-event/v1",
                        "redacted": True,
                        "related_event_ids": related_event_ids,
                    },
                    "redacted investment memory payload",
                )
                redacted_rows.append((redacted_json, now, str(row["event_id"])))
            connection.executemany(
                """
                UPDATE investment_memory_events
                SET payload_json=?, purged_at=?
                WHERE event_id=? AND purged_at IS NULL
                """,
                redacted_rows,
            )
            return len(rows)

    def _latest_investment_memory_analysis_ref(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
    ) -> AnalysisReference | None:
        row = connection.execute(
            """
            SELECT products.chain_id, products.product_version, products.product_json,
                   products.artifact_hash
            FROM products
            JOIN chains ON chains.chain_id=products.chain_id
            WHERE chains.principal_id=? AND chains.chain_kind='consultation'
            ORDER BY products.created_at DESC, products.product_version DESC
            LIMIT 1
            """,
            (principal_id,),
        ).fetchone()
        if row is None:
            return None
        _load_verified_product(str(row["product_json"]), str(row["artifact_hash"]))
        return AnalysisReference(
            chain_id=str(row["chain_id"]),
            product_version=int(row["product_version"]),
            artifact_hash=str(row["artifact_hash"]),
        )

    def _investment_memory_event_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> InvestmentMemoryEvent:
        kind = str(row["kind"])
        if kind not in _INVESTMENT_MEMORY_EVENT_KINDS:
            raise SemanticStateError("semantic_state_corrupt")
        payload = _load_json_object(str(row["payload_json"]))
        if payload.get("schema_version") != "fin.investment-memory-event/v1":
            raise SemanticStateError("semantic_state_corrupt")
        related_event_ids = _investment_memory_related_event_ids_from_payload(payload)
        purged_at = row["purged_at"]
        if purged_at is not None:
            _require_finite(float(purged_at), "stored investment memory purged_at")
            if payload.get("redacted") is not True:
                raise SemanticStateError("semantic_state_corrupt")
            statement: str | None = None
        else:
            if not isinstance(payload.get("statement"), str):
                raise SemanticStateError("semantic_state_corrupt")
            statement = str(payload["statement"])
        analysis_ref = self._investment_memory_analysis_ref_from_row(connection, row)
        account_ref = _investment_memory_account_ref_from_row(row)
        try:
            return InvestmentMemoryEvent(
                event_id=str(row["event_id"]),
                kind=cast(InvestmentMemoryEventKind, kind),
                statement=statement,
                decision=cast(InvestmentMemoryDecision | None, row["decision"]),
                analysis_ref=analysis_ref,
                account_ref=account_ref,
                created_at=float(row["created_at"]),
                state=self._investment_memory_event_state(connection, row),
                related_event_ids=related_event_ids,
            )
        except (TypeError, ValueError) as error:
            raise SemanticStateError("semantic_state_corrupt") from error

    def _validate_investment_memory_related_events(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        event_ids: tuple[str, ...],
    ) -> None:
        """Keep outcome references inside the principal's original journal facts."""

        for event_id in event_ids:
            row = connection.execute(
                """
                SELECT kind
                FROM investment_memory_events
                WHERE principal_id=? AND event_id=?
                """,
                (principal_id, event_id),
            ).fetchone()
            if row is None or str(row["kind"]) not in _INVESTMENT_MEMORY_EVENT_KINDS:
                raise SemanticStateError("investment_memory_not_found")

    def _investment_memory_analysis_ref_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> AnalysisReference | None:
        chain_id = row["analysis_chain_id"]
        version = row["analysis_product_version"]
        if chain_id is None and version is None:
            return None
        if chain_id is None or version is None:
            raise SemanticStateError("semantic_state_corrupt")
        product = connection.execute(
            """
            SELECT products.artifact_hash, products.product_json, chains.principal_id
            FROM products
            JOIN chains ON chains.chain_id=products.chain_id
            WHERE products.chain_id=? AND products.product_version=?
            """,
            (str(chain_id), int(version)),
        ).fetchone()
        if product is None or str(product["principal_id"]) != str(row["principal_id"]):
            raise SemanticStateError("semantic_state_corrupt")
        _load_verified_product(str(product["product_json"]), str(product["artifact_hash"]))
        try:
            return AnalysisReference(
                chain_id=str(chain_id),
                product_version=int(version),
                artifact_hash=str(product["artifact_hash"]),
            )
        except (TypeError, ValueError) as error:
            raise SemanticStateError("semantic_state_corrupt") from error

    def _investment_memory_event_state(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> InvestmentMemoryEventState:
        global_tombstone = connection.execute(
            """
            SELECT MAX(seq)
            FROM investment_memory_events
            WHERE principal_id=? AND kind='TOMBSTONE' AND target_event_id IS NULL
            """,
            (str(row["principal_id"]),),
        ).fetchone()[0]
        if global_tombstone is not None and int(row["seq"]) <= int(global_tombstone):
            return "TOMBSTONED"
        relation = connection.execute(
            """
            SELECT kind
            FROM investment_memory_events
            WHERE principal_id=? AND target_event_id=? AND kind IN ('SUPERSEDES', 'TOMBSTONE')
            ORDER BY seq DESC
            LIMIT 1
            """,
            (str(row["principal_id"]), str(row["event_id"])),
        ).fetchone()
        if relation is not None:
            return "TOMBSTONED" if str(relation["kind"]) == "TOMBSTONE" else "SUPERSEDED"
        return "PURGED" if row["purged_at"] is not None else "ACTIVE"

    def _append_investment_memory_relation(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        relation_key: str,
        kind: str,
        target_event_id: str | None,
        now: float,
    ) -> str:
        if kind not in _INVESTMENT_MEMORY_RELATION_KINDS:
            raise ValueError("investment memory relation kind is invalid")
        self._validate_identity(principal_id, relation_key)
        if target_event_id is not None:
            target = connection.execute(
                """
                SELECT event_id, kind
                FROM investment_memory_events
                WHERE principal_id=? AND event_id=?
                """,
                (principal_id, target_event_id),
            ).fetchone()
            if target is None or str(target["kind"]) not in _INVESTMENT_MEMORY_EVENT_KINDS:
                raise SemanticStateError("investment_memory_not_found")
            scope_payload: dict[str, object] = {"scope": "event"}
        elif kind == "SUPERSEDES":
            raise ValueError("supersedes relation requires a target")
        else:
            target_seq = int(
                connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM investment_memory_events WHERE principal_id=?",
                    (principal_id,),
                ).fetchone()[0]
            )
            scope_payload = {"scope": "all_before", "target_seq": target_seq}
        key_hash = _hash_text(relation_key)
        payload_json, payload_hash = _canonical_object(
            {
                "schema_version": "fin.investment-memory-relation/v1",
                "kind": kind,
                "target_event_id": target_event_id,
                **scope_payload,
            },
            "investment memory relation payload",
        )
        existing = connection.execute(
            """
            SELECT event_id, kind, target_event_id, payload_hash
            FROM investment_memory_events
            WHERE principal_id=? AND event_key_hash=?
            """,
            (principal_id, key_hash),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["kind"]) != kind
                or existing["target_event_id"] != target_event_id
                or not hmac.compare_digest(str(existing["payload_hash"]), payload_hash)
            ):
                raise SemanticStateError("investment_memory_conflict")
            return str(existing["event_id"])
        relation_id = uuid4().hex
        connection.execute(
            """
            INSERT INTO investment_memory_events(
                event_id, principal_id, event_key_hash, kind, target_event_id,
                analysis_chain_id, analysis_product_version,
                account_snapshot_ref, account_revision, account_as_of,
                decision, payload_json, payload_hash, created_at, purged_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, NULL)
            """,
            (
                relation_id,
                principal_id,
                key_hash,
                kind,
                target_event_id,
                payload_json,
                payload_hash,
                now,
            ),
        )
        return relation_id

    # -- read-only diagnostics -----------------------------------------

    def counts(self) -> StateCounts:
        """Return application fact counts without changing state."""

        connection = self._connect()
        try:
            values = [
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "chains",
                    "continuations",
                    "jobs",
                    "chain_versions",
                    "products",
                    "feedback",
                )
            ]
            return StateCounts(*values)
        finally:
            connection.close()

    def terminal_reconciliation_snapshot(self) -> TerminalReconciliationSnapshot:
        """Return retained terminal coordination facts without changing state."""

        connection = self._connect()
        try:
            return _terminal_reconciliation_snapshot(connection, observed_at=time.time())
        finally:
            connection.close()

    def reconcile_terminal_jobs(self, *, now: float) -> TerminalReconciliationSnapshot:
        """Coordinate every retained terminal job exactly once."""

        _require_finite(now, "now")
        with self._transaction() as connection:
            pending = connection.execute("""
                SELECT *
                FROM jobs
                WHERE state IN ('succeeded', 'partial', 'failed', 'timed_out', 'cancelled')
                  AND coordinated_version_no IS NULL
                ORDER BY seq
                """).fetchall()
            for job in pending:
                self._coordinate_terminal(connection, job=job, now=now)
            return _terminal_reconciliation_snapshot(
                connection,
                observed_at=now,
                reconciled_now=len(pending),
            )

    def get_job(self, job_id: str) -> JobRecord:
        """Return a sanitized job projection for worker/audit inspection."""

        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise SemanticStateError("job_not_found")
            return _job_from_row(row)
        finally:
            connection.close()

    def table_names(self) -> tuple[str, ...]:
        """Expose the owned table inventory for schema health checks."""

        connection = self._connect()
        try:
            rows = connection.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT GLOB 'sqlite_*'
                    ORDER BY name
                    """).fetchall()
            return tuple(str(row["name"]) for row in rows)
        finally:
            connection.close()

    # -- internal transaction helpers ----------------------------------

    def _insert_job(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        chain_id: str,
        principal_id: str,
        contract_json: str,
        input_json: str,
        contract_hash: str,
        input_hash: str,
        request_hash: str,
        deadline_at: float,
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, chain_id, principal_id, state, contract_json, input_json,
                contract_hash, input_hash, request_hash, deadline_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                chain_id,
                principal_id,
                contract_json,
                input_json,
                contract_hash,
                input_hash,
                request_hash,
                deadline_at,
                now,
                now,
            ),
        )

    def _resolve_idempotency(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        key_hash: str,
        request_hash: str,
    ) -> ResearchAdmission | None:
        row = connection.execute(
            """
            SELECT request_hash, chain_id, job_id, product_id
            FROM idempotency
            WHERE principal_id=? AND capability=? AND key_hash=?
            """,
            (principal_id, CAPABILITY, key_hash),
        ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(str(row["request_hash"]), request_hash):
            raise SemanticStateError("idempotency_conflict")
        if row["job_id"] is None or row["product_id"] is not None:
            raise SemanticStateError("idempotency_conflict")
        chain_id = str(row["chain_id"])
        return ResearchAdmission(
            chain_id,
            str(row["job_id"]),
            self._token_for(principal_id, chain_id),
        )

    def _resolve_answer_idempotency(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        key_hash: str,
        request_hash: str,
    ) -> AnswerWrite | None:
        row = connection.execute(
            """
            SELECT request_hash, chain_id, job_id, product_id
            FROM idempotency
            WHERE principal_id=? AND capability=? AND key_hash=?
            """,
            (principal_id, CAPABILITY, key_hash),
        ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(str(row["request_hash"]), request_hash):
            raise SemanticStateError("idempotency_conflict")
        if row["job_id"] is not None or row["product_id"] is None:
            raise SemanticStateError("idempotency_conflict")
        product = connection.execute(
            """
            SELECT
                products.product_id, products.chain_id, products.product_version,
                products.status, products.product_json, products.artifact_hash,
                chain_versions.payload_json
            FROM products
            JOIN chain_versions
              ON chain_versions.product_id=products.product_id
             AND chain_versions.kind='answer'
            WHERE products.product_id=?
            """,
            (str(row["product_id"]),),
        ).fetchone()
        if product is None or str(product["chain_id"]) != str(row["chain_id"]):
            raise SemanticStateError("semantic_state_corrupt")
        response_projection = _load_answer_response_projection(str(product["payload_json"]))
        chain_id = str(product["chain_id"])
        return AnswerWrite(
            chain_id=chain_id,
            product_id=str(product["product_id"]),
            continuation_token=self._token_for(principal_id, chain_id),
            product_version=int(product["product_version"]),
            status=str(product["status"]),
            product=_load_verified_product(
                str(product["product_json"]),
                str(product["artifact_hash"]),
            ),
            artifact_hash=str(product["artifact_hash"]),
            as_of=cast(float, response_projection["as_of"]),
            data_gaps=cast(tuple[str, ...], response_projection["data_gaps"]),
            provenance=cast(dict[str, object], response_projection["provenance"]),
            replayed=True,
            continuity_degraded=cast(bool, response_projection["continuity_degraded"]),
        )

    def find_answer_replay_bound(
        self,
        *,
        principal_id: str,
        idempotency_key: str,
        continuation_token: str | None = None,
        route_key: str | None = None,
        route_generation: str | None = None,
        route_expected_revision: int | None = None,
    ) -> tuple[AnswerWrite, str, str] | None:
        """按 key 返回绑定的原始 answer 及其存储 contract/input（JSON 字符串）。

        与 `find_answer_replay` 不同：不要求调用方提供匹配的 contract/input
        （链头推进后新 derive 的 input 必然不同），而是按 principal/key 原子
        读取该 key 绑定的原始 invocation——重试必须返回首次结果，不受链头
        推进影响。返回 (AnswerWrite, contract_json, input_json)，调用方用
        存储形态重建 invocation 并过 fence/reproof 后再发布。
        """

        self._validate_identity(principal_id, idempotency_key)
        self._validate_route_generation_fence(
            route_key=route_key,
            route_generation=route_generation,
            route_expected_revision=route_expected_revision,
        )
        if (
            continuation_token is not None
            and route_key is not None
            and route_expected_revision is None
        ):
            raise ValueError("continuation route revision is required")
        key_hash = _hash_text(idempotency_key)
        with self._transaction() as connection:
            chain: sqlite3.Row | None = None
            if continuation_token is not None:
                chain = self._resolve_chain(
                    connection,
                    principal_id=principal_id,
                    continuation_token=continuation_token,
                )
                if str(chain["status"]) == "closed":
                    raise SemanticStateError("chain_closed")
                if chain["active_job_id"] is not None:
                    raise SemanticStateError("research_in_progress")
                if (
                    route_key is not None
                    and route_generation is not None
                    and route_expected_revision is not None
                ):
                    self._require_active_route_generation(
                        connection,
                        route_key=route_key,
                        route_generation=route_generation,
                        route_expected_revision=route_expected_revision,
                        chain_id=str(chain["chain_id"]),
                    )
            row = connection.execute(
                """
                SELECT request_hash, chain_id, job_id, product_id
                FROM idempotency
                WHERE principal_id=? AND capability=? AND key_hash=?
                """,
                (principal_id, CAPABILITY, key_hash),
            ).fetchone()
            if row is None:
                if (
                    continuation_token is None
                    and route_key is not None
                    and route_generation is not None
                ):
                    self._fence_answer_route_admission(
                        connection,
                        route_key=route_key,
                        route_generation=route_generation,
                        route_expected_revision=route_expected_revision,
                    )
                return None
            # continuation token 与 idempotency key 必须绑定同一 chain——
            # 否则跨链泄漏（链 A token + 链 B key 不得返回链 B 的 product）。
            if continuation_token is not None:
                if chain is None:  # pragma: no cover - resolved above
                    raise SemanticStateError("semantic_state_corrupt")
                if str(row["chain_id"]) != str(chain["chain_id"]):
                    raise SemanticStateError("idempotency_conflict")
            if row["job_id"] is not None or row["product_id"] is None:
                raise SemanticStateError("idempotency_conflict")
            version = connection.execute(
                """
                SELECT contract_json, input_json
                FROM chain_versions
                WHERE product_id=? AND kind='answer'
                """,
                (str(row["product_id"]),),
            ).fetchone()
            if version is None:
                raise SemanticStateError("semantic_state_corrupt")
            contract_json, _ = _canonical_object(str(version["contract_json"]), "contract")
            input_json, _ = _canonical_object(str(version["input_json"]), "input_snapshot")
            if not hmac.compare_digest(
                _hash_text(f"{contract_json}\x00{input_json}"),
                str(row["request_hash"]),
            ):
                raise SemanticStateError("semantic_state_corrupt")
            product = connection.execute(
                """
                SELECT
                    products.product_id, products.chain_id, products.product_version,
                    products.status, products.product_json, products.artifact_hash,
                    chain_versions.payload_json
                FROM products
                JOIN chain_versions
                  ON chain_versions.product_id=products.product_id
                 AND chain_versions.kind='answer'
                WHERE products.product_id=?
                """,
                (str(row["product_id"]),),
            ).fetchone()
            if product is None or str(product["chain_id"]) != str(row["chain_id"]):
                raise SemanticStateError("semantic_state_corrupt")
            response_projection = _load_answer_response_projection(str(product["payload_json"]))
            chain_id = str(product["chain_id"])
            write = AnswerWrite(
                chain_id=chain_id,
                product_id=str(product["product_id"]),
                continuation_token=self._token_for(principal_id, chain_id),
                product_version=int(product["product_version"]),
                status=str(product["status"]),
                product=_load_verified_product(
                    str(product["product_json"]),
                    str(product["artifact_hash"]),
                ),
                artifact_hash=str(product["artifact_hash"]),
                as_of=cast(float, response_projection["as_of"]),
                data_gaps=cast(tuple[str, ...], response_projection["data_gaps"]),
                provenance=cast(dict[str, object], response_projection["provenance"]),
                replayed=True,
                continuity_degraded=cast(bool, response_projection["continuity_degraded"]),
            )
            if (
                continuation_token is None
                and route_key is not None
                and route_generation is not None
            ):
                self._fence_answer_route_replay(
                    connection,
                    route_key=route_key,
                    route_generation=route_generation,
                    route_expected_revision=route_expected_revision,
                    replay_chain_id=write.chain_id,
                )
            return write, contract_json, input_json

    def _resolve_chain(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        continuation_token: str,
    ) -> sqlite3.Row:
        _require_non_empty(continuation_token, "continuation_token")
        token_hash = _hash_text(continuation_token)
        row = connection.execute(
            """
            SELECT
                chains.chain_id, chains.status,
                continuations.active_job_id, continuations.epoch,
                continuations.active_turn_id,
                continuations.turn_lease_expires_at,
                continuations.turn_fencing_token,
                continuations.runtime_backend,
                continuations.session_id,
                continuations.identity_hash,
                continuations.product_version
            FROM continuations
            JOIN chains ON chains.chain_id=continuations.chain_id
            WHERE continuations.principal_id=? AND continuations.token_hash=?
            """,
            (principal_id, token_hash),
        ).fetchone()
        if row is None:
            raise SemanticStateError("continuation_not_accessible")
        if str(row["epoch"]) != self._epoch:
            raise SemanticStateError("continuation_epoch_unsupported")
        return cast(sqlite3.Row, row)

    def _token_for(self, principal_id: str, chain_id: str) -> str:
        digest = hmac.new(
            self._token_secret,
            f"{self._epoch}\x00{principal_id}\x00{chain_id}".encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _require_live_lease(
        self,
        connection: sqlite3.Connection,
        *,
        lease: JobLease,
        now: float,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT jobs.*, chains.status AS chain_status
            FROM jobs JOIN chains ON chains.chain_id=jobs.chain_id
            WHERE jobs.job_id=?
            """,
            (lease.job_id,),
        ).fetchone()
        if (
            row is None
            or str(row["state"]) != "running"
            or str(row["chain_status"]) != "active"
            or row["lease_owner"] is None
            or not hmac.compare_digest(
                str(row["lease_owner"]).encode("utf-8"),
                lease.worker_id.encode("utf-8"),
            )
            or int(row["attempt"]) != lease.attempt
            or int(row["fencing_token"]) != lease.fencing_token
            or row["lease_expires_at"] is None
            or float(row["lease_expires_at"]) <= now
            or float(row["deadline_at"]) <= now
        ):
            raise SemanticStateError("lease_lost")
        return cast(sqlite3.Row, row)

    def _latest_or_active_job(
        self, connection: sqlite3.Connection, chain: sqlite3.Row
    ) -> sqlite3.Row | None:
        if chain["active_job_id"] is not None:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM jobs WHERE job_id=?", (str(chain["active_job_id"]),)
                ).fetchone(),
            )
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM jobs WHERE chain_id=? ORDER BY seq DESC LIMIT 1",
                (str(chain["chain_id"]),),
            ).fetchone(),
        )

    def _coordinate_terminal(
        self,
        connection: sqlite3.Connection,
        *,
        job: sqlite3.Row,
        now: float,
    ) -> None:
        state = str(job["state"])
        if state not in _TERMINAL_STATES:
            raise SemanticStateError("semantic_state_corrupt")
        chain_id = str(job["chain_id"])
        job_id = str(job["job_id"])
        product_id: str | None = None
        product_version: int | None = None
        public_status = _public_status(state)
        if state in _PRODUCT_STATES:
            if job["product_json"] is None or job["artifact_hash"] is None:
                raise SemanticStateError("semantic_state_corrupt")
            _load_verified_product(str(job["product_json"]), str(job["artifact_hash"]))
            product_version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(product_version), 0) + 1 FROM products WHERE chain_id=?",
                    (chain_id,),
                ).fetchone()[0]
            )
            product_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO products(
                    product_id, chain_id, job_id, product_version, status,
                    product_json, artifact_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    chain_id,
                    job_id,
                    product_version,
                    public_status,
                    str(job["product_json"]),
                    str(job["artifact_hash"]),
                    now,
                ),
            )
        elif job["product_json"] is not None or job["artifact_hash"] is not None:
            raise SemanticStateError("semantic_state_corrupt")
        version_no = self._append_version(
            connection,
            chain_id=chain_id,
            kind="research_terminal",
            job_id=job_id,
            product_id=product_id,
            payload={"product_version": product_version, "status": public_status},
            now=now,
        )
        connection.execute(
            "UPDATE jobs SET coordinated_version_no=?, updated_at=? WHERE job_id=?",
            (version_no, now, job_id),
        )
        connection.execute(
            """
            UPDATE continuations
            SET active_job_id=NULL, updated_at=?
            WHERE chain_id=? AND active_job_id=?
            """,
            (now, chain_id, job_id),
        )

    def _read_from_job(self, connection: sqlite3.Connection, job: sqlite3.Row) -> ResearchRead:
        state = str(job["state"])
        chain_id = str(job["chain_id"])
        public_status = _public_status(state)
        product = connection.execute(
            """
            SELECT chain_id, product_version, status, product_json, artifact_hash
            FROM products WHERE job_id=?
            """,
            (str(job["job_id"]),),
        ).fetchone()
        if state in _PRODUCT_STATES:
            if (
                product is None
                or str(product["chain_id"]) != chain_id
                or str(product["status"]) != public_status
                or job["product_json"] is None
                or job["artifact_hash"] is None
                or str(product["product_json"]) != str(job["product_json"])
                or str(product["artifact_hash"]) != str(job["artifact_hash"])
            ):
                raise SemanticStateError("semantic_state_corrupt")
            return ResearchRead(
                public_status,
                chain_id,
                ("read", "continue", "close", "feedback"),
                int(product["product_version"]),
                _load_verified_product(
                    str(product["product_json"]),
                    str(product["artifact_hash"]),
                ),
                str(product["artifact_hash"]),
            )
        if (
            product is not None
            or job["product_json"] is not None
            or job["artifact_hash"] is not None
        ):
            raise SemanticStateError("semantic_state_corrupt")
        allowed = ("read", "continue", "close")
        problem = None
        if state == "failed":
            problem = "research_failed"
        elif state == "timed_out":
            problem = "research_timeout"
        elif state == "cancelled":
            problem = "research_cancelled"
        return ResearchRead(public_status, chain_id, allowed, problem=problem)

    def _read_answer_product(
        self,
        connection: sqlite3.Connection,
        *,
        chain_id: str,
        product_id: str,
    ) -> ResearchRead:
        product = connection.execute(
            """
            SELECT product_version, status, product_json, artifact_hash, created_at
            FROM products WHERE product_id=? AND chain_id=?
            """,
            (product_id, chain_id),
        ).fetchone()
        if product is None:
            raise SemanticStateError("semantic_state_corrupt")
        handle = self._runtime_handle_of(connection, chain_id=chain_id)
        return ResearchRead(
            str(product["status"]),
            chain_id,
            ("read", "continue", "close", "feedback"),
            int(product["product_version"]),
            _load_verified_product(
                str(product["product_json"]),
                str(product["artifact_hash"]),
            ),
            str(product["artifact_hash"]),
            product_created_at=float(product["created_at"]),
            runtime_handle=handle,
        )

    def _runtime_handle_of(
        self,
        connection: sqlite3.Connection,
        *,
        chain_id: str,
    ) -> dict[str, object] | None:
        """从 continuations 读取 provider-private 会话 handle（有界投影）。

        handle 只含 backend/session_id/identity_hash/product_version，不含
        transcript/prompt/credential；任一新列缺失即视为无 handle。
        """
        row = connection.execute(
            """
            SELECT runtime_backend, session_id, identity_hash, product_version
            FROM continuations WHERE chain_id=?
            """,
            (chain_id,),
        ).fetchone()
        if row is None:
            raise SemanticStateError("semantic_state_corrupt")
        backend = row["runtime_backend"]
        session_id = row["session_id"]
        identity_hash = row["identity_hash"]
        version = row["product_version"]
        if backend is None or session_id is None or identity_hash is None or version is None:
            return None
        return {
            "backend": str(backend),
            "session_id": str(session_id),
            "identity_hash": str(identity_hash),
            "product_version": int(version),
        }

    def _closed_read(self, connection: sqlite3.Connection, chain_id: str) -> ResearchRead:
        product = connection.execute(
            """
            SELECT product_version, product_json, artifact_hash
            FROM products WHERE chain_id=? ORDER BY product_version DESC LIMIT 1
            """,
            (chain_id,),
        ).fetchone()
        if product is None:
            return ResearchRead("closed", chain_id, ("read",))
        return ResearchRead(
            "closed",
            chain_id,
            ("read", "feedback"),
            int(product["product_version"]),
            _load_verified_product(
                str(product["product_json"]),
                str(product["artifact_hash"]),
            ),
            str(product["artifact_hash"]),
        )

    def _append_version(
        self,
        connection: sqlite3.Connection,
        *,
        chain_id: str,
        kind: str,
        job_id: str | None,
        product_id: str | None,
        contract_json: str | None = None,
        input_json: str | None = None,
        contract_hash: str | None = None,
        input_hash: str | None = None,
        payload: Mapping[str, object],
        now: float,
    ) -> int:
        snapshot_fields = (contract_json, input_json, contract_hash, input_hash)
        if any(value is not None for value in snapshot_fields) and not all(
            value is not None for value in snapshot_fields
        ):
            raise SemanticStateError("semantic_state_corrupt")
        version_no = int(
            connection.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM chain_versions WHERE chain_id=?",
                (chain_id,),
            ).fetchone()[0]
        )
        payload_json, _unused_hash = _canonical_object(payload, "version payload")
        connection.execute(
            """
            INSERT INTO chain_versions(
                chain_id, version_no, kind, job_id, product_id,
                contract_json, input_json, contract_hash, input_hash,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chain_id,
                version_no,
                kind,
                job_id,
                product_id,
                contract_json,
                input_json,
                contract_hash,
                input_hash,
                payload_json,
                now,
            ),
        )
        return version_no

    @staticmethod
    def _validate_identity(principal_id: str, idempotency_key: str) -> None:
        _require_non_empty(principal_id, "principal_id")
        _require_non_empty(idempotency_key, "idempotency_key")
        if len(idempotency_key) > 512:
            raise ValueError("idempotency_key exceeds 512 characters")

    @staticmethod
    def _validate_time(deadline_at: float, now: float) -> None:
        _require_finite(deadline_at, "deadline_at")
        _require_finite(now, "now")
        if deadline_at <= now:
            raise ValueError("deadline_at must be after now")

    @staticmethod
    def _validate_answer(
        *,
        status: str,
        product: Mapping[str, object],
        artifact_hash: str,
        now: float,
    ) -> None:
        if status not in {"completed", "partial"}:
            raise ValueError("unsupported answer status")
        _require_non_empty(artifact_hash, "artifact_hash")
        _require_finite(now, "now")
        validate_persistable_product(product)


def _canonical_object(value: object, label: str) -> tuple[str, str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value, parse_constant=lambda item: _reject_json_constant(item))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} must be valid JSON") from exc
    elif isinstance(value, Mapping):
        decoded = dict(value)
    else:
        raise ValueError(f"{label} must be a JSON object or JSON object string")
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise ValueError(f"{label} must be a JSON object with string keys")
    try:
        canonical = json.dumps(
            decoded,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc
    return canonical, _hash_text(canonical)


def canonical_product_artifact_hash(payload: object) -> str:
    """Return the repository-owned canonical identity for one persisted product."""

    _canonical, digest = _canonical_object(payload, "product")
    return f"sha256:{digest}"


def _answer_response_projection(
    *,
    as_of: float,
    data_gaps: tuple[str, ...],
    provenance: Mapping[str, object] | None,
    continuity_degraded: bool = False,
) -> dict[str, object]:
    """Freeze the replayable public fields; current advice is intentionally absent."""

    _require_finite(as_of, "answer response as_of")
    if not all(isinstance(gap, str) for gap in data_gaps):
        raise ValueError("answer response data_gaps must contain strings")
    if not isinstance(continuity_degraded, bool):
        raise ValueError("answer response continuity_degraded must be a boolean")
    provenance_json, _unused_hash = _canonical_object(
        provenance or {},
        "answer response provenance",
    )
    return {
        "as_of": float(as_of),
        "data_gaps": tuple(data_gaps),
        "provenance": _load_json_object(provenance_json),
        "continuity_degraded": continuity_degraded,
    }


def _load_answer_response_projection(payload_json: str) -> dict[str, object]:
    payload = _load_json_object(payload_json)
    raw_projection = payload.get("response_projection")
    if not isinstance(raw_projection, dict):
        raise SemanticStateError("semantic_state_corrupt")
    as_of = raw_projection.get("as_of")
    raw_data_gaps = raw_projection.get("data_gaps")
    raw_provenance = raw_projection.get("provenance")
    raw_continuity_degraded = raw_projection.get("continuity_degraded")
    if (
        not isinstance(as_of, (int, float))
        or isinstance(as_of, bool)
        or not math.isfinite(as_of)
        or not isinstance(raw_data_gaps, list)
        or not all(isinstance(gap, str) for gap in raw_data_gaps)
        or not isinstance(raw_provenance, dict)
        or not all(isinstance(key, str) for key in raw_provenance)
        # A2: 缺字段或非严格 boolean 一律 fail closed，不得静默当作正常连续。
        or not isinstance(raw_continuity_degraded, bool)
    ):
        raise SemanticStateError("semantic_state_corrupt")
    return {
        "as_of": float(as_of),
        "data_gaps": tuple(raw_data_gaps),
        "provenance": cast(dict[str, object], raw_provenance),
        "continuity_degraded": raw_continuity_degraded,
    }


def _reject_json_constant(item: str) -> object:
    raise ValueError(f"non-finite JSON constant {item!r}")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _valid_memory_event_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _investment_memory_input_hash(event: InvestmentMemoryEventInput) -> str:
    _payload, digest = _canonical_object(
        {
            "kind": event.kind,
            "statement": event.statement,
            "decision": event.decision,
            "supersedes_event_id": event.supersedes_event_id,
            "related_event_ids": event.related_event_ids,
        },
        "investment memory input",
    )
    return digest


def _investment_memory_related_event_ids_from_payload(
    payload: Mapping[str, object],
) -> tuple[str, ...]:
    raw_ids = payload.get("related_event_ids", [])
    if not isinstance(raw_ids, list) or len(raw_ids) > 3:
        raise SemanticStateError("semantic_state_corrupt")
    event_ids = tuple(raw_ids)
    if any(not _valid_memory_event_id(event_id) for event_id in event_ids) or len(
        set(event_ids)
    ) != len(event_ids):
        raise SemanticStateError("semantic_state_corrupt")
    return cast(tuple[str, ...], event_ids)


def _investment_memory_account_ref_from_row(row: sqlite3.Row) -> AccountReference | None:
    snapshot_ref = row["account_snapshot_ref"]
    revision = row["account_revision"]
    as_of = row["account_as_of"]
    if snapshot_ref is None and revision is None and as_of is None:
        return None
    if snapshot_ref is None or revision is None or as_of is None:
        raise SemanticStateError("semantic_state_corrupt")
    try:
        return AccountReference(
            snapshot_ref=str(snapshot_ref),
            revision=str(revision),
            as_of=float(as_of),
        )
    except (TypeError, ValueError) as error:
        raise SemanticStateError("semantic_state_corrupt") from error


def _unique_account_references(
    values: Iterator[AccountReference | None],
) -> tuple[AccountReference, ...]:
    unique: list[AccountReference] = []
    for value in values:
        if value is not None and value not in unique:
            unique.append(value)
        if len(unique) == 3:
            break
    return tuple(unique)


def _unique_analysis_references(
    values: Iterator[AnalysisReference | None],
) -> tuple[AnalysisReference, ...]:
    unique: list[AnalysisReference] = []
    for value in values:
        if value is not None and value not in unique:
            unique.append(value)
        if len(unique) == 8:
            break
    return tuple(unique)


def _daily_workspace_timing_sample(
    product: Mapping[str, object],
    *,
    trading_day_id: str,
    checkpoint: str,
) -> DailyWorkspaceTimingSample | None:
    """Project one persisted canonical checkpoint into timing-only facts."""

    if (
        product.get("schema_version") != "fin.daily_workspace_product/v1"
        or product.get("origin") != "scheduled"
        or product.get("trading_day_id") != trading_day_id
        or product.get("checkpoint") != checkpoint
    ):
        return None
    provenance = product.get("agent_provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("runtime_invoked_at_generation") is not True
    ):
        return None
    degraded = product.get("degraded")
    if not isinstance(degraded, bool):
        return None
    timing = product.get("delivery_timing")
    if (
        not isinstance(timing, Mapping)
        or timing.get("schema") != "fin.daily-workspace-delivery-timing/v1"
    ):
        return None
    target_at = _parse_daily_workspace_timing(timing.get("target_at"))
    prepared_at = _parse_daily_workspace_timing(timing.get("prepared_at"))
    generated_at = _parse_daily_workspace_timing(timing.get("generated_at"))
    if (
        target_at is None
        or prepared_at is None
        or generated_at is None
        or generated_at < prepared_at
        or any(
            value.date().isoformat() != trading_day_id
            for value in (target_at, prepared_at, generated_at)
        )
    ):
        return None
    return DailyWorkspaceTimingSample(
        trading_day_id=trading_day_id,
        checkpoint=checkpoint,
        target_at=target_at,
        prepared_at=prepared_at,
        generated_at=generated_at,
        degraded=degraded,
        agent_runtime_invoked=True,
    )


def _parse_daily_workspace_timing(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        return None
    return parsed


def _daily_workspace_timing_sample_from_snapshot(
    value: object,
    *,
    checkpoint: str,
) -> DailyWorkspaceTimingSample | None:
    expected_keys = {
        "trading_day_id",
        "checkpoint",
        "target_at",
        "prepared_at",
        "generated_at",
        "degraded",
        "agent_runtime_invoked",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        return None
    trading_day_id = value.get("trading_day_id")
    degraded = value.get("degraded")
    if (
        not isinstance(trading_day_id, str)
        or value.get("checkpoint") != checkpoint
        or not isinstance(degraded, bool)
        or value.get("agent_runtime_invoked") is not True
    ):
        return None
    target_at = _parse_daily_workspace_timing(value.get("target_at"))
    prepared_at = _parse_daily_workspace_timing(value.get("prepared_at"))
    generated_at = _parse_daily_workspace_timing(value.get("generated_at"))
    if (
        target_at is None
        or prepared_at is None
        or generated_at is None
        or generated_at < prepared_at
        or any(
            item.date().isoformat() != trading_day_id
            for item in (target_at, prepared_at, generated_at)
        )
    ):
        return None
    return DailyWorkspaceTimingSample(
        trading_day_id=trading_day_id,
        checkpoint=checkpoint,
        target_at=target_at,
        prepared_at=prepared_at,
        generated_at=generated_at,
        degraded=degraded,
        agent_runtime_invoked=True,
    )


def _bind_daily_workspace_product(
    product: Mapping[str, object],
    *,
    workspace_ref: str,
    trading_day_id: str,
    product_version: int,
    parent_product_version: int,
) -> dict[str, object]:
    """Bind FIN-owned daily workspace identity and reject internal identity.

    ``workspace_ref``/``trading_day_id``/version fields are injected (or
    verified when the caller already set them); chain/token internals are
    forbidden so the persisted product never leaks repository identity.
    """

    # Local import avoids the semantic-state ↔ consultation package import
    # cycle while keeping this owner as the mandatory write boundary.
    from fin_analyse.consultation.daily_workspace_product_contracts import (
        is_public_daily_workspace_product,
    )

    if not is_public_daily_workspace_product(product):
        raise SemanticStateError("daily_workspace_g_context_unverified")
    stored = dict(product)
    for key in _DAILY_WORKSPACE_FORBIDDEN_IDENTITY_FIELDS:
        if key in stored:
            raise SemanticStateError("forbidden_daily_workspace_identity_field")
    if "workspace_ref" in stored and stored["workspace_ref"] != workspace_ref:
        raise SemanticStateError("daily_workspace_identity_conflict")
    stored["workspace_ref"] = workspace_ref
    if "trading_day_id" in stored and stored["trading_day_id"] != trading_day_id:
        raise SemanticStateError("daily_workspace_identity_conflict")
    stored["trading_day_id"] = trading_day_id
    if "product_version" in stored and stored["product_version"] != product_version:
        raise SemanticStateError("daily_workspace_identity_conflict")
    stored["product_version"] = product_version
    if (
        "parent_product_version" in stored
        and stored["parent_product_version"] != parent_product_version
    ):
        raise SemanticStateError("daily_workspace_identity_conflict")
    stored["parent_product_version"] = parent_product_version
    return stored


def _require_product_artifact_hash(artifact_hash: str, *, product_hash: str) -> None:
    expected = f"sha256:{product_hash}"
    if not isinstance(artifact_hash, str) or not hmac.compare_digest(artifact_hash, expected):
        raise ValueError("artifact_hash must match the canonical product")


def _require_non_empty(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _require_finite(value: float, label: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite")


def _require_finite_expiry(now: float, lease_seconds: float, label: str) -> float:
    """校验 now + lease_seconds 有限（两个各自有限的值相加可能溢出为 inf）。"""
    _require_finite(now, f"{label}.now")
    _require_finite(lease_seconds, f"{label}.lease_seconds")
    expiry = now + lease_seconds
    if not math.isfinite(expiry):
        raise ValueError(f"{label}.expiry must be finite")
    return expiry


def validate_persistable_product(value: object, path: str = "product") -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in _FORBIDDEN_PRODUCT_FIELDS:
                raise SemanticStateError("forbidden_product_field")
            validate_persistable_product(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            validate_persistable_product(nested, f"{path}[{index}]")


def _load_json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SemanticStateError("semantic_state_corrupt") from exc
    if not isinstance(decoded, dict):
        raise SemanticStateError("semantic_state_corrupt")
    return cast(dict[str, object], decoded)


def _load_verified_product(product_json: str, artifact_hash: str) -> dict[str, object]:
    try:
        canonical, product_hash = _canonical_object(product_json, "stored product")
        if not hmac.compare_digest(
            product_json.encode("utf-8"),
            canonical.encode("utf-8"),
        ):
            raise SemanticStateError("semantic_state_corrupt")
        _require_product_artifact_hash(artifact_hash, product_hash=product_hash)
        product = _load_json_object(canonical)
        validate_persistable_product(product)
    except (SemanticStateError, ValueError) as exc:
        raise SemanticStateError("semantic_state_corrupt") from exc
    return product


class SemanticStateSnapshotReader:
    """Read lifecycle facts inside a filesystem-read-only OS sandbox."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        epoch: str = DEFAULT_EPOCH,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        if not self._db_path.is_absolute():
            raise ValueError("semantic state path must be absolute")
        if not epoch.strip():
            raise ValueError("epoch must be non-empty")
        self._epoch = epoch
        self._clock = clock or time.time

    def terminal_reconciliation_snapshot(self) -> TerminalReconciliationSnapshot:
        payload = self._snapshot_payload()
        expected_keys = {
            "schema_version",
            "status",
            "total_jobs",
            "active_jobs",
            "expired_jobs",
            "terminal_jobs",
            "uncoordinated_terminal_jobs",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_keys
            or payload.get("schema_version") != "fin.semantic-terminal-reconciliation/v2"
            or payload.get("status") != "ok"
        ):
            raise SemanticStateError("semantic_state_unavailable")
        values = tuple(
            payload[key]
            for key in (
                "total_jobs",
                "active_jobs",
                "expired_jobs",
                "terminal_jobs",
                "uncoordinated_terminal_jobs",
            )
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values
        ):
            raise SemanticStateError("semantic_state_unavailable")
        total = int(payload["total_jobs"])
        active = int(payload["active_jobs"])
        expired = int(payload["expired_jobs"])
        terminal = int(payload["terminal_jobs"])
        uncoordinated = int(payload["uncoordinated_terminal_jobs"])
        if active + expired + terminal != total or uncoordinated > terminal:
            raise SemanticStateError("semantic_state_corrupt")
        return TerminalReconciliationSnapshot(
            total_jobs=total,
            active_jobs=active,
            expired_jobs=expired,
            terminal_jobs=terminal,
            uncoordinated_terminal_jobs=uncoordinated,
        )

    def daily_workspace_timing_samples(
        self,
        *,
        principal_id: str,
        checkpoint: str,
        max_samples: int,
    ) -> tuple[DailyWorkspaceTimingSample, ...]:
        """Read one checkpoint's timing-only facts without opening writable state."""

        _require_non_empty(principal_id, "principal_id")
        if checkpoint not in _DAILY_WORKSPACE_CHECKPOINTS:
            raise ValueError("checkpoint is invalid")
        if (
            not isinstance(max_samples, int)
            or isinstance(max_samples, bool)
            or not 1 <= max_samples <= _MAX_DAILY_WORKSPACE_TIMING_SAMPLES
        ):
            raise ValueError("max_samples is invalid")
        payload = self._snapshot_payload(
            timing_principal_id=principal_id,
            timing_checkpoint=checkpoint,
            timing_max_samples=max_samples,
        )
        expected_keys = {"schema_version", "status", "checkpoint", "samples"}
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_keys
            or payload.get("schema_version") != _SNAPSHOT_DAILY_WORKSPACE_TIMING_SCHEMA
            or payload.get("status") != "ok"
            or payload.get("checkpoint") != checkpoint
            or not isinstance(payload.get("samples"), list)
            or len(payload["samples"]) > max_samples
        ):
            raise SemanticStateError("semantic_state_unavailable")
        samples: list[DailyWorkspaceTimingSample] = []
        previous_day: str | None = None
        for raw_sample in payload["samples"]:
            sample = _daily_workspace_timing_sample_from_snapshot(
                raw_sample,
                checkpoint=checkpoint,
            )
            if sample is None or (
                previous_day is not None and sample.trading_day_id >= previous_day
            ):
                raise SemanticStateError("semantic_state_unavailable")
            samples.append(sample)
            previous_day = sample.trading_day_id
        return tuple(samples)

    def _snapshot_payload(
        self,
        *,
        timing_principal_id: str | None = None,
        timing_checkpoint: str | None = None,
        timing_max_samples: int | None = None,
    ) -> object:
        timing_values = (timing_principal_id, timing_checkpoint, timing_max_samples)
        if any(value is not None for value in timing_values) and not all(
            value is not None for value in timing_values
        ):
            raise ValueError("semantic timing snapshot arguments are incomplete")
        observed_at = self._clock()
        _require_finite(observed_at, "snapshot observation time")
        before = _semantic_snapshot_generation(self._db_path)
        child = Path(__file__).with_name("_semantic_snapshot_child.py")
        interpreter = Path(sys.executable).resolve(strict=True)
        _require_snapshot_executable(_SNAPSHOT_SANDBOX, root_owned=True)
        _require_snapshot_executable(interpreter, root_owned=False)
        _require_snapshot_child(child)
        try:
            schema_manifest_digest = _canonical_semantic_snapshot_schema_digest(
                self._epoch,
            )
        except (sqlite3.Error, TypeError, ValueError):
            raise SemanticStateError("semantic_state_unavailable") from None
        with _semantic_snapshot_observation_database(
            self._db_path,
            expected_generation=before,
            child=child,
            interpreter=interpreter,
        ) as (observation_database, observation_generation):
            command = _snapshot_sandbox_command(
                child=child,
                interpreter=interpreter,
                database=observation_database,
                epoch=self._epoch,
                schema_manifest_digest=schema_manifest_digest,
                observed_at=float(observed_at),
                timing_principal_id=timing_principal_id,
                timing_checkpoint=timing_checkpoint,
                timing_max_samples=timing_max_samples,
            )
            try:
                completed = _run_bounded_snapshot_child(
                    command,
                    env={
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                        "PATH": "/usr/bin:/bin",
                    },
                    timeout=_SNAPSHOT_CHILD_TIMEOUT_SECONDS,
                    max_output_bytes=_SNAPSHOT_CHILD_MAX_OUTPUT_BYTES,
                )
                stdout = completed.stdout.decode("utf-8")
                stderr = completed.stderr.decode("utf-8")
            except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
                raise SemanticStateError("semantic_state_unavailable") from None
            if completed.returncode != 0 or stderr:
                raise SemanticStateError("semantic_state_unavailable")
            try:
                payload = json.loads(stdout)
            except (TypeError, json.JSONDecodeError):
                raise SemanticStateError("semantic_state_unavailable") from None
            observation_after = _semantic_snapshot_generation(observation_database)
            if observation_generation != observation_after:
                raise SemanticStateError("semantic_state_identity_changed")
        if before != _semantic_snapshot_generation(self._db_path):
            raise SemanticStateError("semantic_state_identity_changed")
        return payload


def _snapshot_sandbox_command(
    *,
    child: Path,
    interpreter: Path,
    database: Path,
    epoch: str | None = None,
    schema_manifest_digest: str | None = None,
    observed_at: float | None = None,
    timing_principal_id: str | None = None,
    timing_checkpoint: str | None = None,
    timing_max_samples: int | None = None,
    writable_root: Path | None = None,
    materialize_rollback_destination: Path | None = None,
) -> tuple[str, ...]:
    mounts: tuple[str, ...] = ()
    if writable_root is not None:
        mounts = ("--bind", str(writable_root), str(writable_root))
    command = (
        str(_SNAPSHOT_SANDBOX),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        "/",
        "/",
        *mounts,
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "LC_ALL",
        "C.UTF-8",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--chdir",
        "/",
        "--",
        str(interpreter),
        "-I",
        "-B",
        str(child),
        "--database",
        str(database),
    )
    if materialize_rollback_destination is not None:
        if (
            epoch is not None
            or schema_manifest_digest is not None
            or observed_at is not None
            or timing_principal_id is not None
            or timing_checkpoint is not None
            or timing_max_samples is not None
            or writable_root is None
        ):
            raise ValueError("invalid rollback snapshot materializer command")
        return (
            *command,
            "--materialize-rollback-destination",
            str(materialize_rollback_destination),
        )
    if (
        epoch is None
        or schema_manifest_digest is None
        or observed_at is None
        or writable_root is not None
    ):
        raise ValueError("invalid semantic snapshot observer command")
    _require_finite(observed_at, "snapshot observation time")
    timing_values = (timing_principal_id, timing_checkpoint, timing_max_samples)
    if any(value is not None for value in timing_values) and not all(
        value is not None for value in timing_values
    ):
        raise ValueError("incomplete semantic timing snapshot command")
    command = (
        *command,
        "--epoch",
        epoch,
        "--schema-manifest-digest",
        schema_manifest_digest,
        "--observed-at",
        repr(float(observed_at)),
    )
    if timing_principal_id is None:
        return command
    if timing_checkpoint is None or timing_max_samples is None:
        raise ValueError("incomplete semantic timing snapshot command")
    return (
        *command,
        "--timing-principal-id",
        timing_principal_id,
        "--timing-checkpoint",
        timing_checkpoint,
        "--timing-max-samples",
        str(timing_max_samples),
    )


@contextmanager
def _semantic_snapshot_observation_database(
    database: Path,
    *,
    expected_generation: tuple[
        tuple[object, ...],
        tuple[object, ...] | None,
        tuple[object, ...] | None,
        tuple[object, ...] | None,
    ],
    child: Path,
    interpreter: Path,
) -> Iterator[
    tuple[
        Path,
        tuple[
            tuple[object, ...],
            tuple[object, ...] | None,
            tuple[object, ...] | None,
            tuple[object, ...] | None,
        ],
    ]
]:
    rollback_journal = expected_generation[3]
    if rollback_journal is None:
        yield database, expected_generation
        return
    if expected_generation[1] is not None or expected_generation[2] is not None:
        raise SemanticStateError("semantic_state_unavailable")
    try:
        temporary = tempfile.TemporaryDirectory(prefix="fin-semantic-snapshot-")
    except OSError:
        raise SemanticStateError("semantic_state_unavailable") from None
    try:
        writable_root = Path(temporary.name)
        try:
            root_metadata = writable_root.lstat()
        except OSError:
            raise SemanticStateError("semantic_state_unavailable") from None
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise SemanticStateError("semantic_state_unavailable")
        materialized_database = writable_root / "state.sqlite3"
        command = _snapshot_sandbox_command(
            child=child,
            interpreter=interpreter,
            database=database,
            writable_root=writable_root,
            materialize_rollback_destination=materialized_database,
        )
        try:
            return_code = _run_silent_snapshot_materializer(
                command,
                env={
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                },
                timeout=_SNAPSHOT_MATERIALIZER_TIMEOUT_SECONDS,
                max_file_bytes=_SNAPSHOT_STORE_PART_MAX_BYTES,
            )
        except (OSError, subprocess.SubprocessError):
            raise SemanticStateError("semantic_state_unavailable") from None
        if expected_generation != _semantic_snapshot_generation(database):
            raise SemanticStateError("semantic_state_identity_changed")
        if return_code != 0:
            raise SemanticStateError("semantic_state_unavailable")
        materialized_generation = _semantic_snapshot_generation(materialized_database)
        if any(part is not None for part in materialized_generation[1:]):
            raise SemanticStateError("semantic_state_unavailable")
        yield materialized_database, materialized_generation
        if materialized_generation != _semantic_snapshot_generation(materialized_database):
            raise SemanticStateError("semantic_state_identity_changed")
    finally:
        try:
            temporary.cleanup()
        except OSError:
            raise SemanticStateError("semantic_state_unavailable") from None


def _run_bounded_snapshot_child(
    argv: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout: float,
    max_output_bytes: int,
) -> subprocess.CompletedProcess[bytes]:
    """Capture child bytes behind OS-enforced file-size and wall-time bounds."""
    if timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("snapshot child bounds must be positive")
    _require_snapshot_executable(_SNAPSHOT_LIMIT_LAUNCHER, root_owned=True)
    _, hard_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    requested_limit = max_output_bytes + 1
    effective_limit = (
        requested_limit
        if hard_limit == resource.RLIM_INFINITY
        else min(requested_limit, hard_limit)
    )
    limited_argv = (
        str(_SNAPSHOT_LIMIT_LAUNCHER),
        f"--fsize={effective_limit}:{effective_limit}",
        "--",
        *argv,
    )
    with (
        tempfile.TemporaryFile() as stdout,
        tempfile.TemporaryFile() as stderr,
    ):
        deadline = time.monotonic() + timeout
        process = subprocess.Popen(
            limited_argv,
            cwd=Path("/"),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            shell=False,
            start_new_session=True,
        )
        leader_identity_held = True
        leader_exited = False
        try:
            while True:
                try:
                    leader_status = os.waitid(
                        os.P_PID,
                        process.pid,
                        os.WEXITED | os.WNOHANG | os.WNOWAIT,
                    )
                except ChildProcessError:
                    leader_identity_held = False
                    leader_exited = True
                    raise SemanticStateError("semantic_state_unavailable") from None
                if leader_status is not None:
                    leader_exited = True
                    break
                if (
                    os.fstat(stdout.fileno()).st_size > max_output_bytes
                    or os.fstat(stderr.fileno()).st_size > max_output_bytes
                ):
                    raise SemanticStateError("semantic_state_unavailable")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(limited_argv, timeout)
                time.sleep(min(0.01, remaining))
        except BaseException as error:
            try:
                _kill_and_reap_snapshot_child(
                    process,
                    signal_process_group=leader_identity_held,
                    leader_exited=leader_exited,
                )
            except BaseException:
                error.add_note("semantic_state_snapshot_cleanup_failed")
            raise
        return_code = _kill_and_reap_snapshot_child(
            process,
            signal_process_group=True,
            leader_exited=True,
        )
        if (
            os.fstat(stdout.fileno()).st_size > max_output_bytes
            or os.fstat(stderr.fileno()).st_size > max_output_bytes
        ):
            raise SemanticStateError("semantic_state_unavailable")
        stdout.seek(0)
        stderr.seek(0)
        stdout_bytes = stdout.read(max_output_bytes + 1)
        stderr_bytes = stderr.read(max_output_bytes + 1)
    return subprocess.CompletedProcess(
        args=limited_argv,
        returncode=return_code,
        stdout=stdout_bytes,
        stderr=stderr_bytes,
    )


def _run_silent_snapshot_materializer(
    argv: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout: float,
    max_file_bytes: int,
) -> int:
    """Run the private-copy materializer with no observable output channel."""
    if timeout <= 0 or max_file_bytes <= 0:
        raise ValueError("snapshot materializer bounds must be positive")
    _require_snapshot_executable(_SNAPSHOT_LIMIT_LAUNCHER, root_owned=True)

    def effective_limit(resource_id: int, requested: int) -> int:
        _soft, hard = resource.getrlimit(resource_id)
        return requested if hard == resource.RLIM_INFINITY else min(requested, hard)

    file_limit = effective_limit(resource.RLIMIT_FSIZE, max_file_bytes)
    address_limit = effective_limit(
        resource.RLIMIT_AS,
        _SNAPSHOT_MATERIALIZER_MAX_ADDRESS_SPACE_BYTES,
    )
    open_file_limit = effective_limit(
        resource.RLIMIT_NOFILE,
        _SNAPSHOT_MATERIALIZER_MAX_OPEN_FILES,
    )
    limited_argv = (
        str(_SNAPSHOT_LIMIT_LAUNCHER),
        f"--fsize={file_limit}:{file_limit}",
        f"--as={address_limit}:{address_limit}",
        f"--nofile={open_file_limit}:{open_file_limit}",
        "--",
        *argv,
    )
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        limited_argv,
        cwd=Path("/"),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        start_new_session=True,
    )
    leader_identity_held = True
    leader_exited = False
    try:
        while True:
            try:
                leader_status = os.waitid(
                    os.P_PID,
                    process.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
            except ChildProcessError:
                leader_identity_held = False
                leader_exited = True
                raise SemanticStateError("semantic_state_unavailable") from None
            if leader_status is not None:
                leader_exited = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(limited_argv, timeout)
            time.sleep(min(0.01, remaining))
    except BaseException as error:
        try:
            _kill_and_reap_snapshot_child(
                process,
                signal_process_group=leader_identity_held,
                leader_exited=leader_exited,
            )
        except BaseException:
            error.add_note("semantic_state_snapshot_cleanup_failed")
        raise
    return _kill_and_reap_snapshot_child(
        process,
        signal_process_group=True,
        leader_exited=True,
    )


def _kill_and_reap_snapshot_child(
    process: subprocess.Popen[bytes],
    *,
    signal_process_group: bool,
    leader_exited: bool,
) -> int:
    cleanup_error: OSError | None = None
    if signal_process_group:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError as error:
            if error.errno != errno.ESRCH:
                cleanup_error = error
    if cleanup_error is not None and not leader_exited:
        try:
            process.kill()
        except OSError as error:
            if error.errno != errno.ESRCH:
                cleanup_error = error
    return_code = process.wait()
    if cleanup_error is not None:
        raise SemanticStateError("semantic_state_unavailable") from None
    return return_code


def _validate_semantic_snapshot_store(path: Path) -> os.stat_result:
    try:
        parent_metadata = path.parent.lstat()
        metadata = path.lstat()
    except OSError:
        raise SemanticStateError("semantic_state_unavailable") from None
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & 0o077
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > _SNAPSHOT_STORE_PART_MAX_BYTES
    ):
        raise SemanticStateError("semantic_state_insecure")
    return metadata


def _semantic_snapshot_generation(
    path: Path,
) -> tuple[
    tuple[object, ...],
    tuple[object, ...] | None,
    tuple[object, ...] | None,
    tuple[object, ...] | None,
]:
    database = _validate_semantic_snapshot_store(path)
    return (
        _snapshot_file_content_identity(path, database),
        _snapshot_sidecar_identity(Path(f"{path}-wal")),
        _snapshot_sidecar_identity(Path(f"{path}-shm")),
        _snapshot_sidecar_identity(Path(f"{path}-journal")),
    )


def _snapshot_sidecar_identity(path: Path) -> tuple[object, ...] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise SemanticStateError("semantic_state_unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size > _SNAPSHOT_STORE_PART_MAX_BYTES
    ):
        raise SemanticStateError("semantic_state_insecure")
    return _snapshot_file_content_identity(path, metadata)


def _snapshot_file_content_identity(
    path: Path,
    metadata: os.stat_result,
) -> tuple[object, ...]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise SemanticStateError("semantic_state_unavailable") from None
    try:
        try:
            before = os.fstat(descriptor)
            if _snapshot_stat_identity(before) != _snapshot_stat_identity(metadata):
                raise SemanticStateError("semantic_state_identity_changed")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _SNAPSHOT_STORE_PART_MAX_BYTES:
                    raise SemanticStateError("semantic_state_unavailable")
                digest.update(chunk)
            after = os.fstat(descriptor)
            if _snapshot_stat_identity(before) != _snapshot_stat_identity(after):
                raise SemanticStateError("semantic_state_identity_changed")
        except OSError:
            raise SemanticStateError("semantic_state_unavailable") from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            raise SemanticStateError("semantic_state_unavailable") from None
    return (*_snapshot_stat_identity(metadata), digest.hexdigest())


def _snapshot_stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_snapshot_executable(path: Path, *, root_owned: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise SemanticStateError("semantic_state_sandbox_unavailable") from None
    expected_uid = 0 if root_owned else os.geteuid()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not stat.S_IMODE(metadata.st_mode) & 0o100
    ):
        raise SemanticStateError("semantic_state_sandbox_unsafe")


def _require_snapshot_child(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise SemanticStateError("semantic_state_sandbox_unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise SemanticStateError("semantic_state_sandbox_unsafe")


def _terminal_reconciliation_snapshot(
    connection: sqlite3.Connection,
    *,
    observed_at: float,
    reconciled_now: int = 0,
) -> TerminalReconciliationSnapshot:
    _require_finite(observed_at, "snapshot observation time")
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS total_jobs,
            SUM(CASE WHEN state IN ('queued', 'running') AND deadline_at > ?
                THEN 1 ELSE 0 END)
                AS active_jobs,
            SUM(CASE WHEN state IN ('queued', 'running') AND deadline_at <= ?
                THEN 1 ELSE 0 END)
                AS expired_jobs,
            SUM(CASE WHEN state IN (
                'succeeded', 'partial', 'failed', 'timed_out', 'cancelled'
            ) THEN 1 ELSE 0 END) AS terminal_jobs,
            SUM(CASE WHEN state IN (
                'succeeded', 'partial', 'failed', 'timed_out', 'cancelled'
            ) AND coordinated_version_no IS NULL THEN 1 ELSE 0 END)
                AS uncoordinated_terminal_jobs
        FROM jobs
        """,
        (observed_at, observed_at),
    ).fetchone()
    if row is None:
        raise SemanticStateError("semantic_state_corrupt")
    return TerminalReconciliationSnapshot(
        total_jobs=int(row["total_jobs"] or 0),
        active_jobs=int(row["active_jobs"] or 0),
        expired_jobs=int(row["expired_jobs"] or 0),
        terminal_jobs=int(row["terminal_jobs"] or 0),
        uncoordinated_terminal_jobs=int(row["uncoordinated_terminal_jobs"] or 0),
        reconciled_now=reconciled_now,
    )


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=str(row["job_id"]),
        chain_id=str(row["chain_id"]),
        state=str(row["state"]),
        contract_json=str(row["contract_json"]),
        input_json=str(row["input_json"]),
        contract_hash=str(row["contract_hash"]),
        input_hash=str(row["input_hash"]),
        request_hash=str(row["request_hash"]),
        deadline_at=float(row["deadline_at"]),
        attempt=int(row["attempt"]),
        fencing_token=int(row["fencing_token"]),
    )


def _lease_from_row(
    row: sqlite3.Row,
    *,
    worker_id: str,
    attempt: int,
    fencing_token: int,
    lease_expires_at: float,
) -> JobLease:
    return JobLease(
        job_id=str(row["job_id"]),
        chain_id=str(row["chain_id"]),
        worker_id=worker_id,
        attempt=attempt,
        fencing_token=fencing_token,
        lease_expires_at=lease_expires_at,
        deadline_at=float(row["deadline_at"]),
        contract_json=str(row["contract_json"]),
        input_json=str(row["input_json"]),
        contract_hash=str(row["contract_hash"]),
        input_hash=str(row["input_hash"]),
    )


def _public_status(state: str) -> str:
    if state == "succeeded":
        return "completed"
    if state == "partial":
        return "partial"
    if state in {"failed", "timed_out"}:
        return "unavailable"
    if state == "cancelled":
        return "cancelled"
    raise SemanticStateError("semantic_state_corrupt")
