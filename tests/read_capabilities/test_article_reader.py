"""read_article reader tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fin_analyse.knowledge.article_reader import ArticleContentReader
from fin_analyse.read_capabilities.types import ProductionReadRequest


def _kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    articles = root / "articles"
    articles.mkdir(parents=True)
    article = articles / "20260813_zsxq-3.md"
    article.write_text(
        "---\n"
        "id: zsxq-3\n"
        "date: 2026-08-13 09:00\n"
        "column: 星大派特刊\n"
        "score: 8.7\n"
        "---\n"
        "\n"
        "# 先进封装特刊\n" + ("正文内容。" * 6000),
        encoding="utf-8",
    )
    (root / "index.json").write_text(
        json.dumps(
            {
                "articles": [
                    {
                        "id": "zsxq-3",
                        "title": "先进封装特刊",
                        "column": "星大派特刊",
                        "date": "2026-08-13 09:00",
                        "score": 8.7,
                        "path": str(article),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def _read(root: Path, article_id: str | None):
    return ArticleContentReader(root).read(
        ProductionReadRequest(
            question="读文章",
            article_id=article_id,
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )


def test_read_article_returns_bounded_body_and_meta(tmp_path: Path) -> None:
    root = _kb(tmp_path)
    result = _read(root, "zsxq-3")
    assert result.data_gaps == ()
    value = result.value
    assert value["article_id"] == "zsxq-3"
    assert value["column"] == "星大派特刊"
    assert value["layer"] == "g"
    assert value["truncated"] is True
    assert len(value["content"]) <= 20_100


def test_read_article_missing_id_and_not_found(tmp_path: Path) -> None:
    root = _kb(tmp_path)
    assert "article_id_required" in _read(root, None).data_gaps
    assert "article_not_found" in _read(root, "no-such").data_gaps
