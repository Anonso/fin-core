"""Production scheduler CLI for the canonical ZSXQ scraper module."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .cdp_runtime import ProductionCdpCompletionReceipt, run_production_cdp_once
from .contracts import ZsxqRunRequest
from .scheduler_handoff_lock import (
    HandoffLockMode,
    SchedulerHandoffLockBusyError,
    SchedulerHandoffLockError,
    hold_scheduler_handoff_lock,
    scheduler_handoff_lock_path,
)

SCHEMA_VERSION = "fin.zsxq-scheduled-run/v3"
INVOCATION_EVENT_SCHEMA_VERSION = "fin.zsxq-scheduled-invocation-event/v1"


def canonical_runtime_db_path() -> Path:
    """Return the one ledger path for the effective Unix account.

    ``Path.home()`` trusts the caller-controlled ``HOME`` environment variable,
    which must not be allowed to create a second exactly-once domain.
    """
    account_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    return account_home / ".local/state/fin-analyse/zsxq-scraper/runtime.sqlite3"


DEFAULT_RUNTIME_DB = canonical_runtime_db_path()


def _default_knowledge_base_root() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "fin-analyse" / "shared" / "knowledge-base"


_EXIT_BY_RUN_FAILURE = {
    "failed": 1,
    "deadline_exceeded": 2,
    "interrupted": 3,
}


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _emit_invocation_event(payload: dict[str, object]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _InvocationBudget:
    monotonic_deadline_at: float
    wall_deadline_at: datetime | None = None

    def with_wall_deadline(self, wall_deadline_at: datetime | None) -> _InvocationBudget:
        return replace(self, wall_deadline_at=wall_deadline_at)


def _start_invocation(
    *,
    intent: str,
    trigger: str,
    deadline_seconds: float,
) -> tuple[str | None, datetime | None]:
    try:
        started_at = _utc_now()
        deadline_at = started_at + timedelta(seconds=deadline_seconds)
    except Exception:
        return None, None
    try:
        invocation_id = str(uuid4())
        _emit_invocation_event(
            {
                "schema_version": INVOCATION_EVENT_SCHEMA_VERSION,
                "phase": "started",
                "invocation_id": invocation_id,
                "intent": intent,
                "trigger": trigger,
                "started_at": started_at.isoformat(),
                "deadline_seconds": deadline_seconds,
                "deadline_at": deadline_at.isoformat(),
            }
        )
    except Exception:
        return None, deadline_at
    return invocation_id, deadline_at


def _finish_invocation(
    *,
    invocation_id: str | None,
    exit_code: int,
    payload: dict[str, object],
) -> int:
    _emit(payload)
    if invocation_id is None:
        return exit_code
    status = payload.get("status")
    with suppress(Exception):
        _emit_invocation_event(
            {
                "schema_version": INVOCATION_EVENT_SCHEMA_VERSION,
                "phase": "finished",
                "invocation_id": invocation_id,
                "status": status if isinstance(status, str) else "internal_error",
                "exit_code": exit_code,
                "finished_at": _utc_now().isoformat(),
            }
        )
    return exit_code


def _prepare_runtime_db(path: Path) -> str | None:
    if not path.is_absolute():
        return "runtime_db_must_be_absolute"
    if path.exists() and (path.is_symlink() or not path.is_file()):
        return "runtime_db_must_be_regular_file"
    parent = path.parent
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        return "runtime_parent_must_be_directory"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    if path.exists():
        path.chmod(0o600)
    return None


def _validate_knowledge_base_root(path: Path) -> str | None:
    if not path.is_absolute():
        return "knowledge_base_root_must_be_absolute"
    if path.is_symlink() or not path.is_dir():
        return "knowledge_base_root_must_be_directory"
    index_file = path / "index.json"
    if index_file.is_symlink() or not index_file.is_file():
        return "knowledge_base_index_missing"
    return None


def _projection(completion: ProductionCdpCompletionReceipt) -> dict[str, object]:
    result = completion.run
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": result.status,
        "completion_status": completion.verified_completion_status(),
        "completion_data_gaps": list(completion.verified_completion_data_gaps()),
        "intent": result.intent,
        "trigger": result.trigger,
        "coalesced": result.coalesced,
    }
    if completion.g_working_set is not None:
        payload["g_working_set"] = completion.g_working_set.to_dict()
    for name in (
        "run_id",
        "active_run_id",
        "changed_count",
        "attempt",
        "started_at",
        "finished_at",
        "failure_reason",
    ):
        value = getattr(result, name, None)
        if value is not None:
            payload[name] = value
    return payload


def _completion_exit_code(completion: ProductionCdpCompletionReceipt) -> int:
    run_failure = _EXIT_BY_RUN_FAILURE.get(completion.run.status)
    if run_failure is not None:
        return run_failure
    completion_status = completion.verified_completion_status()
    if completion_status == "coalesced":
        return 75
    if completion_status == "ready":
        return 0
    if completion_status == "partial":
        return 4
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one canonical ZSXQ reconciliation")
    parser.add_argument("--intent", choices=("sync", "watch"), default="sync")
    parser.add_argument("--trigger", choices=("schedule", "manual", "recovery"), default="schedule")
    parser.add_argument("--deadline-seconds", type=float, default=1200.0)
    parser.add_argument("--runtime-db", type=Path, default=DEFAULT_RUNTIME_DB)
    parser.add_argument("--knowledge-base-root", type=Path, default=_default_knowledge_base_root())
    return parser


def _run_locked(
    *,
    args: argparse.Namespace,
    runtime_db: Path,
    knowledge_base_root: Path,
    invocation_budget: _InvocationBudget,
) -> tuple[int, dict[str, object]]:
    error_code = _prepare_runtime_db(runtime_db)
    if error_code is not None:
        return (
            64,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": error_code,
            },
        )
    error_code = _validate_knowledge_base_root(knowledge_base_root)
    if error_code is not None:
        return (
            64,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": error_code,
            },
        )

    try:
        deadline_seconds = invocation_budget.monotonic_deadline_at - monotonic()
        if invocation_budget.wall_deadline_at is not None:
            wall_remaining = (invocation_budget.wall_deadline_at - _utc_now()).total_seconds()
            deadline_seconds = min(deadline_seconds, wall_remaining)
    except Exception:
        return (
            70,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "invocation_deadline_unavailable",
            },
        )
    if deadline_seconds <= 0:
        return (
            70,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "invocation_deadline_exceeded_before_run",
            },
        )
    request = ZsxqRunRequest(
        intent=args.intent,
        trigger=args.trigger,
        deadline_seconds=deadline_seconds,
    )
    previous_umask = os.umask(0o077)
    try:
        completion = run_production_cdp_once(
            runtime_db_path=runtime_db,
            knowledge_base_root=knowledge_base_root,
            request=request,
        )
    except Exception:
        return (
            70,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "scheduled_run_failed",
            },
        )
    finally:
        os.umask(previous_umask)

    if runtime_db.exists():
        runtime_db.chmod(0o600)
    try:
        terminal_deadline_reached = monotonic() >= invocation_budget.monotonic_deadline_at
    except Exception:
        return (
            70,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "invocation_deadline_unavailable",
            },
        )
    if terminal_deadline_reached:
        return (
            70,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "invocation_deadline_exceeded_after_run",
            },
        )
    return _completion_exit_code(completion), _projection(completion)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime_db = args.runtime_db.expanduser()
    knowledge_base_root = args.knowledge_base_root.expanduser()
    if not runtime_db.is_absolute():
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": "runtime_db_must_be_absolute",
            }
        )
        return 64
    if not 30.0 <= args.deadline_seconds <= 3600.0:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": "deadline_out_of_range",
            }
        )
        return 64

    try:
        invocation_budget = _InvocationBudget(
            monotonic_deadline_at=monotonic() + args.deadline_seconds
        )
    except Exception:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": "invocation_deadline_unavailable",
            }
        )
        return 70

    invocation_id, invocation_deadline_at = _start_invocation(
        intent=args.intent,
        trigger=args.trigger,
        deadline_seconds=args.deadline_seconds,
    )
    invocation_budget = invocation_budget.with_wall_deadline(invocation_deadline_at)

    try:
        lock_path = scheduler_handoff_lock_path(runtime_db)
    except ValueError as error:
        return _finish_invocation(
            invocation_id=invocation_id,
            exit_code=64,
            payload={
                "schema_version": SCHEMA_VERSION,
                "status": "invalid_request",
                "error_code": str(error),
            },
        )

    try:
        with hold_scheduler_handoff_lock(
            lock_path,
            mode=HandoffLockMode.SHARED,
        ):
            exit_code, payload = _run_locked(
                args=args,
                runtime_db=runtime_db,
                knowledge_base_root=knowledge_base_root,
                invocation_budget=invocation_budget,
            )
        return _finish_invocation(
            invocation_id=invocation_id,
            exit_code=exit_code,
            payload=payload,
        )
    except SchedulerHandoffLockBusyError as error:
        return _finish_invocation(
            invocation_id=invocation_id,
            exit_code=75,
            payload={
                "schema_version": SCHEMA_VERSION,
                "status": "coalesced",
                "completion_status": "coalesced",
                "completion_data_gaps": [],
                "intent": args.intent,
                "trigger": args.trigger,
                "coalesced": True,
                "error_code": error.code,
            },
        )
    except SchedulerHandoffLockError as error:
        return _finish_invocation(
            invocation_id=invocation_id,
            exit_code=70,
            payload={
                "schema_version": SCHEMA_VERSION,
                "status": "internal_error",
                "error_code": error.code,
            },
        )


if __name__ == "__main__":
    raise SystemExit(main())
