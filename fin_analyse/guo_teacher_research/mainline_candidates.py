"""Nominate G mainline candidates from ingested ZSXQ material (D-038 v1).

设计门 g-mainline-growth-v1 部件1.  Pure read (index / annotation / read-model);
the only write is the candidate draft markdown under ``$XDG_STATE_HOME``
(0600, deterministic rewrite).  Never writes the knowledge base and never
blocks ingest — the consume wrapper owns the typed audit line and exception
isolation.

Selection: index entries whose ``date`` is later than the annotation ``as_of``
(index carries ``date``, not ``published_at`` — gate fact check 2026-09-03),
classified through :func:`classify_g_source` with the same provenance
parameters the G Working Set uses (``teacher_original=True``,
``is_qa=entry.get("is_qa") is True``).  ``usage=ai_summary_reference``
(每日热点) is excluded from mainline nominations.  Dedup: the index path is
normalized to a canonical article_ref and matched exactly against the
read-model ``sources[].article_ref``; articles already in the read-model are
listed separately (potential new-span candidates) instead of being silently
dropped.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
    CognitionMainlineReadModelReader,
    _normalize_article_ref,
)
from fin_analyse.guo_teacher_research.source_contract import classify_g_source

_SCHEMA_VERSION = "fin.mainline-candidates/v1"
_DRAFT_NAME = "mainline-candidates.md"
_CST = ZoneInfo("Asia/Shanghai")
_AS_OF_RE = re.compile(r"as_of=(\d{4}-\d{2}-\d{2})")
# owner 2026-09-01 拍板边界：每日热点 = 老师 AI 汇总的参考信息（非老师看法），
# 不提名进 G 认知主线；人脉列（systematic_framework，同特刊档）保留。
_EXCLUDED_USAGES = frozenset({"ai_summary_reference"})


@dataclass(frozen=True)
class MainlineCandidateScanResult:
    """Content-free typed outcome; never article titles or paths."""

    schema_version: str = _SCHEMA_VERSION
    disposition: str = "SKIPPED"
    reason: str | None = None
    scanned: int = 0
    after_as_of: int = 0
    nominated: int = 0
    same_article: int = 0
    excluded_usage: int = 0
    not_eligible: int = 0
    malformed_dates: int = 0
    draft_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "disposition": self.disposition,
            "reason": self.reason,
            "scanned": self.scanned,
            "after_as_of": self.after_as_of,
            "nominated": self.nominated,
            "same_article": self.same_article,
            "excluded_usage": self.excluded_usage,
            "not_eligible": self.not_eligible,
            "malformed_dates": self.malformed_dates,
            "draft_path": self.draft_path,
        }


def _annotation_as_of(annotation_path: Path) -> date | None:
    """Parse the documented review anchor (``as_of=YYYY-MM-DD``); None if absent."""

    try:
        text = annotation_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _AS_OF_RE.search(text)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _load_index(index_path: Path) -> list[Any] | None:
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        entries = payload.get("entries") or payload.get("articles")
        if isinstance(entries, list):
            return entries
    return None


def _entry_date(entry: dict[str, Any]) -> date | None:
    """Date-part comparison only: ``as_of`` is a human review day, not an instant."""

    raw = entry.get("date")
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _unit_ids_by_article(payload: dict[str, Any]) -> dict[str, list[str]]:
    article_by_source = {
        str(source.get("source_id")): str(source.get("article_ref"))
        for source in payload.get("sources", [])
        if isinstance(source, dict)
    }
    by_article: dict[str, list[str]] = {}
    for unit in payload.get("units", []):
        if not isinstance(unit, dict):
            continue
        article_ref = article_by_source.get(str(unit.get("source_ref")))
        if article_ref is None:
            continue
        by_article.setdefault(article_ref, []).append(str(unit.get("unit_id")))
    return by_article


def _normalize_entry_ref(entry: dict[str, Any]) -> str:
    """Canonical article_ref for an index entry; "" when unmappable."""

    raw = entry.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        return _normalize_article_ref(raw)
    except Exception:  # noqa: BLE001 - single odd path must not kill the scan
        return ""


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _render_draft(
    *,
    as_of: date,
    scanned: int,
    after_as_of: int,
    excluded_usage: int,
    not_eligible: int,
    malformed_dates: int,
    candidates: list[dict[str, str]],
    same_articles: list[dict[str, str]],
) -> str:
    lines = [
        "# G 主线候选提名",
        "",
        "> 机器提名（g-mainline-growth-v1 部件1），纯读产物；owner 勾选后走起草协议",
        "> （部件2）。未勾选不代表否决，只代表未处理。as_of 锚："
        f"{as_of.isoformat()}。",
        "",
        "## 统计",
        "",
        f"- index 扫描：{scanned}；as_of 后：{after_as_of}；"
        f"排除 ai_summary_reference：{excluded_usage}；闭集未命中：{not_eligible}；"
        f"日期不可解析：{malformed_dates}。",
        f"- 同文已入档（潜在新增段落候选）：{len(same_articles)}；"
        f"本稿提名：{len(candidates)}。",
        "",
    ]
    if candidates:
        lines += [
            "## 候选（未入档）",
            "",
            "| # | date | 栏目 | 标题 | article_ref |",
            "| --- | --- | --- | --- | --- |",
        ]
        for index, row in enumerate(candidates, start=1):
            lines.append(
                f"| {index} | {_cell(row['date'])} | {_cell(row['column'])} | "
                f"{_cell(row['title'])} | {_cell(row['article_ref'])} |"
            )
        lines.append("")
    else:
        lines += ["## 候选（未入档）", "", "（无）", ""]
    if same_articles:
        lines += [
            "## 同文已入档（潜在新增段落候选）",
            "",
            "勾选前先核对既有单元是否已覆盖该文新段落；同文新增段落按起草协议"
            "追加新单元并同步 evolution 节点行。",
            "",
            "| date | 栏目 | 标题 | article_ref | 已入档单元 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in same_articles:
            lines.append(
                f"| {_cell(row['date'])} | {_cell(row['column'])} | "
                f"{_cell(row['title'])} | {_cell(row['article_ref'])} | "
                f"{_cell(row['unit_ids'])} |"
            )
        lines.append("")
    return "\n".join(lines)


def _write_draft(state_root: Path, draft: str) -> Path:
    target_dir = state_root / "fin-analyse"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_dir / _DRAFT_NAME
    tmp = target_dir / (_DRAFT_NAME + ".tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, draft.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.replace(tmp, target)
    return target


def scan_mainline_candidates(
    *,
    annotation_path: str | Path,
    readmodel_root: str | Path,
    index_path: str | Path | None = None,
    state_root: str | Path | None = None,
) -> MainlineCandidateScanResult:
    """Scan once; return a typed result and (on success) rewrite the draft.

    ``index_path`` defaults to ``<canonical KB root>/index.json``; callers that
    already hold the KB root (the consume wrapper) pass it explicitly so the
    knowledge-root seam stays the caller's decision.
    """

    def _skipped(reason: str) -> MainlineCandidateScanResult:
        return MainlineCandidateScanResult(disposition="SKIPPED", reason=reason)

    as_of = _annotation_as_of(Path(annotation_path))
    if as_of is None:
        return _skipped("annotation_as_of_missing")

    readout = CognitionMainlineReadModelReader(Path(readmodel_root)).read()
    if readout.failure_code is not None or not readout.payload:
        return _skipped("readmodel_unavailable")

    if index_path is None:
        from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

        index_path = default_knowledge_base_root() / "index.json"
    index = _load_index(Path(index_path))
    if index is None:
        return _skipped("index_unreadable")

    units_by_article = _unit_ids_by_article(readout.payload)
    candidates: list[dict[str, str]] = []
    same_articles: list[dict[str, str]] = []
    stats = {
        "after_as_of": 0,
        "excluded_usage": 0,
        "not_eligible": 0,
        "malformed_dates": 0,
    }
    for raw_entry in index:
        if not isinstance(raw_entry, dict):
            continue
        entry_date = _entry_date(raw_entry)
        if entry_date is None:
            stats["malformed_dates"] += 1
            continue
        if entry_date <= as_of:
            continue
        stats["after_as_of"] += 1
        decision = classify_g_source(
            raw_entry.get("column"),
            teacher_original=True,
            is_qa=raw_entry.get("is_qa") is True,
            priority_label=raw_entry.get("priority_label"),
        )
        if not decision.eligible or decision.classification is None:
            stats["not_eligible"] += 1
            continue
        if decision.classification.usage in _EXCLUDED_USAGES:
            stats["excluded_usage"] += 1
            continue
        article_ref = _normalize_entry_ref(raw_entry)
        row = {
            "date": str(raw_entry.get("date", "")),
            "column": str(raw_entry.get("column", "")),
            "title": str(raw_entry.get("title", "")),
            "article_ref": article_ref,
        }
        existing_units = units_by_article.get(article_ref) if article_ref else None
        if existing_units is not None:
            same_articles.append(
                {**row, "unit_ids": ", ".join(sorted(existing_units))}
            )
        else:
            candidates.append(row)

    candidates.sort(key=lambda row: (row["date"], row["title"]), reverse=True)
    same_articles.sort(key=lambda row: (row["date"], row["title"]), reverse=True)

    draft = _render_draft(
        as_of=as_of,
        scanned=len(index),
        after_as_of=stats["after_as_of"],
        excluded_usage=stats["excluded_usage"],
        not_eligible=stats["not_eligible"],
        malformed_dates=stats["malformed_dates"],
        candidates=candidates,
        same_articles=same_articles,
    )
    if state_root is None:
        state_root = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        )
    target = _write_draft(Path(state_root), draft)
    return MainlineCandidateScanResult(
        disposition="SCANNED",
        scanned=len(index),
        after_as_of=stats["after_as_of"],
        nominated=len(candidates),
        same_article=len(same_articles),
        excluded_usage=stats["excluded_usage"],
        not_eligible=stats["not_eligible"],
        malformed_dates=stats["malformed_dates"],
        draft_path=str(target),
    )
