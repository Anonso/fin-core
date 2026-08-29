"""Tests for cross-article `since` date filtering.

TDD: get_synthesis(since="2026-07-02") must filter out earlier articles/clusters,
and must explicitly report data gaps when filtering is unavailable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_cluster_dict(
    cluster_id: str,
    theme: str,
    article_ids: list[str],
    *,
    centroid_published_at: str = "",
) -> dict:
    return {
        "cluster_id": cluster_id,
        "theme": theme,
        "centroid_summary": f"Summary for {theme}",
        "article_ids": article_ids,
        "centroid_published_at": centroid_published_at,
        "article_count": len(article_ids),
        "created_at": centroid_published_at,
    }


def _make_analysis_dict(
    analysis_id: str,
    cluster_id: str,
    article_ids: list[str],
    *,
    generated_at: str = "",
) -> dict:
    return {
        "analysis_id": analysis_id,
        "cluster_id": cluster_id,
        "article_ids": article_ids,
        "generated_at": generated_at,
        "summary": f"Analysis of {cluster_id}",
        "focused_stocks": [],
        "themes": [],
    }


def _make_synthesis_dict(
    synthesis_id: str,
    *,
    generated_at: str = "",
    focused_stocks: list | None = None,
) -> dict:
    return {
        "synthesis_id": synthesis_id,
        "generated_at": generated_at,
        "focused_stocks": focused_stocks or [],
        "suggested_signal_queries": [],
        "source_mode": "formal_g",
        "mainline_themes": [],
    }


def _date_iso(days_ago: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ── TDD Step 1: since filter excludes earlier articles ────────────────────────


def test_get_synthesis_since_filters_earlier_clusters():
    """get_synthesis(since='2026-07-02') must exclude clusters with articles
    all published before that date."""
    from fin_analyse.cognition.cross_article.service import (
        CrossArticleSynthesisService,
    )

    since_date = _date_iso(1)  # yesterday
    before_since = _date_iso(10)  # 10 days ago
    after_since = _date_iso(0)  # today

    svc = CrossArticleSynthesisService()

    # Build synthetic clusters with published_at info
    old_cluster = _make_cluster_dict(
        "c-old",
        "Old Theme",
        ["a1", "a2"],
        centroid_published_at=f"{before_since}T10:00:00+00:00",
    )
    new_cluster = _make_cluster_dict(
        "c-new",
        "New Theme",
        ["a3", "a4"],
        centroid_published_at=f"{after_since}T10:00:00+00:00",
    )

    # Directly test _filter_clusters_by_since
    filtered, gaps = svc._filter_clusters_by_since([old_cluster, new_cluster], since_date)

    assert len(filtered) == 1, f"Expected 1 cluster after since filter, got {len(filtered)}"
    assert filtered[0]["cluster_id"] == "c-new", "New cluster should survive since filter"


def test_since_filter_reports_gap_when_filtering_unavailable():
    """When since filtering cannot be applied (no date metadata), service must
    report a data gap — not silently include everything."""
    from fin_analyse.cognition.cross_article.service import (
        CrossArticleSynthesisService,
    )

    since_date = _date_iso(1)
    svc = CrossArticleSynthesisService()

    # Clusters without published_at metadata
    no_date_cluster = _make_cluster_dict(
        "c-nodate",
        "No Date Theme",
        ["a1"],
        centroid_published_at="",  # no date
    )

    filtered, gaps = svc._filter_clusters_by_since([no_date_cluster], since_date, report_gaps=True)

    # When no date info available, include the cluster but report gap
    assert len(filtered) >= 1, "Cluster without date info should be included (conservative)"
    if not no_date_cluster.get("centroid_published_at"):
        assert gaps, "Must report gap when since filter cannot be applied"


def test_since_filter_with_mixed_dates():
    """Cluster with mix of old and new articles should survive since filter
    if at least one article is within the since window."""
    from fin_analyse.cognition.cross_article.service import (
        CrossArticleSynthesisService,
    )

    since_date = _date_iso(1)
    after_since = _date_iso(0)
    before_since = _date_iso(10)

    # We test the internal article-filtering logic
    svc = CrossArticleSynthesisService()

    # Simulate: cluster has one new article, one old
    article_dates = {
        "a-new": f"{after_since}T10:00:00+00:00",
        "a-old": f"{before_since}T10:00:00+00:00",
    }

    has_recent = svc._any_article_after_since(article_dates, since_date)
    assert has_recent is True, "Cluster with at least one recent article should pass since filter"


def test_since_filter_all_old_articles_excluded():
    """Cluster where ALL articles are before since date must be excluded."""
    from fin_analyse.cognition.cross_article.service import (
        CrossArticleSynthesisService,
    )

    since_date = _date_iso(1)
    before_since = _date_iso(10)

    svc = CrossArticleSynthesisService()

    article_dates = {
        "a-old-1": f"{before_since}T10:00:00+00:00",
        "a-old-2": f"{before_since}T08:00:00+00:00",
    }

    has_recent = svc._any_article_after_since(article_dates, since_date)
    assert has_recent is False, "Cluster with all old articles should be excluded by since filter"
