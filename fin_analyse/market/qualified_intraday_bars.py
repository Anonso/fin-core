"""Provider-neutral completed 30-minute bars for advisory market evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from fin_analyse.market.providers.base import OHLCV


@dataclass(frozen=True)
class QualifiedThirtyMinuteBarReadRequest:
    """Account-neutral request for completed 30-minute bars at one cutoff."""

    symbol: str
    trade_date: date
    decision_cutoff_at: datetime
    minimum_completed_bars: int = 1
    deadline_at: datetime | None = None


@dataclass(frozen=True)
class QualifiedThirtyMinuteBarSeries:
    """Completed source-native 30-minute bars with explicit provenance."""

    symbol: str
    provider_id: str
    provider_version: str
    completed_bars: tuple[OHLCV, ...]
    adjustment: str = "UNKNOWN"
    source_revision: str | None = None


class QualifiedThirtyMinuteBarReader(Protocol):
    """Read only 30-minute bars completed at the exact decision cutoff."""

    def read(
        self,
        request: QualifiedThirtyMinuteBarReadRequest,
    ) -> QualifiedThirtyMinuteBarSeries: ...


__all__ = [
    "QualifiedThirtyMinuteBarReadRequest",
    "QualifiedThirtyMinuteBarReader",
    "QualifiedThirtyMinuteBarSeries",
]
