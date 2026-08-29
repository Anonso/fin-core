"""Temporal service — single-entry internal temporal assessment interface.

Current scope:
- g_source_article / priority_article delegates to TemporalInfluenceService
  and assess_time_sensitivity().
- market_snapshot / market_data_freshness, knowledge_query_window /
  knowledge_window, and dynamic_claim / dynamics_decay interpret
  owner-supplied temporal metadata without IO.
"""

from __future__ import annotations

import hashlib
import logging
import re as _re
from typing import Any

from fin_analyse.temporal.models import (
    TemporalAssessment,
    TemporalAssessmentRequest,
    TemporalItem,
    TemporalTaskContext,
)

logger = logging.getLogger(__name__)

# ── Supported types ─────────────────────────────────────────────────────────

_SUPPORTED_ITEM_TYPES = frozenset({
    "g_source_article",
    "market_snapshot",
    "knowledge_query_window",
    "dynamic_claim",
})
_SUPPORTED_CONTEXT_MODES = frozenset({
    "priority_article",
    "market_data_freshness",
    "knowledge_window",
    "dynamics_decay",
})
_SUPPORTED_PAIRS = frozenset({
    ("g_source_article", "priority_article"),
    ("market_snapshot", "market_data_freshness"),
    ("knowledge_query_window", "knowledge_window"),
    ("dynamic_claim", "dynamics_decay"),
})


class TemporalService:
    """Internal temporal assessment service — pure computation, no side effects.

    Supports article attention/content sensitivity plus owner-supplied market,
    knowledge-window, and dynamic-claim temporal metadata interpretation.
    """

    def assess(self, request: TemporalAssessmentRequest) -> TemporalAssessment:
        """Assess temporal context for the given request.

        Returns a TemporalAssessment with context, content_time_sensitivity,
        publish_freshness, events, top_event, attention_policy, and data_gaps.
        """
        data_gaps: list[str] = []

        # ── Validate item_type ──
        if request.item_type not in _SUPPORTED_ITEM_TYPES:
            data_gaps.append("unsupported_temporal_item_type")
            return TemporalAssessment(
                context={},
                content_time_sensitivity={},
                publish_freshness="",
                events=(),
                top_event=None,
                attention_policy={},
                confidence_modifier=0.0,
                confidence_boost_allowed=False,
                advisory_only=True,
                data_gaps=tuple(data_gaps),
            )

        # ── Validate context_mode ──
        if request.context_mode not in _SUPPORTED_CONTEXT_MODES:
            data_gaps.append("unsupported_temporal_context_mode")
            return TemporalAssessment(
                context={},
                content_time_sensitivity={},
                publish_freshness="",
                events=(),
                top_event=None,
                attention_policy={},
                confidence_modifier=0.0,
                confidence_boost_allowed=False,
                advisory_only=True,
                data_gaps=tuple(data_gaps),
            )

        # ── Validate supported pair ──
        if (request.item_type, request.context_mode) not in _SUPPORTED_PAIRS:
            data_gaps.append("unsupported_temporal_item_context_pair")
            return TemporalAssessment(
                context={},
                content_time_sensitivity={},
                publish_freshness="",
                events=(),
                top_event=None,
                attention_policy={},
                confidence_modifier=0.0,
                confidence_boost_allowed=False,
                advisory_only=True,
                data_gaps=tuple(data_gaps),
            )

        # ── Market snapshot branch (B1) ──
        if request.item_type == "market_snapshot":
            return _assess_market_snapshot(request, data_gaps)

        # ── Knowledge window branch (compatible) ──
        if request.item_type == "knowledge_query_window":
            return _assess_knowledge_window(request, data_gaps)

        # ── Dynamic claim branch (compatible) ──
        if request.item_type == "dynamic_claim":
            return _assess_dynamic_claim(request, data_gaps)

        # ── Validate items ──
        if not request.items:
            data_gaps.append("no_temporal_items")
            return TemporalAssessment(
                context={},
                content_time_sensitivity={},
                publish_freshness="",
                events=(),
                top_event=None,
                attention_policy={},
                confidence_modifier=0.0,
                confidence_boost_allowed=False,
                advisory_only=True,
                data_gaps=tuple(data_gaps),
            )

        # ── Build attention context via TemporalInfluenceService ──
        candidate_articles: list[dict[str, Any]] = []
        for item in request.items:
            candidate_articles.append(_item_to_candidate_article(item))

        ctx, ctx_data_gaps = _build_attention_context(
            candidate_articles=candidate_articles,
            now=request.now,
        )
        if ctx_data_gaps:
            data_gaps.extend(ctx_data_gaps)

        # ── Per-item content time sensitivity ──
        # For priority_article mode, assess the first (primary) item
        primary_item = request.items[0]
        article_dict = _item_to_article_dict(primary_item)
        deep_read_result = _item_to_deep_read_result(primary_item)

        ts_assessment = _assess_content_time_sensitivity(
            article=article_dict,
            deep_read_result=deep_read_result,
            temporal_context=ctx,
        )

        # ── Extract publish_freshness from time sensitivity ──
        publish_freshness = ts_assessment.get("publish_freshness", "")

        # ── Build attention_policy ──
        attention_policy: dict[str, Any] = {
            "freshness_driven": False,
            "content_driven": True,
            "confidence_modifier": 0.0,
            "confidence_boost_allowed": False,
            "advisory_only": True,
            "primary_driver": "deep_read_clocks_and_content_semantics",
        }

        # ── Extract events and top_event from context ──
        events = tuple(ctx.get("events", []))
        top_event = ctx.get("top_event")

        # ── Collect additional data gaps from time sensitivity ──
        for gap in ts_assessment.get("data_gaps", []):
            if gap not in data_gaps:
                data_gaps.append(gap)

        return TemporalAssessment(
            context=ctx,
            content_time_sensitivity=ts_assessment,
            publish_freshness=publish_freshness,
            events=events,
            top_event=top_event,
            attention_policy=attention_policy,
            confidence_modifier=0.0,
            confidence_boost_allowed=False,
            advisory_only=True,
            data_gaps=tuple(data_gaps),
        )


# ── Priority article adapter ─────────────────────────────────────────────────


def build_priority_article_temporal_request(
    *,
    article: dict[str, Any],
    deep_read_result: dict[str, Any],
    existing_temporal_context: dict[str, Any] | None,
    now: str,
) -> TemporalAssessmentRequest:
    """Build a TemporalAssessmentRequest from priority article runner data.

    Converts article + deep_read artifacts into a TemporalItem with
    semantic_payload carrying units/clocks/theme_clusters/evidence_chains/suggestions.
    """
    quality_flags: dict[str, Any] = {
        "deep_read_complete": deep_read_result.get("status") != "degraded"
        and article.get("deep_read_complete", True),
        "deep_read_degraded": deep_read_result.get("status") == "degraded"
        or article.get("deep_read_degraded", False),
    }

    semantic_payload: dict[str, Any] = {
        "units": deep_read_result.get("units", []),
        "clocks": deep_read_result.get("clocks", []),
        "theme_clusters": deep_read_result.get("theme_clusters", []),
        "evidence_chains": deep_read_result.get("evidence_chains", []),
        "suggestions": deep_read_result.get("suggestions", []),
    }

    # Carry forward existing temporal context as prior_context if available
    if existing_temporal_context:
        semantic_payload["prior_context"] = existing_temporal_context

    item = TemporalItem(
        item_id=str(article.get("article_id", "")),
        title=str(article.get("title", "")),
        source_scope="g_source",
        source_classification=str(article.get("source_classification", "")),
        column=str(article.get("column", "")),
        published_at=str(article.get("published_at", "")),
        semantic_payload=semantic_payload,
        quality_flags=quality_flags,
    )

    return TemporalAssessmentRequest(
        item_type="g_source_article",
        context_mode="priority_article",
        now=now,
        items=(item,),
        task=TemporalTaskContext(),
    )


# ── Market snapshot adapter (B1) ──────────────────────────────────────────────


def build_market_snapshot_temporal_request(
    *,
    snapshot: dict[str, Any],
    now: str,
) -> TemporalAssessmentRequest:
    """Build a TemporalAssessmentRequest from a market snapshot dict.

    Converts market snapshot data_freshness metadata into a TemporalItem
    with semantic_payload carrying cache_status, cache_hit, cache_session,
    and data_freshness.  Adapter only — no IO, no MarketService call.
    """
    data_freshness: dict[str, Any] = snapshot.get("data_freshness") or {}

    ticker = str(snapshot.get("ticker", ""))
    observed_at = str(data_freshness.get("snapshot_at", ""))

    # Carry forward original data_gaps from snapshot
    quality_flags: dict[str, Any] = {
        "cache_hit": bool(snapshot.get("cache_hit", False)),
        "stale_fallback": bool(
            data_freshness.get("stale_fallback", False)
            or snapshot.get("cache_status") == "stale_fallback"
        ),
    }
    original_gaps = snapshot.get("data_gaps", [])
    if isinstance(original_gaps, list):
        quality_flags["data_gaps"] = list(original_gaps)

    semantic_payload: dict[str, Any] = {
        "cache_status": str(snapshot.get("cache_status", "")),
        "cache_hit": bool(snapshot.get("cache_hit", False)),
        "cache_session": str(snapshot.get("cache_session", "")),
        "data_freshness": data_freshness,
    }

    item = TemporalItem(
        item_id=ticker,
        title=f"market_snapshot:{ticker}",
        source_scope="market_data",
        observed_at=observed_at,
        semantic_payload=semantic_payload,
        quality_flags=quality_flags,
    )

    return TemporalAssessmentRequest(
        item_type="market_snapshot",
        context_mode="market_data_freshness",
        now=now,
        items=(item,),
        task=TemporalTaskContext(),
    )


# ── Knowledge window adapter (compatible) ─────────────────────────────────────


def build_knowledge_window_temporal_request(
    *,
    query_result: dict[str, Any],
    window: str,
    now: str,
) -> TemporalAssessmentRequest:
    """Build a TemporalAssessmentRequest from a knowledge query result and window.

    Converts caller-supplied query_result dict and window into a TemporalItem
    with semantic_payload carrying query, query_mode, count, result_count, and window.
    Adapter only — no IO, no KnowledgeStore call.
    """
    query = str(query_result.get("query", ""))
    query_mode = str(query_result.get("query_mode", ""))
    count = query_result.get("count")
    result_count = query_result.get("result_count")
    generated_at = str(query_result.get("generated_at", now))

    # Derive a deterministic item_id from query text without storing raw state.
    item_id = (
        f"kw:{hashlib.blake2b(query.encode('utf-8'), digest_size=8).hexdigest()}"
        if query
        else "kw:unknown"
    )

    semantic_payload: dict[str, Any] = {
        "query": query,
        "query_mode": query_mode,
        "window": window,
        "generated_at": generated_at,
    }
    if count is not None:
        semantic_payload["count"] = count
    if result_count is not None:
        semantic_payload["result_count"] = result_count

    item = TemporalItem(
        item_id=item_id,
        title=f"knowledge_query:{query[:60]}" if query else "knowledge_query",
        source_scope="knowledge_claim",
        semantic_payload=semantic_payload,
    )

    return TemporalAssessmentRequest(
        item_type="knowledge_query_window",
        context_mode="knowledge_window",
        now=now,
        items=(item,),
        task=TemporalTaskContext(window=window),
    )


# ── Dynamic claim adapter (compatible) ────────────────────────────────────────


def build_dynamic_claim_temporal_request(
    *,
    claim: dict[str, Any],
    as_of: str,
    now: str,
) -> TemporalAssessmentRequest:
    """Build a TemporalAssessmentRequest from a dynamic claim dict.

    Converts caller-supplied claim dict into a TemporalItem with semantic_payload
    carrying claim_id, document_id, subject, claim_type, freshness, scores,
    and half-life metadata.  Adapter only — no IO, no dynamics module import.
    """
    claim_id = str(claim.get("claim_id", ""))
    document_id = str(claim.get("document_id", ""))
    subject = str(claim.get("subject", ""))
    claim_type = str(claim.get("claim_type", ""))

    observed_at = str(claim.get("observed_at") or claim.get("data_cutoff_at") or "")
    visible_at = str(claim.get("visible_at") or "")

    freshness = claim.get("freshness")
    effective_score = claim.get("effective_score")
    normalized_score = claim.get("normalized_score")

    article_tier = str(claim.get("article_tier", ""))
    half_life = claim.get("half_life")

    semantic_payload: dict[str, Any] = {
        "claim_id": claim_id,
        "document_id": document_id,
        "subject": subject,
        "claim_type": claim_type,
        "as_of": as_of,
    }
    if observed_at:
        semantic_payload["observed_at"] = observed_at
    if visible_at:
        semantic_payload["visible_at"] = visible_at
    if freshness is not None:
        semantic_payload["freshness"] = freshness
    if effective_score is not None:
        semantic_payload["effective_score"] = effective_score
    if normalized_score is not None:
        semantic_payload["normalized_score"] = normalized_score
    if article_tier:
        semantic_payload["article_tier"] = article_tier
    if half_life is not None:
        semantic_payload["half_life"] = half_life

    item = TemporalItem(
        item_id=claim_id or "dc:unknown",
        title=f"dynamic_claim:{subject[:60]}" if subject else "dynamic_claim",
        source_scope="knowledge_claim",
        observed_at=observed_at,
        semantic_payload=semantic_payload,
    )

    return TemporalAssessmentRequest(
        item_type="dynamic_claim",
        context_mode="dynamics_decay",
        now=now,
        items=(item,),
        task=TemporalTaskContext(),
    )


# ── Internal helpers ─────────────────────────────────────────────────────────


def _item_to_candidate_article(item: TemporalItem) -> dict[str, Any]:
    """Convert a TemporalItem to the dict shape expected by TemporalInfluenceService."""
    return {
        "article_id": item.item_id,
        "title": item.title,
        "column": item.column,
        "published_at": item.published_at,
        "source_classification": item.source_classification,
        "persona_eligible": bool(
            item.quality_flags.get("persona_eligible", False)
            or item.source_classification == "teacher_original"
        ),
        "deep_read_complete": item.quality_flags.get("deep_read_complete", True),
        "deep_read_degraded": item.quality_flags.get("deep_read_degraded", False),
    }


def _item_to_article_dict(item: TemporalItem) -> dict[str, Any]:
    """Convert a TemporalItem to the article dict shape expected by assess_time_sensitivity()."""
    return {
        "article_id": item.item_id,
        "title": item.title,
        "column": item.column,
        "published_at": item.published_at,
        "urgency": item.quality_flags.get("urgency", ""),
    }


def _item_to_deep_read_result(item: TemporalItem) -> dict[str, Any]:
    """Extract a deep_read_result-shaped dict from a TemporalItem's semantic_payload."""
    sp = item.semantic_payload
    return {
        "units": sp.get("units", []),
        "clocks": sp.get("clocks", []),
        "theme_clusters": sp.get("theme_clusters", []),
        "evidence_chains": sp.get("evidence_chains", []),
        "suggestions": sp.get("suggestions", []),
    }


def _build_attention_context(
    *,
    candidate_articles: list[dict[str, Any]],
    now: str,
) -> tuple[dict[str, Any], list[str]]:
    """Build attention context via TemporalInfluenceService.

    Returns (context_dict, data_gaps_list).
    """
    from fin_analyse.cognition.temporal_influence import (
        TemporalInfluenceRequest,
        TemporalInfluenceService,
    )

    data_gaps: list[str] = []
    try:
        svc = TemporalInfluenceService()
        result = svc.build_context(
            TemporalInfluenceRequest(
                candidate_articles=candidate_articles,
                now=now,
            )
        )
        ctx = result.to_dict()
        ctx_data_gaps = list(result.data_gaps) if result.data_gaps else []
        data_gaps.extend(ctx_data_gaps)
        return ctx, data_gaps
    except Exception as exc:
        logger.warning("TemporalInfluenceService.build_context() failed: %s", exc)
        data_gaps.append("temporal_influence_error")
        return {
            "events": [],
            "top_event": None,
            "advisory_only": True,
            "data_gaps": ["temporal_influence_error"],
            "generated_at": now,
        }, data_gaps


def _assess_content_time_sensitivity(
    *,
    article: dict[str, Any],
    deep_read_result: dict[str, Any],
    temporal_context: dict[str, Any],
) -> dict[str, Any]:
    """Assess content-driven time sensitivity via assess_time_sensitivity().

    Returns a dict representation of TimeSensitivityAssessment.
    """
    from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

    try:
        ts = assess_time_sensitivity(article, deep_read_result, temporal_context)
        return ts.to_dict()
    except Exception as exc:
        logger.warning("assess_time_sensitivity() failed: %s", exc)
        return {
            "category": "unknown",
            "label": "时效未知（评估异常）",
            "horizon": "unknown",
            "publish_freshness": "",
            "reason": f"时间敏感性评估异常: {exc}",
            "evidence": [],
            "confidence": 0.0,
            "data_gaps": ["time_sensitivity_error"],
            "source_level": "agent_inference",
            "quality_mode": "fallback",
        }


def _assess_market_snapshot(
    request: TemporalAssessmentRequest,
    data_gaps: list[str],
) -> TemporalAssessment:
    """Assess market snapshot data_freshness — advisory-only, no IO, no confidence boost.

    Interprets owner-supplied data_freshness metadata; does NOT call
    TemporalInfluenceService or assess_time_sensitivity().
    """
    if not request.items:
        data_gaps.append("no_temporal_items")
        return TemporalAssessment(
            context={},
            content_time_sensitivity={},
            publish_freshness="",
            events=(),
            top_event=None,
            attention_policy={},
            confidence_modifier=0.0,
            confidence_boost_allowed=False,
            advisory_only=True,
            data_gaps=tuple(data_gaps),
        )

    item = request.items[0]
    sp = item.semantic_payload
    data_freshness: dict[str, Any] = sp.get("data_freshness") or {}

    # Check for missing snapshot_at
    if not data_freshness.get("snapshot_at"):
        data_gaps.append("market_snapshot_at_missing")

    # Carry forward original data_gaps from quality_flags
    original_gaps = item.quality_flags.get("data_gaps", [])
    if isinstance(original_gaps, list):
        for gap in original_gaps:
            if gap not in data_gaps:
                data_gaps.append(gap)

    context: dict[str, Any] = {
        "mode": "market_data_freshness",
        "ticker": item.item_id,
        "cache_status": sp.get("cache_status", ""),
        "cache_hit": sp.get("cache_hit", False),
        "cache_session": sp.get("cache_session", ""),
        "data_freshness": data_freshness,
        "generated_at": request.now,
    }

    content_time_sensitivity: dict[str, Any] = {
        "category": "market_data_freshness",
        "label": "市场数据时效",
        "horizon": "market_data",
        "publish_freshness": "",
        "source_level": "structured_market_metadata",
        "quality_mode": "owner_supplied",
        "freshness_driver": "owner_supplied_data_freshness",
        "confidence_boost_allowed": False,
        "confidence": 0.0,
        "evidence": [],
        "reason": "基于调用方传入的 data_freshness 元数据解释，不做独立时效推断",
        "data_gaps": list(data_gaps),
    }

    attention_policy: dict[str, Any] = {
        "freshness_driven": True,
        "content_driven": False,
        "confidence_modifier": 0.0,
        "confidence_boost_allowed": False,
        "advisory_only": True,
        "primary_driver": "owner_supplied_market_data_freshness",
    }

    return TemporalAssessment(
        context=context,
        content_time_sensitivity=content_time_sensitivity,
        publish_freshness="",
        events=(),
        top_event=None,
        attention_policy=attention_policy,
        confidence_modifier=0.0,
        confidence_boost_allowed=False,
        advisory_only=True,
        data_gaps=tuple(data_gaps),
    )


# ── Known knowledge window patterns ────────────────────────────────────────────

_KNOWN_WINDOW_PATTERN = _re.compile(r"^(all|\d+d)$")


def _is_known_window(window: str) -> bool:
    """Check whether a window string matches known knowledge retrieval windows."""
    if not window:
        return False
    return bool(_KNOWN_WINDOW_PATTERN.match(window))


def _window_days(window: str) -> int | None:
    """Extract day count from a window string like '180d'; None for 'all' or unknown."""
    if not window or window == "all":
        return None
    m = _re.match(r"^(\d+)d$", window)
    if m:
        return int(m.group(1))
    return None


# ── Knowledge window assessment ────────────────────────────────────────────────


def _assess_knowledge_window(
    request: TemporalAssessmentRequest,
    data_gaps: list[str],
) -> TemporalAssessment:
    """Assess knowledge query window — advisory-only, no IO, no confidence boost.

    Interprets caller-supplied query_result metadata and window semantics;
    does NOT call KnowledgeStore or any IO service.
    """
    if not request.items:
        data_gaps.append("no_temporal_items")
        return TemporalAssessment(
            context={},
            content_time_sensitivity={},
            publish_freshness="",
            events=(),
            top_event=None,
            attention_policy={},
            confidence_modifier=0.0,
            confidence_boost_allowed=False,
            advisory_only=True,
            data_gaps=tuple(data_gaps),
        )

    item = request.items[0]
    sp = item.semantic_payload

    query = str(sp.get("query", ""))
    query_mode = str(sp.get("query_mode", ""))
    window = str(sp.get("window", ""))
    count = sp.get("count")
    result_count = sp.get("result_count")
    generated_at = str(sp.get("generated_at", request.now))

    # ── Window validation ──
    if not window:
        data_gaps.append("knowledge_window_missing")
    elif not _is_known_window(window):
        data_gaps.append("knowledge_window_unknown_defaulted")

    wd = _window_days(window)

    context: dict[str, Any] = {
        "mode": "knowledge_window",
        "query": query,
        "query_mode": query_mode,
        "window": window,
        "generated_at": generated_at,
    }
    if wd is not None:
        context["window_days"] = wd
    if count is not None:
        context["count"] = count
    if result_count is not None:
        context["result_count"] = result_count

    content_time_sensitivity: dict[str, Any] = {
        "category": "knowledge_window",
        "label": "知识检索窗口",
        "horizon": "knowledge_retrieval",
        "publish_freshness": "",
        "source_level": "structured_knowledge_query_metadata",
        "quality_mode": "owner_supplied",
        "window_is_retrieval_constraint": True,
        "confidence_boost_allowed": False,
        "confidence": 0.0,
        "evidence": [],
        "reason": "检索窗口控制查询范围，不代表内容在此窗口外失效",
        "data_gaps": list(data_gaps),
    }

    attention_policy: dict[str, Any] = {
        "freshness_driven": False,
        "content_driven": False,
        "confidence_modifier": 0.0,
        "confidence_boost_allowed": False,
        "advisory_only": True,
        "primary_driver": "caller_supplied_knowledge_window",
    }

    return TemporalAssessment(
        context=context,
        content_time_sensitivity=content_time_sensitivity,
        publish_freshness="",
        events=(),
        top_event=None,
        attention_policy=attention_policy,
        confidence_modifier=0.0,
        confidence_boost_allowed=False,
        advisory_only=True,
        data_gaps=tuple(data_gaps),
    )


# ── Dynamic claim assessment ───────────────────────────────────────────────────


def _assess_dynamic_claim(
    request: TemporalAssessmentRequest,
    data_gaps: list[str],
) -> TemporalAssessment:
    """Assess dynamic claim decay — advisory-only, no IO, no confidence boost.

    Interprets caller-supplied claim metadata including freshness and scores;
    does NOT call dynamics decay/scoring functions or any IO service.
    """
    if not request.items:
        data_gaps.append("no_temporal_items")
        return TemporalAssessment(
            context={},
            content_time_sensitivity={},
            publish_freshness="",
            events=(),
            top_event=None,
            attention_policy={},
            confidence_modifier=0.0,
            confidence_boost_allowed=False,
            advisory_only=True,
            data_gaps=tuple(data_gaps),
        )

    item = request.items[0]
    sp = item.semantic_payload

    claim_id = str(sp.get("claim_id", ""))
    document_id = str(sp.get("document_id", ""))
    subject = str(sp.get("subject", ""))
    claim_type = str(sp.get("claim_type", ""))
    as_of = str(sp.get("as_of", request.now))

    freshness = sp.get("freshness")
    effective_score = sp.get("effective_score")
    normalized_score = sp.get("normalized_score")
    article_tier = str(sp.get("article_tier", ""))
    half_life = sp.get("half_life")
    observed_at = str(sp.get("observed_at", ""))
    visible_at = str(sp.get("visible_at", ""))

    # ── Freshness validation ──
    if freshness is None:
        data_gaps.append("dynamic_claim_freshness_missing")

    context: dict[str, Any] = {
        "mode": "dynamics_decay",
        "claim_id": claim_id,
        "document_id": document_id,
        "subject": subject,
        "claim_type": claim_type,
        "as_of": as_of,
        "generated_at": request.now,
    }
    if freshness is not None:
        context["freshness"] = freshness
    if effective_score is not None:
        context["effective_score"] = effective_score
    if normalized_score is not None:
        context["normalized_score"] = normalized_score
    if article_tier:
        context["article_tier"] = article_tier
    if half_life is not None:
        context["half_life"] = half_life
    if observed_at:
        context["observed_at"] = observed_at
    if visible_at:
        context["visible_at"] = visible_at

    content_time_sensitivity: dict[str, Any] = {
        "category": "dynamics_decay",
        "label": "观点衰减",
        "horizon": "dynamic_claim",
        "publish_freshness": "",
        "source_level": "structured_dynamic_claim_metadata",
        "quality_mode": "owner_supplied",
        "freshness_driver": "owner_computed_decay",
        "scoring_behavior": "owner_preserved",
        "confidence_boost_allowed": False,
        "confidence": 0.0,
        "evidence": [],
        "reason": "基于调用方传入的衰减元数据解释，不做独立衰减计算",
        "data_gaps": list(data_gaps),
    }

    attention_policy: dict[str, Any] = {
        "freshness_driven": True,
        "content_driven": False,
        "confidence_modifier": 0.0,
        "confidence_boost_allowed": False,
        "advisory_only": True,
        "primary_driver": "owner_computed_dynamic_claim_decay",
    }

    return TemporalAssessment(
        context=context,
        content_time_sensitivity=content_time_sensitivity,
        publish_freshness="",
        events=(),
        top_event=None,
        attention_policy=attention_policy,
        confidence_modifier=0.0,
        confidence_boost_allowed=False,
        advisory_only=True,
        data_gaps=tuple(data_gaps),
    )
