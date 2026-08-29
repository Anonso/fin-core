"""Pure fixed-XDG path resolution for durable FIN runtime state owners."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def semantic_research_state_root(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the semantic lifecycle owner root without creating it."""

    return _state_home(home=home, environ=environ) / "fin-analyse" / ("semantic-research-v1")


def runtime_truth_state_root(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the engineering runtime-fact owner root without creating it."""

    return _state_home(home=home, environ=environ) / "fin-analyse" / ("runtime-truth-v1")


def cognition_mainline_state_root(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the cognition mainline read-model owner root without creating it."""

    return _state_home(home=home, environ=environ) / "fin-analyse" / (
        "cognition-mainline-readmodel-v1"
    )


def project_memory_state_root(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the external auxiliary project-memory root without creating it."""

    return _state_home(home=home, environ=environ) / "fin-analyse" / "project-memory"


def project_sync_report_root(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the external project-sync report root without creating it."""

    return _state_home(home=home, environ=environ) / "fin-analyse" / "project-sync"


def ensure_private_state_directory(path: Path) -> Path:
    """Create or tighten one FIN-owned state directory to owner-only access."""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def write_private_state_text(path: Path, text: str) -> None:
    """Write one UTF-8 state file without a group-readable creation window."""

    ensure_private_state_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(text)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _state_home(
    *,
    home: Path | None,
    environ: Mapping[str, str] | None,
) -> Path:
    source = os.environ if environ is None else environ
    configured = source.get("XDG_STATE_HOME")
    if configured:
        state_home = Path(configured).expanduser()
        if not state_home.is_absolute():
            raise ValueError("XDG_STATE_HOME must be absolute")
        return state_home

    effective_home = Path.home() if home is None else home
    if not effective_home.is_absolute():
        raise ValueError("home must be absolute")
    return effective_home / ".local" / "state"
