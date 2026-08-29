"""ZSXQ Ingestion Runtime Health / Supervisor Bridge.

FIN-owned internal runtime seam that projects ScraperSupervisor health
and run results into stable JSON-safe contracts with engineering-only
boundary flags.

State owner remains ScraperSupervisor / RuntimeStore; this module is
read/projection/delegation only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Runtime health


@dataclass(frozen=True)
class IngestionRuntimeHealthRequest:
    """Request to assess ZSXQ ingestion runtime health."""

    store_root: Path | None = None
    include_recent_results: bool = False


@dataclass(frozen=True)
class IngestionRuntimeHealthResult:
    """Stable projection of ScraperSupervisor health with engineering boundary flags.

    All fields use JSON-safe primitives; no raw dataclass or datetime objects.
    """

    status: str  # ok | degraded | circuit_open | requires_user | failed
    checked_at: str | None
    circuit_open: bool
    circuit_reason: str | None
    last_job: Mapping[str, Any] | None
    last_success: Mapping[str, str]
    last_success_age_minutes: float | None
    next_retry_at: str | None
    user_action_required: str | None
    data_gaps: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    source_boundary: str = "runtime_engineering_status"
    engineering_status_only: bool = True
    advisory_only: bool = True
    investment_evidence: bool = False
    writes_cognition: bool = False
    affects_confidence: bool = False
    trading_decision: bool = False
    execution_allowed: bool = False
    routing_changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe dict with only primitive values."""
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "circuit_open": self.circuit_open,
            "circuit_reason": self.circuit_reason,
            "last_job": dict(self.last_job) if self.last_job else None,
            "last_success": dict(self.last_success),
            "last_success_age_minutes": self.last_success_age_minutes,
            "next_retry_at": self.next_retry_at,
            "user_action_required": self.user_action_required,
            "data_gaps": list(self.data_gaps),
            "warnings": list(self.warnings),
            "source_boundary": self.source_boundary,
            "engineering_status_only": self.engineering_status_only,
            "advisory_only": self.advisory_only,
            "investment_evidence": self.investment_evidence,
            "writes_cognition": self.writes_cognition,
            "affects_confidence": self.affects_confidence,
            "trading_decision": self.trading_decision,
            "execution_allowed": self.execution_allowed,
            "routing_changed": self.routing_changed,
        }


class IngestionRuntimeHealthService:
    """Assess ZSXQ ingestion runtime health via ScraperSupervisor.

    Uses dependency injection (supervisor_factory) so tests can inject
    fake supervisors. The default factory wires RuntimeStore +
    ScraperSupervisor with a no-op runner for health projection.
    """

    def __init__(
        self,
        supervisor_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._supervisor_factory = supervisor_factory

    def assess(
        self,
        request: IngestionRuntimeHealthRequest | None = None,
    ) -> IngestionRuntimeHealthResult:
        """Project supervisor health into a stable engineering contract."""
        request = request or IngestionRuntimeHealthRequest()
        supervisor = self._resolve_supervisor(request)
        if supervisor is None:
            return IngestionRuntimeHealthResult(
                status="degraded",
                checked_at=None,
                circuit_open=False,
                circuit_reason=None,
                last_job=None,
                last_success={},
                last_success_age_minutes=None,
                next_retry_at=None,
                user_action_required=None,
                data_gaps=("zsxq_runtime_health_unavailable",),
                warnings=("supervisor_unavailable",),
            )

        try:
            health = supervisor.get_health()
        except Exception:
            logger.warning("Failed to read supervisor health", exc_info=True)
            return IngestionRuntimeHealthResult(
                status="degraded",
                checked_at=None,
                circuit_open=False,
                circuit_reason=None,
                last_job=None,
                last_success={},
                last_success_age_minutes=None,
                next_retry_at=None,
                user_action_required=None,
                data_gaps=("zsxq_runtime_health_unavailable",),
                warnings=("health_read_error",),
            )

        status = _normalize_status(health.status)
        circuit_open = (
            getattr(health.circuit, "open", False) if hasattr(health, "circuit") else False
        )
        circuit_reason = (
            getattr(health.circuit, "reason", None) if hasattr(health, "circuit") else None
        )

        checked_at: str | None = None
        if health.checked_at is not None:
            checked_at = health.checked_at.isoformat()

        last_job: dict[str, Any] | None = None
        if health.last_job is not None:
            last_job = dict(health.last_job)

        last_success: dict[str, str] = {}
        if health.last_success:
            last_success = {str(k): str(v) for k, v in health.last_success.items()}

        next_retry_at: str | None = None
        if hasattr(health, "next_retry_at") and health.next_retry_at is not None:
            next_retry_at = str(health.next_retry_at)

        data_gaps = _map_health_data_gaps(status, circuit_open)

        return IngestionRuntimeHealthResult(
            status=status,
            checked_at=checked_at,
            circuit_open=circuit_open,
            circuit_reason=circuit_reason,
            last_job=last_job,
            last_success=last_success,
            last_success_age_minutes=health.last_success_age_minutes,
            next_retry_at=next_retry_at,
            user_action_required=health.user_action_required,
            data_gaps=tuple(data_gaps),
        )

    def _resolve_supervisor(self, request: IngestionRuntimeHealthRequest) -> Any:
        # legacy supervisor seam 已退休(2026-08-19):无注入 factory 时不可用,返回 None → degraded
        if self._supervisor_factory is not None:
            return self._supervisor_factory()
        return None



def _normalize_status(raw: str) -> str:
    """Normalize supervisor health status to a known set."""
    known = {"ok", "degraded", "circuit_open", "requires_user", "failed"}
    if raw in known:
        return raw
    return "degraded"


def _map_health_data_gaps(status: str, circuit_open: bool) -> list[str]:
    """Map health status to engineering data gaps."""
    gaps: list[str] = []
    if status == "requires_user":
        gaps.append("zsxq_runtime_requires_user")
    if circuit_open:
        gaps.append("zsxq_runtime_circuit_open")
    if status in ("failed", "degraded"):
        gaps.append("zsxq_runtime_degraded")
    return gaps

def _map_run_data_gaps(status: str) -> list[str]:
    """Map run-job status to engineering data gaps."""
    if status == "requires_user":
        return ["zsxq_runtime_requires_user"]
    if status == "skipped_circuit_open":
        return ["zsxq_runtime_circuit_open"]
    if status == "skipped_lock_busy":
        return ["zsxq_runtime_lock_busy"]
    if status in ("failed", "degraded"):
        return ["zsxq_runtime_degraded"]
    return []


