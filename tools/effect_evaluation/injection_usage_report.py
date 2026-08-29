"""Injection-usage reconciliation for real consultations (read-only).

Reconciles what was injected (``g-injection-audit.jsonl`` selected entries)
against what the Agent actually referenced (product provenance capability
attestations / claims) and prints a content-free usage report per bucket and
ref.  This is the "深入理解" evidence basis: never the question, holdings,
prompt or answer text.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _load_journal_entries(journal: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(json.loads(line))
    return entries


def _referenced_refs(product: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key in (
        "shared_brain_references",
        "g_source_refs",
        "market_overview_references",
        "market_snapshot_references",
    ):
        raw = product.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict):
                ref = item.get("source_ref")
                if isinstance(ref, str) and ref:
                    refs.add(ref)
    return refs


def _referenced_refs_from_sqlite(db_path: Path) -> tuple[set[str], int]:
    """Read persisted consultation products (products.product_json)."""

    refs: set[str] = set()
    product_count = 0
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT product_json FROM products "
            "WHERE product_json LIKE '%shared_brain_references%'"
        )
        for (raw,) in cursor.fetchall():
            try:
                product = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(product, dict):
                continue
            product_count += 1
            refs |= _referenced_refs(product)
    return refs, product_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument(
        "--products",
        type=Path,
        help="directory of consultation product JSON files (optional)",
    )
    parser.add_argument(
        "--sqlite",
        type=Path,
        help="semantic state sqlite (read-only) with products table (optional)",
    )
    args = parser.parse_args(argv)

    entries = _load_journal_entries(args.journal)
    injected: Counter[str] = Counter()
    by_bucket: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        selected = entry.get("selected")
        if not isinstance(selected, list):
            continue
        for item in selected:
            ref = item.get("source_ref")
            bucket = item.get("source_bucket")
            if isinstance(ref, str) and ref:
                injected[ref] += 1
                if isinstance(bucket, str):
                    by_bucket[bucket].append(ref)

    referenced: set[str] = set()
    product_count = 0
    if args.sqlite is not None and args.sqlite.exists():
        referenced, product_count = _referenced_refs_from_sqlite(args.sqlite)
    elif args.products is not None and args.products.is_dir():
        for path in sorted(args.products.glob("*.json")):
            try:
                product = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(product, dict):
                continue
            product_count += 1
            referenced |= _referenced_refs(product)

    report: dict[str, Any] = {
        "schema_version": "fin.injection-usage-report/v1",
        "journal_entries": len(entries),
        "products_scanned": product_count,
        "injected_ref_count": len(injected),
        "referenced_ref_count": len(referenced),
        "usage_rate": round(len(referenced & set(injected)) / len(injected), 3)
        if injected
        else None,
        "per_bucket": {
            bucket: {
                "injected_refs": len(refs),
                "referenced_refs": len(referenced & set(refs)),
                "unreferenced_refs": sorted(set(refs) - referenced),
            }
            for bucket, refs in sorted(by_bucket.items())
        },
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
