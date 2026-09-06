"""Timeout classification at the provider boundary (BUG-046)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from fin_analyse.guo_teacher_research.production_capability_provider import (
    ProductionReadCapabilityProvider,
)
from fin_analyse.guo_teacher_research.ready_evidence import (
    RecentReferenceReadyEvidenceReader,
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


def test_ready_evidence_timeout_propagates_from_inner_reader() -> None:
    """BUG-047 B1 P1③（BUG-046 同款）：内层 reader 兜底 catch 不得吞 TimeoutError。

    provider 层已在 BUG-046 穿透；本处护的是
    RecentReferenceReadyEvidenceReader 对 runtime_context.resolve 的兜底——
    若它先吞成 ready_evidence_context_read_failed，server 的
    read_ready_evidence_deadline_exceeded 专码分支不可达，超时与故障不可分。
    """

    class _TimeoutRuntimeContext:
        def resolve(self, request: object) -> object:
            raise TimeoutError("runtime context deadline reached")

    reader = RecentReferenceReadyEvidenceReader(
        runtime_context=_TimeoutRuntimeContext(),  # type: ignore[arg-type]
    )
    request = ProductionReadRequest(
        question="最近的参考材料",
        as_of=datetime(2026, 9, 5, tzinfo=UTC),
    )

    with pytest.raises(TimeoutError):
        reader.read(request)
