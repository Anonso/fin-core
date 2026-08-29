"""Tests for fin_analyse/temporal/ — internal TemporalService interface.

TDD entry points:
- test_assess_priority_g_source_article_returns_temporal_assessment
- test_assessment_never_allows_confidence_boost
- test_publish_freshness_is_separate_from_content_time_sensitivity
- test_priority_article_adapter_builds_supported_request
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# ── helpers ──────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _hours_ago_iso(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def _sample_article(**overrides) -> dict:
    return {
        "article_id": "art-001",
        "title": "算力金属：锑的战略价值重估",
        "column": "星大派特刊",
        "urgency": "T0",
        "published_at": _hours_ago_iso(2),
        "source_classification": "teacher_original",
        "persona_eligible": True,
        "deep_read_complete": True,
        "deep_read_degraded": False,
        **overrides,
    }


def _sample_deep_read(**overrides) -> dict:
    return {
        "units": [
            {"title": "锑价突破前高", "content": "锑价近期突破历史前高"},
        ],
        "clocks": [
            {"label": "intraday_event", "confidence": 0.9},
        ],
        "theme_clusters": [
            {"theme": "关键矿产供应链", "confidence": 0.85},
        ],
        "evidence_chains": [
            {"reasoning": "锑供应紧张→价格上行→相关公司受益"},
        ],
        "suggestions": [
            {"suggestion_text": "关注锑产业链标的短期机会"},
        ],
        "status": "ok",
        **overrides,
    }


# ── TDD Step 1: TemporalService.assess() returns TemporalAssessment ──────────


def test_assess_priority_g_source_article_returns_temporal_assessment():
    """A 星大派特刊 article with deep_read must produce a full TemporalAssessment."""
    from fin_analyse.temporal.models import (
        TemporalAssessment,
        TemporalAssessmentRequest,
        TemporalItem,
        TemporalTaskContext,
    )
    from fin_analyse.temporal.service import TemporalService

    article = _sample_article()
    dr = _sample_deep_read()
    now = _now_iso()

    # Build request via adapter (tested separately)
    item = TemporalItem(
        item_id=article["article_id"],
        title=article["title"],
        source_scope="g_source",
        source_classification=article["source_classification"],
        column=article["column"],
        published_at=article["published_at"],
        semantic_payload={
            "units": dr.get("units", []),
            "clocks": dr.get("clocks", []),
            "theme_clusters": dr.get("theme_clusters", []),
            "evidence_chains": dr.get("evidence_chains", []),
            "suggestions": dr.get("suggestions", []),
        },
        quality_flags={
            "deep_read_complete": article.get("deep_read_complete", True),
            "deep_read_degraded": article.get("deep_read_degraded", False),
        },
    )

    request = TemporalAssessmentRequest(
        item_type="g_source_article",
        context_mode="priority_article",
        now=now,
        items=(item,),
        task=TemporalTaskContext(),
    )

    svc = TemporalService()
    result = svc.assess(request)

    assert isinstance(result, TemporalAssessment)
    # Required output fields
    assert isinstance(result.context, dict)
    assert isinstance(result.content_time_sensitivity, dict)
    assert isinstance(result.publish_freshness, str)
    assert isinstance(result.events, tuple)
    assert result.top_event is None or isinstance(result.top_event, dict)
    assert isinstance(result.attention_policy, dict)
    assert isinstance(result.data_gaps, tuple)
    # Invariants
    assert result.confidence_modifier == 0.0
    assert result.confidence_boost_allowed is False
    assert result.advisory_only is True
    assert result.execution_allowed is False


# ── TDD Step 2: confidence invariants ────────────────────────────────────────


def test_assessment_never_allows_confidence_boost():
    """TemporalAssessment must always have confidence_modifier==0.0 and boost_allowed==False."""
    from fin_analyse.temporal.models import (
        TemporalAssessmentRequest,
        TemporalItem,
        TemporalTaskContext,
    )
    from fin_analyse.temporal.service import TemporalService

    article = _sample_article()
    dr = _sample_deep_read()

    item = TemporalItem(
        item_id=article["article_id"],
        title=article["title"],
        source_scope="g_source",
        column=article["column"],
        published_at=article["published_at"],
        semantic_payload={
            "units": dr.get("units", []),
            "clocks": dr.get("clocks", []),
        },
        quality_flags={
            "deep_read_complete": True,
            "deep_read_degraded": False,
        },
    )

    request = TemporalAssessmentRequest(
        item_type="g_source_article",
        context_mode="priority_article",
        now=_now_iso(),
        items=(item,),
        task=TemporalTaskContext(),
    )

    result = TemporalService().assess(request)

    # Must be exactly 0.0 — never allow temporal to boost confidence
    assert result.confidence_modifier == 0.0
    assert result.confidence_boost_allowed is False
    assert result.advisory_only is True
    assert result.execution_allowed is False


# ── TDD Step 3: publish_freshness vs content_time_sensitivity ────────────────


def test_publish_freshness_is_separate_from_content_time_sensitivity():
    """publish_freshness expresses objective recency, NOT time sensitivity category.

    A 2-day-old article with quote/supply-demand content should still get
    short_term_tracking (content-driven), while publish_freshness reflects age.
    """
    from fin_analyse.temporal.models import (
        TemporalAssessmentRequest,
        TemporalItem,
        TemporalTaskContext,
    )
    from fin_analyse.temporal.service import TemporalService

    two_days_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()

    article = _sample_article(
        article_id="art-old-quotes",
        title="锂矿供需格局展望与报价跟踪",
        published_at=two_days_ago,
        urgency="T1",
    )
    dr = _sample_deep_read(
        units=[
            {"title": "锂盐报价更新", "content": "电池级碳酸锂报价..."},
            {"title": "供需分析", "content": "供应偏紧..."},
        ],
        clocks=[{"label": "short_term_tracking", "confidence": 0.7}],
    )

    item = TemporalItem(
        item_id=article["article_id"],
        title=article["title"],
        source_scope="g_source",
        column=article["column"],
        published_at=article["published_at"],
        semantic_payload={
            "units": dr.get("units", []),
            "clocks": dr.get("clocks", []),
        },
        quality_flags={
            "deep_read_complete": True,
            "deep_read_degraded": False,
        },
    )

    request = TemporalAssessmentRequest(
        item_type="g_source_article",
        context_mode="priority_article",
        now=_now_iso(),
        items=(item,),
        task=TemporalTaskContext(),
    )

    result = TemporalService().assess(request)

    # publish_freshness reflects age (should be 24-72h or >72h for 2-day-old article)
    assert result.publish_freshness != ""
    # content_time_sensitivity must be present
    assert isinstance(result.content_time_sensitivity, dict)
    # The publish_freshness string should NOT be the same as the category
    # (freshness is about recency, category is about content meaning)
    assert result.publish_freshness != result.content_time_sensitivity.get("category", "")


# ── TDD Step 4: adapter builds supported request ─────────────────────────────


def test_priority_article_adapter_builds_supported_request():
    """build_priority_article_temporal_request produces a valid request for assess()."""
    from fin_analyse.temporal.models import TemporalAssessmentRequest
    from fin_analyse.temporal.service import (
        TemporalService,
        build_priority_article_temporal_request,
    )

    article = _sample_article()
    dr = _sample_deep_read()
    now = _now_iso()

    request = build_priority_article_temporal_request(
        article=article,
        deep_read_result=dr,
        existing_temporal_context=None,
        now=now,
    )

    assert isinstance(request, TemporalAssessmentRequest)
    assert request.item_type == "g_source_article"
    assert request.context_mode == "priority_article"
    assert request.now == now
    assert len(request.items) == 1

    item = request.items[0]
    assert item.item_id == article["article_id"]
    assert item.title == article["title"]
    assert item.source_scope == "g_source"
    assert item.column == article["column"]
    assert item.published_at == article["published_at"]
    # semantic_payload must carry deep_read artifacts
    assert "units" in item.semantic_payload
    assert "clocks" in item.semantic_payload
    assert "theme_clusters" in item.semantic_payload
    assert "evidence_chains" in item.semantic_payload
    assert "suggestions" in item.semantic_payload
    # quality_flags
    assert item.quality_flags.get("deep_read_complete") is True
    assert item.quality_flags.get("deep_read_degraded") is False

    # Request must be consumable by TemporalService.assess()
    result = TemporalService().assess(request)
    assert result.context is not None
    assert result.content_time_sensitivity is not None


# ── TDD Step 5: empty items → data_gap ───────────────────────────────────────


def test_empty_items_returns_no_temporal_items_data_gap():
    """Empty items must produce a data_gap, not a crash."""
    from fin_analyse.temporal.models import (
        TemporalAssessmentRequest,
        TemporalTaskContext,
    )
    from fin_analyse.temporal.service import TemporalService

    request = TemporalAssessmentRequest(
        item_type="g_source_article",
        context_mode="priority_article",
        now=_now_iso(),
        items=(),
        task=TemporalTaskContext(),
    )

    result = TemporalService().assess(request)
    assert "no_temporal_items" in result.data_gaps
    assert len(result.events) == 0
    assert result.top_event is None


# ── TDD Step 6: unsupported item_type ────────────────────────────────────────


def test_unsupported_item_type_returns_data_gap():
    """Unsupported item_type must return a controlled data_gap, not a crash."""
    from fin_analyse.temporal.models import (
        TemporalAssessmentRequest,
        TemporalItem,
        TemporalTaskContext,
    )
    from fin_analyse.temporal.service import TemporalService

    item = TemporalItem(
        item_id="x",
        title="test",
        source_scope="market_data",
        published_at=_hours_ago_iso(1),
    )

    request = TemporalAssessmentRequest(
        item_type="market_data_snapshot",  # not supported in phase A
        context_mode="priority_article",
        now=_now_iso(),
        items=(item,),
        task=TemporalTaskContext(),
    )

    result = TemporalService().assess(request)
    assert "unsupported_temporal_item_type" in result.data_gaps


# ── TDD Step 7: deep_read missing → conservative fallback ────────────────────


def test_missing_deep_read_produces_conservative_fallback():
    """When deep_read payload is empty, use G source conservative fallback."""
    from fin_analyse.temporal.models import (
        TemporalAssessmentRequest,
        TemporalItem,
        TemporalTaskContext,
    )
    from fin_analyse.temporal.service import TemporalService

    article = _sample_article()
    item = TemporalItem(
        item_id=article["article_id"],
        title=article["title"],
        source_scope="g_source",
        column=article["column"],
        published_at=article["published_at"],
        semantic_payload={},
        quality_flags={
            "deep_read_complete": False,
            "deep_read_degraded": True,
        },
    )

    request = TemporalAssessmentRequest(
        item_type="g_source_article",
        context_mode="priority_article",
        now=_now_iso(),
        items=(item,),
        task=TemporalTaskContext(),
    )

    result = TemporalService().assess(request)
    # Should not crash; should produce a result
    assert result.context is not None
    assert result.content_time_sensitivity is not None
    # With no deep_read, G source falls back to short_term_tracking + llm_enrichment_pending
    assert result.data_gaps is not None


# ── TDD Step 8: package exports TemporalService ────────────────────────────────


def test_temporal_package_exports_service():
    """from fin_analyse.temporal import TemporalService must resolve."""
    from fin_analyse.temporal import TemporalService  # noqa: F811

    assert TemporalService is not None
    # Verify it's the class, not something else
    assert isinstance(TemporalService, type)
    svc = TemporalService()
    assert hasattr(svc, "assess")


# ── TDD Step 9: unsupported context_mode ───────────────────────────────────────


def test_unsupported_context_mode_returns_data_gap():
    """Unsupported context_mode must return a controlled data_gap, not proceed silently."""
    from fin_analyse.temporal.models import (
        TemporalAssessmentRequest,
        TemporalItem,
        TemporalTaskContext,
    )
    from fin_analyse.temporal.service import TemporalService

    item = TemporalItem(
        item_id="x",
        title="test",
        source_scope="g_source",
        source_classification="teacher_original",
        column="星大派特刊",
        published_at=_hours_ago_iso(1),
        semantic_payload={
            "units": [],
            "clocks": [],
        },
        quality_flags={
            "deep_read_complete": True,
            "deep_read_degraded": False,
        },
    )

    request = TemporalAssessmentRequest(
        item_type="g_source_article",  # valid item_type
        context_mode="knowledge_window",  # NOT supported in Phase A or B1
        now=_now_iso(),
        items=(item,),
        task=TemporalTaskContext(),
    )

    result = TemporalService().assess(request)
    # knowledge_window is now a supported context_mode (compatible adapters),
    # but g_source_article × knowledge_window is an invalid pair
    assert "unsupported_temporal_item_context_pair" in result.data_gaps
    # Conservative defaults
    assert result.context == {}
    assert result.content_time_sensitivity == {}
    assert result.publish_freshness == ""
    assert result.events == ()
    assert result.top_event is None
    assert result.confidence_modifier == 0.0
    assert result.confidence_boost_allowed is False
    assert result.advisory_only is True


# ── TDD Step 10: invalid item_type × context_mode pairs ────────────────────


def test_g_source_article_with_market_data_freshness_mode_returns_pair_gap():
    """g_source_article + market_data_freshness is an invalid pair — must return pair gap."""
    from fin_analyse.temporal.models import (
        TemporalAssessmentRequest,
        TemporalItem,
        TemporalTaskContext,
    )
    from fin_analyse.temporal.service import TemporalService

    item = TemporalItem(
        item_id="art-001",
        title="test article",
        source_scope="g_source",
        source_classification="teacher_original",
        column="星大派特刊",
        published_at=_hours_ago_iso(1),
        semantic_payload={"units": [], "clocks": []},
        quality_flags={"deep_read_complete": True, "deep_read_degraded": False},
    )

    request = TemporalAssessmentRequest(
        item_type="g_source_article",
        context_mode="market_data_freshness",
        now=_now_iso(),
        items=(item,),
        task=TemporalTaskContext(),
    )

    result = TemporalService().assess(request)
    assert "unsupported_temporal_item_context_pair" in result.data_gaps
    # Must NOT produce priority article output
    assert result.context == {}
    assert result.content_time_sensitivity == {}
    assert result.publish_freshness == ""
    assert result.events == ()
    assert result.top_event is None


def test_market_snapshot_with_priority_article_mode_returns_pair_gap():
    """market_snapshot + priority_article is an invalid pair — must return pair gap."""
    from fin_analyse.temporal.models import (
        TemporalAssessmentRequest,
        TemporalItem,
        TemporalTaskContext,
    )
    from fin_analyse.temporal.service import TemporalService

    item = TemporalItem(
        item_id="600519",
        title="market_snapshot:600519",
        source_scope="market_data",
        semantic_payload={
            "cache_status": "hit",
            "cache_hit": True,
            "cache_session": "realtime",
            "data_freshness": {"snapshot_at": "2026-07-06T10:30:00+08:00"},
        },
        quality_flags={"cache_hit": True, "stale_fallback": False},
    )

    request = TemporalAssessmentRequest(
        item_type="market_snapshot",
        context_mode="priority_article",
        now=_now_iso(),
        items=(item,),
        task=TemporalTaskContext(),
    )

    result = TemporalService().assess(request)
    assert "unsupported_temporal_item_context_pair" in result.data_gaps
    # Must NOT produce market snapshot assessment output
    assert result.context == {}
    assert result.content_time_sensitivity == {}
    assert result.publish_freshness == ""
    assert result.events == ()
    assert result.top_event is None
