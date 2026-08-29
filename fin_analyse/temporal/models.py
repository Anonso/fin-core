"""Temporal service — internal models for temporal assessment requests and results.

Phase A: g_source_article / priority_article only.
Phase B (future): market data_freshness, knowledge window, dynamics half-life.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TemporalItem:
    """A single temporal-candidate item (article, data point, claim, etc.)."""

    item_id: str = ""
    title: str = ""
    source_scope: str = ""  # g_source | external_reference | market_data | knowledge_claim
    source_classification: str = ""
    column: str = ""
    published_at: str = ""
    observed_at: str = ""
    updated_at: str = ""
    semantic_payload: dict[str, Any] = field(default_factory=dict)
    quality_flags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TemporalTaskContext:
    """Task-level context for temporal assessment (topic, ticker, positions, window)."""

    task_type: str = ""
    target_company: str = ""
    target_ticker: str = ""
    topic: str = ""
    window: str = ""
    positions: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class TemporalAssessmentRequest:
    """Single-entry input to TemporalService.assess()."""

    item_type: str = ""  # g_source_article (Phase A)
    context_mode: str = ""  # priority_article (Phase A)
    now: str = ""  # ISO datetime for deterministic scoring
    items: tuple[TemporalItem, ...] = ()
    task: TemporalTaskContext = field(default_factory=TemporalTaskContext)


@dataclass(frozen=True)
class TemporalAssessment:
    """Unified temporal assessment output.

    Invariants (Phase A, enforced by TemporalService):
    - confidence_modifier == 0.0
    - confidence_boost_allowed is False
    - advisory_only is True
    """

    context: dict[str, Any] = field(default_factory=dict)
    content_time_sensitivity: dict[str, Any] = field(default_factory=dict)
    publish_freshness: str = ""
    top_event: dict[str, Any] | None = None
    events: tuple[dict[str, Any], ...] = ()
    attention_policy: dict[str, Any] = field(default_factory=dict)
    confidence_modifier: float = 0.0
    confidence_boost_allowed: bool = False
    advisory_only: bool = True
    execution_allowed: bool = False
    data_gaps: tuple[str, ...] = ()
