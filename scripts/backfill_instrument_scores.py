"""One-shot backfill: 普通栏研报评分表 → instrument_scores.jsonl.

范围（owner 2026-09-02）：column=普通、能量评分 >= --min-score（默认 7.0）、
文章日期 >= --since（默认 2026-07-04，60 自然日窗口）。幂等可重跑：
record_id 唯一键 (source_id, code, 行序号)；内容 hash 变化才覆盖。

用法：
  python scripts/backfill_instrument_scores.py            # dry-run 统计
  python scripts/backfill_instrument_scores.py --write     # 真写
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

from fin_analyse.ingestion.instrument_scores import (
    instrument_scores_path,
    parse_article_records,
    upsert_records,
)
from fin_analyse.runtime.knowledge_root import default_knowledge_base_root


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kb-root",
        type=Path,
        default=None,
        help="knowledge-base root（默认运行时共享根）",
    )
    parser.add_argument("--since", default="2026-07-04", help="文章日期下限")
    parser.add_argument("--min-score", type=float, default=7.0, help="能量评分下限")
    parser.add_argument(
        "--write", action="store_true", help="真写（默认 dry-run 只统计）"
    )
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 篇（调试）")
    args = parser.parse_args(argv)

    kb_root = Path(args.kb_root) if args.kb_root else default_knowledge_base_root()
    since = date.fromisoformat(args.since)
    index_path = kb_root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    articles = index.get("articles") if isinstance(index, dict) else index

    source_by_id: dict[str, dict[str, object]] = {}
    sources_path = kb_root / "runtime" / "cognition" / "zsxq_sources.jsonl"
    if sources_path.exists():
        for line in sources_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            source_by_id[str(record.get("article_id", ""))] = record

    candidates = []
    for row in articles:
        if not isinstance(row, dict) or row.get("column") != "普通":
            continue
        row_date = _parse_date(str(row.get("date", "")))
        score = _as_float(row.get("score"))
        if row_date is None or row_date < since or score is None or score < args.min_score:
            continue
        candidates.append(row)
    candidates.sort(key=lambda row: str(row.get("date", "")))
    if args.limit is not None:
        candidates = candidates[: args.limit]

    stats: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    all_records = []
    for row in candidates:
        source_id = str(row.get("id", ""))
        source_record = source_by_id.get(source_id)
        article = {
            "source_id": source_id,
            "topic_id": str(row.get("topic_id", "") or ""),
            "column": str(row.get("column", "")),
            "title": str(row.get("title", "")),
            "article_date": str(row.get("date", ""))[:10],
            "published_at": (
                str(source_record.get("published_at")) if source_record else None
            ),
            "article_score": _as_float(row.get("score")),
        }
        md_path = Path(str(row.get("path", "")))
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except OSError:
            stats["md_read_error"] += 1
            continue
        records = parse_article_records(
            article=article, md_text=md_text, source_record=source_record
        )
        if records:
            stats["articles_with_tables"] += 1
        all_records.extend(records)
        for record in records:
            stats["records_total"] += 1
            stats[f"status:{record.status}"] += 1
            if record.review_reason:
                reason_counter[record.review_reason] += 1

    print(f"candidates={len(candidates)} since={since} min_score={args.min_score}")
    print("stats:", dict(stats))
    if reason_counter:
        print("needs_review reasons:", dict(reason_counter.most_common()))

    if not args.write:
        print("dry-run: 未写盘；加 --write 真写")
        return 0
    target = instrument_scores_path(kb_root)
    added, updated = upsert_records(target, all_records)
    print(f"written: {target} added={added} updated={updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
