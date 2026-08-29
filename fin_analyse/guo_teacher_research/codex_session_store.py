"""Owner-only Codex session artifact store（Phase 3B）。

把 Codex 的 provider-private 会话 rollout 安全落盘为按 semantic product version
追加的不可覆盖快照，供 ``codex exec resume`` 使用。存储只保存 raw rollout
（高敏感 transcript）；不保存 auth、config、history、state DB 或任何 credential。

布局::

    <state_root>/runtime-sessions/codex-cli/v1/
        <session-uuid-hex>/
            versions/
                <product_version>/
                    manifest.json
                    home/sessions/YYYY/MM/DD/rollout-*.jsonl
                pending-<uuid>.tmp/...  # capture 暂存，fsync 后原子 rename

安全不变量：

- 目录 0700、文件 0600、全部当前 euid 所有、非 symlink、regular 文件单链接
  （``st_nlink == 1``，拒绝 hardlink）。
- 所有读取 ``O_NOFOLLOW | O_CLOEXEC``；manifest 内路径拒绝绝对路径与 ``..``，
  只接受 ``sessions/YYYY/MM/DD/rollout-*.jsonl`` 形状。
- 版本不可覆盖：写 pending 后以 ``renameat2(RENAME_NOREPLACE)`` 原子
  claim+publish——任何情况下 rename 都不会覆盖已存在的版本目录；EEXIST 后
  严格重读判定幂等/AlreadyExists/Invalid，绝不静默覆盖。
- 单版本 32 MiB、manifest 64 KiB（单 session / 全局配额由 3E janitor 负责）。
- 所有 fd 所有权显式转移：每取得一个 fd 立即进入外层 try/finally，失败路径
  全部关闭一次，不存在双重关闭或泄漏。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = "fin.codex-session-artifact/v1"
_BACKEND_DIR = Path("codex-cli")
_FORMAT_DIR = Path("v1")

_MAX_VERSION_BYTES = 32 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_ROLLOUT_RELATIVE_RE = re.compile(r"^sessions/[0-9]{4}/[0-9]{2}/[0-9]{2}/rollout-[^/]+\.jsonl$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PENDING_NAME_RE = re.compile(r"^pending-[0-9a-f]{32}\.tmp$")
_VERSION_NAME_RE = re.compile(r"^[0-9]+$")
_SESSION_NAME_RE = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "session_id",
        "runtime_identity_hash",
        "product_version",
        "codex_executable_sha256",
        "created_at",
        "captured_at",
        "total_bytes",
        "files",
    }
)
_FILE_ENTRY_KEYS = frozenset({"path", "size", "sha256"})

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_RDONLY_DIR = os.O_RDONLY | _CLOEXEC | _DIRECTORY | _NOFOLLOW
_RDONLY_FILE = os.O_RDONLY | _CLOEXEC | _NOFOLLOW | _NONBLOCK
_WRONLY_EXCL_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW

_RENAME_NOREPLACE = 1
_renameat2: Any = None
_libc = ctypes.CDLL(None, use_errno=True)
_renameat2 = getattr(_libc, "renameat2", None)
if _renameat2 is not None:
    _renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _renameat2.restype = ctypes.c_int


class CodexSessionStoreError(RuntimeError):
    """Stable session-store-owned failure."""


class CodexSessionStoreMissingError(CodexSessionStoreError):
    """Requested session/version does not exist."""


class CodexSessionStoreAlreadyExistsError(CodexSessionStoreError):
    """Version already exists with different content (no overwrite)."""


class CodexSessionStoreInvalidError(CodexSessionStoreError):
    """Artifact or layout violates the security/schema contract."""


# 进程内互斥：capture 的 publish 与 sweep 的清理共享同一把锁（当前部署为
# 单 MCP 进程；跨进程原子性由 renameat2(RENAME_NOREPLACE) 提供）。
_STORE_LOCK = threading.RLock()


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return (metadata.st_dev, metadata.st_ino)


def _open_dir_chain(path: Path, *, create: bool) -> int:
    """打开（必要时创建）owner-only 目录链；返回最终目录 fd。

    与 ``OwnerOnlyJsonSnapshotFile`` 的目录链语义一致：链上组件只以
    ``O_NOFOLLOW`` 遍历（拒绝 symlink 中间层），owner-only 只约束最终目录。
    所有权显式转移：成功返回时调用方拥有最终 fd；任何异常路径把所有已打开
    的 fd 恰好关闭一次（无双重关闭、无泄漏）。
    """
    if not path.is_absolute():
        raise CodexSessionStoreInvalidError("store path must be absolute")
    opened: list[int] = []
    try:
        current = os.open(path.anchor, _RDONLY_DIR)
        opened.append(current)
        for part in path.parts[1:]:
            parent = current
            try:
                current = os.open(part, _RDONLY_DIR, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    raise CodexSessionStoreMissingError(f"missing directory: {path}") from None
                os.mkdir(part, 0o700, dir_fd=parent)
                os.fsync(parent)
                current = os.open(part, _RDONLY_DIR, dir_fd=parent)
            except OSError as error:
                raise CodexSessionStoreInvalidError(f"unopenable directory: {path}") from error
            opened.pop()  # parent 已打开子目录：所有权转移后关闭
            os.close(parent)
            opened.append(current)
        metadata = os.fstat(current)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise CodexSessionStoreInvalidError(f"insecure directory: {path}")
        opened.pop()  # 所有权转移给调用方
        return current
    finally:
        for fd in opened:
            os.close(fd)


def _require_owner_directory(path: Path) -> int:
    """校验并打开 owner-only 目标目录（0700、euid、非 symlink）；返回 fd。"""
    return _open_dir_chain(path, create=False)


def _open_checked_dir(parent_fd: int, name: str) -> int:
    """fd-relative 打开并校验 owner-only 目录（directory/euid/0700）；返回新 fd。

    失败或校验不过时把已打开的 fd 关闭后抛 Invalid，不留泄漏。
    """
    try:
        descriptor = os.open(name, _RDONLY_DIR, dir_fd=parent_fd)
    except OSError as error:
        raise CodexSessionStoreInvalidError(f"unopenable directory: {name}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise CodexSessionStoreInvalidError(f"insecure directory: {name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_owner_regular(parent_fd: int, name: str, *, max_bytes: int) -> tuple[int, int]:
    """fd-relative 打开 owner regular 文件（O_NOFOLLOW，单链接 0600）。

    返回 ``(fd, size)``；校验失败或打开失败时关闭 fd 并抛 Invalid。
    """
    try:
        descriptor = os.open(name, _RDONLY_FILE, dir_fd=parent_fd)
    except OSError as error:
        raise CodexSessionStoreInvalidError(f"unopenable regular file: {name}") from error
    try:
        try:
            metadata = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise CodexSessionStoreInvalidError(f"unstable regular file: {name}") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or _identity(metadata) != _identity(named)
            or metadata.st_size > max_bytes
        ):
            raise CodexSessionStoreInvalidError(f"insecure regular file: {name}")
        return descriptor, int(metadata.st_size)
    except BaseException:
        os.close(descriptor)
        raise


def _read_bounded(descriptor: int, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes - total + 1))
        except OSError as error:
            raise CodexSessionStoreInvalidError("artifact read failed") from error
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise CodexSessionStoreInvalidError("artifact exceeds byte bound")
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("write made no progress")
        view = view[written:]
    os.fsync(descriptor)


def _sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_relative_path(relative: str) -> Path:
    """manifest/枚举路径必须为 sessions/ 下的 rollout 相对路径。"""
    if not isinstance(relative, str):
        raise CodexSessionStoreInvalidError("artifact path must be a string")
    normalized = relative.replace(os.sep, "/")
    if _ROLLOUT_RELATIVE_RE.fullmatch(normalized) is None:
        raise CodexSessionStoreInvalidError(f"invalid artifact path: {relative!r}")
    return Path(*normalized.split("/"))


_ROLLOUT_NAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f-]{36})\.jsonl$"
)


def _rollout_session_id(file_name: str) -> str:
    """从 rollout 文件名尾部提取 session UUID；非 canonical 形状抛 Invalid。"""
    if not isinstance(file_name, str):
        raise CodexSessionStoreInvalidError("rollout file name must be a string")
    match = _ROLLOUT_NAME_RE.fullmatch(file_name)
    if match is None:
        raise CodexSessionStoreInvalidError(f"invalid rollout file name: {file_name!r}")
    try:
        parsed = uuid.UUID(match.group(1))
    except (ValueError, TypeError, AttributeError):
        raise CodexSessionStoreInvalidError(f"invalid rollout file name: {file_name!r}") from None
    return str(parsed)


def _rename_noreplace(src_fd: int, dst_fd: int, src: str, dst: str) -> None:
    """``renameat2(RENAME_NOREPLACE)``：目标存在时抛 EEXIST，绝不覆盖。"""
    if _renameat2 is None:
        raise CodexSessionStoreInvalidError("renameat2 unavailable")
    result = _renameat2(
        src_fd,
        os.fsencode(src),
        dst_fd,
        os.fsencode(dst),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


class CodexSessionArtifactStore:
    """Owner-only Codex session artifact store（深模块，五个操作 + 配额查询）。"""

    def __init__(
        self,
        *,
        state_root: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state_root = Path(os.path.abspath(state_root))
        self._root = self._state_root / "runtime-sessions" / _BACKEND_DIR / _FORMAT_DIR
        self._clock = clock or (lambda: datetime.now(UTC))

    # ── 路径 ─────────────────────────────────────────────────────────────

    def _session_dir(self, session_id: str) -> Path:
        try:
            parsed = uuid.UUID(session_id)
        except (ValueError, TypeError, AttributeError):
            raise CodexSessionStoreInvalidError(f"invalid session id: {session_id!r}") from None
        return self._root / parsed.hex

    def _version_dir(self, session_id: str, product_version: int) -> Path:
        if (
            isinstance(product_version, bool)
            or not isinstance(product_version, int)
            or not 1 <= product_version < 2**31
        ):
            raise CodexSessionStoreInvalidError(f"invalid product version: {product_version!r}")
        return self._session_dir(session_id) / "versions" / str(product_version)

    # ── 枚举 source home ─────────────────────────────────────────────────

    def _enumerate_rollouts(
        self,
        source_home: Path,
        *,
        session_id: str,
        max_bytes: int,
    ) -> list[tuple[Path, int, str]]:
        """枚举 source CODEX_HOME 的 sessions/ 下 rollout 文件（size + sha256）。

        只接受 ``sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`` 形状的 owner
        regular 文件；rollout 文件名尾部 UUID 必须与请求的 canonical
        ``session_id`` 完全一致（混入 foreign session 的 rollout 整体 Invalid，
        防止敏感 transcript 串档）；其余路径（auth/config/history/state DB 等）
        一律拒绝。返回按相对路径排序的清单（与 manifest files 的 canonical
        顺序一致）。
        """
        sessions = source_home / "sessions"
        try:
            sessions_fd = _require_owner_directory(sessions)
        except CodexSessionStoreMissingError:
            return []
        collected: list[tuple[Path, int, str]] = []
        try:
            with os.scandir(sessions_fd) as year_iter:
                for year_entry in year_iter:
                    if not year_entry.is_dir(follow_symlinks=False):
                        raise CodexSessionStoreInvalidError(
                            f"unexpected non-directory under sessions/: {year_entry.name}"
                        )
                    year_fd = _open_checked_dir(sessions_fd, year_entry.name)
                    try:
                        with os.scandir(year_fd) as month_iter:
                            for month_entry in month_iter:
                                if not month_entry.is_dir(follow_symlinks=False):
                                    raise CodexSessionStoreInvalidError(
                                        f"unexpected non-directory under sessions/: {month_entry.name}"
                                    )
                                month_fd = _open_checked_dir(year_fd, month_entry.name)
                                try:
                                    with os.scandir(month_fd) as day_iter:
                                        for day_entry in day_iter:
                                            if not day_entry.is_dir(follow_symlinks=False):
                                                raise CodexSessionStoreInvalidError(
                                                    f"unexpected non-directory under sessions/: {day_entry.name}"
                                                )
                                            day_fd = _open_checked_dir(month_fd, day_entry.name)
                                            try:
                                                with os.scandir(day_fd) as file_iter:
                                                    for file_entry in file_iter:
                                                        relative = (
                                                            f"sessions/{year_entry.name}/"
                                                            f"{month_entry.name}/"
                                                            f"{day_entry.name}/{file_entry.name}"
                                                        )
                                                        _validate_relative_path(relative)
                                                        rollout_session = _rollout_session_id(
                                                            file_entry.name
                                                        )
                                                        if rollout_session != session_id:
                                                            raise CodexSessionStoreInvalidError(
                                                                f"foreign session rollout: {relative}"
                                                            )
                                                        descriptor, size = _open_owner_regular(
                                                            day_fd,
                                                            file_entry.name,
                                                            max_bytes=max_bytes,
                                                        )
                                                        try:
                                                            payload = _read_bounded(
                                                                descriptor, max_bytes=max_bytes
                                                            )
                                                        finally:
                                                            os.close(descriptor)
                                                        if size != len(payload):
                                                            raise CodexSessionStoreInvalidError(
                                                                f"rollout size changed: {relative}"
                                                            )
                                                        collected.append(
                                                            (
                                                                Path(*relative.split("/")),
                                                                len(payload),
                                                                _sha256_of(payload),
                                                            )
                                                        )
                                            finally:
                                                os.close(day_fd)
                                finally:
                                    os.close(month_fd)
                    finally:
                        os.close(year_fd)
        finally:
            os.close(sessions_fd)
        return sorted(collected)

    def _read_source_rollout(self, source_home: Path, relative: Path) -> bytes:
        """从 source home 读取单个 rollout（bounded、owner 校验）。

        capture 阶段 source 消失/漂移属于损坏（Invalid）；Missing 只留给
        “请求的 session/version 不存在”。
        """
        try:
            parent_fd = _require_owner_directory(source_home / relative.parent)
        except CodexSessionStoreMissingError:
            raise CodexSessionStoreInvalidError(
                f"rollout parent missing during capture: {relative}"
            ) from None
        try:
            descriptor, _size = _open_owner_regular(
                parent_fd, relative.name, max_bytes=_MAX_VERSION_BYTES
            )
        except BaseException:
            os.close(parent_fd)
            raise
        try:
            return _read_bounded(descriptor, max_bytes=_MAX_VERSION_BYTES)
        finally:
            os.close(descriptor)
            os.close(parent_fd)

    # ── capture ──────────────────────────────────────────────────────────

    def capture(
        self,
        *,
        session_id: str,
        product_version: int,
        runtime_identity_hash: str,
        codex_executable_sha256: str,
        source_home: Path,
    ) -> dict[str, Any]:
        """把 source CODEX_HOME 的 sessions/ 捕获为新版本快照（不可覆盖）。

        crash-safe 顺序（写入 → 原子发布）：
        1. 先在 versions/ 下写 pending 暂存树并逐层 fsync。
        2. ``renameat2(RENAME_NOREPLACE)`` 发布：目标已存在（并发发布或他人
           认领）即 EEXIST——回滚 pending 后严格重读判定幂等/AlreadyExists/
           Invalid，绝不覆盖任何已存在目录。
        3. manifest 预序列化并自验大小与可重读性后，才允许发布。
        失败路径回滚 pending，不留半提交痕迹。
        """
        if (
            not isinstance(runtime_identity_hash, str)
            or _SHA256_RE.fullmatch(runtime_identity_hash) is None
        ):
            raise CodexSessionStoreInvalidError("invalid runtime identity hash")
        if (
            not isinstance(codex_executable_sha256, str)
            or _SHA256_RE.fullmatch(codex_executable_sha256) is None
        ):
            raise CodexSessionStoreInvalidError("invalid executable sha256")
        version_dir = self._version_dir(session_id, product_version)
        files = self._enumerate_rollouts(
            source_home, session_id=session_id, max_bytes=_MAX_VERSION_BYTES
        )
        if not files:
            raise CodexSessionStoreInvalidError("no rollout files to capture")
        total_bytes = sum(size for _path, size, _digest in files)
        if total_bytes > _MAX_VERSION_BYTES:
            raise CodexSessionStoreInvalidError("session version exceeds 32 MiB bound")

        now = self._clock()
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": session_id,
            "runtime_identity_hash": runtime_identity_hash,
            "product_version": product_version,
            "codex_executable_sha256": codex_executable_sha256,
            "created_at": now.timestamp(),
            "captured_at": now.timestamp(),
            "total_bytes": total_bytes,
            "files": [
                {"path": str(path), "size": size, "sha256": digest}
                for path, size, digest in sorted(files)
            ],
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
            raise CodexSessionStoreInvalidError("session manifest exceeds 64 KiB bound")
        # 自验：发布前用同一严格 decoder 确认本 manifest 可被重新读取。
        self._decode_manifest(json.loads(manifest_bytes), session_id, product_version)

        with _STORE_LOCK:
            versions_fd = _open_dir_chain(version_dir.parent, create=True)
            pending_name = f"pending-{uuid.uuid4().hex}.tmp"
            try:
                self._write_pending(pending_name, manifest, files, source_home, versions_fd)
                try:
                    _rename_noreplace(
                        versions_fd,
                        versions_fd,
                        pending_name,
                        version_dir.name,
                    )
                except FileExistsError:
                    # 目标已存在：回滚 pending，严格重读判定幂等/AlreadyExists/Invalid。
                    with suppress(OSError):
                        self._remove_pending(pending_name, versions_fd)
                    try:
                        existing = self._read_manifest(session_id, product_version)
                    except CodexSessionStoreMissingError:
                        raise CodexSessionStoreInvalidError(
                            f"version {product_version} claimed without manifest"
                        ) from None
                    if self._version_matches_source(
                        existing,
                        files,
                        runtime_identity_hash,
                        codex_executable_sha256,
                        session_id,
                        product_version,
                    ):
                        return existing  # 幂等：身份 + 内容 + 已存文件全部一致
                    raise CodexSessionStoreAlreadyExistsError(
                        f"version {product_version} already exists with different content"
                    ) from None
                os.fsync(versions_fd)
            except BaseException:
                with suppress(OSError):
                    self._remove_pending(pending_name, versions_fd)
                raise
            finally:
                os.close(versions_fd)
            return manifest

    def _version_matches_source(
        self,
        existing: Mapping[str, Any],
        files: Sequence[tuple[Path, int, str]],
        runtime_identity_hash: str,
        codex_executable_sha256: str,
        session_id: str,
        product_version: int,
    ) -> bool:
        return (
            existing.get("runtime_identity_hash") == runtime_identity_hash
            and existing.get("codex_executable_sha256") == codex_executable_sha256
            and self._manifest_files_tuple(existing) == list(files)
            and self._stored_files_match_manifest(session_id, product_version, existing)
        )

    def _write_pending(
        self,
        pending_name: str,
        manifest: Mapping[str, Any],
        files: Sequence[tuple[Path, int, str]],
        source_home: Path,
        versions_fd: int,
    ) -> None:
        """pending 目录内写入 home/sessions/... + manifest.json（全部 owner-only）。

        写完每个叶目录后 fsync，再逐层收口到 sessions/home/pending 根，
        保证 rename 前嵌套目录项 crash-safe。
        """
        os.mkdir(pending_name, 0o700, dir_fd=versions_fd)
        pending_fd = _open_checked_dir(versions_fd, pending_name)
        try:
            os.mkdir("home", 0o700, dir_fd=pending_fd)
            home_fd = _open_checked_dir(pending_fd, "home")
            try:
                os.mkdir("sessions", 0o700, dir_fd=home_fd)
                sessions_fd = _open_checked_dir(home_fd, "sessions")
                try:
                    for relative, size, digest in files:
                        parts = relative.parts[1:]  # 去掉 sessions/
                        opened: list[int] = []
                        try:
                            current = sessions_fd
                            for part in parts[:-1]:
                                with suppress(FileExistsError):
                                    os.mkdir(part, 0o700, dir_fd=current)
                                current = _open_checked_dir(current, part)
                                opened.append(current)
                            payload = self._read_source_rollout(source_home, relative)
                            if len(payload) != size or _sha256_of(payload) != digest:
                                raise CodexSessionStoreInvalidError(
                                    f"rollout changed during capture: {relative}"
                                )
                            descriptor = os.open(
                                parts[-1],
                                _WRONLY_EXCL_FILE,
                                0o600,
                                dir_fd=current,
                            )
                            try:
                                os.fchmod(descriptor, 0o600)
                                _write_all(descriptor, payload)
                            finally:
                                os.close(descriptor)
                            # 叶目录收口，再逐层向上收口
                            for fd in reversed(opened):
                                os.fsync(fd)
                        finally:
                            for fd in opened:
                                os.close(fd)
                    os.fsync(sessions_fd)
                finally:
                    os.close(sessions_fd)
                manifest_descriptor = os.open(
                    "manifest.json",
                    _WRONLY_EXCL_FILE,
                    0o600,
                    dir_fd=pending_fd,
                )
                try:
                    os.fchmod(manifest_descriptor, 0o600)
                    _write_all(
                        manifest_descriptor,
                        json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                    )
                finally:
                    os.close(manifest_descriptor)
                os.fsync(home_fd)
                os.fsync(pending_fd)
            finally:
                os.close(home_fd)
        finally:
            os.close(pending_fd)
        os.fsync(versions_fd)

    def _remove_pending(self, pending_name: str, versions_fd: int) -> None:
        """递归删除 pending 目录（崩溃残留清理；名称必须匹配 canonical 模式）。"""
        if _PENDING_NAME_RE.fullmatch(pending_name) is None:
            raise CodexSessionStoreInvalidError(f"invalid pending name: {pending_name!r}")
        pending_fd = os.open(pending_name, _RDONLY_DIR, dir_fd=versions_fd)
        try:
            self._remove_tree_contents(pending_fd)
        finally:
            os.close(pending_fd)
        os.rmdir(pending_name, dir_fd=versions_fd)
        os.fsync(versions_fd)

    # ── materialize / 已存树读取 ─────────────────────────────────────────

    def _open_stored_rollout(self, version_fd: int, relative: Path) -> tuple[int, int]:
        """从版本根 fd 下钻到单个 stored rollout；返回 (fd, size)。

        manifest 内路径相对 ``home/``（即 ``home/sessions/...``）；每层目录
        校验 owner-only，叶文件校验 owner/0600/单链接。中间层 fd 在返回前
        全部关闭，只有叶文件 fd 转移给调用方。
        """
        opened: list[int] = []
        try:
            current = version_fd
            for part in ("home", *relative.parts[:-1]):
                current = _open_checked_dir(current, part)
                opened.append(current)
            descriptor, size = _open_owner_regular(
                current, relative.name, max_bytes=_MAX_VERSION_BYTES
            )
        except BaseException:
            for fd in opened:
                os.close(fd)
            raise
        for fd in opened:
            os.close(fd)
        return descriptor, size

    def _read_stored_rollout(self, version_fd: int, relative: Path) -> bytes:
        descriptor, _size = self._open_stored_rollout(version_fd, relative)
        try:
            return _read_bounded(descriptor, max_bytes=_MAX_VERSION_BYTES)
        finally:
            os.close(descriptor)

    def _stored_files_match_manifest(
        self,
        session_id: str,
        product_version: int,
        manifest: Mapping[str, Any],
    ) -> bool:
        """exact-tree 幂等复核：枚举完整 stored tree，与 manifest 做精确拓扑比较。

        版本根必须恰好为 ``manifest.json + home``，``home`` 恰好为 ``sessions``，
        其下目录与文件必须完全由 manifest 推导；额外文件/symlink/空目录、缺失
        项、mode/hash/size 漂移、未知路径均判定不幂等。
        """
        # 由 manifest 推导期望拓扑：str(relative) -> (size, digest)
        files_tuple = self._manifest_files_tuple(manifest)
        expected_files = {str(relative): (size, digest) for relative, size, digest in files_tuple}
        # relative 前缀结构: sessions/YYYY/MM/DD/<name>
        expected_years: dict[str, set[str]] = {}
        expected_months: dict[tuple[str, str], set[str]] = {}
        expected_days: dict[tuple[str, str, str], set[str]] = {}
        for relative, _size, _digest in files_tuple:
            parts = relative.parts  # sessions/YYYY/MM/DD/name
            if len(parts) != 5:
                return False
            _s, year, month, day, name = parts
            expected_years.setdefault(year, set()).add(month)
            expected_months.setdefault((year, month), set()).add(day)
            expected_days.setdefault((year, month, day), set()).add(name)

        version_fd = _require_owner_directory(self._version_dir(session_id, product_version))
        try:
            # 版本根：恰好 manifest.json + home
            root_names = {e.name for e in os.scandir(version_fd)}
            if root_names != {"manifest.json", "home"}:
                return False
            home_fd = _open_checked_dir(version_fd, "home")
            try:
                if {e.name for e in os.scandir(home_fd)} != {"sessions"}:
                    return False
                sessions_fd = _open_checked_dir(home_fd, "sessions")
                try:
                    if {e.name for e in os.scandir(sessions_fd)} != set(expected_years):
                        return False
                    for year in sorted(expected_years):
                        year_fd = _open_checked_dir(sessions_fd, year)
                        try:
                            if {e.name for e in os.scandir(year_fd)} != expected_years[year]:
                                return False
                            for month in sorted(expected_years[year]):
                                month_fd = _open_checked_dir(year_fd, month)
                                try:
                                    if {e.name for e in os.scandir(month_fd)} != expected_months[
                                        (year, month)
                                    ]:
                                        return False
                                    for day in sorted(expected_months[(year, month)]):
                                        day_fd = _open_checked_dir(month_fd, day)
                                        try:
                                            day_names = {e.name for e in os.scandir(day_fd)}
                                            if day_names != expected_days[(year, month, day)]:
                                                return False
                                            for file_name in sorted(day_names):
                                                key = f"sessions/{year}/{month}/{day}/{file_name}"
                                                descriptor, size = _open_owner_regular(
                                                    day_fd,
                                                    file_name,
                                                    max_bytes=_MAX_VERSION_BYTES,
                                                )
                                                try:
                                                    payload = _read_bounded(
                                                        descriptor,
                                                        max_bytes=_MAX_VERSION_BYTES,
                                                    )
                                                finally:
                                                    os.close(descriptor)
                                                expected_size, expected_digest = expected_files[key]
                                                if (
                                                    size != len(payload)
                                                    or size != expected_size
                                                    or _sha256_of(payload) != expected_digest
                                                ):
                                                    return False
                                        finally:
                                            os.close(day_fd)
                                finally:
                                    os.close(month_fd)
                        finally:
                            os.close(year_fd)
                finally:
                    os.close(sessions_fd)
            finally:
                os.close(home_fd)
        except (CodexSessionStoreInvalidError, OSError):
            return False
        finally:
            os.close(version_fd)
        return True

    def materialize(
        self,
        *,
        session_id: str,
        product_version: int,
        dest_home: Path,
    ) -> int:
        """把已保存版本恢复到 dest CODEX_HOME 的 sessions/（供 resume）。

        返回复制的总字节数。dest_home 必须存在且 owner-only（0700/euid）；
        目标中间层全部 fd-relative 创建并校验 owner-only；stored rollout 必须
        0600 单链接且与 manifest 内容一致。
        """
        manifest = self._read_manifest(session_id, product_version)
        dest_fd = _require_owner_directory(dest_home)
        try:
            version_fd = _require_owner_directory(self._version_dir(session_id, product_version))
        except BaseException:
            os.close(dest_fd)
            raise
        try:
            with suppress(FileExistsError):
                os.mkdir("sessions", 0o700, dir_fd=dest_fd)
            dest_sessions_fd = _open_checked_dir(dest_fd, "sessions")
            try:
                copied = 0
                for relative, expected_size, expected_digest in self._manifest_files_tuple(
                    manifest
                ):
                    payload = self._read_stored_rollout(version_fd, relative)
                    if len(payload) != expected_size or _sha256_of(payload) != expected_digest:
                        raise CodexSessionStoreInvalidError(
                            f"stored rollout digest mismatch: {relative}"
                        )
                    parts = relative.parts[1:]  # 去掉 sessions/
                    opened: list[int] = []
                    try:
                        current = dest_sessions_fd
                        for part in parts[:-1]:
                            with suppress(FileExistsError):
                                os.mkdir(part, 0o700, dir_fd=current)
                            current = _open_checked_dir(current, part)
                            opened.append(current)
                        descriptor = os.open(
                            parts[-1],
                            _WRONLY_EXCL_FILE,
                            0o600,
                            dir_fd=current,
                        )
                        try:
                            os.fchmod(descriptor, 0o600)
                            _write_all(descriptor, payload)
                        finally:
                            os.close(descriptor)
                        for fd in reversed(opened):
                            os.fsync(fd)
                    finally:
                        for fd in opened:
                            os.close(fd)
                    os.fsync(dest_sessions_fd)
                    copied += len(payload)
                return copied
            finally:
                os.close(dest_sessions_fd)
        finally:
            os.close(version_fd)
            os.close(dest_fd)

    # ── delete / sweep / 配额 ────────────────────────────────────────────

    def delete_version(self, *, session_id: str, product_version: int) -> None:
        """删除一个版本（GC 用；调用方先删 artifact，成功后才 reconcile GC 指针）。"""
        version_dir = self._version_dir(session_id, product_version)
        versions_fd = _require_owner_directory(version_dir.parent)
        try:
            try:
                version_fd = os.open(version_dir.name, _RDONLY_DIR, dir_fd=versions_fd)
            except FileNotFoundError:
                raise CodexSessionStoreMissingError(
                    f"session {session_id} version {product_version} not found"
                ) from None
            try:
                self._remove_tree_contents(version_fd)
            finally:
                os.close(version_fd)
            os.rmdir(version_dir.name, dir_fd=versions_fd)
            os.fsync(versions_fd)
        finally:
            os.close(versions_fd)

    def _remove_tree_contents(self, tree_fd: int) -> None:
        """递归删除 fd 目录的内容（file 直接删，目录先递归后 rmdir）。"""
        with os.scandir(tree_fd) as iterator:
            for entry in iterator:
                if entry.is_dir(follow_symlinks=False):
                    sub_fd = os.open(entry.name, _RDONLY_DIR, dir_fd=tree_fd)
                    try:
                        self._remove_tree_contents(sub_fd)
                    finally:
                        os.close(sub_fd)
                    os.rmdir(entry.name, dir_fd=tree_fd)
                else:
                    os.remove(entry.name, dir_fd=tree_fd)

    def sweep_orphans(self) -> int:
        """清理崩溃残留：canonical pending-*.tmp 与无内容的空版本目录。

        只处理 canonical 路径：session 目录必须是 32-hex UUID（未知父树绝不
        进入）；其下只删除 pending-<32hex>.tmp 或纯数字且完全空的版本目录；
        其余未知路径绝不删除。
        """
        removed = 0
        sessions_root = self._root
        if not sessions_root.exists():
            return 0
        with _STORE_LOCK:
            root_fd = _require_owner_directory(sessions_root)
            try:
                with os.scandir(root_fd) as session_iter:
                    for session_entry in session_iter:
                        # 只有 canonical 32-hex session 目录才可能拥有 versions/
                        if not _SESSION_NAME_RE.fullmatch(session_entry.name):
                            continue
                        if not session_entry.is_dir(follow_symlinks=False):
                            continue
                        try:
                            versions_fd = _require_owner_directory(
                                sessions_root / session_entry.name / "versions"
                            )
                        except CodexSessionStoreMissingError:
                            continue
                        try:
                            with os.scandir(versions_fd) as version_iter:
                                for version_entry in version_iter:
                                    name = version_entry.name
                                    if _PENDING_NAME_RE.fullmatch(name):
                                        self._remove_pending(name, versions_fd)
                                        removed += 1
                                    elif _VERSION_NAME_RE.fullmatch(name) and version_entry.is_dir(
                                        follow_symlinks=False
                                    ):
                                        # 空认领目录（capture 认领后崩溃）：仅删除完全空的
                                        try:
                                            version_fd = os.open(
                                                name, _RDONLY_DIR, dir_fd=versions_fd
                                            )
                                        except OSError:
                                            continue
                                        try:
                                            with os.scandir(version_fd) as it:
                                                empty = next(it, None) is None
                                        except OSError:
                                            empty = False
                                        finally:
                                            os.close(version_fd)
                                        if empty:
                                            os.rmdir(name, dir_fd=versions_fd)
                                            removed += 1
                        finally:
                            os.close(versions_fd)
            finally:
                os.close(root_fd)
        return removed

    def store_bytes(self) -> int:
        """当前 store 总字节数（全局配额 512 MiB 由 janitor 决策）。

        严格枚举全部受管版本（复用 session_versions 的 exact-tree 校验）：
        根目录 fd-relative 枚举（O_NOFOLLOW + owner-only），只接受 canonical
        32-hex session 目录；空 symlink 根、大写/dashed UUID alias、pending
        非目录项一律 Invalid——任何损坏传播为计量失败，不静默跳过。
        """
        total = 0
        sessions_root = self._root
        try:
            root_fd = _require_owner_directory(sessions_root)
        except CodexSessionStoreMissingError:
            return 0
        try:
            with os.scandir(root_fd) as iterator:
                for entry in iterator:
                    if not entry.is_dir(follow_symlinks=False):
                        raise CodexSessionStoreInvalidError(
                            f"store root unexpected entry: {entry.name}"
                        )
                    if _SESSION_NAME_RE.fullmatch(entry.name) is None:
                        raise CodexSessionStoreInvalidError(
                            f"store root non-canonical session: {entry.name}"
                        )
                    session_id = str(uuid.UUID(hex=entry.name))
                    for _version, version_bytes in self.session_versions(session_id):
                        total += version_bytes
        finally:
            os.close(root_fd)
        return total

    def session_versions(self, session_id: str) -> list[tuple[int, int]]:
        """某 session 的 (product_version, bytes) 列表（janitor 配额决策用）。

        fd-relative 枚举（O_NOFOLLOW + owner-only + follow_symlinks=False），
        目录名必须等于 canonical ``str(product_version)``；symlink、非目录、
        缺失 manifest、重复 alias（如 ``01/``）一律抛 Invalid——不静默返回
        “完整但为空”的列表。
        """
        session_dir = self._session_dir(session_id)
        versions_root = session_dir / "versions"
        try:
            versions_fd = _require_owner_directory(versions_root)
        except CodexSessionStoreMissingError:
            return []
        result: list[tuple[int, int]] = []
        try:
            with os.scandir(versions_fd) as iterator:
                for entry in iterator:
                    if _PENDING_NAME_RE.fullmatch(entry.name):
                        continue  # pending 暂存名，不是 canonical 版本
                    try:
                        product_version = int(entry.name)
                    except ValueError:
                        # 非数字非 pending 名：未知项 = 损坏（不再静默忽略）
                        raise CodexSessionStoreInvalidError(
                            f"session {session_id} unknown version entry: {entry.name}"
                        ) from None
                    if (
                        isinstance(product_version, bool)
                        or not 1 <= product_version < 2**31
                        or entry.name != str(product_version)
                        or not entry.is_dir(follow_symlinks=False)
                    ):
                        # 越界版本（-1/0/2**31）、非 canonical（01）、非目录 = 损坏
                        raise CodexSessionStoreInvalidError(
                            f"session {session_id} version entry corrupt: {entry.name}"
                        )
                    # 复用统一严格读取（含 RecursionError 等解析异常收敛为 Invalid），
                    # 并验证 exact stored tree（manifest 声明的 total_bytes 与实际
                    # stored 文件一致，防伪造 manifest 虚报大小）。
                    manifest = self._read_manifest(session_id, product_version)
                    if not self._stored_files_match_manifest(session_id, product_version, manifest):
                        raise CodexSessionStoreInvalidError(
                            f"session {session_id} version tree mismatch: {entry.name}"
                        )
                    result.append((product_version, manifest["total_bytes"]))
        finally:
            os.close(versions_fd)
        return sorted(result)

    def _session_id_from_version_path(self, session_dir: Path) -> str:
        raw = session_dir.name
        return str(uuid.UUID(hex=raw))

    # ── manifest ─────────────────────────────────────────────────────────

    def _read_manifest(self, session_id: str, product_version: int) -> dict[str, Any]:
        """读取并严格解码 manifest（schema/身份/路径/配额逐项校验，fail closed）。

        只有版本目录本身不存在才返回 Missing；取得版本 fd 后的 manifest/tree
        缺失、漂移和 OSError 全部转为 Invalid。
        """
        manifest_path = self._version_dir(session_id, product_version) / "manifest.json"
        try:
            parent_fd = _require_owner_directory(manifest_path.parent)
        except CodexSessionStoreMissingError:
            raise CodexSessionStoreMissingError(
                f"session {session_id} version {product_version} not found"
            ) from None
        try:
            descriptor, _size = _open_owner_regular(
                parent_fd, "manifest.json", max_bytes=_MAX_MANIFEST_BYTES
            )
        except BaseException:
            os.close(parent_fd)
            raise
        try:
            payload = _read_bounded(descriptor, max_bytes=_MAX_MANIFEST_BYTES)
        finally:
            os.close(descriptor)
            os.close(parent_fd)
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
            # ValueError：超长整数字面量（如 5000 位）在解析阶段即溢出；
            # RecursionError：深嵌套结构。统一收敛为 store-owned Invalid。
            raise CodexSessionStoreInvalidError("invalid manifest") from error
        return self._decode_manifest(document, session_id, product_version)

    def _decode_manifest(
        self,
        document: object,
        session_id: str,
        product_version: int,
    ) -> dict[str, Any]:
        """统一严格解码：exact schema、原始 JSON 类型、身份/路径/配额全验。

        禁止 bool/float/string coercion（``size: true``、``product_version: true``
        一律拒绝）；未知字段拒绝；所有读入口共用，篡改/损坏 fail closed。
        """
        if not isinstance(document, dict) or set(document) != _MANIFEST_KEYS:
            raise CodexSessionStoreInvalidError("invalid manifest schema")
        if document.get("schema_version") != _SCHEMA_VERSION:
            raise CodexSessionStoreInvalidError("invalid manifest schema")
        raw_session = document.get("session_id")
        raw_version = document.get("product_version")
        if (
            not isinstance(raw_session, str)
            or raw_session != session_id
            or not isinstance(raw_version, int)
            or isinstance(raw_version, bool)
            or raw_version != product_version
        ):
            raise CodexSessionStoreInvalidError("manifest identity mismatch")
        identity = document.get("runtime_identity_hash")
        executable = document.get("codex_executable_sha256")
        if not isinstance(identity, str) or _SHA256_RE.fullmatch(identity) is None:
            raise CodexSessionStoreInvalidError("invalid manifest runtime identity hash")
        if not isinstance(executable, str) or _SHA256_RE.fullmatch(executable) is None:
            raise CodexSessionStoreInvalidError("invalid manifest executable hash")
        for key in ("created_at", "captured_at"):
            value = document.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise CodexSessionStoreInvalidError(f"invalid manifest {key}")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False  # 巨大 JSON integer（如 10**309）超出 float 范围
            if not finite:
                raise CodexSessionStoreInvalidError(f"invalid manifest {key}")
        total_bytes = document.get("total_bytes")
        if (
            not isinstance(total_bytes, int)
            or isinstance(total_bytes, bool)
            or not 0 <= total_bytes <= _MAX_VERSION_BYTES
        ):
            raise CodexSessionStoreInvalidError("invalid manifest total_bytes")
        raw_files = document.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise CodexSessionStoreInvalidError("invalid manifest files")
        files: list[tuple[Path, int, str]] = []
        seen: set[str] = set()
        cumulative = 0
        for item in raw_files:
            if not isinstance(item, dict) or set(item) != _FILE_ENTRY_KEYS:
                raise CodexSessionStoreInvalidError("invalid manifest file entry")
            raw_path = item["path"]
            if not isinstance(raw_path, str):
                raise CodexSessionStoreInvalidError("invalid manifest file entry")
            relative = _validate_relative_path(raw_path)
            raw_size = item["size"]
            raw_digest = item["sha256"]
            if (
                not isinstance(raw_size, int)
                or isinstance(raw_size, bool)
                or raw_size < 0
                or not isinstance(raw_digest, str)
                or _SHA256_RE.fullmatch(raw_digest) is None
            ):
                raise CodexSessionStoreInvalidError("invalid manifest file entry")
            key = str(relative)
            if key in seen:
                raise CodexSessionStoreInvalidError(f"duplicate manifest path: {key}")
            seen.add(key)
            cumulative += raw_size
            if cumulative > _MAX_VERSION_BYTES:
                raise CodexSessionStoreInvalidError("manifest exceeds 32 MiB bound")
            files.append((relative, raw_size, raw_digest))
        if cumulative != total_bytes:
            raise CodexSessionStoreInvalidError("manifest total_bytes mismatch")
        if files != sorted(files):
            raise CodexSessionStoreInvalidError("manifest files not canonical order")
        return {
            "schema_version": _SCHEMA_VERSION,
            "session_id": session_id,
            "runtime_identity_hash": identity,
            "product_version": product_version,
            "codex_executable_sha256": executable,
            "created_at": document["created_at"],
            "captured_at": document["captured_at"],
            "total_bytes": total_bytes,
            "files": [
                {"path": str(path), "size": size, "sha256": digest} for path, size, digest in files
            ],
        }

    def _manifest_files_tuple(self, manifest: Mapping[str, Any]) -> list[tuple[Path, int, str]]:
        """把已解码 manifest 的 files（dict 形状）转成 (Path, size, sha256) 元组。"""
        result: list[tuple[Path, int, str]] = []
        for item in manifest.get("files", []):
            if not isinstance(item, dict) or set(item) != _FILE_ENTRY_KEYS:
                raise CodexSessionStoreInvalidError("invalid manifest file entry")
            raw_path = item["path"]
            raw_size = item["size"]
            raw_digest = item["sha256"]
            if (
                not isinstance(raw_path, str)
                or not isinstance(raw_size, int)
                or isinstance(raw_size, bool)
                or raw_size < 0
                or not isinstance(raw_digest, str)
                or _SHA256_RE.fullmatch(raw_digest) is None
            ):
                raise CodexSessionStoreInvalidError("invalid manifest file entry")
            relative = _validate_relative_path(raw_path)
            result.append((relative, raw_size, raw_digest))
        return result
