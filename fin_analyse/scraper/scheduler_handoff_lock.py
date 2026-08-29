"""Cooperative exclusion for ZSXQ scheduler runs and schedule handoff.

The lock deliberately does not claim to exclude a non-cooperating same-UID
writer.  It gives the canonical scheduled-run entry point and the future
cron-to-timer operator one shared inode on which to coordinate.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

_LOCK_NAME = "scheduler-handoff.lock"


class HandoffLockMode(StrEnum):
    SHARED = "SHARED"
    EXCLUSIVE = "EXCLUSIVE"


class SchedulerHandoffLockError(RuntimeError):
    """The cooperative handoff lock could not be proven safe."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SchedulerHandoffLockBusyError(SchedulerHandoffLockError):
    """Another cooperating scheduler owner currently holds the lock."""


def scheduler_handoff_lock_path(runtime_db: Path) -> Path:
    """Return the one lock path owned by a runtime-ledger directory."""

    if not runtime_db.is_absolute():
        raise ValueError("runtime_db_must_be_absolute")
    try:
        canonical = runtime_db.resolve(strict=False)
    except OSError:
        raise ValueError("runtime_db_must_be_canonical") from None
    if canonical != runtime_db:
        raise ValueError("runtime_db_must_be_canonical")
    return runtime_db.parent / _LOCK_NAME


def _require_bound_parent(descriptor: int, parent: Path) -> None:
    try:
        opened = os.fstat(descriptor)
        named = parent.stat(follow_symlinks=False)
    except OSError as error:
        raise SchedulerHandoffLockError(
            "scheduler_handoff_lock_parent_identity_unavailable"
        ) from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o700
        or not stat.S_ISDIR(named.st_mode)
        or named.st_uid != os.geteuid()
        or stat.S_IMODE(named.st_mode) != 0o700
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise SchedulerHandoffLockError("scheduler_handoff_lock_parent_unsafe")


def _open_bound_parent(parent: Path) -> int:
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise SchedulerHandoffLockError("scheduler_handoff_lock_parent_unavailable") from error
    try:
        _require_bound_parent(descriptor, parent)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_bound_lock(descriptor: int, parent_descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
        named = os.stat(
            _LOCK_NAME,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise SchedulerHandoffLockError("scheduler_handoff_lock_identity_unavailable") from error
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or not stat.S_ISREG(named.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise SchedulerHandoffLockError("scheduler_handoff_lock_unsafe")


@contextmanager
def hold_scheduler_handoff_lock(
    path: Path,
    *,
    mode: HandoffLockMode,
) -> Iterator[None]:
    """Hold one cooperative shared/exclusive lock across the caller's work."""

    if not path.is_absolute() or path.name != _LOCK_NAME:
        raise ValueError("scheduler_handoff_lock_path_invalid")
    if not isinstance(mode, HandoffLockMode):
        raise ValueError("scheduler_handoff_lock_mode_invalid")
    parent_descriptor = _open_bound_parent(path.parent)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            descriptor = os.open(
                _LOCK_NAME,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except OSError as error:
            raise SchedulerHandoffLockError("scheduler_handoff_lock_unavailable") from error
        try:
            _require_bound_parent(parent_descriptor, path.parent)
            _require_bound_lock(descriptor, parent_descriptor)
            operation = fcntl.LOCK_SH if mode is HandoffLockMode.SHARED else fcntl.LOCK_EX
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise SchedulerHandoffLockBusyError("scheduler_handoff_locked") from None
                raise SchedulerHandoffLockError("scheduler_handoff_lock_failed") from error
            _require_bound_parent(parent_descriptor, path.parent)
            _require_bound_lock(descriptor, parent_descriptor)
            try:
                yield
                _require_bound_parent(parent_descriptor, path.parent)
                _require_bound_lock(descriptor, parent_descriptor)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)
