"""Tests for the Daily Workspace checkpoint CLI's late-result wait policy."""

from __future__ import annotations

from scripts import run_daily_workspace_checkpoint as checkpoint_cli


def test_prepare_unit_active_reads_the_activating_state(monkeypatch) -> None:
    monkeypatch.setattr(
        checkpoint_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Completed", (), {"stdout": "activating\n", "returncode": 0}
        )(),
    )

    assert (
        checkpoint_cli._prepare_unit_active("fin-daily-workspace-prepare@morning.service") is True
    )


def test_prepare_unit_active_treats_inactive_and_errors_as_done(monkeypatch) -> None:
    for output in ("inactive\n", "failed\n", "\n"):
        monkeypatch.setattr(
            checkpoint_cli.subprocess,
            "run",
            lambda *_args, _output=output, **_kwargs: type(
                "Completed", (), {"stdout": _output, "returncode": 0}
            )(),
        )
        assert (
            checkpoint_cli._prepare_unit_active("fin-daily-workspace-prepare@morning.service")
            is False
        )

    monkeypatch.setattr(
        checkpoint_cli.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError),
    )
    assert (
        checkpoint_cli._prepare_unit_active("fin-daily-workspace-prepare@morning.service") is False
    )


def test_wait_for_prepare_result_polls_until_the_prepare_unit_stops(
    monkeypatch,
) -> None:
    states = iter(("activating", "activating", "inactive"))
    sleeps: list[float] = []
    monkeypatch.setattr(
        checkpoint_cli,
        "_prepare_unit_active",
        lambda _unit: next(states) in {"activating", "active"},
    )
    monkeypatch.setattr(
        checkpoint_cli._time_module,
        "sleep",
        lambda seconds: sleeps.append(seconds),
    )

    checkpoint_cli._wait_for_prepare_result(checkpoint_cli.DailyWorkspaceCheckpoint.MORNING_1000)()

    assert sleeps == [5.0, 5.0]
