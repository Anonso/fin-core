from __future__ import annotations

from pathlib import Path

import pytest


def test_runtime_state_roots_share_fixed_xdg_base_without_sharing_owners(
    tmp_path: Path,
) -> None:
    from fin_analyse.runtime.state_roots import (
        project_memory_state_root,
        project_sync_report_root,
        runtime_truth_state_root,
        semantic_research_state_root,
    )

    state_home = tmp_path / "xdg-state"
    environ = {"XDG_STATE_HOME": str(state_home)}

    semantic = semantic_research_state_root(environ=environ)
    engineering = runtime_truth_state_root(environ=environ)
    project_memory = project_memory_state_root(environ=environ)
    project_sync = project_sync_report_root(environ=environ)

    assert semantic == state_home / "fin-analyse" / "semantic-research-v1"
    assert engineering == state_home / "fin-analyse" / "runtime-truth-v1"
    assert project_memory == state_home / "fin-analyse" / "project-memory"
    assert project_sync == state_home / "fin-analyse" / "project-sync"
    assert len({semantic, engineering, project_memory, project_sync}) == 4
    assert not state_home.exists()


def test_runtime_state_roots_reject_relative_xdg_state_home() -> None:
    from fin_analyse.runtime.state_roots import runtime_truth_state_root

    with pytest.raises(ValueError, match="XDG_STATE_HOME must be absolute"):
        runtime_truth_state_root(environ={"XDG_STATE_HOME": "relative-state"})
