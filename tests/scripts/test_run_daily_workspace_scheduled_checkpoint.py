"""Tests for the identity-gated scheduled Daily Workspace checkpoint entry."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from scripts import run_daily_workspace_scheduled_checkpoint as scheduled_cli

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_COMMIT_A = "a" * 40
_COMMIT_B = "b" * 40


def _git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ("/usr/bin/git", *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _checkout(tmp_path: Path) -> Path:
    """Create a real owner-only clean git checkout with one commit."""

    root = tmp_path / "checkout"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git(
        "-c",
        "user.name=fin-test",
        "-c",
        "user.email=fin-test@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
        cwd=root,
    )
    root.chmod(0o700)
    return root


def _head(root: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=root).strip()


@pytest.mark.parametrize(
    ("phase", "execute_flag"),
    (("prepare", "--execute-prepare"), ("deliver", "--execute-delivery")),
)
def test_phase_freezes_shanghai_day_and_runs_checkpoint_after_identity(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    execute_flag: str,
) -> None:
    frozen = datetime(2026, 8, 3, 9, 15, tzinfo=_SHANGHAI)
    events: list[str] = []
    observed_argv: list[str] = []

    monkeypatch.setattr(
        scheduled_cli,
        "_verify_checkout_identity",
        lambda **_kwargs: events.append("checkout-verified"),
    )
    monkeypatch.setattr(
        scheduled_cli,
        "_verify_systemd_timer_invocation",
        lambda **_kwargs: events.append("systemd-verified"),
    )

    def checkpoint_main(
        argv: Sequence[str] | None = None,
        *,
        clock: Any,
    ) -> int:
        assert events == ["checkout-verified", "systemd-verified"]
        events.append("checkpoint")
        observed_argv.extend(argv or ())
        assert clock() == frozen
        return 0

    monkeypatch.setattr(scheduled_cli, "_checkpoint_main", checkpoint_main)

    exit_code = scheduled_cli.main(
        ["--checkpoint", "morning", "--phase", phase, "--expected-commit", _COMMIT_A],
        clock=lambda: frozen,
    )

    assert exit_code == 0
    assert observed_argv == [
        "--trade-date",
        "2026-08-03",
        "--checkpoint",
        "morning",
        "--phase",
        phase,
        execute_flag,
    ]
    assert events == ["checkout-verified", "systemd-verified", "checkpoint"]


def test_delivery_retries_the_only_explicit_not_sent_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second attempt is safe only after the outbox proved nothing was sent."""

    frozen = datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI)
    calls = 0
    delays: list[int] = []
    now_values = iter((frozen, frozen + timedelta(seconds=5)))
    monkeypatch.setattr(scheduled_cli, "_verify_checkout_identity", lambda **_kwargs: None)
    monkeypatch.setattr(scheduled_cli, "_verify_systemd_timer_invocation", lambda **_kwargs: None)
    monkeypatch.setattr(scheduled_cli.time, "sleep", lambda seconds: delays.append(seconds))

    def checkpoint_main(_argv: Sequence[str] | None = None, *, clock: Any) -> int:
        nonlocal calls
        calls += 1
        assert clock() == frozen + timedelta(seconds=5 * (calls - 1))
        return 75 if calls == 1 else 0

    monkeypatch.setattr(scheduled_cli, "_checkpoint_main", checkpoint_main)

    assert (
        scheduled_cli.main(
            ["--checkpoint", "morning", "--phase", "deliver", "--expected-commit", _COMMIT_A],
            clock=lambda: next(now_values),
        )
        == 0
    )
    assert calls == 2
    assert delays == [5]


def test_delivery_never_retries_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI)
    calls = 0
    monkeypatch.setattr(scheduled_cli, "_verify_checkout_identity", lambda **_kwargs: None)
    monkeypatch.setattr(scheduled_cli, "_verify_systemd_timer_invocation", lambda **_kwargs: None)

    def checkpoint_main(_argv: Sequence[str] | None = None, *, clock: Any) -> int:
        nonlocal calls
        calls += 1
        assert clock() == frozen
        return 1

    monkeypatch.setattr(scheduled_cli, "_checkpoint_main", checkpoint_main)

    assert (
        scheduled_cli.main(
            ["--checkpoint", "morning", "--phase", "deliver", "--expected-commit", _COMMIT_A],
            clock=lambda: frozen,
        )
        == 1
    )
    assert calls == 1


def test_expected_systemd_unit_distinguishes_prepare_and_delivery() -> None:
    assert (
        scheduled_cli._expected_systemd_service_unit(
            checkpoint=scheduled_cli.DailyWorkspaceCheckpoint.MORNING_1000,
            phase=scheduled_cli.DailyWorkspaceRunPhase.PREPARE,
        )
        == "fin-daily-workspace-prepare@morning.service"
    )
    assert (
        scheduled_cli._expected_systemd_service_unit(
            checkpoint=scheduled_cli.DailyWorkspaceCheckpoint.MORNING_1000,
            phase=scheduled_cli.DailyWorkspaceRunPhase.DELIVER,
        )
        == "fin-daily-workspace-delivery@morning.service"
    )


def test_systemd_identity_requires_exact_unit_invocation_pid_and_cgroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = "fin-daily-workspace-prepare@morning.service"
    monkeypatch.setenv("FIN_DAILY_WORKSPACE_SCHEDULED_UNIT", unit)
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    monkeypatch.setenv("SYSTEMD_EXEC_PID", "4242")
    monkeypatch.setattr(scheduled_cli.os, "getpid", lambda: 4242)
    monkeypatch.setattr(
        scheduled_cli,
        "_read_own_cgroup",
        lambda: f"0::/system.slice/{unit}\n",
        raising=False,
    )

    scheduled_cli._verify_systemd_timer_invocation(
        checkpoint=scheduled_cli.DailyWorkspaceCheckpoint.MORNING_1000,
        phase=scheduled_cli.DailyWorkspaceRunPhase.PREPARE,
    )


@pytest.mark.parametrize(
    ("declared_unit", "exec_pid", "cgroup", "error_code"),
    (
        (
            "fin-daily-workspace-delivery@morning.service",
            "4242",
            "0::/system.slice/fin-daily-workspace-prepare@morning.service\n",
            "DAILY_WORKSPACE_SCHEDULED_IDENTITY_INVALID",
        ),
        (
            "fin-daily-workspace-prepare@morning.service",
            "7",
            "0::/system.slice/fin-daily-workspace-prepare@morning.service\n",
            "DAILY_WORKSPACE_SCHEDULED_IDENTITY_INVALID",
        ),
        (
            "fin-daily-workspace-prepare@morning.service",
            "4242",
            "0::/system.slice/not-this-unit.service\n",
            "DAILY_WORKSPACE_SCHEDULED_CGROUP_MISMATCH",
        ),
    ),
)
def test_systemd_identity_rejects_wrong_phase_unit_pid_or_cgroup(
    monkeypatch: pytest.MonkeyPatch,
    declared_unit: str,
    exec_pid: str,
    cgroup: str,
    error_code: str,
) -> None:
    monkeypatch.setenv("FIN_DAILY_WORKSPACE_SCHEDULED_UNIT", declared_unit)
    monkeypatch.setenv("INVOCATION_ID", "e" * 32)
    monkeypatch.setenv("SYSTEMD_EXEC_PID", exec_pid)
    monkeypatch.setattr(scheduled_cli.os, "getpid", lambda: 4242)
    monkeypatch.setattr(scheduled_cli, "_read_own_cgroup", lambda: cgroup)

    with pytest.raises(scheduled_cli._ScheduledCheckpointError) as raised:
        scheduled_cli._verify_systemd_timer_invocation(
            checkpoint=scheduled_cli.DailyWorkspaceCheckpoint.MORNING_1000,
            phase=scheduled_cli.DailyWorkspaceRunPhase.PREPARE,
        )

    assert raised.value.code == error_code


def test_checkout_identity_accepts_owner_only_clean_checkout(tmp_path: Path) -> None:
    root = _checkout(tmp_path)

    scheduled_cli._verify_checkout_identity(expected_commit=_head(root), project_root=root)


def test_checkout_identity_rejects_permissive_root(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    root.chmod(0o755)

    with pytest.raises(scheduled_cli._ScheduledCheckoutUnsafe) as raised:
        scheduled_cli._verify_checkout_identity(expected_commit=_head(root), project_root=root)

    assert raised.value.reason == "root_identity"


def test_checkout_identity_rejects_symlinked_root(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    link = tmp_path / "linked-checkout"
    link.symlink_to(root, target_is_directory=True)

    with pytest.raises(scheduled_cli._ScheduledCheckoutUnsafe) as raised:
        scheduled_cli._verify_checkout_identity(expected_commit=_head(root), project_root=link)

    assert raised.value.reason == "root_identity"


def test_checkout_identity_rejects_commit_mismatch(tmp_path: Path) -> None:
    root = _checkout(tmp_path)

    with pytest.raises(scheduled_cli._ScheduledCheckoutUnsafe) as raised:
        scheduled_cli._verify_checkout_identity(expected_commit=_COMMIT_B, project_root=root)

    assert raised.value.reason == "head_mismatch"


def test_checkout_identity_rejects_dirty_tree(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    (root / "stray.txt").write_text("untracked\n")

    with pytest.raises(scheduled_cli._ScheduledCheckoutUnsafe) as raised:
        scheduled_cli._verify_checkout_identity(expected_commit=_head(root), project_root=root)

    assert raised.value.reason == "tree_dirty"


def test_checkout_identity_rejects_malformed_expected_commit(tmp_path: Path) -> None:
    root = _checkout(tmp_path)

    with pytest.raises(scheduled_cli._ScheduledCheckoutUnsafe) as raised:
        scheduled_cli._verify_checkout_identity(expected_commit="abc", project_root=root)

    assert raised.value.reason == "expected_commit_format"


def test_checkout_identity_rejects_non_git_directory(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    root.chmod(0o700)

    with pytest.raises(scheduled_cli._ScheduledCheckoutUnsafe) as raised:
        scheduled_cli._verify_checkout_identity(expected_commit=_COMMIT_A, project_root=root)

    assert raised.value.reason == "git_unavailable"


def test_checkout_identity_rejects_unavailable_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(scheduled_cli._ScheduledCheckoutUnsafe) as raised:
        scheduled_cli._verify_checkout_identity(expected_commit=_COMMIT_A, project_root=missing)

    assert raised.value.reason == "root_unavailable"


def test_checkout_unsafe_payload_is_stable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        scheduled_cli,
        "_verify_checkout_identity",
        lambda **_kwargs: (_ for _ in ()).throw(
            scheduled_cli._ScheduledCheckoutUnsafe("head_mismatch")
        ),
    )

    exit_code = scheduled_cli.main(
        ["--checkpoint", "close", "--phase", "prepare", "--expected-commit", _COMMIT_A],
        clock=lambda: datetime(2026, 8, 23, 13, 55, tzinfo=_SHANGHAI),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert payload["error_code"] == "DAILY_WORKSPACE_SCHEDULED_CHECKOUT_UNSAFE"
    assert payload["detail"] == "unsafe=head_mismatch"
    assert payload["side_effects_unknown"] is False
    assert payload["trigger"] == "MANUAL"


def test_unexpected_gate_failure_is_stable_and_does_not_leak_delivery_target(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "FIN_DAILY_WORKSPACE_DELIVERY_TARGET",
        "feishu:sensitive-target",
    )
    monkeypatch.setattr(
        scheduled_cli,
        "_verify_checkout_identity",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("feishu:sensitive-target")),
    )

    exit_code = scheduled_cli.main(
        ["--checkpoint", "morning", "--phase", "deliver", "--expected-commit", _COMMIT_A],
        clock=lambda: datetime(2026, 8, 3, 10, 0, tzinfo=_SHANGHAI),
    )

    output = capsys.readouterr().out
    assert exit_code == 3
    assert "sensitive-target" not in output
    assert json.loads(output)["error_code"] == "DAILY_WORKSPACE_SCHEDULED_INTERNAL_ERROR"


def test_naive_clock_fails_closed_before_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identity_called = False

    def forbidden_identity() -> None:
        nonlocal identity_called
        identity_called = True
        raise AssertionError("naive time must fail before checkout identity")

    monkeypatch.setattr(scheduled_cli, "_verify_checkout_identity", forbidden_identity)

    exit_code = scheduled_cli.main(
        ["--checkpoint", "premarket", "--phase", "prepare", "--expected-commit", _COMMIT_A],
        clock=lambda: datetime(2026, 8, 3, 9, 0),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert identity_called is False
    assert payload["error_code"] == "DAILY_WORKSPACE_SCHEDULED_CLOCK_INVALID"
    assert payload["trading_day_id"] is None


def test_weekend_is_forwarded_to_downstream_runner_without_adapter_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 8, 2, 9, 15, tzinfo=_SHANGHAI)
    observed: list[str] = []

    monkeypatch.setattr(scheduled_cli, "_verify_checkout_identity", lambda **_kwargs: None)
    monkeypatch.setattr(scheduled_cli, "_verify_systemd_timer_invocation", lambda **_kwargs: None)

    def checkpoint_main(
        argv: Sequence[str] | None = None,
        *,
        clock: Any,
    ) -> int:
        observed.extend(argv or ())
        assert clock() == frozen
        return 0

    monkeypatch.setattr(scheduled_cli, "_checkpoint_main", checkpoint_main)

    assert (
        scheduled_cli.main(
            [
                "--checkpoint",
                "premarket",
                "--phase",
                "prepare",
                "--expected-commit",
                _COMMIT_A,
            ],
            clock=lambda: frozen,
        )
        == 0
    )
    assert observed[1] == "2026-08-02"
