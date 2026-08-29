from fin_analyse.cognition.models import DynamicClock, InformationUnit, UsagePolicy
from fin_analyse.cognition.research_suggestion import generate_research_suggestion


def _unit(unit_type: str, title: str = "钼前驱体") -> InformationUnit:
    return InformationUnit(
        unit_id="unit-1",
        source_id="src-1",
        teacher_id="guo",
        unit_type=unit_type,
        title=title,
        thesis="钼前驱体值得重点跟踪。",
        original_evidence=["钼前驱体最具性价比"],
        apprentice_interpretation="这是研究候选，不是老师买入指令。",
        confidence=0.8,
        related_companies=[],
        related_topics=["钼前驱体", "半导体"],
        theme_cluster_ids=["cluster-semi-materials-dejapanization"],
        usage_policy=UsagePolicy.default_research_policy(),
        created_at="2026-06-24T00:00:00",
        metadata={},
    )


def _clock(state: str = "fresh") -> DynamicClock:
    return DynamicClock(
        unit_id="unit-1",
        state=state,
        observed_at="2026-06-24T00:00:00",
        base_half_life_days=60.0,
        effective_until=None,
        freshness_score=0.8,
        upgrade_triggers=["公告验证"],
        downgrade_triggers=["公司澄清"],
        reset_triggers=["订单验证"],
        last_evaluated_at="2026-06-24T00:00:00",
        reason="fresh",
    )


def test_industry_map_becomes_research_candidate():
    suggestion = generate_research_suggestion(_unit("industry_map"), _clock())

    assert suggestion.suggestion_level == "research_candidate"
    assert "公告验证" in suggestion.upgrade_conditions
    assert "direct_buy_signal" in suggestion.forbidden_usage
    assert "automatic_position_change" in suggestion.forbidden_usage


def test_market_timing_becomes_trade_hypothesis_only():
    suggestion = generate_research_suggestion(_unit("market_timing", "不要上头"), _clock())

    assert suggestion.suggestion_level == "trade_hypothesis"
    assert "risk_review" in suggestion.allowed_usage
    assert "direct_buy_signal" in suggestion.forbidden_usage


def test_overheated_thesis_downgraded_to_watchlist():
    """Thesis mentioning '已暴涨' or '已热' is downgraded to watchlist."""
    unit = InformationUnit(
        unit_id="unit-overheated",
        source_id="src-1",
        teacher_id="guo",
        unit_type="industry_map",
        title="WF6 已暴涨",
        thesis="WF6 已暴涨，板块高位震荡，当前更适合作为母题观察扩散方向。",
        original_evidence=["WF6 已暴涨；板块高位震荡。"],
        apprentice_interpretation="不追高，跟踪扩散。",
        confidence=0.75,
        related_companies=[],
        related_topics=["WF6"],
        theme_cluster_ids=[],
        usage_policy=UsagePolicy.default_research_policy(),
        created_at="2026-06-25T00:00:00",
        metadata={},
    )
    suggestion = generate_research_suggestion(unit, _clock())
    assert suggestion.suggestion_level == "watchlist"
    assert "过热" in suggestion.summary


def test_undervalued_thesis_stays_research_candidate():
    """Thesis mentioning '最具性价比' stays research_candidate (not overheated)."""
    unit = InformationUnit(
        unit_id="unit-undervalued",
        source_id="src-1",
        teacher_id="guo",
        unit_type="industry_map",
        title="钼前驱体优先级最高",
        thesis="钼前驱体最具性价比，属于低关注高壁垒方向，尚未交易。",
        original_evidence=["钼前驱体最具性价比；总分14.5。"],
        apprentice_interpretation="这是未充分交易的研究候选。",
        confidence=0.8,
        related_companies=[],
        related_topics=["钼前驱体"],
        theme_cluster_ids=[],
        usage_policy=UsagePolicy.default_research_policy(),
        created_at="2026-06-25T00:00:00",
        metadata={},
    )
    suggestion = generate_research_suggestion(unit, _clock())
    assert suggestion.suggestion_level == "research_candidate"


def test_downgraded_clock_limits_to_observation():
    # downgraded clock with low freshness
    downgraded = DynamicClock(
        unit_id="unit-1",
        state="downgraded",
        observed_at="2026-06-24T00:00:00",
        base_half_life_days=21.0,
        effective_until=None,
        freshness_score=0.3,
        upgrade_triggers=[],
        downgrade_triggers=[],
        reset_triggers=[],
        last_evaluated_at="2026-06-24T00:00:00",
        reason="downgraded due to lack of reinforcement",
    )
    suggestion = generate_research_suggestion(_unit("strategic_thesis"), downgraded)

    assert suggestion.suggestion_level == "observation"
    assert suggestion.confidence == 0.3
