"""Bounded subprocess execution with file-backed output and group cleanup."""

from __future__ import annotations

import errno
import math
import os
import re
import resource
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

_PRLIMIT = Path("/usr/bin/prlimit")


def require_root_controlled_executable(
    path: Path,
    *,
    error_prefix: str = "bounded_process",
) -> None:
    """Require one immutable-enough root-owned executable."""

    _validate_error_prefix(error_prefix)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeError(f"{error_prefix}_binary_unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
        or not metadata.st_mode & 0o100
    ):
        raise RuntimeError(f"{error_prefix}_binary_unsafe")


def run_bounded_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    max_output_bytes: int,
    pass_fds: tuple[int, ...] = (),
    error_prefix: str = "bounded_process",
) -> subprocess.CompletedProcess[str]:
    """Run one argv-only process with bounded files and descendant cleanup."""

    _validate_error_prefix(error_prefix)
    if not argv or not all(
        isinstance(argument, str) and argument and "\x00" not in argument for argument in argv
    ):
        raise ValueError("argv must contain non-empty NUL-free strings")
    if not isinstance(cwd, Path) or not cwd.is_absolute():
        raise ValueError("cwd must be an absolute pathlib.Path")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout < 0
    ):
        raise ValueError("timeout must be finite and non-negative")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
        raise TypeError("max_output_bytes must be an integer")
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")

    require_root_controlled_executable(_PRLIMIT, error_prefix=error_prefix)
    _, hard_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    requested_limit = max_output_bytes + 1
    effective_limit = (
        requested_limit
        if hard_limit == resource.RLIM_INFINITY
        else min(requested_limit, hard_limit)
    )
    limited_argv = (
        str(_PRLIMIT),
        f"--fsize={effective_limit}:{effective_limit}",
        "--",
        *argv,
    )
    with (
        tempfile.TemporaryFile() as stdout,
        tempfile.TemporaryFile() as stderr,
    ):
        deadline = time.monotonic() + float(timeout)
        process = subprocess.Popen(
            limited_argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            pass_fds=pass_fds,
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
                except ChildProcessError as error:
                    leader_identity_held = False
                    leader_exited = True
                    raise RuntimeError(f"{error_prefix}_process_identity_lost") from error
                if leader_status is not None:
                    leader_exited = True
                    break
                if (
                    os.fstat(stdout.fileno()).st_size > max_output_bytes
                    or os.fstat(stderr.fileno()).st_size > max_output_bytes
                ):
                    raise RuntimeError(f"{error_prefix}_output_limit_exceeded")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(limited_argv, timeout)
                time.sleep(min(0.01, remaining))
        except BaseException as error:
            try:
                _kill_and_reap_process(
                    process,
                    signal_process_group=leader_identity_held,
                    leader_exited=leader_exited,
                    error_prefix=error_prefix,
                )
            except BaseException:
                error.add_note(f"{error_prefix}_cleanup_failed")
            raise
        return_code = _kill_and_reap_process(
            process,
            signal_process_group=True,
            leader_exited=True,
            error_prefix=error_prefix,
        )
        stdout_size = os.fstat(stdout.fileno()).st_size
        stderr_size = os.fstat(stderr.fileno()).st_size
        if stdout_size > max_output_bytes or stderr_size > max_output_bytes:
            raise RuntimeError(f"{error_prefix}_output_limit_exceeded")
        stdout.seek(0)
        stderr.seek(0)
        try:
            stdout_text = stdout.read().decode("utf-8")
            stderr_text = stderr.read().decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError(f"{error_prefix}_output_invalid") from error
    return subprocess.CompletedProcess(
        args=limited_argv,
        returncode=return_code,
        stdout=stdout_text,
        stderr=stderr_text,
    )


def _kill_and_reap_process(
    process: subprocess.Popen[Any],
    *,
    signal_process_group: bool,
    leader_exited: bool,
    error_prefix: str,
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
        raise RuntimeError(f"{error_prefix}_cleanup_failed") from cleanup_error
    return return_code


def _validate_error_prefix(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-z]+(?:_[a-z]+)*", value) is None:
        raise ValueError("error_prefix must contain lowercase ASCII letters and underscores")


__all__ = ["require_root_controlled_executable", "run_bounded_command"]
