import json
import subprocess
import sys


def test_zsxq_claims_cli_outputs_claim_counts(tmp_path):
    article_dir = tmp_path / "articles"
    article_dir.mkdir()
    (article_dir / "a.md").write_text(
        """---
id: a1
date: 2026-06-18 08:55
score: 8.8
column: 普通
companies: [华为]
tags: [半导体]
is_qa: False
---

# 标题

华为海思获信创认证。
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "fin_analyse.ingestion.cli", "zsxq-claims", "--root", str(tmp_path)],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["source_id"] == "zsxq"
    assert payload["documents"] == 1
    assert payload["evidence"] == 1
    assert payload["claims"] == 3
    assert payload["by_type"] == {
        "article_score": 1,
        "company_mention": 1,
        "topic_tag": 1,
    }
