"""Shared models for internal Mixture-of-Agents deliberation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class TextCompletionBackend(Protocol):
    """Minimal backend interface required by MoAEngine."""

    def complete(self, prompt: str) -> str:
        """Return a text completion for the prompt."""
        ...


@dataclass(frozen=True)
class MoAReferenceRole:
    """A reference-model role that MoAEngine can call directly."""

    name: str
    prompt: str
    backend_name: str | None = None
    weight: float = 1.0


@dataclass(frozen=True)
class MoAReferenceOutput:
    """One reference output, either precomputed or produced by a role call."""

    role: str
    backend_name: str
    content: str
    ok: bool
    error: str = ""
    latency_ms: float = 0.0
    timed_out: bool = False
    timeout_seconds: float | None = None
    timeout_policy: str | None = None
    timeout_floor_seconds: float | None = None
    timeout_cap_seconds: float | None = None
    timeout_inputs: dict[str, Any] | None = None


@dataclass(frozen=True)
class MoARequest:
    """Input to the internal MoA deliberation seam."""

    task_id: str
    task_type: str
    context: dict[str, Any]
    aggregator_prompt: str
    reference_timeout_seconds: float | None = None
    aggregator_timeout_seconds: float | None = None
    reference_roles: list[MoAReferenceRole] = field(default_factory=list)
    precomputed_references: list[MoAReferenceOutput] = field(default_factory=list)
    expected_schema: dict[str, Any] | None = None
    min_reference_success: int = 1
    fallback_policy: str = "fallback"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MoAResult:
    """Output from MoAEngine.deliberate()."""

    task_id: str
    task_type: str
    status: str
    final: dict[str, Any]
    reference_outputs: list[MoAReferenceOutput]
    consensus: list[str]
    disagreements: list[str]
    blind_spots: list[str]
    confidence: float
    warnings: list[str]
    fallback_reason: str = ""
    data_gaps: list[str] = field(default_factory=list)
    source_boundary: dict[str, Any] = field(default_factory=dict)
    advisory_only: bool = True
    risk_boundary: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
