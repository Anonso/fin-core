"""Tests for Daily Decision Workspace schedule policy."""

from __future__ import annotations

from datetime import date, datetime, time

from fin_analyse.consultation.daily_workspace_product_contracts import (
    DailyWorkspaceCheckpoint,
)
from fin_analyse.consultation.daily_workspace_schedule import (
    DailyWorkspaceSchedulePolicy,
)


def test_checkpoint_targets_are_fin_owned_and_fixed() -> None:
    policy = DailyWorkspaceSchedulePolicy()

    assert policy.target_for(DailyWorkspaceCheckpoint.PREMARKET) == time(9, 20)
    assert policy.target_for(DailyWorkspaceCheckpoint.MORNING_1000) == time(10, 0)
    assert policy.target_for(DailyWorkspaceCheckpoint.CLOSE_1420) == time(14, 20)
    assert policy.target_for(DailyWorkspaceCheckpoint.POSTMARKET) == time(15, 30)


def test_prepare_leads_are_short_and_checkpoint_specific() -> None:
    policy = DailyWorkspaceSchedulePolicy()

    assert policy.prepare_for(DailyWorkspaceCheckpoint.PREMARKET) == time(9, 10)
    assert policy.prepare_for(DailyWorkspaceCheckpoint.MORNING_1000) == time(9, 55)
    assert policy.prepare_for(DailyWorkspaceCheckpoint.CLOSE_1420) == time(14, 10)
    assert policy.prepare_for(DailyWorkspaceCheckpoint.POSTMARKET) == time(15, 25)


def test_in_window_accepts_target_plus_minus_window() -> None:
    policy = DailyWorkspaceSchedulePolicy()

    at = datetime(2026, 8, 3, 9, 20)
    assert policy.in_window(DailyWorkspaceCheckpoint.PREMARKET, at) is True
    assert (
        policy.in_window(
            DailyWorkspaceCheckpoint.PREMARKET,
            datetime(2026, 8, 3, 9, 20) + __import__("datetime").timedelta(minutes=10),
        )
        is True
    )
    # 窗口外不得伪装为准点。
    assert (
        policy.in_window(
            DailyWorkspaceCheckpoint.PREMARKET,
            datetime(2026, 8, 3, 10, 30),
        )
        is False
    )
    # 另一 checkpoint 的 target 不互认。
    assert (
        policy.in_window(
            DailyWorkspaceCheckpoint.MORNING_1000,
            datetime(2026, 8, 3, 9, 20),
        )
        is False
    )


def test_trading_day_fails_closed_without_calendar() -> None:
    policy = DailyWorkspaceSchedulePolicy()

    assert policy.is_trading_day(date(2026, 8, 3)) is False


def test_trading_day_uses_injected_calendar_truth() -> None:
    policy = DailyWorkspaceSchedulePolicy(is_open_date=lambda value: value.weekday() < 5)

    assert policy.is_trading_day(date(2026, 8, 3)) is True  # 周一
    assert policy.is_trading_day(date(2026, 8, 8)) is False  # 周六
