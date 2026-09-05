from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

import pytest

from scripts.zsxq_windows_incremental_scheduler import (
    _poller_timer_calendars,
    assert_windows_incremental_task_contract,
    main,
    render_windows_incremental_wrapper,
    render_wsl_consumer_poller_service,
    render_wsl_consumer_poller_timer,
    render_wsl_consumer_service,
)

_RELEASE_SHA = "a" * 40
_CAPTURE_SHA256 = "b" * 64
_CAPTURE_ROOT = PureWindowsPath(r"C:\\Users\\fin\\fin-zsxq-capture")
_STATE_ROOT = PureWindowsPath(r"C:\\Users\\fin\\AppData\\Local\\fin-analyse\\zsxq-scheduler\\runs")
_USER_SID = "S-1-5-21-100-200-300-1001"
_NOT_BEFORE_RUN_ID = "20260820T160000000-1"
_RELEASE_DIR = PurePosixPath(f"/home/fin/releases/{_RELEASE_SHA}")
_WSL_RUNS_ROOT = PurePosixPath(
    "/mnt/c/Users/fin/AppData/Local/fin-analyse/zsxq-scheduler/runs"
)
_LLM_CONFIG_PATH = PurePosixPath(
    f"/home/fin/.local/share/fin-analyse/runtime-configs/{'c' * 64}/config/llm.yaml"
)


def test_rendered_wsl_consumer_is_release_bound_oneshot_not_a_scheduler() -> None:
    service = render_wsl_consumer_service(
        release_sha=_RELEASE_SHA,
        release_dir=_RELEASE_DIR,
        home=PurePosixPath("/home/fin"),
        runs_root=_WSL_RUNS_ROOT,
        not_before_run_id=_NOT_BEFORE_RUN_ID,
        llm_config_path=_LLM_CONFIG_PATH,
    )

    assert "Type=oneshot" in service
    assert "Restart=no" in service
    assert f"WorkingDirectory={_RELEASE_DIR}" in service
    assert f"EnvironmentFile={PurePosixPath('/home/fin/.config/fin-analyse/llm.env')}" in service
    assert f"Environment=LLM_CONFIG_PATH={_LLM_CONFIG_PATH}" in service
    assert (
        f"ExecStart={_RELEASE_DIR}/.venv/bin/python -I -B -u "
        f"{_RELEASE_DIR}/scripts/consume_zsxq_capture_folder.py "
        f"--runs-root {_WSL_RUNS_ROOT} --source-commit {_RELEASE_SHA} "
        f"--not-before-run-id {_NOT_BEFORE_RUN_ID} --run-id %i"
    ) in service
    assert "OnCalendar=" not in service
    assert "WantedBy=" not in service


def test_consumer_service_renderer_is_available_from_the_operator_cli(capsys) -> None:
    assert (
        main(
            [
                "render-consumer-service",
                "--release-sha",
                _RELEASE_SHA,
                "--release-dir",
                str(_RELEASE_DIR),
                "--home",
                "/home/fin",
                "--runs-root",
                str(_WSL_RUNS_ROOT),
                "--not-before-run-id",
                _NOT_BEFORE_RUN_ID,
                "--llm-config-path",
                str(_LLM_CONFIG_PATH),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert f"WorkingDirectory={_RELEASE_DIR}" in output
    assert f"Environment=LLM_CONFIG_PATH={_LLM_CONFIG_PATH}" in output


def test_rendered_poller_service_is_release_bound_oneshot_without_run_id() -> None:
    service = render_wsl_consumer_poller_service(
        release_sha=_RELEASE_SHA,
        release_dir=_RELEASE_DIR,
        home=PurePosixPath("/home/fin"),
        runs_root=_WSL_RUNS_ROOT,
        not_before_run_id=_NOT_BEFORE_RUN_ID,
        llm_config_path=_LLM_CONFIG_PATH,
    )

    assert f"Description=FIN ZSXQ capture-folder poller ({_RELEASE_SHA})" in service
    assert "Type=oneshot" in service
    assert "Restart=no" in service
    assert f"WorkingDirectory={_RELEASE_DIR}" in service
    assert f"EnvironmentFile={PurePosixPath('/home/fin/.config/fin-analyse/llm.env')}" in service
    assert f"Environment=LLM_CONFIG_PATH={_LLM_CONFIG_PATH}" in service
    assert (
        f"ExecStart={_RELEASE_DIR}/.venv/bin/python -I -B -u "
        f"{_RELEASE_DIR}/scripts/consume_zsxq_capture_folder.py "
        f"--runs-root {_WSL_RUNS_ROOT} --source-commit {_RELEASE_SHA} "
        f"--not-before-run-id {_NOT_BEFORE_RUN_ID}"
    ) in service
    assert "--run-id" not in service
    assert "OnCalendar=" not in service
    assert "WantedBy=" not in service


def test_service_renderers_reject_an_unsafe_llm_config_path() -> None:
    common = {
        "release_sha": _RELEASE_SHA,
        "release_dir": _RELEASE_DIR,
        "home": PurePosixPath("/home/fin"),
        "runs_root": _WSL_RUNS_ROOT,
        "not_before_run_id": _NOT_BEFORE_RUN_ID,
    }
    with pytest.raises(ValueError, match="LLM config path"):
        render_wsl_consumer_service(
            **common,
            llm_config_path=PurePosixPath("relative/llm.yaml"),
        )
    with pytest.raises(ValueError, match="LLM config path"):
        render_wsl_consumer_poller_service(
            **common,
            llm_config_path=PurePosixPath("/home/fin/with space/llm.yaml"),
        )


def test_rendered_poller_timer_fires_in_windows_after_capture_slots() -> None:
    timer = render_wsl_consumer_poller_timer(release_sha=_RELEASE_SHA)

    assert f"Description=FIN ZSXQ capture-folder poller wakeup ({_RELEASE_SHA})" in timer
    for calendar in _poller_timer_calendars():
        assert f"OnCalendar={calendar}" in timer
    assert "OnCalendar=*:0/10" not in timer
    assert "Persistent=true" in timer
    assert "Unit=fin-zsxq-capture-poller.service" in timer
    assert "WantedBy=timers.target" in timer


def test_poller_timer_calendars_cover_each_slot_plus_thirty_minutes() -> None:
    assert _poller_timer_calendars() == (
        "*-*-* 08:45..59:00",
        "*-*-* 09:00..15:00",
        "*-*-* 12:20..50:00",
        "*-*-* 14:40..59:00",
        "*-*-* 15:00..10:00",
        "*-*-* 15:30..59:00",
        "*-*-* 16:00:00",
        "*-*-* 18:00..30:00",
        "*-*-* 20:20..50:00",
    )


def test_poller_service_renderer_is_available_from_the_operator_cli(capsys) -> None:
    assert (
        main(
            [
                "render-poller-service",
                "--release-sha",
                _RELEASE_SHA,
                "--release-dir",
                str(_RELEASE_DIR),
                "--home",
                "/home/fin",
                "--runs-root",
                str(_WSL_RUNS_ROOT),
                "--not-before-run-id",
                _NOT_BEFORE_RUN_ID,
                "--llm-config-path",
                str(_LLM_CONFIG_PATH),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert f"WorkingDirectory={_RELEASE_DIR}" in output
    assert f"Environment=LLM_CONFIG_PATH={_LLM_CONFIG_PATH}" in output


def test_poller_timer_renderer_is_available_from_the_operator_cli(capsys) -> None:
    assert (
        main(
            [
                "render-poller-timer",
                "--release-sha",
                _RELEASE_SHA,
            ]
        )
        == 0
    )
    assert "OnCalendar=*-*-* 08:45..59:00" in capsys.readouterr().out


def test_rendered_wrapper_is_capture_only_and_binds_the_release_capture_hash() -> None:
    wrapper = render_windows_incremental_wrapper(
        release_sha=_RELEASE_SHA,
        capture_sha256=_CAPTURE_SHA256,
        capture_root=_CAPTURE_ROOT,
        state_root=_STATE_ROOT,
    )

    assert f'$expectedSourceCommit = "{_RELEASE_SHA}"' in wrapper
    assert f'$expectedCaptureSha256 = "{_CAPTURE_SHA256}"' in wrapper
    assert "$env:FIN_ZSXQ_CAPTURE_HANDOFF_DIR = $handoffDir" in wrapper
    assert '$artifactPath = Join-Path $handoffDir "capture.latest.json"' in wrapper
    assert "capture_script_sha256 = $actualCaptureSha256" in wrapper
    assert "capture_ready = $captureReady" in wrapper
    assert (
        "wrapper_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256)" in wrapper
    )
    assert "artifact_published = Test-Path" in wrapper
    assert "artifact_changed" not in wrapper
    assert "error_code = $failureStage" in wrapper
    assert 'schema_version = "fin.zsxq-windows-capture/v4"' in wrapper
    assert "run_id = $runId" in wrapper
    # capture-only: no WSL transport, no systemctl consumer trigger, no importer/LLM config
    assert "wsl.exe" not in wrapper
    assert "wslExe" not in wrapper
    assert "systemctl" not in wrapper
    assert "import_zsxq_capture.py" not in wrapper
    assert "LLM_CONFIG_PATH" not in wrapper
    assert "llm_environment_query" not in wrapper
    assert "llm_config_unavailable" not in wrapper
    assert "/usr/bin/env" not in wrapper
    assert '"/usr/bin/true"' not in wrapper
    assert '"-d", "Ubuntu-22.04", "-u", "fin", "--exec"' not in wrapper
    assert "consumerUnit" not in wrapper
    assert "consumer.result.json" not in wrapper
    assert "ConvertFrom-Json" not in wrapper
    assert "fin.zsxq-capture-folder-consumer-result/v1" not in wrapper
    # no consumer/transport telemetry in the summary
    assert "consumer_exit_code" not in wrapper
    assert "consumer_launcher_exit_code" not in wrapper
    assert "transport_exit_code" not in wrapper
    assert "transport_attempts" not in wrapper
    assert "consumer_status" not in wrapper
    assert "consumer_result_sha256" not in wrapper
    # four frozen terminal states: capture_pending(75) -> capture_hash(70) -> capture -> success(null/0)
    assert '$failureStage = "capture_pending"' in wrapper
    assert "$finalExit = 75" in wrapper
    assert '$failureStage = "capture_hash"' in wrapper
    assert wrapper.index('$failureStage = "capture_pending"') < wrapper.index(
        '$failureStage = "capture_hash"'
    )
    assert "Publish-Summary\n\ntry {" in wrapper
    assert "if ($captureExit -eq 0) {" in wrapper
    assert '$failureStage = $null' in wrapper
    assert "$finalExit = 0" in wrapper
    # atomic publish via same-volume MoveFileEx REPLACE_EXISTING (File.Replace is
    # unusable on the live Windows box: ArgumentException "路径的形式不合法")
    assert 'Move-Item -LiteralPath $summaryTempPath -Destination $summaryPath -Force' in wrapper
    assert '[System.IO.File]::Replace' not in wrapper
    assert '[System.IO.File]::Move' not in wrapper
    assert '[System.IO.File]::WriteAllText(\n        $summaryTempPath,' in wrapper
    assert '[System.IO.File]::WriteAllText(\n        $summaryPath,' not in wrapper
    assert "consumer_launcher" not in wrapper


def test_wrapper_quotes_the_capture_script_argument_when_paths_contain_spaces() -> None:
    wrapper = render_windows_incremental_wrapper(
        release_sha=_RELEASE_SHA,
        capture_sha256=_CAPTURE_SHA256,
        capture_root=PureWindowsPath(r"C:\\Users\\fin user\\fin-zsxq-capture"),
        state_root=_STATE_ROOT,
    )

    assert '$captureArgument = \'"{0}"\' -f $captureScript' in wrapper
    assert "-ArgumentList @($captureArgument)" in wrapper


def test_task_contract_accepts_the_single_existing_six_slot_scheduler() -> None:
    wrapper_path = _CAPTURE_ROOT / "run-capture-and-import.ps1"
    xml = _task_xml(wrapper_path)

    assert_windows_incremental_task_contract(
        xml,
        wrapper_path=wrapper_path,
        expected_user_sid=_USER_SID,
    )


@pytest.mark.parametrize(
    ("replacement", "message"),
    (
        ("Parallel", "multiple_instances"),
        ("PT30M", "execution_time_limit"),
        ("false", "start_when_available"),
        ("09:30", "trigger_times"),
        (r"other.ps1", "action"),
    ),
)
def test_task_contract_rejects_scheduler_or_action_drift(
    replacement: str,
    message: str,
) -> None:
    wrapper_path = _CAPTURE_ROOT / "run-capture-and-import.ps1"
    original = _task_xml(wrapper_path)
    if replacement == "Parallel":
        changed = original.replace("IgnoreNew", replacement)
    elif replacement == "PT30M":
        changed = original.replace("PT25M", replacement)
    elif replacement == "false":
        changed = original.replace("<StartWhenAvailable>true", "<StartWhenAvailable>false")
    elif replacement == "09:30":
        changed = original.replace("T00:45:00Z", "T01:30:00Z")
    else:
        changed = original.replace("run-capture-and-import.ps1", replacement)

    with pytest.raises(ValueError, match=message):
        assert_windows_incremental_task_contract(
            changed,
            wrapper_path=wrapper_path,
            expected_user_sid=_USER_SID,
        )


@pytest.mark.parametrize(
    ("old", "new", "message"),
    (
        ("<Enabled>true</Enabled>", "<Enabled>false</Enabled>", "trigger_enabled"),
        ("<DaysInterval>1</DaysInterval>", "<DaysInterval>2</DaysInterval>", "daily"),
        (
            "</Arguments>",
            "; Write-Output unexpected</Arguments>",
            "action",
        ),
        (_USER_SID, "S-1-5-21-100-200-300-1002", "principal"),
    ),
)
def test_task_contract_rejects_disabled_non_daily_or_ambiguous_execution(
    old: str,
    new: str,
    message: str,
) -> None:
    wrapper_path = _CAPTURE_ROOT / "run-capture-and-import.ps1"
    changed = _task_xml(wrapper_path).replace(old, new, 1)

    with pytest.raises(ValueError, match=message):
        assert_windows_incremental_task_contract(
            changed,
            wrapper_path=wrapper_path,
            expected_user_sid=_USER_SID,
        )


@pytest.mark.parametrize(
    ("insertion", "message"),
    (
        (
            "<Repetition><Interval>PT1M</Interval></Repetition>",
            "trigger_shape",
        ),
        ("<RandomDelay>PT1M</RandomDelay>", "trigger_shape"),
        (
            "<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>",
            "restart_on_failure",
        ),
        ("<ComHandler><ClassId>abc</ClassId></ComHandler>", "action"),
    ),
)
def test_task_contract_rejects_hidden_extra_scheduling_or_actions(
    insertion: str,
    message: str,
) -> None:
    wrapper_path = _CAPTURE_ROOT / "run-capture-and-import.ps1"
    original = _task_xml(wrapper_path)
    if insertion.startswith("<RestartOnFailure"):
        changed = original.replace("</Settings>", f"{insertion}</Settings>")
    elif insertion.startswith("<ComHandler"):
        changed = original.replace("</Actions>", f"{insertion}</Actions>")
    else:
        changed = original.replace("<Enabled>true</Enabled>", f"<Enabled>true</Enabled>{insertion}", 1)

    with pytest.raises(ValueError, match=message):
        assert_windows_incremental_task_contract(
            changed,
            wrapper_path=wrapper_path,
            expected_user_sid=_USER_SID,
        )


@pytest.mark.parametrize(
    ("extra_container", "message"),
    (
        ("<Triggers />", "trigger"),
        ("<Settings />", "settings"),
        ("<Principals />", "principal"),
        ("<Actions Context=\"Author\"><ComHandler /></Actions>", "action"),
    ),
)
def test_task_contract_rejects_duplicate_top_level_contract_containers(
    extra_container: str,
    message: str,
) -> None:
    wrapper_path = _CAPTURE_ROOT / "run-capture-and-import.ps1"
    changed = _task_xml(wrapper_path).replace(
        "</Task>",
        f"{extra_container}</Task>",
    )

    with pytest.raises(ValueError, match=message):
        assert_windows_incremental_task_contract(
            changed,
            wrapper_path=wrapper_path,
            expected_user_sid=_USER_SID,
        )


@pytest.mark.parametrize(
    ("old", "new", "message"),
    (
        (
            "</Command>",
            "</Command><Command>C:\\evil.exe</Command>",
            "action",
        ),
        (
            "</StartBoundary>",
            "</StartBoundary><StartBoundary>2026-08-10T00:45:00Z</StartBoundary>",
            "trigger_times",
        ),
        (
            "<Enabled>true</Enabled>",
            "<Enabled>true</Enabled><Enabled>true</Enabled>",
            "trigger_enabled",
        ),
        (
            "</MultipleInstancesPolicy>",
            "</MultipleInstancesPolicy>"
            "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
            "multiple_instances",
        ),
        (
            "</UserId>",
            f"</UserId><UserId>{_USER_SID}</UserId>",
            "principal",
        ),
        (
            "</RunLevel>",
            "</RunLevel><RequiredPrivileges>"
            "<Privilege>SeDebugPrivilege</Privilege>"
            "</RequiredPrivileges>",
            "principal",
        ),
    ),
)
def test_task_contract_rejects_duplicate_required_singletons(
    old: str,
    new: str,
    message: str,
) -> None:
    wrapper_path = _CAPTURE_ROOT / "run-capture-and-import.ps1"
    changed = _task_xml(wrapper_path).replace(old, new, 1)

    with pytest.raises(ValueError, match=message):
        assert_windows_incremental_task_contract(
            changed,
            wrapper_path=wrapper_path,
            expected_user_sid=_USER_SID,
        )


def _task_xml(wrapper_path: PureWindowsPath) -> str:
    triggers = "".join(
        f"<CalendarTrigger><StartBoundary>2026-08-10T{time}:00Z</StartBoundary>"
        "<Enabled>true</Enabled>"
        "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
        "</CalendarTrigger>"
        for time in ("00:45", "04:20", "06:40", "07:30", "10:00", "12:20")
    )
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>{triggers}</Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT25M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Principals>
    <Principal id="Author">
      <UserId>{_USER_SID}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Actions Context="Author">
    <Exec>
      <Command>C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe</Command>
      <Arguments>-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{wrapper_path}"</Arguments>
      <WorkingDirectory>{wrapper_path.parent}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'''
