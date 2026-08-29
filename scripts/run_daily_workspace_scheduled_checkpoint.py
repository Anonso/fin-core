#!/usr/bin/env python3
"""Run one Daily Workspace checkpoint from its identity-gated checkout.

The scheduled entrypoint refuses to execute unless three independent gates
hold: the checkout identity gate below (owner-only checkout pinned to the
commit constant rendered into the unit), the systemd timer identity gate
(exact unit, invocation id, exec pid and cgroup), and the checkpoint's own
argument contract.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import NoReturn

_PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fin_analyse.consultation.daily_workspace_product_contracts import (  # noqa: E402
    DailyWorkspaceCheckpoint,
)
from fin_analyse.consultation.daily_workspace_schedule import SHANGHAI_TZ  # noqa: E402
from fin_analyse.operations.daily_workspace_runner import (  # noqa: E402
    DailyWorkspaceRunPhase,
)
from scripts.run_daily_workspace_checkpoint import main as _checkpoint_main  # noqa: E402

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_SYSTEMD_INVOCATION_ID = re.compile(r"^[0-9a-f]{32}$")
_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_EXPLICIT_NOT_SENT_RETRY_DELAY_SECONDS = 5
_SYSTEM_GIT = "/usr/bin/git"
_GIT_TIMEOUT_SECONDS = 10.0
_MAX_GIT_OUTPUT_BYTES = 1_000_000


class _ScheduledCheckpointError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ScheduledCheckoutUnsafe(RuntimeError):
    """The checkout identity gate rejected this entrypoint invocation.

    ``reason`` uses a fixed vocabulary and is the only text forwarded into
    the evidence payload, which must never leak environment values.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
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
    parser.add_argument(
        "--expected-commit",
        required=True,
        help="full commit the unit was rendered for (written at render time)",
    )
    return parser


def _git_stdout(*arguments: str, project_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            (_SYSTEM_GIT, *arguments),
            cwd=project_root,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > _MAX_GIT_OUTPUT_BYTES:
        return None
    try:
        return completed.stdout.decode("utf-8", errors="strict")
    except UnicodeError:
        return None


def _verify_checkout_identity(
    *,
    expected_commit: str,
    project_root: Path | None = None,
) -> None:
    """Accept only an owner-only clean checkout pinned to ``expected_commit``."""

    root = (project_root or _PROJECT_ROOT).absolute()
    try:
        metadata = root.lstat()
        canonical = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _ScheduledCheckoutUnsafe("root_unavailable") from error
    if (
        canonical != root
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise _ScheduledCheckoutUnsafe("root_identity")
    if _FULL_COMMIT.fullmatch(expected_commit) is None:
        raise _ScheduledCheckoutUnsafe("expected_commit_format")
    head = _git_stdout("rev-parse", "HEAD", project_root=root)
    if head is None:
        raise _ScheduledCheckoutUnsafe("git_unavailable")
    if head.strip() != expected_commit:
        raise _ScheduledCheckoutUnsafe("head_mismatch")
    if _git_stdout("status", "--porcelain", project_root=root) != "":
        raise _ScheduledCheckoutUnsafe("tree_dirty")


def _expected_systemd_service_unit(
    *,
    checkpoint: DailyWorkspaceCheckpoint,
    phase: DailyWorkspaceRunPhase,
) -> str:
    service = "prepare" if phase is DailyWorkspaceRunPhase.PREPARE else "delivery"
    return f"fin-daily-workspace-{service}@{checkpoint.value}.service"


def _verify_systemd_timer_invocation(
    *,
    checkpoint: DailyWorkspaceCheckpoint,
    phase: DailyWorkspaceRunPhase,
) -> None:
    pid = os.getpid()
    expected_unit = _expected_systemd_service_unit(
        checkpoint=checkpoint,
        phase=phase,
    )
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or os.environ.get("FIN_DAILY_WORKSPACE_SCHEDULED_UNIT", "") != expected_unit
        or _SYSTEMD_INVOCATION_ID.fullmatch(os.environ.get("INVOCATION_ID", "")) is None
        or os.environ.get("SYSTEMD_EXEC_PID", "") != str(pid)
    ):
        raise _ScheduledCheckpointError("DAILY_WORKSPACE_SCHEDULED_IDENTITY_INVALID")
    if not _cgroup_contains_unit(_read_own_cgroup(), expected_unit=expected_unit):
        raise _ScheduledCheckpointError("DAILY_WORKSPACE_SCHEDULED_CGROUP_MISMATCH")


def _cgroup_contains_unit(cgroup_text: str, *, expected_unit: str) -> bool:
    for line in cgroup_text.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and expected_unit in tuple(
            component for component in fields[2].split("/") if component
        ):
            return True
    return False


def _read_own_cgroup() -> str:
    try:
        descriptor = os.open("/proc/self/cgroup", os.O_RDONLY | _CLOEXEC | _NOFOLLOW)
    except OSError as error:
        raise _ScheduledCheckpointError("DAILY_WORKSPACE_SCHEDULED_CGROUP_UNAVAILABLE") from error
    try:
        payload = os.read(descriptor, 64 * 1024 + 1)
        if len(payload) > 64 * 1024:
            raise _ScheduledCheckpointError("DAILY_WORKSPACE_SCHEDULED_CGROUP_INVALID")
        return payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as error:
        raise _ScheduledCheckpointError("DAILY_WORKSPACE_SCHEDULED_CGROUP_INVALID") from error
    finally:
        os.close(descriptor)


def _freeze_shanghai_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _ScheduledCheckpointError("DAILY_WORKSPACE_SCHEDULED_CLOCK_INVALID")
    return value.astimezone(SHANGHAI_TZ)


def _error_payload(
    code: str,
    *,
    checkpoint: DailyWorkspaceCheckpoint | None,
    phase: DailyWorkspaceRunPhase | None,
    frozen_now: datetime | None,
    systemd_verified: bool,
    side_effects_unknown: bool,
    detail: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "fin.daily-workspace-scheduled-checkpoint/v1",
        "status": "ERROR",
        "error_code": code,
        "detail": detail,
        "phase": phase.value if phase is not None else None,
        "checkpoint": checkpoint.value if checkpoint is not None else None,
        "trading_day_id": frozen_now.date().isoformat() if frozen_now is not None else None,
        "side_effects_unknown": side_effects_unknown,
        "trigger": "SYSTEMD_MANAGED" if systemd_verified else "MANUAL",
        "production_scheduler": systemd_verified,
    }


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> int:
    checkpoint: DailyWorkspaceCheckpoint | None = None
    phase: DailyWorkspaceRunPhase | None = None
    frozen_now: datetime | None = None
    systemd_verified = False
    operation_entered = False
    try:
        args = _parser().parse_args(argv)
        checkpoint = DailyWorkspaceCheckpoint(args.checkpoint)
        phase = DailyWorkspaceRunPhase(args.phase)
        now = clock or (lambda: datetime.now(SHANGHAI_TZ))
        frozen_now = _freeze_shanghai_now(now)
        _verify_checkout_identity(expected_commit=args.expected_commit)
        _verify_systemd_timer_invocation(checkpoint=checkpoint, phase=phase)
        systemd_verified = True
        execute_flag = (
            "--execute-prepare" if phase is DailyWorkspaceRunPhase.PREPARE else "--execute-delivery"
        )
        stdout = io.StringIO()
        operation_entered = True
        try:
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = _checkpoint_main(
                    [
                        "--trade-date",
                        frozen_now.date().isoformat(),
                        "--checkpoint",
                        checkpoint.value,
                        "--phase",
                        phase.value,
                        execute_flag,
                    ],
                    clock=lambda: frozen_now,
                )
                if phase is DailyWorkspaceRunPhase.DELIVER and exit_code == 75:
                    # The outbox proved that Hermes sent nothing. This is
                    # the sole outcome that may be retried; a timeout,
                    # unknown outcome or malformed result remains a
                    # single failed attempt, so it can never duplicate a
                    # potentially accepted Feishu message.
                    try:
                        time.sleep(_EXPLICIT_NOT_SENT_RETRY_DELAY_SECONDS)
                        retry_now = _freeze_shanghai_now(now)
                    except (OSError, RuntimeError, ValueError):
                        retry_now = None
                    if retry_now is not None and retry_now.date() == frozen_now.date():
                        exit_code = _checkpoint_main(
                            [
                                "--trade-date",
                                retry_now.date().isoformat(),
                                "--checkpoint",
                                checkpoint.value,
                                "--phase",
                                phase.value,
                                execute_flag,
                            ],
                            clock=lambda: retry_now,
                        )
        except SystemExit as error:
            raise _ScheduledCheckpointError(
                "DAILY_WORKSPACE_SCHEDULED_CHECKPOINT_REJECTED"
            ) from error
        except Exception as error:
            raise _ScheduledCheckpointError(
                "DAILY_WORKSPACE_SCHEDULED_INTERNAL_ERROR"
            ) from error
        output = stdout.getvalue()
        delivery_target = os.environ.get("FIN_DAILY_WORKSPACE_DELIVERY_TARGET", "")
        if delivery_target and delivery_target in output:
            raise _ScheduledCheckpointError("DAILY_WORKSPACE_SCHEDULED_INTERNAL_ERROR")
        sys.stdout.write(output)
        return exit_code
    except _ArgumentError:
        code = "DAILY_WORKSPACE_SCHEDULED_INPUT_INVALID"
        exit_code = 2
        detail = None
    except _ScheduledCheckpointError as error:
        code = error.code
        exit_code = 1 if "IDENTITY" in code or "CGROUP" in code else 3
        detail = None
    except _ScheduledCheckoutUnsafe as error:
        code = "DAILY_WORKSPACE_SCHEDULED_CHECKOUT_UNSAFE"
        exit_code = 3
        detail = f"unsafe={error.reason}"
    except Exception:
        code = "DAILY_WORKSPACE_SCHEDULED_INTERNAL_ERROR"
        exit_code = 3
        detail = None
    _print(
        _error_payload(
            code,
            checkpoint=checkpoint,
            phase=phase,
            frozen_now=frozen_now,
            systemd_verified=systemd_verified,
            side_effects_unknown=operation_entered,
            detail=detail,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
