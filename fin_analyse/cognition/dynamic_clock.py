"""Information-level dynamic freshness clocks for ZSXQ cognition units."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fin_analyse.cognition.models import DynamicClock, InformationUnit

logger = logging.getLogger(__name__)

_HALF_LIFE_DAYS: dict[str, float] = {
    "strategic_thesis": 120.0,
    "industry_map": 60.0,
    "company_mapping": 21.0,
    "event_catalyst": 5.0,
    "market_timing": 2.0,
    "methodology": 365.0,
    "methodology_rule": 365.0,
}


def evaluate_dynamic_clock(unit: InformationUnit, *, now: str) -> DynamicClock:
    half_life = _HALF_LIFE_DAYS.get(unit.unit_type, 14.0)
    confidence_adjustment = 0.6 if unit.confidence < 0.5 else 1.0
    freshness_score = round(unit.confidence * confidence_adjustment, 4)
    state = "downgraded" if unit.confidence < 0.5 else "fresh"

    upgrade_triggers = ["老师后续提及", "同主题簇新文章强化", "公告/订单/涨价/认证验证"]
    downgrade_triggers = ["老师修正", "公司澄清", "股价透支", "替代技术证伪"]
    reset_triggers = ["订单验证", "出口管制升级", "主题簇再次强化"]
    if unit.unit_type in {"event_catalyst", "market_timing"}:
        downgrade_triggers.append("事件窗口过期")

    reason = f"{unit.unit_type} uses {half_life:g} day base half-life"
    if state == "downgraded":
        reason = f"low confidence {unit.confidence:.2f}; " + reason

    return DynamicClock(
        unit_id=unit.unit_id,
        state=state,
        observed_at=unit.created_at,
        base_half_life_days=half_life,
        effective_until=None,
        freshness_score=freshness_score,
        upgrade_triggers=upgrade_triggers,
        downgrade_triggers=downgrade_triggers,
        reset_triggers=reset_triggers,
        last_evaluated_at=now,
        reason=reason,
    )


def refresh_clock(clock: DynamicClock, *, now: str | None = None) -> DynamicClock:
    """Re-evaluate a clock's freshness based on elapsed time.

    Returns a new DynamicClock with updated state, freshness_score,
    and last_evaluated_at.  Clocks past their half-life are downgraded
    unless already expired.
    """
    eval_time = now or datetime.now(UTC).isoformat()

    if clock.state in {"expired", "contradicted"}:
        return clock

    try:
        observed = datetime.fromisoformat(clock.observed_at.replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat(eval_time.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return clock

    elapsed_days = (now_dt - observed).total_seconds() / 86400.0
    half_life = clock.base_half_life_days

    # Compute age-based freshness
    age_factor = max(0.0, 1.0 - elapsed_days / (half_life * 2)) if half_life > 0 else 1.0
    freshness_score = round(clock.freshness_score * age_factor, 4)

    # Determine new state
    if freshness_score <= 0.1:
        new_state = "expired"
        reason = f"expired after {elapsed_days:.0f}d (half-life {half_life:.0f}d)"
    elif freshness_score <= 0.3:
        new_state = "downgraded"
        reason = f"aging: {elapsed_days:.0f}d elapsed, half-life {half_life:.0f}d"
    elif clock.state == "downgraded" and freshness_score > 0.3:
        new_state = "downgraded"  # keep downgraded until manual upgrade
        reason = clock.reason
    else:
        new_state = clock.state
        reason = clock.reason

    return replace(
        clock,
        state=new_state,
        freshness_score=freshness_score,
        last_evaluated_at=eval_time,
        reason=reason,
    )


def refresh_all_clocks(
    runtime_root: str | Path,
    *,
    now: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Refresh all clocks in the runtime store. Returns summary dict."""
    from fin_analyse.cognition.evidence_store import JsonlRepository

    root = Path(runtime_root)
    repo = JsonlRepository(root / "dynamic_clocks.jsonl", DynamicClock, "unit_id")
    clocks = repo.list_all()
    if not clocks:
        return {"total": 0, "changed": 0, "expired": 0, "downgraded": 0}

    eval_time = now or datetime.now(UTC).isoformat()
    changed = 0
    expired = 0
    downgraded = 0

    for clock in clocks:
        refreshed = refresh_clock(clock, now=eval_time)
        if refreshed != clock:
            changed += 1
            if refreshed.state == "expired":
                expired += 1
            elif refreshed.state == "downgraded" and clock.state != "downgraded":
                downgraded += 1
            if not dry_run:
                repo.upsert(refreshed)

    if changed:
        logger.info(
            "Clocks refreshed: %d changed (%d expired, %d downgraded) of %d total",
            changed,
            expired,
            downgraded,
            len(clocks),
        )

    return {
        "total": len(clocks),
        "changed": changed,
        "expired": expired,
        "downgraded": downgraded,
        "evaluated_at": eval_time,
    }
