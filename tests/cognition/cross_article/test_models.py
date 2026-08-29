"""Tests for cross_article models and validation helpers."""

from __future__ import annotations

import pytest

from fin_analyse.cognition.cross_article.models import (
    ArticleRef,
    ClusterAnalysis,
    QualityFlags,
    SynthesisReport,
    validate_no_trade_fields,
)


def test_article_ref_requires_persona_eligible_source_fields():
    """ArticleRef must hold required source identification fields."""
    article = ArticleRef(
        article_id="a1",
        title="星大派锐评：半导体材料",
        published_at="2026-06-30",
        column="星大派锐评",
        path="knowledge-base/articles/a1.md",
        source_classification="teacher_original",
        persona_eligible=True,
        content_excerpt="材料国产替代进入加速期。",
        metadata={"score": 9.2},
    )

    assert article.article_id == "a1"
    assert article.persona_eligible is True
    assert article.to_dict()["source_classification"] == "teacher_original"


def test_article_ref_rejects_persona_ineligible_source():
    """persona_eligible=False articles must not enter cross_article pipeline."""
    with pytest.raises(ValueError, match="persona_eligible"):
        ArticleRef(
            article_id="a2",
            title="外部研报",
            published_at="2026-06-30",
            column="研报",
            path="knowledge-base/articles/a2.md",
            source_classification="external_research",
            persona_eligible=False,
        )


def test_cluster_analysis_clamps_inferred_confidence_cap():
    """inferred_from_logic stocks must respect confidence cap from evidence_sufficiency."""
    analysis = ClusterAnalysis(
        analysis_id="ca1",
        cluster_id="cluster1",
        generated_at="2026-06-30T12:00:00+08:00",
        article_ids=["a1"],
        core_viewpoints=[],
        mentioned_stocks=[
            {
                "company": "雅克科技",
                "reference_type": "inferred_from_logic",
                "confidence": 0.95,
                "source_articles": ["a1"],
            }
        ],
        evidence_sufficiency={
            "sufficient": True,
            "reason": "direct evidence",
            "allowed_uses": ["focused_stock"],
            "confidence_cap": 0.7,
        },
        quality_mode="single_model",
    )

    assert analysis.mentioned_stocks[0]["confidence"] == 0.7


def test_synthesis_report_is_advisory_and_source_backed():
    """Every SynthesisReport must be advisory_only and carry source refs."""
    report = SynthesisReport(
        synthesis_id="syn1",
        generated_at="2026-06-30T12:00:00+08:00",
        source_article_ids=["a1"],
        source_cluster_ids=["cluster1"],
        sector_directions=[
            {
                "sector": "半导体材料",
                "direction": "关注国产替代",
                "source_clusters": ["cluster1"],
                "source_article_ids": ["a1"],
                "strength": 0.8,
            }
        ],
        focused_stocks=[],
        viewpoint_changes=[],
        quality_flags=QualityFlags(cache_hit=False),
        confidence=0.78,
    )

    payload = report.to_dict()
    assert payload["advisory_only"] is True
    assert payload["quality_flags"]["fallback"] is False
    assert payload["sector_directions"][0]["source_clusters"] == ["cluster1"]


def test_synthesis_report_clamps_direct_mention_confidence():
    """direct_mention stock confidence capped at 0.9."""
    report = SynthesisReport(
        synthesis_id="s1",
        generated_at="2026-06-30T12:00:00+08:00",
        source_article_ids=["a1"],
        source_cluster_ids=["c1"],
        sector_directions=[
            {
                "sector": "s",
                "direction": "d",
                "source_clusters": ["c1"],
                "source_article_ids": ["a1"],
                "strength": 0.5,
            }
        ],
        focused_stocks=[
            {
                "company": "test",
                "reference_type": "direct_mention",
                "confidence": 1.0,
                "source_clusters": ["c1"],
                "source_article_ids": ["a1"],
                "derivation_chain": "",
            }
        ],
        viewpoint_changes=[],
        quality_flags=QualityFlags(),
        confidence=0.5,
    )

    assert report.focused_stocks[0]["confidence"] == 0.9
    assert report.focused_stocks[0]["derivation_chain"]


def test_validate_no_trade_fields_rejects_action_words():
    """Validation must catch trade-related fields that shouldn't appear."""
    with pytest.raises(ValueError, match="trade field"):
        validate_no_trade_fields({"action": "BUY", "position_pct": 0.2})
