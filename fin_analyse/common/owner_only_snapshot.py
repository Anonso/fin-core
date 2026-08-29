"""Owner-only immutable JSON snapshot storage primitive.

The primitive owns filesystem safety and compare-and-swap publication. Domain
modules own JSON decoding and semantic validation through injected decoders;
file descriptors, locks and rename details never cross this public seam.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class OwnerOnlySnapshotMissingError(RuntimeError):
    pass


class OwnerOnlySnapshotInvalidError(RuntimeError):
    pass


class OwnerOnlySnapshotReason(StrEnum):
    APPLY_REQUIRED = "APPLY_REQUIRED"
    SOURCE_INVALID = "SOURCE_INVALID"
    CANDIDATE_MISMATCH = "CANDIDATE_MISMATCH"
    CURRENT_INVALID = "CURRENT_INVALID"
    CAS_MISMATCH = "CAS_MISMATCH"
    TARGET_INVALID = "TARGET_INVALID"
    TARGET_CHANGED = "TARGET_CHANGED"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    INCOMPATIBLE = "INCOMPATIBLE"
    WRITE_FAILED = "WRITE_FAILED"


class OwnerOnlySnapshotInspectionError(RuntimeError):
    def __init__(self, reason: OwnerOnlySnapshotReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class OwnerOnlySnapshotValue(Generic[T]):
    value: T
    revision: str


@dataclass(frozen=True, slots=True)
class OwnerOnlySnapshotInspection(Generic[T]):
    candidate: OwnerOnlySnapshotValue[T]
    current: OwnerOnlySnapshotValue[T] | None


@dataclass(frozen=True, slots=True)
class OwnerOnlySnapshotPublication(Generic[T]):
    status: str
    reason: OwnerOnlySnapshotReason | None
    candidate: OwnerOnlySnapshotValue[T] | None
    current: OwnerOnlySnapshotValue[T] | None
    writes_state: bool


class _DirectoryPreparationError(OwnerOnlySnapshotInvalidError):
    def __init__(self, *, writes_state: bool) -> None:
        super().__init__()
        self.writes_state = writes_state


class _AfterReplaceError(OSError):
    pass


class OwnerOnlyJsonSnapshotFile:
    """One fixed target with stable reads and CAS publication."""

    def __init__(
        self,
        *,
        target: Path,
        forbidden_root: Path,
        max_bytes: int = 64 * 1024,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._target = Path(os.path.abspath(target))
        self._forbidden_root = forbidden_root.resolve()
        self._max_bytes = max_bytes

    def read(self) -> bytes:
        return _read_owner_only(
            self._target,
            forbidden_root=self._forbidden_root,
            max_bytes=self._max_bytes,
        )

    def inspect(
        self,
        *,
        source: Path,
        decode_candidate: Callable[[bytes], T],
        decode_current: Callable[[bytes], T],
    ) -> OwnerOnlySnapshotInspection[T]:
        try:
            candidate = self._read_source(source, decode=decode_candidate)
        except (
            OwnerOnlySnapshotMissingError,
            OwnerOnlySnapshotInvalidError,
            OSError,
            ValueError,
            TypeError,
        ):
            raise OwnerOnlySnapshotInspectionError(OwnerOnlySnapshotReason.SOURCE_INVALID) from None
        try:
            current = self._read_current(decode=decode_current)
        except (OwnerOnlySnapshotInvalidError, OSError, ValueError, TypeError):
            raise OwnerOnlySnapshotInspectionError(
                OwnerOnlySnapshotReason.CURRENT_INVALID
            ) from None
        return OwnerOnlySnapshotInspection(candidate=candidate, current=current)

    def publish(
        self,
        *,
        source: Path,
        candidate_revision: str,
        expected_current_revision: str,
        apply: bool,
        decode_candidate: Callable[[bytes], T],
        decode_current: Callable[[bytes], T],
        compatible: Callable[[T, T | None], bool] | None = None,
    ) -> OwnerOnlySnapshotPublication[T]:
        if not apply:
            return _rejected(OwnerOnlySnapshotReason.APPLY_REQUIRED)
        try:
            inspection = self.inspect(
                source=source,
                decode_candidate=decode_candidate,
                decode_current=decode_current,
            )
        except OwnerOnlySnapshotInspectionError as error:
            return _rejected(error.reason)
        if inspection.candidate.revision != candidate_revision:
            return _rejected(
                OwnerOnlySnapshotReason.CANDIDATE_MISMATCH,
                candidate=inspection.candidate,
                current=inspection.current,
            )
        if _revision(inspection.current) != expected_current_revision:
            return _rejected(
                OwnerOnlySnapshotReason.CAS_MISMATCH,
                candidate=inspection.candidate,
                current=inspection.current,
            )
        if compatible is not None and not compatible(
            inspection.candidate.value,
            inspection.current.value if inspection.current is not None else None,
        ):
            return _rejected(
                OwnerOnlySnapshotReason.INCOMPATIBLE,
                candidate=inspection.candidate,
                current=inspection.current,
            )

        try:
            directory_fd, parent_identity, prepared = _ensure_owner_directory(
                self._target.parent,
                forbidden_root=self._forbidden_root,
            )
        except _DirectoryPreparationError as error:
            return _rejected(
                OwnerOnlySnapshotReason.TARGET_INVALID,
                writes_state=error.writes_state,
            )
        except (OwnerOnlySnapshotInvalidError, OSError):
            return _rejected(OwnerOnlySnapshotReason.TARGET_INVALID)
        publish_attempted = False
        try:
            import fcntl

            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            if not _fixed_parent_matches(self._target.parent, parent_identity):
                return _rejected(
                    OwnerOnlySnapshotReason.TARGET_CHANGED,
                    writes_state=prepared,
                )
            try:
                current = self._read_current_at(directory_fd, decode=decode_current)
            except (OwnerOnlySnapshotInvalidError, OSError, ValueError, TypeError):
                return _rejected(
                    OwnerOnlySnapshotReason.CURRENT_INVALID,
                    writes_state=prepared,
                )
            try:
                refreshed = self._read_source(source, decode=decode_candidate)
            except (
                OwnerOnlySnapshotMissingError,
                OwnerOnlySnapshotInvalidError,
                OSError,
                ValueError,
            ):
                return _rejected(
                    OwnerOnlySnapshotReason.SOURCE_CHANGED,
                    writes_state=prepared,
                )
            if refreshed.revision != candidate_revision:
                return _rejected(
                    OwnerOnlySnapshotReason.SOURCE_CHANGED,
                    writes_state=prepared,
                )
            if _revision(current) != expected_current_revision:
                return _rejected(
                    OwnerOnlySnapshotReason.CAS_MISMATCH,
                    candidate=refreshed,
                    current=current,
                    writes_state=prepared,
                )
            if compatible is not None and not compatible(
                refreshed.value,
                current.value if current is not None else None,
            ):
                return _rejected(
                    OwnerOnlySnapshotReason.INCOMPATIBLE,
                    candidate=refreshed,
                    current=current,
                    writes_state=prepared,
                )
            if current is not None and current.revision == refreshed.revision:
                return OwnerOnlySnapshotPublication(
                    status="EXACT_REPLAY",
                    reason=None,
                    candidate=refreshed,
                    current=current,
                    writes_state=prepared,
                )
            if not _fixed_parent_matches(self._target.parent, parent_identity):
                return _rejected(
                    OwnerOnlySnapshotReason.TARGET_CHANGED,
                    writes_state=prepared,
                )
            try:
                payload = _read_owner_only(
                    source,
                    forbidden_root=self._forbidden_root,
                    max_bytes=self._max_bytes,
                )
            except (
                OwnerOnlySnapshotMissingError,
                OwnerOnlySnapshotInvalidError,
                OSError,
            ):
                return _rejected(
                    OwnerOnlySnapshotReason.SOURCE_CHANGED,
                    writes_state=prepared,
                )
            if _sha_revision(payload) != refreshed.revision:
                return _rejected(
                    OwnerOnlySnapshotReason.SOURCE_CHANGED,
                    writes_state=prepared,
                )
            # From this point onward the operation may have written a temporary
            # file or replaced the target even when durability verification
            # subsequently fails.  Audit conservatively: a failed publish
            # attempt is never reported as a zero-write operation.
            publish_attempted = True
            _atomic_publish(
                directory_fd,
                self._target.name,
                payload,
                max_bytes=self._max_bytes,
            )
            if not _fixed_parent_matches(self._target.parent, parent_identity):
                return _rejected(OwnerOnlySnapshotReason.TARGET_CHANGED, writes_state=True)
            committed = self._read_current_at(directory_fd, decode=decode_current)
            if committed is None or committed.revision != refreshed.revision:
                return _rejected(OwnerOnlySnapshotReason.WRITE_FAILED, writes_state=True)
            return OwnerOnlySnapshotPublication(
                status="PUBLISHED",
                reason=None,
                candidate=refreshed,
                current=committed,
                writes_state=True,
            )
        except (
            OwnerOnlySnapshotInvalidError,
            _AfterReplaceError,
            OSError,
            ValueError,
            TypeError,
        ):
            return _rejected(
                OwnerOnlySnapshotReason.WRITE_FAILED,
                writes_state=publish_attempted or prepared,
            )
        finally:
            with suppress(OSError):
                import fcntl

                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            os.close(directory_fd)

    def _read_source(
        self,
        source: Path,
        *,
        decode: Callable[[bytes], T],
    ) -> OwnerOnlySnapshotValue[T]:
        if not source.is_absolute():
            raise OwnerOnlySnapshotInvalidError
        payload = _read_owner_only(
            source,
            forbidden_root=self._forbidden_root,
            max_bytes=self._max_bytes,
        )
        return OwnerOnlySnapshotValue(value=decode(payload), revision=_sha_revision(payload))

    def _read_current(
        self,
        *,
        decode: Callable[[bytes], T],
    ) -> OwnerOnlySnapshotValue[T] | None:
        try:
            payload = self.read()
        except OwnerOnlySnapshotMissingError:
            return None
        return OwnerOnlySnapshotValue(value=decode(payload), revision=_sha_revision(payload))

    def _read_current_at(
        self,
        directory_fd: int,
        *,
        decode: Callable[[bytes], T],
    ) -> OwnerOnlySnapshotValue[T] | None:
        try:
            payload = _read_owner_only_at(
                directory_fd,
                self._target.name,
                max_bytes=self._max_bytes,
            )
        except OwnerOnlySnapshotMissingError:
            return None
        return OwnerOnlySnapshotValue(value=decode(payload), revision=_sha_revision(payload))


def _rejected(
    reason: OwnerOnlySnapshotReason,
    *,
    candidate: OwnerOnlySnapshotValue | None = None,
    current: OwnerOnlySnapshotValue | None = None,
    writes_state: bool = False,
) -> OwnerOnlySnapshotPublication:
    return OwnerOnlySnapshotPublication(
        status="REJECTED",
        reason=reason,
        candidate=candidate,
        current=current,
        writes_state=writes_state,
    )


def _revision(value: OwnerOnlySnapshotValue | None) -> str:
    return "MISSING" if value is None else value.revision


def _sha_revision(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _read_owner_only(path: Path, *, forbidden_root: Path, max_bytes: int) -> bytes:
    if not path.is_absolute():
        raise OwnerOnlySnapshotInvalidError
    path = Path(os.path.abspath(path))
    if path == forbidden_root or forbidden_root in path.parents:
        raise OwnerOnlySnapshotInvalidError
    parent_fd = terminal_parent_fd = file_fd = -1
    try:
        parent_fd, parent_chain = _open_owner_parent(path.parent)
        try:
            file_fd = os.open(
                path.name,
                os.O_RDONLY | _CLOEXEC | _NOFOLLOW | _NONBLOCK,
                dir_fd=parent_fd,
            )
        except FileNotFoundError as error:
            raise OwnerOnlySnapshotMissingError from error
        except OSError as error:
            raise OwnerOnlySnapshotInvalidError from error
        before = os.fstat(file_fd)
        named_before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        _validate_file(before, named_before)
        payload = _read_bounded_twice(file_fd, before.st_size, max_bytes=max_bytes)
        after = os.fstat(file_fd)
        terminal_parent_fd, terminal_chain = _open_owner_parent(path.parent)
        named_after = os.stat(path.name, dir_fd=terminal_parent_fd, follow_symlinks=False)
        if (
            _stable_file(before) != _stable_file(after)
            or _identity(after) != _identity(named_after)
            or parent_chain != terminal_chain
        ):
            raise OwnerOnlySnapshotInvalidError
        return payload
    except OSError as error:
        raise OwnerOnlySnapshotInvalidError from error
    finally:
        for descriptor in (file_fd, terminal_parent_fd, parent_fd):
            if descriptor >= 0:
                os.close(descriptor)


def _read_owner_only_at(directory_fd: int, name: str, *, max_bytes: int) -> bytes:
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | _CLOEXEC | _NOFOLLOW | _NONBLOCK,
                dir_fd=directory_fd,
            )
        except FileNotFoundError as error:
            raise OwnerOnlySnapshotMissingError from error
        except OSError as error:
            raise OwnerOnlySnapshotInvalidError from error
        before = os.fstat(descriptor)
        named_before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_file(before, named_before)
        payload = _read_bounded_twice(descriptor, before.st_size, max_bytes=max_bytes)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _stable_file(before) != _stable_file(after) or _identity(after) != _identity(
            named_after
        ):
            raise OwnerOnlySnapshotInvalidError
        return payload
    except OSError as error:
        raise OwnerOnlySnapshotInvalidError from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_owner_parent(path: Path) -> tuple[int, tuple[tuple[int, int], ...]]:
    descriptor = -1
    try:
        descriptor = os.open(path.anchor, os.O_RDONLY | _CLOEXEC | _DIRECTORY | _NOFOLLOW)
        identities = [_identity(os.fstat(descriptor))]
        metadata = os.fstat(descriptor)
        for component in path.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | _CLOEXEC | _DIRECTORY | _NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
            metadata = os.fstat(descriptor)
            identities.append(_identity(metadata))
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise OwnerOnlySnapshotInvalidError
        result = descriptor
        descriptor = -1
        return result, tuple(identities)
    except FileNotFoundError as error:
        raise OwnerOnlySnapshotMissingError from error
    except OSError as error:
        raise OwnerOnlySnapshotInvalidError from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _ensure_owner_directory(
    path: Path,
    *,
    forbidden_root: Path,
) -> tuple[int, tuple[int, int], bool]:
    if not path.is_absolute() or path == forbidden_root or forbidden_root in path.parents:
        raise OwnerOnlySnapshotInvalidError
    descriptor = os.open(path.anchor, os.O_RDONLY | _CLOEXEC | _DIRECTORY | _NOFOLLOW)
    prepared = False
    try:
        for component in path.parts[1:]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                prepared = True
            except FileExistsError:
                pass
            next_descriptor = os.open(
                component,
                os.O_RDONLY | _CLOEXEC | _DIRECTORY | _NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise OwnerOnlySnapshotInvalidError
        result = descriptor
        descriptor = -1
        return result, _identity(metadata), prepared
    except (OwnerOnlySnapshotInvalidError, OSError) as error:
        raise _DirectoryPreparationError(writes_state=prepared) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fixed_parent_matches(path: Path, expected: tuple[int, int]) -> bool:
    descriptor = -1
    try:
        descriptor, _ = _open_owner_parent(path)
        return _identity(os.fstat(descriptor)) == expected
    except (OwnerOnlySnapshotInvalidError, OwnerOnlySnapshotMissingError, OSError):
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_publish(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    max_bytes: int,
    write_all: Callable[[int, bytes], None] | None = None,
) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    replaced = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, 0o600)
        (write_all or _write_all)(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        verify = os.open(temporary, os.O_RDONLY | _CLOEXEC | _NOFOLLOW, dir_fd=directory_fd)
        try:
            if _read_bounded(verify, max_bytes=max_bytes) != payload:
                raise OSError("temporary snapshot verification failed")
        finally:
            os.close(verify)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        replaced = True
        os.fsync(directory_fd)
    except OSError as error:
        if replaced:
            raise _AfterReplaceError from error
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory_fd)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("snapshot write failed")
        view = view[written:]


def _read_bounded_twice(descriptor: int, size: int, *, max_bytes: int) -> bytes:
    if size <= 0 or size > max_bytes:
        raise OwnerOnlySnapshotInvalidError
    payload = _read_bounded(descriptor, max_bytes=max_bytes)
    os.lseek(descriptor, 0, os.SEEK_SET)
    if _read_bounded(descriptor, max_bytes=max_bytes) != payload:
        raise OwnerOnlySnapshotInvalidError
    return payload


def _read_bounded(descriptor: int, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(descriptor, 65536):
        size += len(chunk)
        if size > max_bytes:
            raise OwnerOnlySnapshotInvalidError
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_file(opened: os.stat_result, named: os.stat_result) -> None:
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or _identity(opened) != _identity(named)
    ):
        raise OwnerOnlySnapshotInvalidError


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _stable_file(metadata: os.stat_result) -> tuple[int, ...]:
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


__all__ = [
    "OwnerOnlyJsonSnapshotFile",
    "OwnerOnlySnapshotInspection",
    "OwnerOnlySnapshotInspectionError",
    "OwnerOnlySnapshotInvalidError",
    "OwnerOnlySnapshotMissingError",
    "OwnerOnlySnapshotPublication",
    "OwnerOnlySnapshotReason",
    "OwnerOnlySnapshotValue",
]
