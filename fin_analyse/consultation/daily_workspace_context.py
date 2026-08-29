"""Bounded, non-evidence context projected from a Daily Workspace version."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Protocol

from fin_analyse.consultation.daily_workspace_product_contracts import (
    is_verified_daily_workspace_advisory,
)

_MAX_ANSWER_TEXT_CHARS = 6_500


class _PreviousOpenDateCalendar(Protocol):
    def previous_open_date(self, *, before: date, known_at: datetime) -> object: ...


class _WorkspaceVersionReader(Protocol):
    def find_daily_workspace_version_by_key(
        self,
        *,
        principal_id: str,
        trading_day_id: str,
        idempotency_key: str,
    ) -> object | None: ...


def resolve_scheduled_workspace_context(
    *,
    same_day_parent: object | None,
    calendar: _PreviousOpenDateCalendar | None,
    repository: _WorkspaceVersionReader,
    principal_id: str,
    trading_day_id: str,
    as_of: datetime,
) -> dict[str, object]:
    """Return the sole bounded predecessor ContextPack for one checkpoint.

    Same-day checkpoints use their exact chain parent.  A fresh day can only
    read the calendar-selected preceding open date's exact postmarket version;
    it never guesses weekdays or searches further back for a successful day.
    """

    if same_day_parent is not None:
        return _context_from_parent(
            same_day_parent,
            relationship="same_trading_day_parent",
            missing_gap="daily_workspace_same_day_parent_invalid",
            partial_gap="daily_workspace_same_day_parent_partial",
            projection_gap="daily_workspace_same_day_parent_projection_unavailable",
        )
    if calendar is None:
        return _unavailable_context(
            relationship="previous_trading_day",
            gap="daily_workspace_previous_trading_day_calendar_unavailable",
        )
    try:
        current_day = date.fromisoformat(trading_day_id)
        decision = calendar.previous_open_date(before=current_day, known_at=as_of)
        previous_day = getattr(decision, "previous_open_date", None)
        if not isinstance(previous_day, date):
            raise ValueError("calendar previous open date is invalid")
    except Exception:
        return _unavailable_context(
            relationship="previous_trading_day",
            gap="daily_workspace_previous_trading_day_calendar_unavailable",
        )
    try:
        parent = repository.find_daily_workspace_version_by_key(
            principal_id=principal_id,
            trading_day_id=previous_day.isoformat(),
            idempotency_key=f"daily:{previous_day.isoformat()}:postmarket",
        )
    except Exception:
        return _unavailable_context(
            relationship="previous_trading_day",
            gap="daily_workspace_previous_trading_day_unavailable",
        )
    if parent is None:
        return _unavailable_context(
            relationship="previous_trading_day",
            gap="daily_workspace_previous_trading_day_postmarket_missing",
        )
    return _context_from_parent(
        parent,
        relationship="previous_trading_day",
        missing_gap="daily_workspace_previous_trading_day_invalid",
        partial_gap="daily_workspace_previous_trading_day_partial",
        projection_gap="daily_workspace_previous_trading_day_projection_unavailable",
        required_checkpoint="postmarket",
    )


def _context_from_parent(
    parent: object,
    *,
    relationship: str,
    missing_gap: str,
    partial_gap: str,
    projection_gap: str,
    required_checkpoint: str | None = None,
) -> dict[str, object]:
    """Project one published parent or leave an explicit, non-injected gap."""

    source = _source(parent)
    if source is None:
        return _unavailable_context(
            relationship=relationship,
            gap=missing_gap,
        )
    if required_checkpoint is not None and source["checkpoint"] != required_checkpoint:
        return {
            **_unavailable_context(
                relationship=relationship,
                gap="daily_workspace_previous_trading_day_postmarket_missing",
            ),
            "source": source,
        }
    if getattr(parent, "status", None) != "completed":
        return {
            **_unavailable_context(
                relationship=relationship,
                gap=partial_gap,
            ),
            "source": source,
        }
    product = getattr(parent, "product", None)
    if not isinstance(product, Mapping):
        return {
            **_unavailable_context(
                relationship=relationship,
                gap=missing_gap,
            ),
            "source": source,
        }
    if not is_verified_daily_workspace_advisory(product):
        return {
            **_unavailable_context(
                relationship=relationship,
                gap="daily_workspace_parent_g_context_unverified",
            ),
            "source": source,
        }
    consultation_product = product.get("consultation_product")
    if not isinstance(consultation_product, Mapping):
        return {
            **_unavailable_context(
                relationship=relationship,
                gap=projection_gap,
            ),
            "source": source,
        }
    carry_over = _carry_over(consultation_product)
    if carry_over is None:
        return {
            **_unavailable_context(
                relationship=relationship,
                gap=projection_gap,
            ),
            "source": source,
        }
    return {
        "schema_version": "fin.daily-workspace-context/v1",
        "classification": "prior_daily_workspace_context_not_evidence",
        "relationship": relationship,
        "source": source,
        "carry_over": carry_over,
        "data_gaps": [],
    }


def _source(parent: object) -> dict[str, object] | None:
    trading_day_id = getattr(parent, "trading_day_id", None)
    product_version = getattr(parent, "product_version", None)
    artifact_hash = getattr(parent, "artifact_hash", None)
    product = getattr(parent, "product", None)
    checkpoint = product.get("checkpoint") if isinstance(product, Mapping) else None
    if (
        not isinstance(trading_day_id, str)
        or not trading_day_id
        or not isinstance(checkpoint, str)
        or not checkpoint
        or not isinstance(product_version, int)
        or product_version < 1
        or not isinstance(artifact_hash, str)
        or not artifact_hash
    ):
        return None
    return {
        "trading_day_id": trading_day_id,
        "checkpoint": checkpoint,
        "product_version": product_version,
        "artifact_hash": artifact_hash,
    }


def _unavailable_context(*, relationship: str, gap: str) -> dict[str, object]:
    return {
        "schema_version": "fin.daily-workspace-context/v1",
        "classification": "prior_daily_workspace_context_not_evidence",
        "relationship": relationship,
        "source": None,
        "carry_over": {
            "answer_text": "",
        },
        "data_gaps": [gap],
    }


def _carry_over(product: Mapping[str, object]) -> dict[str, object] | None:
    answer_text = product.get("answer_text")
    if not isinstance(answer_text, str) or not answer_text:
        return None
    return {
        "answer_text": answer_text[:_MAX_ANSWER_TEXT_CHARS],
    }
