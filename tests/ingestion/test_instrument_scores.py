"""Instrument score parser/store unit tests (synthetic fixtures only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin_analyse.ingestion.instrument_scores import (
    InstrumentScoreRecord,
    build_record,
    normalize_score,
    parse_article_records,
    parse_rows_from_text,
    upsert_records,
)


def test_normalize_score_rules() -> None:
    assert normalize_score("85") == 8.5
    assert normalize_score("9.5") == 9.5
    assert normalize_score("8.5分") == 8.5
    assert normalize_score("95%") == 9.5
    assert normalize_score("") is None
    assert normalize_score("abc") is None
    assert normalize_score("0") is None
    assert normalize_score("150") is None  # 15.0 > 10 → invalid


TABLE_MD = """| 公司名称（代码） | 核心业务 | 所属板块 | 利好度 | 共识度 | 预计多久启动 | 持有时间 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 紫金矿业（601899.SH） | 金属矿业 | 有色 | 8.2 | 95 | 已发 | 1-3个月 |
| 中金黄金（600489.SH） | 黄金开采 | 黄金 | 8.8 | 92 | 已发 | 1-3个月 |
"""


def test_parse_markdown_table_with_aliases() -> None:
    drafts = parse_rows_from_text(TABLE_MD)
    assert len(drafts) == 2
    first = drafts[0]
    assert first["code"] == "601899"
    assert first["name"] == "紫金矿业"
    assert first["lihao"] == 8.2
    assert first["consensus"] == 9.5
    assert first["core_business"] == "金属矿业"
    assert first["horizon"] == "1-3个月"


def test_parse_table_with_separate_code_column_reversed_order() -> None:
    text = """| 公司代码 | 公司名称 | 核心业务 | 所属板块 | 利好度 | 共识度 |
|---|---:|---|---:|---:|---:|
| 603993 | 洛阳钼业 | 铜钴钼 | 有色 | 9.0 | 85 |
| 601899 | 紫金矿业 | 铜金 | 有色 | 9.2 | 88 |
"""
    drafts = parse_rows_from_text(text)
    assert drafts[0]["code"] == "603993"
    assert drafts[0]["name"] == "洛阳钼业"
    assert drafts[0]["lihao"] == 9.0
    assert drafts[0]["consensus"] == 8.5


LIST_MD = """1. **思源电气 002428**
   * 核心业务：开关/变压器
   * 所属板块：电网设备
   * 利好度：9.5
   * 市场共识度：92
   * 预计介入时机：1-2周
   * 持有时间：3-6个月
"""


def test_parse_list_style() -> None:
    drafts = parse_rows_from_text(LIST_MD)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["code"] == "002428"
    assert draft["name"] == "思源电气"
    assert draft["lihao"] == 9.5
    assert draft["consensus"] == 9.2


def test_missing_consensus_marks_needs_review() -> None:
    article = {
        "source_id": "zsxq-article-1",
        "topic_id": "topic-1",
        "column": "普通",
        "title": "研报",
        "article_date": "2026-08-01",
        "published_at": None,
        "article_score": 7.5,
    }
    md = """## 图片描述
### 000.jpg (LLM · fake)
1. **示例公司 600000**
   * 核心业务：示例业务
   * 所属板块：示例板块
   * 利好度：8.0
"""
    records = parse_article_records(article=article, md_text=md, source_record=None)
    assert len(records) == 1
    assert records[0].status == "needs_review"
    assert records[0].review_reason == "missing_fields:consensus"


def test_same_code_two_rows_both_kept() -> None:
    text = """1. **甲公司 600111**
   * 核心业务：业务A
   * 所属板块：板块A
   * 利好度：8.0
   * 共识度：80
2. **甲公司 600111**
   * 核心业务：业务B
   * 所属板块：板块B
   * 利好度：9.0
   * 共识度：90
"""
    drafts = parse_rows_from_text(text)
    assert len(drafts) == 2


def test_cross_carrier_conflict_marks_needs_review() -> None:
    article = {
        "source_id": "zsxq-article-2",
        "topic_id": "topic-2",
        "column": "普通",
        "title": "研报",
        "article_date": "2026-08-02",
        "published_at": None,
        "article_score": 8.0,
    }
    source = {
        "image_descriptions": [
            "| 公司名称（代码） | 利好度 | 共识度 |\n|---:|---:|---:|\n| 乙公司（600222.SH） | 9.0 | 90 |"
        ],
        "image_ocr": ["乙公司（600222.SH）\n利好度：8.0\n共识度：90"],
    }
    records = parse_article_records(
        article=article, md_text=None, source_record=source
    )
    assert len(records) == 1
    assert records[0].status == "needs_review"
    assert records[0].review_reason == "cross_source_conflict"


def test_upsert_store_is_atomic_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "instrument_scores.jsonl"
    article = {
        "source_id": "zsxq-a",
        "topic_id": "t",
        "column": "普通",
        "title": "标题",
        "article_date": "2026-08-03",
        "published_at": None,
        "article_score": 7.0,
    }
    record = build_record(
        draft={
            "code": "600000",
            "name": "示例公司",
            "core_business": "业务",
            "sector": "板块",
            "lihao": 8.0,
            "consensus": 8.0,
            "launch_in": None,
            "horizon": None,
        },
        article=article,
        raw_origin="test",
        provenance=None,
        sequence=0,
    )
    added, updated = upsert_records(path, [record])
    assert (added, updated) == (1, 0)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_name(path.name + ".tmp").exists()
    added2, updated2 = upsert_records(path, [record])
    assert (added2, updated2) == (0, 0)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["record_id"] == record.record_id
