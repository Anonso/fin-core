from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WRITE_GUARD = Path(".claude/hooks/block_insecure_cc_task_state_write.py")
SETTINGS = Path(".claude/settings.json")



def _run_write_guard(
    state_home: Path,
    *,
    tool_name: str,
    file_path: Path,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["XDG_STATE_HOME"] = str(state_home)
    return subprocess.run(
        [sys.executable, "-B", str(WRITE_GUARD)],
        input=json.dumps(
            {
                "cwd": str(Path.cwd()),
                "tool_name": tool_name,
                "tool_input": {"file_path": str(file_path)},
            }
        ),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )



def test_write_guard_blocks_write_and_edit_inside_fin_state(tmp_path: Path) -> None:
    task_file = tmp_path / "fin-analyse" / "a2-test" / "artifact.json"

    for tool_name in ("Write", "Edit"):
        result = _run_write_guard(
            tmp_path,
            tool_name=tool_name,
            file_path=task_file,
        )

        assert result.returncode == 2
        assert "Git-external FIN state must be written by secure Bash" in result.stderr


def test_write_guard_allows_repository_edits(tmp_path: Path) -> None:
    result = _run_write_guard(
        tmp_path,
        tool_name="Write",
        file_path=Path.cwd() / "docs" / "example.md",
    )

    assert result.returncode == 0
    assert result.stderr == ""


def test_claude_settings_wire_write_guard() -> None:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    pre_tool_use_hooks = settings["hooks"]["PreToolUse"]

    assert any(
        group.get("matcher") == "Write|Edit"
        and group["hooks"][0]["command"].endswith(
            '.claude/hooks/block_insecure_cc_task_state_write.py"'
        )
        for group in pre_tool_use_hooks
    )
