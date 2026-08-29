"""Bounded investment research suggestions for ZSXQ cognition units."""

from __future__ import annotations

from fin_analyse.cognition.models import DynamicClock, InformationUnit, InvestmentResearchSuggestion
from fin_analyse.utils.ids import stable_id

_FORBIDDEN = [
    "direct_buy_signal",
    "automatic_position_change",
    "bypass_risk_guard",
    "persona_rebuild_without_review",
]


# Content signals that suggest an already-priced-in / overheated thesis
_OVERHEATED_SIGNALS = (
    "已暴涨",
    "已热",
    "高位震荡",
    "充分交易",
    "已交易",
    "已涨",
    "题材热",
    "透支",
    "追高",
)

# Content signals that suggest a high-conviction, under-appreciated opportunity
_UNDERVALUED_SIGNALS = (
    "紧缺",
    "卡脖子",
    "替代空间",
    "确定性最高",
    "最具性价比",
    "优先级最高",
    "低关注",
    "未充分交易",
    "尚未交易",
)


def _content_semantic_adjustment(unit: InformationUnit) -> str | None:
    """Check thesis/evidence for content semantics that override the unit_type default.

    Returns an adjusted suggestion level, or None to keep the default.
    """
    haystack = " ".join([unit.title, unit.thesis, *unit.original_evidence])
    has_overheated = any(kw in haystack for kw in _OVERHEATED_SIGNALS)
    has_undervalued = any(kw in haystack for kw in _UNDERVALUED_SIGNALS)

    if has_overheated and not has_undervalued:
        return "watchlist"
    return None


def _level_for(unit: InformationUnit, clock: DynamicClock) -> str:
    if clock.state in {"downgraded", "expired", "contradicted"}:
        return "observation"
    if unit.unit_type == "market_timing":
        return "trade_hypothesis"
    if unit.unit_type in {"industry_map", "company_mapping", "strategic_thesis"}:
        # Content-semantic adjustment: downgrade overheated theses
        adjusted = _content_semantic_adjustment(unit)
        if adjusted is not None:
            return adjusted
        return "research_candidate"
    if unit.unit_type == "event_catalyst":
        return "catalyst_tracking"
    return "watchlist"


def generate_research_suggestion(
    unit: InformationUnit,
    clock: DynamicClock,
) -> InvestmentResearchSuggestion:
    level = _level_for(unit, clock)
    confidence = round(min(unit.confidence, clock.freshness_score), 4)
    allowed = ["research_tracking", "daily_key_interpretation"]
    if level == "trade_hypothesis":
        allowed.append("risk_review")
    if level == "catalyst_tracking":
        allowed.append("catalyst_monitoring")

    adjusted_by = _content_semantic_adjustment(unit)
    summary = f"{unit.title}: {unit.thesis}"
    if level == "observation":
        summary = f"降级为观察线索：{summary}"
    elif level == "trade_hypothesis":
        summary = f"仅作为交易节奏假设，不构成买入信号：{summary}"
    elif level == "watchlist" and adjusted_by == "watchlist":
        summary = f"内容过热降级为观察：{summary}"
    else:
        summary = f"进入{level}：{summary}"

    return InvestmentResearchSuggestion(
        suggestion_id=stable_id("zsxq-suggestion", unit.unit_id, level),
        unit_id=unit.unit_id,
        suggestion_level=level,
        summary=summary,
        upgrade_conditions=list(clock.upgrade_triggers),
        downgrade_conditions=list(clock.downgrade_triggers),
        tracking_indicators=["公告", "订单", "涨价", "产能", "客户认证", "老师后续提及"],
        risk_boundaries=["不直接触发买入", "不突破风控", "区分老师原文与学徒推演"],
        allowed_usage=allowed,
        forbidden_usage=_FORBIDDEN,
        confidence=confidence,
    )
