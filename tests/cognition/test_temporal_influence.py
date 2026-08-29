"""Tests for TemporalInfluenceService — fresh G attention ranking.

TDD: test_new_xingdapai_tekan_gets_intraday_top_attention is the canonical
starting point.  All tests must fail before implementation exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_article(
    article_id: str,
    title: str,
    column: str,
    published_at: str,
    *,
    source_classification: str = "teacher_original",
    persona_eligible: bool = True,
    deep_read_complete: bool = True,
    deep_read_degraded: bool = False,
    score: float | None = None,
) -> dict:
    """Build a synthetic article dict matching expected index/article metadata shape."""
    return {
        "article_id": article_id,
        "title": title,
        "column": column,
        "published_at": published_at,
        "source_classification": source_classification,
        "persona_eligible": persona_eligible,
        "deep_read_complete": deep_read_complete,
        "deep_read_degraded": deep_read_degraded,
        "score": score,
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _hours_ago_iso(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


# ── TDD Step 1: fresh 特刊 gets top attention ────────────────────────────────


def test_new_xingdapai_tekan_gets_intraday_top_attention():
    """A 星大派特刊 published within 0-6h must be the highest-attention event."""
    from fin_analyse.cognition.temporal_influence import (
        TemporalInfluenceRequest,
        TemporalInfluenceService,
    )

    fresh_tekan = _make_article(
        article_id="art-001",
        title="算力金属：锑的战略价值重估",
        column="星大派特刊",
        published_at=_hours_ago_iso(2),
    )
    old_article = _make_article(
        article_id="art-002",
        title="一周市场回顾",
        column="普通",
        published_at=_hours_ago_iso(72),
    )
    research_ref = _make_article(
        article_id="art-003",
        title="券商研报：半导体行业展望",
        column="普通",
        published_at=_hours_ago_iso(24),
        source_classification="research_reference",
        persona_eligible=False,
    )

    svc = TemporalInfluenceService()
    ctx = svc.build_context(
        TemporalInfluenceRequest(
            candidate_articles=[fresh_tekan, old_article, research_ref],
            now=_now_iso(),
        )
    )

    assert ctx.events, "Expected at least one event"
    top = ctx.events[0]
    assert top.article_id == "art-001", f"Expected fresh tekan top, got {top.article_id}"
    assert top.attention_score > 0.0, "Attention score must be positive"
    # Fresh tekan must have highest attention
    scores = [e.attention_score for e in ctx.events]
    assert top.attention_score == max(scores), "Fresh tekan must have highest attention score"
    # Source classification preserved
    assert top.source_classification == "teacher_original"


# ── TDD Step 2: incomplete / DOM fallback lowers completeness ─────────────────


def test_incomplete_deep_read_lowers_completeness_weight():
    """Article with degraded deep_read must have lower completeness weight."""
    from fin_analyse.cognition.temporal_influence import (
        TemporalInfluenceRequest,
        TemporalInfluenceService,
    )

    complete = _make_article(
        article_id="art-complete",
        title="完整深度分析",
        column="星大派特刊",
        published_at=_hours_ago_iso(3),
        deep_read_complete=True,
        deep_read_degraded=False,
    )
    incomplete = _make_article(
        article_id="art-incomplete",
        title="不完整深度分析（DOM fallback）",
        column="星大派特刊",
        published_at=_hours_ago_iso(4),
        deep_read_complete=False,
        deep_read_degraded=True,
    )

    svc = TemporalInfluenceService()
    ctx = svc.build_context(
        TemporalInfluenceRequest(
            candidate_articles=[complete, incomplete],
            now=_now_iso(),
        )
    )

    assert len(ctx.events) >= 2
    complete_ev = next(e for e in ctx.events if e.article_id == "art-complete")
    incomplete_ev = next(e for e in ctx.events if e.article_id == "art-incomplete")

    # Completeness weight lower for degraded
    assert incomplete_ev.completeness_weight < complete_ev.completeness_weight, (
        f"Degraded completeness {incomplete_ev.completeness_weight} "
        f"should be < complete {complete_ev.completeness_weight}"
    )

    # Degraded flag exposed
    assert incomplete_ev.degraded is True
    assert complete_ev.degraded is False


# ── TDD Step 3: freshness affects attention, NOT confidence ──────────────────


def test_freshness_affects_attention_not_confidence():
    """Latest 星大派 boosts attention_score, does NOT modify confidence."""
    from fin_analyse.cognition.temporal_influence import (
        TemporalInfluenceRequest,
        TemporalInfluenceService,
    )

    artikel = _make_article(
        article_id="art-fresh",
        title="稀土政策转向深度分析",
        column="星大派锐评",
        published_at=_hours_ago_iso(1),
    )

    svc = TemporalInfluenceService()
    ctx = svc.build_context(
        TemporalInfluenceRequest(
            candidate_articles=[artikel],
            now=_now_iso(),
        )
    )

    assert len(ctx.events) == 1
    ev = ctx.events[0]
    assert ev.attention_score > 0.0

    # Confidence must NOT be directly modified by temporal influence
    assert ev.confidence_modifier == 0.0, "Temporal influence must not directly modify confidence"

    # advisory_only must be true
    assert ctx.advisory_only is True
    assert ctx.execution_allowed is False


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_empty_candidates_returns_empty_context():
    """No candidate articles → empty context, no crash."""
    from fin_analyse.cognition.temporal_influence import (
        TemporalInfluenceRequest,
        TemporalInfluenceService,
    )

    svc = TemporalInfluenceService()
    ctx = svc.build_context(TemporalInfluenceRequest(candidate_articles=[], now=_now_iso()))

    assert ctx.events == []
    assert ctx.top_event is None
    assert ctx.data_gaps  # should report no candidates


def test_mixed_sources_preserve_classification():
    """Teacher original and research reference must keep distinct classifications."""
    from fin_analyse.cognition.temporal_influence import (
        TemporalInfluenceRequest,
        TemporalInfluenceService,
    )

    articles = [
        _make_article(
            article_id="art-g",
            title="G source article",
            column="星大派特刊",
            published_at=_hours_ago_iso(2),
            source_classification="teacher_original",
        ),
        _make_article(
            article_id="art-z",
            title="Z reference article",
            column="普通",
            published_at=_hours_ago_iso(24),
            source_classification="research_reference",
            persona_eligible=False,
        ),
    ]

    svc = TemporalInfluenceService()
    ctx = svc.build_context(TemporalInfluenceRequest(candidate_articles=articles, now=_now_iso()))

    g_ev = next(e for e in ctx.events if e.article_id == "art-g")
    z_ev = next(e for e in ctx.events if e.article_id == "art-z")

    assert g_ev.source_classification == "teacher_original"
    assert z_ev.source_classification == "research_reference"
    # G must have higher attention
    assert g_ev.attention_score > z_ev.attention_score


# ── P0-1: date-only published_at ────────────────────────────────────────────


def test_date_only_published_at_no_type_error():
    """published_at='2026-07-02' (date-only, no time) must not raise TypeError."""
    from fin_analyse.cognition.temporal_influence import (
        TemporalInfluenceRequest,
        TemporalInfluenceService,
    )

    article = _make_article(
        article_id="art-date-only",
        title="Date-only test",
        column="星大派特刊",
        published_at="2026-07-02",  # date-only, no time component
    )

    svc = TemporalInfluenceService()
    ctx = svc.build_context(
        TemporalInfluenceRequest(
            candidate_articles=[article],
            now="2026-07-02T10:00:00+00:00",
        )
    )

    assert len(ctx.events) == 1
    assert ctx.events[0].article_id == "art-date-only"
    assert ctx.events[0].attention_score > 0.0


def test_naive_datetime_gets_utc_normalized():
    """Naive datetime like '2026-07-02T10:00:00' (no tz) must be normalized,
    not crash with offset-naive vs offset-aware comparison."""
    from fin_analyse.cognition.temporal_influence import (
        TemporalInfluenceRequest,
        TemporalInfluenceService,
    )

    article = _make_article(
        article_id="art-naive",
        title="Naive datetime test",
        column="星大派锐评",
        published_at="2026-07-02T10:00:00",  # naive — no timezone
    )

    svc = TemporalInfluenceService()
    ctx = svc.build_context(
        TemporalInfluenceRequest(
            candidate_articles=[article],
            now="2026-07-02T12:00:00+00:00",
        )
    )

    assert len(ctx.events) == 1
    assert ctx.events[0].freshness_score > 0.0
