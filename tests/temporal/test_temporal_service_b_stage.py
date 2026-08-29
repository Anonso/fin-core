"""Tests for fin_analyse/temporal/ — B1 market snapshot temporal adapter.

TDD entry points:
- test_market_snapshot_adapter_builds_supported_request
- test_market_snapshot_assessment_is_advisory_and_never_boosts_confidence
- test_market_snapshot_missing_snapshot_at_reports_data_gap
"""

from __future__ import annotations

from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sample_snapshot(**overrides) -> dict:
    return {
        "ticker": "600519",
        "cache_status": "hit",
        "cache_hit": True,
        "cache_session": "realtime",
        "data_freshness": {
            "snapshot_at": "2026-07-06T10:30:00+08:00",
            "provider": "mootdx",
            "stale_fallback": False,
            "financial_time_series": True,
        },
        "data_gaps": [],
        **overrides,
    }


# ── TDD Step 1: adapter builds supported request ──────────────────────────


def test_market_snapshot_adapter_builds_supported_request():
    """build_market_snapshot_temporal_request produces a valid request for assess()."""
    from fin_analyse.temporal.models import TemporalAssessmentRequest
    from fin_analyse.temporal.service import (
        TemporalService,
        build_market_snapshot_temporal_request,
    )

    snapshot = _sample_snapshot()
    now = _now_iso()

    request = build_market_snapshot_temporal_request(snapshot=snapshot, now=now)

    assert isinstance(request, TemporalAssessmentRequest)
    assert request.item_type == "market_snapshot"
    assert request.context_mode == "market_data_freshness"
    assert request.now == now
    assert len(request.items) == 1

    item = request.items[0]
    assert item.item_id == snapshot["ticker"]
    assert item.title == f"market_snapshot:{snapshot['ticker']}"
    assert item.source_scope == "market_data"
    assert item.observed_at == snapshot["data_freshness"]["snapshot_at"]

    # semantic_payload must carry cache/freshness metadata
    assert item.semantic_payload["cache_status"] == snapshot["cache_status"]
    assert item.semantic_payload["cache_hit"] == snapshot["cache_hit"]
    assert item.semantic_payload["cache_session"] == snapshot["cache_session"]
    assert item.semantic_payload["data_freshness"] == snapshot["data_freshness"]

    # quality_flags must carry original data_gaps and cache metadata
    assert item.quality_flags.get("cache_hit") is True
    assert item.quality_flags.get("stale_fallback") is False

    # Request must be consumable by TemporalService.assess()
    result = TemporalService().assess(request)
    assert result.context is not None
    assert result.content_time_sensitivity is not None
    # must NOT produce unsupported item/context gaps
    assert "unsupported_temporal_item_type" not in result.data_gaps
    assert "unsupported_temporal_context_mode" not in result.data_gaps


# ── TDD Step 2: assessment invariants for market snapshot ──────────────────


def test_market_snapshot_assessment_is_advisory_and_never_boosts_confidence():
    """Market snapshot assessment must be advisory-only, never boost confidence."""
    from fin_analyse.temporal.service import (
        TemporalService,
        build_market_snapshot_temporal_request,
    )

    snapshot = _sample_snapshot()
    now = _now_iso()

    request = build_market_snapshot_temporal_request(snapshot=snapshot, now=now)
    result = TemporalService().assess(request)

    # Core invariants
    assert result.confidence_modifier == 0.0
    assert result.confidence_boost_allowed is False
    assert result.advisory_only is True

    # Market snapshot specifics
    assert result.publish_freshness == ""
    assert result.events == ()
    assert result.top_event is None

    # Context must contain market data freshness metadata
    assert result.context.get("mode") == "market_data_freshness"
    assert result.context.get("ticker") == snapshot["ticker"]
    assert result.context.get("cache_status") == snapshot["cache_status"]
    assert result.context.get("cache_hit") == snapshot["cache_hit"]
    assert result.context.get("cache_session") == snapshot["cache_session"]
    assert result.context.get("data_freshness") == snapshot["data_freshness"]

    # content_time_sensitivity must be present with correct category
    cts = result.content_time_sensitivity
    assert cts.get("category") == "market_data_freshness"
    assert cts.get("source_level") == "structured_market_metadata"
    assert cts.get("freshness_driver") == "owner_supplied_data_freshness"
    assert cts.get("confidence_boost_allowed") is False

    # attention_policy must be conservative
    ap = result.attention_policy
    assert ap.get("confidence_modifier") == 0.0
    assert ap.get("confidence_boost_allowed") is False
    assert ap.get("advisory_only") is True


# ── TDD Step 3: missing snapshot_at → data_gap ─────────────────────────────


def test_market_snapshot_missing_snapshot_at_reports_data_gap():
    """When data_freshness has no snapshot_at, report market_snapshot_at_missing."""
    from fin_analyse.temporal.service import (
        TemporalService,
        build_market_snapshot_temporal_request,
    )

    snapshot = _sample_snapshot(
        data_freshness={
            "provider": "mootdx",
            "stale_fallback": True,
        },
    )
    now = _now_iso()

    request = build_market_snapshot_temporal_request(snapshot=snapshot, now=now)
    result = TemporalService().assess(request)

    assert "market_snapshot_at_missing" in result.data_gaps
    # item.observed_at should be empty string when snapshot_at is missing
    assert request.items[0].observed_at == ""

    # Still must be advisory-only
    assert result.confidence_modifier == 0.0
    assert result.confidence_boost_allowed is False
    assert result.advisory_only is True


# ── TDD Step 4: stale fallback from cache_status ───────────────────────────


def test_market_snapshot_stale_fallback_captured_from_cache_status():
    """Treat cache_status == 'stale_fallback' as stale even without data_freshness.stale_fallback."""
    from fin_analyse.temporal.service import (
        build_market_snapshot_temporal_request,
    )

    snapshot = _sample_snapshot(
        cache_status="stale_fallback",
        cache_hit=False,
        data_freshness={
            "snapshot_at": "2026-07-05T10:30:00+08:00",
            "provider": "mootdx",
            "financial_time_series": True,
            # NOTE: no stale_fallback key here
        },
    )

    request = build_market_snapshot_temporal_request(snapshot=snapshot, now=_now_iso())
    item = request.items[0]

    # quality_flags.stale_fallback must be True from cache_status alone
    assert item.quality_flags.get("stale_fallback") is True
    # cache_hit should be False (cache_status="stale_fallback" is not a hit)
    assert item.quality_flags.get("cache_hit") is False

    # semantic_payload must carry the original cache_status
    assert item.semantic_payload["cache_status"] == "stale_fallback"
