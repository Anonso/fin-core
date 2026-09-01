"""User-maintained typed A-share watchlist (user context, not investment evidence).

One principal-scoped typed list owned by FIN.  The list is user context: it may
enter the consultation as a trusted focus-of-attention enhancement, never as
investment evidence, and its absence or read failure must never block a direct
Agent answer.  Mutations require an explicit, unambiguous command intent
(CLI or the semantic-service command lane); ordinary research questions never
write.  All state is owner-only (0700/0600), audited minimally, and never
records conversation text, accounts, positions or credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fin_analyse.consultation.instrument_identity import ConsultationInstrumentIdentity

_STATE_RELATIVE = Path("user-watchlist-v1")
# 空态显式 revision：与 `_bump_revision` 的 `or "r0"` 基线一致。
_EMPTY_REVISION = "r0"
# 镜像 fin_analyse.guo_teacher_research.principal_binding._PRINCIPAL_ID_PATTERN；
# 保持同步（binding 层产出必然通过；此处是 store 侧纵深防御）。
_PRINCIPAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    market_symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    added_at TEXT NOT NULL,
    revision TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'owner',
    tags TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision TEXT NOT NULL,
    operation TEXT NOT NULL,
    market_symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    result TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_PROVENANCES = frozenset({"owner", "assistant"})
_RESERVED_TAG_SUGGEST_DELETE = "suggest_delete"
_MAX_TAGS_PER_ENTRY = 8
_MAX_TAG_CHARS = 24


def _validate_provenance(provenance: str) -> str:
    if not isinstance(provenance, str) or provenance not in _PROVENANCES:
        raise UserWatchlistTagError("watchlist_provenance_invalid")
    return provenance


def _validate_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(tags, tuple):
        raise UserWatchlistTagError("watchlist_tags_invalid")
    validated: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if (
            not isinstance(tag, str)
            or not tag
            or tag != tag.strip()
            or len(tag) > _MAX_TAG_CHARS
            or not tag.isprintable()
            or any(ch.isspace() for ch in tag)
        ):
            raise UserWatchlistTagError("watchlist_tag_invalid")
        if tag not in seen:
            seen.add(tag)
            validated.append(tag)
    if len(validated) > _MAX_TAGS_PER_ENTRY:
        raise UserWatchlistTagError("watchlist_tags_too_many")
    return tuple(validated)


def _parse_tags(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str):
        return ()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(tag for tag in parsed if isinstance(tag, str))


def _dedupe_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return tuple(out)


class UserWatchlistError(RuntimeError):
    """Base typed error."""


class UserWatchlistStateError(UserWatchlistError):
    """Owner-only state path validation failed (fail closed, never repaired)."""


class UserWatchlistAddError(UserWatchlistError):
    pass


class UserWatchlistDuplicateError(UserWatchlistAddError):
    pass


class UserWatchlistConflictError(UserWatchlistAddError):
    """Revision CAS conflict: the expected revision no longer matches current."""


class UserWatchlistRemoveError(UserWatchlistError):
    pass


class UserWatchlistTagError(UserWatchlistError):
    pass


class UserWatchlistMissingError(UserWatchlistRemoveError):
    pass


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    market_symbol: str
    name: str
    added_at: str
    provenance: str = "owner"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WatchlistRead:
    entries: tuple[WatchlistEntry, ...]
    revision: str
    as_of: str


@dataclass(frozen=True, slots=True)
class WatchlistMutationResult:
    changed: bool
    revision: str
    market_symbol: str


class UserWatchlistStore:
    """One principal-scoped typed watchlist with revision CAS and audit."""

    def __init__(self, *, root: Path, principal_id: str) -> None:
        if not principal_id or not principal_id.strip():
            raise ValueError("principal_id must be non-empty")
        if not _PRINCIPAL_ID_RE.fullmatch(principal_id):
            raise ValueError("principal_id invalid")
        self._root = root
        self._principal_id = principal_id
        self._db_path = root / _STATE_RELATIVE / f"{principal_id}.sqlite3"

    # ── internal ──────────────────────────────────────────────────────────

    def _require_owner_dir(self, path: Path, *, create: bool = False) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                raise UserWatchlistStateError("watchlist_state_root_missing") from None
            # 并发创建：以下 lstat 复验
            with suppress(FileExistsError):
                path.mkdir(mode=0o700)
            info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise UserWatchlistStateError("watchlist_state_path_not_directory")
        if info.st_uid != os.geteuid():
            raise UserWatchlistStateError("watchlist_state_path_not_owner")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise UserWatchlistStateError("watchlist_state_path_world_accessible")

    def _require_owner_file(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise UserWatchlistStateError("watchlist_state_db_missing") from None
        if not stat.S_ISREG(info.st_mode):
            raise UserWatchlistStateError("watchlist_state_db_not_regular")
        if info.st_nlink != 1:
            raise UserWatchlistStateError("watchlist_state_db_multi_link")
        if info.st_uid != os.geteuid():
            raise UserWatchlistStateError("watchlist_state_db_not_owner")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise UserWatchlistStateError("watchlist_state_db_world_accessible")

    def _ensure_db(self) -> None:
        self._require_owner_dir(self._root)
        directory = self._db_path.parent
        self._require_owner_dir(directory, create=True)
        if self._db_path.exists():
            self._require_owner_file(self._db_path)
            return
        try:
            descriptor = os.open(self._db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # 并发首写：复验 winner 创建的对象（owner regular/nlink=1/0600）后继续。
            self._require_owner_file(self._db_path)
        else:
            os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        self._ensure_db()
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        # schema 初始化必须先于写事务完成（executescript 会隐式提交既有事务）。
        connection.executescript(_SCHEMA)
        self._migrate_columns(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    def _migrate_columns(self, connection: sqlite3.Connection) -> None:
        """Add provenance/tags to legacy entries tables (owner-only migration)."""
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(entries)").fetchall()
        }
        if "provenance" not in columns:
            connection.execute(
                "ALTER TABLE entries ADD COLUMN provenance TEXT NOT NULL DEFAULT 'owner'"
            )
        if "tags" not in columns:
            connection.execute(
                "ALTER TABLE entries ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'"
            )

    def _read_connection(self) -> sqlite3.Connection | None:
        if not self._db_path.is_file():
            return None
        return sqlite3.connect(f"{self._db_path.resolve().as_uri()}?mode=ro", uri=True)

    def _current_revision(self, cursor: sqlite3.Cursor) -> str | None:
        row = cursor.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        return row[0] if row else None

    def _bump_revision(self, cursor: sqlite3.Cursor, *, occurred_at: str) -> str:
        previous = self._current_revision(cursor) or "r0"
        sequence = int(previous.split("-")[0][1:]) + 1 if previous.startswith("r") else 1
        rows = cursor.execute(
            "SELECT market_symbol, name, provenance, tags FROM entries "
            "ORDER BY market_symbol"
        ).fetchall()
        content = json.dumps(rows, ensure_ascii=False, sort_keys=True) + occurred_at
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        revision = f"r{sequence}-{digest}"
        cursor.execute(
            "INSERT INTO meta(key, value) VALUES('revision', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (revision,),
        )
        return revision

    def _check_cas(self, cursor: sqlite3.Cursor, expected_revision: str) -> None:
        current = self._current_revision(cursor) or _EMPTY_REVISION
        if expected_revision != current:
            raise UserWatchlistConflictError("watchlist_revision_conflict")

    def _audit(
        self,
        cursor: sqlite3.Cursor,
        *,
        revision: str,
        operation: str,
        market_symbol: str,
        name: str,
        result: str,
        occurred_at: str,
    ) -> None:
        cursor.execute(
            "INSERT INTO audit(revision, operation, market_symbol, name, result, occurred_at) "
            "VALUES (?,?,?,?,?,?)",
            (revision, operation, market_symbol, name, result, occurred_at),
        )

    # ── public ────────────────────────────────────────────────────────────

    def list(self) -> WatchlistRead:
        connection = self._read_connection()
        if connection is None:
            return WatchlistRead(
                entries=(),
                revision="",
                as_of=datetime.now(UTC).isoformat(),
            )
        try:
            cursor = connection.cursor()
            columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(entries)").fetchall()
            }
            has_provenance = "provenance" in columns
            has_tags = "tags" in columns
            select = (
                "SELECT market_symbol, name, added_at"
                + (", provenance" if has_provenance else "")
                + (", tags" if has_tags else "")
                + " FROM entries ORDER BY added_at"
            )
            rows = cursor.execute(select).fetchall()
            revision = self._current_revision(cursor) or ""
            as_of = datetime.now(UTC).isoformat()
            return WatchlistRead(
                entries=tuple(
                    WatchlistEntry(
                        market_symbol=row[0],
                        name=row[1],
                        added_at=row[2],
                        provenance=row[3] if has_provenance else "owner",
                        tags=_parse_tags(row[4]) if has_tags else (),
                    )
                    for row in rows
                ),
                revision=revision,
                as_of=as_of,
            )
        finally:
            connection.close()

    def add(
        self,
        identity: ConsultationInstrumentIdentity,
        *,
        expected_revision: str,
        provenance: str = "owner",
        tags: tuple[str, ...] = (),
    ) -> WatchlistMutationResult:
        market_symbol = identity.market_symbol
        if not market_symbol:
            raise UserWatchlistAddError("watchlist_instrument_identity_unresolved")
        name = identity.semantic_ref.name or market_symbol
        provenance = _validate_provenance(provenance)
        tags = _validate_tags(tags)
        occurred_at = datetime.now(UTC).isoformat()
        connection = self._connect()
        try:
            cursor = connection.cursor()
            self._check_cas(cursor, expected_revision)
            if cursor.execute(
                "SELECT 1 FROM entries WHERE market_symbol=?", (market_symbol,)
            ).fetchone():
                raise UserWatchlistDuplicateError("watchlist_duplicate_symbol")
            revision = self._bump_revision(cursor, occurred_at=occurred_at)
            cursor.execute(
                "INSERT INTO entries(market_symbol, name, added_at, revision, provenance, tags) "
                "VALUES (?,?,?,?,?,?)",
                (market_symbol, name, occurred_at, revision, provenance, json.dumps(list(tags))),
            )
            self._audit(
                cursor,
                revision=revision,
                operation="add",
                market_symbol=market_symbol,
                name=name,
                result="added",
                occurred_at=occurred_at,
            )
            connection.commit()
            return WatchlistMutationResult(changed=True, revision=revision, market_symbol=market_symbol)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def add_tags(
        self,
        market_symbol: str,
        tags: tuple[str, ...],
        *,
        expected_revision: str,
    ) -> WatchlistMutationResult:
        """Add tags to an existing entry (idempotent merge, zero-write on no-op)."""
        tags = _validate_tags(tags)
        if not tags:
            raise UserWatchlistTagError("watchlist_tags_required")
        occurred_at = datetime.now(UTC).isoformat()
        connection = self._connect()
        try:
            cursor = connection.cursor()
            self._check_cas(cursor, expected_revision)
            row = cursor.execute(
                "SELECT name, tags FROM entries WHERE market_symbol=?", (market_symbol,)
            ).fetchone()
            if row is None:
                raise UserWatchlistMissingError("watchlist_missing_symbol")
            name, raw_tags = row
            current = _parse_tags(raw_tags)
            merged = _dedupe_tags((*current, *tags))
            if merged == current:
                revision = self._current_revision(cursor) or _EMPTY_REVISION
                return WatchlistMutationResult(
                    changed=False, revision=revision, market_symbol=market_symbol
                )
            revision = self._bump_revision(cursor, occurred_at=occurred_at)
            cursor.execute(
                "UPDATE entries SET tags=?, revision=? WHERE market_symbol=?",
                (json.dumps(list(merged)), revision, market_symbol),
            )
            self._audit(
                cursor,
                revision=revision,
                operation="add_tags",
                market_symbol=market_symbol,
                name=name,
                result=f"tags={','.join(merged)}",
                occurred_at=occurred_at,
            )
            connection.commit()
            return WatchlistMutationResult(
                changed=True, revision=revision, market_symbol=market_symbol
            )
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def remove_tags(
        self,
        market_symbol: str,
        tags: tuple[str, ...],
        *,
        expected_revision: str,
    ) -> WatchlistMutationResult:
        """Remove tags from an existing entry (owner CLI only; zero-write on no-op)."""
        tags = _validate_tags(tags)
        if not tags:
            raise UserWatchlistTagError("watchlist_tags_required")
        occurred_at = datetime.now(UTC).isoformat()
        connection = self._connect()
        try:
            cursor = connection.cursor()
            self._check_cas(cursor, expected_revision)
            row = cursor.execute(
                "SELECT name, tags FROM entries WHERE market_symbol=?", (market_symbol,)
            ).fetchone()
            if row is None:
                raise UserWatchlistMissingError("watchlist_missing_symbol")
            name, raw_tags = row
            current = _parse_tags(raw_tags)
            remaining = tuple(t for t in current if t not in set(tags))
            if remaining == current:
                revision = self._current_revision(cursor) or _EMPTY_REVISION
                return WatchlistMutationResult(
                    changed=False, revision=revision, market_symbol=market_symbol
                )
            revision = self._bump_revision(cursor, occurred_at=occurred_at)
            cursor.execute(
                "UPDATE entries SET tags=?, revision=? WHERE market_symbol=?",
                (json.dumps(list(remaining)), revision, market_symbol),
            )
            self._audit(
                cursor,
                revision=revision,
                operation="remove_tags",
                market_symbol=market_symbol,
                name=name,
                result=f"tags={','.join(remaining)}",
                occurred_at=occurred_at,
            )
            connection.commit()
            return WatchlistMutationResult(
                changed=True, revision=revision, market_symbol=market_symbol
            )
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def remove(
        self,
        identity: ConsultationInstrumentIdentity,
        *,
        expected_revision: str,
    ) -> WatchlistMutationResult:
        market_symbol = identity.market_symbol
        if not market_symbol:
            raise UserWatchlistRemoveError("watchlist_instrument_identity_unresolved")
        occurred_at = datetime.now(UTC).isoformat()
        connection = self._connect()
        try:
            cursor = connection.cursor()
            self._check_cas(cursor, expected_revision)
            existing = cursor.execute(
                "SELECT name FROM entries WHERE market_symbol=?", (market_symbol,)
            ).fetchone()
            if existing is None:
                raise UserWatchlistMissingError("watchlist_missing_symbol")
            revision = self._bump_revision(cursor, occurred_at=occurred_at)
            cursor.execute("DELETE FROM entries WHERE market_symbol=?", (market_symbol,))
            self._audit(
                cursor,
                revision=revision,
                operation="remove",
                market_symbol=market_symbol,
                name=existing[0],
                result="removed",
                occurred_at=occurred_at,
            )
            connection.commit()
            return WatchlistMutationResult(changed=True, revision=revision, market_symbol=market_symbol)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def audit_events(self) -> tuple[dict[str, Any], ...]:
        connection = self._read_connection()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT revision, operation, market_symbol, name, result, occurred_at "
                "FROM audit ORDER BY id"
            ).fetchall()
            return tuple(
                {
                    "revision": row[0],
                    "operation": row[1],
                    "market_symbol": row[2],
                    "name": row[3],
                    "result": row[4],
                    "occurred_at": row[5],
                }
                for row in rows
            )
        finally:
            connection.close()
