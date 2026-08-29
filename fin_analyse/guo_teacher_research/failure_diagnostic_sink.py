"""Owner-only consultation failure diagnostic sink.

Public ``ConsultationResult`` intentionally exposes only sanitized problems.
This module persists enough private, typed evidence for an operator to
correlate consultation-layer failures (subject binding rejections, product
finalization rejections) without persisting a prompt, question text, payload
content, credential or ticker detail.

Contract (roadmap v3 §0.1, rebaselined by user 2026-08-15):

- Idempotency key ``(conversation_turn_idempotency_key, stage, error_code)``;
  ``chain_id``/``generation`` are optional context fields and never part of
  the key.
- Exactly-one per key: publish via temp + fsync + readback + rename
  NOREPLACE; EEXIST triggers a readback check — an existing valid entry or
  tombstone with the same key skips silently, anything else is a sink
  failure.
- Live entries capped at 5000 (oldest-first by ``(sink_written_at, digest)``,
  removed BEFORE the new write under the process lock); entries whose mtime
  is older than 30 days are renamed to ``.tombstone`` markers that keep the
  key identity — a later replay of the same key is already recorded and does
  not resurrect as a fresh entry. Tombstones are swept after 60 days.
- Read-time expiry is authoritative and complete: operators see only
  entries younger than the TTL regardless of file presence (the tombstone
  rename is an eventual per-write sweep).
- Any sink failure (ENOSPC/EACCES/…) never changes the public error
  code/error_id/response/deadline; it only increments a thread-safe
  in-process no-content counter and returns.
- Every field is validated (name AND value domain) before any I/O.
- 16 KiB = final UTF-8 bytes; larger payloads are rejected before any I/O.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fin_analyse.common import owner_only_collection as collection
from fin_analyse.common.owner_only_collection import OwnerOnlyCollectionError

SCHEMA_VERSION = "fin.consultation-failure-diagnostic/v1"
MAX_ENTRY_BYTES = 16 * 1024
MAX_LIVE_ENTRIES = 5000
MAX_TOMBSTONES = 5000
ENTRY_TTL = timedelta(days=30)
TOMBSTONE_TTL = timedelta(days=60)
TEMP_SWEEP_TTL = timedelta(days=1)

# 幂等键可参与字段与值域（任何 I/O 前校验，越域即拒）。
ALLOWLIST = frozenset(
    {
        "schema_version",
        "conversation_turn_key",
        "stage",
        "error_code",
        "error_id",
        "chain_id",
        "generation",
        "subject_count",
        "subject_kind",
        "question_char_len_bucket",
        "elapsed_ms",
        "deadline_ms",
        "sink_written_at",
        "payload_hash",
    }
)
_STAGES = frozenset({"binding", "finalization"})
# 真实逐轮身份：transport_envelope.conversation_turn_idempotency_key 的
# canonical 形状（A5L-3：machine-derived，绝不由 prompt/LLM 输入派生）。
_TURN_KEY_RE = re.compile(r"^fin\.turn-idempotency/v1:[0-9a-f]{64}$")
_CHAIN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_ERROR_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,79}$")
_ERROR_ID_RE = re.compile(r"^err_[0-9a-f]{32}$")
_PAYLOAD_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_QUESTION_LENGTH_BUCKETS = frozenset({"le_100", "le_500", "le_2000", "le_8000", "gt_8000"})
_SUBJECT_KINDS = frozenset({"single_asset", "multi_asset", "portfolio"})
_LIVE_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")
_TOMBSTONE_NAME_RE = re.compile(r"^[0-9a-f]{64}\.tombstone$")
_TEMP_NAME_RE = re.compile(r"^[0-9a-f]{64}\.[0-9a-f]{32}\.tmp$")

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

_SINK_LOCK = threading.RLock()
_COUNTER_LOCK = threading.Lock()
_sink_failure_count = 0
_SWEEP_BATCH = 64
_sweep_cursors: dict[str, int] = {}


def sink_failure_count() -> int:
    """Current in-process no-content failure count (test/operator seam)."""
    with _COUNTER_LOCK:
        return _sink_failure_count


def reset_sink_failure_count() -> None:
    """Reset the in-process failure count (tests only)."""
    global _sink_failure_count
    with _COUNTER_LOCK:
        _sink_failure_count = 0


def _count_failure() -> None:
    global _sink_failure_count
    with _COUNTER_LOCK:
        _sink_failure_count += 1


def default_sink_root() -> Path:
    """Default owner-only diagnostics root under ``$XDG_STATE_HOME``."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "fin-analyse" / "consultation-failure-diagnostics"


def canonical_payload_hash(candidate: object) -> str | None:
    """SHA-256 over canonical UTF-8 JSON of a rejected candidate payload.

    NaN/Inf are not JSON-representable: the hash is omitted (None) and the
    diagnostic is still recorded.
    """
    try:
        encoded = json.dumps(
            candidate,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _entry_key(turn_key: str, stage: str, error_code: str) -> str:
    canonical = "\x1f".join((turn_key, stage, error_code))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_valid_turn_key(value: object) -> bool:
    """True only for the canonical A5L-3 machine turn key shape."""
    return isinstance(value, str) and _TURN_KEY_RE.fullmatch(value) is not None


def _optional_text_value(value: object) -> str | None:
    """Exact-type gate for EEXIST readback payloads；异类型即拒。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("payload field type mismatch")
    return value


def _optional_int_value(value: object) -> int | None:
    """Exact-type gate for EEXIST readback payloads；异类型/布尔即拒。"""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("payload field type mismatch")
    return value


def _optional_text(name: str, value: object, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} out of domain")
    return value


def _optional_int(name: str, value: object, *, lo: int, hi: int) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValueError(f"{name} out of domain")
    return value


def _validate_fields(
    *,
    conversation_turn_key: str,
    stage: str,
    error_code: str,
    error_id: str | None,
    chain_id: str | None,
    generation: int | None,
    subject_count: int | None,
    subject_kind: str | None,
    question_char_len_bucket: str | None,
    elapsed_ms: int | None,
    deadline_ms: int | None,
    payload_hash: str | None,
) -> dict[str, object]:
    if not isinstance(conversation_turn_key, str) or not _TURN_KEY_RE.fullmatch(
        conversation_turn_key
    ):
        raise ValueError("conversation_turn_key out of domain")
    if stage not in _STAGES:
        raise ValueError("stage out of domain")
    if not isinstance(error_code, str) or not _ERROR_CODE_RE.fullmatch(error_code):
        raise ValueError("error_code out of domain")
    entry: dict[str, object] = {
        "conversation_turn_key": conversation_turn_key,
        "stage": stage,
        "error_code": error_code,
    }
    optional_text = (
        _optional_text("error_id", error_id, _ERROR_ID_RE),
        _optional_text("chain_id", chain_id, _CHAIN_ID_RE),
        _optional_text("payload_hash", payload_hash, _PAYLOAD_HASH_RE),
    )
    for name, value in (
        ("error_id", optional_text[0]),
        ("chain_id", optional_text[1]),
        ("payload_hash", optional_text[2]),
    ):
        if value is not None:
            entry[name] = value
    for name, numeric_value, lo, hi in (
        ("generation", generation, 0, 2**31 - 1),
        ("subject_count", subject_count, 0, 500),
        ("elapsed_ms", elapsed_ms, 0, 10**9),
        ("deadline_ms", deadline_ms, 0, 10**13),
    ):
        bounded = _optional_int(name, numeric_value, lo=lo, hi=hi)
        if bounded is not None:
            entry[name] = bounded
    if subject_kind is not None:
        if subject_kind not in _SUBJECT_KINDS:
            raise ValueError("subject_kind out of domain")
        entry["subject_kind"] = subject_kind
    if question_char_len_bucket is not None:
        if question_char_len_bucket not in _QUESTION_LENGTH_BUCKETS:
            raise ValueError("question_char_len_bucket out of domain")
        entry["question_char_len_bucket"] = question_char_len_bucket
    return entry


def _encode_entry(entry: Mapping[str, object]) -> bytes:
    encoded = json.dumps(
        entry,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_ENTRY_BYTES:
        raise ValueError("entry exceeds byte bound")
    return encoded


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("diagnostic write failed")
        view = view[written:]


def _entry_matches(payload: Mapping[str, object], digest: str) -> bool:
    """EEXIST readback gate：既有条目必须整体合法且同键才算 exactly-one。

    只查三个键字段会让带额外字段/损坏值域的 owner-file 永久抑制真实诊断；
    这里对既有 payload 走与写入完全相同的全量校验。
    """
    if payload.get("schema_version") != SCHEMA_VERSION:
        return False
    turn_key = payload.get("conversation_turn_key")
    stage = payload.get("stage")
    error_code = payload.get("error_code")
    if (
        not isinstance(turn_key, str)
        or not isinstance(stage, str)
        or not isinstance(error_code, str)
    ):
        return False
    written_at = payload.get("sink_written_at")
    if not isinstance(written_at, str):
        return False
    try:
        datetime.fromisoformat(written_at)
    except ValueError:
        return False
    try:
        validated = _validate_fields(
            conversation_turn_key=turn_key,
            stage=stage,
            error_code=error_code,
            error_id=_optional_text_value(payload.get("error_id")),
            chain_id=_optional_text_value(payload.get("chain_id")),
            generation=_optional_int_value(payload.get("generation")),
            subject_count=_optional_int_value(payload.get("subject_count")),
            subject_kind=_optional_text_value(payload.get("subject_kind")),
            question_char_len_bucket=_optional_text_value(
                payload.get("question_char_len_bucket")
            ),
            elapsed_ms=_optional_int_value(payload.get("elapsed_ms")),
            deadline_ms=_optional_int_value(payload.get("deadline_ms")),
            payload_hash=_optional_text_value(payload.get("payload_hash")),
        )
    except (TypeError, ValueError):
        return False
    if any(key not in ALLOWLIST for key in payload):
        return False
    validated_keys = set(validated)
    if any(
        key not in validated_keys
        for key in payload
        if key not in {"schema_version", "sink_written_at"}
    ):
        return False
    return _entry_key(turn_key, stage, error_code) == digest


def _sweep(directory_fd: int, *, now: datetime, root_key: str) -> None:
    """Tombstone expired entries, sweep old tombstones/temps, then enforce cap.

    TTL is judged by the entry's own ``sink_written_at`` (never filesystem
    mtime); expired live entries are replaced by content-free ``.tombstone``
    identity markers (≤256B, never readable as diagnostics).  Every
    rename/unlink only touches files that passed the owner-only verification
    (regular/0600/euid/nlink=1/identity); unverifiable names count toward
    the cap but are never read or removed.  Cap enforcement is exact: when
    the name count reaches the cap the oldest live entries are removed
    BEFORE the new publish, so ``MAX_LIVE_ENTRIES`` is a hard bound.
    """
    live_names: list[str] = []
    tombstone_names: list[str] = []
    temp_names: list[str] = []
    foreign_count = 0
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            name = entry.name
            if _LIVE_NAME_RE.fullmatch(name):
                live_names.append(name)
            elif _TOMBSTONE_NAME_RE.fullmatch(name):
                tombstone_names.append(name)
            elif _TEMP_NAME_RE.fullmatch(name):
                temp_names.append(name)
            else:
                foreign_count += 1
    cursor = _sweep_cursors.get(root_key, 0)
    live_names.sort()
    tombstone_names.sort()
    temp_names.sort()
    _rotate_live_sweep(live_names, cursor, directory_fd, now)
    _rotate_tombstone_sweep(tombstone_names, cursor, directory_fd, now)
    verified_temps: list[tuple[datetime, str]] = []
    for name in temp_names:
        metadata = _verified_stat(directory_fd, name)
        if metadata is None:
            foreign_count += 1  # 未验证 temp：占配额、不读、不删
            continue
        if _mtime(metadata) < now - TEMP_SWEEP_TTL:
            os.unlink(name, dir_fd=directory_fd)
        else:
            verified_temps.append((_mtime(metadata), name))
    if len(verified_temps) > MAX_LIVE_ENTRIES:
        # 崩溃残留 temp 硬封顶：按 mtime 删最旧。
        verified_temps.sort(key=lambda pair: pair[0])
        for _, name in verified_temps[: len(verified_temps) - MAX_LIVE_ENTRIES]:
            os.unlink(name, dir_fd=directory_fd)
    _sweep_cursors[root_key] = cursor + _SWEEP_BATCH
    if len(live_names) + foreign_count < MAX_LIVE_ENTRIES:
        return
    ordered: list[tuple[str, str]] = []
    for name in live_names:
        try:
            raw = collection.read_owner_regular(
                directory_fd, name, max_bytes=MAX_ENTRY_BYTES
            )
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, OwnerOnlyCollectionError, UnicodeDecodeError, ValueError):
            # 不可验证/不可解析条目按 foreign 处理（不可删，占配额）。
            continue
        written_at = payload.get("sink_written_at")
        if not isinstance(written_at, str):
            continue
        ordered.append((written_at, name))
    ordered.sort(key=lambda pair: (pair[0], pair[1]))
    to_remove = len(live_names) + foreign_count - MAX_LIVE_ENTRIES + 1
    if len(ordered) < to_remove:
        raise OSError("capacity cleanup failed")
    for _, name in ordered[:to_remove]:
        os.unlink(name, dir_fd=directory_fd)


def _verified_stat(directory_fd: int, name: str) -> os.stat_result | None:
    """owner-only 验证 + stat：任何违例（symlink/hardlink/权限/身份）返回 None。"""
    try:
        return collection.verify_owner_regular(
            directory_fd, name, max_bytes=MAX_ENTRY_BYTES
        )
    except (OSError, OwnerOnlyCollectionError):
        return None


def _mtime(metadata: os.stat_result) -> datetime:
    return datetime.fromtimestamp(metadata.st_mtime_ns / 1e9, tz=UTC)


def _write_tombstone(directory_fd: int, name: str) -> None:
    """Content-free identity marker：空文件，0600，绝不携带诊断字段。"""
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _rotate_live_sweep(
    names: list[str],
    cursor: int,
    directory_fd: int,
    now: datetime,
) -> None:
    if not names:
        return
    for offset in range(_SWEEP_BATCH):
        index = (cursor + offset) % len(names)
        name = names[index]
        try:
            raw = collection.read_owner_regular(
                directory_fd, name, max_bytes=MAX_ENTRY_BYTES
            )
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, OwnerOnlyCollectionError, UnicodeDecodeError, ValueError):
            continue  # 未通过验证的条目：不读、不删，占配额
        written_at = payload.get("sink_written_at")
        if not isinstance(written_at, str):
            continue
        try:
            written = datetime.fromisoformat(written_at)
        except ValueError:
            continue
        if now - written <= ENTRY_TTL:
            continue
        tombstone_name = f"{name[:64]}.tombstone"
        with suppress(FileExistsError):  # tombstone 已保留同一键身份
            _write_tombstone(directory_fd, tombstone_name)
        os.unlink(name, dir_fd=directory_fd)


def _rotate_tombstone_sweep(
    names: list[str],
    cursor: int,
    directory_fd: int,
    now: datetime,
) -> None:
    if not names:
        return
    if len(names) > MAX_TOMBSTONES:
        # tombstone 硬封顶：完整判定，按 mtime 删最旧（marker 无字段内容）。
        aged: list[tuple[datetime, str]] = []
        for name in names:
            metadata = _verified_stat(directory_fd, name)
            if metadata is not None:
                aged.append((_mtime(metadata), name))
        aged.sort(key=lambda pair: pair[0])
        for _, name in aged[: len(names) - MAX_TOMBSTONES]:
            os.unlink(name, dir_fd=directory_fd)
        return
    for offset in range(_SWEEP_BATCH):
        index = (cursor + offset) % len(names)
        name = names[index]
        metadata = _verified_stat(directory_fd, name)
        if metadata is None:
            continue
        if _mtime(metadata) < now - TOMBSTONE_TTL:
            os.unlink(name, dir_fd=directory_fd)


def _publish(directory_fd: int, digest: str, encoded: bytes) -> None:
    """Publish one entry with record-level atomicity (temp+fsync+readback).

    The tombstone check runs first: a tombstone with the same key means the
    occurrence is already recorded and must not be written again.
    """
    try:
        collection.verify_owner_regular(
            directory_fd,
            f"{digest}.tombstone",
            max_bytes=MAX_ENTRY_BYTES,
        )
        return  # 同键已被已验证 tombstone 记录（旧身份不得复活）
    except (OSError, OwnerOnlyCollectionError):
        pass  # 无已验证 tombstone：继续 publish，EEXIST 回读兜底
    temporary = f"{digest}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        verified = collection.read_owner_regular(
            directory_fd, temporary, max_bytes=MAX_ENTRY_BYTES
        )
        if verified != encoded:
            raise OSError("temporary diagnostic verification failed")
        try:
            collection.rename_noreplace(
                directory_fd, temporary, directory_fd, f"{digest}.json"
            )
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise
            existing = collection.read_owner_regular(
                directory_fd, f"{digest}.json", max_bytes=MAX_ENTRY_BYTES
            )
            try:
                payload = json.loads(existing.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as decode_error:
                raise OSError("existing diagnostic unreadable") from decode_error
            if not _entry_matches(payload, digest):
                raise OSError("existing diagnostic key mismatch") from None
            # exactly-one：同键已有合法条目——本临时文件清理后静默跳过。
            os.unlink(temporary, dir_fd=directory_fd)
            return
        os.fsync(directory_fd)
    except OSError:
        with suppress(OSError):
            os.unlink(temporary, dir_fd=directory_fd)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def record_failure_diagnostic(
    *,
    sink_root: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    conversation_turn_key: str,
    stage: Literal["binding", "finalization"],
    error_code: str,
    error_id: str | None = None,
    chain_id: str | None = None,
    generation: int | None = None,
    subject_count: int | None = None,
    subject_kind: str | None = None,
    question_char_len_bucket: str | None = None,
    elapsed_ms: int | None = None,
    deadline_ms: int | None = None,
    payload_hash: str | None = None,
) -> None:
    """Record one failure diagnostic. Never raises, never changes the caller.

    ``sink_written_at`` is always sink-generated.  Any failure — validation,
    capacity, or any filesystem error — increments the no-content counter
    and returns; the public error code/error_id/response/deadline of the
    main chain are untouched by construction.
    """
    try:
        entry = _validate_fields(
            conversation_turn_key=conversation_turn_key,
            stage=stage,
            error_code=error_code,
            error_id=error_id,
            chain_id=chain_id,
            generation=generation,
            subject_count=subject_count,
            subject_kind=subject_kind,
            question_char_len_bucket=question_char_len_bucket,
            elapsed_ms=elapsed_ms,
            deadline_ms=deadline_ms,
            payload_hash=payload_hash,
        )
        active_clock = clock or (lambda: datetime.now(UTC))
        entry["schema_version"] = SCHEMA_VERSION
        written = active_clock()
        if written.tzinfo is None:
            written = written.replace(tzinfo=UTC)
        entry["sink_written_at"] = written.astimezone(UTC).isoformat(
            timespec="microseconds"
        )
        digest = _entry_key(conversation_turn_key, stage, error_code)
        encoded = _encode_entry(entry)
    except (TypeError, ValueError):
        _count_failure()
        return
    root = Path(sink_root) if sink_root is not None else default_sink_root()
    try:
        with _SINK_LOCK:
            directory_fd = collection.require_owner_directory(root, create=True)
            try:
                _sweep(directory_fd, now=active_clock(), root_key=str(root))
                _publish(directory_fd, digest, encoded)
            finally:
                os.close(directory_fd)
    except (OSError, OwnerOnlyCollectionError, ValueError):
        _count_failure()
        return


def record_stage_failure(
    *,
    sink_root: Path | None,
    stage: Literal["binding", "finalization"],
    turn_key: str | None,
    error_code: str | None,
    candidate: object | None = None,
    chain_id: str | None = None,
    generation: int | None = None,
) -> None:
    """Rejection-site convenience: no-op unless composition configured a root.

    The subject count and payload hash are derived from the rejected candidate
    when it is a mapping; both are optional and never block the diagnostic.
    """
    if sink_root is None or turn_key is None or error_code is None:
        return
    subject_count: int | None = None
    if isinstance(candidate, Mapping):
        binding = candidate.get("context_binding")
        subjects = binding.get("subjects") if isinstance(binding, Mapping) else None
        if subjects is not None and not isinstance(subjects, (str, bytes)):
            try:
                subject_count = len(subjects)  # type: ignore[arg-type]
            except TypeError:
                subject_count = None
    record_failure_diagnostic(
        sink_root=sink_root,
        conversation_turn_key=turn_key,
        stage=stage,
        error_code=error_code,
        subject_count=subject_count,
        payload_hash=canonical_payload_hash(candidate) if candidate is not None else None,
        chain_id=chain_id,
        generation=generation,
    )


def list_recent_failures(
    sink_root: Path | None = None,
    *,
    limit: int = 100,
    clock: Callable[[], datetime] | None = None,
) -> list[dict[str, object]]:
    """Operator read: live entries younger than the TTL, newest first.

    Read-time expiry is authoritative: entries whose ``sink_written_at`` is
    older than the TTL are refused even if the file is still present, and
    tombstones are never readable as diagnostics.
    """
    root = Path(sink_root) if sink_root is not None else default_sink_root()
    active_clock = clock or (lambda: datetime.now(UTC))
    now = active_clock()
    try:
        directory_fd = collection.require_owner_directory(root, create=False)
    except (OSError, OwnerOnlyCollectionError):
        return []
    entries: list[dict[str, object]] = []
    try:
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                if not _LIVE_NAME_RE.fullmatch(entry.name):
                    continue
                try:
                    raw = collection.read_owner_regular(
                        directory_fd, entry.name, max_bytes=MAX_ENTRY_BYTES
                    )
                    payload = json.loads(raw.decode("utf-8"))
                except (OSError, OwnerOnlyCollectionError, UnicodeDecodeError, ValueError):
                    continue  # 不可验证条目永不呈现
                written_at = payload.get("sink_written_at")
                if not isinstance(written_at, str):
                    continue
                try:
                    written = datetime.fromisoformat(written_at)
                except ValueError:
                    continue
                if now - written > ENTRY_TTL:
                    continue  # read-time expiry gate
                entries.append(payload)
    finally:
        os.close(directory_fd)
    entries.sort(key=lambda item: str(item.get("sink_written_at", "")), reverse=True)
    return entries[:limit]


__all__ = [
    "ALLOWLIST",
    "ENTRY_TTL",
    "MAX_ENTRY_BYTES",
    "MAX_LIVE_ENTRIES",
    "SCHEMA_VERSION",
    "canonical_payload_hash",
    "default_sink_root",
    "is_valid_turn_key",
    "list_recent_failures",
    "record_failure_diagnostic",
    "record_stage_failure",
    "reset_sink_failure_count",
    "sink_failure_count",
]
