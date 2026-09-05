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


#: NOW #30 / BUG-029 防复发：每个 MCP 工具的 handler 参数被显式表格钉死。
#: 工具描述会主动指示 agent 传参——BUG-029 事故即「描述让传 date_from、
#: 通用 handler 签名没有，pydantic extra=ignore 在 MCP 面静默丢参」。
#: 描述承诺的参数与 handler 签名的对账在此强制：改任何工具参数必须同改本表。
_TOOL_PARAM_TABLE: dict[str, set[str]] = {
    # 通用只读面（_make_tool_handler）
    **{
        name: {
            "question",
            "instruments",
            "article_id",
            "as_of",
            "deadline_seconds",
            "session_hint",
        }
        for name in (
            "read_g_context",
            "read_actual_portfolio",
            "read_market_snapshot",
            "read_market_overview",
            "read_margin_evidence",
            "read_ready_evidence",
            "read_instrument_scores",
            "read_article",
            "read_macro_brain",
            "read_shared_brain",
            "read_user_watchlist",
        )
    },
    # 唯一带日期枚举的检索工具（custom handler，BUG-029 修复面）
    "read_article_search": {
        "question",
        "instruments",
        "article_id",
        "as_of",
        "date_from",
        "date_to",
        "deadline_seconds",
        "session_hint",
    },
    # 写缝与 journal 读（各自 custom handler）
    "update_user_watchlist": {"action", "operations", "question", "session_hint", "token"},
    "read_decision_journal": {
        "question",
        "symbol",
        "decision_type",
        "date_from",
        "date_to",
        "limit",
        "session_hint",
    },
    "record_decision": {
        "question",
        "action",
        "symbol",
        "decision_type",
        "decision_date",
        "rationale",
        "note",
        "revert_of",
        "date_from",
        "date_to",
        "limit",
        "token",
        "session_hint",
    },
}


def test_handler_signatures_match_pinned_param_table() -> None:
    """描述承诺的参数 ⊆ handler 签名，逐工具对账（NOW #30）。"""
    import inspect

    from fin_analyse.read_capabilities.server import (
        _make_article_search_handler,
        _make_decision_journal_read_handler,
        _make_record_decision_handler,
        _make_tool_handler,
        _make_watchlist_handler,
    )

    factories = {
        "update_user_watchlist": _make_watchlist_handler,
        "read_decision_journal": _make_decision_journal_read_handler,
        "record_decision": _make_record_decision_handler,
        "read_article_search": _make_article_search_handler,
    }

    assert set(_TOOL_PARAM_TABLE) == set(_TOOL_DESCRIPTIONS), (
        "工具清单与参数表不同步：新增/删除工具必须同改 _TOOL_PARAM_TABLE"
    )

    for tool, expected in _TOOL_PARAM_TABLE.items():
        factory = factories.get(tool)
        handler = factory() if factory is not None else _make_tool_handler(tool)
        actual = set(inspect.signature(handler).parameters)
        assert actual == expected, (
            f"{tool}: handler 参数 {sorted(actual)} 与钉死表格 {sorted(expected)} 不一致——"
            "若描述新增承诺参数，handler 签名与本表必须同步（BUG-029 教训）"
        )
