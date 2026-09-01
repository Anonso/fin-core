from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from fin_analyse.consultation.daily_workspace import DailyWorkspaceService
from fin_analyse.consultation.daily_workspace_product_contracts import (
    DailyWorkspaceCheckpoint,
)
from fin_analyse.consultation.daily_workspace_schedule import (
    DailyWorkspaceSchedulePolicy,
)
from fin_analyse.guo_teacher_research.semantic_state import ResearchStateRepository
from fin_analyse.operations.daily_workspace_generator import (
    DailyWorkspaceGenerationUnavailableError,
)
from fin_analyse.operations.daily_workspace_runner import (
    DailyWorkspaceCheckpointRunner,
    DailyWorkspaceCheckpointRunRequest,
    DailyWorkspaceDeliveryReceipt,
    DailyWorkspacePreparationError,
    DailyWorkspaceRunPhase,
    DailyWorkspaceRunStatus,
    DailyWorkspaceStateProductAdapter,
    PreparedDailyWorkspaceProduct,
    _TimingBoundGenerator,
)
from tests.fixtures.daily_workspace import consultation_product

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DAY = "2026-08-03"
_TARGET = datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI)
_PREPARED = datetime(2026, 8, 3, 9, 55, tzinfo=_SHANGHAI)
_EVIDENCE_CUTOFF = datetime(2026, 8, 3, 1, 50, tzinfo=UTC)


class _Principal:
    principal_id = "finp_daily"


class _Repository:
    def __init__(self) -> None:
        self.read: object | None = None
        self.calls: list[dict[str, str]] = []

    def find_daily_workspace_version_by_key(self, **kwargs: str) -> object | None:
        self.calls.append(kwargs)
        return self.read


class _Service:
    def __init__(self, repository: _Repository, *, invoke_generator: bool = True) -> None:
        self._repository = repository
        self._invoke_generator = invoke_generator
        self.calls: list[dict[str, object]] = []

    def scheduled(
        self,
        trading_day_id: str,
        checkpoint: str,
        *,
        principal: object,
        generator: object,
    ) -> object:
        self.calls.append(
            {
                "trading_day_id": trading_day_id,
                "checkpoint": checkpoint,
                "principal": principal,
                "generator": generator,
            }
        )
        if not self._invoke_generator:
            return SimpleNamespace(status="unavailable")
        product = generator.generate(  # type: ignore[attr-defined]
            snapshot={"parent_artifact_hash": "parent-hash"},
            principal=principal,
        )
        self._repository.read = SimpleNamespace(
            workspace_ref="dw:opaque",
            product_version=2,
            artifact_hash="sha256:" + "b" * 64,
            product=product,
        )
        return SimpleNamespace(status=product["consultation_status"])


class _Generator:
    def __init__(
        self,
        product: dict[str, object] | None = None,
        *,
        consultation_as_of: object = _EVIDENCE_CUTOFF.isoformat(),
        include_receipt: bool = True,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._product = product or {
            "schema_version": "fin.daily_workspace_product/v1",
            "checkpoint": "morning",
            "trading_day_id": _DAY,
            "origin": "scheduled",
            "generated_via": "consultation-chain-v1",
            "consultation_status": "completed",
            "agent_provenance": {
                "runtime_invoked_at_generation": True,
                "output_used": True,
            },
            "consultation_product": consultation_product(
                summary="prepared",
                with_g=False,
            ),
            "context_boundaries": {
                "prior_product": "FIN_OWNED_PRIOR_CONTEXT_NOT_NEW_EVIDENCE",
                "user_question": "NOT_EVIDENCE",
            },
            "first_screen": {
                "top_items": [{"item": "prepared", "disposition": "OBSERVE"}],
                "rationale": [],
                "changes_vs_previous": [],
                "unknowns": [],
                "portfolio_review": [],
            },
            "data_gaps": [],
        }
        if include_receipt:
            self._product["input_snapshot_receipt"] = {
                "schema": "fin.daily-workspace-input-receipt/v1",
                "consultation_as_of": consultation_as_of,
            }

    def generate(self, *, snapshot: object, principal: object) -> object:
        self.calls.append({"snapshot": snapshot, "principal": principal})
        return self._product


class _UnavailableGenerator:
    def generate(self, *, snapshot: object, principal: object) -> object:
        del snapshot, principal
        raise DailyWorkspaceGenerationUnavailableError(
            ("model_route_unavailable",),
            agent_runtime_invoked=True,
        )


class _BlockingGenerator(_Generator):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def generate(self, *, snapshot: object, principal: object) -> object:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test generator was not released")
        return super().generate(snapshot=snapshot, principal=principal)


class _Outbox:
    def __init__(self) -> None:
        self.products: list[PreparedDailyWorkspaceProduct] = []

    def dispatch(
        self,
        product: PreparedDailyWorkspaceProduct,
        *,
        delivered_at: datetime,
    ) -> DailyWorkspaceDeliveryReceipt:
        self.products.append(product)
        return DailyWorkspaceDeliveryReceipt(
            artifact_hash=product.artifact_hash,
            delivered_at=delivered_at,
        )


class _FinishNormalBeforeParentReadRepository:
    def __init__(
        self,
        delegate: ResearchStateRepository,
        finish_normal: Callable[[], PreparedDailyWorkspaceProduct | None],
    ) -> None:
        self._delegate = delegate
        self._finish_normal = finish_normal
        self.normal_product: PreparedDailyWorkspaceProduct | None = None

    def find_daily_workspace(self, **kwargs: str) -> object | None:
        if self.normal_product is None:
            self.normal_product = self._finish_normal()
        return self._delegate.find_daily_workspace(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _FailParentReadRepository:
    def __init__(self, delegate: ResearchStateRepository) -> None:
        self._delegate = delegate

    def find_daily_workspace(self, **kwargs: str) -> object | None:
        del kwargs
        raise RuntimeError("parent read failed")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def _adapter(
    *,
    service: object,
    repository: object,
    generator: object,
    generated_at: datetime,
) -> DailyWorkspaceStateProductAdapter:
    return DailyWorkspaceStateProductAdapter(
        service=cast(Any, service),
        repository=cast(Any, repository),
        generator=cast(Any, generator),
        principal=_Principal(),
        clock=lambda: generated_at,
    )


def test_prepare_persists_one_timing_bound_product_and_reads_exact_version() -> None:
    repository = _Repository()
    service = _Service(repository)
    generator = _Generator()
    generated_at = datetime(2026, 8, 3, 9, 51, tzinfo=_SHANGHAI)
    adapter = _adapter(
        service=service,
        repository=repository,
        generator=generator,
        generated_at=generated_at,
    )

    product = adapter.prepare(
        trading_day_id=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
        target_at=_TARGET,
        prepared_at=_PREPARED,
    )

    assert product is not None
    assert product.target_at == _TARGET
    assert product.prepared_at == _PREPARED
    assert product.generated_at == generated_at
    assert product.evidence_cutoff_at == _EVIDENCE_CUTOFF.astimezone(_SHANGHAI)
    assert product.degraded is False
    assert len(generator.calls) == 1
    assert generator.calls[0]["snapshot"] == {
        "parent_artifact_hash": "parent-hash",
        "daily_workspace_deadline_at": _TARGET.isoformat(),
    }
    assert repository.calls[-1] == {
        "principal_id": "finp_daily",
        "trading_day_id": _DAY,
        "idempotency_key": "daily:2026-08-03:morning",
    }
    stored = cast(Any, repository.read).product
    assert stored["agent_provenance"] == {
        "runtime_invoked_at_generation": True,
        "output_used": True,
    }
    assert stored["delivery_timing"] == {
        "schema": "fin.daily-workspace-delivery-timing/v1",
        "target_at": _TARGET.isoformat(),
        "prepared_at": _PREPARED.isoformat(),
        "generated_at": generated_at.isoformat(),
        "evidence_cutoff_at": _EVIDENCE_CUTOFF.astimezone(_SHANGHAI).isoformat(),
    }
    assert "delivered_at" not in stored["delivery_timing"]


def test_completion_clock_allows_evidence_created_after_the_frozen_prepare_start() -> None:
    """A scheduler's entry timestamp is not the product completion timestamp."""

    repository = _Repository()
    completed_at = _PREPARED.replace(second=1)
    adapter = _adapter(
        service=_Service(repository),
        repository=repository,
        generator=_Generator(consultation_as_of=completed_at.isoformat()),
        generated_at=completed_at,
    )

    product = adapter.prepare(
        trading_day_id=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
        target_at=_TARGET,
        prepared_at=_PREPARED,
    )

    assert product is not None
    assert product.degraded is False
    assert product.generated_at == completed_at
    assert product.evidence_cutoff_at == completed_at


def test_typed_generation_unavailable_does_not_freeze_a_degraded_product() -> None:
    class FlakyGenerator(_Generator):
        def generate(self, *, snapshot: object, principal: object) -> object:
            self.calls.append({"snapshot": snapshot, "principal": principal})
            if len(self.calls) == 1:
                raise DailyWorkspaceGenerationUnavailableError(
                    ("model_route_unavailable",),
                    agent_runtime_invoked=True,
                )
            return self._product

    generator = FlakyGenerator(consultation_as_of=_PREPARED.isoformat())
    repository = _Repository()
    service = _Service(repository)
    adapter = _adapter(
        service=service,
        repository=repository,
        generator=generator,
        generated_at=datetime(2026, 8, 3, 9, 36, tzinfo=_SHANGHAI),
    )

    with pytest.raises(DailyWorkspaceGenerationUnavailableError) as failure:
        adapter.prepare(
            trading_day_id=_DAY,
            checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
            target_at=_TARGET,
            prepared_at=_PREPARED,
        )

    assert failure.value.data_gaps == ("model_route_unavailable",)
    assert repository.read is None


def test_product_finishing_at_or_after_target_freezes_for_immediate_delivery() -> None:
    """A late result is allowed and recorded honestly (delivery waits for it)."""

    repository = _Repository()
    service = _Service(repository)
    adapter = _adapter(
        service=service,
        repository=repository,
        generator=_Generator(),
        generated_at=_TARGET,
    )

    product = adapter.prepare(
        trading_day_id=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
        target_at=_TARGET,
        prepared_at=_PREPARED,
    )

    assert product is not None
    assert product.degraded is False
    assert product.generated_at == _TARGET
    stored = cast(Any, repository.read).product
    assert stored["delivery_timing"]["generated_at"] == _TARGET.isoformat()


def test_post_runtime_identity_rejection_preserves_invocation_fact(tmp_path: Path) -> None:
    repository = ResearchStateRepository(
        tmp_path / "identity-rejection.sqlite3",
        token_secret=b"daily-identity-rejection-secret!!",
    )
    wrong_identity = _Generator(
        product={
            "schema_version": "fin.daily_workspace_product/v1",
            "checkpoint": "close",
            "trading_day_id": _DAY,
            "consultation_status": "completed",
            "agent_provenance": {
                "runtime_invoked_at_generation": True,
                "output_used": True,
            },
            "first_screen": {"top_items": [], "unknowns": []},
            "data_gaps": [],
        }
    )
    result = DailyWorkspaceService(
        consultation_runner=cast(Any, object()),
        state_repository=repository,
    ).scheduled(
        _DAY,
        DailyWorkspaceCheckpoint.MORNING_1000.value,
        principal=_Principal(),
        generator=_TimingBoundGenerator(
            generator=wrong_identity,
            trading_day_id=_DAY,
            checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
            target_at=_TARGET,
            prepared_at=_PREPARED,
            clock=lambda: datetime(2026, 8, 3, 9, 51, tzinfo=_SHANGHAI),
        ),
    )

    assert result.status == "unavailable"
    assert result.data_gaps == ("daily_workspace_generation_identity_invalid",)
    assert result.result_meta.agent_runtime_invoked is True
    assert result.result_meta.agent_output_used is False
    assert (
        repository.find_daily_workspace_version_by_key(
            principal_id=_Principal.principal_id,
            trading_day_id=_DAY,
            idempotency_key="daily:2026-08-03:morning",
        )
        is None
    )


@pytest.mark.parametrize(
    "generator",
    (
        _Generator(include_receipt=False),
        _Generator(consultation_as_of="2026-08-03T09:50:00"),
        _Generator(consultation_as_of=_TARGET.isoformat()),
    ),
)
def test_invalid_evidence_cutoff_receipt_does_not_freeze_a_product(
    generator: _Generator,
) -> None:
    repository = _Repository()
    service = _Service(repository)
    adapter = _adapter(
        service=service,
        repository=repository,
        generator=generator,
        generated_at=datetime(2026, 8, 3, 9, 51, tzinfo=_SHANGHAI),
    )

    with pytest.raises(DailyWorkspaceGenerationUnavailableError) as failure:
        adapter.prepare(
            trading_day_id=_DAY,
            checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
            target_at=_TARGET,
            prepared_at=_PREPARED,
        )

    assert failure.value.data_gaps == ("daily_workspace_evidence_cutoff_unavailable",)
    assert repository.read is None


def test_replayed_evidence_cutoff_may_predate_prepare_start() -> None:
    cutoff = datetime(2026, 8, 3, 9, 30, tzinfo=_SHANGHAI)
    repository = _Repository()
    adapter = _adapter(
        service=_Service(repository),
        repository=repository,
        generator=_Generator(consultation_as_of=cutoff.isoformat()),
        generated_at=datetime(2026, 8, 3, 9, 51, tzinfo=_SHANGHAI),
    )

    product = adapter.prepare(
        trading_day_id=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
        target_at=_TARGET,
        prepared_at=_PREPARED,
    )

    assert product is not None
    assert product.degraded is False
    assert product.evidence_cutoff_at == cutoff


def test_prepare_failure_notice_is_delivery_only() -> None:
    repository = _Repository()
    service = _Service(repository)
    generator = _Generator()
    adapter = _adapter(
        service=service,
        repository=repository,
        generator=generator,
        generated_at=_TARGET,
    )

    with pytest.raises(ValueError, match="failure notice is delivery-only"):
        adapter.prepare_degraded(
            trading_day_id=_DAY,
            checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
            target_at=_TARGET,
            prepared_at=_TARGET,
            reason_code="daily_workspace_preparation_unavailable",
        )

    assert generator.calls == []
    assert repository.read is None


def test_find_prepared_reads_exact_checkpoint_without_running_service_or_generator() -> None:
    repository = _Repository()
    service = _Service(repository)
    generator = _Generator()
    adapter = _adapter(
        service=service,
        repository=repository,
        generator=generator,
        generated_at=datetime(2026, 8, 3, 9, 51, tzinfo=_SHANGHAI),
    )
    adapter.prepare(
        trading_day_id=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
        target_at=_TARGET,
        prepared_at=_PREPARED,
    )
    service.calls.clear()
    generator.calls.clear()

    product = adapter.find_prepared(
        trading_day_id=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
    )

    assert product is not None
    assert product.artifact_hash == "sha256:" + "b" * 64
    assert service.calls == []
    assert generator.calls == []


def test_find_prepared_accepts_normal_product_without_g_receipt() -> None:
    repository = _Repository()
    service = _Service(repository)
    adapter = _adapter(
        service=service,
        repository=repository,
        generator=_Generator(),
        generated_at=datetime(2026, 8, 3, 9, 51, tzinfo=_SHANGHAI),
    )
    prepared = adapter.prepare(
        trading_day_id=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
        target_at=_TARGET,
        prepared_at=_PREPARED,
    )
    assert prepared is not None

    found = adapter.find_prepared(
        trading_day_id=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
    )
    assert found is not None


def test_unavailable_service_fails_without_inventing_a_version() -> None:
    repository = _Repository()
    service = _Service(repository, invoke_generator=False)
    adapter = _adapter(
        service=service,
        repository=repository,
        generator=_Generator(),
        generated_at=_PREPARED,
    )

    with pytest.raises(DailyWorkspacePreparationError):
        adapter.prepare(
            trading_day_id=_DAY,
            checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
            target_at=_TARGET,
            prepared_at=_PREPARED,
        )

    assert repository.read is None


def test_delivery_fallback_fences_a_slow_prepare_claim_in_real_state(
    tmp_path: Path,
) -> None:
    repository = ResearchStateRepository(
        tmp_path / "semantic-state.sqlite3",
        token_secret=b"daily-workspace-test-secret-32-bytes",
    )
    service = DailyWorkspaceService(
        consultation_runner=SimpleNamespace(),
        state_repository=cast(Any, repository),
        clock=lambda: _PREPARED,
    )
    generator = _BlockingGenerator()
    adapter = _adapter(
        service=service,
        repository=repository,
        generator=generator,
        generated_at=_PREPARED,
    )
    prepare_results: list[PreparedDailyWorkspaceProduct | None] = []
    prepare_errors: list[BaseException] = []

    def run_prepare() -> None:
        try:
            prepare_results.append(
                adapter.prepare(
                    trading_day_id=_DAY,
                    checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
                    target_at=_TARGET,
                    prepared_at=_PREPARED,
                )
            )
        except BaseException as error:  # pragma: no cover - surfaced below
            prepare_errors.append(error)

    thread = Thread(target=run_prepare, daemon=True)
    thread.start()
    assert generator.started.wait(timeout=3)
    outbox = _Outbox()
    runner = DailyWorkspaceCheckpointRunner(
        schedule=DailyWorkspaceSchedulePolicy(is_open_date=lambda _day: True),
        products=adapter,
        outbox=outbox,
        clock=lambda: _TARGET,
    )

    try:
        delivered = runner.run(
            DailyWorkspaceCheckpointRunRequest(
                trading_day=_TARGET.date(),
                checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
                phase=DailyWorkspaceRunPhase.DELIVER,
            )
        )
    finally:
        generator.release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert prepare_results == []
    assert len(prepare_errors) == 1
    assert isinstance(prepare_errors[0], DailyWorkspacePreparationError)
    assert delivered.status is DailyWorkspaceRunStatus.FAILURE_NOTICE_DELIVERED
    assert delivered.artifact_hash is not None
    assert delivered.artifact_hash.startswith("sha256:")
    assert len(delivered.artifact_hash) == len("sha256:") + 64
    assert [product.artifact_hash for product in outbox.products] == [delivered.artifact_hash]
    frozen = adapter.find_prepared(
        trading_day_id=_DAY,
        checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
    )
    assert frozen is None
    fallback = repository.find_daily_workspace_version_by_key(
        principal_id=_Principal.principal_id,
        trading_day_id=_DAY,
        idempotency_key=f"daily:{_DAY}:morning:delivery-fallback",
    )
    assert fallback is not None
    assert fallback.artifact_hash == delivered.artifact_hash


def test_delivery_prefers_normal_product_that_wins_before_fallback_parent_cas(
    tmp_path: Path,
) -> None:
    repository = ResearchStateRepository(
        tmp_path / "semantic-state.sqlite3",
        token_secret=b"daily-workspace-test-secret-32-bytes",
    )
    service = DailyWorkspaceService(
        consultation_runner=SimpleNamespace(),
        state_repository=cast(Any, repository),
        clock=lambda: _PREPARED,
    )
    normal = _adapter(
        service=service,
        repository=repository,
        generator=_Generator(consultation_as_of=_PREPARED.isoformat()),
        generated_at=_PREPARED,
    )
    racing_repository = _FinishNormalBeforeParentReadRepository(
        repository,
        lambda: normal.prepare(
            trading_day_id=_DAY,
            checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
            target_at=_TARGET,
            prepared_at=_PREPARED,
        ),
    )
    delivery = _adapter(
        service=service,
        repository=racing_repository,
        generator=_UnavailableGenerator(),
        generated_at=_TARGET,
    )
    outbox = _Outbox()

    result = DailyWorkspaceCheckpointRunner(
        schedule=DailyWorkspaceSchedulePolicy(is_open_date=lambda _day: True),
        products=delivery,
        outbox=outbox,
        clock=lambda: _TARGET,
    ).run(
        DailyWorkspaceCheckpointRunRequest(
            trading_day=_TARGET.date(),
            checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
            phase=DailyWorkspaceRunPhase.DELIVER,
        )
    )

    assert racing_repository.normal_product is not None
    assert result.status is DailyWorkspaceRunStatus.DELIVERED
    assert result.artifact_hash == racing_repository.normal_product.artifact_hash
    latest = repository.find_daily_workspace(
        principal_id=_Principal.principal_id,
        trading_day_id=_DAY,
    )
    assert latest is not None
    assert latest.product_version == 1


def test_delivery_fallback_releases_its_claim_when_parent_read_fails(
    tmp_path: Path,
) -> None:
    repository = ResearchStateRepository(
        tmp_path / "semantic-state.sqlite3",
        token_secret=b"daily-workspace-test-secret-32-bytes",
    )
    service = DailyWorkspaceService(
        consultation_runner=SimpleNamespace(),
        state_repository=cast(Any, repository),
        clock=lambda: _PREPARED,
    )
    adapter = _adapter(
        service=service,
        repository=_FailParentReadRepository(repository),
        generator=_UnavailableGenerator(),
        generated_at=_TARGET,
    )

    with pytest.raises(RuntimeError, match="parent read failed"):
        adapter.prepare_degraded(
            trading_day_id=_DAY,
            checkpoint=DailyWorkspaceCheckpoint.MORNING_1000,
            target_at=_TARGET,
            prepared_at=_TARGET,
            reason_code="daily_workspace_prepared_product_missing",
        )

    fallback_key = f"daily:{_DAY}:morning:delivery-fallback"
    assert repository.acquire_daily_workspace_checkpoint(
        principal_id=_Principal.principal_id,
        trading_day_id=_DAY,
        idempotency_key=fallback_key,
        now=_TARGET.timestamp(),
    )
