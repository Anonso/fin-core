"""Owner-stated decision journal (append-only durable store).

One principal-scoped, append-only record of investment decisions the owner
stated explicitly (buy/sell/plan/revert).  The journal is user context for
review questions: it is the factual history of past decisions, never
investment evidence, and its absence or read failure must never block a
direct Agent answer.  There is no update or delete path — corrections are
appended as ``revert`` records pointing at the corrected decision (IFF:
``decision_type='revert'`` ⇔ ``revert_of`` non-null, target must exist and
be not-yet-reverted; enforced by a table CHECK, a foreign key and a partial
unique index, and re-checked in code before any insert).  All state is
owner-only (0700/0600), revision-accounted and minimally audited; append
does no read-modify-write, so there is no CAS and no false-conflict surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_STATE_RELATIVE = Path("decision-journal-v1")
_SCHEMA_VERSION = "decision-journal.v1"
_SOURCE = "owner_stated"
_DECISION_TYPES = frozenset({"buy", "sell", "plan", "revert"})
_MAX_RATIONALE_CHARS = 2000
_MAX_NOTE_CHARS = 500
_MAX_QUERY_LIMIT = 200
_ID_ATTEMPTS = 3
# 镜像 fin_analyse.portfolio.user_watchlist._PRINCIPAL_ID_PATTERN；
# 保持同步（binding 层产出必然通过；此处是 store 侧纵深防御）。
_PRINCIPAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    decision_type TEXT NOT NULL CHECK (decision_type IN ('buy','sell','plan','revert')),
    symbol TEXT,
    decision_date TEXT NOT NULL,
    rationale TEXT NOT NULL,
    note TEXT,
    source TEXT NOT NULL,
    revert_of TEXT REFERENCES decisions(decision_id),
    recorded_at TEXT NOT NULL,
    CHECK ((decision_type='revert') = (revert_of IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol);
CREATE INDEX IF NOT EXISTS idx_decisions_date ON decisions(decision_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_revert_of
    ON decisions(revert_of) WHERE revert_of IS NOT NULL;
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision TEXT NOT NULL,
    operation TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    symbol TEXT,
    result TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _is_iso_date(value: object) -> bool:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    try:
        return datetime.fromisoformat(value).date().isoformat() == value
    except ValueError:
        return False


def new_decision_id(decision_date: str) -> str:
    return f"DJ-{decision_date}-{secrets.token_hex(2)}"


def normalize_decision_fields(
    *,
    decision_type: object,
    symbol: object,
    decision_date: object,
    rationale: object,
    note: object,
    revert_of: object,
) -> dict[str, object]:
    """Shared closed-set/shape validation and normalization.

    The service preview layer calls this before token issue and maps any
    typed error message verbatim into a REJECTED reason code; the store
    re-runs it inside the write transaction so the invariants cannot drift.
    Raises DecisionJournalAppendError with a ``decision_journal_*`` code.
    """
    if not isinstance(decision_type, str) or decision_type not in _DECISION_TYPES:
        raise DecisionJournalAppendError("decision_journal_type_invalid")
    if not isinstance(rationale, str) or not rationale.strip():
        raise DecisionJournalAppendError("decision_journal_rationale_required")
    if len(rationale) > _MAX_RATIONALE_CHARS:
        raise DecisionJournalAppendError("decision_journal_rationale_too_long")
    if note is not None:
        if not isinstance(note, str) or not note.strip():
            raise DecisionJournalAppendError("decision_journal_note_invalid")
        if len(note) > _MAX_NOTE_CHARS:
            raise DecisionJournalAppendError("decision_journal_note_too_long")
    if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
        raise DecisionJournalAppendError("decision_journal_symbol_invalid")
    if not _is_iso_date(decision_date):
        raise DecisionJournalAppendError("decision_journal_date_invalid")
    normalized_revert_of: str | None = None
    if revert_of is not None:
        if not isinstance(revert_of, str) or not revert_of.strip():
            raise DecisionJournalAppendError("decision_journal_revert_of_invalid")
        if decision_type != "revert":
            raise DecisionJournalAppendError("decision_journal_revert_of_invalid")
        normalized_revert_of = revert_of.strip()
    elif decision_type == "revert":
        raise DecisionJournalAppendError("decision_journal_revert_of_required")
    return {
        "decision_type": decision_type,
        "symbol": (symbol or None) if not isinstance(symbol, str) else (symbol.strip() or None),
        "decision_date": decision_date,
        "rationale": rationale.strip(),
        "note": (note.strip() if isinstance(note, str) and note.strip() else None),
        "revert_of": normalized_revert_of,
    }


class DecisionJournalError(RuntimeError):
    """Base typed error."""


class DecisionJournalStateError(DecisionJournalError):
    """Owner-only state path validation failed (fail closed, never repaired)."""


class DecisionJournalAppendError(DecisionJournalError):
    """Append rejected: field invalid, constraint hit, or id space exhausted."""


class DecisionJournalRevertError(DecisionJournalAppendError):
    """Revert target missing, already reverted, or revert constraint hit."""


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    decision_type: str
    decision_date: str
    rationale: str
    recorded_at: str
    symbol: str | None = None
    note: str | None = None
    revert_of: str | None = None
    reverted_by: str | None = None
    source: str = _SOURCE
    schema_version: str = _SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class DecisionJournalRead:
    records: tuple[DecisionRecord, ...]
    revision: str
    as_of: str


@dataclass(frozen=True, slots=True)
class DecisionJournalMutation:
    decision_id: str
    revision: str


class DecisionJournalStore:
    """One principal-scoped append-only decision journal with revision accounting."""

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
                raise DecisionJournalStateError("decision_journal_state_root_missing") from None
            # 并发创建：以下 lstat 复验
            with suppress(FileExistsError):
                path.mkdir(mode=0o700)
            info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise DecisionJournalStateError("decision_journal_state_path_not_directory")
        if info.st_uid != os.geteuid():
            raise DecisionJournalStateError("decision_journal_state_path_not_owner")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise DecisionJournalStateError("decision_journal_state_path_world_accessible")

    def _require_owner_file(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise DecisionJournalStateError("decision_journal_state_db_missing") from None
        if not stat.S_ISREG(info.st_mode):
            raise DecisionJournalStateError("decision_journal_state_db_not_regular")
        if info.st_nlink != 1:
            raise DecisionJournalStateError("decision_journal_state_db_multi_link")
        if info.st_uid != os.geteuid():
            raise DecisionJournalStateError("decision_journal_state_db_not_owner")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise DecisionJournalStateError("decision_journal_state_db_world_accessible")

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
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    def _read_connection(self) -> sqlite3.Connection | None:
        if not self._db_path.is_file():
            return None
        # 读路径同样 fail-closed（外审 P2）：坏权限/symlink/multi-link 的既有库
        # 不许被静默读出，typed 拒绝；空态（库不存在）保持合法。
        self._require_owner_dir(self._db_path.parent)
        self._require_owner_file(self._db_path)
        return sqlite3.connect(f"{self._db_path.resolve().as_uri()}?mode=ro", uri=True)

    def _current_revision(self, cursor: sqlite3.Cursor) -> str | None:
        row = cursor.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        return row[0] if row else None

    def _bump_revision(self, cursor: sqlite3.Cursor, *, occurred_at: str) -> str:
        previous = self._current_revision(cursor) or "r0"
        sequence = int(previous.split("-")[0][1:]) + 1 if previous.startswith("r") else 1
        rows = cursor.execute(
            "SELECT decision_id, decision_type, symbol, decision_date, revert_of "
            "FROM decisions ORDER BY decision_id"
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

    def _preflight_revert(
        self, cursor: sqlite3.Cursor, revert_of: str | None
    ) -> None:
        if revert_of is None:
            return
        row = cursor.execute(
            "SELECT decision_id FROM decisions WHERE decision_id=?", (revert_of,)
        ).fetchone()
        if row is None:
            raise DecisionJournalRevertError("decision_journal_revert_target_missing")
        if cursor.execute(
            "SELECT 1 FROM decisions WHERE revert_of=?", (revert_of,)
        ).fetchone():
            raise DecisionJournalRevertError(
                "decision_journal_revert_target_already_reverted"
            )

    def _audit(
        self,
        cursor: sqlite3.Cursor,
        *,
        revision: str,
        decision_id: str,
        decision_type: str,
        symbol: str | None,
        result: str,
        occurred_at: str,
    ) -> None:
        cursor.execute(
            "INSERT INTO audit(revision, operation, decision_id, decision_type, symbol, "
            "result, occurred_at) VALUES (?,?,?,?,?,?,?)",
            (revision, "append", decision_id, decision_type, symbol, result, occurred_at),
        )

    # ── public ────────────────────────────────────────────────────────────

    def append(
        self,
        *,
        decision_type: str,
        decision_date: str,
        rationale: str,
        symbol: str | None = None,
        note: str | None = None,
        revert_of: str | None = None,
    ) -> DecisionJournalMutation:
        """Append one owner-stated decision (append-only; no CAS by design)."""
        fields = normalize_decision_fields(
            decision_type=decision_type,
            symbol=symbol,
            decision_date=decision_date,
            rationale=rationale,
            note=note,
            revert_of=revert_of,
        )
        occurred_at = datetime.now(UTC).isoformat()
        connection = self._connect()
        try:
            cursor = connection.cursor()
            self._preflight_revert(cursor, fields["revert_of"])
            decision_id: str | None = None
            for _ in range(_ID_ATTEMPTS):
                candidate = new_decision_id(str(fields["decision_date"]))
                try:
                    cursor.execute(
                        "INSERT INTO decisions(decision_id, schema_version, decision_type, "
                        "symbol, decision_date, rationale, note, source, revert_of, "
                        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            candidate,
                            _SCHEMA_VERSION,
                            fields["decision_type"],
                            fields["symbol"],
                            fields["decision_date"],
                            fields["rationale"],
                            fields["note"],
                            _SOURCE,
                            fields["revert_of"],
                            occurred_at,
                        ),
                    )
                except sqlite3.IntegrityError:
                    if cursor.execute(
                        "SELECT 1 FROM decisions WHERE decision_id=?", (candidate,)
                    ).fetchone():
                        continue  # PK 撞号（4hex 空间极小概率）：重生成重试。
                    raise DecisionJournalAppendError(
                        "decision_journal_append_constraint"
                    ) from None
                decision_id = candidate
                break
            if decision_id is None:
                raise DecisionJournalAppendError("decision_journal_id_exhausted")
            revision = self._bump_revision(cursor, occurred_at=occurred_at)
            self._audit(
                cursor,
                revision=revision,
                decision_id=decision_id,
                decision_type=str(fields["decision_type"]),
                symbol=fields["symbol"],
                result="appended",
                occurred_at=occurred_at,
            )
            connection.commit()
            return DecisionJournalMutation(decision_id=decision_id, revision=revision)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, decision_id: str) -> DecisionRecord | None:
        connection = self._read_connection()
        if connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT d.decision_id, d.decision_type, d.symbol, d.decision_date, "
                "d.rationale, d.note, d.source, d.revert_of, d.recorded_at, "
                "r.decision_id FROM decisions d "
                "LEFT JOIN decisions r ON r.revert_of = d.decision_id "
                "WHERE d.decision_id=?",
                (decision_id,),
            ).fetchone()
            return _record_from_row(row) if row is not None else None
        finally:
            connection.close()

    def query(
        self,
        *,
        symbol: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        decision_type: str | None = None,
        limit: int = 50,
    ) -> DecisionJournalRead:
        clauses: list[str] = []
        params: list[object] = []
        if symbol:
            clauses.append("d.symbol = ?")
            params.append(symbol)
        if date_from:
            clauses.append("d.decision_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("d.decision_date <= ?")
            params.append(date_to)
        if decision_type:
            clauses.append("d.decision_type = ?")
            params.append(decision_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT d.decision_id, d.decision_type, d.symbol, d.decision_date, "
            "d.rationale, d.note, d.source, d.revert_of, d.recorded_at, r.decision_id "
            "FROM decisions d LEFT JOIN decisions r ON r.revert_of = d.decision_id "
            f"{where} "
            "ORDER BY d.decision_date DESC, d.recorded_at DESC, d.decision_id DESC "
            "LIMIT ?"
        )
        params.append(max(1, min(int(limit), _MAX_QUERY_LIMIT)))
        connection = self._read_connection()
        if connection is None:
            return DecisionJournalRead(
                records=(), revision="", as_of=datetime.now(UTC).isoformat()
            )
        try:
            # 同一读事务：records 与 revision 取同一 WAL 快照（外审 P3），
            # 并发 append 不会出现旧 records 配新 revision。
            connection.execute("BEGIN")
            rows = connection.execute(sql, params).fetchall()
            revision = _read_revision(connection)
            connection.commit()
            return DecisionJournalRead(
                records=tuple(_record_from_row(row) for row in rows),
                revision=revision,
                as_of=datetime.now(UTC).isoformat(),
            )
        finally:
            connection.close()

    def audit_events(self) -> tuple[dict[str, object], ...]:
        connection = self._read_connection()
        if connection is None:
            return ()
        try:
            rows = connection.execute(
                "SELECT revision, operation, decision_id, decision_type, symbol, "
                "result, occurred_at FROM audit ORDER BY id"
            ).fetchall()
            return tuple(
                {
                    "revision": row[0],
                    "operation": row[1],
                    "decision_id": row[2],
                    "decision_type": row[3],
                    "symbol": row[4],
                    "result": row[5],
                    "occurred_at": row[6],
                }
                for row in rows
            )
        finally:
            connection.close()


def _read_revision(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
    return row[0] if row else ""


def _record_from_row(row: tuple[object, ...]) -> DecisionRecord:
    return DecisionRecord(
        decision_id=str(row[0]),
        decision_type=str(row[1]),
        symbol=row[2] if row[2] is None else str(row[2]),
        decision_date=str(row[3]),
        rationale=str(row[4]),
        note=row[5] if row[5] is None else str(row[5]),
        source=str(row[6]),
        revert_of=row[7] if row[7] is None else str(row[7]),
        recorded_at=str(row[8]),
        reverted_by=row[9] if row[9] is None else str(row[9]),
    )
