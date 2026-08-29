from pathlib import Path

from fin_analyse.ingestion.zsxq_adapter import ZsxqMarkdownAdapter


def write_article(root: Path):
    article_dir = root / "articles"
    article_dir.mkdir(parents=True)
    path = article_dir / "2026-06-18_0855_score8.8_abcd.md"
    path.write_text(
        """---
id: abcd
date: 2026-06-18 08:55
score: 8.8
column: 普通
companies: [华为, 海光信息]
tags: [半导体, AI芯片]
is_qa: False
---

# 国产算力信创认证

2026-06-18 08:55
华为海思、海光信息获信创认证。
#半导体 #AI芯片
""",
        encoding="utf-8",
    )
    return path


def test_fetch_reads_markdown_articles_as_raw_documents(tmp_path):
    write_article(tmp_path)
    adapter = ZsxqMarkdownAdapter(root=tmp_path)

    docs = adapter.fetch()

    assert len(docs) == 1
    assert docs[0].source_id == "zsxq"
    assert docs[0].external_id == "abcd"
    assert docs[0].title == "国产算力信创认证"
    assert docs[0].metadata["score"] == 8.8
    assert docs[0].metadata["companies"] == ["华为", "海光信息"]


def test_parse_returns_metadata_and_text_artifacts(tmp_path):
    write_article(tmp_path)
    adapter = ZsxqMarkdownAdapter(root=tmp_path)
    doc = adapter.fetch()[0]

    artifacts = adapter.parse(doc)

    assert {a.artifact_type for a in artifacts} == {"metadata", "text"}
    assert any(
        a.content == "2026-06-18 08:55\n华为海思、海光信息获信创认证。\n#半导体 #AI芯片"
        for a in artifacts
    )


def test_fetch_falls_back_to_content_company_matches_when_frontmatter_empty(tmp_path):
    article_dir = tmp_path / "articles"
    article_dir.mkdir(parents=True)
    (article_dir / "a.md").write_text(
        """---
id: a1
date: 2026-06-18 08:55
score: 8.8
column: 普通
companies: []
tags: [半导体]
is_qa: False
---

# 标题

华为海思、海光信息获信创认证。
""",
        encoding="utf-8",
    )
    adapter = ZsxqMarkdownAdapter(root=tmp_path)

    doc = adapter.fetch()[0]
    evidence = adapter.extract_evidence(doc)[0]

    assert set(doc.metadata["companies"]) >= {"华为", "海光信息"}
    assert set(evidence.metadata["companies"]) >= {"华为", "海光信息"}
