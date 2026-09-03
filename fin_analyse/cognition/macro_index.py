"""宏观参考条目侧车（macro_index.json）——ZSXQ 宏观条目增量落盘。

与 article_tags 同族：best-effort 侧车，写入失败绝不阻塞 ingest。
存储面：
- ``<kb_root>/runtime/cognition/macro_index.json``（目录 0700 / 文件 0600）；
- 单一 JSON：``{schema_version, rules_version, baseline_at, updated_at,
  entries[]}``，条目以 article_id 为键，整文件原子替换。

v0 语义（owner 2026-09-02 校准清单后落定）：
- 基线 = config 中 kept（12 条普通栏人工保留）+ 全 index 的每日热点列
  （7 条并入）；历史普通栏不按规则回溯——311 条已剔除结果不回灌。
- 增量 = 每次保存的新文章（saved_ids）按规则打标；每日热点与新普通栏
  宏观词命中都入。
- 规则版本变更只重评已在库条目，不回扫历史；排除单条裁决（excluded）
  优先于规则。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULES_CONFIG_PATH = PROJECT_ROOT / "config" / "macro_brain_rules.json"

MACRO_INDEX_RELATIVE_PATH = Path("runtime") / "cognition" / "macro_index.json"

SCHEMA_VERSION = "fin.macro-index/v1"
RULES_SCHEMA_VERSION = "fin.macro-rules/v1"

LOCK_RETRIES = 3
LOCK_RETRY_SECONDS = 0.05

_DAILY_SOURCE = "daily_hot"
_MANUAL_SOURCE = "manual_keep"
_RULE_SOURCE = "rule"


@dataclass
class MacroIndexReport:
    """一次 macro_index 更新的结果（幂等计数）。"""

    tagged: int = 0
    updated: int = 0
    already: int = 0
    removed: int = 0
    baseline_seeded: bool = False
    lock_busy: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        return self.lock_busy or bool(self.warnings)


def macro_index_path(knowledge_base_root: Path) -> Path:
    return Path(knowledge_base_root) / MACRO_INDEX_RELATIVE_PATH


def load_rules(path: Path | None = None) -> dict[str, Any]:
    """读取宏观规则/校准配置；缺失或 schema 不符直接抛错（写入方 fail-fast）。"""
    config_path = RULES_CONFIG_PATH if path is None else Path(path)
    raw = config_path.read_text(encoding="utf-8")
    rules = json.loads(raw)
    if not isinstance(rules, Mapping):
        raise ValueError("macro rules must be a JSON object")
    if rules.get("schema_version") != RULES_SCHEMA_VERSION:
        raise ValueError(f"unsupported macro rules schema: {rules.get('schema_version')!r}")
    if not isinstance(rules.get("rules_version"), int):
        raise ValueError("macro rules_version must be an integer")
    if not isinstance(rules.get("macro_terms"), list):
        raise ValueError("macro rules macro_terms must be a list")
    if not isinstance(rules.get("kept"), list):
        raise ValueError("macro rules kept must be a list")
    if not isinstance(rules.get("excluded", []), list):
        raise ValueError("macro rules excluded must be a list")
    return dict(rules)


def classify_macro_row(row: Mapping[str, Any], rules: Mapping[str, Any]) -> dict[str, Any] | None:
    """一条 index 行 → 宏观条目（无则 None）。低分不排除（评分只作维度）。"""
    article_id = str(row.get("id") or row.get("article_id") or "")
    if not article_id:
        return None
    if article_id in _kept_ids(rules):
        return {
            "source": _MANUAL_SOURCE,
            "reason": _kept_reason(rules, article_id),
            "matched_terms": [],
        }
    column = str(row.get("column", ""))
    if column in _column_set(rules, "daily_hot_columns"):
        return {
            "source": _DAILY_SOURCE,
            "reason": "每日热点并入",
            "matched_terms": ["ai_summary_reference"],
        }
    if column not in _column_set(rules, "scannable_columns"):
        return None
    title = str(row.get("title", ""))
    if any(term in title for term in rules.get("report_title_terms", [])):
        return None
    companies = row.get("companies")
    if isinstance(companies, list) and len(companies) >= int(
        rules.get("company_exclude_min", 3)
    ):
        return None
    pattern = str(rules.get("rate_term_pattern", ""))
    if pattern:
        cleaned = re.sub(pattern, "", title)
    else:
        cleaned = title
    matched = tuple(
        str(term) for term in rules.get("macro_terms", []) if term in cleaned or term in title
    )
    if not matched:
        return None
    return {
        "source": _RULE_SOURCE,
        "reason": None,
        "matched_terms": list(matched),
    }


def read_macro_index(knowledge_base_root: Path) -> dict[str, Any] | None:
    """读整份 macro_index；缺失/损坏返回 None（reader 走旧启发式兜底）。"""
    path = macro_index_path(knowledge_base_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping) or payload.get("schema_version") != SCHEMA_VERSION:
        return None
    if not isinstance(payload.get("entries"), list):
        return None
    return dict(payload)


def load_macro_entries(knowledge_base_root: Path) -> list[dict[str, Any]] | None:
    payload = read_macro_index(knowledge_base_root)
    if payload is None:
        return None
    return [entry for entry in payload["entries"] if isinstance(entry, Mapping)]


def update_macro_index(
    knowledge_base_root: Path,
    *,
    saved_ids: Iterable[str] | None = None,
    rules: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> MacroIndexReport:
    """幂等增量合并：基线 + saved_ids 打标 + 规则版本/排除清理。"""
    rules = load_rules() if rules is None else dict(rules)
    version = int(rules["rules_version"])
    excluded = {str(item) for item in rules.get("excluded", [])}
    index_rows = _index_rows_by_id(Path(knowledge_base_root))
    requested = {str(item) for item in (saved_ids or ())}
    stamp = (now if now is not None else datetime.now(UTC)).isoformat(timespec="seconds")
    return _write_locked(
        macro_index_path(knowledge_base_root),
        index_rows=index_rows,
        rules=rules,
        version=version,
        excluded=excluded,
        requested_ids=requested,
        stamp=stamp,
    )


def _write_locked(
    path: Path,
    *,
    index_rows: Mapping[str, Mapping[str, Any]],
    rules: Mapping[str, Any],
    version: int,
    excluded: set[str],
    requested_ids: set[str],
    stamp: str,
) -> MacroIndexReport:
    report = MacroIndexReport()
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(path.parent, 0o700)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    dir_fd = os.open(path.parent, dir_flags)
    try:
        locked = False
        for attempt in range(LOCK_RETRIES):
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if attempt + 1 >= LOCK_RETRIES:
                    report.lock_busy = True
                    report.warnings.append("macro_index lock busy")
                    return report
                time.sleep(LOCK_RETRY_SECONDS)
        if not locked:
            report.lock_busy = True
            report.warnings.append("macro_index lock busy")
            return report
        current = _read_current(path)
        changed = False
        entries = list(current.get("entries", []))
        by_id = {str(entry.get("article_id", "")): entry for entry in entries}

        if not current:
            entries = []
            by_id = {}
            changed = True

        # 规则版本/排除清理：只在库条目上重评，不回扫历史。
        for article_id in list(by_id):
            entry = by_id[article_id]
            if article_id in excluded:
                del by_id[article_id]
                report.removed += 1
                changed = True
                continue
            if entry.get("source") == _MANUAL_SOURCE:
                if article_id not in _kept_ids(rules):
                    del by_id[article_id]
                    report.removed += 1
                    changed = True
                elif entry.get("rules_version") != version:
                    entry["rules_version"] = version
                    entry["tagged_at"] = stamp
                    changed = True
                continue
            if entry.get("rules_version") == version:
                continue
            snapshot = {
                "id": article_id,
                "title": str(entry.get("title", "")),
                "column": str(entry.get("column", "")),
                "companies": list(range(int(entry.get("company_count", 0)))),
            }
            classified = classify_macro_row(snapshot, rules)
            if classified is None:
                del by_id[article_id]
                report.removed += 1
            else:
                entry["source"] = classified["source"]
                entry["reason"] = classified["reason"]
                entry["matched_terms"] = classified["matched_terms"]
                entry["rules_version"] = version
                entry["tagged_at"] = stamp
                report.updated += 1
            changed = True

        # 基线：整份文件缺失时，只入 kept + 每日热点，不回溯普通栏规则。
        if not current or current.get("baseline_at") is None:
            for article_id, row in index_rows.items():
                if article_id in excluded:
                    continue
                classified = classify_macro_row(row, rules)
                if classified is None or classified["source"] not in (
                    _MANUAL_SOURCE,
                    _DAILY_SOURCE,
                ):
                    continue
                existing = by_id.get(article_id)
                if existing is None:
                    by_id[article_id] = _entry_from_row(row, classified, version, stamp)
                    report.tagged += 1
                    changed = True
            current["baseline_at"] = stamp
            report.baseline_seeded = True
            changed = True

        # 增量：本次新保存的文章按规则打标。
        for article_id in sorted(requested_ids):
            row = index_rows.get(article_id)
            if row is None:
                report.warnings.append(f"macro_index row missing: {article_id}")
                continue
            if article_id in excluded:
                if article_id in by_id:
                    del by_id[article_id]
                    report.removed += 1
                    changed = True
                continue
            classified = classify_macro_row(row, rules)
            existing = by_id.get(article_id)
            if classified is None:
                if existing is not None and existing.get("source") != _MANUAL_SOURCE:
                    del by_id[article_id]
                    report.removed += 1
                    changed = True
                continue
            if existing is None:
                by_id[article_id] = _entry_from_row(row, classified, version, stamp)
                report.tagged += 1
                changed = True
            elif (
                existing.get("source") != classified["source"]
                or existing.get("matched_terms") != classified["matched_terms"]
                or existing.get("rules_version") != version
            ):
                for key, value in _entry_from_row(row, classified, version, stamp).items():
                    existing[key] = value
                report.updated += 1
                changed = True
            else:
                report.already += 1

        if changed:
            _write_current(
                path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "rules_version": version,
                    "baseline_at": current.get("baseline_at") or stamp,
                    "updated_at": stamp,
                    "entries": sorted(by_id.values(), key=lambda item: item["article_id"]),
                },
            )
        return report
    finally:
        os.close(dir_fd)


def _read_current(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, Mapping) or not isinstance(payload.get("entries"), list):
        return {}
    return dict(payload)


def _write_current(path: Path, payload: Mapping[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            temp_path.unlink()
        raise
    os.replace(temp_path, path)
    with suppress(OSError):
        os.chmod(path, 0o600)
    dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _index_rows_by_id(knowledge_base_root: Path) -> dict[str, Mapping[str, Any]]:
    index_path = Path(knowledge_base_root) / "index.json"
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = payload.get("articles") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        return {}
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if isinstance(row, Mapping):
            article_id = str(row.get("id") or row.get("article_id") or "")
            if article_id:
                by_id[article_id] = row
    return by_id


def _entry_from_row(
    row: Mapping[str, Any],
    classified: Mapping[str, Any],
    version: int,
    stamp: str,
) -> dict[str, Any]:
    companies = row.get("companies")
    company_count = len(companies) if isinstance(companies, list) else 0
    return {
        "article_id": str(row.get("id") or row.get("article_id") or ""),
        "title": str(row.get("title", ""))[:160],
        "column": str(row.get("column", "")),
        "date": str(row.get("date", ""))[:10],
        "score": row.get("score"),
        "company_count": company_count,
        "source": str(classified["source"]),
        "reason": classified.get("reason"),
        "matched_terms": list(classified.get("matched_terms", [])),
        "rules_version": version,
        "tagged_at": stamp,
    }


def _kept_ids(rules: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("article_id", ""))
        for item in rules.get("kept", [])
        if isinstance(item, Mapping) and item.get("article_id")
    }


def _kept_reason(rules: Mapping[str, Any], article_id: str) -> str | None:
    for item in rules.get("kept", []):
        if isinstance(item, Mapping) and str(item.get("article_id", "")) == article_id:
            reason = item.get("reason")
            return str(reason) if reason else None
    return None


def _column_set(rules: Mapping[str, Any], key: str) -> set[str]:
    values = rules.get(key, [])
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values}
