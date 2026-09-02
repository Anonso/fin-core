"""read_article_search reader tests (temp index + articles)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fin_analyse.knowledge.article_search import ArticleKeywordSearchReader
from fin_analyse.read_capabilities.types import ProductionReadRequest


def _kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    articles = root / "articles"
    articles.mkdir(parents=True)
    (articles / "20260801_zsxq-1.md").write_text(
        "---\n"
        "id: zsxq-1\n"
        "date: 2026-08-01\n"
        "column: 普通\n"
        "score: 8.1\n"
        "---\n"
        "\n"
        "# 先进封装点评\n封装封测进入景气周期。\n",
        encoding="utf-8",
    )
    (articles / "20260815_zsxq-2.md").write_text(
        "---\n"
        "id: zsxq-2\n"
        "date: 2026-08-15\n"
        "column: 普通\n"
        "score: 8.0\n"
        "---\n"
        "\n"
        "# 黄金周报\n金价新高。\n",
        encoding="utf-8",
    )
    (root / "index.json").write_text(
        json.dumps(
            {
                "articles": [
                    {
                        "id": "zsxq-1",
                        "title": "先进封装点评",
                        "column": "普通",
                        "date": "2026-08-01",
                        "score": 8.1,
                        "path": str(articles / "20260801_zsxq-1.md"),
                    },
                    {
                        "id": "zsxq-2",
                        "title": "黄金周报",
                        "column": "普通",
                        "date": "2026-08-15",
                        "score": 8.0,
                        "path": str(articles / "20260815_zsxq-2.md"),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def _read(root: Path, question: str):
    return ArticleKeywordSearchReader(root).read(
        ProductionReadRequest(question=question, as_of=datetime(2026, 9, 2, tzinfo=UTC))
    )


def test_meta_and_body_search(tmp_path: Path) -> None:
    root = _kb(tmp_path)
    result = _read(root, "封装封测的行业点评文章")
    assert result.data_gaps == ()
    hits = result.value["hits"]
    assert hits[0]["article_id"] == "zsxq-1"
    assert "封装" in hits[0]["title"]

    result = _read(root, "金价新高")
    assert [hit["article_id"] for hit in result.value["hits"]] == ["zsxq-2"]
    assert result.value["hits"][0]["excerpt"]


def test_no_match_and_missing_index(tmp_path: Path) -> None:
    root = _kb(tmp_path)
    result = _read(root, "不存在的主题词")
    assert "article_search_no_match" in result.data_gaps

    empty = tmp_path / "empty"
    empty.mkdir()
    result = _read(empty, "封装")
    assert "article_search_index_unavailable" in result.data_gaps
