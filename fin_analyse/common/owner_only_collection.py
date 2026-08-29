"""Owner-only multi-entry collection primitives for private diagnostics sinks.

Shared by sinks that persist one bounded entry per event in a flat owner-only
directory with ring eviction.  The primitives own filesystem safety only:
every directory component is traversed with ``O_NOFOLLOW``, terminal
directories must be real 0700 directories owned by the current euid, regular
files must be 0600/euid-owned/single-link, and removal only happens on files
that passed the same verification.  Domain modules own naming, JSON and
semantic validation.
"""

from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path
from typing import Any

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_RDONLY_DIR = os.O_RDONLY | _CLOEXEC | _DIRECTORY | _NOFOLLOW
_RDONLY_FILE = os.O_RDONLY | _CLOEXEC | _NOFOLLOW | _NONBLOCK

_RENAME_NOREPLACE = 1
_renameat2: Any = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
if _renameat2 is not None:
    _renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _renameat2.restype = ctypes.c_int


class OwnerOnlyCollectionError(RuntimeError):
    """Collection filesystem contract violation (unverified path or file)."""


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return (metadata.st_dev, metadata.st_ino)


def _stable(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_owner_regular(
    metadata: os.stat_result,
    named: os.stat_result,
    *,
    name: str,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or _identity(metadata) != _identity(named)
    ):
        raise OwnerOnlyCollectionError(f"insecure file: {name}")


def require_owner_directory(path: Path, *, create: bool) -> int:
    """Open (optionally create) an owner-only directory chain; return final fd.

    Mirrors the session-store chain semantics: components are traversed only
    with ``O_NOFOLLOW`` (symlink mid-chain components are rejected), and the
    terminal directory must be a real 0700 directory owned by the current
    euid.  Every acquired fd is closed exactly once on every path.
    """
    if not path.is_absolute():
        raise OwnerOnlyCollectionError("collection path must be absolute")
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
                    raise OwnerOnlyCollectionError(f"missing directory: {path}") from None
                os.mkdir(part, 0o700, dir_fd=parent)
                os.fsync(parent)
                current = os.open(part, _RDONLY_DIR, dir_fd=parent)
            except OSError as error:
                raise OwnerOnlyCollectionError(f"unopenable directory: {path}") from error
            opened.pop()
            os.close(parent)
            opened.append(current)
        metadata = os.fstat(current)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OwnerOnlyCollectionError(f"insecure directory: {path}")
        opened.pop()
        return current
    finally:
        for fd in opened:
            os.close(fd)


def read_owner_regular(parent_fd: int, name: str, *, max_bytes: int) -> bytes:
    """Verify and read a bounded owner-only regular file (no-follow).

    The verification contract is the same as ``require_owner_directory``
    regular-file invariants (0600, euid, single link, identity match); the
    content is read through the verified descriptor and re-checked for
    stability before being returned.  Symlinks and hardlinks are refused.
    The descriptor is closed exactly once on every path.
    """
    try:
        descriptor = os.open(name, _RDONLY_FILE, dir_fd=parent_fd)
    except OSError as error:
        raise OwnerOnlyCollectionError(f"unopenable file: {name}") from error
    try:
        before = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_owner_regular(before, named, name=name)
        if before.st_size > max_bytes:
            raise OwnerOnlyCollectionError(f"entry exceeds byte bound: {name}")
        chunks: list[bytes] = []
        size = 0
        while True:
            try:
                chunk = os.read(descriptor, 64 * 1024)
            except OSError as error:
                raise OwnerOnlyCollectionError(f"read failed: {name}") from error
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise OwnerOnlyCollectionError(f"entry exceeds byte bound: {name}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _stable(before) != _stable(after) or _identity(after) != _identity(named_after):
            raise OwnerOnlyCollectionError(f"unstable file: {name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def verify_owner_regular(parent_fd: int, name: str, *, max_bytes: int) -> os.stat_result:
    """Verify an owner-only regular file (no-follow) without reading content.

    Same invariants as ``read_owner_regular`` (regular, 0600, euid, single
    link, identity match, size bound); used by sweep/removal paths that must
    only ever touch verified files.
    """
    try:
        descriptor = os.open(name, _RDONLY_FILE, dir_fd=parent_fd)
    except OSError as error:
        raise OwnerOnlyCollectionError(f"unopenable file: {name}") from error
    try:
        metadata = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_owner_regular(metadata, named, name=name)
        if metadata.st_size > max_bytes:
            raise OwnerOnlyCollectionError(f"entry exceeds byte bound: {name}")
        return metadata
    finally:
        os.close(descriptor)


def rename_noreplace(
    src_dir_fd: int,
    src_name: str,
    dst_dir_fd: int,
    dst_name: str,
) -> None:
    """Atomically rename without ever replacing an existing destination."""
    if _renameat2 is None:
        raise OSError("renameat2 unavailable")
    result = _renameat2(
        src_dir_fd,
        os.fsencode(src_name),
        dst_dir_fd,
        os.fsencode(dst_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), dst_name)


__all__ = [
    "OwnerOnlyCollectionError",
    "read_owner_regular",
    "rename_noreplace",
    "require_owner_directory",
    "verify_owner_regular",
]
