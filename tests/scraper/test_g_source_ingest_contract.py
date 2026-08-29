"""R1-6 source contract at the existing ZSXQ scrape → priority-event seam."""

from __future__ import annotations

import json

from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper, ScrapeResult


def test_save_article_persists_independent_source_axes(tmp_path) -> None:
    scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)

    scraper._save_article(
        {
            "date": "2026-07-26 09:30",
            "title": "凤仙郡小故事：产业演进",
            "content": "长期产业与政治经济框架。",
            "score": 8.0,
            "column": "凤仙郡小故事",
            "priority_label": "重中之重",
            "companies": [],
            "tags": [],
            "char_count": 12,
            "is_qa": False,
        }
    )

    [entry] = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))["articles"]
    assert entry["source_classification"] == "teacher_original"
    assert entry["source_family"] == "凤仙郡小故事"
    assert entry["content_type"] == "长期故事"
    assert entry["source_usage"] == "long_term_framework"
    assert entry["priority_label"] == "重中之重"
    assert "valid_until" not in entry


def test_priority_writer_rejects_ambiguous_label_and_records_typed_gap(tmp_path) -> None:
    scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
    scraper._index = {
        "ambiguous": {
            "id": "ambiguous",
            "title": "星大派：未细分",
            "column": "星大派",
            "score": 9.5,
            "path": str(tmp_path / "ambiguous.md"),
            "is_qa": False,
        },
        "exact": {
            "id": "exact",
            "title": "星大派特刊：系统框架",
            "column": "星大派特刊",
            "score": 9.5,
            "path": str(tmp_path / "exact.md"),
            "is_qa": False,
        },
    }
    result = ScrapeResult()

    created = scraper._write_priority_events_for_new_articles(result, ["ambiguous", "exact"])

    assert created == 1
    assert any(w == "g_source_type_ambiguous:ambiguous" for w in result.warnings)
    rows = [
        json.loads(line)
        for line in (tmp_path / "runtime" / "cognition" / "priority_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["article_id"] for row in rows] == ["exact"]
    metadata = rows[0]["metadata"]
    assert metadata["source_family"] == "星大派"
    assert metadata["content_type"] == "特刊"
    assert metadata["source_usage"] == "systematic_framework"
    assert metadata["priority_label"] is None
    assert "valid_until" not in metadata
    assert rows[0]["push_reason"] == "G source: 星大派/特刊"


def test_parser_keeps_heaviest_label_orthogonal_to_exact_source_type(tmp_path) -> None:
    scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)

    post = scraper._parse_post(
        "2026-07-26 09:30\n"
        "重中之重：星大派特刊\n"
        "这是系统性框架的正文，包含足够长的产业链与风险分析内容。" * 3
    )

    assert post is not None
    assert post["column"] == "星大派特刊"
    assert post["priority_label"] == "重中之重"
