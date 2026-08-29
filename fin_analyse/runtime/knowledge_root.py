"""Canonical production knowledge-root configuration.

Mutable FIN knowledge is deployment-owned data, not release-owned source.
Gateway and background workers cross this single seam so they cannot drift to
different implicit roots.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

KNOWLEDGE_BASE_ROOT_ENV = "FIN_KNOWLEDGE_BASE_ROOT"


class KnowledgeRootConfigurationError(RuntimeError):
    """Stable, path-free failure raised for an unsafe knowledge root."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def default_knowledge_base_root() -> Path:
    """Resolve the canonical production knowledge root or fail closed.

    Resolution order: FIN_KNOWLEDGE_BASE_ROOT, then the XDG-shared
    deployment data root.  Never falls back to the repository-side
    ``knowledge-base/`` mirror — silently reading that stale copy is how
    BUG-007-style misdiagnoses happen.
    """
    configured = os.environ.get(KNOWLEDGE_BASE_ROOT_ENV)
    if configured is not None and str(configured).strip():
        return validate_knowledge_base_root(configured)
    shared = (
        Path.home() / ".local" / "share" / "fin-analyse" / "shared" / "knowledge-base"
    )
    if shared.is_dir():
        return shared
    raise KnowledgeRootConfigurationError(
        "knowledge_root_missing",
        f"{KNOWLEDGE_BASE_ROOT_ENV} is required when {shared} is absent",
    )


def validate_knowledge_base_root(configured: str | Path | None) -> Path:
    """Return one existing canonical real directory or fail closed.

    The returned object is the exact configured path.  It is never silently
    expanded, rebased, or replaced with ``Path.resolve()`` because doing so
    would accept configuration drift or a symlink-selected data owner.
    """

    if configured is None or not str(configured).strip():
        raise KnowledgeRootConfigurationError(
            "knowledge_root_missing",
            f"{KNOWLEDGE_BASE_ROOT_ENV} is required",
        )

    candidate = Path(configured)
    if not candidate.is_absolute():
        raise KnowledgeRootConfigurationError(
            "knowledge_root_not_absolute",
            "knowledge base root must be absolute",
        )
    if ".." in candidate.parts:
        raise KnowledgeRootConfigurationError(
            "knowledge_root_not_canonical",
            "knowledge base root must be canonical and contain no symlink",
        )

    try:
        canonical = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise KnowledgeRootConfigurationError(
            "knowledge_root_unavailable",
            "knowledge base root must exist",
        ) from exc

    if canonical != candidate:
        raise KnowledgeRootConfigurationError(
            "knowledge_root_not_canonical",
            "knowledge base root must be canonical and contain no symlink",
        )
    if not candidate.is_dir():
        raise KnowledgeRootConfigurationError(
            "knowledge_root_not_directory",
            "knowledge base root must be a directory",
        )
    return candidate


def knowledge_base_root_from_environment(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Read and validate the sole production knowledge-root binding."""

    source = os.environ if environ is None else environ
    return validate_knowledge_base_root(source.get(KNOWLEDGE_BASE_ROOT_ENV))
