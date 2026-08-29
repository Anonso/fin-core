"""CLI tests for priority article event generation."""

import json
from pathlib import Path

from click.testing import CliRunner

from fin_analyse.cognition.cli import main


def _write_article(path: Path, frontmatter: str, body: str) -> None:
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


def test_priority_events_cli_writes_deduped_outbox(tmp_path: Path):
    kb_root = tmp_path / "knowledge-base"
    articles = kb_root / "articles"
    runtime = kb_root / "runtime" / "cognition"
    articles.mkdir(parents=True)
    runtime.mkdir(parents=True)
    _write_article(
        articles / "star.md",
        "id: star1\ndate: 2026-06-27\ncolumn: 星大派锐评",
        "# 星大派锐评\n关键不在情绪，而在订单、价格、利润率和风险边界是否兑现。",
    )
    _write_article(
        articles / "report.md",
        "id: report1\ndate: 2026-06-27\ncolumn: 普通\nscore: 9.2",
        "# 9分研报\n券商研报给予买入评级，盈利预测和目标价均上调，正文足够长。",
    )

    runner = CliRunner()
    first = runner.invoke(
        main,
        [
            "priority-events",
            "--kb-root",
            str(kb_root),
            "--runtime-root",
            str(runtime),
            "--limit",
            "10",
        ],
    )
    second = runner.invoke(
        main,
        [
            "priority-events",
            "--kb-root",
            str(kb_root),
            "--runtime-root",
            str(runtime),
            "--limit",
            "10",
        ],
    )

    assert first.exit_code == 0
    first_payload = json.loads(first.output)
    second_payload = json.loads(second.output)
    assert first_payload["events_created"] == 2
    assert first_payload["duplicates"] == 0
    assert second_payload["events_created"] == 0
    assert second_payload["duplicates"] == 2
    assert Path(first_payload["outbox_path"]).exists()


def test_priority_events_cli_dry_run_writes_nothing(tmp_path: Path):
    kb_root = tmp_path / "knowledge-base"
    articles = kb_root / "articles"
    runtime = kb_root / "runtime" / "cognition"
    articles.mkdir(parents=True)
    runtime.mkdir(parents=True)
    _write_article(
        articles / "star.md",
        "id: star1\ndate: 2026-06-27\ncolumn: 星大派特刊",
        "# 星大派特刊\n关键变量、产业链逻辑、风险边界和验证节奏都需要观察，这是重要的一课。",
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "priority-events",
            "--kb-root",
            str(kb_root),
            "--runtime-root",
            str(runtime),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["events_created"] == 1
    assert Path(payload["outbox_path"]).exists() is False
