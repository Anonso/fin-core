from pathlib import Path

from fin_analyse.claims import RuleBasedClaimExtractor
from fin_analyse.ingestion.zsxq_adapter import ZsxqMarkdownAdapter
from fin_analyse.knowledge.store import KnowledgeStore


def write_article(root: Path, article_id="a1", companies="[华为]", tags="[半导体]", score="8.8"):
    article_dir = root / "articles"
    article_dir.mkdir(exist_ok=True)
    (article_dir / f"{article_id}.md").write_text(
        f"""---
id: {article_id}
date: 2026-06-18 08:55
score: {score}
column: 普通
companies: {companies}
tags: {tags}
is_qa: False
---

# 标题{article_id}

华为海思获信创认证。
""",
        encoding="utf-8",
    )


def test_knowledge_store_loads_documents_evidence_and_claims(tmp_path):
    write_article(tmp_path)
    store = KnowledgeStore.from_adapter(
        ZsxqMarkdownAdapter(root=tmp_path),
        RuleBasedClaimExtractor(),
    )

    assert len(store.documents) == 1
    assert len(store.evidence) == 1
    assert len(store.claims) == 3
    assert store.documents[0].document_id == "zsxq:a1"
