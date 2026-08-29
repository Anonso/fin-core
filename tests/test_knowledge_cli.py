import json
import subprocess
import sys


def test_claims_cli_filters_by_company(tmp_path):
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
        [
            sys.executable,
            "-m",
            "fin_analyse.knowledge.cli",
            "claims",
            "--root",
            str(tmp_path),
            "--company",
            "华为",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["claims"][0]["subject"] == "华为"
