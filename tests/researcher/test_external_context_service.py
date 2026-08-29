"""Cache-only external research evidence contract.

This seam may project previously collected external evidence.  It must never
start an LLM, a tool loop, a live provider call, or a second generic Agent.
"""

from __future__ import annotations

import time
from dataclasses import fields

from fin_analyse.researcher.context import (
    ExternalResearchContextRequest,
    ExternalResearchContextResult,
    ExternalResearchContextService,
)
from fin_analyse.researcher.stage_trace import StageTrace


def _request(**overrides: object) -> ExternalResearchContextRequest:
    values: dict[str, object] = {
        "company": "测试公司",
        "ticker": "000001",
        "question": "库存如何",
        "dimensions": ["供给", "库存"],
    }
    values.update(overrides)
    return ExternalResearchContextRequest(**values)  # type: ignore[arg-type]


def _cached_payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "short_answer": "库存处于低位。",
        "executive_summary": "缓存的外部研究摘要。",
        "confidence": 0.72,
        "caveats": ["仅作外部参考"],
        "coverage": {
            "can_support_agent_view": True,
            "can_raise_confidence": True,
            "can_drive_recommendation": True,
            "private_raw_payload": "must not leak",
        },
    }
    values.update(overrides)
    return values


def test_request_has_no_legacy_depth_or_live_mode_axis() -> None:
    names = {item.name for item in fields(ExternalResearchContextRequest)}

    assert "depth" not in names
    assert "researcher_depth" not in names
    assert "external_research_mode" not in names


def test_cache_miss_fails_closed_without_live_fallback() -> None:
    service = ExternalResearchContextService()

    result = service.collect(_request())

    assert result.status == "skipped"
    assert result.cache_hit is False
    assert result.short_answer == ""
    assert result.executive_summary == ""
    assert result.data_gaps == ["external_research_cache_missing"]
    assert result.error is None


def test_cache_hit_projects_bounded_non_g_evidence() -> None:
    cache: dict[str, tuple[float, object]] = {}
    service = ExternalResearchContextService(cache=cache)
    request = _request()
    cache[service.cache_key_for(request)] = (time.monotonic(), _cached_payload())

    result = service.collect(request)

    assert result.status == "ok"
    assert result.cache_hit is True
    assert result.short_answer == "库存处于低位。"
    assert result.executive_summary == "缓存的外部研究摘要。"
    assert result.confidence == 0.72
    assert result.caveats == ["仅作外部参考"]
    assert result.coverage == {
        "evidence_role": "external_context",
        "quality_tier": "quick_scan",
        "can_support_agent_view": False,
        "can_raise_confidence": False,
        "can_drive_recommendation": False,
    }


def test_expired_or_malformed_cache_entry_fails_closed() -> None:
    cache: dict[str, tuple[float, object]] = {}
    service = ExternalResearchContextService(cache=cache, cache_ttl_seconds=60)
    request = _request()
    key = service.cache_key_for(request)
    cache[key] = (time.monotonic() - 61, _cached_payload())

    expired = service.collect(request)
    cache[key] = (time.monotonic(), "not-a-mapping")
    malformed = service.collect(request)

    assert expired.status == "skipped"
    assert expired.data_gaps == ["external_research_cache_expired"]
    assert malformed.status == "skipped"
    assert malformed.data_gaps == ["external_research_cache_invalid"]


def test_cache_key_is_semantic_and_has_no_depth_axis() -> None:
    service = ExternalResearchContextService()

    assert service.cache_key_for(_request(question="q1")) != service.cache_key_for(
        _request(question="q2")
    )
    assert service.cache_key_for(_request(question="same")) == service.cache_key_for(
        _request(question="same")
    )


def test_result_stage_trace_keeps_external_reference_boundaries() -> None:
    result = ExternalResearchContextResult(
        status="ok",
        confidence=0.8,
        cache_hit=True,
        latency_ms=7,
        data_gaps=["source_date_missing"],
        coverage={"can_raise_confidence": True, "private": "drop"},
    )

    trace = result.to_stage_trace()

    assert isinstance(trace, StageTrace)
    assert trace.stage == "external_research"
    assert trace.source_type == "web"
    assert trace.cache_hit is True
    assert trace.latency_ms == 7
    assert trace.data_gaps == ["source_date_missing"]
    assert trace.coverage == {
        "evidence_role": "external_context",
        "quality_tier": "quick_scan",
        "can_support_agent_view": False,
        "can_raise_confidence": False,
        "can_drive_recommendation": False,
    }


def test_provider_degradation_is_additive_and_cannot_change_boundaries() -> None:
    from fin_analyse.runtime.provider_health import (
        ProviderHealthResult,
        ProviderRuntimeStatus,
    )

    health = ProviderHealthResult(
        statuses=[
            ProviderRuntimeStatus(
                category="external_research_provider",
                provider_name="evidence_store",
                status="degraded",
                reason="stale",
            )
        ]
    )
    cache: dict[str, tuple[float, object]] = {}
    service = ExternalResearchContextService(cache=cache)
    request = _request(provider_health=health)
    cache[service.cache_key_for(request)] = (time.monotonic(), _cached_payload())

    result = service.collect(request)
    trace = result.to_stage_trace()

    assert result.status == "ok"
    assert result.provider_degradation["consumer"] == "external_research_context"
    assert trace.coverage["provider_degradation"]["consumer"] == ("external_research_context")
    assert trace.coverage["can_support_agent_view"] is False
    assert trace.coverage["can_raise_confidence"] is False
    assert trace.coverage["can_drive_recommendation"] is False
