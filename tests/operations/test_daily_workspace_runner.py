from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fin_analyse.consultation.daily_workspace_product_contracts import (
    DailyWorkspaceCheckpoint,
)
from fin_analyse.consultation.daily_workspace_schedule import (
    DailyWorkspaceSchedulePolicy,
)
from fin_analyse.operations.daily_workspace_runner import (
    DailyWorkspaceCheckpointRunner,
    DailyWorkspaceCheckpointRunRequest,
    DailyWorkspaceDeliveryReceipt,
    DailyWorkspacePreparationError,
    DailyWorkspaceRunPhase,
    DailyWorkspaceRunStatus,
    PreparedDailyWorkspaceProduct,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DAY = date(2026, 8, 3)


class _Products:
    def __init__(self, prepared: PreparedDailyWorkspaceProduct | None = None) -> None:
        self.prepared = prepared
        self.prepare_calls: list[dict[str, object]] = []
        self.find_calls: list[tuple[str, DailyWorkspaceCheckpoint]] = []
        self.degraded_calls: list[dict[str, object]] = []

    def prepare(self, **kwargs: object) -> PreparedDailyWorkspaceProduct | None:
        self.prepare_calls.append(kwargs)
        return self.prepared

    def find_prepared(
        self,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
    ) -> PreparedDailyWorkspaceProduct | None:
        self.find_calls.append((trading_day_id, checkpoint))
        return self.prepared

    def prepare_degraded(self, **kwargs: object) -> PreparedDailyWorkspaceProduct:
        self.degraded_calls.append(kwargs)
        target_at = kwargs["target_at"]
        prepared_at = kwargs["prepared_at"]
        assert isinstance(target_at, datetime)
        assert isinstance(prepared_at, datetime)
        self.prepared = _prepared(
            target_at=target_at,
            prepared_at=prepared_at,
            generated_at=prepared_at,
            degraded=True,
            data_gaps=(str(kwargs["reason_code"]),),
        )
        return self.prepared


class _Outbox:
    def __init__(self) -> None:
        self.calls: list[tuple[PreparedDailyWorkspaceProduct, datetime]] = []
        self.sent_artifacts: set[str] = set()

    def dispatch(
        self,
        product: PreparedDailyWorkspaceProduct,
        *,
        delivered_at: datetime,
    ) -> DailyWorkspaceDeliveryReceipt:
        self.calls.append((product, delivered_at))
        already_delivered = product.artifact_hash in self.sent_artifacts
        self.sent_artifacts.add(product.artifact_hash)
        return DailyWorkspaceDeliveryReceipt(
            artifact_hash=product.artifact_hash,
            delivered_at=delivered_at,
            already_delivered=already_delivered,
        )


def _prepared(
    *,
    target_at: datetime | None = None,
    prepared_at: datetime | None = None,
    generated_at: datetime | None = None,
    degraded: bool = False,
    data_gaps: tuple[str, ...] = (),
) -> PreparedDailyWorkspaceProduct:
    target = target_at or datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI)
    prepared = prepared_at or datetime(2026, 8, 3, 9, 35, tzinfo=_SHANGHAI)
    return PreparedDailyWorkspaceProduct(
        trading_day_id="2026-08-03",
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
        workspace_ref="dw:opaque",
        product_version=2,
        artifact_hash="sha256:" + "a" * 64,
        target_at=target,
        prepared_at=prepared,
        generated_at=generated_at or prepared,
        evidence_cutoff_at=None if degraded else prepared,
        degraded=degraded,
        data_gaps=data_gaps,
    )


def _runner(
    now: datetime,
    *,
    products: _Products,
    outbox: _Outbox,
    is_open: bool = True,
) -> DailyWorkspaceCheckpointRunner:
    return DailyWorkspaceCheckpointRunner(
        schedule=DailyWorkspaceSchedulePolicy(is_open_date=lambda _day: is_open),
        products=products,
        outbox=outbox,
        clock=lambda: now,
    )


def _request(phase: DailyWorkspaceRunPhase) -> DailyWorkspaceCheckpointRunRequest:
    return DailyWorkspaceCheckpointRunRequest(
        trading_day=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
        phase=phase,
    )


def test_prepare_runs_at_prepare_cutoff_and_binds_product_times() -> None:
    now = datetime(2026, 8, 3, 9, 35, tzinfo=_SHANGHAI)
    product = _prepared(prepared_at=now, generated_at=now)
    products = _Products(product)
    outbox = _Outbox()

    result = _runner(now, products=products, outbox=outbox).run(
        _request(DailyWorkspaceRunPhase.PREPARE)
    )

    assert result.status is DailyWorkspaceRunStatus.PREPARED
    assert result.target_at == datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI)
    assert result.prepared_at == now
    assert result.generated_at == now
    assert result.evidence_cutoff_at == now
    assert products.prepare_calls == [
        {
            "trading_day_id": "2026-08-03",
            "checkpoint": DailyWorkspaceCheckpoint.MORNING_1000,
            "target_at": result.target_at,
            "prepared_at": now,
        }
    ]
    assert products.find_calls == []
    assert products.degraded_calls == []
    assert outbox.calls == []


@pytest.mark.parametrize(
    ("now", "expected_status"),
    (
        (
            datetime(2026, 8, 3, 9, 34, 59, tzinfo=_SHANGHAI),
            DailyWorkspaceRunStatus.NOT_DUE,
        ),
        (
            datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI),
            DailyWorkspaceRunStatus.WINDOW_MISSED,
        ),
    ),
)
def test_prepare_never_starts_full_generation_outside_prepare_window(
    now: datetime,
    expected_status: DailyWorkspaceRunStatus,
) -> None:
    products = _Products(_prepared())
    outbox = _Outbox()

    result = _runner(now, products=products, outbox=outbox).run(
        _request(DailyWorkspaceRunPhase.PREPARE)
    )

    assert result.status is expected_status
    assert products.prepare_calls == []
    assert outbox.calls == []


def test_unavailable_prepare_fails_without_freezing_a_product() -> None:
    now = datetime(2026, 8, 3, 9, 35, tzinfo=_SHANGHAI)
    products = _Products()
    outbox = _Outbox()

    with pytest.raises(DailyWorkspacePreparationError):
        _runner(now, products=products, outbox=outbox).run(
            _request(DailyWorkspaceRunPhase.PREPARE)
        )

    assert len(products.prepare_calls) == 1
    assert products.degraded_calls == []
    assert outbox.calls == []


def test_delivery_uses_the_exact_frozen_product_without_full_generation() -> None:
    now = datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI)
    product = _prepared()
    products = _Products(product)
    outbox = _Outbox()

    result = _runner(now, products=products, outbox=outbox).run(
        _request(DailyWorkspaceRunPhase.DELIVER)
    )

    assert result.status is DailyWorkspaceRunStatus.DELIVERED
    assert result.artifact_hash == product.artifact_hash
    assert result.delivered_at == now
    assert products.find_calls == [("2026-08-03", DailyWorkspaceCheckpoint.MORNING_1000)]
    assert products.prepare_calls == []
    assert products.degraded_calls == []
    assert outbox.calls == [(product, now)]


def test_missing_product_at_target_delivers_a_failure_notice() -> None:
    now = datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI)
    products = _Products()
    outbox = _Outbox()

    result = _runner(now, products=products, outbox=outbox).run(
        _request(DailyWorkspaceRunPhase.DELIVER)
    )

    assert result.status is DailyWorkspaceRunStatus.FAILURE_NOTICE_DELIVERED
    assert result.evidence_cutoff_at is None
    assert result.data_gaps == ("daily_workspace_prepared_product_missing",)
    assert products.prepare_calls == []
    assert len(products.degraded_calls) == 1
    assert outbox.calls[0][0].degraded is True


def test_delivery_retry_reuses_the_same_artifact_and_reports_existing_ack() -> None:
    now = datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI)
    product = _prepared()
    products = _Products(product)
    outbox = _Outbox()
    runner = _runner(now, products=products, outbox=outbox)

    first = runner.run(_request(DailyWorkspaceRunPhase.DELIVER))
    retry = runner.run(_request(DailyWorkspaceRunPhase.DELIVER))

    assert first.status is DailyWorkspaceRunStatus.DELIVERED
    assert retry.status is DailyWorkspaceRunStatus.ALREADY_DELIVERED
    assert [item.artifact_hash for item, _at in outbox.calls] == [
        product.artifact_hash,
        product.artifact_hash,
    ]
    assert products.prepare_calls == []


@pytest.mark.parametrize(
    "now",
    (
        datetime(2026, 8, 3, 9, 59, 59, tzinfo=_SHANGHAI),
        datetime(2026, 8, 3, 10, 15, 1, tzinfo=_SHANGHAI),
    ),
)
def test_delivery_does_not_send_outside_target_window(now: datetime) -> None:
    products = _Products(_prepared())
    outbox = _Outbox()

    result = _runner(now, products=products, outbox=outbox).run(
        _request(DailyWorkspaceRunPhase.DELIVER)
    )

    assert result.status in {
        DailyWorkspaceRunStatus.NOT_DUE,
        DailyWorkspaceRunStatus.WINDOW_MISSED,
    }
    assert products.find_calls == []
    assert outbox.calls == []


def test_non_trading_day_and_invalid_clock_fail_closed_without_side_effects() -> None:
    now = datetime(2026, 8, 3, 9, 35, tzinfo=_SHANGHAI)
    products = _Products(_prepared())
    outbox = _Outbox()

    closed = _runner(now, products=products, outbox=outbox, is_open=False).run(
        _request(DailyWorkspaceRunPhase.PREPARE)
    )
    assert closed.status is DailyWorkspaceRunStatus.NOT_TRADING_DAY
    assert products.prepare_calls == []

    with pytest.raises(ValueError, match="timezone-aware"):
        _runner(
            now.replace(tzinfo=None),
            products=products,
            outbox=outbox,
        ).run(_request(DailyWorkspaceRunPhase.PREPARE))


def test_delivery_window_includes_the_exact_fifteen_minute_deadline() -> None:
    target = datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI)
    now = target + timedelta(minutes=15)
    product = _prepared()
    products = _Products(product)
    outbox = _Outbox()

    result = _runner(now, products=products, outbox=outbox).run(
        _request(DailyWorkspaceRunPhase.DELIVER)
    )

    assert result.status is DailyWorkspaceRunStatus.DELIVERED
