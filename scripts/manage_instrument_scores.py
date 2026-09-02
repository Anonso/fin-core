"""needs_review 人工闭环：list / confirm / drop 评分记录。

用法：
  python scripts/manage_instrument_scores.py list [--limit 30]
  python scripts/manage_instrument_scores.py confirm <record_id>
  python scripts/manage_instrument_scores.py drop <record_id>
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from fin_analyse.ingestion.instrument_scores import (
    instrument_scores_path,
    load_records,
)
from fin_analyse.runtime.knowledge_root import default_knowledge_base_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-root", type=Path, default=None)
    sub = parser.add_subparsers(dest="action", required=True)
    list_p = sub.add_parser("list")
    list_p.add_argument("--limit", type=int, default=30)
    list_p.add_argument("--all", action="store_true", help="ok + needs_review 都列")
    sub.add_parser("confirm")
    sub.add_parser("drop")
    parser.add_argument("record_id", nargs="?")
    args = parser.parse_args(argv)

    root = Path(args.kb_root) if args.kb_root else default_knowledge_base_root()
    path = instrument_scores_path(root)
    records = load_records(path)
    if args.action == "list":
        rows = list(records.values())
        if not args.all:
            rows = [row for row in rows if row.get("status") == "needs_review"]
        rows.sort(key=lambda row: str(row.get("article_date", "")), reverse=True)
        print(f"total={len(records)} showing={len(rows[: args.limit])}")
        for row in rows[: args.limit]:
            print(
                row.get("record_id", "")[:16],
                row.get("code"),
                row.get("name"),
                row.get("article_date"),
                row.get("review_reason"),
            )
        return 0
    if not args.record_id:
        parser.error("record_id required")
    record = records.get(args.record_id)
    if record is None:
        print(f"not found: {args.record_id}")
        return 2
    if args.action == "confirm":
        record["status"] = "ok"
        record["review_reason"] = None
    elif args.action == "drop":
        records.pop(args.record_id, None)
    from fin_analyse.ingestion.instrument_scores import upsert_records

    class _Record:
        def __init__(self, value: dict) -> None:
            self.record_id = value["record_id"]
            self._value = value

        def to_dict(self) -> dict:
            return self._value

    if args.action == "confirm":
        upsert_records(path, [_Record(record)])
    else:
        body = "\n".join(
            json.dumps(value, ensure_ascii=False, default=str)
            for value in records.values()
        )
        if body:
            body += "\n"
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    print(f"{args.action}: {args.record_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
