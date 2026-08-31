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


def test_market_snapshot_description_keeps_gap_honesty_rule() -> None:
    text = _TOOL_DESCRIPTIONS["read_market_snapshot"]
    assert "DATA UNAVAILABLE" in text


def test_seven_read_tools_all_described() -> None:
    assert set(_TOOL_DESCRIPTIONS) == {
        "read_g_context",
        "read_actual_portfolio",
        "read_market_snapshot",
        "read_market_overview",
        "read_margin_evidence",
        "read_ready_evidence",
        "read_user_watchlist",
    }


def test_user_watchlist_description_keeps_user_context_rule() -> None:
    text = _TOOL_DESCRIPTIONS["read_user_watchlist"]
    assert "never investment evidence" in text
    assert "not an error" in text
