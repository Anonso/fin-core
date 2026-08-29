"""JSONL repositories for cognition runtime data."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar


class _DictSerializable(Protocol):
    """Protocol for objects that support to_dict / from_dict round-trip."""

    def to_dict(self) -> dict[str, Any]: ...

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Any: ...


T = TypeVar("T", bound=_DictSerializable)
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_MAX_OWNER_ONLY_JSONL_BYTES = 8 * 1024 * 1024
_MAX_OWNER_ONLY_JSONL_ITEMS = 8_192


class OwnerOnlyJsonlReadError(ValueError):
    """Raised when an existing cognition JSONL cannot be trusted for production reads."""


def validate_existing_owner_only_directory(path: Path) -> None:
    """Validate one canonical existing owner-only directory without mutating it."""

    root = Path(path)
    if not root.is_absolute():
        raise OwnerOnlyJsonlReadError("owner_only_jsonl_root_invalid")
    try:
        if root.resolve(strict=True) != root:
            raise OwnerOnlyJsonlReadError("owner_only_jsonl_root_invalid")
        descriptor = os.open(root, _DIRECTORY_FLAGS)
    except (OSError, RuntimeError) as error:
        raise OwnerOnlyJsonlReadError("owner_only_jsonl_root_invalid") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OwnerOnlyJsonlReadError("owner_only_jsonl_root_invalid")
    finally:
        os.close(descriptor)


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


class JsonlRepository(Generic[T]):
    """Small JSONL repository with append, list, find, and id-based upsert."""

    def __init__(self, path: Path, model_type: type[T], id_field: str | None = None) -> None:
        self.path = path
        self.model_type = model_type
        self.id_field = id_field

    def append(self, item: T) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True)  # type: ignore[union-attr]
                + "\n"
            )

    def list_all(self) -> list[T]:
        if not self.path.exists():
            return []
        items: list[T] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    items.append(
                        self.model_type.from_dict(json.loads(stripped))  # type: ignore[union-attr]
                    )
        return items

    def find(self, predicate: Callable[[T], bool]) -> list[T]:
        return [item for item in self.list_all() if predicate(item)]

    def upsert(self, item: T) -> None:
        if self.id_field is None:
            raise ValueError("id_field is required for upsert")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        item_id = getattr(item, self.id_field)
        items = [
            existing for existing in self.list_all() if getattr(existing, self.id_field) != item_id
        ]
        items.append(item)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            for existing in items:
                fh.write(
                    json.dumps(existing.to_dict(), ensure_ascii=False, sort_keys=True)  # type: ignore[union-attr]
                    + "\n"
                )
        tmp_path.replace(self.path)


class OwnerOnlyReadJsonlRepository(JsonlRepository[T]):
    """Read-only stable JSONL view for FIN-trusted production cognition."""

    def append(self, item: T) -> None:
        del item
        raise PermissionError("owner_only_jsonl_read_only")

    def upsert(self, item: T) -> None:
        del item
        raise PermissionError("owner_only_jsonl_read_only")

    def list_all(self) -> list[T]:
        validate_existing_owner_only_directory(self.path.parent)
        directory_fd = os.open(self.path.parent, _DIRECTORY_FLAGS)
        try:
            directory_before = os.fstat(directory_fd)
            try:
                descriptor = os.open(self.path.name, _FILE_FLAGS, dir_fd=directory_fd)
            except FileNotFoundError:
                if _identity(os.fstat(directory_fd)) != _identity(directory_before):
                    raise OwnerOnlyJsonlReadError("owner_only_jsonl_directory_drift") from None
                return []
            except OSError as error:
                raise OwnerOnlyJsonlReadError("owner_only_jsonl_file_invalid") from error
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != os.geteuid()
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > _MAX_OWNER_ONLY_JSONL_BYTES
                ):
                    raise OwnerOnlyJsonlReadError("owner_only_jsonl_file_invalid")
                payload = bytearray()
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    payload.extend(chunk)
                    if len(payload) > _MAX_OWNER_ONLY_JSONL_BYTES:
                        raise OwnerOnlyJsonlReadError("owner_only_jsonl_file_oversized")
                after = os.fstat(descriptor)
                if _identity(after) != _identity(before):
                    raise OwnerOnlyJsonlReadError("owner_only_jsonl_file_drift")
            finally:
                os.close(descriptor)
            if _identity(os.fstat(directory_fd)) != _identity(directory_before):
                raise OwnerOnlyJsonlReadError("owner_only_jsonl_directory_drift")
        finally:
            os.close(directory_fd)

        try:
            text = bytes(payload).decode("utf-8")
            items: list[T] = []
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                value = json.loads(stripped)
                if not isinstance(value, dict):
                    raise OwnerOnlyJsonlReadError("owner_only_jsonl_item_invalid")
                items.append(self.model_type.from_dict(value))  # type: ignore[union-attr]
                if len(items) > _MAX_OWNER_ONLY_JSONL_ITEMS:
                    raise OwnerOnlyJsonlReadError("owner_only_jsonl_items_oversized")
            return items
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OwnerOnlyJsonlReadError("owner_only_jsonl_content_invalid") from error
