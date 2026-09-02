"""Single-source G freshness window configuration and trading-day cutoffs.

BUG-006③: window tiers were duplicated across ``g_working_set`` (admission)
and ``runtime_context`` (selection) with inconsistent values and natural-day
semantics.  This module is the one seam both layers read:

- ``load_g_window_config`` — file-backed tuning values (missing file = built-in
  defaults; malformed file = loud warning + defaults, never silent).
- ``commentary_window_cutoff`` — 锐评 cutoff in TRADING-day semantics (owner
  ruling 2026-08-19 "最近 2 天" upgraded to trading days), falling back to the
  exact legacy natural-day rolling window when the calendar cannot answer.

Both layers must call these helpers so the live selection can never drift
outside the published working-set manifest (the ``sources_changed`` guard).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from fin_analyse.market.trading_calendar import (
    AShareTradingCalendar,
    CalendarArtifactError,
)

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
WINDOW_CONFIG_PATH = _PROJECT_ROOT / "config" / "g_context_windows.json"
ZSXQ_REFERENCE_WINDOW_CONFIG_PATH = (
    _PROJECT_ROOT / "config" / "zsxq_reference_windows.json"
)
_TOPIC_RULES_PATH = _PROJECT_ROOT / "config" / "position_topic_rules.json"
_CALENDAR_PATH = _PROJECT_ROOT / "config" / "market" / "a_share_calendar_2026.json"


@dataclass(frozen=True)
class GWindowConfig:
    """Immutable snapshot of one resolve/generation's window policy."""

    commentary_trading_days: int = 2
    special_report_days: int = 30
    historical_days: int = 60


_CONFIG_INT_FIELDS = (
    "commentary_trading_days",
    "special_report_days",
    "historical_days",
)


def load_g_window_config(path: Path | None = None) -> GWindowConfig:
    """Load window tuning values, or the built-in defaults.

    A missing file is the normal configured-by-code state and stays silent.
    A file that exists but is malformed is LOUD: one warning per load, then
    defaults — "改了配置没生效" must be observable, never silent.
    """

    config_path = path or WINDOW_CONFIG_PATH
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return GWindowConfig()
    except (OSError, ValueError) as error:
        logger.warning("g window config unreadable (%s): %s", config_path, type(error).__name__)
        return GWindowConfig()
    if not isinstance(payload, dict):
        logger.warning("g window config not a mapping: %s", config_path)
        return GWindowConfig()
    values: dict[str, int] = {}
    for field_name in _CONFIG_INT_FIELDS:
        raw = payload.get(field_name)
        if raw is None:
            continue
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
            logger.warning("g window config field %s invalid: %r", field_name, raw)
            return GWindowConfig()
        values[field_name] = raw
    return GWindowConfig(**values)


def reference_window_days(
    column: str,
    *,
    config_path: Path | None = None,
    default_days: int = 60,
) -> int:
    """Reference-lane recency window (natural days) for one ZSXQ column.

    Owner 2026-09-02: 普通研报 60 天、Q&A 栏目 20 天、缺省 60 天。
    配置缺失/损坏 → default（损坏时响一声警告，规则 6 改配置须可观察）。
    """

    path = config_path or ZSXQ_REFERENCE_WINDOW_CONFIG_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_days
    except (OSError, ValueError) as error:
        logger.warning(
            "zsxq reference window config unreadable (%s): %s",
            path,
            type(error).__name__,
        )
        return default_days
    if not isinstance(payload, dict):
        logger.warning("zsxq reference window config not a mapping: %s", path)
        return default_days
    windows = payload.get("windows")
    if isinstance(windows, dict):
        entry = windows.get(column)
        if isinstance(entry, dict) and isinstance(entry.get("days"), int):
            return entry["days"]
    raw_default = payload.get("default_days")
    if isinstance(raw_default, int):
        return raw_default
    return default_days


@lru_cache(maxsize=1)
def _calendar() -> AShareTradingCalendar | None:
    """The loaded 2026 calendar artifact, or None when it cannot be read.

    A None here means the deployment is missing a shipped config artifact —
    that is the loud, gap-worthy case (see ``calendar_artifact_available``).
    """
    try:
        return AShareTradingCalendar.from_file(_CALENDAR_PATH)
    except (CalendarArtifactError, OSError, ValueError) as error:
        logger.warning("a-share calendar artifact unavailable: %s", type(error).__name__)
        return None


def calendar_artifact_available() -> bool:
    return _calendar() is not None


def trading_window_cutoff(
    evaluation_time: datetime,
    trading_days: int,
) -> tuple[datetime, bool]:
    """Cutoff covering the last *trading_days* CN trading sessions.

    Sessions counted are those whose local date is a trading date and whose
    00:00 is at or before the evaluation instant; the cutoff is the earliest
    such session's local midnight.  Returns ``(cutoff, used_trading_days)``.

    Fallback semantics are two-tier by design: outside the calendar's
    authority (instants before ``verified_at``, dates beyond coverage) the
    legacy natural-day rolling window IS the defined policy and returns
    silently; a missing/corrupt calendar artifact is surfaced through
    ``calendar_artifact_available`` so callers can gap loudly.
    """

    fallback = evaluation_time - timedelta(days=trading_days)
    if trading_days < 1:
        return fallback, False
    calendar = _calendar()
    if calendar is None:
        return fallback, False
    try:
        local_anchor = evaluation_time.astimezone(_CN_TZ)
        session = calendar.session_at(local_anchor)
        if session.status.value == "UNKNOWN" or session.phase.value == "UNKNOWN":
            return fallback, False
        dates: list = []
        cursor_date = local_anchor.date()
        cursor_at = local_anchor
        while len(dates) < trading_days:
            if session.phase.value != "CLOSED_DAY":
                dates.append(cursor_date)
            decision = calendar.previous_open_date(before=cursor_date, known_at=evaluation_time)
            cursor_date = decision.previous_open_date
            cursor_at = datetime(
                cursor_date.year,
                cursor_date.month,
                cursor_date.day,
                tzinfo=_CN_TZ,
            )
            session = calendar.session_at(cursor_at)
            if session.status.value == "UNKNOWN" or session.phase.value == "UNKNOWN":
                return fallback, False
        earliest = dates[-1]
        return datetime(
            earliest.year, earliest.month, earliest.day, tzinfo=_CN_TZ
        ), True
    except (CalendarArtifactError, OSError, ValueError):
        return fallback, False


def load_position_topic_rules() -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] | None:
    """Load configured position→topic rules, or ``None`` to use built-ins.

    Loaded once per resolve into an immutable snapshot (no IO inside the
    matching loop, no mid-request rule mixing).  A malformed file is loud and
    falls back to the built-in table.
    """

    try:
        payload = json.loads(_TOPIC_RULES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("position topic rules unreadable: %s", type(error).__name__)
        return None
    if not isinstance(payload, list):
        logger.warning("position topic rules not a list")
        return None
    rules: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            logger.warning("position topic rule entry invalid")
            return None
        needles = entry.get("match")
        topics = entry.get("topics")
        if (
            not isinstance(needles, list)
            or not isinstance(topics, list)
            or not all(isinstance(n, str) and n for n in needles)
            or not all(isinstance(t, str) and t for t in topics)
        ):
            logger.warning("position topic rule fields invalid")
            return None
        rules.append((tuple(needles), tuple(topics)))
    if not rules:
        logger.warning("position topic rules empty")
        return None
    return tuple(rules)
