"""Tests for ZSXQ ingestion adapter."""

from fin_analyse.ingestion.models import RawDocument
from fin_analyse.ingestion.zsxq_adapter import ZsxqMarkdownAdapter


class TestZsxqMarkdownAdapter:
    def test_adapter_initialization(self, tmp_path):
        adapter = ZsxqMarkdownAdapter(root=tmp_path)
        assert adapter.root == tmp_path

    def test_adapter_fetch_empty(self, tmp_path):
        adapter = ZsxqMarkdownAdapter(root=tmp_path)
        documents = adapter.fetch()
        assert isinstance(documents, list)
        assert len(documents) == 0

    def test_adapter_fetch_with_articles(self, tmp_path):
        # Create test article
        articles_dir = tmp_path / "articles"
        articles_dir.mkdir()
        (articles_dir / "test1.md").write_text("""---
id: test1
date: 2026-06-18 08:55
score: 8.8
column: 普通
companies: [华为]
tags: [半导体]
is_qa: False
---

# Test Article

This is a test article.
""")

        adapter = ZsxqMarkdownAdapter(root=tmp_path)
        documents = adapter.fetch()

        assert len(documents) == 1
        assert isinstance(documents[0], RawDocument)
        assert documents[0].document_id == "zsxq:test1"

    def test_adapter_source_info(self, tmp_path):
        adapter = ZsxqMarkdownAdapter(root=tmp_path)
        info = adapter.source_info
        assert info.source_id == "zsxq"
