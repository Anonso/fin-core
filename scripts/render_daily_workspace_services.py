#!/usr/bin/env python3
"""Render the checkout-bound systemd service units for Daily Workspace.

The timers are intentionally installed separately: this script owns only the
two service templates, so a schedule update cannot silently change cadence.
Each rendered unit pins the checkout through ``--expected-commit``; the
scheduled entrypoint re-derives ``git rev-parse HEAD`` at start and refuses
to run a drifted or permissive tree.  The scheduled entrypoint itself
retries delivery once only after the outbox has proved that Hermes sent
nothing (exit status 75); systemd's ``Type=oneshot`` cannot safely own that
retry policy.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Literal, NoReturn

_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")

_Phase = Literal["prepare", "delivery"]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "delivery"), required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--home", type=Path, required=True)
    return parser


def _safe_absolute_path(path: Path) -> str:
    value = str(path)
    if not path.is_absolute() or any(character in value for character in "\n\r\x00"):
        raise ValueError("embedded systemd path must be absolute and single-line")
    return value.rstrip("/") or "/"


def render_daily_workspace_service(
    *,
    phase: _Phase,
    project_root: Path,
    expected_commit: str,
    home: Path,
) -> str:
    """Render one exact checkout-bound service unit without writing it."""

    if phase not in {"prepare", "delivery"}:
        raise ValueError("phase must be prepare or delivery")
    if _FULL_COMMIT.fullmatch(expected_commit) is None:
        raise ValueError("expected commit must be a full lowercase 40-hex commit")
    root = _safe_absolute_path(project_root)
    owner_home = _safe_absolute_path(home)
    service_name = f"fin-daily-workspace-{phase}"
    target_environment = (
        f"EnvironmentFile={owner_home}/.config/fin-analyse/daily-workspace-target.env\n"
        if phase == "delivery"
        else ""
    )
    # Delivery may wait (poll) for a late prepare result; its timeout must
    # exceed the prepare unit's 35min process cap (agent budget 30min + margin).
    timeout = "35min" if phase == "prepare" else "40min"
    retry_policy = "Restart=no\n"
    return (
        "[Unit]\n"
        f"Description=FIN Daily Workspace {phase} checkpoint\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "RefuseManualStart=yes\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={root}\n"
        f"Environment=HOME={owner_home}\n"
        f"EnvironmentFile={owner_home}/.config/fin-analyse/fin.env\n"
        f"{target_environment}"
        "Environment=PYTHONSAFEPATH=1\n"
        "Environment=PYTHONDONTWRITEBYTECODE=1\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        f"Environment=PATH={root}/.venv/bin:{owner_home}/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
        f"Environment=FIN_KNOWLEDGE_BASE_ROOT={owner_home}/.local/share/fin-analyse/shared/knowledge-base\n"
        f"Environment=FIN_DAILY_WORKSPACE_SCHEDULED_UNIT={service_name}@%i.service\n"
        "UnsetEnvironment=FEISHU_ALLOWED_USERS FEISHU_ALLOW_ALL_USERS FEISHU_APP_ID "
        "FEISHU_APP_SECRET FEISHU_CONNECTION_MODE FEISHU_DOMAIN FEISHU_GROUP_POLICY\n"
        "UMask=0077\n"
        f"ExecStart={root}/.venv/bin/python -B -u "
        f"{root}/scripts/run_daily_workspace_scheduled_checkpoint.py "
        f"--checkpoint %i --phase {'deliver' if phase == 'delivery' else 'prepare'} "
        f"--expected-commit {expected_commit}\n"
        f"{retry_policy}"
        f"TimeoutStartSec={timeout}\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n"
        "KillMode=mixed\n"
        "KillSignal=SIGTERM\n"
        "TimeoutStopSec=30\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        f"SyslogIdentifier={service_name}\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(
        render_daily_workspace_service(
            phase=args.phase,
            project_root=args.project_root,
            expected_commit=args.expected_commit,
            home=args.home,
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
