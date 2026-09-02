"""ZSXQ Windows-native capture artifact → WSL ingest 手工入口。

Windows canonical artifact 始终是不可变 mailbox 输入。本入口在稳定源目录锁下
做同一 fd 的有界读取，将 raw create-only stage 到 canonical runtime owner 的
``capture-recovery-v1``，再恢复或执行 ledger/KB/G 主链。终态先发布 archived raw，
再发布 receipt marker，最后删除 stage；因此已冻结的 kill point 可精确重放。
wire 形状与 ``fin.zsxq-scheduled-run/v3`` 同构。
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .capture_artifact import (
    MAX_CAPTURE_ARTIFACT_BYTES,
    CaptureArtifact,
    CaptureArtifactError,
    parse_capture_artifact,
)
from .cdp_runtime import ProductionCdpCompletionReceipt, run_capture_ingest_once
from .contracts import ZsxqRunRequest
from .runtime_repository import (
    CAPTURE_RECOVERY_COMPLETION_SCHEMA_VERSION,
    CaptureIdentityConflictError,
    CaptureIngestRecord,
    ScraperRuntimeRepository,
    decode_capture_completion_projection,
)
from .scheduled_run import DEFAULT_RUNTIME_DB
from .scheduler_handoff_lock import (
    HandoffLockMode,
    SchedulerHandoffLockBusyError,
    SchedulerHandoffLockError,
    hold_scheduler_handoff_lock,
    scheduler_handoff_lock_path,
)
from .zsxq_stability import build_capture_ingest_audit

SCHEMA_VERSION = "fin.zsxq-capture-ingest/v1"
CONSUMED_RECEIPT_SCHEMA_VERSION = "fin.zsxq-capture-consumed/v1"
REJECTED_RECEIPT_SCHEMA_VERSION = "fin.zsxq-capture-rejected/v1"
CAPTURE_RECOVERY_DIRECTORY_NAME = "capture-recovery-v1"
MAX_CAPTURE_RECEIPT_BYTES = 2 * 1024 * 1024
_CAPTURE_RECOVERY_OWNER_SCHEMA_VERSION = "fin.zsxq-capture-recovery-owner/v1"
_CAPTURE_RECOVERY_OWNER_FILE = "runtime-owner.json"

_EXIT_INVALID_REQUEST = 64
_EXIT_INTERNAL_ERROR = 70
_EXIT_COALESCED = 75
_EXIT_BY_RUN_FAILURE = {
    "failed": 1,
    "deadline_exceeded": 2,
    "interrupted": 3,
}


class _CaptureHandoffLockBusyError(RuntimeError):
    pass


class _CaptureHandoffLockError(RuntimeError):
    pass


class _CaptureArchiveConflictError(RuntimeError):
    pass


class _CaptureRecoveryOwnerConflictError(RuntimeError):
    pass


def _rename_noreplace_at(directory_fd: int, source_name: str, target_name: str) -> None:
    """Atomically move one name only while the destination is absent (Linux/WSL)."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    result = renameat2(
        ctypes.c_int(directory_fd),
        ctypes.c_char_p(os.fsencode(source_name)),
        ctypes.c_int(directory_fd),
        ctypes.c_char_p(os.fsencode(target_name)),
        ctypes.c_uint(1),  # RENAME_NOREPLACE
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, os.strerror(error_number), target_name)
        raise OSError(error_number, os.strerror(error_number), target_name)


@dataclass(frozen=True)
class _BoundFile:
    raw: bytes
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class _StagedArtifact:
    name: str
    artifact: CaptureArtifact
    raw: bytes


def _require_bound_capture_handoff(descriptor: int, parent: Path) -> None:
    try:
        opened = os.fstat(descriptor)
        named = parent.stat(follow_symlinks=False)
    except OSError as error:
        raise _CaptureHandoffLockError from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise _CaptureHandoffLockError


@contextmanager
def _hold_capture_handoff_lock(artifact_path: Path) -> Iterator[int]:
    """Serialize importers by stable handoff directory, not replaceable artifact inode."""
    parent = artifact_path.parent
    try:
        descriptor = os.open(
            parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise _CaptureHandoffLockError from error

    try:
        _require_bound_capture_handoff(descriptor, parent)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise _CaptureHandoffLockBusyError from error
            raise _CaptureHandoffLockError from error
        _require_bound_capture_handoff(descriptor, parent)
        yield descriptor
        _require_bound_capture_handoff(descriptor, parent)
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise _CaptureHandoffLockError from error


def _default_knowledge_base_root() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "fin-analyse" / "shared" / "knowledge-base"


def capture_recovery_root_path(runtime_db: Path) -> Path:
    """Return importer-owned payload storage beside the canonical runtime ledger."""
    return scheduler_handoff_lock_path(runtime_db).parent / CAPTURE_RECOVERY_DIRECTORY_NAME


def capture_runtime_owner_id(runtime_db: Path) -> str:
    """Return a domain-separated identity for the one ledger owning recovery payloads."""
    scheduler_handoff_lock_path(runtime_db)
    identity = b"fin.zsxq-capture-runtime-owner/v1\0" + os.fsencode(runtime_db)
    return hashlib.sha256(identity).hexdigest()


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _capture_prior_g_json(knowledge_base_root: Path) -> str:
    """Freeze bounded pre-run G evidence for NO_CHANGE recovery."""
    try:
        from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService

        assessment = GWorkingSetService(kb_root=knowledge_base_root).evaluate()
        return _canonical_json(assessment.to_publication_evidence().to_dict())
    except Exception:
        return "{}"


def _validate_runtime_db(path: Path) -> str | None:
    if not path.is_absolute():
        return "runtime_db_must_be_absolute"
    if path.exists() and (path.is_symlink() or not path.is_file()):
        return "runtime_db_must_be_regular_file"
    parent = path.parent
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        return "runtime_parent_must_be_directory"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    if path.exists():
        path.chmod(0o600)
    return None


def _validate_knowledge_base_root(path: Path) -> str | None:
    if not path.is_absolute():
        return "knowledge_base_root_must_be_absolute"
    if path.is_symlink() or not path.is_dir():
        return "knowledge_base_root_must_be_directory"
    index_file = path / "index.json"
    if index_file.is_symlink() or not index_file.is_file():
        return "knowledge_base_index_missing"
    return None


def _projection(
    completion: ProductionCdpCompletionReceipt,
    *,
    artifact_run_id: str,
    artifact_file: str,
    artifact_sha256: str,
) -> dict[str, object]:
    result = completion.run
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": result.status,
        "completion_status": completion.verified_completion_status(),
        "completion_data_gaps": list(completion.verified_completion_data_gaps()),
        "artifact": {
            "run_id": artifact_run_id,
            "file": artifact_file,
            "content_sha256": artifact_sha256,
        },
        "intent": result.intent,
        "trigger": result.trigger,
        "coalesced": result.coalesced,
    }
    if completion.g_working_set is not None:
        payload["g_working_set"] = completion.g_working_set.to_dict()
    for name in (
        "run_id",
        "active_run_id",
        "changed_count",
        "attempt",
        "started_at",
        "finished_at",
        "failure_reason",
    ):
        value = getattr(result, name, None)
        if value is not None:
            payload[name] = value
    return payload


def _read_index_articles_for_audit(knowledge_base_root: Path) -> list[dict[str, object]] | None:
    """Read only the bounded index projection needed for the consumed receipt."""
    try:
        raw = (knowledge_base_root / "index.json").read_bytes()
        payload = json.loads(raw)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    articles = payload.get("articles") if isinstance(payload, Mapping) else None
    if not isinstance(articles, list):
        return None
    result: list[dict[str, object]] = []
    for article in articles:
        if not isinstance(article, Mapping):
            return None
        result.append(dict(article))
    return result


def _completion_exit_code(completion: ProductionCdpCompletionReceipt) -> int:
    run_failure = _EXIT_BY_RUN_FAILURE.get(completion.run.status)
    if run_failure is not None:
        return run_failure
    completion_status = completion.verified_completion_status()
    if completion_status == "coalesced":
        return _EXIT_COALESCED
    if completion_status == "ready":
        return 0
    if completion_status == "partial":
        return 4
    return 1


def _resume_completed_capture(
    record: CaptureIngestRecord,
    *,
    recovery_fd: int,
    source_name: str,
    staged: _StagedArtifact,
) -> tuple[int, dict[str, object]]:
    if record.completion_json is None:
        raise RuntimeError("completed capture ingest has no completion projection")
    if record.ingest_run_id is None or record.business_json is None:
        raise RuntimeError("completed capture ingest business projection is missing")
    decoded = decode_capture_completion_projection(
        record.completion_json,
        artifact_run_id=record.artifact_run_id,
        content_sha256=record.content_sha256,
        ingest_run_id=record.ingest_run_id,
        business_json=record.business_json,
        prior_g_json=record.prior_g_json,
        publication_plan_json=record.publication_plan_json,
    )
    disposition = str(decoded["archive_disposition"])
    receipt = decoded.get("receipt")
    if not isinstance(receipt, dict):
        raise RuntimeError("completed capture ingest receipt is invalid")
    already_published = False
    if disposition == "consumed":
        already_published = _completion_archive_is_exact(
            recovery_fd=recovery_fd,
            receipt=receipt,
            artifact_run_id=record.artifact_run_id,
            content_sha256=record.content_sha256,
        )
        warning = _publish_completion_archive(
            recovery_fd,
            directory_name="consumed",
            receipt=receipt,
            expected_sha256=record.content_sha256,
            staged=staged,
        )
    else:
        warning = _publish_completion_archive(
            recovery_fd,
            directory_name="rejected",
            receipt=receipt,
            expected_sha256=record.content_sha256,
            staged=staged,
        )
    payload_value = decoded["payload"]
    exit_code = decoded["exit_code"]
    if not isinstance(payload_value, dict) or type(exit_code) is not int:
        raise RuntimeError("completed capture ingest projection is invalid")
    payload = dict(payload_value)
    if warning is not None:
        return _EXIT_INTERNAL_ERROR, {**payload, "archive_warning": warning}
    if already_published:
        return (
            _EXIT_INVALID_REQUEST,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "duplicate",
                "original_status": payload.get("status"),
                "original_completion_status": payload.get("completion_status"),
                "original_exit_code": exit_code,
                "artifact": {
                    "run_id": record.artifact_run_id,
                    "file": source_name,
                    "content_sha256": record.content_sha256,
                },
            },
        )
    return exit_code, payload


@dataclass(frozen=True)
class _Args:
    artifact: Path
    runtime_db: Path
    knowledge_base_root: Path
    trigger: str
    deadline_seconds: float


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="校验并导入一份 ZSXQ Windows-native capture artifact"
    )
    parser.add_argument("--artifact", type=Path, required=True)
    # The capture CLI has one canonical production ledger.  Keep parsing the
    # historical option only so an old invocation fails with a typed result
    # instead of silently creating a second exactly-once domain.
    parser.add_argument("--runtime-db", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--knowledge-base-root", type=Path, default=_default_knowledge_base_root())
    parser.add_argument(
        "--trigger",
        choices=("manual", "recovery", "schedule"),
        default="manual",
    )
    parser.add_argument("--deadline-seconds", type=float, default=1200.0)
    return parser


def _parse_args(
    argv: list[str] | None,
    *,
    canonical_runtime_db: Path,
) -> tuple[_Args | None, int | None]:
    args = _parser().parse_args(argv)
    artifact = args.artifact.expanduser()
    runtime_db = canonical_runtime_db.expanduser()
    kb_root = args.knowledge_base_root.expanduser()
    if not artifact.is_absolute():
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": "artifact_must_be_absolute",
            }
        )
        return None, _EXIT_INVALID_REQUEST
    if not runtime_db.is_absolute():
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": "runtime_db_must_be_absolute",
            }
        )
        return None, _EXIT_INVALID_REQUEST
    if args.runtime_db is not None and args.runtime_db.expanduser() != runtime_db:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": "runtime_db_not_canonical",
            }
        )
        return None, _EXIT_INVALID_REQUEST
    if not 30.0 <= args.deadline_seconds <= 3600.0:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": "deadline_out_of_range",
            }
        )
        return None, _EXIT_INVALID_REQUEST
    return (
        _Args(
            artifact=artifact,
            runtime_db=runtime_db,
            knowledge_base_root=kb_root,
            trigger=args.trigger,
            deadline_seconds=args.deadline_seconds,
        ),
        None,
    )


def _open_private_directory_at(parent_fd: int, name: str, *, create: bool) -> int:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("capture directory name is invalid")
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass

    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        _require_bound_private_directory_at(parent_fd, name, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_bound_private_directory_at(parent_fd: int, name: str, descriptor: int) -> None:
    """Require one owner-only dirfd to remain bound to its canonical name."""
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise ValueError("capture directory identity is unavailable") from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        or opened.st_uid != os.geteuid()
        or named.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o700
        or stat.S_IMODE(named.st_mode) != 0o700
    ):
        raise ValueError("capture directory identity drifted")


@contextmanager
def _hold_capture_recovery_root(runtime_db: Path) -> Iterator[int]:
    """Hold the Linux owner-only payload root paired with the runtime ledger."""
    root = capture_recovery_root_path(runtime_db)
    parent_fd = os.open(
        root.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    recovery_fd = -1
    try:
        recovery_fd = _open_private_directory_at(
            parent_fd,
            root.name,
            create=True,
        )
        owner_raw = (
            _canonical_json(
                {
                    "schema_version": _CAPTURE_RECOVERY_OWNER_SCHEMA_VERSION,
                    "runtime_owner_id": capture_runtime_owner_id(runtime_db),
                }
            )
            + "\n"
        ).encode("utf-8")
        try:
            existing_owner = _read_recovery_file_at(
                recovery_fd,
                _CAPTURE_RECOVERY_OWNER_FILE,
                max_bytes=4096,
            )
            if existing_owner is None:
                owner_temp_name = f".{_CAPTURE_RECOVERY_OWNER_FILE}.tmp"
                names = set(os.listdir(recovery_fd))
                if not names.issubset({owner_temp_name}):
                    raise ValueError("ownerless capture recovery root is not empty")
            _publish_create_only_at(
                recovery_fd,
                _CAPTURE_RECOVERY_OWNER_FILE,
                owner_raw,
                max_bytes=4096,
            )
        except ValueError as error:
            raise _CaptureRecoveryOwnerConflictError from error
        yield recovery_fd
        _require_bound_private_directory_at(parent_fd, root.name, recovery_fd)
    finally:
        if recovery_fd >= 0:
            os.close(recovery_fd)
        os.close(parent_fd)


def _read_bound_file_at(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int,
    required_mode: int | None,
) -> _BoundFile | None:
    """Read one stable fd snapshot; source path replacement is intentionally allowed."""
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("capture file name is invalid")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("capture file is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 0
            or before.st_size > max_bytes
            or (
                required_mode is not None
                and (
                    before.st_uid != os.geteuid()
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != required_mode
                )
            )
        ):
            raise ValueError("capture file is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise ValueError("capture file is truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("capture file grew while reading")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise ValueError("capture file identity drifted")
        return _BoundFile(
            raw=b"".join(chunks),
            device=before.st_dev,
            inode=before.st_ino,
            size=before.st_size,
            modified_ns=before.st_mtime_ns,
        )
    finally:
        os.close(descriptor)


def _read_recovery_file_at(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int,
) -> _BoundFile | None:
    """Read an owner-only recovery file and bind its fd to the current name."""
    bound = _read_bound_file_at(
        directory_fd,
        name,
        max_bytes=max_bytes,
        required_mode=0o600,
    )
    if bound is None:
        return None
    try:
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise ValueError("capture recovery file identity drifted") from error
    if (
        named.st_dev,
        named.st_ino,
        named.st_size,
        named.st_mtime_ns,
    ) != (
        bound.device,
        bound.inode,
        bound.size,
        bound.modified_ns,
    ):
        raise ValueError("capture recovery file identity drifted")
    return bound


def read_archived_capture_receipt_pairs(
    runtime_db: str | Path,
    receipt_paths: Sequence[str | Path],
) -> list[dict[str, object]]:
    """Read canonical raw+marker pairs from one owner-bound recovery root."""
    runtime_path = Path(runtime_db).expanduser()
    paths = tuple(Path(path).expanduser() for path in receipt_paths)
    root_fd = -1
    consumed_fd = -1
    parent_fd = -1
    try:
        if not runtime_path.is_absolute() or not paths:
            raise ValueError("capture archive paths are invalid")
        recovery_root = capture_recovery_root_path(runtime_path)
        consumed_root = recovery_root / "consumed"
        parent_fd = os.open(
            recovery_root.parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        root_fd = _open_private_directory_at(
            parent_fd,
            recovery_root.name,
            create=False,
        )
        owner = _read_recovery_file_at(
            root_fd,
            _CAPTURE_RECOVERY_OWNER_FILE,
            max_bytes=4096,
        )
        if owner is None:
            raise ValueError("capture recovery owner is missing")
        expected_owner_raw = (
            _canonical_json(
                {
                    "schema_version": _CAPTURE_RECOVERY_OWNER_SCHEMA_VERSION,
                    "runtime_owner_id": capture_runtime_owner_id(runtime_path),
                }
            )
            + "\n"
        ).encode("utf-8")
        if owner.raw != expected_owner_raw:
            raise ValueError("capture recovery owner conflicts")
        consumed_fd = _open_private_directory_at(root_fd, "consumed", create=False)

        receipts: list[dict[str, object]] = []
        for receipt_path in paths:
            if not receipt_path.is_absolute() or receipt_path.parent != consumed_root:
                raise ValueError("capture receipt is outside the canonical archive")
            marker = _read_recovery_file_at(
                consumed_fd,
                receipt_path.name,
                max_bytes=MAX_CAPTURE_RECEIPT_BYTES,
            )
            if marker is None:
                raise ValueError("capture receipt marker is missing")
            receipt = json.loads(marker.raw)
            if not isinstance(receipt, dict):
                raise ValueError("capture receipt marker is invalid")
            if marker.raw != (_canonical_json(receipt) + "\n").encode("utf-8"):
                raise ValueError("capture receipt marker is not canonical")
            run_id = receipt.get("run_id")
            content_sha256 = receipt.get("content_sha256")
            if (
                not isinstance(run_id, str)
                or not isinstance(content_sha256, str)
                or receipt_path.name != f"{run_id}.json"
            ):
                raise ValueError("capture receipt marker identity is invalid")
            archived_raw = _read_recovery_file_at(
                consumed_fd,
                f"{run_id}.artifact.json",
                max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
            )
            if archived_raw is None:
                raise ValueError("capture archived raw is missing")
            artifact = parse_capture_artifact(archived_raw.raw)
            if (
                artifact.run_id != run_id
                or artifact.content_sha256 != content_sha256
            ):
                raise ValueError("capture archive pair identity differs")
            receipts.append(dict(receipt))

        _require_bound_private_directory_at(root_fd, "consumed", consumed_fd)
        _require_bound_private_directory_at(
            parent_fd,
            recovery_root.name,
            root_fd,
        )
        return receipts
    except (CaptureArtifactError, OSError, TypeError, ValueError) as error:
        raise ValueError("capture archive pair is invalid") from error
    finally:
        if consumed_fd >= 0:
            os.close(consumed_fd)
        if root_fd >= 0:
            os.close(root_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _unlink_recovery_file_at(
    directory_fd: int,
    name: str,
    *,
    expected: _BoundFile,
) -> None:
    try:
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise ValueError("capture recovery cleanup identity drifted") from error
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_uid != os.geteuid()
        or named.st_nlink != 1
        or stat.S_IMODE(named.st_mode) != 0o600
        or (
            named.st_dev,
            named.st_ino,
            named.st_size,
            named.st_mtime_ns,
        )
        != (
            expected.device,
            expected.inode,
            expected.size,
            expected.modified_ns,
        )
    ):
        raise ValueError("capture recovery cleanup identity drifted")
    os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _discard_publish_temp_at(directory_fd: int, temp_name: str) -> None:
    try:
        info = os.stat(temp_name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("capture publication temp is unsafe")
    os.unlink(temp_name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _publish_create_only_at(
    directory_fd: int,
    name: str,
    raw: bytes,
    *,
    max_bytes: int,
) -> bool:
    """Atomically publish one immutable owner-only file without overwriting truth."""
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("capture publication name is invalid")
    if len(raw) > max_bytes:
        raise ValueError("capture publication is oversized")
    existing = _read_recovery_file_at(
        directory_fd,
        name,
        max_bytes=max_bytes,
    )
    if existing is not None:
        if existing.raw != raw:
            raise ValueError("capture publication conflicts")
        return False

    temp_name = f".{name}.tmp"
    _discard_publish_temp_at(directory_fd, temp_name)
    descriptor = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("capture publication write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    try:
        _rename_noreplace_at(directory_fd, temp_name, name)
    except FileExistsError:
        raced = _read_recovery_file_at(
            directory_fd,
            name,
            max_bytes=max_bytes,
        )
        if raced is None or raced.raw != raw:
            raise ValueError("capture publication conflicts") from None
        _discard_publish_temp_at(directory_fd, temp_name)
        return False
    os.fsync(directory_fd)
    return True


def _reject_artifact(
    recovery_fd: int,
    source_name: str,
    run_id: str,
    raw: bytes,
    source: _BoundFile,
) -> str | None:
    """Copy pre-admission evidence to Linux storage; never mutate the mailbox."""
    del source_name, source
    descriptor = -1
    try:
        descriptor = _open_private_directory_at(recovery_fd, "rejected", create=True)
        digest = hashlib.sha256(raw).hexdigest()
        target_name = f"pre-admission.{run_id}.{digest}.artifact.json"
        _publish_create_only_at(
            descriptor,
            target_name,
            raw,
            max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
        )
        archived = _read_recovery_file_at(
            descriptor,
            target_name,
            max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
        )
        if archived is None or archived.raw != raw:
            raise ValueError("rejected artifact publication conflicts")
        _require_bound_private_directory_at(recovery_fd, "rejected", descriptor)
        return None
    except (OSError, ValueError) as error:
        return f"rejected_archive_failed:{type(error).__name__}"
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _stage_validated_artifact(
    recovery_fd: int,
    artifact: CaptureArtifact,
    raw: bytes,
) -> _StagedArtifact:
    """Create or reuse one immutable Linux payload copy; the ledger owns phase."""
    descriptor = _open_private_directory_at(recovery_fd, "staged", create=True)
    try:
        name = f"{artifact.run_id}.{artifact.content_sha256}.artifact.json"
        _publish_create_only_at(
            descriptor,
            name,
            raw,
            max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
        )
        stored = _read_recovery_file_at(
            descriptor,
            name,
            max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
        )
        if stored is None or stored.raw != raw:
            raise ValueError("staged capture conflicts with immutable identity")
        reparsed = parse_capture_artifact(stored.raw)
        if reparsed.run_id != artifact.run_id or reparsed.content_sha256 != artifact.content_sha256:
            raise ValueError("staged capture conflicts with immutable identity")
        _require_bound_private_directory_at(recovery_fd, "staged", descriptor)
        return _StagedArtifact(name=name, artifact=reparsed, raw=stored.raw)
    finally:
        os.close(descriptor)


def _discover_staged_artifact(recovery_fd: int) -> _StagedArtifact | None:
    """Recover the sole final stage or its deterministic pre-rename temp."""
    try:
        descriptor = _open_private_directory_at(recovery_fd, "staged", create=False)
    except FileNotFoundError:
        return None
    try:
        names = sorted(os.listdir(descriptor))
        if not names:
            _require_bound_private_directory_at(recovery_fd, "staged", descriptor)
            return None
        candidates: list[tuple[str, bool, _BoundFile, CaptureArtifact]] = []
        for name in names:
            is_temp = name.startswith(".") and name.endswith(".artifact.json.tmp")
            is_final = not name.startswith(".") and name.endswith(".artifact.json")
            if not (is_temp or is_final):
                raise ValueError("staged capture directory contains unknown evidence")
            stored = _read_recovery_file_at(
                descriptor,
                name,
                max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
            )
            if stored is None:
                raise ValueError("staged capture disappeared")
            artifact = parse_capture_artifact(stored.raw)
            expected_name = f"{artifact.run_id}.{artifact.content_sha256}.artifact.json"
            if artifact.final_status == "failed" or name != (
                f".{expected_name}.tmp" if is_temp else expected_name
            ):
                raise ValueError("staged capture identity is invalid")
            candidates.append((name, is_temp, stored, artifact))
        identities = {
            (candidate.run_id, candidate.content_sha256, stored.raw)
            for _, _, stored, candidate in candidates
        }
        if len(identities) != 1:
            raise ValueError("multiple staged captures require operator recovery")
        final_entries = [entry for entry in candidates if not entry[1]]
        temp_entries = [entry for entry in candidates if entry[1]]
        if len(final_entries) > 1 or len(temp_entries) > 1:
            raise ValueError("multiple staged captures require operator recovery")
        if final_entries:
            name, _, stored, artifact = final_entries[0]
            if temp_entries:
                _unlink_recovery_file_at(
                    descriptor,
                    temp_entries[0][0],
                    expected=temp_entries[0][2],
                )
        else:
            temp_name, _, stored, artifact = temp_entries[0]
            name = f"{artifact.run_id}.{artifact.content_sha256}.artifact.json"
            try:
                _rename_noreplace_at(descriptor, temp_name, name)
            except FileExistsError as error:
                raise ValueError("staged capture publication conflicts") from error
            os.fsync(descriptor)
            stored = _read_recovery_file_at(
                descriptor,
                name,
                max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
            )
            if stored is None:
                raise ValueError("staged capture disappeared")
        _require_bound_private_directory_at(recovery_fd, "staged", descriptor)
        return _StagedArtifact(name=name, artifact=artifact, raw=stored.raw)
    finally:
        os.close(descriptor)


def _publish_archive_raw(directory_fd: int, name: str, raw: bytes) -> None:
    try:
        _publish_create_only_at(
            directory_fd,
            name,
            raw,
            max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
        )
    except ValueError as error:
        raise _CaptureArchiveConflictError(
            "capture artifact conflicts with recovery identity"
        ) from error


def _publish_receipt_marker(
    directory_fd: int,
    name: str,
    receipt: dict[str, object],
) -> None:
    raw = (_canonical_json(receipt) + "\n").encode("utf-8")
    try:
        _publish_create_only_at(
            directory_fd,
            name,
            raw,
            max_bytes=2 * 1024 * 1024,
        )
    except ValueError as error:
        raise _CaptureArchiveConflictError(
            "capture receipt conflicts with ledger completion"
        ) from error


def _completion_archive_is_exact(
    *,
    recovery_fd: int,
    receipt: dict[str, object],
    artifact_run_id: str,
    content_sha256: str,
) -> bool:
    try:
        directory_fd = _open_private_directory_at(recovery_fd, "consumed", create=False)
    except FileNotFoundError:
        return False
    try:
        artifact_file = _read_recovery_file_at(
            directory_fd,
            f"{artifact_run_id}.artifact.json",
            max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
        )
        marker_file = _read_recovery_file_at(
            directory_fd,
            f"{artifact_run_id}.json",
            max_bytes=2 * 1024 * 1024,
        )
        _require_bound_private_directory_at(recovery_fd, "consumed", directory_fd)
    finally:
        os.close(directory_fd)
    if artifact_file is None or marker_file is None:
        return False
    try:
        archived = parse_capture_artifact(artifact_file.raw)
    except CaptureArtifactError as error:
        raise ValueError("consumed artifact is invalid") from error
    expected_marker = (_canonical_json(receipt) + "\n").encode("utf-8")
    return (
        archived.run_id == artifact_run_id
        and archived.content_sha256 == content_sha256
        and marker_file.raw == expected_marker
    )


def _require_exact_archive_pair_at(
    *,
    recovery_fd: int,
    directory_name: str,
    directory_fd: int,
    artifact_name: str,
    artifact_raw: bytes,
    marker_name: str,
    receipt: dict[str, object],
) -> None:
    artifact_file = _read_recovery_file_at(
        directory_fd,
        artifact_name,
        max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
    )
    marker_file = _read_recovery_file_at(
        directory_fd,
        marker_name,
        max_bytes=2 * 1024 * 1024,
    )
    expected_marker = (_canonical_json(receipt) + "\n").encode("utf-8")
    if (
        artifact_file is None
        or artifact_file.raw != artifact_raw
        or marker_file is None
        or marker_file.raw != expected_marker
    ):
        raise _CaptureArchiveConflictError(
            "capture completion archive conflicts with durable recovery state"
        )
    try:
        artifact = parse_capture_artifact(artifact_file.raw)
    except CaptureArtifactError as error:
        raise _CaptureArchiveConflictError("capture archive artifact is invalid") from error
    if (
        artifact.run_id != receipt.get("run_id")
        or artifact.content_sha256 != receipt.get("content_sha256")
    ):
        raise _CaptureArchiveConflictError("capture archive pair identity conflicts")
    _require_bound_private_directory_at(recovery_fd, directory_name, directory_fd)


def _publish_completion_archive(
    recovery_fd: int,
    *,
    directory_name: str,
    receipt: dict[str, object],
    expected_sha256: str,
    staged: _StagedArtifact,
) -> str | None:
    """Publish raw then marker from Linux stage, then remove only that private stage."""
    archive_fd = -1
    staged_fd = -1
    try:
        if directory_name not in {"consumed", "rejected"}:
            raise ValueError("capture archive disposition is invalid")
        staged_fd = _open_private_directory_at(recovery_fd, "staged", create=False)
        staged_file = _read_recovery_file_at(
            staged_fd,
            staged.name,
            max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
        )
        if staged_file is None or staged_file.raw != staged.raw:
            raise ValueError("staged capture identity conflicts")
        reparsed = parse_capture_artifact(staged_file.raw)
        if (
            reparsed.run_id != receipt.get("run_id")
            or reparsed.content_sha256 != expected_sha256
        ):
            raise ValueError("capture archive identity conflicts")
        archive_fd = _open_private_directory_at(recovery_fd, directory_name, create=True)
        run_id = str(receipt["run_id"])
        artifact_name = f"{run_id}.artifact.json"
        marker_name = f"{run_id}.json"
        _publish_archive_raw(archive_fd, artifact_name, staged_file.raw)
        _publish_receipt_marker(archive_fd, marker_name, receipt)
        _require_exact_archive_pair_at(
            recovery_fd=recovery_fd,
            directory_name=directory_name,
            directory_fd=archive_fd,
            artifact_name=artifact_name,
            artifact_raw=staged_file.raw,
            marker_name=marker_name,
            receipt=receipt,
        )
        _require_bound_private_directory_at(recovery_fd, "staged", staged_fd)
        _unlink_recovery_file_at(
            staged_fd,
            staged.name,
            expected=staged_file,
        )
        return None
    except _CaptureArchiveConflictError:
        raise
    except (OSError, ValueError) as error:
        return f"{directory_name}_archive_failed:{type(error).__name__}"
    finally:
        if archive_fd >= 0:
            os.close(archive_fd)
        if staged_fd >= 0:
            os.close(staged_fd)

def _run_ingest(args: _Args, handoff_fd: int) -> tuple[int, dict[str, object]]:
    runtime_error = _validate_runtime_db(args.runtime_db)
    if runtime_error is not None:
        return (
            _EXIT_INVALID_REQUEST,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": f"runtime_db:{runtime_error}",
            },
        )
    try:
        with _hold_capture_recovery_root(args.runtime_db) as recovery_fd:
            return _run_ingest_with_recovery(args, handoff_fd, recovery_fd)
    except _CaptureRecoveryOwnerConflictError:
        return (
            _EXIT_INVALID_REQUEST,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "conflict",
                "error_code": "capture_recovery_owner_conflict",
            },
        )
    except (OSError, ValueError):
        return (
            _EXIT_INTERNAL_ERROR,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "capture_recovery_storage_failed",
            },
        )


def _run_ingest_with_recovery(
    args: _Args,
    handoff_fd: int,
    recovery_fd: int,
) -> tuple[int, dict[str, object]]:
    source_name = args.artifact.name
    source: _BoundFile | None = None

    # 1. Recover the single immutable stage before consulting a replaceable
    #    source name. With no stage, read the source once through the locked
    #    handoff fd and reject symlinks/non-regular/oversized inputs.
    try:
        staged = _discover_staged_artifact(recovery_fd)
    except (CaptureArtifactError, OSError, ValueError):
        return (
            _EXIT_INVALID_REQUEST,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "conflict",
                "error_code": "capture_staging_identity_conflict",
            },
        )
    if staged is None:
        try:
            source = _read_bound_file_at(
                handoff_fd,
                source_name,
                max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
                required_mode=None,
            )
            if source is None:
                raise FileNotFoundError(source_name)
            artifact_raw = source.raw
            artifact = parse_capture_artifact(artifact_raw)
        except CaptureArtifactError as error:
            assert source is not None
            warning = _reject_artifact(
                recovery_fd,
                source_name,
                "invalid",
                source.raw,
                source,
            )
            payload: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": f"capture_artifact_invalid:{error.code}",
            }
            if warning is not None:
                payload["archive_warning"] = warning
            return _EXIT_INVALID_REQUEST, payload
        except (OSError, ValueError) as error:
            return (
                _EXIT_INVALID_REQUEST,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "invalid_request",
                    "error_code": f"capture_artifact_unreadable:{type(error).__name__}",
                },
            )
    else:
        artifact_raw = staged.raw
        artifact = staged.artifact
        try:
            candidate_source = _read_bound_file_at(
                handoff_fd,
                source_name,
                max_bytes=MAX_CAPTURE_ARTIFACT_BYTES,
                required_mode=None,
            )
        except (OSError, ValueError):
            candidate_source = None
        if candidate_source is not None:
            try:
                candidate_artifact = parse_capture_artifact(candidate_source.raw)
            except CaptureArtifactError:
                candidate_artifact = None
            if candidate_artifact is not None and candidate_artifact.run_id == artifact.run_id:
                if candidate_artifact.content_sha256 != artifact.content_sha256:
                    return (
                        _EXIT_INVALID_REQUEST,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "conflict",
                            "error_code": "capture_artifact_identity_conflict",
                        },
                    )
                source = candidate_source

    # 2. Capture-side failure artifacts never enter the durable business chain.
    #    An orphan stage can never carry this phase because staging occurs below.
    if artifact.final_status == "failed":
        if source is None:
            raise RuntimeError("failed capture source identity is missing")
        warning = _reject_artifact(
            recovery_fd,
            source_name,
            artifact.run_id,
            artifact_raw,
            source,
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "failure_reason": artifact.failure_reason or "unknown",
            "artifact": {
                "run_id": artifact.run_id,
                "file": source_name,
                "content_sha256": artifact.content_sha256,
            },
        }
        if warning is not None:
            payload["archive_warning"] = warning
        return _EXIT_BY_RUN_FAILURE["failed"], payload

    # 3. Path validation remains outside the durable claim.
    for validator, path, label in (
        (_validate_knowledge_base_root, args.knowledge_base_root, "knowledge_base"),
    ):
        error_code = validator(path)
        if error_code is not None:
            return (
                _EXIT_INVALID_REQUEST,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "invalid_request",
                    "error_code": f"{label}:{error_code}",
                },
            )

    if staged is None:
        try:
            staged = _stage_validated_artifact(recovery_fd, artifact, artifact_raw)
        except ValueError:
            return (
                _EXIT_INVALID_REQUEST,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "conflict",
                    "error_code": "capture_staging_identity_conflict",
                },
            )
        except OSError:
            return (
                _EXIT_INTERNAL_ERROR,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "internal_error",
                    "error_code": "capture_staging_failed",
                },
            )

    # 4. Durable identity claim. COMPLETE is owner truth: a retry republishes
    #    the stored bounded projection and never re-enters KB/G business.
    try:
        repository = ScraperRuntimeRepository(args.runtime_db)
        try:
            capture_record = repository.claim_capture_ingest(
                artifact_run_id=artifact.run_id,
                content_sha256=artifact.content_sha256,
                prior_g_json=_capture_prior_g_json(args.knowledge_base_root),
            )
        finally:
            repository.close()
    except CaptureIdentityConflictError:
        return (
            _EXIT_INVALID_REQUEST,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "conflict",
                "error_code": "capture_artifact_identity_conflict",
                "artifact": {
                    "run_id": artifact.run_id,
                    "file": source_name,
                    "content_sha256": artifact.content_sha256,
                },
            },
        )
    except Exception:
        return (
            _EXIT_INTERNAL_ERROR,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "capture_recovery_claim_failed",
            },
        )
    if capture_record.phase == "COMPLETE":
        try:
            return _resume_completed_capture(
                capture_record,
                recovery_fd=recovery_fd,
                source_name=source_name,
                staged=staged,
            )
        except _CaptureArchiveConflictError:
            return _capture_archive_conflict_payload(artifact, source_name)
        except Exception:
            return (
                _EXIT_INTERNAL_ERROR,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "internal_error",
                    "error_code": "capture_recovery_projection_invalid",
                },
            )

    # 5. 导入（module + ledger + KB + G 发布，复用既有语义）
    previous_umask = os.umask(0o077)
    try:
        request = ZsxqRunRequest(
            intent="sync",
            trigger=args.trigger,
            deadline_seconds=args.deadline_seconds,
        )
        completion = run_capture_ingest_once(
            runtime_db_path=args.runtime_db,
            knowledge_base_root=args.knowledge_base_root,
            artifact=artifact,  # F-04：已校验对象，校验/导入/归档同一身份
            request=request,
        )
    except Exception as exc:  # noqa: BLE001 — 任何意外失败都不得刷新 G
        return (
            _EXIT_INTERNAL_ERROR,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": f"capture_ingest_failed:{type(exc).__name__}",
            },
        )
    finally:
        os.umask(previous_umask)

    if args.runtime_db.exists():
        args.runtime_db.chmod(0o600)

    exit_code = _completion_exit_code(completion)
    payload = _projection(
        completion,
        artifact_run_id=artifact.run_id,
        artifact_file=source_name,
        artifact_sha256=artifact.content_sha256,
    )
    raw_g_working_set = payload.get("g_working_set")
    g_working_set = raw_g_working_set if isinstance(raw_g_working_set, Mapping) else None
    audit = build_capture_ingest_audit(
        artifact,
        ingest_status=payload["status"],
        completion_status=payload["completion_status"],
        g_working_set=g_working_set,
        index_articles=_read_index_articles_for_audit(args.knowledge_base_root),
    )
    audit_chain = audit.get("chain")
    audit_denominator = audit.get("denominator")
    payload["capture_audit"] = {
        "integrity_status": audit["integrity_status"],
        "chain_ready": (audit_chain.get("ready") if isinstance(audit_chain, Mapping) else False),
        "denominator_status": (
            audit_denominator.get("status") if isinstance(audit_denominator, Mapping) else "UNKNOWN"
        ),
        "data_gaps": audit["data_gaps"],
    }

    # 6. 先把 completion/audit 投影提交到 ledger，再归档。进程在两者之间
    #    消失时，重启只重投影 filesystem，不再创建第二条业务 run。
    #    F-04：归档失败（含身份不匹配）→ archive_warning + exit 70——绝不静默成功，
    #    防止同 artifact 重跑重盖 G 而不被察觉。
    if exit_code == _EXIT_COALESCED:
        return exit_code, payload
    ingest_run_id = completion.run.run_id
    if not isinstance(ingest_run_id, str) or not ingest_run_id:
        return (
            _EXIT_INTERNAL_ERROR,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "capture_recovery_terminal_run_missing",
            },
        )
    archive_disposition = "consumed" if exit_code in (0, 4) else "rejected"
    completion_receipt = {
        "schema_version": (
            CONSUMED_RECEIPT_SCHEMA_VERSION
            if archive_disposition == "consumed"
            else REJECTED_RECEIPT_SCHEMA_VERSION
        ),
        "run_id": artifact.run_id,
        "content_sha256": artifact.content_sha256,
        "ingested_at": _utc_now().isoformat(),
        "ingest_run_id": ingest_run_id,
        "status": payload["status"],
        "completion_status": payload["completion_status"],
        "audit": audit,
    }
    recovery_completion = {
        "schema_version": CAPTURE_RECOVERY_COMPLETION_SCHEMA_VERSION,
        "artifact_run_id": artifact.run_id,
        "content_sha256": artifact.content_sha256,
        "exit_code": exit_code,
        "payload": payload,
        "archive_disposition": archive_disposition,
        "receipt": completion_receipt,
    }
    try:
        repository = ScraperRuntimeRepository(args.runtime_db)
        try:
            repository.complete_capture_ingest(
                artifact_run_id=artifact.run_id,
                content_sha256=artifact.content_sha256,
                ingest_run_id=ingest_run_id,
                business_json=_canonical_json(completion.run.to_dict()),
                completion_json=_canonical_json(recovery_completion),
                audit_json=_canonical_json(audit),
                knowledge_base_root=args.knowledge_base_root,
            )
        finally:
            repository.close()
    except Exception:
        return (
            _EXIT_INTERNAL_ERROR,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "capture_recovery_completion_failed",
            },
        )
    try:
        if exit_code in (0, 4):
            warning = _publish_completion_archive(
                recovery_fd,
                directory_name="consumed",
                receipt=completion_receipt,
                expected_sha256=artifact.content_sha256,
                staged=staged,
            )
        else:
            warning = _publish_completion_archive(
                recovery_fd,
                directory_name="rejected",
                receipt=completion_receipt,
                expected_sha256=artifact.content_sha256,
                staged=staged,
            )
    except _CaptureArchiveConflictError:
        return _capture_archive_conflict_payload(artifact, source_name)
    if warning is not None:
        payload = {**payload, "archive_warning": warning}
        return _EXIT_INTERNAL_ERROR, payload
    return exit_code, payload


def _capture_archive_conflict_payload(
    artifact: CaptureArtifact,
    artifact_file: str,
) -> tuple[int, dict[str, object]]:
    return (
        _EXIT_INVALID_REQUEST,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "conflict",
            "error_code": "capture_completion_archive_conflict",
            "artifact": {
                "run_id": artifact.run_id,
                "file": artifact_file,
                "content_sha256": artifact.content_sha256,
            },
        },
    )


def main(
    argv: list[str] | None = None,
    *,
    _canonical_runtime_db: Path | None = None,
) -> int:
    parsed, invalid_exit = _parse_args(
        argv,
        canonical_runtime_db=(
            DEFAULT_RUNTIME_DB
            if _canonical_runtime_db is None
            else _canonical_runtime_db
        ),
    )
    if parsed is None:
        return invalid_exit or _EXIT_INVALID_REQUEST
    args = parsed

    try:
        lock_path = scheduler_handoff_lock_path(args.runtime_db)
    except ValueError as error:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": str(error),
            }
        )
        return _EXIT_INVALID_REQUEST

    try:
        with (
            hold_scheduler_handoff_lock(lock_path, mode=HandoffLockMode.EXCLUSIVE),
            _hold_capture_handoff_lock(args.artifact) as handoff_fd,
        ):
            exit_code, payload = _run_ingest(args, handoff_fd)
    except _CaptureHandoffLockBusyError:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "coalesced",
                "completion_status": "coalesced",
                "completion_data_gaps": [],
                "intent": "sync",
                "trigger": args.trigger,
                "coalesced": True,
                "error_code": "capture_handoff_locked",
            }
        )
        return _EXIT_COALESCED
    except _CaptureHandoffLockError:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "capture_handoff_lock_failed",
            }
        )
        return _EXIT_INTERNAL_ERROR
    except SchedulerHandoffLockBusyError as error:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "coalesced",
                "completion_status": "coalesced",
                "completion_data_gaps": [],
                "intent": "sync",
                "trigger": args.trigger,
                "coalesced": True,
                "error_code": error.code,
            }
        )
        return _EXIT_COALESCED
    except SchedulerHandoffLockError as error:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": error.code,
            }
        )
        return _EXIT_INTERNAL_ERROR

    _emit(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
