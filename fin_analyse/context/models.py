"""External context records for apprentice cognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContextRequestScope:
    """Platform-agnostic request scope for multi-person access isolation."""

    tenant_id: str = "default"
    user_id: str = "default"
    platform: str = "local"
    conversation_id: str = ""
    visibility: str = "private"


@dataclass(frozen=True)
class ExternalContextRecord:
    """Reference-only external market context.

    These records enrich the apprentice agent's observation space. They are not
    teacher cognition and must not directly drive trade actions or position size.
    """

    record_id: str
    source: str
    category: str
    ticker: str
    title: str
    summary: str
    occurred_at: str
    url: str = ""
    importance: float = 0.5
    is_decision_factor: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalContextBundle:
    """A collection of reference-only context records for one ticker and request scope."""

    ticker: str
    records: list[ExternalContextRecord]
    warnings: list[str] = field(default_factory=list)
    reference_only: bool = True
    scope: ContextRequestScope = field(default_factory=ContextRequestScope)
