"""One-shot backfill: 普通栏研报评分表 → instrument_scores.jsonl.

范围（D-037，2026-09-03）：column=普通、能量评分 >= --min-score（默认 6.0）、
文章日期 >= --since（默认 2026-07-04，60 自然日窗口）。幂等可重跑：
record_id 唯一键 (source_id, code, 行序号)；内容 hash 变化才覆盖。

用法：
  python scripts/backfill_instrument_scores.py            # dry-run 统计
  python scripts/backfill_instrument_scores.py --write     # 真写

⚠ 重析复活语义（2026-09-07 实证）：--write 会按解析器口径重写范围内全部行，
  覆盖人工处置（confirm/drop）。重析后需按处置台账重放：
  $STATE/fin-analyse/adjudication-inbox-v1/bulk-disposition-20260907.jsonl
  （身份门政策化后，confirm 类大部分由解析器自动承继，仅 drop/C 档需重放。）
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

from fin_analyse.ingestion.instrument_scores import (
    instrument_scores_path,
    normalize_inline_codes,
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


def _load_a_share_entries(kb_root: Path) -> dict[str, dict[str, object]]:
    path = kb_root / "runtime" / "a_share_name_map.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kb-root",
        type=Path,
        default=None,
        help="knowledge-base root（默认运行时共享根）",
    )
    parser.add_argument("--since", default="2026-07-04", help="文章日期下限")
    parser.add_argument("--min-score", type=float, default=6.0, help="能量评分下限")
    parser.add_argument(
        "--write", action="store_true", help="真写（默认 dry-run 只统计）"
    )
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 篇（调试）")
    args = parser.parse_args(argv)

    kb_root = Path(args.kb_root) if args.kb_root else default_knowledge_base_root()
    a_share_entries = _load_a_share_entries(kb_root)
    since = date.fromisoformat(args.since)
    index_path = kb_root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    articles = index.get("articles") if isinstance(index, dict) else index

    # BUG-047 施工前置（09-05 夜裁决）：六仓停写，读面去 zsxq_sources，
    # 图片描述以文章 md「## 图片描述」节为准；published_at 取 index date。

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
        row_date = str(row.get("date", ""))
        article = {
            "source_id": source_id,
            "topic_id": str(row.get("topic_id", "") or ""),
            "column": str(row.get("column", "")),
            "title": str(row.get("title", "")),
            "article_date": row_date[:10],
            "published_at": row_date if ":" in row_date else None,
            "article_score": _as_float(row.get("score")),
        }
        md_path = Path(str(row.get("path", "")))
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except OSError:
            stats["md_read_error"] += 1
            continue
        md_text, _ = normalize_inline_codes(md_text, a_share_entries)
        records = parse_article_records(
            article=article,
            md_text=md_text,
            source_record=None,
            name_map=a_share_entries,
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
    # v3 语义（owner 09-05）：正文显式代码优先，名册 mismatch 是合法行，
    # 不再按 _is_code_name_mismatch 清除（那是 v2 误码时代的补救，已删）。
    added, updated = upsert_records(target, all_records)
    print(f"written: {target} added={added} updated={updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
