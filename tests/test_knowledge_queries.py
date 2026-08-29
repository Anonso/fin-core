from pathlib import Path

from fin_analyse.claims import RuleBasedClaimExtractor
from fin_analyse.ingestion.zsxq_adapter import ZsxqMarkdownAdapter
from fin_analyse.knowledge.store import KnowledgeStore


def write_article(root: Path, article_id, companies, tags, score):
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

正文内容
""",
        encoding="utf-8",
    )


def make_store(tmp_path):
    write_article(tmp_path, "a1", "[华为]", "[半导体]", "8.8")
    write_article(tmp_path, "a2", "[宁德时代]", "[新能源]", "8.2")
    return KnowledgeStore.from_adapter(ZsxqMarkdownAdapter(tmp_path), RuleBasedClaimExtractor())


def test_find_claims_by_company(tmp_path):
    store = make_store(tmp_path)

    claims = store.find_claims(company="华为")

    assert {claim.subject for claim in claims} == {"华为"}
    assert all(claim.claim_type == "company_mention" for claim in claims)


def test_find_claims_by_topic(tmp_path):
    store = make_store(tmp_path)

    claims = store.find_claims(topic="新能源")

    assert {claim.subject for claim in claims} == {"新能源"}
    assert all(claim.claim_type == "topic_tag" for claim in claims)


def test_find_claims_by_min_score(tmp_path):
    store = make_store(tmp_path)

    claims = store.find_claims(claim_type="article_score", min_score=8.5)

    assert len(claims) == 1
    assert claims[0].document_id == "zsxq:a1"
