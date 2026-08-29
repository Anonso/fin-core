"""TemporalInfluenceService — fresh G attention ranking without confidence leakage.

Design invariants (from plan):
- Freshness boosts attention_score, NOT confidence.
- Completeness weight reflects deep_read quality (DOM fallback lowers it).
- Source classification preserved: teacher_original > research_reference.
- advisory_only is always True — this module never generates trade signals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Columns considered "星大派 G source" — always eligible for top attention
XINGDAPAI_COLUMNS = frozenset({"星大派特刊", "星大派锐评"})

# Attention score weights (sum to 1.0)
WEIGHT_FRESHNESS = 0.45
WEIGHT_SOURCE = 0.30
WEIGHT_COMPLETENESS = 0.25

# Freshness windows
FRESH_WINDOW_HOURS = 6  # 0-6h = peak freshness
RECENT_WINDOW_HOURS = 24  # 6-24h = recent
# > 24h = normal/stale


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TemporalInfluenceRequest:
    """Input to TemporalInfluenceService.build_context()."""

    candidate_articles: list[dict[str, Any]] = field(default_factory=list)
    now: str = ""  # ISO datetime for deterministic scoring
    positions_json: str = ""  # optional: user portfolio for relevance boost


@dataclass
class TemporalInfluenceEvent:
    """A single ranked event in the temporal influence context."""

    article_id: str
    title: str
    column: str = ""
    published_at: str = ""
    source_classification: str = "unknown"
    persona_eligible: bool = False
    deep_read_complete: bool = False
    deep_read_degraded: bool = False

    # Scores
    freshness_score: float = 0.0
    source_score: float = 0.0
    completeness_weight: float = 0.0
    attention_score: float = 0.0

    # Flags
    confidence_modifier: float = 0.0  # always 0.0 — invariant
    degraded: bool = False
    is_xingdapai: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "title": self.title,
            "column": self.column,
            "published_at": self.published_at,
            "source_classification": self.source_classification,
            "persona_eligible": self.persona_eligible,
            "deep_read_complete": self.deep_read_complete,
            "deep_read_degraded": self.deep_read_degraded,
            "freshness_score": self.freshness_score,
            "source_score": self.source_score,
            "completeness_weight": self.completeness_weight,
            "attention_score": self.attention_score,
            "confidence_modifier": self.confidence_modifier,
            "degraded": self.degraded,
            "is_xingdapai": self.is_xingdapai,
        }


@dataclass
class TemporalInfluenceContext:
    """Output of TemporalInfluenceService.build_context().

    Provides ranked fresh G evidence for consumption by agent tasks,
    priority article runners, and briefing pipelines.

    Invariants:
    - confidence_modifier is always 0.0 for every event.
    - advisory_only is always True.
    - Events are sorted by attention_score descending.
    """

    events: list[TemporalInfluenceEvent] = field(default_factory=list)
    top_event: TemporalInfluenceEvent | None = None
    advisory_only: bool = True
    execution_allowed: bool = False
    data_gaps: list[str] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [e.to_dict() for e in self.events],
            "top_event": self.top_event.to_dict() if self.top_event else None,
            "advisory_only": self.advisory_only,
            "execution_allowed": self.execution_allowed,
            "data_gaps": list(self.data_gaps),
            "generated_at": self.generated_at,
        }


# ── Service ───────────────────────────────────────────────────────────────────


class TemporalInfluenceService:
    """Compute task-scoped fresh G context with attention ranking.

    Usage::

        svc = TemporalInfluenceService()
        ctx = svc.build_context(TemporalInfluenceRequest(
            candidate_articles=articles,
            now="2026-07-02T10:00:00+00:00",
        ))
        # ctx.events sorted by attention_score desc
        # ctx.top_event is the highest-attention event
    """

    def build_context(self, request: TemporalInfluenceRequest) -> TemporalInfluenceContext:
        now = self._parse_now(request.now)
        data_gaps: list[str] = []

        if not request.candidate_articles:
            data_gaps.append("no_candidate_articles")
            return TemporalInfluenceContext(
                events=[],
                top_event=None,
                advisory_only=True,
                data_gaps=data_gaps,
                generated_at=now.isoformat(),
            )

        events: list[TemporalInfluenceEvent] = []
        for art in request.candidate_articles:
            event = self._score_article(art, now)
            events.append(event)

        # Sort by attention_score descending
        events.sort(key=lambda e: e.attention_score, reverse=True)

        top = events[0] if events else None

        return TemporalInfluenceContext(
            events=events,
            top_event=top,
            advisory_only=True,
            data_gaps=data_gaps,
            generated_at=now.isoformat(),
        )

    # ── Scoring ───────────────────────────────────────────────────────────

    def _score_article(self, art: dict[str, Any], now: datetime) -> TemporalInfluenceEvent:
        article_id = str(art.get("article_id", ""))
        title = str(art.get("title", ""))
        column = str(art.get("column", ""))
        published_at = str(art.get("published_at", ""))
        source_classification = str(art.get("source_classification", "unknown"))
        persona_eligible = bool(art.get("persona_eligible", False))
        deep_read_complete = bool(art.get("deep_read_complete", True))
        deep_read_degraded = bool(art.get("deep_read_degraded", False))

        freshness = self._compute_freshness(published_at, now)
        source = self._compute_source_score(column, source_classification, persona_eligible)
        completeness = self._compute_completeness(deep_read_complete, deep_read_degraded)

        # Weighted attention score
        attention = (
            WEIGHT_FRESHNESS * freshness
            + WEIGHT_SOURCE * source
            + WEIGHT_COMPLETENESS * completeness
        )

        is_xdp = column in XINGDAPAI_COLUMNS

        return TemporalInfluenceEvent(
            article_id=article_id,
            title=title,
            column=column,
            published_at=published_at,
            source_classification=source_classification,
            persona_eligible=persona_eligible,
            deep_read_complete=deep_read_complete,
            deep_read_degraded=deep_read_degraded,
            freshness_score=freshness,
            source_score=source,
            completeness_weight=completeness,
            attention_score=round(attention, 4),
            confidence_modifier=0.0,  # INVARIANT: never modify confidence
            degraded=deep_read_degraded or not deep_read_complete,
            is_xingdapai=is_xdp,
        )

    @staticmethod
    def _compute_freshness(published_at: str, now: datetime) -> float:
        """Score 0.0-1.0 based on how recently the article was published.

        0-6h   → 1.0 (peak)
        6-24h  → 0.5-1.0 linear decay
        24-72h → 0.1-0.5 linear decay
        >72h   → 0.1 (baseline)
        """
        try:
            pub_dt = datetime.fromisoformat(published_at)
            # Normalize naive datetimes to UTC to avoid offset-naive/aware TypeError
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return 0.1  # unknown date → baseline

        age_hours = (now - pub_dt).total_seconds() / 3600.0

        if age_hours <= FRESH_WINDOW_HOURS:
            return 1.0
        elif age_hours <= RECENT_WINDOW_HOURS:
            # Linear decay from 1.0 at 6h to 0.5 at 24h
            return 1.0 - 0.5 * (age_hours - FRESH_WINDOW_HOURS) / (
                RECENT_WINDOW_HOURS - FRESH_WINDOW_HOURS
            )
        elif age_hours <= 72:
            # Linear decay from 0.5 at 24h to 0.1 at 72h
            return 0.5 - 0.4 * (age_hours - RECENT_WINDOW_HOURS) / (72 - RECENT_WINDOW_HOURS)
        else:
            return 0.1

    @staticmethod
    def _compute_source_score(
        column: str, source_classification: str, persona_eligible: bool
    ) -> float:
        """Score 0.0-1.0 based on source authority.

        星大派特刊/锐评 + teacher_original + persona_eligible → 1.0
        teacher_original (non-xingdapai)                         → 0.6
        research_reference                                        → 0.2
        unknown / other                                           → 0.1
        """
        if (
            column in XINGDAPAI_COLUMNS
            and source_classification == "teacher_original"
            and persona_eligible
        ):
            return 1.0
        if source_classification == "teacher_original":
            return 0.6
        if source_classification == "research_reference":
            return 0.2
        return 0.1

    @staticmethod
    def _compute_completeness(deep_read_complete: bool, deep_read_degraded: bool) -> float:
        """Score 0.0-1.0 based on deep_read quality.

        complete + not degraded → 1.0
        complete + degraded      → 0.3 (DOM fallback)
        incomplete               → 0.0
        """
        if not deep_read_complete:
            return 0.0
        if deep_read_degraded:
            return 0.3
        return 1.0

    @staticmethod
    def _parse_now(now_str: str) -> datetime:
        if now_str:
            try:
                dt = datetime.fromisoformat(now_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except (ValueError, TypeError):
                pass
        return datetime.now(UTC)
