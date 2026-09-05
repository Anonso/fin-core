"""CLI adapter for cognition backfill, verification, and sampling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from fin_analyse.cognition.article_tags import tags_cli
from fin_analyse.cognition.service import CognitiveService


@click.group()
def main() -> None:
    """fin-cognition — 认知层回填、验证、抽样。"""


main.add_command(tags_cli)


@main.command()
@click.option("--limit", "-n", default=20, type=int, help="最多处理 N 篇文章")
@click.option("--resume/--no-resume", default=True, help="是否跳过已存在的 evidence")
@click.option("--dry-run", is_flag=True, help="只扫描不写入")
@click.option("--all", "all_", is_flag=True, help="全量处理（忽略 limit）")
@click.option("--teacher", default="guo", help="老师 ID")
def backfill(limit: int, resume: bool, dry_run: bool, all_: bool, teacher: str) -> None:
    """从知识库 Markdown 回填 EvidenceItem 和 ReasoningTrace。"""
    from fin_analyse.cognition.backfill import CognitionBackfillRunner
    from fin_analyse.cognition.llm import CognitionLLM
    from fin_analyse.cognition.service import CognitiveService
    from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

    kb_root = default_knowledge_base_root()
    llm = CognitionLLM.from_config()
    svc = CognitiveService(runtime_root=kb_root / "runtime" / "cognition", llm_helper=llm)

    runner = CognitionBackfillRunner(
        kb_root=kb_root,
        service=svc,
        teacher_id=teacher,
        limit=None if all_ else limit,
        resume=resume,
        dry_run=dry_run,
    )
    report = runner.run()
    click.echo(f"scanned={report.scanned_count} evidence_saved={report.evidence_saved_count}")
    click.echo(
        f"teacher_original={report.teacher_original_count} "
        f"research_report={report.research_report_count}"
    )
    click.echo(f"ai_assisted={report.ai_assisted_count} unknown={report.unknown_count}")
    click.echo(
        f"persona_eligible={report.persona_eligible_count} "
        f"persona_rejected={report.persona_rejected_count} "
        f"persona_gate_unknown={report.persona_gate_unknown_count}"
    )
    click.echo(
        f"traces_created={report.traces_created_count} "
        f"skipped={report.skipped_count} errors={report.error_count}"
    )
    click.echo(f"llm_available={report.llm_available}")
    click.echo(f"llm_failed={report.llm_failed_count}")
    if report.llm_failed_ids:
        click.echo(f"llm_failed_ids: {', '.join(report.llm_failed_ids[:20])}")
    if report.errors_sample:
        click.echo(f"errors_sample: {report.errors_sample[:5]}")
    if report.sample_trace_ids:
        click.echo(f"sample_trace_ids: {report.sample_trace_ids[:10]}")


@main.command()
@click.option("--limit", "-n", default=20, type=int)
@click.option("--teacher", default="guo", help="老师 ID")
def sample_traces(limit: int, teacher: str) -> None:
    """抽样查看 ReasoningTrace。"""
    from fin_analyse.cognition.service import CognitiveService
    from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

    kb_root = default_knowledge_base_root()
    svc = CognitiveService(runtime_root=kb_root / "runtime" / "cognition")

    traces = [t for t in svc.trace_repo.list_all() if t.teacher_id == teacher][:limit]
    click.echo(json.dumps([t.to_dict() for t in traces], ensure_ascii=False, indent=2))


@main.command("priority-events")
@click.option("--kb-root", type=click.Path(file_okay=False), default=None)
@click.option("--runtime-root", type=click.Path(file_okay=False), default=None)
@click.option("--limit", "limit_", default=50, type=int, help="最多扫描 N 篇文章")
@click.option("--dry-run", is_flag=True, help="只计算事件，不写 outbox")
def priority_events(kb_root: str | None, runtime_root: str | None, limit_: int, dry_run: bool) -> None:
    """生成平台无关的优先级文章推送事件。"""
    from fin_analyse.cognition.priority_articles import (
        PRIORITY_OUTBOX_NAME,
        scan_articles_for_priority,
    )
    from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

    kb = default_knowledge_base_root() if kb_root is None else Path(kb_root)
    article_dir = kb / "articles"
    outbox_path = (
        kb / "runtime" / "cognition" / PRIORITY_OUTBOX_NAME
        if runtime_root is None
        else Path(runtime_root) / PRIORITY_OUTBOX_NAME
    )
    result = scan_articles_for_priority(
        article_dir,
        limit=limit_,
        dry_run=dry_run,
        outbox_path=outbox_path,
    )
    payload: dict[str, Any] = {
        "scanned": result["scanned"],
        "events_created": result["events_created"],
        "duplicates": result["duplicates"],
        "skipped": result["skipped"],
        "dry_run": dry_run,
        "outbox_path": str(outbox_path),
    }
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@main.command("verify-traces")
@click.option(
    "--threshold", default=0.5, type=float, help="验证 extraction_confidence <= threshold 的 trace"
)
@click.option("--limit", "limit_", "-n", default=3, type=int, help="最多验证 N 条 trace")
@click.option("--resume/--no-resume", default=True, help="是否跳过已有 verification 的 trace")
@click.option("--teacher", default="guo", help="老师 ID")
def verify_traces(threshold: float, limit_: int, resume: bool, teacher: str) -> None:
    """对低置信 ReasoningTrace 做 LLM 二次验证。"""
    from fin_analyse.cognition.llm import CognitionLLM
    from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

    kb_root = default_knowledge_base_root()
    llm = CognitionLLM.from_config(preferred=("glm53", "deepseek", "qwen", "claude"))
    svc = CognitiveService(runtime_root=kb_root / "runtime" / "cognition", llm_helper=llm)
    report = svc.verify_low_confidence_traces(
        threshold=threshold,
        limit=limit_,
        resume=resume,
        teacher_id=teacher,
    )
    click.echo(
        f"selected={report.selected_count} verified={report.verified_count} "
        f"keep={report.keep_count} revise={report.revise_count} "
        f"reject={report.reject_count} skipped={report.skipped_count} errors={report.error_count}"
    )
    if report.verification_ids:
        click.echo(f"verification_ids: {report.verification_ids[:10]}")
    if report.errors_sample:
        click.echo(f"errors_sample: {report.errors_sample[:5]}")


if __name__ == "__main__":
    main()
