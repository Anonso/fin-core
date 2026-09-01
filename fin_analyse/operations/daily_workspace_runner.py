"""Schedule-bound prepare/deliver runner for Daily Workspace checkpoints.

The expensive product preparation and the user-visible delivery are separate
invocations. Delivery only consumes an already frozen consultation result. If
none exists, it may send one deterministic failure notice, but that notice is
never counted as a consultation result and never starts the Agent at target.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol

from fin_analyse.consultation.daily_workspace_product_contracts import (
    DailyWorkspaceCheckpoint,
    is_public_daily_workspace_product,
)
from fin_analyse.consultation.daily_workspace_schedule import (
    SHANGHAI_TZ,
    DailyWorkspaceSchedulePolicy,
)
from fin_analyse.operations.daily_workspace_generator import (
    DailyWorkspaceGenerationUnavailableError,
)

_ARTIFACT_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")


class DailyWorkspaceRunPhase(StrEnum):
    PREPARE = "prepare"
    DELIVER = "deliver"


class DailyWorkspaceRunStatus(StrEnum):
    NOT_TRADING_DAY = "NOT_TRADING_DAY"
    NOT_DUE = "NOT_DUE"
    WINDOW_MISSED = "WINDOW_MISSED"
    PREPARED = "PREPARED"
    DELIVERED = "DELIVERED"
    FAILURE_NOTICE_DELIVERED = "FAILURE_NOTICE_DELIVERED"
    ALREADY_DELIVERED = "ALREADY_DELIVERED"


class DailyWorkspacePreparationError(RuntimeError):
    """A prepare attempt failed without freezing a consultation product."""

    def __init__(self, data_gaps: tuple[str, ...]) -> None:
        self.data_gaps = tuple(
            dict.fromkeys(gap for gap in data_gaps if isinstance(gap, str) and gap)
        ) or ("daily_workspace_preparation_unavailable",)
        self.code = self.data_gaps[0]
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class DailyWorkspaceCheckpointRunRequest:
    trading_day: date
    checkpoint: DailyWorkspaceCheckpoint
    phase: DailyWorkspaceRunPhase
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedDailyWorkspaceProduct:
    """Exact immutable workspace version consumed by delivery."""

    trading_day_id: str
    checkpoint: DailyWorkspaceCheckpoint
    workspace_ref: str
    product_version: int
    artifact_hash: str
    target_at: datetime
    prepared_at: datetime
    generated_at: datetime
    evidence_cutoff_at: datetime | None
    degraded: bool = False
    data_gaps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DailyWorkspaceDeliveryReceipt:
    artifact_hash: str
    delivered_at: datetime
    already_delivered: bool = False
    # B0: dispatch acceptance 事实——平台接受发送返回的 message_id（可空；
    # None = 未取得/OUTCOME_UNKNOWN，不得冒充 delivery）。
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class DailyWorkspaceCheckpointRunResult:
    phase: DailyWorkspaceRunPhase
    status: DailyWorkspaceRunStatus
    trading_day_id: str
    checkpoint: DailyWorkspaceCheckpoint
    prepare_at: datetime
    target_at: datetime
    workspace_ref: str | None = None
    product_version: int | None = None
    artifact_hash: str | None = None
    prepared_at: datetime | None = None
    generated_at: datetime | None = None
    evidence_cutoff_at: datetime | None = None
    delivered_at: datetime | None = None
    data_gaps: tuple[str, ...] = ()


class DailyWorkspaceProductPort(Protocol):
    """Prepare and retrieve versions owned by the Daily Workspace state chain."""

    def prepare(
        self,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
        target_at: datetime,
        prepared_at: datetime,
    ) -> PreparedDailyWorkspaceProduct | None: ...

    def find_prepared(
        self,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
    ) -> PreparedDailyWorkspaceProduct | None: ...

    def prepare_degraded(
        self,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
        target_at: datetime,
        prepared_at: datetime,
        reason_code: str,
    ) -> PreparedDailyWorkspaceProduct: ...


class DailyWorkspaceDeliveryOutboxPort(Protocol):
    """Idempotently enqueue/send one exact immutable product artifact."""

    def dispatch(
        self,
        product: PreparedDailyWorkspaceProduct,
        *,
        delivered_at: datetime,
    ) -> DailyWorkspaceDeliveryReceipt: ...


class _ScheduledWorkspaceService(Protocol):
    def scheduled(
        self,
        trading_day_id: str,
        checkpoint: str,
        *,
        principal: object,
        generator: object,
    ) -> object: ...


class _WorkspaceVersionRepository(Protocol):
    def create_daily_workspace_chain(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        now: float,
    ) -> object: ...

    def find_daily_workspace(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
    ) -> object | None: ...

    def find_daily_workspace_version_by_key(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
    ) -> object | None: ...

    def acquire_daily_workspace_checkpoint(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        now: float,
    ) -> bool: ...

    def release_daily_workspace_checkpoint(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
    ) -> None: ...

    def append_daily_workspace_version(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        contract: Mapping[str, object] | str,
        input_snapshot: object,
        expected_parent_product_version: int,
        status: str,
        product: Mapping[str, object],
        now: float,
        data_gaps: tuple[str, ...] = (),
    ) -> object: ...

    def finalize_scheduled_checkpoint(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
        checkpoint: str,
        product: Mapping[str, object],
        now: float,
        expected_parent_product_version: int = 0,
    ) -> object: ...


class _WorkspaceGenerator(Protocol):
    def generate(self, *, snapshot: object, principal: object) -> object: ...


@dataclass(frozen=True, slots=True)
class _TimingBoundGenerator:
    generator: _WorkspaceGenerator
    trading_day_id: str
    checkpoint: DailyWorkspaceCheckpoint
    target_at: datetime
    prepared_at: datetime
    clock: Callable[[], datetime]

    def generate(self, *, snapshot: object, principal: object) -> object:
        timed_snapshot = (
            {
                **snapshot,
                "daily_workspace_deadline_at": self.target_at.isoformat(),
            }
            if isinstance(snapshot, Mapping)
            else snapshot
        )
        generated = self.generator.generate(snapshot=timed_snapshot, principal=principal)
        generated_at = _aware_shanghai(self.clock())
        # 2026-09-01（owner 拍板）：晚于推送点完成的结果不再拒绝落库；
        # delivery 会等待 prepare 结束并在结果可用时立刻投递（generated_at
        # 与 target_at 分开记录，晚到的事实不会被冒充成准点）。
        if not isinstance(generated, dict):
            raise DailyWorkspaceGenerationUnavailableError(("daily_workspace_generation_invalid",))
        if (
            generated.get("trading_day_id") != self.trading_day_id
            or generated.get("checkpoint") != self.checkpoint.value
        ):
            raise DailyWorkspaceGenerationUnavailableError(
                ("daily_workspace_generation_identity_invalid",),
                agent_runtime_invoked=_generated_agent_runtime_invoked(generated),
            )
        evidence_cutoff_at = _normal_evidence_cutoff(
            generated,
            generated_at=generated_at,
        )
        if evidence_cutoff_at is None:
            raise DailyWorkspaceGenerationUnavailableError(
                ("daily_workspace_evidence_cutoff_unavailable",),
                agent_runtime_invoked=_generated_agent_runtime_invoked(generated),
            )
        return {
            **generated,
            "degraded": False,
            "delivery_timing": _delivery_timing(
                target_at=self.target_at,
                prepared_at=self.prepared_at,
                generated_at=generated_at,
                evidence_cutoff_at=evidence_cutoff_at,
            ),
        }


class DailyWorkspaceStateProductAdapter:
    """Bind the runner to the existing DailyWorkspaceService/state owner."""

    def __init__(
        self,
        *,
        service: _ScheduledWorkspaceService,
        repository: _WorkspaceVersionRepository,
        generator: _WorkspaceGenerator,
        principal: object,
        clock: Callable[[], datetime],
    ) -> None:
        principal_id = getattr(principal, "principal_id", None)
        if not isinstance(principal_id, str) or not principal_id:
            raise ValueError("daily workspace production principal is invalid")
        self._service = service
        self._repository = repository
        self._generator = generator
        self._principal = principal
        self._principal_id = principal_id
        self._clock = clock

    def prepare(
        self,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
        target_at: datetime,
        prepared_at: datetime,
    ) -> PreparedDailyWorkspaceProduct | None:
        return self._schedule_and_read(
            trading_day_id=trading_day_id,
            checkpoint=checkpoint,
            generator=_TimingBoundGenerator(
                generator=self._generator,
                trading_day_id=trading_day_id,
                checkpoint=checkpoint,
                target_at=target_at,
                prepared_at=prepared_at,
                clock=self._clock,
            ),
        )

    def find_prepared(
        self,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
    ) -> PreparedDailyWorkspaceProduct | None:
        return self._find_prepared_by_key(
            trading_day_id=trading_day_id,
            checkpoint=checkpoint,
            idempotency_key=_checkpoint_key(trading_day_id, checkpoint),
        )

    def _find_prepared_by_key(
        self,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
        idempotency_key: str,
    ) -> PreparedDailyWorkspaceProduct | None:
        read = self._repository.find_daily_workspace_version_by_key(
            principal_id=self._principal_id,
            trading_day_id=trading_day_id,
            idempotency_key=idempotency_key,
        )
        return (
            None
            if read is None
            else _project_prepared_version(
                read,
                trading_day_id=trading_day_id,
                checkpoint=checkpoint,
            )
        )

    def prepare_degraded(
        self,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
        target_at: datetime,
        prepared_at: datetime,
        reason_code: str,
    ) -> PreparedDailyWorkspaceProduct:
        if reason_code != "daily_workspace_prepared_product_missing":
            raise ValueError("daily workspace failure notice is delivery-only")
        return self._freeze_delivery_fallback(
            trading_day_id=trading_day_id,
            checkpoint=checkpoint,
            target_at=target_at,
            prepared_at=prepared_at,
            reason_code=reason_code,
        )

    def _freeze_delivery_fallback(
        self,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
        target_at: datetime,
        prepared_at: datetime,
        reason_code: str,
    ) -> PreparedDailyWorkspaceProduct:
        existing = self.find_prepared(
            trading_day_id=trading_day_id,
            checkpoint=checkpoint,
        )
        if existing is not None:
            return existing

        prepared_at = _aware_shanghai(prepared_at)
        fallback_key = _delivery_fallback_key(trading_day_id, checkpoint)
        existing_fallback = self._find_prepared_by_key(
            trading_day_id=trading_day_id,
            checkpoint=checkpoint,
            idempotency_key=fallback_key,
        )
        if existing_fallback is not None:
            return existing_fallback
        self._repository.create_daily_workspace_chain(
            principal_id=self._principal_id,
            trading_day_id=trading_day_id,
            idempotency_key=f"daily:{trading_day_id}:init",
            now=prepared_at.timestamp(),
        )
        acquired = self._repository.acquire_daily_workspace_checkpoint(
            principal_id=self._principal_id,
            trading_day_id=trading_day_id,
            idempotency_key=fallback_key,
            now=prepared_at.timestamp(),
        )
        if not acquired:
            existing = self.find_prepared(
                trading_day_id=trading_day_id,
                checkpoint=checkpoint,
            )
            if existing is not None:
                return existing
            existing_fallback = self._find_prepared_by_key(
                trading_day_id=trading_day_id,
                checkpoint=checkpoint,
                idempotency_key=fallback_key,
            )
            if existing_fallback is not None:
                return existing_fallback
            raise RuntimeError("daily workspace delivery fallback is in progress")

        try:
            parent = self._repository.find_daily_workspace(
                principal_id=self._principal_id,
                trading_day_id=trading_day_id,
            )
            canonical = self._repository.find_daily_workspace_version_by_key(
                principal_id=self._principal_id,
                trading_day_id=trading_day_id,
                idempotency_key=_checkpoint_key(trading_day_id, checkpoint),
            )
            if canonical is not None:
                return _project_prepared_version(
                    canonical,
                    trading_day_id=trading_day_id,
                    checkpoint=checkpoint,
                )
            parent_version = getattr(parent, "product_version", 0) if parent is not None else 0
            parent_hash = getattr(parent, "artifact_hash", None) if parent is not None else None
            snapshot = {
                "schema": "fin.daily-workspace-input-snapshot/v1",
                "trading_day_id": trading_day_id,
                "checkpoint": checkpoint.value,
                "parent_product_version": parent_version,
                "parent_artifact_hash": parent_hash,
            }
            product = _degraded_product_payload(
                snapshot=snapshot,
                trading_day_id=trading_day_id,
                checkpoint=checkpoint,
                target_at=target_at,
                prepared_at=prepared_at,
                generated_at=prepared_at,
                data_gaps=(reason_code,),
            )
            try:
                read = self._repository.finalize_scheduled_checkpoint(
                    principal_id=self._principal_id,
                    trading_day_id=trading_day_id,
                    idempotency_key=fallback_key,
                    checkpoint=checkpoint.value,
                    product=product,
                    now=prepared_at.timestamp(),
                    expected_parent_product_version=parent_version,
                )
            except Exception as error:
                if getattr(error, "code", None) in {
                    "continuation_conflict",
                    "idempotency_conflict",
                }:
                    existing = self.find_prepared(
                        trading_day_id=trading_day_id,
                        checkpoint=checkpoint,
                    )
                    if existing is not None:
                        return existing
                    existing_fallback = self._find_prepared_by_key(
                        trading_day_id=trading_day_id,
                        checkpoint=checkpoint,
                        idempotency_key=fallback_key,
                    )
                    if existing_fallback is not None:
                        return existing_fallback
                raise
            return _project_prepared_version(
                getattr(read, "read", read),
                trading_day_id=trading_day_id,
                checkpoint=checkpoint,
            )
        finally:
            self._repository.release_daily_workspace_checkpoint(
                principal_id=self._principal_id,
                trading_day_id=trading_day_id,
                idempotency_key=fallback_key,
            )

    def _schedule_and_read(
        self,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
        generator: object,
    ) -> PreparedDailyWorkspaceProduct | None:
        result = self._service.scheduled(
            trading_day_id,
            checkpoint.value,
            principal=self._principal,
            generator=generator,
        )
        if getattr(result, "status", None) not in {"completed", "partial"}:
            gaps = getattr(result, "data_gaps", ())
            raise DailyWorkspacePreparationError(
                tuple(gaps) if isinstance(gaps, (list, tuple)) else ()
            )
        read = self._repository.find_daily_workspace_version_by_key(
            principal_id=self._principal_id,
            trading_day_id=trading_day_id,
            idempotency_key=_checkpoint_key(trading_day_id, checkpoint),
        )
        if read is None:
            raise RuntimeError("daily workspace service reported success without a version")
        return _project_prepared_version(
            read,
            trading_day_id=trading_day_id,
            checkpoint=checkpoint,
        )


def _checkpoint_key(trading_day_id: str, checkpoint: DailyWorkspaceCheckpoint) -> str:
    return f"daily:{trading_day_id}:{checkpoint.value}"


def _delivery_fallback_key(
    trading_day_id: str,
    checkpoint: DailyWorkspaceCheckpoint,
) -> str:
    return f"{_checkpoint_key(trading_day_id, checkpoint)}:delivery-fallback"


def _aware_shanghai(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("daily workspace product clock must be timezone-aware")
    return value.astimezone(SHANGHAI_TZ)


def _normal_evidence_cutoff(
    generated: dict[str, object],
    *,
    generated_at: datetime,
) -> datetime | None:
    receipt = generated.get("input_snapshot_receipt")
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "fin.daily-workspace-input-receipt/v1"
    ):
        return None
    value = receipt.get("consultation_as_of")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        return None
    cutoff = parsed.astimezone(SHANGHAI_TZ)
    return cutoff if cutoff <= generated_at else None


def _delivery_timing(
    *,
    target_at: datetime,
    prepared_at: datetime,
    generated_at: datetime,
    evidence_cutoff_at: datetime | None,
) -> dict[str, object]:
    return {
        "schema": "fin.daily-workspace-delivery-timing/v1",
        "target_at": target_at.isoformat(),
        "prepared_at": prepared_at.isoformat(),
        "generated_at": generated_at.isoformat(),
        "evidence_cutoff_at": (
            None if evidence_cutoff_at is None else evidence_cutoff_at.isoformat()
        ),
    }


def _degraded_product_payload(
    *,
    snapshot: object,
    trading_day_id: str,
    checkpoint: DailyWorkspaceCheckpoint,
    target_at: datetime,
    prepared_at: datetime,
    generated_at: datetime,
    data_gaps: tuple[str, ...],
    agent_runtime_invoked_at_generation: bool = False,
) -> dict[str, Any]:
    parent_artifact_hash = (
        snapshot.get("parent_artifact_hash") if isinstance(snapshot, dict) else None
    )
    gaps = tuple(dict.fromkeys(gap for gap in data_gaps if isinstance(gap, str) and gap))
    if not gaps:
        gaps = ("daily_workspace_preparation_unavailable",)
    return {
        "schema_version": "fin.daily_workspace_product/v1",
        "checkpoint": checkpoint.value,
        "trading_day_id": trading_day_id,
        "origin": "scheduled",
        "parent_artifact_hash": parent_artifact_hash,
        "generated_via": "deterministic-degraded-v1",
        "consultation_status": "partial",
        "agent_provenance": {
            "runtime_invoked_at_generation": agent_runtime_invoked_at_generation,
            "output_used": False,
        },
        "degraded": True,
        "delivery_timing": _delivery_timing(
            target_at=target_at,
            prepared_at=prepared_at,
            generated_at=generated_at,
            evidence_cutoff_at=None,
        ),
        "first_screen": {
            "top_items": [],
            "rationale": [],
            "changes_vs_previous": [],
            "unknowns": list(gaps),
            "portfolio_review": [],
        },
        "data_gaps": list(gaps),
        "consultation_product": None,
    }


def _generated_agent_runtime_invoked(product: object) -> bool:
    if not isinstance(product, dict):
        return False
    provenance = product.get("agent_provenance")
    return isinstance(provenance, dict) and provenance.get("runtime_invoked_at_generation") is True


def _project_prepared_version(
    read: object,
    *,
    trading_day_id: str,
    checkpoint: DailyWorkspaceCheckpoint,
) -> PreparedDailyWorkspaceProduct:
    product = getattr(read, "product", None)
    if not isinstance(product, dict):
        raise ValueError("daily workspace stored product is invalid")
    if not is_public_daily_workspace_product(product):
        raise ValueError("daily workspace stored product G context is unverified")
    timing = product.get("delivery_timing")
    if (
        not isinstance(timing, dict)
        or timing.get("schema") != "fin.daily-workspace-delivery-timing/v1"
        or product.get("trading_day_id") != trading_day_id
        or product.get("checkpoint") != checkpoint.value
    ):
        raise ValueError("daily workspace stored product timing is invalid")
    gaps_value = product.get("data_gaps", ())
    if not isinstance(gaps_value, (list, tuple)) or any(
        not isinstance(gap, str) or not gap for gap in gaps_value
    ):
        raise ValueError("daily workspace stored product gaps are invalid")
    workspace_ref = getattr(read, "workspace_ref", None)
    product_version = getattr(read, "product_version", None)
    artifact_hash = getattr(read, "artifact_hash", None)
    if (
        not isinstance(workspace_ref, str)
        or not workspace_ref
        or not isinstance(product_version, int)
        or isinstance(product_version, bool)
        or product_version < 1
        or not isinstance(artifact_hash, str)
        or _ARTIFACT_HASH.fullmatch(artifact_hash) is None
    ):
        raise ValueError("daily workspace stored version identity is invalid")
    degraded = product.get("degraded") is True
    evidence_cutoff_at = _parse_optional_timing(timing, "evidence_cutoff_at")
    if degraded is (evidence_cutoff_at is not None):
        raise ValueError("daily workspace stored product timing is invalid")
    return PreparedDailyWorkspaceProduct(
        trading_day_id=trading_day_id,
        checkpoint=checkpoint,
        workspace_ref=workspace_ref,
        product_version=product_version,
        artifact_hash=artifact_hash,
        target_at=_parse_timing(timing, "target_at"),
        prepared_at=_parse_timing(timing, "prepared_at"),
        generated_at=_parse_timing(timing, "generated_at"),
        evidence_cutoff_at=evidence_cutoff_at,
        degraded=degraded,
        data_gaps=tuple(gaps_value),
    )


def _parse_timing(timing: dict[str, object], field: str) -> datetime:
    value = timing.get(field)
    if not isinstance(value, str):
        raise ValueError("daily workspace stored product timing is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("daily workspace stored product timing is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError("daily workspace stored product timing is invalid")
    return parsed


def _parse_optional_timing(
    timing: dict[str, object],
    field: str,
) -> datetime | None:
    if field not in timing:
        raise ValueError("daily workspace stored product timing is invalid")
    return None if timing[field] is None else _parse_timing(timing, field)


class DailyWorkspaceCheckpointRunner:
    """Run one FIN-owned prepare or delivery checkpoint invocation."""

    def __init__(
        self,
        *,
        schedule: DailyWorkspaceSchedulePolicy,
        products: DailyWorkspaceProductPort,
        outbox: DailyWorkspaceDeliveryOutboxPort,
        clock: Callable[[], datetime],
        wait_for_result: Callable[[], None] | None = None,
    ) -> None:
        self._schedule = schedule
        self._products = products
        self._outbox = outbox
        self._clock = clock
        self._wait_for_result = wait_for_result

    def run(
        self,
        request: DailyWorkspaceCheckpointRunRequest,
    ) -> DailyWorkspaceCheckpointRunResult:
        if not isinstance(request, DailyWorkspaceCheckpointRunRequest):
            raise TypeError("daily workspace run request is invalid")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("daily workspace runner clock must be timezone-aware")
        now = now.astimezone(SHANGHAI_TZ)
        trading_day_id = request.trading_day.isoformat()
        prepare_at = self._schedule.prepare_at(request.trading_day, request.checkpoint)
        target_at = self._schedule.target_at(request.trading_day, request.checkpoint)

        if not self._schedule.is_trading_day(request.trading_day):
            return self._empty_result(
                request,
                DailyWorkspaceRunStatus.NOT_TRADING_DAY,
                prepare_at=prepare_at,
                target_at=target_at,
            )
        if request.phase is DailyWorkspaceRunPhase.PREPARE:
            if now < prepare_at:
                status = DailyWorkspaceRunStatus.NOT_DUE
            elif now >= target_at:
                status = DailyWorkspaceRunStatus.WINDOW_MISSED
            else:
                return self._prepare(
                    request,
                    trading_day_id=trading_day_id,
                    prepare_at=prepare_at,
                    target_at=target_at,
                    now=now,
                )
            return self._empty_result(
                request,
                status,
                prepare_at=prepare_at,
                target_at=target_at,
            )

        if now < target_at:
            status = DailyWorkspaceRunStatus.NOT_DUE
        elif not self._schedule.in_window(request.checkpoint, now):
            status = DailyWorkspaceRunStatus.WINDOW_MISSED
        else:
            return self._deliver(
                request,
                trading_day_id=trading_day_id,
                prepare_at=prepare_at,
                target_at=target_at,
                now=now,
            )
        return self._empty_result(
            request,
            status,
            prepare_at=prepare_at,
            target_at=target_at,
        )

    def _prepare(
        self,
        request: DailyWorkspaceCheckpointRunRequest,
        *,
        trading_day_id: str,
        prepare_at: datetime,
        target_at: datetime,
        now: datetime,
    ) -> DailyWorkspaceCheckpointRunResult:
        product = self._products.prepare(
            trading_day_id=trading_day_id,
            checkpoint=request.checkpoint,
            target_at=target_at,
            prepared_at=now,
        )
        if product is None:
            raise DailyWorkspacePreparationError(("daily_workspace_preparation_unavailable",))
        self._validate_product(
            product,
            trading_day_id=trading_day_id,
            checkpoint=request.checkpoint,
            target_at=target_at,
        )
        if product.degraded:
            raise DailyWorkspacePreparationError(product.data_gaps)
        return self._product_result(
            request,
            product,
            status=DailyWorkspaceRunStatus.PREPARED,
            prepare_at=prepare_at,
            target_at=target_at,
        )

    def _deliver(
        self,
        request: DailyWorkspaceCheckpointRunRequest,
        *,
        trading_day_id: str,
        prepare_at: datetime,
        target_at: datetime,
        now: datetime,
    ) -> DailyWorkspaceCheckpointRunResult:
        product = self._products.find_prepared(
            trading_day_id=trading_day_id,
            checkpoint=request.checkpoint,
        )
        if product is None and self._wait_for_result is not None:
            # 到推送点时结果未就绪：等 prepare 结束（成功冻结或失败退出），
            # 结果一出立刻投递；prepare 确实失败后才发失败通知。
            self._wait_for_result()
            now = _aware_shanghai(self._clock())
            product = self._products.find_prepared(
                trading_day_id=trading_day_id,
                checkpoint=request.checkpoint,
            )
        if product is None:
            product = self._products.prepare_degraded(
                trading_day_id=trading_day_id,
                checkpoint=request.checkpoint,
                target_at=target_at,
                prepared_at=now,
                reason_code="daily_workspace_prepared_product_missing",
            )
        self._validate_product(
            product,
            trading_day_id=trading_day_id,
            checkpoint=request.checkpoint,
            target_at=target_at,
        )
        receipt = self._outbox.dispatch(product, delivered_at=now)
        if receipt.artifact_hash != product.artifact_hash:
            raise ValueError("daily workspace delivery receipt artifact mismatch")
        if receipt.delivered_at.tzinfo is None or receipt.delivered_at.utcoffset() is None:
            raise ValueError("daily workspace delivery receipt time must be timezone-aware")
        if product.degraded:
            # The transport ACK only proves that the failure notice arrived;
            # it never turns the failed consultation into a successful result.
            status = DailyWorkspaceRunStatus.FAILURE_NOTICE_DELIVERED
        elif receipt.already_delivered:
            status = DailyWorkspaceRunStatus.ALREADY_DELIVERED
        else:
            status = DailyWorkspaceRunStatus.DELIVERED
        return self._product_result(
            request,
            product,
            status=status,
            prepare_at=prepare_at,
            target_at=target_at,
            delivered_at=receipt.delivered_at,
        )

    @staticmethod
    def _validate_product(
        product: PreparedDailyWorkspaceProduct,
        *,
        trading_day_id: str,
        checkpoint: DailyWorkspaceCheckpoint,
        target_at: datetime,
    ) -> None:
        if not isinstance(product, PreparedDailyWorkspaceProduct):
            raise TypeError("daily workspace prepared product is invalid")
        if (
            product.trading_day_id != trading_day_id
            or product.checkpoint is not checkpoint
            or product.target_at != target_at
            or not product.workspace_ref
            or product.product_version < 1
            or _ARTIFACT_HASH.fullmatch(product.artifact_hash) is None
        ):
            raise ValueError("daily workspace prepared product identity mismatch")
        for value in (
            product.target_at,
            product.prepared_at,
            product.generated_at,
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("daily workspace product times must be timezone-aware")
        if product.degraded:
            if product.evidence_cutoff_at is not None:
                raise ValueError("degraded daily workspace must not claim evidence cutoff")
        elif (
            product.evidence_cutoff_at is None
            or product.evidence_cutoff_at.tzinfo is None
            or product.evidence_cutoff_at.utcoffset() is None
            or product.evidence_cutoff_at > product.generated_at
        ):
            raise ValueError("normal daily workspace evidence cutoff is invalid")

    @staticmethod
    def _empty_result(
        request: DailyWorkspaceCheckpointRunRequest,
        status: DailyWorkspaceRunStatus,
        *,
        prepare_at: datetime,
        target_at: datetime,
    ) -> DailyWorkspaceCheckpointRunResult:
        return DailyWorkspaceCheckpointRunResult(
            phase=request.phase,
            status=status,
            trading_day_id=request.trading_day.isoformat(),
            checkpoint=request.checkpoint,
            prepare_at=prepare_at,
            target_at=target_at,
        )

    @staticmethod
    def _product_result(
        request: DailyWorkspaceCheckpointRunRequest,
        product: PreparedDailyWorkspaceProduct,
        *,
        status: DailyWorkspaceRunStatus,
        prepare_at: datetime,
        target_at: datetime,
        delivered_at: datetime | None = None,
    ) -> DailyWorkspaceCheckpointRunResult:
        return DailyWorkspaceCheckpointRunResult(
            phase=request.phase,
            status=status,
            trading_day_id=product.trading_day_id,
            checkpoint=product.checkpoint,
            prepare_at=prepare_at,
            target_at=target_at,
            workspace_ref=product.workspace_ref,
            product_version=product.product_version,
            artifact_hash=product.artifact_hash,
            prepared_at=product.prepared_at,
            generated_at=product.generated_at,
            evidence_cutoff_at=product.evidence_cutoff_at,
            delivered_at=delivered_at,
            data_gaps=product.data_gaps,
        )
