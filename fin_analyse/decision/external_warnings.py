"""S-010C: External sensory data → decision warnings.

Interprets external data (lockup expiration, block trades, announcements,
capital flows) as advisory warning signals for decision-making.

All warnings are advisory-only and must retain human confirmation / fallback.
Any use affecting risk_guard/decision requires explicit design.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExternalWarning:
    """A warning signal derived from external sensory data."""

    warning_id: str
    category: str  # lockup, block_trade, announcement, capital_flow, data_gap
    severity: str  # critical, high, medium, low
    company: str = ""
    ticker: str = ""
    summary: str = ""
    source: str = ""  # which provider/source
    detected_at: str = ""
    expires_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "warning_id": self.warning_id,
            "category": self.category,
            "severity": self.severity,
            "company": self.company,
            "ticker": self.ticker,
            "summary": self.summary,
            "source": self.source,
            "detected_at": self.detected_at,
            "expires_at": self.expires_at,
            "metadata": dict(self.metadata),
        }


@dataclass
class ProviderQualityScore:
    """Quality assessment for a data provider."""

    provider_name: str
    overall_rating: str  # excellent, good, degraded, poor, unknown
    data_freshness: str  # fresh (<1h), recent (<1d), stale (<3d), old (>3d)
    field_completeness: float = 1.0  # 0.0-1.0
    consecutive_failures: int = 0
    circuit_open: bool = False
    last_success_at: str = ""
    last_failure_at: str = ""
    drift_warnings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "overall_rating": self.overall_rating,
            "data_freshness": self.data_freshness,
            "field_completeness": self.field_completeness,
            "consecutive_failures": self.consecutive_failures,
            "circuit_open": self.circuit_open,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "drift_warnings": list(self.drift_warnings),
            "recommendations": list(self.recommendations),
        }


class ProviderQualityAssessor:
    """Assesses provider health beyond circuit breaker status.

    Adds data freshness, field completeness, and drift detection
    to the existing circuit-breaker-based health check.
    """

    def assess(self, provider_name: str, health_data: dict[str, Any]) -> ProviderQualityScore:
        """Evaluate provider quality from health data.

        Args:
            provider_name: Provider name.
            health_data: Health data from ProviderRegistry.health() for this provider.
        """
        failures = int(health_data.get("consecutive_failures", 0))
        circuit_open = bool(health_data.get("circuit_open", False))
        last_failure = str(health_data.get("last_failure", "") or "")

        # Determine freshness from health data
        freshness = "unknown"
        last_success = ""
        if health_data.get("last_success_at"):
            last_success = str(health_data["last_success_at"])
            try:
                success_dt = datetime.fromisoformat(last_success)
                hours_ago = (datetime.now(UTC) - success_dt).total_seconds() / 3600
                if hours_ago < 1:
                    freshness = "fresh"
                elif hours_ago < 24:
                    freshness = "recent"
                elif hours_ago < 72:
                    freshness = "stale"
                else:
                    freshness = "old"
            except (ValueError, TypeError):
                pass

        # Determine rating
        if circuit_open:
            overall = "poor"
        elif failures >= 3:
            overall = "degraded"
        elif freshness in ("fresh", "recent"):
            overall = "good"
        elif freshness == "stale":
            overall = "degraded"
        elif freshness == "old":
            overall = "poor"
        else:
            overall = "good" if failures == 0 else "degraded"

        # Field completeness check (from health metadata if available)
        field_completeness = 1.0
        field_status = health_data.get("field_status", {})
        if field_status:
            present = sum(1 for v in field_status.values() if v)
            total = len(field_status)
            if total > 0:
                field_completeness = present / total

        # Drift warnings
        drift_warnings: list[str] = []
        if freshness == "old":
            drift_warnings.append(f"{provider_name} 数据超过3天未更新，可能存在字段漂移")
        if field_completeness < 0.7:
            drift_warnings.append(
                f"{provider_name} 字段完整度 {field_completeness:.0%}，关键数据可能缺失"
            )
        if failures >= 2:
            drift_warnings.append(f"{provider_name} 连续失败 {failures} 次，数据质量不可靠")

        # Recommendations
        recommendations: list[str] = []
        if overall == "poor":
            recommendations.append(f"建议检查 {provider_name} 连接配置或切换备用 provider")
        if freshness in ("stale", "old"):
            recommendations.append(f"{provider_name} 数据过期，建议触发手动刷新")
        if circuit_open:
            recommendations.append(f"{provider_name} 已断路，将在冷却期后自动重试")

        return ProviderQualityScore(
            provider_name=provider_name,
            overall_rating=overall,
            data_freshness=freshness,
            field_completeness=field_completeness,
            consecutive_failures=failures,
            circuit_open=circuit_open,
            last_success_at=last_success,
            last_failure_at=last_failure,
            drift_warnings=drift_warnings,
            recommendations=recommendations,
        )


class DecisionWarningAggregator:
    """Aggregates external sensory warnings for decision support.

    Checks for:
    - Data quality issues (provider failures, stale data)
    - Knowledge coverage gaps (companies not in KB)
    - Cross-validation gaps (claims without verification)
    - Market data anomalies (abnormal volume, price gaps)

    All warnings are ADVISORY-ONLY. Human confirmation required before
    any decision that affects risk_guard or position changes.
    """

    def aggregate(
        self,
        *,
        company: str = "",
        ticker: str = "",
        provider_health: dict[str, Any] | None = None,
    ) -> list[ExternalWarning]:
        """Aggregate warnings for a given company/ticker context.

        Args:
            company: Company name for context-specific warnings.
            ticker: Ticker for market data checks.
            provider_health: Health data from all providers.

        Returns:
            List of ExternalWarning, sorted by severity.
        """
        warnings: list[ExternalWarning] = []
        now = datetime.now(UTC).isoformat()

        # 1. Provider quality warnings
        if provider_health:
            for pname, pdata in provider_health.get("providers", {}).items():
                failures = pdata.get("consecutive_failures", 0)
                if failures >= 2:
                    warnings.append(
                        ExternalWarning(
                            warning_id=f"provider_degraded:{pname}",
                            category="data_gap",
                            severity="medium" if failures < 5 else "high",
                            summary=f"数据源 {pname} 连续失败 {failures} 次，{ticker or company} 行情数据可能不准确",
                            source=pname,
                            detected_at=now,
                            metadata={"provider": pname, "failures": failures},
                        )
                    )
                if pdata.get("circuit_open"):
                    warnings.append(
                        ExternalWarning(
                            warning_id=f"provider_circuit_open:{pname}",
                            category="data_gap",
                            severity="high",
                            summary=f"数据源 {pname} 已断路，{ticker or company} 相关数据不可用",
                            source=pname,
                            detected_at=now,
                            metadata={"provider": pname},
                        )
                    )

        # 2. Knowledge coverage warning (S-010 specific)
        # This would check ExternalContextService for coverage gaps
        # For now, a placeholder that can be enriched when S-010A/B provide data

        # 3. Market anomaly warnings (placeholder for future S-010 enrichment)
        # - Abnormal volume detection
        # - Price gap detection
        # - Lockup expiration calendar

        # Sort by severity
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        warnings.sort(key=lambda w: severity_order.get(w.severity, 99))

        return warnings

    def aggregate_portfolio(
        self,
        *,
        companies: list[dict[str, str]],
        provider_health: dict[str, Any] | None = None,
    ) -> list[ExternalWarning]:
        """Aggregate warnings for a portfolio of companies."""
        all_warnings: list[ExternalWarning] = []
        for c in companies:
            all_warnings.extend(
                self.aggregate(
                    company=c.get("company", ""),
                    ticker=c.get("ticker", ""),
                    provider_health=provider_health,
                )
            )
        # Deduplicate by warning_id
        seen: set[str] = set()
        unique: list[ExternalWarning] = []
        for w in all_warnings:
            if w.warning_id not in seen:
                seen.add(w.warning_id)
                unique.append(w)
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        unique.sort(key=lambda w: severity_order.get(w.severity, 99))
        return unique
