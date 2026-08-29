from pathlib import Path

from fin_analyse.claims import RuleBasedClaimExtractor
from fin_analyse.ingestion.zsxq_adapter import ZsxqMarkdownAdapter
from fin_analyse.knowledge.search import TextSearch
from fin_analyse.knowledge.store import KnowledgeStore


def write_article(root: Path, article_id, title, content, companies="[华为]"):
    article_dir = root / "articles"
    article_dir.mkdir(exist_ok=True)
    (article_dir / f"{article_id}.md").write_text(
        f"""---
id: {article_id}
date: 2026-06-18 08:55
score: 8.8
column: 普通
companies: {companies}
tags: [半导体]
is_qa: False
---

# {title}

{content}
""",
        encoding="utf-8",
    )


def test_search_finds_relevant_documents(tmp_path):
    write_article(
        tmp_path, "a1", "半导体突破", "华为海思在AI芯片领域取得重大突破，国产算力迎来新机遇"
    )
    write_article(tmp_path, "a2", "物流快递", "快递行业反内卷，价格修复带动盈利改善")
    store = KnowledgeStore.from_adapter(ZsxqMarkdownAdapter(tmp_path), RuleBasedClaimExtractor())
    ts = TextSearch(store)

    results = ts.search("AI芯片")

    assert len(results) > 0
    assert results[0]["document_id"] == "zsxq:a1"


def test_search_returns_empty_for_no_match(tmp_path):
    write_article(tmp_path, "a1", "标题", "内容")
    store = KnowledgeStore.from_adapter(ZsxqMarkdownAdapter(tmp_path), RuleBasedClaimExtractor())
    ts = TextSearch(store)

    results = ts.search("完全不相关关键词")
    assert len(results) == 0
