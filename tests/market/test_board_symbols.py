"""Board alias lane tests (board-index-support §2.1, design archived at merge).

The table lives in config/market/board_symbols.json (家规 6: growing lists go
to config); the loader is fail-closed — malformed or colliding tables must
raise at import, never reroute silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin_analyse.market.board_symbols import (
    BOARD_ALIASES,
    BOARD_NAMES,
    BOARD_SYMBOLS,
    _load_boards,
    split_board_aliases,
)


def test_seed_table_loads_with_expected_shape() -> None:
    assert len(BOARD_SYMBOLS) == 13
    assert BOARD_NAMES["02GN2224.PT"] == "液冷"
    assert BOARD_ALIASES["液冷"] == "02GN2224.PT"


def test_canonical_symbol_is_its_own_alias() -> None:
    """规范符号直传直接命中（Q1-P2），与 index lane 限定符直传能力对称。"""

    for symbol in BOARD_SYMBOLS:
        assert BOARD_ALIASES[symbol] == symbol


def test_split_extracts_board_and_leaves_rest_untouched() -> None:
    remaining, names, symbols = split_board_aliases(
        ("液冷", "601899.SH", "科创50", "半导体", 42, "02GN2224.PT")
    )

    assert symbols == ("02GN2224.PT", "01801081.PT")
    assert names == {"02GN2224.PT": "液冷", "01801081.PT": "半导体"}
    assert remaining == ("601899.SH", "科创50", 42)


def test_alias_and_canonical_duplicate_dedupes_by_symbol() -> None:
    _remaining, names, symbols = split_board_aliases(("液冷", "液冷概念", "02GN2224.PT"))

    assert symbols == ("02GN2224.PT",)
    assert names == {"02GN2224.PT": "液冷"}


def test_unknown_name_stays_in_remaining() -> None:
    remaining, names, symbols = split_board_aliases(("光模块",))

    assert symbols == ()
    assert names == {}
    assert remaining == ("光模块",)


def _write_config(tmp_path: Path, payload: object) -> Path:
    config = tmp_path / "board_symbols.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    return config


def test_loader_rejects_alias_collision_across_boards(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        {
            "boards": {
                "01801081.PT": {"name": "半导体", "aliases": ["芯片"]},
                "01801083.PT": {"name": "元件", "aliases": ["芯片"]},
            }
        },
    )

    with pytest.raises(ValueError, match="collides across boards"):
        _load_boards(config)


def test_loader_rejects_malformed_symbol(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        {"boards": {"BK1031.PT": {"name": "半导体", "aliases": ["半导体"]}}},
    )

    with pytest.raises(ValueError, match="malformed"):
        _load_boards(config)


def test_loader_rejects_empty_board_set(tmp_path: Path) -> None:
    config = _write_config(tmp_path, {"boards": {}})

    with pytest.raises(ValueError, match="non-empty"):
        _load_boards(config)


def test_loader_rejects_board_without_aliases(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        {"boards": {"01801081.PT": {"name": "半导体", "aliases": []}}},
    )

    with pytest.raises(ValueError, match="aliases are missing"):
        _load_boards(config)
