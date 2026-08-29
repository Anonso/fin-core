"""Sanitized request/outcome facts for the official FIN public entry.

The ledger owns transport-engineering facts only.  It never persists request
content, investment evidence, teacher cognition, account data, or provider
transcripts.  Until an independent Hermes delivery observation exists, its
snapshot deliberately reports only FIN MCP dispatch coverage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

_REALM = re.compile(r"[a-z][a-z0-9-]{0,31}")
_OUTCOMES = frozenset({"completed", "partial", "unavailable", "failed"})
_FIN_RESPONSE_OUTCOMES = frozenset({"completed", "partial", "unavailable"})
_SCHEMA_NAME = "fin-public-entry-ledger"
_SCHEMA_VERSION = "3"
_APPLICATION_ID = 0x46494E50
_USER_VERSION = 3
_SCHEMA_SQL = {
    "runtime_truth_meta": (
        "CREATE TABLE runtime_truth_meta (" "key TEXT PRIMARY KEY, " "value TEXT NOT NULL" ")"
    ),
    "public_entry_requests": (
        "CREATE TABLE public_entry_requests ("
        "request_id TEXT PRIMARY KEY, "
        "tool_name TEXT NOT NULL, "
        "principal_scope_hash TEXT NOT NULL, "
        "idempotency_key_hash TEXT, "
        "request_hash TEXT NOT NULL, "
        "first_seen_at TEXT NOT NULL"
        ")"
    ),
    "public_entry_attempts": (
        "CREATE TABLE public_entry_attempts ("
        "attempt_id TEXT PRIMARY KEY, "
        "request_id TEXT NOT NULL REFERENCES public_entry_requests(request_id), "
        "started_at TEXT NOT NULL, "
        "finished_at TEXT, "
        "outcome TEXT CHECK (outcome IS NULL OR outcome IN "
        "('completed', 'partial', 'unavailable', 'failed')), "
        "problem_code TEXT, "
        "dedupe_disposition TEXT NOT NULL CHECK "
        "(dedupe_disposition IN ('new', 'duplicate', 'conflict')), "
        "CHECK ((finished_at IS NULL AND outcome IS NULL AND problem_code IS NULL) "
        "OR (finished_at IS NOT NULL AND outcome IS NOT NULL))"
        ")"
    ),
    "public_entry_delivery_events": (
        "CREATE TABLE public_entry_delivery_events ("
        "event_id TEXT PRIMARY KEY, "
        "attempt_id TEXT NOT NULL REFERENCES public_entry_attempts(attempt_id), "
        "channel TEXT NOT NULL, "
        "stage TEXT NOT NULL CHECK "
        "(stage IN ('rendered', 'dispatched', 'delivered', 'displayed', 'acknowledged')), "
        "status TEXT NOT NULL CHECK "
        "(status IN ('pending', 'succeeded', 'failed', 'abandoned', 'unknown', 'unobservable', "
        "'OUTCOME_UNKNOWN')), "
        "source_contract TEXT, "
        "observed_at TEXT NOT NULL, "
        "message_id TEXT"
        ")"
    ),
}
_EXPECTED_TABLE_INFO = {
    "runtime_truth_meta": (
        ("key", "TEXT", 0, None, 1, 0),
        ("value", "TEXT", 1, None, 0, 0),
    ),
    "public_entry_requests": (
        ("request_id", "TEXT", 0, None, 1, 0),
        ("tool_name", "TEXT", 1, None, 0, 0),
        ("principal_scope_hash", "TEXT", 1, None, 0, 0),
        ("idempotency_key_hash", "TEXT", 0, None, 0, 0),
        ("request_hash", "TEXT", 1, None, 0, 0),
        ("first_seen_at", "TEXT", 1, None, 0, 0),
    ),
    "public_entry_attempts": (
        ("attempt_id", "TEXT", 0, None, 1, 0),
        ("request_id", "TEXT", 1, None, 0, 0),
        ("started_at", "TEXT", 1, None, 0, 0),
        ("finished_at", "TEXT", 0, None, 0, 0),
        ("outcome", "TEXT", 0, None, 0, 0),
        ("problem_code", "TEXT", 0, None, 0, 0),
        ("dedupe_disposition", "TEXT", 1, None, 0, 0),
    ),
    "public_entry_delivery_events": (
        ("event_id", "TEXT", 0, None, 1, 0),
        ("attempt_id", "TEXT", 1, None, 0, 0),
        ("channel", "TEXT", 1, None, 0, 0),
        ("stage", "TEXT", 1, None, 0, 0),
        ("status", "TEXT", 1, None, 0, 0),
        ("source_contract", "TEXT", 0, None, 0, 0),
        ("observed_at", "TEXT", 1, None, 0, 0),
        ("message_id", "TEXT", 0, None, 0, 0),
    ),
}
_EXPECTED_FOREIGN_KEYS = {
    "runtime_truth_meta": (),
    "public_entry_requests": (),
    "public_entry_attempts": (
        (
            "public_entry_requests",
            "request_id",
            "request_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    ),
    "public_entry_delivery_events": (
        (
            "public_entry_attempts",
            "attempt_id",
            "attempt_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    ),
}
_EXPECTED_PRIMARY_KEY_COLUMNS = {
    "runtime_truth_meta": ("key",),
    "public_entry_requests": ("request_id",),
    "public_entry_attempts": ("attempt_id",),
    "public_entry_delivery_events": ("event_id",),
}


class PublicEntryLedgerError(RuntimeError):
    """The public-entry engineering ledger cannot provide trustworthy facts."""


@dataclass(frozen=True)
class PublicEntryAttempt:
    attempt_id: str
    request_id: str
    dedupe_disposition: Literal["new", "duplicate", "conflict"]


@dataclass(frozen=True)
class PublicEntrySnapshot:
    total_attempts: int
    unique_requests: int
    duplicate_attempts: int
    idempotency_conflicts: int
    terminal_attempts: int
    fin_response_attempts: int
    fin_response_rate: float | None
    transport_success_rate: float | None = None
    observation_scope: str = "fin_mcp_dispatch_only"
    data_gaps: tuple[str, ...] = ("hermes_delivery_observation_missing",)
    engineering_status_only: bool = True
    investment_evidence: bool = False
    writes_cognition: bool = False
    affects_confidence: bool = False
    trading_decision: bool = False
    execution_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_attempts": self.total_attempts,
            "unique_requests": self.unique_requests,
            "duplicate_attempts": self.duplicate_attempts,
            "idempotency_conflicts": self.idempotency_conflicts,
            "terminal_attempts": self.terminal_attempts,
            "fin_response_attempts": self.fin_response_attempts,
            "fin_response_rate": self.fin_response_rate,
            "transport_success_rate": self.transport_success_rate,
            "observation_scope": self.observation_scope,
            "data_gaps": list(self.data_gaps),
            "engineering_status_only": self.engineering_status_only,
            "investment_evidence": self.investment_evidence,
            "writes_cognition": self.writes_cognition,
            "affects_confidence": self.affects_confidence,
            "trading_decision": self.trading_decision,
            "execution_allowed": self.execution_allowed,
        }


class PublicEntryLedger:
    """SQLite owner for sanitized official-entry request/outcome facts."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        realm: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if _REALM.fullmatch(realm) is None:
            raise ValueError("realm must be a lowercase stable identifier")
        self._db_path = Path(db_path)
        if not self._db_path.is_absolute():
            raise ValueError("public-entry ledger path must be absolute")
        self._realm = realm
        self._clock = clock or (lambda: datetime.now(UTC))
        self._expected_store_identity = self._prepare_store()
        self._initialize()
        metadata = _validate_existing_store(self._db_path)
        if (metadata.st_dev, metadata.st_ino) != self._expected_store_identity:
            raise PublicEntryLedgerError("public_entry_store_identity_changed")

    def begin(
        self,
        *,
        tool_name: str,
        principal_namespace: str,
        principal_id: str,
        idempotency_key: str | None,
        request_payload: object,
    ) -> PublicEntryAttempt:
        """Start one transport attempt without persisting request content."""

        normalized_tool = _stable_text(tool_name, label="tool_name", limit=128)
        normalized_namespace = _stable_text(
            principal_namespace,
            label="principal_namespace",
            limit=128,
        )
        normalized_principal = _stable_text(
            principal_id,
            label="principal_id",
            limit=256,
        )
        principal_scope_hash = _sha256(
            (f"{self._realm}\0{normalized_namespace}\0{normalized_principal}").encode()
        )
        if idempotency_key is not None:
            normalized_key = _stable_text(
                idempotency_key,
                label="idempotency_key",
                limit=256,
            )
            key_hash = _sha256(
                (
                    f"{self._realm}\0{normalized_tool}\0{principal_scope_hash}\0{normalized_key}"
                ).encode()
            )
            request_id = f"req_{key_hash[:32]}"
        else:
            key_hash = None
            request_id = f"req_{secrets.token_hex(16)}"
        request_hash = _canonical_hash(request_payload)
        attempt_id = f"att_{secrets.token_hex(16)}"
        started_at = _timestamp(self._clock())

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT request_hash
                FROM public_entry_requests
                WHERE request_id=?
                """,
                (request_id,),
            ).fetchone()
            if existing is None:
                disposition: Literal["new", "duplicate", "conflict"] = "new"
                connection.execute(
                    """
                    INSERT INTO public_entry_requests(
                        request_id,
                        tool_name,
                        principal_scope_hash,
                        idempotency_key_hash,
                        request_hash,
                        first_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        normalized_tool,
                        principal_scope_hash,
                        key_hash,
                        request_hash,
                        started_at,
                    ),
                )
            elif str(existing["request_hash"]) == request_hash:
                disposition = "duplicate"
            else:
                disposition = "conflict"
            connection.execute(
                """
                INSERT INTO public_entry_attempts(
                    attempt_id,
                    request_id,
                    started_at,
                    dedupe_disposition
                ) VALUES (?, ?, ?, ?)
                """,
                (attempt_id, request_id, started_at, disposition),
            )

        return PublicEntryAttempt(
            attempt_id=attempt_id,
            request_id=request_id,
            dedupe_disposition=disposition,
        )

    def finish(
        self,
        attempt: PublicEntryAttempt,
        *,
        outcome: str,
        problem_code: str | None = None,
    ) -> None:
        """Finalize one attempt exactly once with a stable outcome class."""

        if outcome not in _OUTCOMES:
            raise ValueError(f"unsupported public-entry outcome: {outcome}")
        normalized_problem = (
            _stable_text(problem_code, label="problem_code", limit=128)
            if problem_code is not None
            else None
        )
        finished_at = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_id, outcome, problem_code
                FROM public_entry_attempts
                WHERE attempt_id=?
                """,
                (attempt.attempt_id,),
            ).fetchone()
            if row is None or str(row["request_id"]) != attempt.request_id:
                raise PublicEntryLedgerError("public_entry_attempt_unknown")
            if row["outcome"] is not None:
                if row["outcome"] == outcome and row["problem_code"] == normalized_problem:
                    return
                raise PublicEntryLedgerError("public_entry_attempt_already_terminal")
            connection.execute(
                """
                UPDATE public_entry_attempts
                SET finished_at=?, outcome=?, problem_code=?
                WHERE attempt_id=?
                """,
                (finished_at, outcome, normalized_problem, attempt.attempt_id),
            )

    def record_delivery_event(
        self,
        *,
        event_id: str,
        attempt_id: str,
        channel: str,
        stage: str,
        status: str,
        source_contract: str | None = None,
        message_id: str | None = None,
        allow_same_request_replay: bool = False,
    ) -> None:
        """Record one sanitized transport fact (idempotent by event_id).

        stage='dispatched' = 平台接受发送（dispatch acceptance，可带 message_id）；
        stage='delivered'/'displayed' = exact-correlated delivery/displayed 回执。
        两事实永不混称。跨 attempt 的同 request replay 必须由调用方显式启用；默认只允许
        同一个 attempt 重放其 event_id。
        """

        _stable_text(event_id, label="event_id", limit=128)
        _stable_text(attempt_id, label="attempt_id", limit=128)
        if channel not in {"feishu", "cli", "api"}:
            raise ValueError(f"unsupported delivery channel: {channel}")
        if stage not in {"rendered", "dispatched", "delivered", "displayed", "acknowledged"}:
            raise ValueError(f"unsupported delivery stage: {stage}")
        if status not in {
            "pending",
            "succeeded",
            "failed",
            "abandoned",
            "unknown",
            "unobservable",
            "OUTCOME_UNKNOWN",
        }:
            raise ValueError(f"unsupported delivery status: {status}")
        normalized_contract = (
            _stable_text(source_contract, label="source_contract", limit=256)
            if source_contract is not None
            else None
        )
        if not isinstance(allow_same_request_replay, bool):
            raise ValueError("allow_same_request_replay must be a bool")
        observed_at = _timestamp(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                """
                SELECT attempt_id, request_id, outcome, dedupe_disposition
                FROM public_entry_attempts WHERE attempt_id=?
                """,
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise PublicEntryLedgerError("public_entry_attempt_unknown")
            if attempt["outcome"] not in {"completed", "partial", "unavailable"}:
                raise PublicEntryLedgerError("public_entry_delivery_attempt_not_fin_response")
            if attempt["dedupe_disposition"] == "conflict":
                raise PublicEntryLedgerError("public_entry_delivery_attempt_conflict")
            existing = connection.execute(
                """
                SELECT event.attempt_id, event.channel, event.stage, event.status,
                       event.source_contract, event.message_id,
                       attempt.request_id, attempt.dedupe_disposition
                FROM public_entry_delivery_events AS event
                JOIN public_entry_attempts AS attempt
                  ON attempt.attempt_id = event.attempt_id
                WHERE event.event_id=?
                """,
                (event_id,),
            ).fetchone()
            if existing is not None:
                same_attempt = str(existing["attempt_id"]) == attempt_id
                same_replayable_request = (
                    allow_same_request_replay
                    and attempt["dedupe_disposition"] in {"new", "duplicate"}
                    and existing["dedupe_disposition"] in {"new", "duplicate"}
                    and str(existing["request_id"]) == str(attempt["request_id"])
                )
                if (
                    (same_attempt or same_replayable_request)
                    and str(existing["channel"]) == channel
                    and str(existing["stage"]) == stage
                    and str(existing["status"]) == status
                    and (existing["source_contract"] or None) == normalized_contract
                    and existing["message_id"] == message_id
                ):
                    return
                raise PublicEntryLedgerError("public_entry_delivery_event_conflict")
            connection.execute(
                """
                INSERT INTO public_entry_delivery_events
                    (event_id, attempt_id, channel, stage, status, source_contract, observed_at, message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    attempt_id,
                    channel,
                    stage,
                    status,
                    normalized_contract,
                    observed_at,
                    message_id,
                ),
            )

    def recent_attempts(self, *, limit: int = 200) -> list[dict[str, str | None]]:
        """Return recent FIN-response attempt identities for the delivery
        observer's strict correlation (only outcomes that produce a FIN
        presentation)."""

        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("recent attempts limit is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT attempt_id, finished_at
                FROM public_entry_attempts
                WHERE finished_at IS NOT NULL
                  AND outcome IN ('completed', 'partial', 'unavailable')
                ORDER BY finished_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {"attempt_id": str(row["attempt_id"]), "finished_at": str(row["finished_at"])}
            for row in rows
        ]

    def snapshot(self) -> PublicEntrySnapshot:
        """Return aggregate FIN-dispatch facts without claiming Hermes delivery."""

        with self._connect() as connection:
            return _snapshot_from_connection(connection)

    def _prepare_store(self) -> tuple[int, int]:
        parent = self._db_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_metadata = parent.lstat()
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
            or parent_metadata.st_mode & 0o077
        ):
            raise PublicEntryLedgerError("public_entry_store_parent_insecure")
        try:
            descriptor = os.open(
                self._db_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            descriptor = -1
        if descriptor >= 0:
            os.close(descriptor)
        metadata = self._db_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PublicEntryLedgerError("public_entry_store_insecure")
        return metadata.st_dev, metadata.st_ino

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            objects = _user_schema_objects(connection)
            # v2→v3 时 object 集合与 v3 expected 相同（同四表），仅 user_version 不同——
            # guard 必须同时比较 user_version，否则旧库直接进严格校验而失败。
            if objects and (
                set(objects) != _expected_schema_objects()
                or _pragma_int(connection, "user_version") != _USER_VERSION
            ):
                table_names = {name for _kind, name, _name in objects}
                if table_names == {
                    "runtime_truth_meta",
                    "public_entry_requests",
                    "public_entry_attempts",
                } and _pragma_int(connection, "user_version") == 1:
                    connection.execute(_SCHEMA_SQL["public_entry_delivery_events"])
                    connection.execute(f"PRAGMA user_version={_USER_VERSION}")
                    connection.execute(
                        "UPDATE runtime_truth_meta SET value=? WHERE key='schema_version'",
                        (_SCHEMA_VERSION,),
                    )
                elif table_names == {
                    "runtime_truth_meta",
                    "public_entry_requests",
                    "public_entry_attempts",
                    "public_entry_delivery_events",
                } and _pragma_int(connection, "user_version") == 2:
                    # v2 → v3：重建 delivery_events（ALTER 不换 CHECK 闭集，必须整表重建）
                    # ——换 stage/status 闭集（dispatched/OUTCOME_UNKNOWN）+ message_id 列；
                    # 既有 Hermes obligation 观察（平台接受发送）重标为 dispatched——
                    # 其真实语义是平台接受发送，不是 exact delivery 回执（两事实分离）。
                    # delivery_events 只引用 attempts（不反向），DROP 不违反 FK。
                    # 先 RENAME 旧表、再以最终名 CREATE 新表：sqlite RENAME 会把
                    # sqlite_master 中存储的 CREATE 改写成带引号表名，与 _SCHEMA_SQL
                    # 逐字校验不匹配；最终名直建则存储 SQL 与期望逐字一致。
                    connection.execute(
                        "ALTER TABLE public_entry_delivery_events "
                        "RENAME TO public_entry_delivery_events_v2"
                    )
                    connection.execute(_SCHEMA_SQL["public_entry_delivery_events"])
                    connection.execute(
                        """
                        INSERT INTO public_entry_delivery_events
                            (event_id, attempt_id, channel, stage, status,
                             source_contract, observed_at, message_id)
                        SELECT event_id, attempt_id, channel,
                               CASE WHEN stage='delivered' AND
                                         source_contract='hermes.delivery_obligations/v0.19.0'
                                    THEN 'dispatched' ELSE stage END,
                               status, source_contract, observed_at, NULL
                        FROM public_entry_delivery_events_v2
                        """
                    )
                    connection.execute("DROP TABLE public_entry_delivery_events_v2")
                    connection.execute(f"PRAGMA user_version={_USER_VERSION}")
                    connection.execute(
                        "UPDATE runtime_truth_meta SET value=? WHERE key='schema_version'",
                        (_SCHEMA_VERSION,),
                    )
                else:
                    raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
            if not objects:
                if (
                    _pragma_int(connection, "application_id") != 0
                    or _pragma_int(connection, "user_version") != 0
                ):
                    raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
                for statement in _SCHEMA_SQL.values():
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={_USER_VERSION}")
                connection.executemany(
                    "INSERT INTO runtime_truth_meta(key, value) VALUES (?, ?)",
                    (
                        ("schema_name", _SCHEMA_NAME),
                        ("schema_version", _SCHEMA_VERSION),
                        ("realm", self._realm),
                    ),
                )
            _require_public_entry_schema(connection, realm=self._realm)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with _bound_sqlite_connection(
            self._db_path,
            read_only=False,
            expected_identity=self._expected_store_identity,
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()


class PublicEntrySnapshotReader:
    """Read an existing realm-bound ledger without provisioning or migrating it."""

    def __init__(self, db_path: str | Path, *, realm: str) -> None:
        if _REALM.fullmatch(realm) is None:
            raise ValueError("realm must be a lowercase stable identifier")
        self._db_path = Path(db_path)
        if not self._db_path.is_absolute():
            raise ValueError("public-entry ledger path must be absolute")
        self._realm = realm

    def snapshot(self) -> PublicEntrySnapshot:
        try:
            with _bound_sqlite_connection(
                self._db_path,
                read_only=True,
                require_stable_generation=True,
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                _require_public_entry_schema(connection, realm=self._realm)
                return _snapshot_from_connection(connection)
        except PublicEntryLedgerError:
            raise
        except sqlite3.Error as error:
            raise PublicEntryLedgerError("public_entry_store_unavailable") from error


@contextmanager
def _bound_sqlite_connection(
    path: Path,
    *,
    read_only: bool,
    expected_identity: tuple[int, int] | None = None,
    require_stable_generation: bool = False,
) -> Iterator[sqlite3.Connection]:
    before = _validate_existing_store(path)
    identity = (before.st_dev, before.st_ino)
    if expected_identity is not None and identity != expected_identity:
        raise PublicEntryLedgerError("public_entry_store_identity_changed")
    flags = (
        (os.O_RDONLY if read_only else os.O_RDWR)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PublicEntryLedgerError("public_entry_store_unavailable") from error
    connection: sqlite3.Connection | None = None
    try:
        pinned = os.fstat(descriptor)
        if (pinned.st_dev, pinned.st_ino) != identity:
            raise PublicEntryLedgerError("public_entry_store_identity_changed")
        if read_only:
            _require_stable_rollback_store(path, descriptor)
        generation_before = (
            _descriptor_generation(descriptor) if require_stable_generation else None
        )
        descriptor_path = Path("/proc/self/fd") / str(descriptor)
        if not descriptor_path.parent.is_dir():
            raise PublicEntryLedgerError("public_entry_store_descriptor_unavailable")
        mode = "ro" if read_only else "rw"
        connection = sqlite3.connect(
            f"{descriptor_path.as_uri()}?mode={mode}",
            uri=True,
            timeout=5.0,
        )
        after_open = os.fstat(descriptor)
        path_after_open = _validate_existing_store(path)
        if (after_open.st_dev, after_open.st_ino) != identity or (
            path_after_open.st_dev,
            path_after_open.st_ino,
        ) != identity:
            raise PublicEntryLedgerError("public_entry_store_identity_changed")
        yield connection
        if require_stable_generation:
            _require_stable_rollback_store(path, descriptor)
            if _descriptor_generation(descriptor) != generation_before:
                raise PublicEntryLedgerError("public_entry_store_generation_changed")
        path_after_use = _validate_existing_store(path)
        if (path_after_use.st_dev, path_after_use.st_ino) != identity:
            raise PublicEntryLedgerError("public_entry_store_identity_changed")
    finally:
        if connection is not None:
            connection.close()
        os.close(descriptor)


def _require_stable_rollback_store(path: Path, descriptor: int) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            Path(f"{path}{suffix}").lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise PublicEntryLedgerError("public_entry_store_unavailable") from error
        raise PublicEntryLedgerError("public_entry_store_generation_unsupported")
    metadata = os.fstat(descriptor)
    if metadata.st_size == 0:
        return
    try:
        header = os.pread(descriptor, 20, 0)
    except OSError as error:
        raise PublicEntryLedgerError("public_entry_store_unavailable") from error
    if len(header) != 20 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise PublicEntryLedgerError("public_entry_store_generation_unsupported")


def _descriptor_generation(descriptor: int) -> tuple[object, ...]:
    before = os.fstat(descriptor)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except OSError as error:
        raise PublicEntryLedgerError("public_entry_store_unavailable") from error
    finally:
        with suppress(OSError):
            os.lseek(descriptor, 0, os.SEEK_SET)
    after = os.fstat(descriptor)
    before_identity = _stat_identity(before)
    if _stat_identity(after) != before_identity:
        raise PublicEntryLedgerError("public_entry_store_generation_changed")
    return (*before_identity, digest.hexdigest())


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _validate_existing_store(path: Path) -> os.stat_result:
    try:
        parent_metadata = path.parent.lstat()
        metadata = path.lstat()
    except OSError as error:
        raise PublicEntryLedgerError("public_entry_store_unavailable") from error
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & 0o077
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise PublicEntryLedgerError("public_entry_store_insecure")
    return metadata


def _user_schema_objects(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str, str], str]:
    return {(str(row[0]), str(row[1]), str(row[2])): str(row[3]) for row in connection.execute("""
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT GLOB 'sqlite_*'
            ORDER BY type, name
            """)}


def _expected_schema_objects() -> set[tuple[str, str, str]]:
    return {("table", table, table) for table in _SCHEMA_SQL}


def _pragma_int(connection: sqlite3.Connection, name: str) -> int:
    if name not in {"application_id", "user_version"}:
        raise ValueError("unsupported public-entry pragma")
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or not isinstance(row[0], int):
        raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
    return int(row[0])


def _require_public_entry_schema(
    connection: sqlite3.Connection,
    *,
    realm: str,
) -> None:
    objects = _user_schema_objects(connection)
    if set(objects) != _expected_schema_objects():
        raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
    if (
        _pragma_int(connection, "application_id") != _APPLICATION_ID
        or _pragma_int(connection, "user_version") != _USER_VERSION
    ):
        raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
    for table, expected_sql in _SCHEMA_SQL.items():
        if objects[("table", table, table)] != expected_sql:
            raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
        table_info = tuple(
            (
                str(row[1]),
                str(row[2]),
                int(row[3]),
                row[4],
                int(row[5]),
                int(row[6]),
            )
            for row in connection.execute(f"PRAGMA table_xinfo({table})")
        )
        if table_info != _EXPECTED_TABLE_INFO[table]:
            raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
        foreign_keys = tuple(
            (
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
            )
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        )
        if foreign_keys != _EXPECTED_FOREIGN_KEYS[table]:
            raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
        primary_indexes = [
            row for row in connection.execute(f"PRAGMA index_list({table})") if str(row[3]) == "pk"
        ]
        if len(primary_indexes) != 1 or int(primary_indexes[0][2]) != 1:
            raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
        index_name = str(primary_indexes[0][1])
        primary_columns = tuple(
            str(row[2]) for row in connection.execute(f"PRAGMA index_info({index_name})")
        )
        if primary_columns != _EXPECTED_PRIMARY_KEY_COLUMNS[table]:
            raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
    metadata = {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM runtime_truth_meta")
    }
    if metadata.get("schema_name") != _SCHEMA_NAME:
        raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
    if metadata.get("schema_version") != _SCHEMA_VERSION:
        raise PublicEntryLedgerError("public_entry_store_schema_unsupported")
    if metadata.get("realm") != realm:
        raise PublicEntryLedgerError("public_entry_store_realm_mismatch")
    if set(metadata) != {"schema_name", "schema_version", "realm"}:
        raise PublicEntryLedgerError("public_entry_store_schema_mismatch")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise PublicEntryLedgerError("public_entry_store_integrity_mismatch")
    quick_check = connection.execute("PRAGMA quick_check").fetchall()
    if len(quick_check) != 1 or str(quick_check[0][0]) != "ok":
        raise PublicEntryLedgerError("public_entry_store_integrity_mismatch")


def _snapshot_from_connection(
    connection: sqlite3.Connection,
) -> PublicEntrySnapshot:
    row = connection.execute("""
        SELECT
            COUNT(*) AS total_attempts,
            COUNT(DISTINCT request_id) AS unique_requests,
            SUM(CASE WHEN dedupe_disposition='duplicate' THEN 1 ELSE 0 END)
                AS duplicate_attempts,
            SUM(CASE WHEN dedupe_disposition='conflict' THEN 1 ELSE 0 END)
                AS idempotency_conflicts,
            SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END)
                AS terminal_attempts,
            SUM(CASE WHEN outcome IN ('completed', 'partial', 'unavailable')
                THEN 1 ELSE 0 END) AS fin_response_attempts
        FROM public_entry_attempts
        """).fetchone()
    assert row is not None
    total = int(row["total_attempts"] or 0)
    fin_responses = int(row["fin_response_attempts"] or 0)
    delivery = connection.execute(
        """
        SELECT COUNT(DISTINCT e.attempt_id) AS delivered
        FROM public_entry_delivery_events e
        JOIN public_entry_attempts a ON a.attempt_id = e.attempt_id
        WHERE e.status='succeeded' AND e.channel='feishu' AND e.stage='delivered'
          AND a.outcome IN ('completed', 'partial', 'unavailable')
        """
    ).fetchone()
    delivered = int(delivery["delivered"] or 0) if delivery is not None else 0
    gaps: tuple[str, ...] = ()
    scope = "fin_mcp_dispatch_only"
    transport_rate: float | None = None
    if delivered > 0:
        scope = "fin_mcp_dispatch_plus_hermes_delivery"
        transport_rate = (delivered / fin_responses) if fin_responses else None
    else:
        gaps = ("hermes_delivery_observation_missing",)
    return PublicEntrySnapshot(
        total_attempts=total,
        unique_requests=int(row["unique_requests"] or 0),
        duplicate_attempts=int(row["duplicate_attempts"] or 0),
        idempotency_conflicts=int(row["idempotency_conflicts"] or 0),
        terminal_attempts=int(row["terminal_attempts"] or 0),
        fin_response_attempts=fin_responses,
        fin_response_rate=(fin_responses / total) if total else None,
        transport_success_rate=transport_rate,
        observation_scope=scope,
        data_gaps=gaps,
    )


def _canonical_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("request_payload must be canonical JSON") from error
    return _sha256(encoded)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_text(value: str, *, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > limit or any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("public-entry clock must return an aware datetime")
    return value.astimezone(UTC).isoformat()
