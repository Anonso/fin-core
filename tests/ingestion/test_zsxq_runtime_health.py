"""Tests for ZSXQ Ingestion Runtime Health Service.

Uses fake supervisor/runner; no real CDP/browser/login required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

TZ = timezone(timedelta(hours=8))


# Fake supervisor types


@dataclass
class FakeCircuitState:
    open: bool = False
    reason: str | None = None
    until: datetime | None = None
    consecutive_failures: int = 0


@dataclass
class FakeScraperSupervisorHealth:
    status: str = "ok"
    checked_at: datetime | None = None
    circuit: FakeCircuitState = field(default_factory=FakeCircuitState)
    consecutive_by_kind: dict[str, int] = field(default_factory=dict)
    last_job: dict | None = None
    last_success: dict = field(default_factory=dict)
    last_success_age_minutes: float | None = None
    next_retry_at: str | None = None
    user_action_required: str | None = None


class FakeSupervisor:
    """Fake supervisor exposing get_health(); no CDP/browser dependency."""

    def __init__(self, health: FakeScraperSupervisorHealth | None = None) -> None:
        self._health = health

    def get_health(self) -> FakeScraperSupervisorHealth:
        if self._health is None:
            return FakeScraperSupervisorHealth()
        return self._health


def _make_fake_health(**overrides: Any) -> FakeScraperSupervisorHealth:
    """Build a fake health snapshot with sensible defaults."""
    now = datetime(2026, 7, 8, 10, 0, 0, tzinfo=TZ)
    defaults: dict[str, Any] = {
        "status": "circuit_open",
        "checked_at": now,
        "circuit": FakeCircuitState(
            open=True,
            reason="3 consecutive CDP failures",
            consecutive_failures=3,
        ),
        "last_job": {
            "job_id": "j-001",
            "kind": "incremental",
            "status": "failed",
        },
        "last_success": {
            "kind": "incremental",
            "at": "2026-07-07T10:00:00+08:00",
        },
        "last_success_age_minutes": 1440.0,
        "user_action_required": "login_expired",
    }
    defaults.update(overrides)
    return FakeScraperSupervisorHealth(**defaults)


# Red test: health projection with engineering boundary flags


def test_assess_projects_supervisor_health_with_engineering_boundaries() -> None:
    """Red test: importing runtime_health module should fail until implemented."""
    from fin_analyse.ingestion.runtime_health import (
        IngestionRuntimeHealthRequest,
        IngestionRuntimeHealthService,
    )

    supervisor = FakeSupervisor(health=_make_fake_health())
    service = IngestionRuntimeHealthService(
        supervisor_factory=lambda: supervisor,
    )
    request = IngestionRuntimeHealthRequest(
        store_root=None,
        include_recent_results=False,
    )
    result = service.assess(request)

    # Core health projection
    assert result.status == "circuit_open"
    assert result.circuit_open is True
    assert result.circuit_reason == "3 consecutive CDP failures"
    assert result.last_job is not None
    assert result.last_job["job_id"] == "j-001"
    assert result.last_success["kind"] == "incremental"
    assert result.last_success_age_minutes == 1440.0
    assert result.user_action_required == "login_expired"

    # Engineering boundary flags
    assert result.source_boundary == "runtime_engineering_status"
    assert result.engineering_status_only is True
    assert result.advisory_only is True
    assert result.investment_evidence is False
    assert result.writes_cognition is False
    assert result.affects_confidence is False
    assert result.trading_decision is False
    assert result.execution_allowed is False
    assert result.routing_changed is False

    # Data gap mapping: no gaps for healthy projection
    assert isinstance(result.data_gaps, tuple)
    assert isinstance(result.warnings, tuple)

    # to_dict() stability
    d = result.to_dict()
    assert isinstance(d, dict)
    assert d["status"] == "circuit_open"
    assert d["circuit_open"] is True
    assert d["source_boundary"] == "runtime_engineering_status"
    assert d["engineering_status_only"] is True
    assert d["advisory_only"] is True
    assert d["investment_evidence"] is False
    assert d["writes_cognition"] is False
    assert d["affects_confidence"] is False
    assert d["trading_decision"] is False
    assert d["execution_allowed"] is False
    assert d["routing_changed"] is False
    # checked_at must be a string (ISO format), not a datetime
    assert isinstance(d["checked_at"], str)


def test_assess_ok_health_no_data_gaps() -> None:
    """Healthy supervisor produces no data gaps."""
    from fin_analyse.ingestion.runtime_health import (
        IngestionRuntimeHealthRequest,
        IngestionRuntimeHealthService,
    )

    supervisor = FakeSupervisor(
        health=_make_fake_health(status="ok", circuit=FakeCircuitState(open=False))
    )
    service = IngestionRuntimeHealthService(supervisor_factory=lambda: supervisor)
    result = service.assess(IngestionRuntimeHealthRequest())

    assert result.status == "ok"
    assert result.circuit_open is False
    assert result.data_gaps == ()


def test_assess_requires_user_produces_data_gap() -> None:
    """REQUIRES_USER health status maps to zsxq_runtime_requires_user data gap."""
    from fin_analyse.ingestion.runtime_health import (
        IngestionRuntimeHealthRequest,
        IngestionRuntimeHealthService,
    )

    supervisor = FakeSupervisor(health=_make_fake_health(status="requires_user"))
    service = IngestionRuntimeHealthService(supervisor_factory=lambda: supervisor)
    result = service.assess(IngestionRuntimeHealthRequest())

    assert "zsxq_runtime_requires_user" in result.data_gaps


def test_assess_degraded_produces_data_gap() -> None:
    """DEGRADED health status maps to zsxq_runtime_degraded data gap."""
    from fin_analyse.ingestion.runtime_health import (
        IngestionRuntimeHealthRequest,
        IngestionRuntimeHealthService,
    )

    supervisor = FakeSupervisor(health=_make_fake_health(status="degraded"))
    service = IngestionRuntimeHealthService(supervisor_factory=lambda: supervisor)
    result = service.assess(IngestionRuntimeHealthRequest())

    assert "zsxq_runtime_degraded" in result.data_gaps


def test_assess_unavailable_supervisor_produces_data_gap() -> None:
    """When supervisor is unavailable, produces zsxq_runtime_health_unavailable gap."""
    from fin_analyse.ingestion.runtime_health import (
        IngestionRuntimeHealthRequest,
        IngestionRuntimeHealthService,
    )

    class RaisingSupervisor:
        def get_health(self) -> None:
            raise RuntimeError("health file missing")

    service = IngestionRuntimeHealthService(supervisor_factory=lambda: RaisingSupervisor())
    result = service.assess(IngestionRuntimeHealthRequest())

    assert result.status == "degraded"
    assert "zsxq_runtime_health_unavailable" in result.data_gaps


def test_assess_failed_produces_data_gap() -> None:
    """FAILED health status maps to zsxq_runtime_degraded data gap."""
    from fin_analyse.ingestion.runtime_health import (
        IngestionRuntimeHealthRequest,
        IngestionRuntimeHealthService,
    )

    supervisor = FakeSupervisor(health=_make_fake_health(status="failed"))
    service = IngestionRuntimeHealthService(supervisor_factory=lambda: supervisor)
    result = service.assess(IngestionRuntimeHealthRequest())

    assert "zsxq_runtime_degraded" in result.data_gaps


def test_assess_without_factory_returns_degraded_supervisor_unavailable() -> None:
    """Legacy supervisor seam retired (2026-08-19): no injected factory -> degraded."""
    from fin_analyse.ingestion.runtime_health import (
        IngestionRuntimeHealthRequest,
        IngestionRuntimeHealthService,
    )

    result = IngestionRuntimeHealthService().assess(IngestionRuntimeHealthRequest())

    assert result.status == "degraded"
    assert "zsxq_runtime_health_unavailable" in result.data_gaps
    assert "supervisor_unavailable" in result.warnings
