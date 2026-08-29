"""Provider-neutral completed daily bars for on-demand market evidence.

The reader seam owns neither FIN authority nor a trading judgment.  A concrete
adapter returns only explicit completed bars and their provider provenance;
the consultation Agent decides how those facts affect its reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from fin_analyse.market.providers.base import OHLCV

MINIMUM_COMPLETED_BARS = 65


@dataclass(frozen=True)
class QualifiedDailyBarReadRequest:
    """Account-neutral scope for one completed-daily-bar read."""

    symbol: str
    trade_date: date
    decision_cutoff_at: datetime
    minimum_completed_bars: int = MINIMUM_COMPLETED_BARS
    deadline_at: datetime | None = None


@dataclass(frozen=True)
class QualifiedDailyBarSeries:
    """Explicit completed bars plus replay-identifying provider provenance."""

    symbol: str
    provider_id: str
    provider_version: str
    completed_bars: tuple[OHLCV, ...]
    adjustment: str = "UNKNOWN"
    source_revision: str | None = None


class QualifiedDailyBarReader(Protocol):
    """Read replay-stable point-in-time bars for the exact request scope.

    An adapter must validate ``decision_cutoff_at`` and return only bars completed
    before that trading day.  A raw prior-day history artifact may therefore be
    shared by same-provider, same-symbol reads at different cutoffs on that day.
    ``deadline_at`` is an operational response budget: adapters must bound waits
    against it, while artifact identity must remain independent of that deadline.
    The adapter returns no FIN seal, judgment, account fact, or trading permission.
    """

    def read(self, request: QualifiedDailyBarReadRequest) -> QualifiedDailyBarSeries: ...


__all__ = [
    "MINIMUM_COMPLETED_BARS",
    "QualifiedDailyBarReadRequest",
    "QualifiedDailyBarReader",
    "QualifiedDailyBarSeries",
]
