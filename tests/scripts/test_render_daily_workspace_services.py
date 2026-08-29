from __future__ import annotations

from pathlib import Path

import pytest

from scripts.render_daily_workspace_services import render_daily_workspace_service


def test_delivery_service_leaves_the_single_safe_retry_to_its_entrypoint() -> None:
    unit = render_daily_workspace_service(
        phase="delivery",
        release_root=Path("/opt/fin/releases/" + "a" * 40),
        home=Path("/home/fin"),
    )

    assert "EnvironmentFile=/home/fin/.config/fin-analyse/daily-workspace-target.env\n" in unit
    assert "--checkpoint %i --phase deliver\n" in unit
    assert "Restart=no\n" in unit
    assert "RestartForceExitStatus" not in unit
    assert "RestartSec=" not in unit
    assert "TimeoutStartSec=3min\n" in unit
    assert "OnCalendar=" not in unit


def test_prepare_service_cannot_retry_or_read_a_delivery_target() -> None:
    unit = render_daily_workspace_service(
        phase="prepare",
        release_root=Path("/opt/fin/releases/" + "b" * 40),
        home=Path("/home/fin"),
    )

    assert "--checkpoint %i --phase prepare\n" in unit
    assert "daily-workspace-target.env" not in unit
    assert "RestartForceExitStatus" not in unit
    assert "Restart=no\n" in unit


@pytest.mark.parametrize(
    "release_root",
    (Path("relative/release"), Path("/opt/fin/releases/bad\nname")),
)
def test_renderer_rejects_paths_that_cannot_be_safely_embedded(release_root: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        render_daily_workspace_service(
            phase="prepare",
            release_root=release_root,
            home=Path("/home/fin"),
        )
