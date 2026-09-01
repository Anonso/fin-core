from __future__ import annotations

from pathlib import Path

import pytest

from scripts.render_daily_workspace_services import render_daily_workspace_service


def test_delivery_service_leaves_the_single_safe_retry_to_its_entrypoint() -> None:
    unit = render_daily_workspace_service(
        phase="delivery",
        project_root=Path("/home/fin/fin-core"),
        expected_commit="a" * 40,
        home=Path("/home/fin"),
    )

    assert "EnvironmentFile=/home/fin/.config/fin-analyse/daily-workspace-target.env\n" in unit
    assert "--checkpoint %i --phase deliver --expected-commit " + "a" * 40 + "\n" in unit
    assert "Restart=no\n" in unit
    assert "RestartForceExitStatus" not in unit
    assert "RestartSec=" not in unit
    assert "TimeoutStartSec=40min\n" in unit
    assert "OnCalendar=" not in unit


def test_prepare_service_cannot_retry_or_read_a_delivery_target() -> None:
    unit = render_daily_workspace_service(
        phase="prepare",
        project_root=Path("/home/fin/fin-core"),
        expected_commit="b" * 40,
        home=Path("/home/fin"),
    )

    assert "--checkpoint %i --phase prepare --expected-commit " + "b" * 40 + "\n" in unit
    assert "daily-workspace-target.env" not in unit
    assert "RestartForceExitStatus" not in unit
    assert "Restart=no\n" in unit


def test_service_binds_the_checkout_root_into_every_path() -> None:
    root = "/home/fin/fin-core"
    unit = render_daily_workspace_service(
        phase="prepare",
        project_root=Path(root),
        expected_commit="c" * 40,
        home=Path("/home/fin"),
    )

    assert f"WorkingDirectory={root}\n" in unit
    assert f"Environment=PATH={root}/.venv/bin:" in unit
    assert f"ExecStart={root}/.venv/bin/python -B -u " in unit
    assert f"{root}/scripts/run_daily_workspace_scheduled_checkpoint.py " in unit


@pytest.mark.parametrize(
    "project_root",
    (Path("relative/checkout"), Path("/home/fin/bad\nname")),
)
def test_renderer_rejects_paths_that_cannot_be_safely_embedded(project_root: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        render_daily_workspace_service(
            phase="prepare",
            project_root=project_root,
            expected_commit="a" * 40,
            home=Path("/home/fin"),
        )


@pytest.mark.parametrize("expected_commit", ("short", "A" * 40, "a" * 39 + "g"))
def test_renderer_rejects_malformed_expected_commit(expected_commit: str) -> None:
    with pytest.raises(ValueError, match="commit"):
        render_daily_workspace_service(
            phase="prepare",
            project_root=Path("/home/fin/fin-core"),
            expected_commit=expected_commit,
            home=Path("/home/fin"),
        )
