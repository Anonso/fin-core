from fin_analyse.claims.extractor import RuleBasedClaimExtractor
from fin_analyse.ingestion.models import Evidence


def make_evidence():
    return Evidence(
        evidence_id="zsxq:doc1:text:0",
        source_id="zsxq",
        document_id="zsxq:doc1",
        evidence_type="text_chunk",
        content="华为海思、海光信息获信创认证。#半导体 #AI芯片",
        metadata={
            "title": "国产算力信创认证",
            "companies": ["华为", "海光信息"],
            "tags": ["半导体", "AI芯片"],
            "score": 8.8,
        },
    )


def test_extractor_creates_company_mention_claims():
    claims = RuleBasedClaimExtractor().extract(make_evidence())

    company_claims = [c for c in claims if c.claim_type == "company_mention"]
    assert {c.subject for c in company_claims} == {"华为", "海光信息"}
    assert all(c.evidence_ids == ["zsxq:doc1:text:0"] for c in company_claims)
    assert all(c.polarity == "positive" for c in company_claims)


def test_extractor_creates_topic_claims():
    claims = RuleBasedClaimExtractor().extract(make_evidence())

    topic_claims = [c for c in claims if c.claim_type == "topic_tag"]
    assert {c.subject for c in topic_claims} == {"半导体", "AI芯片"}
    assert all(c.predicate == "tagged_in" for c in topic_claims)


def test_extractor_creates_score_claim():
    claims = RuleBasedClaimExtractor().extract(make_evidence())

    score_claims = [c for c in claims if c.claim_type == "article_score"]
    assert len(score_claims) == 1
    assert score_claims[0].subject == "zsxq:doc1"
    assert score_claims[0].object_value == "8.8"
    assert score_claims[0].confidence == 1.0


# ---------------------------------------------------------------------------
# Temporal horizon enrichment tests (P0/P1 gap closure)
# ---------------------------------------------------------------------------


def make_intraday_evidence():
    """Evidence with intraday/trading clues → should get short horizon."""
    return Evidence(
        evidence_id="zsxq:intraday:text:0",
        source_id="zsxq",
        document_id="zsxq:intraday",
        evidence_type="text_chunk",
        content="今天盘中涨停！下午跳水后尾盘拉升，盘口情绪明显。",
        metadata={
            "title": "今日盘中观察",
            "companies": ["贵州茅台"],
            "tags": ["白酒"],
            "score": 7.5,
        },
    )


def make_durable_evidence():
    """Evidence with methodology/framework clues → should get durable horizon."""
    return Evidence(
        evidence_id="zsxq:durable:text:0",
        source_id="zsxq",
        document_id="zsxq:durable",
        evidence_type="text_chunk",
        content="从长期来看，估值模型和商业模式决定了企业的护城河深度。"
        "投资哲学的核心是认知框架和定价权分析。",
        metadata={
            "title": "方法论：长期投资框架",
            "companies": ["腾讯"],
            "tags": ["互联网", "方法论"],
            "score": 9.0,
        },
    )


def make_neutral_evidence():
    """Evidence without clear temporal clues → should keep 180d fallback."""
    return Evidence(
        evidence_id="zsxq:neutral:text:0",
        source_id="zsxq",
        document_id="zsxq:neutral",
        evidence_type="text_chunk",
        content="公司发布了新产品线，市场反应平平。",
        metadata={
            "title": "某公司产品发布",
            "companies": ["比亚迪"],
            "tags": ["新能源汽车"],
            "score": 6.0,
        },
    )


class TestRuleBasedClaimExtractorTemporalHorizon:
    """RuleBasedClaimExtractor must enrich claims with deterministic temporal horizon."""

    def test_intraday_clues_produce_intraday_horizon_for_company_claim(self):
        """Content with 盘中/涨停/跌停 etc → company claim horizon='intraday'."""
        claims = RuleBasedClaimExtractor().extract(make_intraday_evidence())
        company_claims = [c for c in claims if c.claim_type == "company_mention"]
        assert len(company_claims) >= 1
        for c in company_claims:
            assert c.horizon == "intraday", (
                f"Company claim with intraday clues should have horizon='intraday', "
                f"got horizon={c.horizon!r}, metadata={c.metadata}"
            )
            assert "time_sensitivity" in c.metadata, (
                f"Company claim metadata should include time_sensitivity, got {c.metadata}"
            )
            assert "temporal_category" in c.metadata, (
                f"Company claim metadata should include temporal_category, got {c.metadata}"
            )

    def test_intraday_clues_produce_intraday_horizon_for_topic_claim(self):
        """Content with intraday clues → topic claim horizon='intraday'."""
        claims = RuleBasedClaimExtractor().extract(make_intraday_evidence())
        topic_claims = [c for c in claims if c.claim_type == "topic_tag"]
        assert len(topic_claims) >= 1
        for c in topic_claims:
            assert c.horizon == "intraday", (
                f"Topic claim with intraday clues should have horizon='intraday', "
                f"got horizon={c.horizon!r}"
            )
            assert "time_sensitivity" in c.metadata

    def test_durable_clues_produce_durable_horizon_for_company_claim(self):
        """Content with 方法论/长期框架 etc → company claim horizon='durable'."""
        claims = RuleBasedClaimExtractor().extract(make_durable_evidence())
        company_claims = [c for c in claims if c.claim_type == "company_mention"]
        assert len(company_claims) >= 1
        for c in company_claims:
            assert c.horizon == "durable", (
                f"Company claim with durable clues should have horizon='durable', "
                f"got horizon={c.horizon!r}"
            )
            assert "time_sensitivity" in c.metadata
            assert c.metadata.get("temporal_category") == "durable_framework"

    def test_durable_clues_produce_durable_horizon_for_topic_claim(self):
        """Content with durable clues → topic claim horizon='durable'."""
        claims = RuleBasedClaimExtractor().extract(make_durable_evidence())
        topic_claims = [c for c in claims if c.claim_type == "topic_tag"]
        assert len(topic_claims) >= 1
        for c in topic_claims:
            assert c.horizon == "durable", (
                f"Topic claim with durable clues should have horizon='durable', "
                f"got horizon={c.horizon!r}"
            )

    def test_neutral_content_keeps_180d_fallback(self):
        """Content without clear temporal clues → horizon stays '180d'."""
        claims = RuleBasedClaimExtractor().extract(make_neutral_evidence())
        company_claims = [c for c in claims if c.claim_type == "company_mention"]
        assert len(company_claims) >= 1
        for c in company_claims:
            assert c.horizon == "180d", (
                f"Company claim without temporal clues should keep horizon='180d', "
                f"got horizon={c.horizon!r}"
            )
            # Should still have temporal metadata (with unknown category)
            assert "time_sensitivity" in c.metadata

    def test_score_claim_also_gets_temporal_metadata(self):
        """Score claims should also receive temporal metadata."""
        claims = RuleBasedClaimExtractor().extract(make_intraday_evidence())
        score_claims = [c for c in claims if c.claim_type == "article_score"]
        assert len(score_claims) == 1
        c = score_claims[0]
        assert "time_sensitivity" in c.metadata, (
            f"Score claim should include time_sensitivity, got {c.metadata}"
        )
        assert "temporal_category" in c.metadata, (
            f"Score claim should include temporal_category, got {c.metadata}"
        )

    def test_temporal_metadata_includes_reason(self):
        """Temporal metadata should include time_sensitivity_reason for audit."""
        claims = RuleBasedClaimExtractor().extract(make_intraday_evidence())
        for c in claims:
            assert "time_sensitivity_reason" in c.metadata, (
                f"Claim {c.claim_id} should include time_sensitivity_reason, "
                f"got {c.metadata}"
            )

    def test_temporal_metadata_includes_unit_type(self):
        """Temporal metadata should include unit_type for half-life resolver."""
        claims = RuleBasedClaimExtractor().extract(make_intraday_evidence())
        for c in claims:
            assert "unit_type" in c.metadata, (
                f"Claim {c.claim_id} should include unit_type, got {c.metadata}"
            )
