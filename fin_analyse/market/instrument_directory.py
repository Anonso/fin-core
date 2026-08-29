"""Strict, read-only A-share instrument identity directory.

The legacy name map contains both code and name keys and was built for
best-effort search.  Consultation needs a stronger contract: exact lookup,
one-to-many names, and independently verified venue families.
"""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from os import stat_result
from pathlib import Path
from typing import Protocol

_DEFAULT_RELATIVE_PATH = Path("runtime") / "a_share_name_map.json"
_MAX_BYTES = 4 * 1024 * 1024
_CODE = re.compile(r"^[0-9]{6}$")
_SH_EQUITY_PREFIXES = ("600", "601", "603", "605", "688", "689")
_SZ_EQUITY_PREFIXES = ("000", "001", "002", "003", "300", "301")
_MARKET_BY_VENUE = {"SH": "上交所", "SZ": "深交所"}


def _norm_name(value: str) -> str:
    """Normalize a stock name for lookup: strip all whitespace, casefold."""
    return re.sub(r"\s+", "", value).casefold()


@dataclass(frozen=True, slots=True)
class AShareInstrumentDirectoryEntry:
    symbol: str
    name: str


class AShareInstrumentDirectory(Protocol):
    def lookup(self, value: str) -> tuple[AShareInstrumentDirectoryEntry, ...]: ...


class RuntimeAshareInstrumentDirectory:
    """Reload the generated directory only when its file identity changes."""

    def __init__(self, *, path: Path | None = None) -> None:
        # BUG-007: resolve the production knowledge root via the single
        # seam; the stale repository-side mirror is never a fallback.
        from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

        self._path = path or (default_knowledge_base_root() / _DEFAULT_RELATIVE_PATH)
        self._signature: tuple[int, int, int, int] | None = None
        self._by_code: dict[str, tuple[AShareInstrumentDirectoryEntry, ...]] = {}
        self._by_name: dict[str, tuple[AShareInstrumentDirectoryEntry, ...]] = {}

    def lookup(self, value: str) -> tuple[AShareInstrumentDirectoryEntry, ...]:
        self._reload_if_changed()
        normalized = value.strip()
        if _CODE.fullmatch(normalized) is not None:
            return self._by_code.get(normalized, ())
        return self._by_name.get(_norm_name(normalized), ())

    def _reload_if_changed(self) -> None:
        try:
            file_stat = self._path.stat()
        except OSError:
            self._signature = None
            self._by_code = {}
            self._by_name = {}
            return
        signature = (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
        )
        if signature == self._signature:
            return
        by_code, by_name = _read_directory(self._path, file_stat=file_stat)
        self._signature = signature
        self._by_code = by_code
        self._by_name = by_name


def verified_a_share_equity_venue(code: str) -> str | None:
    """Return the independently verified SH/SZ equity venue for a six-digit code."""

    if _CODE.fullmatch(code) is None:
        return None
    if code.startswith(_SH_EQUITY_PREFIXES):
        return "SH"
    if code.startswith(_SZ_EQUITY_PREFIXES):
        return "SZ"
    return None


def _read_directory(
    path: Path,
    *,
    file_stat: stat_result,
) -> tuple[
    dict[str, tuple[AShareInstrumentDirectoryEntry, ...]],
    dict[str, tuple[AShareInstrumentDirectoryEntry, ...]],
]:
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_size <= 0
        or file_stat.st_size > _MAX_BYTES
    ):
        return {}, {}
    try:
        raw_bytes = path.read_bytes()
        if len(raw_bytes) != file_stat.st_size or len(raw_bytes) > _MAX_BYTES:
            return {}, {}
        raw = json.loads(raw_bytes, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}, {}
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), dict):
        return {}, {}
    entries = raw["entries"]
    expected_count = raw.get("count")
    raw_code_count = sum(
        1 for key in entries if isinstance(key, str) and _CODE.fullmatch(key) is not None
    )
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count <= 0
        or raw_code_count != expected_count
    ):
        return {}, {}
    code_entries: dict[str, AShareInstrumentDirectoryEntry] = {}
    for key, candidate in entries.items():
        if not isinstance(key, str) or _CODE.fullmatch(key) is None:
            continue
        entry = _validated_entry(key, candidate)
        if entry is not None:
            code_entries[key] = entry

    names: dict[str, list[AShareInstrumentDirectoryEntry]] = {}
    for entry in code_entries.values():
        names.setdefault(_norm_name(entry.name), []).append(entry)
    return (
        {code: (entry,) for code, entry in code_entries.items()},
        {name: tuple(sorted(group, key=lambda item: item.symbol)) for name, group in names.items()},
    )


def _validated_entry(
    code: str,
    raw: object,
) -> AShareInstrumentDirectoryEntry | None:
    if not isinstance(raw, Mapping):
        return None
    ticker = raw.get("ticker")
    market = raw.get("market")
    name = raw.get("name")
    venue = verified_a_share_equity_venue(code)
    if (
        ticker != code
        or venue is None
        or market != _MARKET_BY_VENUE[venue]
        or not isinstance(name, str)
        or not name.strip()
        or len(name.strip()) > 128
    ):
        return None
    return AShareInstrumentDirectoryEntry(symbol=f"{code}.{venue}", name=name.strip())


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = [
    "AShareInstrumentDirectory",
    "AShareInstrumentDirectoryEntry",
    "RuntimeAshareInstrumentDirectory",
    "verified_a_share_equity_venue",
]
