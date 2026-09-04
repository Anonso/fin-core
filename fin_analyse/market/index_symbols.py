"""Major A-share index symbols served by the on-demand tactical lane.

Bare six-digit codes stay equity-only (000688 = 国城矿业, 000001 = 平安银行 —
the numeric namespace genuinely collides with indices). Indices enter by exact
alias or canonical qualified symbol; both resolve through one table so the
provider alias lane, the daily-bar tencent-first routing, and the quote-gap
suppression share a single definition. Design: docs/design/snapshot-index-support.md §2.1.
"""

from __future__ import annotations

# 规范显示名：符号 → 指数名（provider 层 name 标签用）。
INDEX_NAMES: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "399106.SZ": "深证综指",
}

# 别名/限定符键 → 规范符号（strip 后精确匹配；裸六位码不入键）。
INDEX_ALIASES: dict[str, str] = {
    "上证指数": "000001.SH",
    "上证": "000001.SH",
    "沪指": "000001.SH",
    "000001.SH": "000001.SH",
    "深证成指": "399001.SZ",
    "深成指": "399001.SZ",
    "399001.SZ": "399001.SZ",
    "创业板指": "399006.SZ",
    "399006.SZ": "399006.SZ",
    "科创50": "000688.SH",
    "科学家50": "000688.SH",
    "000688.SH": "000688.SH",
    "深证综指": "399106.SZ",
    "399106.SZ": "399106.SZ",
}

# 规范符号集：日线 tencent-first 路由判据、涨跌停 gap 抑制判据共用。
MAJOR_INDEX_SYMBOLS: frozenset[str] = frozenset(INDEX_NAMES)


def split_index_aliases(
    instruments: tuple[str, ...],
) -> tuple[tuple[str, ...], dict[str, str | None], tuple[str, ...]]:
    """Split inputs into (equity_targets, index_names_by_symbol, ordered_index_symbols).

    Alias hits are removed from the equity list so the shared equity resolver
    never sees them; duplicates across aliases (「上证指数」+「沪指」) dedupe by
    symbol and do not count as unresolved. Non-string entries pass through to
    the equity lane untouched.
    """

    equity: list[str] = []
    index_symbols: list[str] = []
    index_names: dict[str, str | None] = {}
    for value in instruments:
        matched = INDEX_ALIASES.get(value.strip()) if isinstance(value, str) else None
        if matched is None:
            equity.append(value)
        elif matched not in index_names:
            index_names[matched] = INDEX_NAMES[matched]
            index_symbols.append(matched)
    return tuple(equity), index_names, tuple(index_symbols)


__all__ = [
    "INDEX_ALIASES",
    "INDEX_NAMES",
    "MAJOR_INDEX_SYMBOLS",
    "split_index_aliases",
]
