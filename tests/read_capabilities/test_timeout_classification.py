"""Timeout classification at the provider boundary (BUG-046)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from fin_analyse.guo_teacher_research.production_capability_provider import (
    ProductionReadCapabilityProvider,
)
from fin_analyse.read_capabilities.types import ProductionReadRequest


def test_margin_evidence_timeout_propagates_as_timeout_error() -> None:
    """BUG-046：reader 的 TimeoutError 必须穿透 provider 兜底 catch。

    server 的 `except TimeoutError` 把它归类为 `*_deadline_exceeded`；
    若 provider 先吞成 `*_read_failed`，trace 里超时与故障不可分。
    """

    class _TimeoutMarginReader:
        def read(self, request: object) -> object:
            raise TimeoutError("margin evidence deadline reached")

    provider = ProductionReadCapabilityProvider(
        runtime_context=SimpleNamespace(),
        margin_evidence=_TimeoutMarginReader(),  # type: ignore[arg-type]
    )
    request = ProductionReadRequest(
        question="两融拥挤度",
        as_of=datetime(2026, 9, 5, tzinfo=UTC),
    )

    with pytest.raises(TimeoutError):
        provider.read_margin_evidence(request)


def test_market_overview_timeout_propagates_as_timeout_error() -> None:
    class _TimeoutOverviewReader:
        def read(self, request: object) -> object:
            raise TimeoutError("overview deadline reached")

    provider = ProductionReadCapabilityProvider(
        runtime_context=SimpleNamespace(),
        market_overview=_TimeoutOverviewReader(),  # type: ignore[arg-type]
    )
    request = ProductionReadRequest(
        question="今天大盘怎么样",
        as_of=datetime(2026, 9, 5, tzinfo=UTC),
    )

    with pytest.raises(TimeoutError):
        provider.read_market_overview(request)
