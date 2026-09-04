"""Pin the semantics of read-capability tool descriptions (BUG-004).

Descriptions are the only channel guaranteed to reach every client model;
drift between a description and its implementation once made the model
skip market-wide margin data as "account margin with zero balance".
"""

from __future__ import annotations

from fin_analyse.read_capabilities.server import _TOOL_DESCRIPTIONS


def test_margin_description_is_market_wide_not_account() -> None:
    text = _TOOL_DESCRIPTIONS["read_margin_evidence"]
    assert "market-wide" in text
    assert "for the account" not in text


def test_portfolio_description_is_user_confirmed_snapshot() -> None:
    text = _TOOL_DESCRIPTIONS["read_actual_portfolio"]
    assert "user-confirmed" in text
    # Hard rules added in BUG-005 must survive future edits.
    assert "read_g_context" in text
    assert "analysis/opinion" in text
    assert "skips" in text


def test_g_context_description_analysis_first_rule() -> None:
    text = _TOOL_DESCRIPTIONS["read_g_context"]
    # Owner 2026-09-01: any analysis question calls G FIRST; only pure factual
    # lookups are exempt. The tool description is the channel every client sees.
    assert "HARD RULE" in text
    assert "analysis always consults the G mainline" in text
    assert "skips it" in text
    # Owner 2026-09-01: explicit user opt-out is the only other exemption.
    assert "explicit user opt-out" in text
    # 方案A (2026-09-01): daily hot list = AI-summarized reference info,
    # not teacher opinion; must not be presented as teacher judgment.
    assert "AI-summarized reference information" in text
    assert "not teacher opinions" in text


def test_market_snapshot_description_keeps_gap_honesty_rule() -> None:
    text = _TOOL_DESCRIPTIONS["read_market_snapshot"]
    assert "DATA UNAVAILABLE" in text


def test_instrument_score_description_carries_coverage_boundary() -> None:
    text = _TOOL_DESCRIPTIONS["read_instrument_scores"]
    assert "energy score >= 6.0" in text
    assert "full local-index backfill" in text
    assert "newest row is the current anchor" in text
    assert "does NOT mean ZSXQ" in text
    assert "read_article_search" in text


def test_all_tools_described() -> None:
    assert set(_TOOL_DESCRIPTIONS) == {
        "read_g_context",
        "read_actual_portfolio",
        "read_article_search",
        "read_article",
        "read_market_snapshot",
        "read_market_overview",
        "read_margin_evidence",
        "read_macro_brain",
        "read_shared_brain",
        "read_instrument_scores",
        "read_ready_evidence",
        "read_user_watchlist",
        "read_decision_journal",
        "update_user_watchlist",
        "record_decision",
    }


def test_user_watchlist_description_keeps_user_context_rule() -> None:
    text = _TOOL_DESCRIPTIONS["read_user_watchlist"]
    assert "never investment evidence" in text
    assert "not an error" in text


def test_update_user_watchlist_description_bounds_writes() -> None:
    text = _TOOL_DESCRIPTIONS["update_user_watchlist"]
    assert "add" in text
    assert "remove" in text
    assert "suggest_delete" in text
    assert "NEVER delete" in text
    assert "explicit" in text
    assert "Never apply without the user's explicit confirmation" in text


def test_read_decision_journal_description_pins_review_semantics() -> None:
    text = _TOOL_DESCRIPTIONS["read_decision_journal"]
    # 空表诚实答空（沿 read_user_watchlist 措辞先例）。
    assert "not an error" in text
    # 日志供事实、G 供框架：不得让日志取代 G-first 反证链（外审 Q4-P2）。
    assert "read_g_context" in text
    assert "HARD RULE" in text
    # 被更正记录不隐藏。
    assert "reverted_by" in text


def test_record_decision_description_pins_confirmation_bound() -> None:
    text = _TOOL_DESCRIPTIONS["record_decision"]
    assert "preview" in text
    assert "apply" in text
    assert "owner_stated" in text
    assert "single use" in text
    # 不催记录（anti-nag 纪律）。
    assert "NEVER" in text
    assert "nag" in text
    # headless/one-shot 只 preview 不 apply。
    assert "headless/one-shot" in text
