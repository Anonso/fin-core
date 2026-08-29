"""Tests for knowledge window and dynamics decay compatible adapters.

TDD entry points:
- test_knowledge_window_adapter_builds_supported_request
- test_knowledge_assessment_advisory_only_and_identifies_retrieval_window_semantics
- test_knowledge_missing_or_unknown_window_data_gaps
- test_dynamic_claim_adapter_builds_supported_request
- test_dynamic_assessment_advisory_only_and_preserves_owner_score_metadata
- test_dynamic_missing_freshness_data_gap
- test_invalid_known_name_pairs_return_unsupported_temporal_item_context_pair
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── Knowledge Window Adapter ───────────────────────────────────────────────────


def test_knowledge_window_adapter_builds_supported_request():
    """build_knowledge_window_temporal_request produces a valid request for assess()."""
    from fin_analyse.temporal.models import TemporalAssessmentRequest
    from fin_analyse.temporal.service import (
        TemporalService,
        build_knowledge_window_temporal_request,
    )

    query_result = {
        "query": "锑矿供需",
        "query_mode": "semantic",
        "count": 12,
        "result_count": 8,
        "generated_at": _now_iso(),
    }
    window = "180d"
    now = _now_iso()

    request = build_knowledge_window_temporal_request(
        query_result=query_result,
        window=window,
        now=now,
    )

    assert isinstance(request, TemporalAssessmentRequest)
    assert request.item_type == "knowledge_query_window"
    assert request.context_mode == "knowledge_window"
    assert request.now == now
    assert len(request.items) == 1

    item = request.items[0]
    expected_query_id = hashlib.blake2b(
        query_result["query"].encode("utf-8"),
        digest_size=8,
    ).hexdigest()
    assert item.item_id == f"kw:{expected_query_id}"
    assert item.source_scope == "knowledge_claim"
    # semantic_payload must carry query metadata
    assert "query" in item.semantic_payload
    assert item.semantic_payload["query"] == query_result["query"]
    assert item.semantic_payload["query_mode"] == query_result["query_mode"]
    assert item.semantic_payload["count"] == query_result["count"]
    assert item.semantic_payload["result_count"] == query_result["result_count"]
    assert item.semantic_payload["window"] == window

    # Request must be consumable by TemporalService.assess()
    result = TemporalService().assess(request)
    assert result.context is not None
    assert result.content_time_sensitivity is not None
    assert "unsupported_temporal" not in str(result.data_gaps)


def test_knowledge_assessment_advisory_only_and_identifies_retrieval_window_semantics():
    """Knowledge window assessment must be advisory-only and identify retrieval window semantics."""
    from fin_analyse.temporal.service import (
        TemporalService,
        build_knowledge_window_temporal_request,
    )

    query_result = {
        "query": "光伏产业链",
        "query_mode": "hybrid",
        "count": 20,
        "result_count": 15,
        "generated_at": _now_iso(),
    }
    window = "90d"
    now = _now_iso()

    request = build_knowledge_window_temporal_request(
        query_result=query_result,
        window=window,
        now=now,
    )

    result = TemporalService().assess(request)

    # Advisory-only invariants
    assert result.confidence_modifier == 0.0
    assert result.confidence_boost_allowed is False
    assert result.advisory_only is True

    # Content time sensitivity must identify knowledge window semantics
    cts = result.content_time_sensitivity
    assert cts.get("category") == "knowledge_window"
    assert cts.get("source_level") == "structured_knowledge_query_metadata"
    assert cts.get("window_is_retrieval_constraint") is True
    assert cts.get("confidence_boost_allowed") is False

    # Context must preserve query metadata
    ctx = result.context
    assert ctx.get("query") == query_result["query"]
    assert ctx.get("query_mode") == query_result["query_mode"]
    assert ctx.get("window") == window
    assert ctx.get("result_count") == query_result["result_count"]


def test_knowledge_missing_or_unknown_window_data_gaps():
    """Missing or unknown window must produce controlled data_gaps."""
    from fin_analyse.temporal.service import (
        TemporalService,
        build_knowledge_window_temporal_request,
    )

    query_result = {
        "query": "新能源",
        "query_mode": "semantic",
        "count": 5,
        "result_count": 3,
    }
    now = _now_iso()

    # ── Missing window ──
    request_missing = build_knowledge_window_temporal_request(
        query_result=query_result,
        window="",
        now=now,
    )
    result_missing = TemporalService().assess(request_missing)
    assert "knowledge_window_missing" in result_missing.data_gaps
    # Still advisory-only
    assert result_missing.confidence_modifier == 0.0
    assert result_missing.confidence_boost_allowed is False

    # ── Unknown window ──
    request_unknown = build_knowledge_window_temporal_request(
        query_result=query_result,
        window="unknown_value_not_in_known_set",
        now=now,
    )
    result_unknown = TemporalService().assess(request_unknown)
    assert "knowledge_window_unknown_defaulted" in result_unknown.data_gaps
    # Still advisory-only
    assert result_unknown.confidence_modifier == 0.0
    assert result_unknown.confidence_boost_allowed is False


# ── Dynamics Decay Adapter ─────────────────────────────────────────────────────


def test_dynamic_claim_adapter_builds_supported_request():
    """build_dynamic_claim_temporal_request produces a valid request for assess()."""
    from fin_analyse.temporal.models import TemporalAssessmentRequest
    from fin_analyse.temporal.service import (
        TemporalService,
        build_dynamic_claim_temporal_request,
    )

    claim = {
        "claim_id": "claim-001",
        "document_id": "doc-abc",
        "subject": "锑价走势",
        "claim_type": "price_direction",
        "observed_at": "2026-07-01T10:00:00+08:00",
        "data_cutoff_at": "2026-07-01T10:00:00+08:00",
        "visible_at": "2026-07-01T12:00:00+08:00",
        "freshness": 0.72,
        "effective_score": 0.85,
        "normalized_score": 0.78,
        "article_tier": "teacher_original",
        "half_life": 14.0,
    }
    as_of = "2026-07-06T10:00:00Z"
    now = _now_iso()

    request = build_dynamic_claim_temporal_request(
        claim=claim,
        as_of=as_of,
        now=now,
    )

    assert isinstance(request, TemporalAssessmentRequest)
    assert request.item_type == "dynamic_claim"
    assert request.context_mode == "dynamics_decay"
    assert request.now == now
    assert len(request.items) == 1

    item = request.items[0]
    assert item.item_id == claim["claim_id"]
    assert item.source_scope == "knowledge_claim"
    # semantic_payload must carry claim metadata
    sp = item.semantic_payload
    assert sp["claim_id"] == claim["claim_id"]
    assert sp["document_id"] == claim["document_id"]
    assert sp["subject"] == claim["subject"]
    assert sp["claim_type"] == claim["claim_type"]
    assert sp["freshness"] == claim["freshness"]
    assert sp["effective_score"] == claim["effective_score"]
    assert sp["normalized_score"] == claim["normalized_score"]

    # Request must be consumable by TemporalService.assess()
    result = TemporalService().assess(request)
    assert result.context is not None
    assert result.content_time_sensitivity is not None
    assert "unsupported_temporal" not in str(result.data_gaps)


def test_dynamic_assessment_advisory_only_and_preserves_owner_score_metadata():
    """Dynamic claim assessment must be advisory-only and preserve owner score metadata."""
    from fin_analyse.temporal.service import (
        TemporalService,
        build_dynamic_claim_temporal_request,
    )

    claim = {
        "claim_id": "claim-002",
        "document_id": "doc-def",
        "subject": "碳酸锂供需",
        "claim_type": "supply_demand",
        "observed_at": "2026-07-03T08:00:00+08:00",
        "freshness": 0.45,
        "effective_score": 0.62,
        "normalized_score": 0.58,
        "article_tier": "general_analysis",
        "half_life": 7.0,
    }
    as_of = "2026-07-06T10:00:00Z"
    now = _now_iso()

    request = build_dynamic_claim_temporal_request(
        claim=claim,
        as_of=as_of,
        now=now,
    )

    result = TemporalService().assess(request)

    # Advisory-only invariants
    assert result.confidence_modifier == 0.0
    assert result.confidence_boost_allowed is False
    assert result.advisory_only is True

    # Content time sensitivity must identify dynamics decay semantics
    cts = result.content_time_sensitivity
    assert cts.get("category") == "dynamics_decay"
    assert cts.get("source_level") == "structured_dynamic_claim_metadata"
    assert cts.get("freshness_driver") == "owner_computed_decay"
    assert cts.get("scoring_behavior") == "owner_preserved"
    assert cts.get("confidence_boost_allowed") is False

    # Context must preserve claim metadata
    ctx = result.context
    assert ctx.get("claim_id") == claim["claim_id"]
    assert ctx.get("document_id") == claim["document_id"]
    assert ctx.get("subject") == claim["subject"]
    assert ctx.get("claim_type") == claim["claim_type"]
    assert ctx.get("freshness") == claim["freshness"]
    assert ctx.get("effective_score") == claim["effective_score"]
    assert ctx.get("normalized_score") == claim["normalized_score"]
    assert ctx.get("as_of") == as_of

    # publish_freshness must be empty for dynamics decay (not article publish)
    assert result.publish_freshness == ""


def test_dynamic_missing_freshness_data_gap():
    """Missing freshness must produce dynamic_claim_freshness_missing data_gap."""
    from fin_analyse.temporal.service import (
        TemporalService,
        build_dynamic_claim_temporal_request,
    )

    claim = {
        "claim_id": "claim-003",
        "document_id": "doc-ghi",
        "subject": "稀土政策",
        "claim_type": "policy_analysis",
        # No freshness field
        "effective_score": 0.55,
        "normalized_score": 0.50,
    }
    as_of = "2026-07-06T10:00:00Z"
    now = _now_iso()

    request = build_dynamic_claim_temporal_request(
        claim=claim,
        as_of=as_of,
        now=now,
    )

    result = TemporalService().assess(request)
    assert "dynamic_claim_freshness_missing" in result.data_gaps
    # Still advisory-only
    assert result.confidence_modifier == 0.0
    assert result.confidence_boost_allowed is False


# ── Invalid Pair Validation ────────────────────────────────────────────────────


def test_invalid_known_name_pairs_return_unsupported_temporal_item_context_pair():
    """Known item_type + context_mode but invalid combination must return pair gap."""
    from fin_analyse.temporal.models import (
        TemporalAssessmentRequest,
        TemporalItem,
        TemporalTaskContext,
    )
    from fin_analyse.temporal.service import TemporalService

    now = _now_iso()

    # knowledge_query_window + priority_article (wrong mode)
    item_kw = TemporalItem(
        item_id="kw-001",
        title="knowledge query",
        source_scope="knowledge_claim",
        semantic_payload={"query": "test", "window": "180d"},
    )
    req1 = TemporalAssessmentRequest(
        item_type="knowledge_query_window",
        context_mode="priority_article",
        now=now,
        items=(item_kw,),
        task=TemporalTaskContext(),
    )
    result1 = TemporalService().assess(req1)
    assert "unsupported_temporal_item_context_pair" in result1.data_gaps
    assert result1.context == {}

    # knowledge_query_window + market_data_freshness (wrong mode)
    req2 = TemporalAssessmentRequest(
        item_type="knowledge_query_window",
        context_mode="market_data_freshness",
        now=now,
        items=(item_kw,),
        task=TemporalTaskContext(),
    )
    result2 = TemporalService().assess(req2)
    assert "unsupported_temporal_item_context_pair" in result2.data_gaps

    # knowledge_query_window + dynamics_decay (wrong mode)
    req3 = TemporalAssessmentRequest(
        item_type="knowledge_query_window",
        context_mode="dynamics_decay",
        now=now,
        items=(item_kw,),
        task=TemporalTaskContext(),
    )
    result3 = TemporalService().assess(req3)
    assert "unsupported_temporal_item_context_pair" in result3.data_gaps

    # dynamic_claim + priority_article (wrong mode)
    item_dc = TemporalItem(
        item_id="dc-001",
        title="dynamic claim",
        source_scope="knowledge_claim",
        semantic_payload={"claim_id": "c1", "freshness": 0.5},
    )
    req4 = TemporalAssessmentRequest(
        item_type="dynamic_claim",
        context_mode="priority_article",
        now=now,
        items=(item_dc,),
        task=TemporalTaskContext(),
    )
    result4 = TemporalService().assess(req4)
    assert "unsupported_temporal_item_context_pair" in result4.data_gaps

    # dynamic_claim + market_data_freshness (wrong mode)
    req5 = TemporalAssessmentRequest(
        item_type="dynamic_claim",
        context_mode="market_data_freshness",
        now=now,
        items=(item_dc,),
        task=TemporalTaskContext(),
    )
    result5 = TemporalService().assess(req5)
    assert "unsupported_temporal_item_context_pair" in result5.data_gaps

    # dynamic_claim + knowledge_window (wrong mode)
    req6 = TemporalAssessmentRequest(
        item_type="dynamic_claim",
        context_mode="knowledge_window",
        now=now,
        items=(item_dc,),
        task=TemporalTaskContext(),
    )
    result6 = TemporalService().assess(req6)
    assert "unsupported_temporal_item_context_pair" in result6.data_gaps

    # g_source_article + dynamics_decay (wrong mode)
    item_gs = TemporalItem(
        item_id="art-001",
        title="test article",
        source_scope="g_source",
        source_classification="teacher_original",
        column="星大派特刊",
        published_at=now,
        semantic_payload={"units": [], "clocks": []},
        quality_flags={"deep_read_complete": True, "deep_read_degraded": False},
    )
    req7 = TemporalAssessmentRequest(
        item_type="g_source_article",
        context_mode="dynamics_decay",
        now=now,
        items=(item_gs,),
        task=TemporalTaskContext(),
    )
    result7 = TemporalService().assess(req7)
    assert "unsupported_temporal_item_context_pair" in result7.data_gaps

    # market_snapshot + dynamics_decay (wrong mode)
    item_ms = TemporalItem(
        item_id="600519",
        title="market_snapshot:600519",
        source_scope="market_data",
        semantic_payload={
            "cache_status": "hit",
            "data_freshness": {"snapshot_at": now},
        },
        quality_flags={"cache_hit": True, "stale_fallback": False},
    )
    req8 = TemporalAssessmentRequest(
        item_type="market_snapshot",
        context_mode="dynamics_decay",
        now=now,
        items=(item_ms,),
        task=TemporalTaskContext(),
    )
    result8 = TemporalService().assess(req8)
    assert "unsupported_temporal_item_context_pair" in result8.data_gaps


# ── Existing B1 compatibility ──────────────────────────────────────────────────


def test_existing_b1_market_snapshot_adapter_still_works():
    """B1 market_snapshot / market_data_freshness must continue to work."""
    from fin_analyse.temporal.service import (
        TemporalService,
        build_market_snapshot_temporal_request,
    )

    snapshot = {
        "ticker": "600519",
        "cache_status": "hit",
        "cache_hit": True,
        "cache_session": "realtime",
        "data_freshness": {
            "snapshot_at": "2026-07-06T10:30:00+08:00",
            "stale_fallback": False,
            "stale_strategy": "none",
        },
        "data_gaps": [],
    }
    now = _now_iso()

    request = build_market_snapshot_temporal_request(snapshot=snapshot, now=now)
    result = TemporalService().assess(request)

    assert "unsupported_temporal" not in str(result.data_gaps)
    cts = result.content_time_sensitivity
    assert cts.get("category") == "market_data_freshness"
    assert cts.get("freshness_driver") == "owner_supplied_data_freshness"
    assert result.confidence_modifier == 0.0
    assert result.confidence_boost_allowed is False
    assert result.advisory_only is True


def test_existing_b1_priority_article_adapter_still_works():
    """B1 priority_article adapter must continue to work."""
    from datetime import timedelta

    from fin_analyse.temporal.service import (
        TemporalService,
        build_priority_article_temporal_request,
    )

    article = {
        "article_id": "art-b1-check",
        "title": "B1 compatibility check",
        "column": "星大派特刊",
        "urgency": "T0",
        "published_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        "source_classification": "teacher_original",
        "persona_eligible": True,
        "deep_read_complete": True,
        "deep_read_degraded": False,
    }
    dr = {
        "units": [{"title": "test", "content": "test"}],
        "clocks": [{"label": "intraday_event", "confidence": 0.9}],
        "theme_clusters": [],
        "evidence_chains": [],
        "suggestions": [],
        "status": "ok",
    }
    now = _now_iso()

    request = build_priority_article_temporal_request(
        article=article,
        deep_read_result=dr,
        existing_temporal_context=None,
        now=now,
    )
    result = TemporalService().assess(request)

    assert "unsupported_temporal" not in str(result.data_gaps)
    assert result.context is not None
    assert result.content_time_sensitivity is not None
    assert result.confidence_modifier == 0.0
    assert result.confidence_boost_allowed is False
    assert result.advisory_only is True
