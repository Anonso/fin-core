#!/usr/bin/env python3
"""Require secure Bash for writes to FIN's Git-external state root."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _state_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    state_home = Path(configured).expanduser() if configured else Path.home() / ".local/state"
    return (state_home / "fin-analyse").resolve(strict=False)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") not in {"Write", "Edit"}:
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str):
        return 0
    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        cwd = payload.get("cwd")
        candidate = Path(cwd) / candidate if isinstance(cwd, str) else Path.cwd() / candidate
    if not candidate.resolve(strict=False).is_relative_to(_state_root()):
        return 0
    print(
        "Git-external FIN state must be written by secure Bash beginning with "
        "`umask 077; install -d -m 0700 <task-state-dir> ...`; Write/Edit cannot "
        "set owner-only modes.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
