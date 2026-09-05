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


def test_date_enumeration_covers_keyword_misses(tmp_path: Path) -> None:
    """BUG-029：枚举模式按日期全量返回——关键词零交叠的文章也列得出。"""

    root = _kb(tmp_path)
    reader = ArticleKeywordSearchReader(root)
    # 关键词模式：问题与「黄金周报」内容零交叠 → 召回缺席。
    keyword_result = reader.read(
        ProductionReadRequest(
            question="液冷 CDU 算力网 普通栏 最新",
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )
    keyword_ids = {h["article_id"] for h in keyword_result.value["hits"]}
    assert "zsxq-2" not in keyword_ids

    # 枚举模式：date_from=date_to=当日 → 当日全部条目、时间升序、mode 标注。
    result = reader.read(
        ProductionReadRequest(
            question="8月15日发布的文章讲了什么",
            date_from="2026-08-15",
            date_to="2026-08-15",
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )
    assert result.data_gaps == ()
    assert result.value["mode"] == "date_enumeration"
    assert [h["article_id"] for h in result.value["hits"]] == ["zsxq-2"]


def test_date_enumeration_range_and_ordering(tmp_path: Path) -> None:
    root = _kb(tmp_path)
    result = ArticleKeywordSearchReader(root).read(
        ProductionReadRequest(
            question="近半月消息序列",
            date_from="2026-08-01",
            date_to="2026-08-31",
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )
    assert [h["article_id"] for h in result.value["hits"]] == ["zsxq-1", "zsxq-2"]


def test_date_enumeration_empty_range_is_typed_gap(tmp_path: Path) -> None:
    root = _kb(tmp_path)
    result = ArticleKeywordSearchReader(root).read(
        ProductionReadRequest(
            question="不存在的日期",
            date_from="2027-01-01",
            date_to="2027-01-31",
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )
    assert result.value["hits"] == []
    assert result.data_gaps == ("article_search_date_range_empty",)


def test_date_enumeration_rejects_malformed_bounds(tmp_path: Path) -> None:
    root = _kb(tmp_path)
    result = ArticleKeywordSearchReader(root).read(
        ProductionReadRequest(
            question="坏日期",
            date_from="2026/08/01",
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )
    assert result.data_gaps == ("article_search_date_bounds_invalid",)


def test_index_warmup_returns_typed_gap_instead_of_freezing(
    monkeypatch: object, tmp_path: Path
) -> None:
    """BUG-037：索引构建在后台线程预热，首查只做有界等待。

    构建未完成时返回 article_search_index_warming typed gap（而非同步建索引
    冻住整个 MCP 事件循环）；构建完成后同一 reader 正常出结果。
    """
    import threading
    import time as time_mod

    from fin_analyse.knowledge import article_search as mod

    root = _kb(tmp_path)
    release = threading.Event()
    entered = threading.Event()

    class _BlockingAdapter:
        def __init__(self, kb_root: Path) -> None:
            self.kb_root = kb_root

        def fetch(self) -> list:
            entered.set()
            release.wait(5.0)
            return []

    monkeypatch.setattr(mod, "ZsxqMarkdownAdapter", _BlockingAdapter)  # type: ignore[arg-type]
    reader = ArticleKeywordSearchReader(root)

    started = time_mod.monotonic()
    result = reader.read(
        ProductionReadRequest(
            question="先进封装",
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
            deadline_at=datetime.fromtimestamp(time_mod.time() + 0.2, tz=UTC),
        )
    )
    elapsed = time_mod.monotonic() - started

    # 构建仍阻塞在 fetch 里，读已经返回 typed gap——没有冻住调用线程。
    assert entered.is_set() and not release.is_set()
    assert "article_search_index_warming" in result.data_gaps
    assert elapsed < 5.0

    release.set()
    result2 = reader.read(
        ProductionReadRequest(
            question="先进封装",
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
        )
    )
    assert "article_search_index_warming" not in result2.data_gaps
