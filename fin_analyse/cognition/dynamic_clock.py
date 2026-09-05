"""Information-level dynamic freshness clocks for ZSXQ cognition units."""

from __future__ import annotations

from fin_analyse.cognition.models import DynamicClock, InformationUnit

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
