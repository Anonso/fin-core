#!/usr/bin/env python3
"""Run or preview one FIN Daily Workspace prepare/delivery checkpoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import time as _time_module
from collections.abc import Callable, Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Protocol, cast

from fin_analyse.consultation.daily_workspace_product_contracts import (
    DailyWorkspaceCheckpoint,
)
from fin_analyse.consultation.daily_workspace_schedule import (
    SHANGHAI_TZ,
    DailyWorkspaceSchedulePolicy,
)
from fin_analyse.operations.daily_workspace_runner import (
    DailyWorkspaceCheckpointRunner,
    DailyWorkspaceCheckpointRunRequest,
    DailyWorkspaceCheckpointRunResult,
    DailyWorkspaceRunPhase,
    DailyWorkspaceRunStatus,
)

_SCHEMA = "fin.daily-workspace-checkpoint-run/v1"
_DELIVERY_TARGET_ENV = "FIN_DAILY_WORKSPACE_DELIVERY_TARGET"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CALENDAR_PATH = _PROJECT_ROOT / "config" / "market" / "a_share_calendar_2026.json"
_REASON_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}\Z")
_EXPLICIT_NOT_SENT_EXIT_CODE = 75
_PREPARE_WAIT_POLL_SECONDS = 5
_PREPARE_STATE_TIMEOUT_SECONDS = 10.0


class _Runner(Protocol):
    def run(
        self, request: DailyWorkspaceCheckpointRunRequest
    ) -> DailyWorkspaceCheckpointRunResult: ...


RunnerFactory = Callable[..., _Runner]


class _DailyWorkspaceCompositionError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _generation_now() -> datetime:
    """Return the physical completion clock for a prepared product.

    The scheduled runner freezes its entry clock to decide whether a checkpoint
    is due. Reusing that start instant as ``generated_at`` makes evidence
    produced during the permitted preparation interval look artificially late.
    """

    return datetime.now(SHANGHAI_TZ)


def _canonical_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("trade date must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("trade date must be canonical YYYY-MM-DD")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=_canonical_date)
    parser.add_argument(
        "--checkpoint",
        required=True,
        choices=tuple(item.value for item in DailyWorkspaceCheckpoint),
    )
    parser.add_argument(
        "--phase",
        required=True,
        choices=tuple(item.value for item in DailyWorkspaceRunPhase),
    )
    effect = parser.add_mutually_exclusive_group()
    effect.add_argument("--execute-prepare", action="store_true")
    effect.add_argument("--execute-delivery", action="store_true")
    return parser


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _result_payload(
    result: DailyWorkspaceCheckpointRunResult,
    *,
    effect_allowed: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": _SCHEMA,
        "status": result.status.value,
        "effect_allowed": effect_allowed,
        "phase": result.phase.value,
        "trading_day_id": result.trading_day_id,
        "checkpoint": result.checkpoint.value,
        "prepare_at": result.prepare_at.isoformat(),
        "target_at": result.target_at.isoformat(),
        "data_gaps": list(result.data_gaps),
    }
    for field, value in (
        ("workspace_ref", result.workspace_ref),
        ("product_version", result.product_version),
        ("artifact_hash", result.artifact_hash),
        ("prepared_at", _iso(result.prepared_at)),
        ("generated_at", _iso(result.generated_at)),
        ("evidence_cutoff_at", _iso(result.evidence_cutoff_at)),
        ("delivered_at", _iso(result.delivered_at)),
    ):
        if value is not None:
            payload[field] = value
    return payload


def _preview_payload(
    *,
    trading_day: date,
    checkpoint: DailyWorkspaceCheckpoint,
    phase: DailyWorkspaceRunPhase,
) -> dict[str, object]:
    schedule = DailyWorkspaceSchedulePolicy()
    return {
        "schema_version": _SCHEMA,
        "status": "PLANNED",
        "effect_allowed": False,
        "phase": phase.value,
        "trading_day_id": trading_day.isoformat(),
        "checkpoint": checkpoint.value,
        "prepare_at": schedule.prepare_at(trading_day, checkpoint).isoformat(),
        "target_at": schedule.target_at(trading_day, checkpoint).isoformat(),
    }


def _failure_payload(
    *,
    trading_day: date,
    checkpoint: DailyWorkspaceCheckpoint,
    phase: DailyWorkspaceRunPhase,
    error: Exception,
) -> dict[str, object]:
    candidate = getattr(error, "code", None)
    reason_code = (
        candidate
        if isinstance(candidate, str) and _REASON_CODE.fullmatch(candidate)
        else "daily_workspace_runtime_failed"
    )
    return {
        "schema_version": _SCHEMA,
        "status": "FAILED",
        "reason_code": reason_code,
        "effect_allowed": True,
        "phase": phase.value,
        "trading_day_id": trading_day.isoformat(),
        "checkpoint": checkpoint.value,
    }


class _ForbiddenConsultationRunner:
    def handle(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("daily_workspace_delivery_must_not_invoke_consultation")


class _ForbiddenGenerator:
    def generate(self, **_kwargs: object) -> object:
        raise RuntimeError("daily_workspace_delivery_must_not_generate")


class _ForbiddenOutbox:
    def dispatch(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("daily_workspace_prepare_must_not_deliver")


def _prepare_unit_active(unit: str) -> bool:
    """True while the scheduled prepare oneshot is still activating/active.

    ``systemctl is-active`` treats Type=oneshot's ``activating`` state as
    non-active, so the ActiveState property is read instead.  Any failure to
    determine the state fails closed (treated as not running) so delivery
    falls back to the historical failure-notice behavior instead of hanging.
    """

    try:
        completed = subprocess.run(
            ("systemctl", "--user", "show", unit, "-p", "ActiveState", "--value"),
            capture_output=True,
            timeout=_PREPARE_STATE_TIMEOUT_SECONDS,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.stdout.strip() in {"activating", "active"}


def _wait_for_prepare_result(checkpoint: DailyWorkspaceCheckpoint) -> Callable[[], None]:
    """Block until the prepare unit is no longer running (result frozen or failed)."""

    unit = f"fin-daily-workspace-prepare@{checkpoint.value}.service"

    def wait_for_result() -> None:
        while _prepare_unit_active(unit):
            _time_module.sleep(_PREPARE_WAIT_POLL_SECONDS)

    return wait_for_result


def _read_owner_secret(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise _DailyWorkspaceCompositionError("daily_workspace_state_insecure")
        secret = os.read(descriptor, 33)
        if len(secret) != 32:
            raise _DailyWorkspaceCompositionError("daily_workspace_state_insecure")
        return secret
    finally:
        os.close(descriptor)


def _require_owner_path(path: Path, *, mode: int, directory: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise _DailyWorkspaceCompositionError("daily_workspace_state_unavailable") from error
    valid_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not valid_type or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != mode:
        raise _DailyWorkspaceCompositionError("daily_workspace_state_insecure")


def _require_secure_state_root(state_root: Path) -> None:
    _require_owner_path(state_root, mode=0o700, directory=True)
    for name in (
        "installation-identity.hex",
        "continuation-token-secret.bin",
        "state.sqlite3",
    ):
        _require_owner_path(state_root / name, mode=0o600, directory=False)


def _is_open_date(calendar: Any, value: date) -> bool:
    from fin_analyse.market.trading_calendar import TradingSessionPhase

    decision = calendar.session_at(datetime.combine(value, time(10, 0), tzinfo=SHANGHAI_TZ))
    return not decision.data_gaps and decision.phase is not TradingSessionPhase.CLOSED_DAY


def build_production_runner(
    *,
    phase: DailyWorkspaceRunPhase,
    delivery_target: str | None,
    clock: Callable[[], datetime],
    checkpoint: DailyWorkspaceCheckpoint,
) -> DailyWorkspaceCheckpointRunner:
    """Compose through FIN-owned state/interfaces; delivery never builds an Agent."""

    from fin_analyse.consultation.daily_workspace import DailyWorkspaceService
    from fin_analyse.guo_teacher_research.principal_binding import (
        LocalInstallationPrincipalProvider,
    )
    from fin_analyse.guo_teacher_research.semantic_state import ResearchStateRepository
    from fin_analyse.market.trading_calendar import AShareTradingCalendar
    from fin_analyse.operations.daily_workspace_delivery import (
        HermesCliMessageSender,
        PublicEntryLedgerDispatchAcceptancePort,
        SqliteDailyWorkspaceDeliveryOutbox,
    )
    from fin_analyse.operations.daily_workspace_generator import (
        L1DirectWorkspaceGenerator,
    )
    from fin_analyse.operations.daily_workspace_runner import (
        DailyWorkspaceStateProductAdapter,
    )
    from fin_analyse.runtime.public_entry_ledger import PublicEntryLedger
    from fin_analyse.runtime.state_roots import semantic_research_state_root

    state_root = semantic_research_state_root()
    _require_secure_state_root(state_root)
    repository = ResearchStateRepository(
        state_root / "state.sqlite3",
        token_secret=_read_owner_secret(state_root / "continuation-token-secret.bin"),
    )
    principal = LocalInstallationPrincipalProvider(
        identity_path=state_root / "installation-identity.hex",
        installation_namespace="fin.local-installation.v1",
    ).require_binding()

    if phase is DailyWorkspaceRunPhase.PREPARE:
        from fin_analyse.operations.daily_workspace_generator import (
            build_default_material_provider,
        )
        from fin_analyse.runtime.knowledge_root import knowledge_base_root_from_environment

        knowledge_root = knowledge_base_root_from_environment()
        generator: object = L1DirectWorkspaceGenerator(
            material_provider=build_default_material_provider(
                knowledge_base_root=str(knowledge_root),
                as_of_clock=clock,
            ),
            clock=clock,
        )
        consultation_runner: object = _ForbiddenConsultationRunner()
        outbox: object = _ForbiddenOutbox()
    else:
        if delivery_target is None:
            raise RuntimeError("daily_workspace_delivery_target_missing")
        generator = _ForbiddenGenerator()
        consultation_runner = _ForbiddenConsultationRunner()
        ledger = PublicEntryLedger(
            db_path=Path.home() / ".local/state/fin-analyse/runtime-truth-v1/public-entry.sqlite3",
            realm="production",
        )
        outbox = SqliteDailyWorkspaceDeliveryOutbox(
            db_path=state_root.parent / "daily-workspace-delivery-v1" / "outbox.sqlite3",
            repository=repository,
            principal_id=principal.principal_id,
            sender=HermesCliMessageSender(target=delivery_target),
            obligation_port=repository,
            acceptance_port=PublicEntryLedgerDispatchAcceptancePort(
                ledger=ledger,
                principal_id=principal.principal_id,
            ),
        )

    calendar = AShareTradingCalendar.from_file(_CALENDAR_PATH)
    service = DailyWorkspaceService(
        consultation_runner=cast(Any, consultation_runner),
        state_repository=cast(Any, repository),
        clock=clock,
        calendar=calendar,
    )
    products = DailyWorkspaceStateProductAdapter(
        service=cast(Any, service),
        repository=repository,
        generator=cast(Any, generator),
        principal=principal,
        clock=_generation_now,
    )
    return DailyWorkspaceCheckpointRunner(
        schedule=DailyWorkspaceSchedulePolicy(
            is_open_date=lambda value: _is_open_date(calendar, value)
        ),
        products=products,
        outbox=cast(Any, outbox),
        clock=clock,
        wait_for_result=(
            _wait_for_prepare_result(checkpoint)
            if phase is DailyWorkspaceRunPhase.DELIVER
            else None
        ),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    runner_factory: RunnerFactory = build_production_runner,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    checkpoint = DailyWorkspaceCheckpoint(args.checkpoint)
    phase = DailyWorkspaceRunPhase(args.phase)
    execute = args.execute_prepare or args.execute_delivery
    if args.execute_prepare and phase is not DailyWorkspaceRunPhase.PREPARE:
        parser.error("--execute-prepare requires --phase prepare")
    if args.execute_delivery and phase is not DailyWorkspaceRunPhase.DELIVER:
        parser.error("--execute-delivery requires --phase deliver")
    if not execute:
        print(
            json.dumps(
                _preview_payload(
                    trading_day=args.trade_date,
                    checkpoint=checkpoint,
                    phase=phase,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    delivery_target = None
    if phase is DailyWorkspaceRunPhase.DELIVER:
        delivery_target = os.environ.get(_DELIVERY_TARGET_ENV)
        if delivery_target is None or not delivery_target.strip():
            parser.error(f"{_DELIVERY_TARGET_ENV} is required for delivery")
    active_clock = clock or (lambda: datetime.now(SHANGHAI_TZ))
    try:
        runner = runner_factory(
            phase=phase,
            delivery_target=delivery_target,
            clock=active_clock,
            checkpoint=checkpoint,
        )
        result = runner.run(
            DailyWorkspaceCheckpointRunRequest(
                trading_day=args.trade_date,
                checkpoint=checkpoint,
                phase=phase,
            )
        )
    except Exception as error:
        print(
            json.dumps(
                _failure_payload(
                    trading_day=args.trade_date,
                    checkpoint=checkpoint,
                    phase=phase,
                    error=error,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return (
            _EXPLICIT_NOT_SENT_EXIT_CODE
            if (
                phase is DailyWorkspaceRunPhase.DELIVER
                and getattr(error, "code", None) == "DAILY_WORKSPACE_DELIVERY_SEND_FAILED"
            )
            else 1
        )
    print(
        json.dumps(
            _result_payload(result, effect_allowed=True),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return (
        1
        if result.status
        in {
            DailyWorkspaceRunStatus.WINDOW_MISSED,
            DailyWorkspaceRunStatus.FAILURE_NOTICE_DELIVERED,
        }
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
