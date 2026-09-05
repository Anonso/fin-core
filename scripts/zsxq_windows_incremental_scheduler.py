#!/usr/bin/env python3
"""Render and verify the one Windows-owned ZSXQ incremental scheduler.

This is deliberately not a scheduler: Windows Task Scheduler remains the sole
owner of the six daily triggers.  The tool only makes its wrapper and task
contract release-bound and auditable.
"""

from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NoReturn
from zoneinfo import ZoneInfo

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_SID = re.compile(r"^S-\d(?:-\d+){2,14}$")
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{9}-[1-9][0-9]*$")
_TASK_NAMESPACE = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_EXPECTED_TIMES = (
    "08:45",
    "12:20",
    "14:40",
    "15:30",
    "18:00",
    "20:20",
)
# WSL poller 只在每个 Windows capture 时点后 30 分钟窗口内轮询，窗口起点即时点本身。
_POLLER_WINDOW_MINUTES = 30


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    render = actions.add_parser("render-wrapper")
    render.add_argument("--release-sha", required=True)
    render.add_argument("--capture-sha256", required=True)
    render.add_argument("--capture-root", required=True)
    render.add_argument("--state-root", required=True)
    service = actions.add_parser("render-consumer-service")
    service.add_argument("--release-sha", required=True)
    service.add_argument("--release-dir", type=PurePosixPath, required=True)
    service.add_argument("--home", type=PurePosixPath, required=True)
    service.add_argument("--runs-root", type=PurePosixPath, required=True)
    service.add_argument("--not-before-run-id", required=True)
    service.add_argument("--llm-config-path", type=PurePosixPath, required=True)
    poller_service = actions.add_parser("render-poller-service")
    poller_service.add_argument("--release-sha", required=True)
    poller_service.add_argument("--release-dir", type=PurePosixPath, required=True)
    poller_service.add_argument("--home", type=PurePosixPath, required=True)
    poller_service.add_argument("--runs-root", type=PurePosixPath, required=True)
    poller_service.add_argument("--not-before-run-id", required=True)
    poller_service.add_argument("--llm-config-path", type=PurePosixPath, required=True)
    poller_timer = actions.add_parser("render-poller-timer")
    poller_timer.add_argument("--release-sha", required=True)
    verify = actions.add_parser("verify-task-xml")
    verify.add_argument("--task-xml", type=Path, required=True)
    verify.add_argument("--wrapper-path", required=True)
    verify.add_argument("--user-sid", required=True)
    return parser


def _windows_path(value: str) -> PureWindowsPath:
    path = PureWindowsPath(value)
    if not path.drive or not path.root or any(character in value for character in '\r\n\x00"'):
        raise ValueError("Windows path must be absolute and single-line")
    return path


def _require_sha(value: str, *, pattern: re.Pattern[str], label: str) -> str:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{label} has invalid format")
    return value


def _systemd_path(value: PurePosixPath, *, label: str) -> str:
    rendered = str(value)
    if not value.is_absolute() or any(
        character.isspace() or character in '\x00"\\'
        for character in rendered
    ):
        raise ValueError(f"{label} must be an absolute systemd-safe path")
    return rendered


def render_wsl_consumer_service(
    *,
    release_sha: str,
    release_dir: PurePosixPath,
    home: PurePosixPath,
    runs_root: PurePosixPath,
    not_before_run_id: str,
    llm_config_path: PurePosixPath,
) -> str:
    """Render the release-bound one-shot consumer used by the Windows task."""

    source_commit = _require_sha(release_sha, pattern=_FULL_SHA, label="release SHA")
    release = _systemd_path(release_dir, label="release directory")
    if release_dir.name != source_commit:
        raise ValueError("release directory must end with the release SHA")
    home_path = _systemd_path(home, label="home")
    runs = _systemd_path(runs_root, label="runs root")
    llm_env = _systemd_path(
        home / ".config/fin-analyse/llm.env",
        label="LLM environment file",
    )
    llm_config = _systemd_path(llm_config_path, label="LLM config path")
    if _RUN_ID.fullmatch(not_before_run_id) is None:
        raise ValueError("not-before run ID has invalid format")
    return f"""[Unit]
Description=FIN ZSXQ capture-folder consumer ({source_commit})
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Restart=no
WorkingDirectory={release}
Environment=HOME={home_path}
Environment=PYTHONSAFEPATH=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=PATH={release}/.venv/bin:{home_path}/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile={llm_env}
Environment=LLM_CONFIG_PATH={llm_config}
UMask=0077
ExecStart={release}/.venv/bin/python -I -B -u {release}/scripts/consume_zsxq_capture_folder.py --runs-root {runs} --source-commit {source_commit} --not-before-run-id {not_before_run_id} --run-id %i
TimeoutStartSec=15min
NoNewPrivileges=true
PrivateTmp=true
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fin-zsxq-capture-consumer
"""


def render_windows_incremental_wrapper(
    *,
    release_sha: str,
    capture_sha256: str,
    capture_root: PureWindowsPath,
    state_root: PureWindowsPath,
) -> str:
    """Render the one release-bound capture-only wrapper without writing it.

    Windows Task Scheduler stays the sole capture scheduler.  The wrapper only
    verifies the pinned capture script, runs it, and atomically publishes a
    capture-only ``fin.zsxq-windows-capture/v4`` summary; it never touches WSL,
    systemd, the importer or LLM configuration.
    """

    source_commit = _require_sha(release_sha, pattern=_FULL_SHA, label="release SHA")
    expected_capture_sha = _require_sha(
        capture_sha256,
        pattern=_SHA256,
        label="capture SHA-256",
    )
    if (
        not capture_root.drive
        or not capture_root.root
        or not state_root.drive
        or not state_root.root
    ):
        raise ValueError("Windows paths must be absolute")
    capture_root_text = str(capture_root)
    state_root_text = str(state_root)
    if any(
        "'" in value or any(character in value for character in "\r\n\x00")
        for value in (capture_root_text, state_root_text)
    ):
        raise ValueError("Windows paths must be safe PowerShell literals")
    template = r"""[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskName = "FIN-ZSXQ-Incremental"
$expectedSourceCommit = "__SOURCE_COMMIT__"
$expectedCaptureSha256 = "__CAPTURE_SHA256__"
$env:FIN_OPENCLI_PROFILE = "FIN-ZSXQ"

$nodeExe = "C:\Program Files\nodejs\node.exe"
$captureRoot = '__CAPTURE_ROOT__'
$captureScript = Join-Path $captureRoot "capture-zsxq.cjs"
$captureArgument = '"{0}"' -f $captureScript
$stateRoot = '__STATE_ROOT__'
$stamp = Get-Date -Format "yyyyMMddTHHmmssfff"
$runId = "{0}-{1}" -f $stamp, $PID
$runDir = Join-Path $stateRoot $runId
$handoffDir = Join-Path $runDir "handoff"
$artifactPath = Join-Path $handoffDir "capture.latest.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

New-Item -ItemType Directory -Path $handoffDir -Force | Out-Null

$captureStdout = Join-Path $runDir "capture.stdout.json"
$captureStderr = Join-Path $runDir "capture.stderr.log"
$summaryPath = Join-Path $runDir "summary.json"
$summaryTempPath = Join-Path $runDir ("summary.json.tmp-{0}" -f $PID)

$startedAt = (Get-Date).ToUniversalTime().ToString("o")
$captureExit = $null
$captureReady = $false
$actualCaptureSha256 = $null
$artifactSha256 = $null
$errorType = $null
$failureStage = $null
$finalExit = 70

function Publish-Summary {
    $summary = [ordered]@{
        schema_version = "fin.zsxq-windows-capture/v4"
        run_id = $runId
        task_name = $taskName
        trigger = "schedule"
        started_at = $startedAt
        finished_at = (Get-Date).ToUniversalTime().ToString("o")
        profile = "FIN-ZSXQ"
        source_commit = $expectedSourceCommit
        wrapper_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
        capture_script_expected_sha256 = $expectedCaptureSha256
        capture_script_sha256 = $actualCaptureSha256
        capture_exit_code = $captureExit
        capture_ready = $captureReady
        artifact_published = Test-Path -LiteralPath $artifactPath -PathType Leaf
        artifact_sha256 = $artifactSha256
        error_code = $failureStage
        error_type = $errorType
        exit_code = $finalExit
    }
    [System.IO.File]::WriteAllText(
        $summaryTempPath,
        (($summary | ConvertTo-Json -Depth 4) + [Environment]::NewLine),
        $utf8NoBom
    )
    # atomic publish via same-volume MoveFileEx REPLACE_EXISTING; File.Replace is
    # unusable on the live Windows box (ArgumentException "路径的形式不合法")
    Move-Item -LiteralPath $summaryTempPath -Destination $summaryPath -Force
}

$failureStage = "capture_pending"
$finalExit = 75
Publish-Summary

try {
    $failureStage = "capture_hash"
    $actualCaptureSha256 = (Get-FileHash -LiteralPath $captureScript -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualCaptureSha256 -ne $expectedCaptureSha256) {
        throw [System.InvalidOperationException]::new("capture script hash does not match the scheduled release")
    }

    $failureStage = "capture"
    $previousHandoff = [Environment]::GetEnvironmentVariable("FIN_ZSXQ_CAPTURE_HANDOFF_DIR", "Process")
    try {
        $env:FIN_ZSXQ_CAPTURE_HANDOFF_DIR = $handoffDir
        $capture = Start-Process -FilePath $nodeExe -ArgumentList @($captureArgument) `
            -WorkingDirectory $captureRoot -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $captureStdout -RedirectStandardError $captureStderr
        $captureExit = $capture.ExitCode
    }
    finally {
        [Environment]::SetEnvironmentVariable("FIN_ZSXQ_CAPTURE_HANDOFF_DIR", $previousHandoff, "Process")
    }

    if ($captureExit -ne 0 -or -not (Test-Path -LiteralPath $artifactPath -PathType Leaf)) {
        throw [System.InvalidOperationException]::new("capture did not publish this run's artifact")
    }

    if ($captureExit -eq 0) {
        $captureReady = $true
        $artifactSha256 = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $failureStage = $null
        $finalExit = 0
        Publish-Summary
    }
}
catch {
    $errorType = $_.Exception.GetType().FullName
    $finalExit = if ($null -ne $captureExit -and $captureExit -ne 0) {
        $captureExit
    }
    else {
        70
    }
}
finally {
    Publish-Summary
}

exit $finalExit
"""
    return (
        template.replace("__SOURCE_COMMIT__", source_commit)
        .replace("__CAPTURE_SHA256__", expected_capture_sha)
        .replace("__CAPTURE_ROOT__", capture_root_text)
        .replace("__STATE_ROOT__", state_root_text)
    )


def render_wsl_consumer_poller_service(
    *,
    release_sha: str,
    release_dir: PurePosixPath,
    home: PurePosixPath,
    runs_root: PurePosixPath,
    not_before_run_id: str,
    llm_config_path: PurePosixPath,
) -> str:
    """Render the release-bound WSL poller that drains one pending capture.

    The poller invokes the folder consumer WITHOUT ``--run-id`` so each timer
    fire consumes the oldest pending artifact.  It is a transport/ingest owner,
    not a capture scheduler; the Windows Task remains the sole capture owner.
    """

    source_commit = _require_sha(release_sha, pattern=_FULL_SHA, label="release SHA")
    release = _systemd_path(release_dir, label="release directory")
    if release_dir.name != source_commit:
        raise ValueError("release directory must end with the release SHA")
    home_path = _systemd_path(home, label="home")
    runs = _systemd_path(runs_root, label="runs root")
    llm_env = _systemd_path(
        home / ".config/fin-analyse/llm.env",
        label="LLM environment file",
    )
    llm_config = _systemd_path(llm_config_path, label="LLM config path")
    if _RUN_ID.fullmatch(not_before_run_id) is None:
        raise ValueError("not-before run ID has invalid format")
    return f"""[Unit]
Description=FIN ZSXQ capture-folder poller ({source_commit})
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Restart=no
WorkingDirectory={release}
Environment=HOME={home_path}
Environment=PYTHONSAFEPATH=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=PATH={release}/.venv/bin:{home_path}/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile={llm_env}
Environment=LLM_CONFIG_PATH={llm_config}
UMask=0077
ExecStart={release}/.venv/bin/python -I -B -u {release}/scripts/consume_zsxq_capture_folder.py --runs-root {runs} --source-commit {source_commit} --not-before-run-id {not_before_run_id}
# 协作 deadline 1200s（consume 内 --deadline-seconds）+ 尾部余量（一次 LLM 尾/terminalize/G 发布）
TimeoutStartSec=25min
# 75 = coalesced（前 run 仍在跑，本次合并），是良性收编；consumer 单元不得加
# （其 75 = unavailable 真失败，加了会吞错）。
SuccessExitStatus=75
NoNewPrivileges=true
PrivateTmp=true
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fin-zsxq-capture-poller
"""


def render_wsl_consumer_poller_timer(
    *,
    release_sha: str,
) -> str:
    """Render the release-bound WSL poller timer.

    The poller only fires inside a 30-minute window after each Windows capture
    slot (``_EXPECTED_TIMES`` is the single clock source shared with the Task
    verifier), never all day.  The 15:30 slot crosses the hour, so it gets an
    explicit ``16:00:00`` catch-up trigger that also replays retryable
    artifacts after the window closes.  ``Persistent=true`` catches up after
    WSL sleep/reboot; the timer only triggers the poller service
    (transport/ingest owner), never capture.
    """

    source_commit = _require_sha(release_sha, pattern=_FULL_SHA, label="release SHA")
    calendars = "\n".join(f"OnCalendar={expr}" for expr in _poller_timer_calendars())
    return f"""[Unit]
Description=FIN ZSXQ capture-folder poller wakeup ({source_commit})

[Timer]
{calendars}
Persistent=true
AccuracySec=1s
RandomizedDelaySec=0
Unit=fin-zsxq-capture-poller.service

[Install]
WantedBy=timers.target
"""


def _poller_timer_calendars() -> tuple[str, ...]:
    """Return the windowed ``OnCalendar`` expressions for every capture slot.

    Each slot gets ``HH:MM..(MM+30):00`` (inclusive, one trigger per minute).
    A slot whose window crosses the hour boundary is split into
    ``HH:MM..59:00`` plus the next hour's remaining range.  For example, 15:30
    becomes ``15:30..59:00`` + ``16:00:00``, while 08:45 becomes
    ``08:45..59:00`` + ``09:00..15:00``.  Keeping the windows derived from
    ``_EXPECTED_TIMES`` prevents clock drift between the Windows Task triggers
    and the WSL consumer windows.
    """

    calendars: list[str] = []
    for slot in _EXPECTED_TIMES:
        hour_text, minute_text = slot.split(":", maxsplit=1)
        hour = int(hour_text)
        end_minute = int(minute_text) + _POLLER_WINDOW_MINUTES
        if end_minute >= 60:
            calendars.append(f"*-*-* {hour:02d}:{minute_text}..59:00")
            next_hour_end = end_minute - 60
            if next_hour_end == 0:
                calendars.append(f"*-*-* {hour + 1:02d}:00:00")
            else:
                calendars.append(f"*-*-* {hour + 1:02d}:00..{next_hour_end:02d}:00")
        else:
            calendars.append(f"*-*-* {hour:02d}:{minute_text}..{end_minute:02d}:00")
    return tuple(calendars)


def assert_windows_incremental_task_contract(
    task_xml: str,
    *,
    wrapper_path: PureWindowsPath,
    expected_user_sid: str,
) -> None:
    """Reject drift from the one existing six-slot Task Scheduler contract."""

    try:
        root = ElementTree.fromstring(task_xml)
    except ElementTree.ParseError as error:
        raise ValueError("task_xml_invalid") from error
    namespace = _TASK_NAMESPACE
    settings_nodes = root.findall(f"{namespace}Settings")
    if len(settings_nodes) != 1:
        raise ValueError("task_settings_missing")
    settings = settings_nodes[0]
    expected_settings = {
        "MultipleInstancesPolicy": "IgnoreNew",
        "StartWhenAvailable": "true",
        "ExecutionTimeLimit": "PT25M",
        "Enabled": "true",
    }
    for field, expected in expected_settings.items():
        elements = settings.findall(f"{namespace}{field}")
        if len(elements) != 1 or elements[0].text != expected:
            code = {
                "MultipleInstancesPolicy": "multiple_instances",
                "StartWhenAvailable": "start_when_available",
                "ExecutionTimeLimit": "execution_time_limit",
                "Enabled": "enabled",
            }[field]
            raise ValueError(f"task_{code}_invalid")
    if settings.findall(f"{namespace}RestartOnFailure"):
        raise ValueError("task_restart_on_failure_invalid")
    trigger_parents = root.findall(f"{namespace}Triggers")
    if len(trigger_parents) != 1:
        raise ValueError("task_trigger_times_invalid")
    triggers = list(trigger_parents[0])
    if len(triggers) != len(_EXPECTED_TIMES) or any(
        trigger.tag != f"{namespace}CalendarTrigger" for trigger in triggers
    ):
        raise ValueError("task_trigger_times_invalid")
    times: list[str] = []
    for trigger in triggers:
        allowed_trigger_children = {
            f"{namespace}StartBoundary",
            f"{namespace}Enabled",
            f"{namespace}ScheduleByDay",
        }
        if any(child.tag not in allowed_trigger_children for child in trigger):
            raise ValueError("task_trigger_shape_invalid")
        enabled = trigger.findall(f"{namespace}Enabled")
        if len(enabled) != 1 or enabled[0].text != "true":
            raise ValueError("task_trigger_enabled_invalid")
        schedules = trigger.findall(f"{namespace}ScheduleByDay")
        boundaries = trigger.findall(f"{namespace}StartBoundary")
        days_intervals = (
            schedules[0].findall(f"{namespace}DaysInterval")
            if len(schedules) == 1
            else []
        )
        if (
            len(schedules) != 1
            or len(days_intervals) != 1
            or days_intervals[0].text != "1"
            or len(list(schedules[0])) != 1
        ):
            raise ValueError("task_trigger_daily_invalid")
        if len(boundaries) != 1:
            raise ValueError("task_trigger_times_invalid")
        boundary = boundaries[0]
        if not isinstance(boundary.text, str):
            raise ValueError("task_trigger_times_invalid")
        try:
            parsed = datetime.fromisoformat(boundary.text.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("task_trigger_times_invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("task_trigger_times_invalid")
        times.append(parsed.astimezone(_SHANGHAI_TZ).strftime("%H:%M"))
    if tuple(sorted(times)) != _EXPECTED_TIMES:
        raise ValueError("task_trigger_times_invalid")
    actions_parents = root.findall(f"{namespace}Actions")
    actions_parent = actions_parents[0] if len(actions_parents) == 1 else None
    actions = (
        actions_parent.findall(f"{namespace}Exec")
        if actions_parent is not None
        else []
    )
    if (
        len(actions) != 1
        or actions_parent is None
        or actions_parent.get("Context") != "Author"
        or len(list(actions_parent)) != 1
    ):
        raise ValueError("task_action_invalid")
    action = actions[0]
    action_fields = {
        field: action.findall(f"{namespace}{field}")
        for field in ("Command", "Arguments", "WorkingDirectory")
    }
    if len(list(action)) != 3 or any(
        len(elements) != 1 for elements in action_fields.values()
    ):
        raise ValueError("task_action_invalid")
    command = action_fields["Command"][0].text
    arguments = action_fields["Arguments"][0].text
    working_directory = action_fields["WorkingDirectory"][0].text
    expected_command = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    expected_arguments = (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden '
        f'-File "{wrapper_path}"'
    )
    if (
        command is None
        or command.lower() != expected_command.lower()
        or arguments != expected_arguments
        or working_directory != str(wrapper_path.parent)
    ):
        raise ValueError("task_action_invalid")
    if _WINDOWS_SID.fullmatch(expected_user_sid) is None:
        raise ValueError("expected_user_sid_invalid")
    principal_parents = root.findall(f"{namespace}Principals")
    principals = (
        principal_parents[0].findall(f"{namespace}Principal")
        if len(principal_parents) == 1
        else []
    )
    if len(principals) != 1:
        raise ValueError("task_principal_invalid")
    principal = principals[0]
    principal_fields = {
        field: principal.findall(f"{namespace}{field}")
        for field in ("UserId", "LogonType", "RunLevel")
    }
    if (
        principal.get("id") != "Author"
        or len(list(principal)) != 3
        or any(len(elements) != 1 for elements in principal_fields.values())
        or principal_fields["UserId"][0].text != expected_user_sid
        or principal_fields["LogonType"][0].text != "InteractiveToken"
        or principal_fields["RunLevel"][0].text != "LeastPrivilege"
    ):
        raise ValueError("task_principal_invalid")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "render-wrapper":
        print(
            render_windows_incremental_wrapper(
                release_sha=args.release_sha,
                capture_sha256=args.capture_sha256,
                capture_root=_windows_path(args.capture_root),
                state_root=_windows_path(args.state_root),
            ),
            end="",
        )
        return 0
    if args.action == "render-consumer-service":
        print(
            render_wsl_consumer_service(
                release_sha=args.release_sha,
                release_dir=args.release_dir,
                home=args.home,
                runs_root=args.runs_root,
                not_before_run_id=args.not_before_run_id,
                llm_config_path=args.llm_config_path,
            ),
            end="",
        )
        return 0
    if args.action == "render-poller-service":
        print(
            render_wsl_consumer_poller_service(
                release_sha=args.release_sha,
                release_dir=args.release_dir,
                home=args.home,
                runs_root=args.runs_root,
                not_before_run_id=args.not_before_run_id,
                llm_config_path=args.llm_config_path,
            ),
            end="",
        )
        return 0
    if args.action == "render-poller-timer":
        print(
            render_wsl_consumer_poller_timer(
                release_sha=args.release_sha,
            ),
            end="",
        )
        return 0
    if args.action == "verify-task-xml":
        assert_windows_incremental_task_contract(
            args.task_xml.read_text(encoding="utf-16"),
            wrapper_path=_windows_path(args.wrapper_path),
            expected_user_sid=args.user_sid,
        )
        return 0
    raise AssertionError("unreachable parser action")


if __name__ == "__main__":
    raise SystemExit(main())
