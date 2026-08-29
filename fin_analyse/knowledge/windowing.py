"""Knowledge windowing helpers used by the Knowledge Query module and QA engine.

Provides window-day resolution, window-since computation, and document/hit
filtering functions for retrieval callers that need shared window semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

_WINDOW_DAYS_MAP: dict[str, int] = {
    "3d": 3,
    "7d": 7,
    "14d": 14,
    "30d": 30,
    "60d": 60,
    "90d": 90,
    "180d": 180,
    "365d": 365,
    "all": 3650,
}


def window_days(window: str) -> int:
    """Return the number of days for a given window label.

    Unknown window values default to 180 days.
    """
    return _WINDOW_DAYS_MAP.get(window, 180)


def window_since(window: str, now: datetime | None = None) -> str | None:
    """Return a YYYY-MM-DD date string before which documents are out of window.

    window="all" → None (no filtering).
    Unknown window → defaults to 180 days.
    """
    if window == "all":
        return None
    days = window_days(window)
    ref = now or datetime.now(UTC)
    return (ref - timedelta(days=days)).strftime("%Y-%m-%d")


def doc_in_window(
    doc_or_hit: dict[str, Any],
    since: str | None,
    store: Any | None = None,
) -> bool:
    """Return True if *doc_or_hit* is within the window boundary.

    Parameters
    ----------
    doc_or_hit : dict
        Must contain a ``date`` key with a ``YYYY-MM-DD`` value.
    since : str | None
        The window-since date string.  ``None`` means no filter (always True).
    store : optional
        Ignored; accepted for backwards-compatible call sites.
    """
    if since is None:
        return True
    doc_date = str(doc_or_hit.get("date", ""))
    if not doc_date:
        return False
    return doc_date >= since


def filter_hits_by_window(
    hits: list[dict[str, Any]],
    store: Any,
    since: str | None,
) -> list[dict[str, Any]]:
    """Return only those hits whose date is >= *since*.

    Hits missing a date or whose date is before *since* are dropped.
    When *since* is ``None``, all hits are returned unchanged.
    """
    if since is None:
        return hits
    return [h for h in hits if doc_in_window(h, since)]
