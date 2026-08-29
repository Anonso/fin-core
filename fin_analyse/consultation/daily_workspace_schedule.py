"""Daily Decision Workspace schedule policy (#2).

FIN owns the checkpoint timing: the timer only passes the checkpoint enum and
never a prompt.  The policy fixes each checkpoint's target time and the
acceptance window, and defers trading-day truth to the injected calendar
(``AShareTradingCalendar`` in production, a stub in tests).  The product keeps
``target_at`` / ``generated_at`` / ``evidence_cutoff_at`` separately so a late
result is never presented as an on-time fact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo

from fin_analyse.consultation.daily_workspace_product_contracts import (
    DailyWorkspaceCheckpoint,
)

DEFAULT_CHECKPOINT_TARGETS: Final[Mapping[DailyWorkspaceCheckpoint, time]] = MappingProxyType(
    {
        DailyWorkspaceCheckpoint.PREMARKET: time(9, 20),
        DailyWorkspaceCheckpoint.MORNING_1000: time(10, 0),
        DailyWorkspaceCheckpoint.CLOSE_1420: time(14, 20),
        DailyWorkspaceCheckpoint.POSTMARKET: time(15, 30),
    }
)
DEFAULT_WINDOW_MINUTES: Final = 15
DEFAULT_PREPARE_LEAD_MINUTES: Final = 25
SHANGHAI_TZ: Final = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class DailyWorkspaceSchedulePolicy:
    """FIN-owned checkpoint timing and trading-day acceptance policy."""

    is_open_date: Callable[[date], bool] | None = None

    def target_for(self, checkpoint: DailyWorkspaceCheckpoint) -> time:
        """Return the fixed FIN target time for one checkpoint."""

        return DEFAULT_CHECKPOINT_TARGETS[checkpoint]

    def prepare_for(self, checkpoint: DailyWorkspaceCheckpoint) -> time:
        """Return when expensive preparation should start for one checkpoint."""

        target = datetime.combine(date.min, self.target_for(checkpoint))
        return (target - timedelta(minutes=DEFAULT_PREPARE_LEAD_MINUTES)).time()

    def target_at(self, trading_day: date, checkpoint: DailyWorkspaceCheckpoint) -> datetime:
        return datetime.combine(trading_day, self.target_for(checkpoint), tzinfo=SHANGHAI_TZ)

    def prepare_at(self, trading_day: date, checkpoint: DailyWorkspaceCheckpoint) -> datetime:
        return datetime.combine(trading_day, self.prepare_for(checkpoint), tzinfo=SHANGHAI_TZ)

    def in_window(self, checkpoint: DailyWorkspaceCheckpoint, at: datetime) -> bool:
        """True when ``at`` falls within target ± window on the same date.

        A call outside the window must not be presented as an on-time
        checkpoint result; the caller records ``generated_at`` separately.
        """

        window = timedelta(minutes=DEFAULT_WINDOW_MINUTES)
        if at.tzinfo is not None and at.utcoffset() is not None:
            at = at.astimezone(SHANGHAI_TZ)
            target_at = self.target_at(at.date(), checkpoint)
        else:
            target_at = datetime.combine(at.date(), self.target_for(checkpoint))
        start = target_at - window
        end = target_at + window
        return start <= at <= end

    def is_trading_day(self, value: date) -> bool:
        """Trading-day truth from the injected calendar; None fails closed."""

        if self.is_open_date is None:
            return False
        return bool(self.is_open_date(value))
