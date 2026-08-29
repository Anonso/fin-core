"""Tests for G→Z suggested_signal_queries builder."""

from __future__ import annotations

import pytest

from fin_analyse.cognition.cross_article.models import (
    QualityFlags,
    SuggestedSignalQuery,
    SynthesisReport,
    build_suggested_signal_queries,
)


def _make_sector_direction(sector: str, clusters: list[str], articles: list[str]) -> dict:
    return {
        "sector": sector,
        "direction": "看好",
        "source_clusters": clusters,
        "source_article_ids": articles,
        "strength": 0.8,
    }


def _make_stock(
    company: str,
    ticker: str,
    ref_type: str,
    confidence: float,
    clusters: list[str],
    articles: list[str],
    evidence_sufficient: bool = True,
) -> dict:
    return {
        "company": company,
        "ticker": ticker,
        "reference_type": ref_type,
        "confidence": confidence,
        "source_clusters": clusters,
        "source_article_ids": articles,
        "derivation_chain": "test chain",
        "evidence_mode": "sufficient" if evidence_sufficient else "observation_only",
    }


def test_direct_mention_with_ticker_emits_high_priority():
    """Direct mention + ticker + sufficient evidence → high priority query."""
    queries = build_suggested_signal_queries(
        focused_stocks=[
            _make_stock("雅克科技", "002409", "direct_mention", 0.9, ["c1"], ["a1"]),
        ],
    )

    assert len(queries) == 1
    q = queries[0]
    assert q.company == "雅克科技"
    assert q.ticker == "002409"
    assert q.priority == "high"
    assert "analyze_stock" in q.query_tools
    assert "get_market_snapshot" in q.query_tools
    assert q.reference_type == "direct_mention"
    assert q.evidence_mode == "sufficient"


def test_direct_mention_without_ticker_still_emits():
    """Direct mention without ticker emits query with company-only args."""
    queries = build_suggested_signal_queries(
        focused_stocks=[
            _make_stock("某科技", "", "direct_mention", 0.85, ["c1"], ["a1"]),
        ],
    )

    assert len(queries) == 1
    q = queries[0]
    assert q.priority == "high"
    # analyze_stock can use company name without ticker
    assert q.tool_args.get("analyze_stock", {}).get("company") == "某科技"


def test_inferred_stock_emits_medium_priority():
    """Inferred stocks get medium priority."""
    queries = build_suggested_signal_queries(
        focused_stocks=[
            _make_stock("铜陵有色", "000630", "inferred_from_logic", 0.55, ["c1"], ["a1"]),
        ],
    )

    assert len(queries) == 1
    assert queries[0].priority == "medium"


def test_mixed_stocks_generate_correct_priorities():
    """Direct + inferred stocks with mixed evidence."""
    queries = build_suggested_signal_queries(
        focused_stocks=[
            _make_stock("中船特气", "688146", "direct_mention", 0.9, ["c1"], ["a1"]),
            _make_stock("中钨高新", "000657", "inferred_from_logic", 0.5, ["c2"], ["a2"]),
            _make_stock("江丰电子", "300666", "direct_mention", 0.88, ["c1"], ["a1"]),
        ],
    )

    assert len(queries) == 3
    priorities = {q.company: q.priority for q in queries}
    assert priorities["中船特气"] == "high"
    assert priorities["中钨高新"] == "medium"
    assert priorities["江丰电子"] == "high"


def test_no_stocks_produces_empty_queries():
    """Empty focused_stocks → empty queries."""
    queries = build_suggested_signal_queries(focused_stocks=[])
    assert queries == []


def test_query_never_includes_trade_actions():
    """Query intents must not contain buy/sell/position."""
    queries = build_suggested_signal_queries(
        focused_stocks=[
            _make_stock("test", "000001", "direct_mention", 0.9, ["c1"], ["a1"]),
        ],
    )
    q = queries[0].to_dict()
    flat = str(q).lower()
    for word in ("buy", "sell", "买入", "卖出", "加仓", "减仓", "position_pct"):
        assert word not in flat


def test_suggested_signal_query_model_roundtrip():
    """Model serializes and deserializes correctly."""
    q = SuggestedSignalQuery(
        company="雅克科技",
        ticker="002409",
        reason="G 中直接点名",
        priority="high",
        query_tools=["analyze_stock"],
        tool_args={
            "analyze_stock": {"company": "雅克科技", "ticker": "002409"},
        },
        reference_type="direct_mention",
        source_clusters=["c1"],
        source_article_ids=["a1"],
        evidence_mode="sufficient",
    )

    d = q.to_dict()
    assert d["company"] == "雅克科技"
    assert d["priority"] == "high"
    q2 = SuggestedSignalQuery.from_dict(d)
    assert q2 == q


def test_suggested_signal_query_tool_args_match_current_mcp_signatures():
    """Every tool_args must match current MCP signatures exactly."""
    queries = build_suggested_signal_queries(
        focused_stocks=[
            _make_stock("雅克科技", "002409", "direct_mention", 0.9, ["c1"], ["a1"]),
        ],
    )

    q = queries[0].to_dict()

    assert q["company"] == "雅克科技"
    assert q["ticker"] == "002409"
    assert q["tool_args"]["analyze_stock"] == {
        "company": "雅克科技",
        "ticker": "002409",
    }
    assert q["tool_args"]["get_market_snapshot"] == {"ticker": "002409"}

    allowed_args = {
        "analyze_stock": {
            "ticker_or_name",
            "question",
            "window",
            "company",
            "ticker",
            "focus",
        },
        "get_market_snapshot": {"ticker"},
    }
    for tool_name, args in q["tool_args"].items():
        assert set(args) <= allowed_args[tool_name], f"{tool_name}: {set(args)} has extra keys"


def test_synthesis_report_includes_suggested_queries():
    """SynthesisReport carries suggested_signal_queries field."""
    queries = build_suggested_signal_queries(
        focused_stocks=[
            _make_stock("雅克科技", "002409", "direct_mention", 0.9, ["c1"], ["a1"]),
        ],
    )

    report = SynthesisReport(
        synthesis_id="s1",
        generated_at="2026-06-30T12:00:00+08:00",
        source_article_ids=["a1"],
        source_cluster_ids=["c1"],
        sector_directions=[
            _make_sector_direction("半导体", ["c1"], ["a1"]),
        ],
        focused_stocks=[
            _make_stock("雅克科技", "002409", "direct_mention", 0.9, ["c1"], ["a1"]),
        ],
        viewpoint_changes=[],
        quality_flags=QualityFlags(),
        confidence=0.8,
        suggested_signal_queries=queries,
    )

    d = report.to_dict()
    assert report.execution_allowed is False
    assert d["execution_allowed"] is False
    assert len(d["suggested_signal_queries"]) == 1
    assert d["suggested_signal_queries"][0]["company"] == "雅克科技"


def test_synthesis_report_rejects_execution_allowed_true():
    with pytest.raises(ValueError, match="execution_allowed"):
        SynthesisReport(
            synthesis_id="s1",
            generated_at="2026-06-30T12:00:00+08:00",
            source_article_ids=["a1"],
            source_cluster_ids=["c1"],
            sector_directions=[
                _make_sector_direction("半导体", ["c1"], ["a1"]),
            ],
            focused_stocks=[],
            viewpoint_changes=[],
            quality_flags=QualityFlags(),
            confidence=0.8,
            execution_allowed=True,
        )


def test_synthesis_report_from_legacy_dict_backfills_suggested_queries():
    legacy = {
        "synthesis_id": "syn-legacy",
        "generated_at": "2026-06-30T12:00:00+08:00",
        "source_article_ids": ["a1"],
        "source_cluster_ids": ["c1"],
        "sector_directions": [
            _make_sector_direction("半导体", ["c1"], ["a1"]),
        ],
        "focused_stocks": [
            _make_stock("雅克科技", "002409", "direct_mention", 0.9, ["c1"], ["a1"]),
        ],
        "viewpoint_changes": [],
        "quality_flags": {"cache_hit": False},
        "confidence": 0.8,
        # Legacy payload intentionally has no suggested_signal_queries.
    }

    report = SynthesisReport.from_dict(legacy)
    payload = report.to_dict()

    assert report.execution_allowed is False
    assert payload["execution_allowed"] is False
    assert len(payload["suggested_signal_queries"]) == 1
    query = payload["suggested_signal_queries"][0]
    assert query["company"] == "雅克科技"
    assert query["ticker"] == "002409"
    assert query["priority"] == "high"
    assert "get_signals" not in query["tool_args"]
