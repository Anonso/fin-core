"""No-output stall watchdog for bounded subprocess execution.

Single-threaded selector loop over the child's stdout/stderr pipes: any
written byte counts as activity (no readline/read buffering traps), the
whole process group is killed on stall or timeout (covering grandchildren
that may still hold the pipe write ends), and the pipes are drained
deterministically without background threads — so cleanup never blocks on
a thread holding the buffered-read lock.

Shared by the direct CLI path and the local capability transport so that
every FIN-owned codex child is covered by the same watchdog semantics.
"""

from __future__ import annotations

import hashlib
import locale
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

# sol/ultra 最高档推理在部分请求下会长时间静默输出（2026-08-15 凌晨实测
# CODEX_CHILD_STALL 556s 被提前击杀）；用户拍板增大静默容忍窗，总超时仍由
# runtime budget（3595s）兜底。
STALL_DETECTION_SECONDS = 600.0

_STALL_POLL_SECONDS = 0.5
_READ_CHUNK_BYTES = 4096
# 成功路径完整缓冲上限(bytes):超过则截断并置 truncated(高输出量 child
# 不无限累积内存,N8 bounded retention;截断导致解析失败由调用方按
# missing-terminal 处理)。
_SUCCESS_BUFFER_MAX_BYTES = 8 * 1024 * 1024
# 部分输出快照的尾部保留上限(bytes)。完整流只保留 sha256 与字节计数
# (N8: total/tail/truncated 语义)——tail 仅供内存分类,调用方不得落盘。
_PARTIAL_TAIL_MAX_BYTES = 4096


class _PartialStreamSnapshot:
    """Bounded in-memory snapshot of one child stream.

    完整流不落盘:只保留尾部缓冲(上限 _PARTIAL_TAIL_MAX_BYTES)、完整流
    sha256 与字节计数;truncated 标记尾部是否发生过丢弃。
    """

    __slots__ = ("_tail", "total_bytes", "truncated", "_hash")

    def __init__(self) -> None:
        self._tail = bytearray()
        self.total_bytes = 0
        self.truncated = False
        self._hash = hashlib.sha256()

    def update(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total_bytes += len(chunk)
        self._hash.update(chunk)
        self._tail.extend(chunk)
        if len(self._tail) > _PARTIAL_TAIL_MAX_BYTES:
            del self._tail[: len(self._tail) - _PARTIAL_TAIL_MAX_BYTES]
            self.truncated = True

    def tail(self) -> bytes:
        return bytes(self._tail)

    def sha256(self) -> str:
        return self._hash.hexdigest()


class StallError(TimeoutError):
    """The child produced no stdout/stderr output within the stall window.

    Raised instead of waiting the full timeout: a live process that stops
    emitting (stall) is killed early so failover can start (user decision:
    never wait passively for the cap).  Carries bounded partial stream
    snapshots for in-memory classification only.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout_tail: bytes = b"",
        stderr_tail: bytes = b"",
        stdout_total_bytes: int = 0,
        stderr_total_bytes: int = 0,
        stdout_sha256: str = "",
        stderr_sha256: str = "",
        truncated: bool = False,
    ) -> None:
        super().__init__(message)
        self.stdout_tail = stdout_tail
        self.stderr_tail = stderr_tail
        self.stdout_total_bytes = stdout_total_bytes
        self.stderr_total_bytes = stderr_total_bytes
        self.stdout_sha256 = stdout_sha256
        self.stderr_sha256 = stderr_sha256
        self.truncated = truncated


class WatchdogTimeoutExpired(subprocess.TimeoutExpired):
    """Hard-cap timeout with the same partial stream snapshot attached."""

    def __init__(
        self,
        cmd: Any,
        timeout: float,
        *,
        stdout_tail: bytes = b"",
        stderr_tail: bytes = b"",
        stdout_total_bytes: int = 0,
        stderr_total_bytes: int = 0,
        stdout_sha256: str = "",
        stderr_sha256: str = "",
        truncated: bool = False,
    ) -> None:
        super().__init__(cmd, timeout)
        self.stdout_tail = stdout_tail
        self.stderr_tail = stderr_tail
        self.stdout_total_bytes = stdout_total_bytes
        self.stderr_total_bytes = stderr_total_bytes
        self.stdout_sha256 = stdout_sha256
        self.stderr_sha256 = stderr_sha256
        self.truncated = truncated


def run_with_stall_watchdog(
    command: list[str],
    *,
    timeout: float | None,
    stall_seconds: float,
    early_progress_check: Callable[[], bool] | None = None,
    early_progress_seconds: float = 0.0,
    **popen_kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run one child with a no-output watchdog and process-group cleanup.

    ``timeout`` is the hard cap (``subprocess.TimeoutExpired`` on breach);
    ``stall_seconds`` is the no-output window (``StallError`` on breach).
    ``early_progress_check`` (optional) is a best-effort startup marker:
    while it keeps returning False past ``early_progress_seconds`` the child
    is killed with ``StallError``; once it returns True it is permanently
    latched and never consulted again (N7: best-effort signal, not a
    security boundary).  Defaults (None/0.0) preserve historical behavior.
    Remaining kwargs mirror ``subprocess.run``; ``capture_output``/``check``
    are consumed for compatibility and ``text`` is forced (streams are read
    as bytes and decoded with the locale encoding so any byte is activity).
    Timeout and stall exceptions carry bounded partial stream snapshots
    (tail buffers + full-stream sha256 + byte counts, truncated flag).
    """

    popen_kwargs.pop("capture_output", None)
    popen_kwargs.pop("check", None)
    popen_kwargs.pop("text", None)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        **popen_kwargs,
    )
    if process.stdout is None or process.stderr is None:
        _terminate_group(process)
        raise subprocess.SubprocessError("stall watchdog streams unavailable")
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    selector: selectors.BaseSelector | None = None
    try:
        # 非阻塞读 + selector：select 的"可读"在写端打开但无字节时是假就绪，
        # os.read 返回 BlockingIOError（区别于 EOF 的 b""）。
        # 初始化（set_blocking/selector/register）也在 try 内：任何初始化
        # 异常都必须回收已启动的 child 与已创建的资源。
        os.set_blocking(stdout_fd, False)
        os.set_blocking(stderr_fd, False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_snapshot = _PartialStreamSnapshot()
        stderr_snapshot = _PartialStreamSnapshot()
        success_truncated = False
        last_output = time.monotonic()
        started = time.monotonic()
        marker_seen = early_progress_check is None
        marker_started = started
        while selector.get_map() or process.poll() is None:
            elapsed = time.monotonic() - started
            if timeout is not None and elapsed >= timeout:
                _terminate_group(process)
                raise WatchdogTimeoutExpired(
                    command,
                    timeout,
                    stdout_tail=stdout_snapshot.tail(),
                    stderr_tail=stderr_snapshot.tail(),
                    stdout_total_bytes=stdout_snapshot.total_bytes,
                    stderr_total_bytes=stderr_snapshot.total_bytes,
                    stdout_sha256=stdout_snapshot.sha256(),
                    stderr_sha256=stderr_snapshot.sha256(),
                    truncated=(
                        stdout_snapshot.truncated or stderr_snapshot.truncated
                    ),
                )
            if time.monotonic() - last_output >= stall_seconds:
                _terminate_group(process)
                raise StallError(
                    f"no child output for {stall_seconds:.0f}s",
                    stdout_tail=stdout_snapshot.tail(),
                    stderr_tail=stderr_snapshot.tail(),
                    stdout_total_bytes=stdout_snapshot.total_bytes,
                    stderr_total_bytes=stderr_snapshot.total_bytes,
                    stdout_sha256=stdout_snapshot.sha256(),
                    stderr_sha256=stderr_snapshot.sha256(),
                    truncated=(
                        stdout_snapshot.truncated or stderr_snapshot.truncated
                    ),
                )
            if not marker_seen and early_progress_check is not None:
                if early_progress_check():
                    marker_seen = True  # 永久 latch,之后不再检查
                elif (
                    early_progress_seconds > 0
                    and time.monotonic() - marker_started >= early_progress_seconds
                ):
                    _terminate_group(process)
                    raise StallError(
                        f"no startup marker within {early_progress_seconds:.0f}s",
                        stdout_tail=stdout_snapshot.tail(),
                        stderr_tail=stderr_snapshot.tail(),
                        stdout_total_bytes=stdout_snapshot.total_bytes,
                        stderr_total_bytes=stderr_snapshot.total_bytes,
                        stdout_sha256=stdout_snapshot.sha256(),
                        stderr_sha256=stderr_snapshot.sha256(),
                        truncated=(
                            stdout_snapshot.truncated or stderr_snapshot.truncated
                        ),
                    )
            for key, _ in selector.select(timeout=_STALL_POLL_SECONDS):
                stream: Any = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), _READ_CHUNK_BYTES)
                except BlockingIOError:
                    continue  # 写端开但无数据——不算 EOF，不算活动
                if not chunk:
                    selector.unregister(stream)  # EOF：写端全部关闭
                    continue
                is_stdout = stream is process.stdout
                if is_stdout:
                    stdout_snapshot.update(chunk)
                    if stdout_snapshot.total_bytes <= _SUCCESS_BUFFER_MAX_BYTES:
                        stdout_chunks.append(chunk)
                    else:
                        success_truncated = True
                else:
                    stderr_snapshot.update(chunk)
                    if stderr_snapshot.total_bytes <= _SUCCESS_BUFFER_MAX_BYTES:
                        stderr_chunks.append(chunk)
                    else:
                        success_truncated = True
                last_output = time.monotonic()
        returncode = process.wait()
    except BaseException:
        # 后代可能仍持有 pipe 写端：杀整个进程组使 EOF 到达，保证确定性退出。
        _terminate_group(process)
        raise
    finally:
        if selector is not None:
            selector.close()
        with suppress(OSError):
            if process.stdout is not None:
                process.stdout.close()
        with suppress(OSError):
            if process.stderr is not None:
                process.stderr.close()
    encoding = locale.getpreferredencoding(False)
    stdout = b"".join(stdout_chunks).decode(encoding, errors="replace")
    stderr = b"".join(stderr_chunks).decode(encoding, errors="replace")
    completed = subprocess.CompletedProcess(command, returncode, stdout, stderr)
    completed.truncated = success_truncated  # type: ignore[attr-defined]
    completed.stdout_total_bytes = stdout_snapshot.total_bytes  # type: ignore[attr-defined]
    completed.stderr_total_bytes = stderr_snapshot.total_bytes  # type: ignore[attr-defined]
    completed.stdout_sha256 = stdout_snapshot.sha256()  # type: ignore[attr-defined]
    completed.stderr_sha256 = stderr_snapshot.sha256()  # type: ignore[attr-defined]
    return completed


def _terminate_group(process: subprocess.Popen[Any]) -> None:
    """Kill the whole process group (grandchildren included) and reap."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


__all__ = ["STALL_DETECTION_SECONDS", "StallError", "run_with_stall_watchdog"]
