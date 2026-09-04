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


def test_reader_aggregates_zsxq_v2_no_cards(tmp_path: Path) -> None:
    """宏观纯化 v2：书卡腿拆出，search_needed/gaps 只依赖 zsxq。"""
    _cards(tmp_path)  # 卡库非空也不得回流宏观接口
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
    assert value["schema_version"] == "fin.macro-brain/v2"
    assert [item["article_id"] for item in value["zsxq_macro"]] == [
        "zsxq-macro-1",
        "zsxq-hot-1",
    ]
    assert "shared_brain_cards" not in value  # 删键升版：书卡归接口B
    assert value["search_needed"] is False
    assert result.data_gaps == ()


def test_reader_v2_empty_zsxq_signals_search_not_cards(tmp_path: Path) -> None:
    """空 zsxq + 宏观信号 → search_needed=True、gaps 只看 zsxq（与卡库无关）。"""
    from datetime import UTC, datetime

    from fin_analyse.read_capabilities.types import ProductionReadRequest

    result = MacroBrainQueryReader(tmp_path).read(
        ProductionReadRequest(
            question="当前宏观流动性和美联储政策怎么看",
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )
    value = result.value
    assert value["schema_version"] == "fin.macro-brain/v2"
    assert value["zsxq_macro"] == []
    assert value["search_needed"] is True
    assert value["suggested_queries"]
    assert result.data_gaps == ("macro_brain_no_local_match",)

    # 非宏观问题、本地无料：不引导搜索、仍然 gaps（不再因空卡库语义漂移）
    result2 = MacroBrainQueryReader(tmp_path).read(
        ProductionReadRequest(
            question="通富微电研报评分",
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )
    assert result2.value["search_needed"] is False
    assert result2.data_gaps == ("macro_brain_no_local_match",)


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


def test_match_activation_terms_primary_beats_fallback(tmp_path: Path) -> None:
    """主级：卡自声明激活词零 2-gram 重叠也命中，且排在 2-gram 兜底之前。"""
    cards = [
        {
            "item_id": "lens_card",
            "title": "第二层思维定位卡",
            "summary": "完全不相干的摘要文字",
            "scope": "shared_brain_framework",
            "is_g_source": False,
            "metadata": {"activation_terms": ["预期差"]},
            "updated_at": "2026-08-01T00:00:00+08:00",
        },
        {
            "item_id": "overlap_card",
            "title": "预期管理入口清单",
            "summary": "预期与兑现的核对清单",
            "scope": "methodology_memory",
            "is_g_source": False,
            "updated_at": "2026-08-02T00:00:00+08:00",
        },
    ]
    # 题面含激活词（lens_card 主级命中、自身零 2-gram 重叠），
    # 同时 overlap_card 走 2-gram 兜底命中（预期/期差）——主级排前。
    matched = match_shared_brain_cards(cards, "这只票的预期差怎么看")
    assert [card["item_id"] for card in matched] == ["lens_card", "overlap_card"]
    # 纯激活词题面：主级单独命中（兜底为零也进结果）
    only = match_shared_brain_cards(cards, "预期差")
    assert [card["item_id"] for card in only] == ["lens_card"]


def test_match_fallback_without_activation_terms(tmp_path: Path) -> None:
    """兜底：无激活词卡走既有 2-gram 路径，不命中不返回。"""
    _cards(tmp_path)
    cards = load_shared_brain_cards(tmp_path)
    assert [card["item_id"] for card in match_shared_brain_cards(cards, "宏观政策分析")] == ["c1"]
    # 「研报」2-gram 命中 c2（既有兜底行为保持）
    assert [card["item_id"] for card in match_shared_brain_cards(cards, "通富微电研报评分")] == ["c2"]
    assert match_shared_brain_cards(cards, "今天天气怎么样") == []


def test_match_activation_case_insensitive_substring(tmp_path: Path) -> None:
    """激活词大小写不敏感、子串命中。"""
    cards = [
        {
            "item_id": "cycle_card",
            "title": "周期定位卡",
            "summary": "与题面无字面重叠",
            "scope": "shared_brain_framework",
            "is_g_source": False,
            "metadata": {"activation_terms": ["Second Level"]},
        }
    ]
    assert [c["item_id"] for c in match_shared_brain_cards(cards, "讲讲 second level thinking")] == [
        "cycle_card"
    ]
    assert match_shared_brain_cards(cards, "完全无关的问题") == []
