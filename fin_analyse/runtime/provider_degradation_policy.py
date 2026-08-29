"""Provider Degradation Policy — convert provider health into consumer-specific degradation decisions.

This module is the runtime policy seam between ProviderHealthService.assess()
and upper-layer consumers (MarketSnapshot, ExternalResearchContext, Gateway runtime
display). It outputs recommendations, warnings, data gaps,
and provider lists — NOT routing changes, confidence adjustments, cognition writes,
RiskGuard changes, or trading actions.

All results are engineering/runtime quality only:
- engineering_quality_only=True
- affects_confidence=False
- writes_cognition=False
- routing_changed=False
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from fin_analyse.runtime.provider_health import ProviderHealthResult

# ---------------------------------------------------------------------------
# Consumer type
# ---------------------------------------------------------------------------

ProviderHealthConsumer = Literal[
    "market_snapshot",
    "external_research_context",
    "gateway_runtime_display",
]

# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

_CONSUMER_CATEGORY_MAP: dict[ProviderHealthConsumer, tuple[str, ...]] = {
    "market_snapshot": ("market_provider",),
    "external_research_context": ("external_research_provider",),
    "gateway_runtime_display": (
        "llm_backend",
        "market_provider",
        "external_research_provider",
    ),
}

# ---------------------------------------------------------------------------
# Fallback recommendation map
# ---------------------------------------------------------------------------

_FALLBACK_MAP: dict[ProviderHealthConsumer, dict[str, str]] = {
    "market_snapshot": {
        "healthy": "none",
        "degraded": "prefer_cache_or_stale_fallback",
        "unavailable": "prefer_cache_or_stale_fallback",
        "unknown": "prefer_cache_or_stale_fallback",
    },
    "external_research_context": {
        "healthy": "none",
        "degraded": "prefer_cache_first_or_mark_external_research_gap",
        "unavailable": "prefer_cache_first_or_mark_external_research_gap",
        "unknown": "prefer_cache_first_or_mark_external_research_gap",
    },
    "gateway_runtime_display": {
        "healthy": "none",
        "degraded": "none",
        "unavailable": "none",
        "unknown": "none",
    },
}

# Status priority: higher index = worse
_STATUS_PRIORITY: dict[str, int] = {
    "healthy": 0,
    "unknown": 1,
    "degraded": 2,
    "unavailable": 3,
}


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderDegradationDecision:
    """Consumer-specific degradation decision produced by ProviderDegradationPolicy.evaluate().

    All boundary flags default to runtime/engineering-only — this policy does
    not change routing, confidence, cognition, or trading actions.
    """

    consumer: ProviderHealthConsumer
    status: str  # healthy | degraded | unavailable | unknown
    warnings: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)
    fallback_recommendation: str = "none"
    degraded_providers: list[str] = field(default_factory=list)
    unavailable_providers: list[str] = field(default_factory=list)
    engineering_quality_only: bool = True
    advisory_only: bool = True
    investment_evidence: bool = False
    affects_confidence: bool = False
    writes_cognition: bool = False
    trading_decision: bool = False
    execution_allowed: bool = False
    routing_changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer": self.consumer,
            "status": self.status,
            "warnings": list(self.warnings),
            "data_gaps": list(self.data_gaps),
            "fallback_recommendation": self.fallback_recommendation,
            "degraded_providers": list(self.degraded_providers),
            "unavailable_providers": list(self.unavailable_providers),
            "engineering_quality_only": self.engineering_quality_only,
            "advisory_only": self.advisory_only,
            "investment_evidence": self.investment_evidence,
            "affects_confidence": self.affects_confidence,
            "writes_cognition": self.writes_cognition,
            "trading_decision": self.trading_decision,
            "execution_allowed": self.execution_allowed,
            "routing_changed": self.routing_changed,
        }


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class ProviderDegradationPolicy:
    """Convert ProviderHealthResult into consumer-specific ProviderDegradationDecision.

    Does NOT perform routing, confidence adjustment, cognition writes,
    RiskGuard changes, or trading actions.
    """

    @staticmethod
    def evaluate(
        health: ProviderHealthResult,
        consumer: ProviderHealthConsumer,
    ) -> ProviderDegradationDecision:
        """Evaluate provider health for a specific consumer.

        Parameters
        ----------
        health : ProviderHealthResult
            The unified provider health assessment result.
        consumer : ProviderHealthConsumer
            The consumer to evaluate degradation for.

        Returns
        -------
        ProviderDegradationDecision
            Consumer-specific degradation decision with warnings, data gaps,
            fallback recommendation, and boundary flags.
        """
        if consumer not in _CONSUMER_CATEGORY_MAP:
            raise ValueError(
                f"Unknown consumer: {consumer!r}. Supported: {list(_CONSUMER_CATEGORY_MAP.keys())}"
            )

        target_categories = _CONSUMER_CATEGORY_MAP[consumer]

        # Filter statuses to the consumer's categories
        relevant = [s for s in health.statuses if s.category in target_categories]

        # Collect warnings and data gaps from relevant statuses only.
        # Do NOT blindly copy global health.sanitized_warnings / health.data_gaps —
        # those may contain warnings/gaps from unrelated provider categories.
        # For gateway_runtime_display, relevant statuses already include all
        # categories, so status-level warnings/gaps cover everything.
        warnings: list[str] = []
        data_gaps: list[str] = []

        # Determine worst status across relevant providers
        worst_priority = -1
        worst_status = "unknown"
        degraded_providers: list[str] = []
        unavailable_providers: list[str] = []

        for s in relevant:
            priority = _STATUS_PRIORITY.get(s.status, 1)
            if priority > worst_priority:
                worst_priority = priority
                worst_status = s.status

            if s.status == "unavailable":
                unavailable_providers.append(s.provider_name)
            elif s.status == "degraded":
                degraded_providers.append(s.provider_name)

            # Collect status-level warnings and data gaps
            for w in s.warnings:
                if w not in warnings:
                    warnings.append(w)
            for g in s.data_gaps:
                if g not in data_gaps:
                    data_gaps.append(g)

        # If no relevant statuses found, treat as unknown
        if not relevant:
            worst_status = "unknown"

        # When a healthy provider exists, "unavailable" is downgraded to "degraded"
        # (e.g. one unavailable + one healthy → degraded, not unavailable)
        if worst_status == "unavailable":
            has_healthy = any(s.status == "healthy" for s in relevant)
            if has_healthy:
                worst_status = "degraded"

        # All providers are unknown → treat as degraded (not healthy)
        if worst_status == "unknown" and relevant:
            worst_status = "degraded"

        # Determine fallback recommendation
        fallback = _FALLBACK_MAP.get(consumer, {}).get(worst_status, "none")

        return ProviderDegradationDecision(
            consumer=consumer,
            status=worst_status,
            warnings=warnings,
            data_gaps=data_gaps,
            fallback_recommendation=fallback,
            degraded_providers=degraded_providers,
            unavailable_providers=unavailable_providers,
        )
