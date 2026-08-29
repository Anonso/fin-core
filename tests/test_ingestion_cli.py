import json
import subprocess
import sys


def test_zsxq_stats_cli_outputs_document_and_evidence_counts(tmp_path):
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

正文内容
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "fin_analyse.ingestion.cli",
            "zsxq-stats",
            "--root",
            str(tmp_path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload == {"source_id": "zsxq", "documents": 1, "evidence": 1}
