"""裁决收件箱 durable store（adjudication-inbox 设计页 §数据面）。

只做「清单 + 状态」，不做裁决执行：内容与确认权威留在各功能自己的确认面。
item 只存元数据与指针（payload_ref 指回 producer 自己的产物，不复制内容）。

并发硬约束：单条 item 变更全程 ``BEGIN IMMEDIATE``，连接级 ``busy_timeout``；
SQLite 事务是唯一串行化点，不引入额外 flock。时区单源：所有「日」口径 =
判定时刻 ``Asia/Shanghai`` 历日（由调用方折算，本模块只存 epoch 秒）。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fin_analyse.runtime.state_roots import adjudication_inbox_state_root

_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5000
_OPEN = "open"
_RESOLVED = "resolved"

__all__ = [
    "AdjudicationInbox",
    "AdjudicationInboxError",
    "AdjudicationItem",
    "ItemRow",
    "inbox_fingerprint",
    "open_default_inbox",
]


class AdjudicationInboxError(RuntimeError):
    """Typed failure for inbox I/O, schema, or state-claim violations."""


@dataclass(frozen=True, slots=True)
class AdjudicationItem:
    """Producer-side declaration of one owner-adjudication truth."""

    item_id: str
    kind: str
    title: str
    payload_ref: str | None = None
    resolution_hint: str | None = None


@dataclass(frozen=True, slots=True)
class ItemRow:
    """Projected inbox item (read surface)."""

    item_id: str
    kind: str
    title: str
    payload_ref: str | None
    resolution_hint: str | None
    status: str
    opened_at: float
    last_seen_at: float
    resolved_at: float | None
    resolve_source: str | None
    note: str | None
    reopen_count: int


def open_default_inbox() -> "AdjudicationInbox":
    """Open the inbox at the canonical XDG state root."""

    return AdjudicationInbox(state_root=adjudication_inbox_state_root())


def inbox_fingerprint(items: list[ItemRow]) -> str:
    """Stable digest of the open set (item_id, title, status), per design."""

    material = "\n".join(
        sorted(f"{row.item_id}\x1f{row.title}\x1f{row.status}" for row in items)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AdjudicationInbox:
    """SQLite-backed adjudication inbox (WAL, BEGIN IMMEDIATE serialization)."""

    def __init__(
        self,
        *,
        state_root: Path,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._root = state_root
        self._db_path = state_root / "inbox.sqlite"
        # 连接线程本地：sqlite3 连接不可跨线程共享；跨线程/跨进程串行化靠
        # WAL + BEGIN IMMEDIATE + busy_timeout（设计页并发硬约束）。
        self._local = threading.local()

    # -- lifecycle ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            return existing
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._root.chmod(0o700)
            # Pre-create with owner-only bits so the DB never exists with a
            # group/world-readable creation window (SQLite default is 0644&~umask).
            fd = os.open(self._db_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            os.chmod(self._db_path, 0o600)
            connection = sqlite3.connect(
                self._db_path, timeout=_BUSY_TIMEOUT_MS / 1000.0, isolation_level=None
            )
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            self._ensure_wal(connection)
            connection.execute("PRAGMA foreign_keys = ON")
            self._local.connection = connection
        except sqlite3.Error as error:
            raise AdjudicationInboxError(f"adjudication_inbox_open_failed: {error}") from error
        except OSError as error:
            raise AdjudicationInboxError(f"adjudication_inbox_state_insecure: {error}") from error
        self._enforce_modes()
        self._migrate()
        return self._local.connection

    def _enforce_modes(self) -> None:
        """Keep db/-wal/-shm owner-only (WAL sidecars may outlive umask)."""

        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self._db_path) + suffix)
            try:
                os.chmod(candidate, 0o600)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise AdjudicationInboxError(
                    f"adjudication_inbox_state_insecure: {error}"
                ) from error

    @staticmethod
    def _ensure_wal(connection: sqlite3.Connection) -> None:
        """Switch to WAL once; concurrent openers skip or bounded-retry.

        ``journal_mode=WAL`` needs a brief exclusive lock — racing another
        connection's write transaction raises ``database is locked``. Once
        persisted, subsequent opens read back 'wal' and skip the switch.
        """

        current = connection.execute("PRAGMA journal_mode").fetchone()
        if current and str(current[0]).lower() == "wal":
            return
        last_error: sqlite3.Error | None = None
        for _ in range(3):
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as error:
                last_error = error
                time.sleep(0.05)
        raise AdjudicationInboxError(
            f"adjudication_inbox_wal_switch_failed: {last_error}"
        ) from last_error

    def _migrate(self) -> None:
        connection = self._local.connection
        assert connection is not None
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT value FROM inbox_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO inbox_meta (key, value) VALUES ('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            elif int(row[0]) != _SCHEMA_VERSION:
                raise AdjudicationInboxError(
                    f"adjudication_inbox_schema_drift: {row[0]} != {_SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    item_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload_ref TEXT,
                    resolution_hint TEXT,
                    status TEXT NOT NULL CHECK (status IN ('open','resolved')),
                    opened_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    resolved_at REAL,
                    resolve_source TEXT,
                    note TEXT,
                    reopen_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    at REAL NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('submit','reopen','resolve')),
                    note TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_log (
                    day TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    at REAL NOT NULL,
                    PRIMARY KEY (day, fingerprint)
                )
                """
            )
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            self._rollback(connection)
            raise AdjudicationInboxError(f"adjudication_inbox_migrate_failed: {error}") from error

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        with suppress_sqlite():
            connection.execute("ROLLBACK")

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            with suppress_sqlite():
                connection.close()
            self._local.connection = None

    # -- producer seam -----------------------------------------------------

    def reconcile(self, item: AdjudicationItem, *, pending: bool) -> None:
        """Sync one producer truth. Idempotent replays add no events."""

        _validate_item(item)
        now = self._clock()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT kind, title, payload_ref, resolution_hint, status, reopen_count"
                " FROM items WHERE item_id = ?",
                (item.item_id,),
            ).fetchone()
            if pending:
                if row is None:
                    connection.execute(
                        "INSERT INTO items (item_id, kind, title, payload_ref,"
                        " resolution_hint, status, opened_at, last_seen_at,"
                        " resolved_at, resolve_source, note, reopen_count)"
                        " VALUES (?, ?, ?, ?, ?, 'open', ?, ?, NULL, NULL, NULL, 0)",
                        (
                            item.item_id,
                            item.kind,
                            item.title,
                            item.payload_ref,
                            item.resolution_hint,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO events (item_id, at, action) VALUES (?, ?, 'submit')",
                        (item.item_id, now),
                    )
                elif row[4] == _OPEN:
                    # 内容不变的高频重放只更 last_seen_at：不追加事件、不动 opened_at。
                    connection.execute(
                        "UPDATE items SET kind = ?, title = ?, payload_ref = ?,"
                        " resolution_hint = ?, last_seen_at = ? WHERE item_id = ?",
                        (
                            item.kind,
                            item.title,
                            item.payload_ref,
                            item.resolution_hint,
                            now,
                            item.item_id,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE items SET kind = ?, title = ?, payload_ref = ?,"
                        " resolution_hint = ?, status = 'open', opened_at = ?,"
                        " last_seen_at = ?, resolved_at = NULL, resolve_source = NULL,"
                        " note = NULL, reopen_count = ? WHERE item_id = ?",
                        (
                            item.kind,
                            item.title,
                            item.payload_ref,
                            item.resolution_hint,
                            now,
                            now,
                            int(row[5]) + 1,
                            item.item_id,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO events (item_id, at, action) VALUES (?, ?, 'reopen')",
                        (item.item_id, now),
                    )
            else:
                if row is not None and row[4] == _OPEN:
                    connection.execute(
                        "UPDATE items SET status = 'resolved', resolved_at = ?,"
                        " resolve_source = 'producer' WHERE item_id = ?",
                        (now, item.item_id),
                    )
                    connection.execute(
                        "INSERT INTO events (item_id, at, action) VALUES (?, ?, 'resolve')",
                        (item.item_id, now),
                    )
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            self._rollback(connection)
            raise AdjudicationInboxError(f"adjudication_inbox_reconcile_failed: {error}") from error
        self._enforce_modes()

    # -- owner seam --------------------------------------------------------

    def resolve(
        self,
        item_id: str,
        *,
        source: str,
        note: str | None = None,
    ) -> None:
        """Close one open item (owner `done` or producer explicit resolve)."""

        now = self._clock()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is None or row[0] != _OPEN:
                self._rollback(connection)
                raise AdjudicationInboxError(
                    f"adjudication_item_not_open: {item_id}（用 show 核对当前状态）"
                )
            connection.execute(
                "UPDATE items SET status = 'resolved', resolved_at = ?,"
                " resolve_source = ?, note = ? WHERE item_id = ?",
                (now, source, note, item_id),
            )
            connection.execute(
                "INSERT INTO events (item_id, at, action, note) VALUES (?, ?, 'resolve', ?)",
                (item_id, now, note),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            self._rollback(connection)
            raise AdjudicationInboxError(f"adjudication_inbox_resolve_failed: {error}") from error

    def add_manual(
        self,
        *,
        title: str,
        resolution_hint: str | None,
        payload_ref: str | None = None,
        item_id: str | None = None,
    ) -> str:
        """Register a manual item (doc-anchored or ad-hoc) via the same seam."""

        if not title or not title.strip():
            raise AdjudicationInboxError("adjudication_manual_title_required")
        slug = item_id or _slugify(title)
        resolved_id = slug if slug.startswith("manual:") else f"manual:{slug}"
        self.reconcile(
            AdjudicationItem(
                item_id=resolved_id,
                kind="manual",
                title=title.strip(),
                payload_ref=payload_ref,
                resolution_hint=resolution_hint,
            ),
            pending=True,
        )
        return resolved_id

    # -- read surface ------------------------------------------------------

    def list_open(self) -> list[ItemRow]:
        connection = self._connect()
        rows = connection.execute(
            "SELECT item_id, kind, title, payload_ref, resolution_hint, status,"
            " opened_at, last_seen_at, resolved_at, resolve_source, note, reopen_count"
            " FROM items WHERE status = 'open' ORDER BY opened_at ASC, item_id ASC"
        ).fetchall()
        return [_row_to_item(row) for row in rows]

    def get(self, item_id: str) -> ItemRow | None:
        connection = self._connect()
        row = connection.execute(
            "SELECT item_id, kind, title, payload_ref, resolution_hint, status,"
            " opened_at, last_seen_at, resolved_at, resolve_source, note, reopen_count"
            " FROM items WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return _row_to_item(row) if row is not None else None

    # -- push ledger -------------------------------------------------------

    def sent_day_fingerprints(self, day: str) -> set[str]:
        connection = self._connect()
        rows = connection.execute(
            "SELECT fingerprint FROM sent_log WHERE day = ?", (day,)
        ).fetchall()
        return {row[0] for row in rows}

    def last_sent_at(self, day: str) -> float | None:
        connection = self._connect()
        row = connection.execute(
            "SELECT MAX(at) FROM sent_log WHERE day = ?", (day,)
        ).fetchone()
        return float(row[0]) if row is not None and row[0] is not None else None

    def record_sent(self, *, day: str, fingerprint: str) -> None:
        now = self._clock()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO sent_log (day, fingerprint, at) VALUES (?, ?, ?)",
                (day, fingerprint, now),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            self._rollback(connection)
            raise AdjudicationInboxError(f"adjudication_inbox_sentlog_failed: {error}") from error

    def has_transition_since(self, *, action_set: tuple[str, ...], since: float) -> bool:
        """Whether any state-transition event happened at/after ``since``."""

        connection = self._connect()
        placeholders = ",".join("?" for _ in action_set)
        row = connection.execute(
            f"SELECT 1 FROM events WHERE action IN ({placeholders}) AND at >= ? LIMIT 1",
            (*action_set, since),
        ).fetchone()
        return row is not None


# -- helpers ---------------------------------------------------------------


def _validate_item(item: AdjudicationItem) -> None:
    if not item.item_id or not item.item_id.strip():
        raise AdjudicationInboxError("adjudication_item_id_required")
    if not item.kind or not item.kind.strip():
        raise AdjudicationInboxError(f"adjudication_item_kind_required: {item.item_id}")
    if not item.title or not item.title.strip():
        raise AdjudicationInboxError(f"adjudication_item_title_required: {item.item_id}")


def _slugify(title: str) -> str:
    cleaned = "".join(
        character if character.isalnum() else "-"
        for character in title.strip().lower()
    )
    collapsed = "-".join(part for part in cleaned.split("-") if part)
    trimmed = collapsed[:40] or "item"
    return f"{trimmed}-{hashlib.sha1(title.encode('utf-8')).hexdigest()[:8]}"


def _row_to_item(row: tuple) -> ItemRow:
    return ItemRow(
        item_id=row[0],
        kind=row[1],
        title=row[2],
        payload_ref=row[3],
        resolution_hint=row[4],
        status=row[5],
        opened_at=row[6],
        last_seen_at=row[7],
        resolved_at=row[8],
        resolve_source=row[9],
        note=row[10],
        reopen_count=row[11],
    )


class suppress_sqlite:
    """Suppress sqlite errors during best-effort teardown."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, sqlite3.Error)
