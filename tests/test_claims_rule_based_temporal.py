"""Tests for rule-based claim temporal horizon enrichment."""

from __future__ import annotations

from fin_analyse.claims.extractor import RuleBasedClaimExtractor
from fin_analyse.ingestion.models import Evidence


def _evidence(content: str, *, title: str = "Test") -> Evidence:
    return Evidence(
        evidence_id="e1",
        source_id="zsxq",
        document_id="doc1",
        evidence_type="text_chunk",
        content=content,
        metadata={
            "title": title,
            "companies": ["华为"],
            "tags": ["AI"],
            "score": 8,
        },
    )


def test_rule_based_claims_get_intraday_horizon_from_content() -> None:
    evidence = _evidence("今日盘面出现情绪杀，盘口快速跳水后又拉升，适合盘中跟踪。")

    claims = RuleBasedClaimExtractor().extract(evidence)

    assert claims
    assert {claim.horizon for claim in claims} == {"intraday"}
    for claim in claims:
        assert claim.metadata["time_sensitivity"] == "intraday"
        assert claim.metadata["temporal_category"] == "intraday_event"
        assert claim.metadata["time_sensitivity_reason"]
        assert claim.metadata["temporal_evidence"]


def test_rule_based_claims_get_durable_horizon_from_content() -> None:
    evidence = _evidence("这篇主要讨论长期投资方法论、认知框架和商业模式护城河。")

    claims = RuleBasedClaimExtractor().extract(evidence)

    assert claims
    assert {claim.horizon for claim in claims} == {"durable"}
    for claim in claims:
        assert claim.metadata["time_sensitivity"] == "durable"
        assert claim.metadata["temporal_category"] == "durable_framework"


def test_rule_based_claims_keep_180d_when_temporal_clues_unknown() -> None:
    evidence = _evidence("普通内容，没有足够明确的时效性线索。")

    claims = RuleBasedClaimExtractor().extract(evidence)

    assert claims
    assert {claim.horizon for claim in claims} == {"180d"}
    for claim in claims:
        assert claim.metadata["time_sensitivity"] == "180d"
        assert claim.metadata["temporal_category"] == "unknown"
        assert claim.metadata["data_gap"] == "no_temporal_clues"
