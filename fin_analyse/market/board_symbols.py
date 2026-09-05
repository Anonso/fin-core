"""Tencent sector/concept board symbols served by the on-demand board lane.

Canonical symbols are ``{code}.PT`` where code is Tencent's 8-character board
code (``01801081`` industry, ``02GN2224`` concept). Boards are Tencent-published
aggregate statistics with no cross-source twin, so the lane is single-source by
design. The universe grows with owner usage, so the table lives in
``config/market/board_symbols.json`` (家规 6: changing lists go to config) —
unlike the closed major-index table in ``index_symbols.py``. The loader is
fail-closed at import: a malformed or colliding table must be loud, never a
silent routing change. Design: docs/design/board-index-support.md §2.1 (archived
at merge, git f475220).

扩面警示（config `_note` 同文）：新增板块避开主指数别名与常见个股全名——
板块别名在 snapshot 拆分顺序中最优先，撞名会改变既有问询行为。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _PROJECT_ROOT / "config" / "market" / "board_symbols.json"
_BOARD_KEY = re.compile(r"^[0-9A-Z]{8}\.PT$")


def _load_boards(config_path: Path) -> tuple[dict[str, str], dict[str, str], frozenset[str]]:
    """Parse and validate the board table; raise ValueError on any malformation."""

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("board symbols config is unreadable") from error
    if not isinstance(payload, dict):
        raise ValueError("board symbols config must be an object")
    boards = payload.get("boards")
    if not isinstance(boards, dict) or not boards:
        raise ValueError("board symbols config requires a non-empty boards object")
    names: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for symbol, entry in boards.items():
        if not isinstance(symbol, str) or _BOARD_KEY.fullmatch(symbol) is None:
            raise ValueError(f"board symbol is malformed: {symbol!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"board entry must be an object: {symbol!r}")
        name = entry.get("name")
        raw_aliases = entry.get("aliases")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"board name is missing: {symbol!r}")
        if not isinstance(raw_aliases, list) or not raw_aliases:
            raise ValueError(f"board aliases are missing: {symbol!r}")
        if symbol in names:
            raise ValueError(f"board symbol is duplicated: {symbol!r}")
        names[symbol] = name
        # 规范符号自身入键（Q1-P2）：直传 `02GN2224.PT` 直接命中，与 index lane
        # 的 qualified symbol 直传能力对称，不落 equity 解析器误报 UNRESOLVED。
        cleaned = [symbol, *(alias for alias in raw_aliases)]
        for alias in cleaned:
            if not isinstance(alias, str) or not alias.strip():
                raise ValueError(f"board alias is malformed: {symbol!r}")
            key = alias.strip()
            existing = aliases.get(key)
            if existing is not None and existing != symbol:
                raise ValueError(
                    f"board alias collides across boards: {key!r} -> {existing!r}/{symbol!r}"
                )
            aliases[key] = symbol
    return aliases, names, frozenset(names)


def split_board_aliases(
    instruments: tuple[str, ...],
) -> tuple[tuple[str, ...], dict[str, str | None], tuple[str, ...]]:
    """Split inputs into (non_board_targets, board_names_by_symbol, ordered_board_symbols).

    Alias hits are removed from the remaining list so the index lane and the
    shared equity resolver never see them; duplicates across aliases dedupe by
    symbol and do not count as unresolved. Non-string entries pass through.
    """

    remaining: list[str] = []
    board_symbols: list[str] = []
    board_names: dict[str, str | None] = {}
    for value in instruments:
        matched = BOARD_ALIASES.get(value.strip()) if isinstance(value, str) else None
        if matched is None:
            remaining.append(value)
        elif matched not in board_names:
            board_names[matched] = BOARD_NAMES[matched]
            board_symbols.append(matched)
    return tuple(remaining), board_names, tuple(board_symbols)


BOARD_ALIASES, BOARD_NAMES, BOARD_SYMBOLS = _load_boards(_CONFIG_PATH)

__all__ = [
    "BOARD_ALIASES",
    "BOARD_NAMES",
    "BOARD_SYMBOLS",
    "split_board_aliases",
]
