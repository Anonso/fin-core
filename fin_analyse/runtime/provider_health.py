"""Provider health / sensory quality — unified runtime quality observation.

This module owns the request/result/status contract and normalization for
LLM backend, market provider, and external research provider health.
It is the FIN-owned runtime quality seam; gateway transport calls it as
an adapter, not as a business-logic owner.

All results are engineering/runtime quality only:
- engineering_quality_only=True
- affects_confidence=False
- writes_cognition=False
- routing_changed=False
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from typing import Any

_MARKET_OBSERVATION_MAX_AGE_SECONDS = 24 * 60 * 60
_MARKET_OBSERVATION_MAX_FUTURE_SKEW_SECONDS = 5 * 60
# Preserve exact integers across JSON consumers and bound formatting work.
_MAX_SAFE_OBSERVATION_INTEGER = (1 << 53) - 1

# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass
class ProviderHealthRequest:
    """Input for ProviderHealthService.assess().

    All fields are optional — when omitted the service falls back to
    reading real (but read-only, no-probe) sources.
    """

    include_categories: tuple[str, ...] = (
        "llm_backend",
        "market_provider",
        "external_research_provider",
    )
    llm_backend_health: dict[str, dict[str, Any]] | None = None
    configured_llm_backends: dict[str, Any] | None = None
    market_registry_health: dict[str, Any] | None = None
    external_research_observations: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass
class ProviderRuntimeStatus:
    """Normalized status for a single provider within a category."""

    category: str
    provider_name: str
    status: str  # healthy | degraded | unavailable | unknown
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "provider_name": self.provider_name,
            "status": self.status,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "data_gaps": list(self.data_gaps),
            "observations": dict(self.observations),
        }


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ProviderHealthResult:
    """Output of ProviderHealthService.assess().

    Carries unified statuses across provider categories plus sanitized
    warnings and a summary. All boundary flags default to runtime-only.
    """

    statuses: list[ProviderRuntimeStatus] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    sanitized_warnings: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)
    engineering_quality_only: bool = True
    advisory_only: bool = True
    investment_evidence: bool = False
    affects_confidence: bool = False
    writes_cognition: bool = False
    trading_decision: bool = False
    execution_allowed: bool = False
    routing_changed: bool = False

    # Legacy fields carried forward for MCP response compatibility
    _legacy_total_providers: int | None = None
    _legacy_providers: dict[str, Any] = field(default_factory=dict)
    _legacy_quality_scores: dict[str, Any] = field(default_factory=dict)
    _legacy_priority_dispatch: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_statuses": [s.to_dict() for s in self.statuses],
            "provider_health_summary": dict(self.summary),
            "sanitized_warnings": list(self.sanitized_warnings),
            "data_gaps": list(self.data_gaps),
            "engineering_quality_only": self.engineering_quality_only,
            "advisory_only": self.advisory_only,
            "investment_evidence": self.investment_evidence,
            "affects_confidence": self.affects_confidence,
            "writes_cognition": self.writes_cognition,
            "trading_decision": self.trading_decision,
            "execution_allowed": self.execution_allowed,
            "routing_changed": self.routing_changed,
            "total_providers": self._legacy_total_providers,
            "providers": dict(self._legacy_providers),
            "quality_scores": dict(self._legacy_quality_scores),
            "priority_dispatch": dict(self._legacy_priority_dispatch),
        }

    def to_mcp_payload(self) -> dict[str, Any]:
        """Return the MCP response payload preserving legacy field names."""
        return self.to_dict()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ProviderHealthService:
    """Unified runtime health assessment for LLM, market, and external research providers.

    Does NOT actively probe backends or providers — it only reads
    already-available health snapshots and circuit-breaker state.
    ``observation_reader`` (optional) supplies a sanitized owner snapshot
    produced by the low-frequency ``ProviderRuntimeObserver``; ``assess``
    itself never performs remote I/O.
    """

    def __init__(self, observation_reader: Any = None) -> None:
        self._observation_reader = observation_reader

    def assess(self, request: ProviderHealthRequest) -> ProviderHealthResult:
        statuses: list[ProviderRuntimeStatus] = []
        sanitized_warnings: list[str] = []
        data_gaps: list[str] = []
        sanitized_market_providers: dict[str, dict[str, Any]] = {}
        assessment_timestamp = _utc_now_timestamp()

        categories = request.include_categories

        # Sanitized owner snapshot from the low-frequency observer.
        # Read once for all categories; caller-provided observations take
        # precedence (design: request > observer > breaker > config).
        observer_observations: dict[str, Any] = {}
        if self._observation_reader is not None:
            try:
                observer_observations = {
                    name: _observer_observation_fragment(obs)
                    for name, obs in self._observation_reader.fresh().items()
                }
            except Exception:
                observer_observations = {}

        # ── LLM backend health ──────────────────────────────────────────
        if "llm_backend" in categories:
            llm_health = request.llm_backend_health
            configured = request.configured_llm_backends

            if llm_health is None and configured is None:
                llm_health = _read_llm_backend_snapshot()
                configured = _read_configured_llm_backends()

            if configured:
                # 优先级：caller 显式提供的 backend observation > observer snapshot
                # > circuit-breaker > config-only identity（request > observer）。
                owner_snapshot = llm_health if isinstance(llm_health, dict) else {}
                for backend_name in configured:
                    if backend_name in owner_snapshot:
                        status = _llm_status_from_owner_observation(
                            backend_name,
                            owner_snapshot[backend_name],
                            observed=True,
                        )
                    elif backend_name in observer_observations:
                        status = _observer_llm_status(
                            backend_name, observer_observations[backend_name]
                        )
                    else:
                        status = _llm_status_from_owner_observation(
                            backend_name,
                            None,
                            observed=False,
                        )
                    statuses.append(status)
                    if status.warnings:
                        sanitized_warnings.extend(status.warnings)
                    if status.data_gaps:
                        data_gaps.extend(status.data_gaps)
            elif llm_health:
                for backend_name, state in llm_health.items():
                    status = _llm_status_from_owner_observation(
                        backend_name,
                        state,
                        observed=True,
                    )
                    statuses.append(status)
                    if status.warnings:
                        sanitized_warnings.extend(status.warnings)
                    if status.data_gaps:
                        data_gaps.extend(status.data_gaps)
            elif observer_observations:
                # Observer observations are tri-state checks, not
                # circuit-breaker state: map them directly.
                for backend_name, fragment in sorted(observer_observations.items()):
                    status = _observer_llm_status(backend_name, fragment)
                    statuses.append(status)
                    if status.warnings:
                        sanitized_warnings.extend(status.warnings)
                    if status.data_gaps:
                        data_gaps.extend(status.data_gaps)
            else:
                data_gaps.append("llm_backend_no_data")

        # ── Market provider health ──────────────────────────────────────
        market_registry_health = request.market_registry_health
        if "market_provider" in categories:
            if market_registry_health is None:
                market_fragment = observer_observations.get("market_registry")
                if market_fragment is not None:
                    status = _observer_market_status(market_fragment)
                else:
                    status = ProviderRuntimeStatus(
                        category="market_provider",
                        provider_name="market_registry",
                        status="unknown",
                        reason="No owner-provided runtime observation available",
                        data_gaps=["market_provider_no_runtime_observation"],
                    )
                statuses.append(status)
                data_gaps.extend(status.data_gaps)
            elif not isinstance(market_registry_health, dict):
                status = ProviderRuntimeStatus(
                    category="market_provider",
                    provider_name="market_registry",
                    status="unknown",
                    reason="Owner runtime observation is invalid",
                    data_gaps=["market_provider_observation_invalid"],
                )
                statuses.append(status)
                data_gaps.extend(status.data_gaps)
            else:
                providers = market_registry_health.get("providers")
                if not isinstance(providers, dict):
                    status = ProviderRuntimeStatus(
                        category="market_provider",
                        provider_name="market_registry",
                        status="unknown",
                        reason="Owner runtime observation is invalid",
                        data_gaps=["market_provider_observation_invalid"],
                    )
                    statuses.append(status)
                    data_gaps.extend(status.data_gaps)
                elif not providers:
                    status = ProviderRuntimeStatus(
                        category="market_provider",
                        provider_name="market_registry",
                        status="unavailable",
                        reason="Owner runtime observation has no configured providers",
                        data_gaps=["market_provider_registry_empty"],
                    )
                    statuses.append(status)
                    data_gaps.extend(status.data_gaps)
                else:
                    for pname, pdata in providers.items():
                        if not isinstance(pname, str) or not pname.strip():
                            status = ProviderRuntimeStatus(
                                category="market_provider",
                                provider_name="invalid_market_provider",
                                status="unknown",
                                reason="provider identity is invalid",
                                data_gaps=["market_provider_identity_invalid"],
                            )
                            statuses.append(status)
                            data_gaps.extend(status.data_gaps)
                            continue
                        if not isinstance(pdata, dict):
                            status = ProviderRuntimeStatus(
                                category="market_provider",
                                provider_name=str(pname),
                                status="unknown",
                                reason="provider runtime observation is invalid",
                                data_gaps=[f"market_provider_observation_invalid:{pname}"],
                            )
                            statuses.append(status)
                            data_gaps.extend(status.data_gaps)
                            continue
                        status = _normalize_market_provider(
                            pname,
                            pdata,
                            assessment_timestamp=assessment_timestamp,
                        )
                        statuses.append(status)
                        if not any(
                            gap.startswith("market_provider_observation_invalid:")
                            for gap in status.data_gaps
                        ):
                            sanitized_market_providers[pname] = dict(status.observations)
                        if status.warnings:
                            sanitized_warnings.extend(status.warnings)
                        if status.data_gaps:
                            data_gaps.extend(status.data_gaps)

        # ── External research provider health ───────────────────────────
        if "external_research_provider" in categories:
            observations = request.external_research_observations
            if observations:
                for obs in observations:
                    status = _normalize_external_research(obs)
                    statuses.append(status)
                    if status.warnings:
                        sanitized_warnings.extend(status.warnings)
                    if status.data_gaps:
                        data_gaps.extend(status.data_gaps)
            else:
                status = ProviderRuntimeStatus(
                    category="external_research_provider",
                    provider_name="researcher",
                    status="unknown",
                    reason="No recent external research observation available",
                    data_gaps=["external_research_no_recent_observation"],
                )
                statuses.append(status)
                data_gaps.extend(status.data_gaps)

        # ── Build legacy fields ─────────────────────────────────────────
        raw_market_providers = (
            market_registry_health.get("providers")
            if isinstance(market_registry_health, dict)
            else None
        )
        legacy_total = (
            len(sanitized_market_providers) if isinstance(raw_market_providers, dict) else None
        )
        legacy_providers = sanitized_market_providers

        # Quality scores from market providers
        legacy_quality_scores = _build_quality_scores(
            sanitized_market_providers,
            provider_statuses=statuses,
        )

        # Priority dispatch health
        legacy_priority_dispatch = _read_priority_dispatch_health()

        # ── Summary ─────────────────────────────────────────────────────
        category_counts: dict[str, dict[str, int]] = {}
        for s in statuses:
            cat = category_counts.setdefault(s.category, {})
            cat[s.status] = cat.get(s.status, 0) + 1

        summary = {
            "total_providers_assessed": len(statuses),
            "by_category": category_counts,
            "total_sanitized_warnings": len(sanitized_warnings),
            "total_data_gaps": len(data_gaps),
        }

        return ProviderHealthResult(
            statuses=statuses,
            summary=summary,
            sanitized_warnings=sanitized_warnings,
            data_gaps=data_gaps,
            _legacy_total_providers=legacy_total,
            _legacy_providers=legacy_providers,
            _legacy_quality_scores=legacy_quality_scores,
            _legacy_priority_dispatch=legacy_priority_dispatch,
        )


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _llm_status_from_owner_observation(
    backend_name: str,
    state: object,
    *,
    observed: bool,
) -> ProviderRuntimeStatus:
    if not observed or state == {}:
        return ProviderRuntimeStatus(
            category="llm_backend",
            provider_name=backend_name,
            status="unknown",
            reason="No owner-provided runtime observation available",
            data_gaps=[f"llm_backend_no_runtime_observation:{backend_name}"],
        )
    required = {
        "consecutive_failures",
        "cooldown_remaining_seconds",
        "last_failure_reason",
    }
    if not isinstance(state, dict) or not required.issubset(state):
        return ProviderRuntimeStatus(
            category="llm_backend",
            provider_name=backend_name,
            status="unknown",
            reason="Owner runtime observation is invalid",
            data_gaps=[f"llm_backend_observation_invalid:{backend_name}"],
        )
    return _normalize_llm_backend(backend_name, state)


def _normalize_llm_backend(backend_name: str, state: dict[str, Any]) -> ProviderRuntimeStatus:
    """Normalize a single LLM backend circuit-breaker state."""
    failures = int(state.get("consecutive_failures", 0))
    cooldown = float(state.get("cooldown_remaining_seconds", 0))
    reason = str(state.get("last_failure_reason", ""))

    status: str
    status_reason: str
    warnings: list[str] = []
    data_gaps: list[str] = []

    if cooldown > 0:
        status = "unavailable"
        status_reason = f"cooldown:{cooldown:.0f}s failures={failures}"
        if reason:
            status_reason += f" ({reason})"
        warnings.append(
            f"LLM backend {backend_name} 处于冷却期（剩余 {cooldown:.0f}s，连续失败 {failures} 次）"
        )
    elif failures > 0:
        status = "degraded"
        status_reason = f"failures={failures}"
        if reason:
            status_reason += f" ({reason})"
        if failures >= 2:
            warnings.append(f"LLM backend {backend_name} 近期失败 {failures} 次")
    else:
        status = "healthy"
        status_reason = "no recent failures"

    return ProviderRuntimeStatus(
        category="llm_backend",
        provider_name=backend_name,
        status=status,
        reason=status_reason,
        warnings=warnings,
        data_gaps=data_gaps,
        observations={
            "consecutive_failures": failures,
            "cooldown_remaining_seconds": cooldown,
            "last_failure_reason": reason,
        },
    )


def _normalize_market_provider(
    provider_name: str,
    health_data: dict[str, Any],
    *,
    assessment_timestamp: float,
) -> ProviderRuntimeStatus:
    """Normalize a single market provider health entry."""
    raw_priority = health_data.get("priority")
    raw_circuit_open = health_data.get("circuit_open")
    raw_failures = health_data.get("consecutive_failures")
    if (
        isinstance(raw_priority, bool)
        or not isinstance(raw_priority, int)
        or raw_priority < 0
        or raw_priority > _MAX_SAFE_OBSERVATION_INTEGER
        or not isinstance(raw_circuit_open, bool)
        or isinstance(raw_failures, bool)
        or not isinstance(raw_failures, int)
        or raw_failures < 0
        or raw_failures > _MAX_SAFE_OBSERVATION_INTEGER
    ):
        return ProviderRuntimeStatus(
            category="market_provider",
            provider_name=provider_name,
            status="unknown",
            reason="provider runtime observation is invalid",
            data_gaps=[f"market_provider_observation_invalid:{provider_name}"],
        )
    circuit_open = raw_circuit_open
    failures = raw_failures
    priority = raw_priority
    last_failure = health_data.get("last_failure")
    last_success = health_data.get("last_success")
    last_failure_state, last_failure_timestamp = _market_observation_timestamp(last_failure)
    last_success_state, last_success_timestamp = _market_observation_timestamp(last_success)
    has_runtime_observation = last_failure_state == "valid" or last_success_state == "valid"

    status: str
    status_reason: str
    warnings: list[str] = []
    data_gaps: list[str] = []

    if "invalid" in (last_failure_state, last_success_state) or any(
        timestamp is not None
        and timestamp > assessment_timestamp + _MARKET_OBSERVATION_MAX_FUTURE_SKEW_SECONDS
        for timestamp in (last_failure_timestamp, last_success_timestamp)
    ):
        status = "unknown"
        status_reason = "provider runtime observation is invalid"
        data_gaps.append(f"market_provider_observation_invalid:{provider_name}")
    elif circuit_open:
        status = "unavailable"
        status_reason = "circuit_open"
        warnings.append(f"行情数据源 {provider_name} 已断路（连续失败 {failures} 次）")
    elif failures > 0:
        status = "degraded"
        status_reason = f"consecutive_failures={failures}"
        if failures >= 2:
            warnings.append(f"行情数据源 {provider_name} 近期失败 {failures} 次")
    elif last_failure_timestamp is not None and (
        last_success_timestamp is None or last_success_timestamp <= last_failure_timestamp
    ):
        status = "unknown"
        status_reason = "provider has no success after its last failure"
        data_gaps.append(f"market_provider_failure_not_recovered:{provider_name}")
    elif (
        last_success_timestamp is not None
        and assessment_timestamp - last_success_timestamp > _MARKET_OBSERVATION_MAX_AGE_SECONDS
    ):
        status = "unknown"
        status_reason = "provider success observation is stale"
        data_gaps.append(f"market_provider_observation_stale:{provider_name}")
    elif not has_runtime_observation:
        status = "unknown"
        status_reason = "provider has no runtime success or failure observation"
        data_gaps.append(f"market_provider_not_observed:{provider_name}")
    else:
        status = "healthy"
        status_reason = "no failures"

    return ProviderRuntimeStatus(
        category="market_provider",
        provider_name=provider_name,
        status=status,
        reason=status_reason,
        warnings=warnings,
        data_gaps=data_gaps,
        observations={
            "priority": priority,
            "circuit_open": circuit_open,
            "consecutive_failures": failures,
            "last_failure": last_failure_timestamp,
            "last_success": last_success_timestamp,
        },
    )


def _market_observation_timestamp(value: Any) -> tuple[str, float | None]:
    if value is None:
        return "missing", None
    if isinstance(value, str):
        return ("missing", None) if value == "" else ("invalid", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "invalid", None
    try:
        timestamp = float(value)
    except (OverflowError, TypeError, ValueError):
        return "invalid", None
    if timestamp == 0:
        return "missing", None
    if not isfinite(timestamp) or timestamp < 0:
        return "invalid", None
    return "valid", timestamp


def _utc_now_timestamp() -> float:
    return datetime.now(UTC).timestamp()


def _normalize_external_research(
    observation: Any,
) -> ProviderRuntimeStatus:
    """Normalize an external research observation into a unified status."""
    if isinstance(observation, dict):
        obs_status = str(observation.get("status", "unknown"))
        provider_name = str(observation.get("provider_name", "researcher"))
        obs_data_gaps = list(observation.get("data_gaps", []))
        error = str(observation.get("error", ""))
    else:
        obs_status = getattr(observation, "status", "unknown")
        provider_name = getattr(observation, "provider_name", "researcher")
        obs_data_gaps = list(getattr(observation, "data_gaps", []))
        error = str(getattr(observation, "error", ""))

    status: str
    status_reason: str
    warnings: list[str] = []
    data_gaps: list[str] = list(obs_data_gaps)

    if obs_status == "ok":
        status = "healthy"
        status_reason = "external research completed successfully"
    elif obs_status in ("timeout", "error"):
        status = "degraded"
        status_reason = f"external research {obs_status}"
        if error:
            status_reason += f": {error[:120]}"
        warnings.append(f"外部研究 {provider_name} {obs_status}：数据可能不完整")
    elif obs_status in ("skipped", "cache_missing"):
        status = "unknown"
        status_reason = f"external research {obs_status}"
        data_gaps.append(f"external_research_{obs_status}")
    else:
        status = "unknown"
        status_reason = f"unrecognized status: {obs_status}"

    return ProviderRuntimeStatus(
        category="external_research_provider",
        provider_name=provider_name,
        status=status,
        reason=status_reason,
        warnings=warnings,
        data_gaps=data_gaps,
        observations={
            "raw_status": obs_status,
        },
    )


# ---------------------------------------------------------------------------
# Real-source readers (read-only, no probes)
# ---------------------------------------------------------------------------


def _read_llm_backend_snapshot() -> dict[str, dict[str, Any]]:
    """Read LLM backend circuit breaker snapshot without probing."""
    try:
        from fin_analyse.claims.backend_health import get_backend_circuit_breaker

        return get_backend_circuit_breaker().snapshot()
    except Exception:
        return {}


def _read_configured_llm_backends() -> dict[str, Any]:
    """Read configured LLM backend names without constructing backends."""
    try:
        from fin_analyse.claims.config_loader import _configured_text, load_llm_config

        config = load_llm_config(load_dotenv=False)
        models = config.get("models", {})
        configured: dict[str, Any] = {}
        if not isinstance(models, dict):
            return configured
        for name, cfg in models.items():
            if not isinstance(cfg, dict):
                continue
            if not cfg.get("enabled", False):
                continue
            api_key = cfg.get("api_key", "")
            # BUG-038：口径与 config_loader._plan_is_configured 对齐——含未解析
            # ${ENV} 引用的 key 按「未配置」报，不再与 loader 的实际跳过行为相反。
            if not isinstance(api_key, str) or not _configured_text(api_key):
                continue
            configured[str(name)] = dict(cfg)
        return configured
    except Exception:
        return {}


def _build_quality_scores(
    market_providers: dict[str, dict[str, Any]],
    *,
    provider_statuses: list[ProviderRuntimeStatus],
) -> dict[str, Any]:
    """Build quality scores for market providers using ProviderQualityAssessor."""
    if not market_providers:
        return {}
    try:
        from fin_analyse.decision.external_warnings import ProviderQualityAssessor

        assessor = ProviderQualityAssessor()
        quality_scores: dict[str, Any] = {}
        observed_provider_names = {
            status.provider_name
            for status in provider_statuses
            if status.category == "market_provider" and status.status != "unknown"
        }
        for pname, pdata in market_providers.items():
            if pname not in observed_provider_names:
                continue
            assessor_health = dict(pdata)
            _, last_success_timestamp = _market_observation_timestamp(pdata.get("last_success"))
            if last_success_timestamp is not None:
                assessor_health["last_success_at"] = datetime.fromtimestamp(
                    last_success_timestamp,
                    UTC,
                ).isoformat()
            score = assessor.assess(pname, assessor_health)
            quality_scores[pname] = score.to_dict()
        return quality_scores
    except Exception:
        return {}


def _read_priority_dispatch_health() -> dict[str, Any]:
    """Read priority dispatch health without side effects."""
    try:
        from fin_analyse.cognition.priority_articles import (
            check_priority_dispatch_health,
        )

        dispatch = check_priority_dispatch_health()
        return dispatch.to_dict()
    except Exception:
        return {}


def _observer_observation_fragment(obs: Any) -> dict[str, Any]:
    """Map one sanitized observer observation to an assess-consumable fragment."""
    status = getattr(obs, "status", "unknown")
    return {
        "status": status if status in {"ok", "failed", "unknown"} else "unknown",
        "reason": getattr(obs, "reason", "unknown"),
        "observed_at": getattr(obs, "observed_at", 0.0),
        "latency_ms": getattr(obs, "latency_ms", 0),
    }


def _observer_llm_status(
    backend_name: str,
    fragment: Mapping[str, Any],
) -> ProviderRuntimeStatus:
    """Map a tri-state observer fragment to a runtime status (no breaker math)."""
    status = fragment.get("status")
    reason = str(fragment.get("reason") or "unknown")
    if status == "ok":
        return ProviderRuntimeStatus(
            category="llm_backend",
            provider_name=backend_name,
            status="healthy",
            reason=reason,
        )
    if status == "failed":
        return ProviderRuntimeStatus(
            category="llm_backend",
            provider_name=backend_name,
            status="unavailable",
            reason=reason,
            data_gaps=[f"llm_backend_observation_failed:{backend_name}"],
        )
    return ProviderRuntimeStatus(
        category="llm_backend",
        provider_name=backend_name,
        status="unknown",
        reason=reason or "No owner-provided runtime observation available",
        data_gaps=[f"llm_backend_no_runtime_observation:{backend_name}"],
    )


def _observer_market_status(fragment: Mapping[str, Any]) -> ProviderRuntimeStatus:
    """Map a tri-state observer fragment to a market provider status."""
    status = fragment.get("status")
    reason = str(fragment.get("reason") or "unknown")
    if status == "ok":
        return ProviderRuntimeStatus(
            category="market_provider",
            provider_name="market_registry",
            status="healthy",
            reason=reason,
        )
    if status == "failed":
        return ProviderRuntimeStatus(
            category="market_provider",
            provider_name="market_registry",
            status="unavailable",
            reason=reason,
            data_gaps=["market_provider_observation_failed"],
        )
    return ProviderRuntimeStatus(
        category="market_provider",
        provider_name="market_registry",
        status="unknown",
        reason=reason or "No owner-provided runtime observation available",
        data_gaps=["market_provider_no_runtime_observation"],
    )
