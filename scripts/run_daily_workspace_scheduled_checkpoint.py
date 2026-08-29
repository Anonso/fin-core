#!/usr/bin/env python3
"""Run one Daily Workspace checkpoint from its active frozen release."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

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
from scripts.prepare_fin_release import (  # noqa: E402
    ReleaseLayout,
    inspect_release,
    locked_ready_release,
)
from scripts.run_daily_workspace_checkpoint import main as _checkpoint_main  # noqa: E402

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_SYSTEMD_INVOCATION_ID = re.compile(r"^[0-9a-f]{32}$")
_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_EXPLICIT_NOT_SENT_RETRY_DELAY_SECONDS = 5


class _ScheduledCheckpointError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ScheduledReleaseNotCurrentError(RuntimeError):
    """The scheduled entrypoint's own release is not the active ``current``."""


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
    return parser


def _own_release_layout(
    *,
    project_root: Path | None = None,
    home: Path | None = None,
) -> ReleaseLayout:
    root = (project_root or _PROJECT_ROOT).absolute()
    owner_home = (home or Path.home()).absolute()
    try:
        metadata = root.lstat()
        canonical = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RuntimeError("scheduled entrypoint is not in an exact release") from error
    commit = root.name
    if (
        canonical != root
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or _FULL_COMMIT.fullmatch(commit) is None
    ):
        raise RuntimeError("scheduled entrypoint is not in an exact release")
    layout = ReleaseLayout(home=owner_home, commit=commit)
    if layout.release_root != root:
        raise RuntimeError("scheduled entrypoint is not in an exact release")
    return layout


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


def _require_current_release(layout: ReleaseLayout) -> None:
    try:
        metadata = layout.current_link.lstat()
        expected_target = layout.release_root.relative_to(layout.current_link.parent)
        valid = (
            stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and os.readlink(layout.current_link) == str(expected_target)
            and layout.current_link.resolve(strict=True) == layout.release_root
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError("scheduled entrypoint is not the active release") from error
    if not valid:
        raise RuntimeError("scheduled entrypoint is not the active release")


@contextmanager
def _locked_current_ready_release(
    layout: ReleaseLayout,
) -> Iterator[dict[str, Any]]:
    with locked_ready_release(layout) as status:
        try:
            _require_current_release(layout)
        except RuntimeError as error:
            raise _ScheduledReleaseNotCurrentError(str(error)) from error
        yield status
        try:
            _require_current_release(layout)
        except RuntimeError as error:
            raise _ScheduledReleaseNotCurrentError(str(error)) from error


_RELEASE_GATE_KEYS = (
    "real_release_root",
    "commit_matches",
    "top_level_matches",
    "detached",
    "tracked_clean",
    "frozen_sync_receipt",
)
_RELEASE_GATE_GROUPS = (
    "stable_assets",
    "secure_directories",
    "critical_runtime_files",
    "handoff_modes",
    "bindings",
)


def _release_unsafe_detail(layout: ReleaseLayout | None) -> str | None:
    """Summarize the failing readiness gates with a fixed vocabulary.

    The summary must never carry exception text or environment values: the
    scheduled error payload is consumed as evidence and must not leak
    delivery targets or other sensitive state.
    """
    if layout is None:
        return None
    try:
        status = inspect_release(layout)
    except (OSError, PermissionError, RuntimeError, ValueError):
        return "release status unavailable"
    code_value = status.get("code")
    code = code_value if isinstance(code_value, dict) else {}
    failed: list[str] = []
    for key in _RELEASE_GATE_KEYS:
        if code.get(key) is not True:
            failed.append(f"code.{key}")
    if code.get("unexpected_untracked") not in (None, ()):
        failed.append("code.unexpected_untracked")
    if code.get("unexpected_ignored") not in (None, ()):
        failed.append("code.unexpected_ignored")
    for group in _RELEASE_GATE_GROUPS:
        values = status.get(group)
        if not isinstance(values, dict):
            continue
        invalid = sorted(key for key, value in values.items() if value is not True)
        if invalid:
            failed.append(f"{group}:[{','.join(invalid)}]")
    return "ready=false " + " ".join(failed) if failed else "ready=false"


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
    layout: ReleaseLayout | None = None
    try:
        args = _parser().parse_args(argv)
        checkpoint = DailyWorkspaceCheckpoint(args.checkpoint)
        phase = DailyWorkspaceRunPhase(args.phase)
        now = clock or (lambda: datetime.now(SHANGHAI_TZ))
        frozen_now = _freeze_shanghai_now(now)
        layout = _own_release_layout()
        _verify_systemd_timer_invocation(checkpoint=checkpoint, phase=phase)
        systemd_verified = True
        execute_flag = (
            "--execute-prepare" if phase is DailyWorkspaceRunPhase.PREPARE else "--execute-delivery"
        )
        stdout = io.StringIO()
        with _locked_current_ready_release(layout):
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
    except _ScheduledReleaseNotCurrentError:
        code = "DAILY_WORKSPACE_SCHEDULED_RELEASE_NOT_CURRENT"
        exit_code = 3
        detail = None
    except (OSError, PermissionError, RuntimeError, ValueError):
        code = "DAILY_WORKSPACE_SCHEDULED_RELEASE_UNSAFE"
        exit_code = 3
        detail = _release_unsafe_detail(layout)
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
