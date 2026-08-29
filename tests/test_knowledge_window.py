from pathlib import Path

from fin_analyse.claims import RuleBasedClaimExtractor
from fin_analyse.ingestion.zsxq_adapter import ZsxqMarkdownAdapter
from fin_analyse.knowledge.store import KnowledgeStore


def write_article(root: Path, article_id, date):
    article_dir = root / "articles"
    article_dir.mkdir(exist_ok=True)
    (article_dir / f"{article_id}.md").write_text(
        f"""---
id: {article_id}
date: {date}
score: 8.8
column: 普通
companies: [华为]
tags: [半导体]
is_qa: False
---

# 标题{article_id}

正文内容
""",
        encoding="utf-8",
    )


def test_find_claims_filters_by_since_until(tmp_path):
    write_article(tmp_path, "old", "2026-01-01 08:55")
    write_article(tmp_path, "new", "2026-06-18 08:55")
    store = KnowledgeStore.from_adapter(ZsxqMarkdownAdapter(tmp_path), RuleBasedClaimExtractor())

    claims = store.find_claims(company="华为", since="2026-06-01", until="2026-06-30")

    assert {claim.document_id for claim in claims} == {"zsxq:new"}
