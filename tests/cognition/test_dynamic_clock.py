from fin_analyse.cognition.dynamic_clock import (
    evaluate_dynamic_clock,
    refresh_all_clocks,
    refresh_clock,
)
from fin_analyse.cognition.models import DynamicClock, InformationUnit, UsagePolicy


def _unit(unit_type: str) -> InformationUnit:
    return InformationUnit(
        unit_id=f"unit-{unit_type}",
        source_id="src-1",
        teacher_id="guo",
        unit_type=unit_type,
        title="demo",
        thesis="demo",
        original_evidence=["demo"],
        apprentice_interpretation="demo",
        confidence=0.8,
        related_companies=[],
        related_topics=[],
        theme_cluster_ids=[],
        usage_policy=UsagePolicy.default_research_policy(),
        created_at="2026-06-24T00:00:00",
        metadata={},
    )


def test_strategic_thesis_has_long_half_life():
    clock = evaluate_dynamic_clock(_unit("strategic_thesis"), now="2026-06-24T00:00:00")

    assert clock.state == "fresh"
    assert clock.base_half_life_days == 120.0
    assert clock.freshness_score == 0.8
    assert "老师后续提及" in clock.upgrade_triggers


def test_market_timing_has_short_half_life():
    clock = evaluate_dynamic_clock(_unit("market_timing"), now="2026-06-24T00:00:00")

    assert clock.base_half_life_days == 2.0
    assert "事件窗口过期" in clock.downgrade_triggers


def test_low_confidence_unit_is_downgraded():
    low = _unit("company_mapping")
    low = low.__class__.from_dict({**low.to_dict(), "confidence": 0.35})

    clock = evaluate_dynamic_clock(low, now="2026-06-24T00:00:00")

    assert clock.state == "downgraded"
    assert clock.freshness_score == 0.21


def test_refresh_clock_ages_market_timing():
    """After ~1.5 half-lives (3 days), a market_timing clock is downgraded."""
    clock = DynamicClock(
        unit_id="unit-mt",
        state="fresh",
        observed_at="2026-06-22T00:00:00",
        base_half_life_days=2.0,
        freshness_score=0.8,
        effective_until=None,
        upgrade_triggers=[],
        downgrade_triggers=[],
        reset_triggers=[],
        last_evaluated_at="2026-06-22T00:00:00",
        reason="fresh",
    )
    refreshed = refresh_clock(clock, now="2026-06-25T00:00:00")
    assert refreshed.state == "downgraded"
    assert refreshed.freshness_score < 0.3


def test_refresh_clock_expired_after_long_silence():
    """After many half-lives, a clock expires."""
    clock = DynamicClock(
        unit_id="unit-exp",
        state="fresh",
        observed_at="2026-01-01T00:00:00",
        base_half_life_days=2.0,
        freshness_score=0.8,
        effective_until=None,
        upgrade_triggers=[],
        downgrade_triggers=[],
        reset_triggers=[],
        last_evaluated_at="2026-01-01T00:00:00",
        reason="fresh",
    )
    refreshed = refresh_clock(clock, now="2026-06-25T00:00:00")
    assert refreshed.state == "expired"
    assert refreshed.freshness_score <= 0.1


def test_refresh_clock_preserves_contradicted():
    """Contradicted clocks are untouched."""
    clock = DynamicClock(
        unit_id="unit-contra",
        state="contradicted",
        observed_at="2026-06-01T00:00:00",
        base_half_life_days=2.0,
        freshness_score=0.1,
        effective_until=None,
        upgrade_triggers=[],
        downgrade_triggers=[],
        reset_triggers=[],
        last_evaluated_at="2026-06-01T00:00:00",
        reason="contradicted",
    )
    refreshed = refresh_clock(clock, now="2026-06-25T00:00:00")
    assert refreshed.state == "contradicted"
    assert refreshed == clock


def test_refresh_all_clocks_on_empty_store(tmp_path):
    result = refresh_all_clocks(tmp_path)
    assert result["total"] == 0
    assert result["changed"] == 0


def test_refresh_all_clocks_dry_run_does_not_write(tmp_path):
    """With a fresh clock inside the window, dry_run should report 0 changes."""
    result = refresh_all_clocks(tmp_path, dry_run=True)
    assert result["total"] == 0


def test_methodology_rule_has_long_half_life():
    """methodology_rule 与 methodology 同语义:365 天(长期规则,非 14 天默认)。"""
    clock = evaluate_dynamic_clock(_unit("methodology_rule"), now="2026-06-24T00:00:00")
    assert "365 day" in clock.reason
