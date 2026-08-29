"""JSONL/JSON file I/O with safe defaults (locking, fsync, atomic writes)."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Atomically append a JSON line with file locking and fsync.

    Safe for concurrent writers from the same machine (fcntl advisory lock).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSONL records, skipping corrupted or partial lines."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip corrupted partial line from interrupted write
                continue
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Overwrite a JSONL file (used for upsert semantics)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def write_json(path: Path, record: dict[str, Any]) -> None:
    """Atomically write a JSON file using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(record, ensure_ascii=False, indent=2, default=str)
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read a JSON file, returning *default* (or ``{}``) if missing."""
    if not path.exists():
        return {} if default is None else dict(default)
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data
