"""Tests for ProviderDegradationPolicy — provider health → consumer-specific degradation decisions."""

from __future__ import annotations

import pytest

from fin_analyse.runtime.provider_degradation_policy import (
    ProviderDegradationPolicy,
    ProviderHealthConsumer,
)
from fin_analyse.runtime.provider_health import (
    ProviderHealthResult,
    ProviderRuntimeStatus,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _health_with_statuses(*statuses: ProviderRuntimeStatus) -> ProviderHealthResult:
    """Build a ProviderHealthResult from statuses, computing summary/warnings/gaps."""
    sanitized_warnings: list[str] = []
    data_gaps: list[str] = []
    for s in statuses:
        sanitized_warnings.extend(s.warnings)
        data_gaps.extend(s.data_gaps)
    return ProviderHealthResult(
        statuses=list(statuses),
        summary={},
        sanitized_warnings=sanitized_warnings,
        data_gaps=data_gaps,
    )


def _market_status(
    provider_name: str,
    status: str = "healthy",
    reason: str = "",
    warnings: list[str] | None = None,
    data_gaps: list[str] | None = None,
) -> ProviderRuntimeStatus:
    return ProviderRuntimeStatus(
        category="market_provider",
        provider_name=provider_name,
        status=status,
        reason=reason or f"test {status}",
        warnings=warnings or [],
        data_gaps=data_gaps or [],
    )


def _llm_status(
    provider_name: str,
    status: str = "healthy",
    reason: str = "",
    warnings: list[str] | None = None,
    data_gaps: list[str] | None = None,
) -> ProviderRuntimeStatus:
    return ProviderRuntimeStatus(
        category="llm_backend",
        provider_name=provider_name,
        status=status,
        reason=reason or f"test {status}",
        warnings=warnings or [],
        data_gaps=data_gaps or [],
    )


def _ext_status(
    provider_name: str = "researcher",
    status: str = "healthy",
    reason: str = "",
    warnings: list[str] | None = None,
    data_gaps: list[str] | None = None,
) -> ProviderRuntimeStatus:
    return ProviderRuntimeStatus(
        category="external_research_provider",
        provider_name=provider_name,
        status=status,
        reason=reason or f"test {status}",
        warnings=warnings or [],
        data_gaps=data_gaps or [],
    )


# ── RED: first failing test — market snapshot unavailable provider ───────────


class TestMarketSnapshotUnavailableProviderRecommendsCacheFallback:
    """TDD starting point: unavailable market provider → cache fallback recommendation."""

    def test_market_snapshot_unavailable_provider_recommends_cache_fallback_without_routing_change(
        self,
    ):
        """Unavailable market provider → prefer_cache_or_stale_fallback, routing unchanged."""
        health = _health_with_statuses(
            _market_status("eastmoney", status="unavailable", reason="circuit_open"),
            _market_status("baostock", status="healthy"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")

        assert decision.consumer == "market_snapshot"
        assert decision.status == "degraded"  # one unavailable, one healthy → degraded
        assert decision.fallback_recommendation == "prefer_cache_or_stale_fallback"
        assert "eastmoney" in decision.unavailable_providers
        assert "baostock" not in decision.unavailable_providers
        assert decision.routing_changed is False
        assert decision.engineering_quality_only is True
        assert decision.advisory_only is True
        assert decision.investment_evidence is False
        assert decision.affects_confidence is False
        assert decision.writes_cognition is False
        assert decision.trading_decision is False
        assert decision.execution_allowed is False


# ── Status priority ──────────────────────────────────────────────────────────


class TestStatusPriority:
    """unavailable > degraded > unknown > healthy."""

    def test_all_healthy_produces_healthy(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="healthy"),
            _market_status("baostock", status="healthy"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.status == "healthy"
        assert decision.fallback_recommendation == "none"
        assert decision.unavailable_providers == []
        assert decision.degraded_providers == []

    def test_unavailable_trumps_degraded(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="unavailable"),
            _market_status("baostock", status="degraded"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.status == "unavailable"

    def test_degraded_trumps_unknown(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="degraded"),
            _market_status("baostock", status="unknown"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.status == "degraded"

    def test_unknown_trumps_healthy(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="unknown"),
            _market_status("baostock", status="healthy"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.status == "degraded"  # mixed with unknown → degraded

    def test_all_unavailable_produces_unavailable(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="unavailable"),
            _market_status("baostock", status="unavailable"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.status == "unavailable"
        assert len(decision.unavailable_providers) == 2


# ── Consumer category mapping ────────────────────────────────────────────────


class TestConsumerCategoryMapping:
    """Each consumer maps to the correct provider category."""

    def test_market_snapshot_filters_market_provider_only(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="degraded"),
            _llm_status("gpt5", status="unavailable"),
            _ext_status("researcher", status="unavailable"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        # Only market provider statuses should be considered
        assert decision.status == "degraded"  # market is degraded, ignoring llm/ext unavailable
        assert decision.fallback_recommendation == "prefer_cache_or_stale_fallback"

    def test_external_research_context_filters_external_research_only(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="unavailable"),
            _llm_status("gpt5", status="unavailable"),
            _ext_status("researcher", status="degraded", warnings=["ext degraded"]),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="external_research_context")
        assert decision.status == "degraded"
        assert (
            decision.fallback_recommendation == "prefer_cache_first_or_mark_external_research_gap"
        )

    def test_gateway_runtime_display_sees_all_categories(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="degraded"),
            _llm_status("gpt5", status="unavailable"),
            _ext_status("researcher", status="unknown"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="gateway_runtime_display")
        assert decision.status == "unavailable"  # gpt5 is unavailable across all cats
        # Should include providers from all categories
        assert "gpt5" in decision.unavailable_providers
        assert "eastmoney" in (decision.degraded_providers + decision.unavailable_providers)


# ── Fallback recommendations ─────────────────────────────────────────────────


class TestFallbackRecommendations:
    """Consumer-specific fallback recommendations."""

    def test_healthy_market_gives_none(self):
        health = _health_with_statuses(_market_status("eastmoney", status="healthy"))
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.fallback_recommendation == "none"

    def test_degraded_market_gives_cache_fallback(self):
        health = _health_with_statuses(_market_status("eastmoney", status="degraded"))
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.fallback_recommendation == "prefer_cache_or_stale_fallback"

    def test_unavailable_market_gives_cache_fallback(self):
        health = _health_with_statuses(_market_status("eastmoney", status="unavailable"))
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.fallback_recommendation == "prefer_cache_or_stale_fallback"

    def test_degraded_external_research_gives_cache_first(self):
        health = _health_with_statuses(_ext_status("researcher", status="degraded"))
        decision = ProviderDegradationPolicy.evaluate(health, consumer="external_research_context")
        assert (
            decision.fallback_recommendation == "prefer_cache_first_or_mark_external_research_gap"
        )

    def test_unavailable_external_research_gives_cache_first(self):
        health = _health_with_statuses(_ext_status("researcher", status="unavailable"))
        decision = ProviderDegradationPolicy.evaluate(health, consumer="external_research_context")
        assert (
            decision.fallback_recommendation == "prefer_cache_first_or_mark_external_research_gap"
        )

    def test_unknown_external_research_gives_cache_first(self):
        health = _health_with_statuses(_ext_status("researcher", status="unknown"))
        decision = ProviderDegradationPolicy.evaluate(health, consumer="external_research_context")
        assert (
            decision.fallback_recommendation == "prefer_cache_first_or_mark_external_research_gap"
        )

    def test_healthy_external_research_gives_none(self):
        health = _health_with_statuses(_ext_status("researcher", status="healthy"))
        decision = ProviderDegradationPolicy.evaluate(health, consumer="external_research_context")
        assert decision.fallback_recommendation == "none"


# ── Boundary flags ───────────────────────────────────────────────────────────


class TestBoundaryFlags:
    """Boundary flags always remain engineering-only, no routing/cognition/confidence changes."""

    @pytest.mark.parametrize(
        "consumer",
        ["market_snapshot", "external_research_context", "gateway_runtime_display"],
    )
    def test_boundary_flags_always_engineering_only(self, consumer: ProviderHealthConsumer):
        health = _health_with_statuses(
            _market_status("eastmoney", status="unavailable"),
            _llm_status("gpt5", status="unavailable"),
            _ext_status("researcher", status="degraded"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer=consumer)
        assert decision.engineering_quality_only is True
        assert decision.advisory_only is True
        assert decision.investment_evidence is False
        assert decision.affects_confidence is False
        assert decision.writes_cognition is False
        assert decision.trading_decision is False
        assert decision.execution_allowed is False
        assert decision.routing_changed is False

    def test_boundary_flags_in_to_dict(self):
        health = _health_with_statuses(_market_status("eastmoney", status="healthy"))
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        d = decision.to_dict()
        assert d["engineering_quality_only"] is True
        assert d["advisory_only"] is True
        assert d["investment_evidence"] is False
        assert d["affects_confidence"] is False
        assert d["writes_cognition"] is False
        assert d["trading_decision"] is False
        assert d["execution_allowed"] is False
        assert d["routing_changed"] is False


# ── Warnings and data gaps ───────────────────────────────────────────────────


class TestWarningsAndDataGaps:
    """Warnings and data_gaps are collected from provider statuses and merged."""

    def test_warnings_from_statuses_are_collected(self):
        health = _health_with_statuses(
            _market_status(
                "eastmoney",
                status="unavailable",
                warnings=["eastmoney circuit open"],
            ),
            _market_status(
                "baostock",
                status="degraded",
                warnings=["baostock recent failures"],
            ),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert "eastmoney circuit open" in decision.warnings
        assert "baostock recent failures" in decision.warnings

    def test_data_gaps_from_statuses_are_collected(self):
        health = _health_with_statuses(
            _market_status(
                "eastmoney",
                status="unavailable",
                data_gaps=["market_data_missing"],
            ),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert "market_data_missing" in decision.data_gaps

    def test_health_level_warnings_not_tied_to_relevant_status_excluded(self):
        """Health-level warnings not originating from a relevant status are excluded."""
        health = ProviderHealthResult(
            statuses=[_market_status("eastmoney", status="healthy")],
            sanitized_warnings=["global health warning"],
            data_gaps=["global data gap"],
            summary={},
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        # Global warnings/gaps NOT tied to any market status must NOT leak in.
        assert "global health warning" not in decision.warnings
        assert "global data gap" not in decision.data_gaps

    def test_warnings_deduplicated(self):
        health = _health_with_statuses(
            _market_status(
                "eastmoney",
                status="unavailable",
                warnings=["duplicate warning"],
            ),
            _market_status(
                "baostock",
                status="degraded",
                warnings=["duplicate warning"],
            ),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        # Count occurrences — should not duplicate
        assert decision.warnings.count("duplicate warning") == 1


# ── Degraded / unavailable provider lists ────────────────────────────────────


class TestProviderLists:
    """degraded_providers and unavailable_providers are correctly populated."""

    def test_degraded_list_excludes_unavailable(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="degraded"),
            _market_status("baostock", status="unavailable"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert "eastmoney" in decision.degraded_providers
        assert "eastmoney" not in decision.unavailable_providers
        assert "baostock" in decision.unavailable_providers
        assert "baostock" not in decision.degraded_providers

    def test_healthy_providers_not_in_either_list(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="healthy"),
            _market_status("baostock", status="degraded"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert "eastmoney" not in decision.degraded_providers
        assert "eastmoney" not in decision.unavailable_providers

    def test_unknown_providers_not_in_degraded_or_unavailable(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="unknown"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert "eastmoney" not in decision.degraded_providers
        assert "eastmoney" not in decision.unavailable_providers


# ── to_dict serialization ────────────────────────────────────────────────────


class TestToDict:
    """to_dict() produces stable, consumer-facing payload."""

    def test_to_dict_contains_all_required_fields(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="unavailable", reason="circuit_open"),
            _market_status("baostock", status="healthy"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        d = decision.to_dict()

        assert d["consumer"] == "market_snapshot"
        assert d["status"] == "degraded"
        assert isinstance(d["warnings"], list)
        assert isinstance(d["data_gaps"], list)
        assert d["fallback_recommendation"] == "prefer_cache_or_stale_fallback"
        assert isinstance(d["degraded_providers"], list)
        assert isinstance(d["unavailable_providers"], list)
        assert d["engineering_quality_only"] is True
        assert d["advisory_only"] is True
        assert d["investment_evidence"] is False
        assert d["affects_confidence"] is False
        assert d["writes_cognition"] is False
        assert d["trading_decision"] is False
        assert d["execution_allowed"] is False
        assert d["routing_changed"] is False


# ── Empty / edge cases ───────────────────────────────────────────────────────


class TestConsumerWarningAndGapIsolation:
    """Non-gateway consumers must not leak warnings/gaps from unrelated categories."""

    def test_market_snapshot_does_not_leak_llm_warning_or_gap(self):
        """market_snapshot must not include LLM backend health-level warnings or data gaps."""
        health = ProviderHealthResult(
            statuses=[
                _market_status("eastmoney", status="healthy"),
            ],
            sanitized_warnings=["llm_backend_cooldown_active"],
            data_gaps=["llm_response_latency_spike"],
            summary={},
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")

        assert "llm_backend_cooldown_active" not in decision.warnings, (
            "market_snapshot should not leak LLM health-level warning"
        )
        assert "llm_response_latency_spike" not in decision.data_gaps, (
            "market_snapshot should not leak LLM health-level data gap"
        )

    def test_market_snapshot_does_not_leak_llm_status_level_warning_or_gap(self):
        """market_snapshot must not include warnings/gaps from LLM statuses in the health result."""
        health = _health_with_statuses(
            _market_status("eastmoney", status="healthy"),
            _llm_status(
                "gpt5", status="degraded", warnings=["llm_cooldown"], data_gaps=["llm_gap"]
            ),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")

        assert "llm_cooldown" not in decision.warnings, (
            "market_snapshot should not leak LLM status-level warning"
        )
        assert "llm_gap" not in decision.data_gaps, (
            "market_snapshot should not leak LLM status-level data gap"
        )

    def test_external_research_does_not_leak_market_warning_or_gap(self):
        """external_research_context must not include market provider warnings/gaps."""
        health = ProviderHealthResult(
            statuses=[
                _ext_status("researcher", status="healthy"),
            ],
            sanitized_warnings=["market_data_latency"],
            data_gaps=["market_price_gap"],
            summary={},
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="external_research_context")

        assert "market_data_latency" not in decision.warnings
        assert "market_price_gap" not in decision.data_gaps

    def test_gateway_runtime_display_includes_all_category_warnings_and_gaps(self):
        """gateway_runtime_display sees all categories, so all warnings/gaps are included."""
        health = _health_with_statuses(
            _market_status("eastmoney", status="degraded", warnings=["market_warn"]),
            _llm_status("gpt5", status="degraded", warnings=["llm_warn"]),
            _ext_status("researcher", status="degraded", warnings=["ext_warn"]),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="gateway_runtime_display")

        assert "market_warn" in decision.warnings
        assert "llm_warn" in decision.warnings
        assert "ext_warn" in decision.warnings


class TestEdgeCases:
    """Edge cases: empty health, missing category, all unknown."""

    def test_empty_health_produces_unknown(self):
        health = ProviderHealthResult(statuses=[], summary={})
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.status == "unknown"
        assert decision.fallback_recommendation == "prefer_cache_or_stale_fallback"

    def test_no_matching_category_produces_unknown(self):
        """When health has only llm_backend statuses but consumer is market_snapshot."""
        health = _health_with_statuses(
            _llm_status("gpt5", status="unavailable"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.status == "unknown"
        assert decision.fallback_recommendation == "prefer_cache_or_stale_fallback"

    def test_all_unknown_produces_degraded(self):
        health = _health_with_statuses(
            _market_status("eastmoney", status="unknown"),
            _market_status("baostock", status="unknown"),
        )
        decision = ProviderDegradationPolicy.evaluate(health, consumer="market_snapshot")
        assert decision.status == "degraded"


# ── ProviderHealthConsumer type validation ────────────────────────────────────


class TestConsumerTypeValidation:
    """Consumer type must be one of the supported values."""

    def test_invalid_consumer_raises_value_error(self):
        health = _health_with_statuses(_market_status("eastmoney", status="healthy"))
        with pytest.raises(ValueError, match="Unknown consumer"):
            ProviderDegradationPolicy.evaluate(health, consumer="invalid_consumer")  # type: ignore[arg-type]
