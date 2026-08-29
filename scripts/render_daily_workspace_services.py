#!/usr/bin/env python3
"""Render the release-bound systemd service units for Daily Workspace.

The timers are intentionally installed separately: this script owns only the
two service templates, so a release update cannot silently create another
schedule.  The scheduled entrypoint itself retries delivery once only after
the outbox has proved that Hermes sent nothing (exit status 75); systemd's
``Type=oneshot`` cannot safely own that retry policy.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Literal, NoReturn

_Phase = Literal["prepare", "delivery"]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "delivery"), required=True)
    parser.add_argument("--release-root", type=Path, required=True)
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
    release_root: Path,
    home: Path,
) -> str:
    """Render one exact release-bound service unit without writing it."""

    if phase not in {"prepare", "delivery"}:
        raise ValueError("phase must be prepare or delivery")
    release = _safe_absolute_path(release_root)
    owner_home = _safe_absolute_path(home)
    service_name = f"fin-daily-workspace-{phase}"
    target_environment = (
        f"EnvironmentFile={owner_home}/.config/fin-analyse/daily-workspace-target.env\n"
        if phase == "delivery"
        else ""
    )
    timeout = "24min" if phase == "prepare" else "3min"
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
        f"WorkingDirectory={release}\n"
        f"Environment=HOME={owner_home}\n"
        f"EnvironmentFile={owner_home}/.config/fin-analyse/fin.env\n"
        f"{target_environment}"
        "Environment=PYTHONSAFEPATH=1\n"
        "Environment=PYTHONDONTWRITEBYTECODE=1\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        f"Environment=PATH={release}/.venv/bin:{owner_home}/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
        f"Environment=FIN_KNOWLEDGE_BASE_ROOT={owner_home}/.local/share/fin-analyse/shared/knowledge-base\n"
        f"Environment=FIN_DAILY_WORKSPACE_SCHEDULED_UNIT={service_name}@%i.service\n"
        "UnsetEnvironment=FEISHU_ALLOWED_USERS FEISHU_ALLOW_ALL_USERS FEISHU_APP_ID "
        "FEISHU_APP_SECRET FEISHU_CONNECTION_MODE FEISHU_DOMAIN FEISHU_GROUP_POLICY\n"
        "UMask=0077\n"
        f"ExecStart={release}/.venv/bin/python -B -u "
        f"{release}/scripts/run_daily_workspace_scheduled_checkpoint.py "
        f"--checkpoint %i --phase {'deliver' if phase == 'delivery' else 'prepare'}\n"
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
            release_root=args.release_root,
            home=args.home,
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
