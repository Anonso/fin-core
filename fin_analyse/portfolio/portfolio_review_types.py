"""Domain types for the redesigned actual-advisory portfolio review.

The model submits observed facts (ObservedPortfolioFacts).  The module returns
ReviewResult or ConfirmResult.  Identity, confirmation, revision, and token are
never authored by the model — they are injected by the transport adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Model-facing observation types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservedPosition:
    """One position observed by the model (from screenshot or user statement)."""

    instrument: str
    """A-share code (600000 / 600000.SH) or stock name (雅克科技)."""

    shares: int | None = None
    """None = genuinely unknown (system asks); 0 = explicit zero (ignored)."""

    sellable_shares: int | None = None
    """None = 默认全部可卖（用户规则）；显式值仅在部分不可卖时提供。"""

    average_cost: float | None = None
    snapshot_price: float | None = None
    market_value: float | None = None

    thesis: str | None = None
    """Owner's per-holding reason (framework §三 用户明确决定). None = 未提供."""


@dataclass(frozen=True, slots=True)
class ObservedPortfolioFacts:
    """The complete set of facts observed by the model in one turn."""

    observed_at: str
    """User-stated timestamp or screenshot time.  May be partial (time only)."""

    cash_available: float | None = None
    net_assets: float | None = None
    margin_debt: float | None = None
    positions: tuple[ObservedPosition, ...] = ()


# ---------------------------------------------------------------------------
# Review outcomes
# ---------------------------------------------------------------------------


class ReviewStatus(StrEnum):
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    PREVIEW_READY = "PREVIEW_READY"
    REJECTED = "REJECTED"
    BUSY = "BUSY"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """Outcome of review_full_replacement."""

    status: ReviewStatus
    question: str | None = None
    """Targeted question text when status == NEEDS_INFORMATION."""

    readable_preview: str | None = None
    """Human-readable full-replacement diff when status == PREVIEW_READY."""

    ignored_observations: tuple[str, ...] = ()
    """Descriptions of observations that were discarded (e.g. zero-share rows)."""

    corrected_observations: tuple[str, ...] = ()
    """Descriptions of deterministic corrections (e.g. stock code normalization)."""

    candidate_revision: str | None = None
    """Revision of the frozen candidate (present when PREVIEW_READY)."""

    current_revision: str | None = None
    """Revision of the current formal snapshot."""

    reason_codes: tuple[str, ...] = ()
    """Internal reason codes when status == REJECTED."""


# ---------------------------------------------------------------------------
# Confirm outcomes
# ---------------------------------------------------------------------------


class ConfirmStatus(StrEnum):
    PUBLISHED = "PUBLISHED"
    UNCHANGED = "UNCHANGED"
    CURRENT_CHANGED = "CURRENT_CHANGED"
    REVIEW_EXPIRED = "REVIEW_EXPIRED"
    NO_PENDING_REVIEW = "NO_PENDING_REVIEW"
    BUSY = "BUSY"
    UNAVAILABLE = "UNAVAILABLE"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    """Outcome of confirm_latest_review."""

    status: ConfirmStatus
    snapshot_ref: str | None = None
    """Stable public reference when PUBLISHED or UNCHANGED."""

    reason_codes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Transport binding types (constructed by gateway, not by model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoundInteraction:
    """Server-owned identity binding for one interaction."""

    principal_id: str
    profile_name: str
    platform: str
    session_key: str
    subject_kind: str
    subject_id: str
    session_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class BoundUserConfirmation:
    """Server-owned machine confirmation proof."""

    confirmed: bool
    turn_id: str


# ---------------------------------------------------------------------------
# Pending review state (stored durably)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingReview:
    """A frozen candidate pending user confirmation."""

    candidate_snapshot: dict[str, Any]
    candidate_revision: str
    base_revision: str
    readable_preview: str
    identity_digest: str
    preview_turn: str
    issued_at: float
    ttl_seconds: int = 900
    as_of_source: str = "SYSTEM"
    """EXACT: user provided exact timestamp; USER_STATED: user provided partial date;
    SYSTEM: system defaulted to current time (source data may be stale)."""
    recorded_at: float | None = None
    """System wall-clock time when the review was processed (epoch seconds)."""
    session_id: str | None = None
    """Session generation that saw this preview. Confirm requires equality
    (fail-closed for legacy pending files where the field is absent)."""

    @property
    def expired(self) -> bool:
        import time

        return time.time() > self.issued_at + self.ttl_seconds


__all__ = [
    "BoundInteraction",
    "BoundUserConfirmation",
    "ConfirmResult",
    "ConfirmStatus",
    "ObservedPortfolioFacts",
    "ObservedPosition",
    "PendingReview",
    "ReviewResult",
    "ReviewStatus",
]
