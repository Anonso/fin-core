"""macro_brain helpers unit tests."""

from __future__ import annotations

import json
from pathlib import Path

from fin_analyse.guo_teacher_research.macro_brain import (
    MacroBrainQueryReader,
    load_shared_brain_cards,
    macro_search_signal,
    match_shared_brain_cards,
    suggested_queries,
    zsxq_macro_articles,
)
from fin_analyse.cognition.macro_index import load_rules, update_macro_index


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


def test_reader_aggregates_zsxq_and_cards(tmp_path: Path) -> None:
    _cards(tmp_path)
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "articles": [
                    {
                        "id": "zsxq-macro-1",
                        "title": "当前市场流动性与大类资产复盘",
                        "column": "普通",
                        "date": "2026-09-01 09:00",
                        "score": 8.0,
                        "companies": [],
                    },
                    {
                        "id": "zsxq-hot-1",
                        "title": "星大派每日热点（0901）",
                        "column": "星大派每日热点",
                        "date": "2026-09-01 08:00",
                        "companies": ["英伟达"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    from datetime import UTC, datetime

    from fin_analyse.read_capabilities.types import ProductionReadRequest

    result = MacroBrainQueryReader(tmp_path).read(
        ProductionReadRequest(
            question="宏观流动性和大类资产怎么看",
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )
    value = result.value
    assert value["status"] == "READY"
    assert [item["article_id"] for item in value["zsxq_macro"]] == [
        "zsxq-macro-1",
        "zsxq-hot-1",
    ]
    assert [card["item_id"] for card in value["shared_brain_cards"]] == ["c1"]
    assert value["search_needed"] is False


def test_reader_prefers_macro_index_over_heuristic(tmp_path: Path) -> None:
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    (kb_root / "index.json").write_text(
        json.dumps(
            {
                "articles": [
                    {
                        "id": "zsxq-hot-1",
                        "title": "星大派每日热点（0902）",
                        "column": "星大派每日热点",
                        "date": "2026-09-02 08:00",
                        "companies": [],
                    },
                    {
                        "id": "zsxq-rule-1",
                        "title": "当前市场流动性与大类资产复盘",
                        "column": "普通",
                        "date": "2026-09-01 09:00",
                        "score": 8.0,
                        "companies": [],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    rules = load_rules()
    rules = {**rules, "kept": [], "excluded": []}
    update_macro_index(kb_root, saved_ids=["zsxq-hot-1"], rules=rules)

    from datetime import UTC, datetime

    got = zsxq_macro_articles(
        kb_root,
        as_of=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        cap=3,
    )
    assert [item["article_id"] for item in got] == ["zsxq-hot-1"]

    fallback_kb = tmp_path / "no-index"
    fallback_kb.mkdir()
    (fallback_kb / "index.json").write_text(
        json.dumps(
            {
                "articles": [
                    {
                        "id": "zsxq-hot-1",
                        "title": "星大派每日热点（0902）",
                        "column": "星大派每日热点",
                        "date": "2026-09-02 08:00",
                        "companies": [],
                    },
                    {
                        "id": "zsxq-rule-1",
                        "title": "当前市场流动性与大类资产复盘",
                        "column": "普通",
                        "date": "2026-09-01 09:00",
                        "score": 8.0,
                        "companies": [],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    fallback = zsxq_macro_articles(
        fallback_kb,
        as_of=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        cap=3,
    )
    assert [item["article_id"] for item in fallback] == [
        "zsxq-hot-1",
        "zsxq-rule-1",
    ]


def test_reader_fallback_honors_kept_list(tmp_path: Path) -> None:
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    (kb_root / "index.json").write_text(
        json.dumps(
            {
                "articles": [
                    {
                        "id": "zsxq-55522445554815414",
                        "title": "还能撑一段时间，但“撑”的代价越来越高",
                        "column": "普通",
                        "date": "2026-08-02 10:00",
                        "score": None,
                        "companies": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    from datetime import UTC, datetime

    got = zsxq_macro_articles(
        kb_root,
        as_of=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
        cap=3,
    )
    assert [item["article_id"] for item in got] == ["zsxq-55522445554815414"]
