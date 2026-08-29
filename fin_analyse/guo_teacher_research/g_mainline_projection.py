"""Stateless, read-only G mainline projection (P1).

Projects the G working set's bound articles into per-theme "mainline"
summaries where every thesis keeps its time and source identity.  Pure
function of its inputs: no state, no writes, no caches, no background
work, no generative aggregation.  Absence, emptiness, budget overflow or
invalid entries surface as typed gaps and never block a consultation.

Input binding: one manifest ``generation`` + content ``manifest_sha256``
must be supplied by the caller; the projection never resolves or writes
anything itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_BUDGET_DEFAULT = 10
_MAX_THESES_PER_THEME = 3


@dataclass(frozen=True, slots=True)
class MainlineThesis:
    source_ref: str
    title: str
    published_at: str
    generation: str
    thesis_heads: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MainlineTheme:
    theme: str
    theses: tuple[MainlineThesis, ...]


@dataclass(frozen=True, slots=True)
class GMainlineProjectionResult:
    themes: tuple[MainlineTheme, ...]
    generation: str
    manifest_sha256: str
    data_gaps: tuple[str, ...]


def _sort_key(value: str) -> str:
    return value or ""


def project_mainline(
    articles: Sequence[Mapping[str, Any]],
    *,
    generation: str,
    manifest_sha256: str,
    budget: int = _BUDGET_DEFAULT,
) -> GMainlineProjectionResult:
    """Group bounded articles by theme with deterministic ordering."""
    if budget <= 0:
        return GMainlineProjectionResult(
            themes=(), generation=generation, manifest_sha256=manifest_sha256,
            data_gaps=("g_mainline_budget_truncated",),
        )
    gaps: list[str] = []
    by_theme: dict[str, list[MainlineThesis]] = {}
    for raw in articles:
        if not isinstance(raw, Mapping):
            gaps.append("g_mainline_entry_invalid")
            continue
        source_ref = str(raw.get("source_ref") or "")
        title = str(raw.get("title") or "")
        published_at = str(raw.get("published_at") or "")
        clusters = raw.get("theme_clusters")
        if not source_ref or not published_at or not isinstance(clusters, (list, tuple)):
            gaps.append("g_mainline_entry_invalid")
            continue
        heads = raw.get("thesis_heads")
        thesis_heads = tuple(str(h) for h in heads) if isinstance(heads, (list, tuple)) else ()
        thesis = MainlineThesis(
            source_ref=source_ref,
            title=title,
            published_at=published_at,
            generation=generation,
            thesis_heads=thesis_heads[: _MAX_THESES_PER_THEME],
        )
        for cluster in clusters:
            theme = str(cluster).strip()
            if theme:
                by_theme.setdefault(theme, []).append(thesis)

    if not by_theme:
        gaps.append("g_mainline_no_samples")
        return GMainlineProjectionResult(
            themes=(), generation=generation, manifest_sha256=manifest_sha256,
            data_gaps=tuple(gaps),
        )

    # 确定性:主题排序;主题内按 published_at 升序、同时间按 source_ref
    ordered_themes: list[MainlineTheme] = []
    remaining = budget
    for theme in sorted(by_theme, key=_sort_key):
        if remaining <= 0:
            # 预算已尽:后续主题全部记录截断 gap,不产出条目
            gaps.append("g_mainline_budget_truncated")
            continue
        theses = sorted(
            by_theme[theme],
            key=lambda t: (t.published_at, t.source_ref),
        )
        if len(theses) > remaining:
            gaps.append("g_mainline_budget_truncated")
            theses = theses[:remaining]
        if not theses:
            continue
        ordered_themes.append(MainlineTheme(theme=theme, theses=tuple(theses)))
        remaining -= len(theses)

    return GMainlineProjectionResult(
        themes=tuple(ordered_themes),
        generation=generation,
        manifest_sha256=manifest_sha256,
        data_gaps=tuple(dict.fromkeys(gaps)),
    )


class GMainlineProjection:
    """Stateless facade over :func:`project_mainline`."""

    def project(
        self,
        articles: Sequence[Mapping[str, Any]],
        *,
        generation: str,
        manifest_sha256: str,
        budget: int = _BUDGET_DEFAULT,
    ) -> GMainlineProjectionResult:
        return project_mainline(
            articles,
            generation=generation,
            manifest_sha256=manifest_sha256,
            budget=budget,
        )
