"""macro_brain helpers unit tests."""

from __future__ import annotations

import json
from pathlib import Path

from fin_analyse.guo_teacher_research.macro_brain import (
    load_shared_brain_cards,
    macro_search_signal,
    match_shared_brain_cards,
    suggested_queries,
)


def _cards(tmp_path: Path) -> list[dict]:
    path = tmp_path / "runtime" / "shared_brain" / "items.jsonl"
    path.parent.mkdir(parents=True)
    cards = [
        {
            "item_id": "c1",
            "title": "政治经济学入口问题清单",
            "summary": "政策分析入口 checklist，宏观评估用",
            "scope": "methodology_memory",
            "is_g_source": False,
            "updated_at": "2026-08-01T00:00:00+08:00",
        },
        {
            "item_id": "c2",
            "title": "公司研报方法论",
            "summary": "公司层面估值",
            "scope": "shared_brain_framework",
            "is_g_source": False,
            "updated_at": "2026-08-02T00:00:00+08:00",
        },
        {
            "item_id": "c3",
            "title": "市场数据卡片",
            "summary": "行情数据",
            "scope": "market_data",
            "is_g_source": False,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(card, ensure_ascii=False) for card in cards) + "\n",
        encoding="utf-8",
    )
    return cards


def test_load_scopes_exclude_market_data(tmp_path: Path) -> None:
    _cards(tmp_path)
    cards = load_shared_brain_cards(tmp_path)
    assert [card["item_id"] for card in cards] == ["c1", "c2"]


def test_match_and_search_signal(tmp_path: Path) -> None:
    _cards(tmp_path)
    cards = load_shared_brain_cards(tmp_path)
    matched = match_shared_brain_cards(cards, "宏观政策分析")
    assert [card["item_id"] for card in matched] == ["c1"]
    assert macro_search_signal("当前宏观流动性和美联储怎么看")
    assert not macro_search_signal("通富微电研报评分")
    assert suggested_queries("宏观政策")
