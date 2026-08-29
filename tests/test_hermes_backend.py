"""Integration tests for HermesBackend LLM claim extraction."""

import os

import pytest

from fin_analyse.claims.hermes_backend import HermesBackend, create_hermes_backend
from fin_analyse.claims.llm_extractor import LLMClaimExtractor
from fin_analyse.ingestion.models import Evidence

pytestmark = pytest.mark.skipif(
    HermesBackend._find_hermes is None or not os.environ.get("HERMES_API_KEY"),
    reason="Hermes not installed or API key not configured",
)


class TestHermesBackend:
    def test_find_hermes_binary(self):
        backend = HermesBackend()
        assert backend._hermes_bin is not None
        assert "hermes" in backend._hermes_bin

    def test_read_hermes_env(self):
        backend = HermesBackend(profile="fin")
        env = backend._read_hermes_env()
        # Should at least not crash; may or may not have keys
        assert isinstance(env, dict)

    @pytest.mark.llm
    def test_complete_simple_json(self):
        backend = HermesBackend(model="kimi-k2.6", profile="fin")
        response = backend.complete('Reply ONLY this JSON: {"test": true}')
        assert "test" in response

    @pytest.mark.llm
    def test_file_backend(self):
        backend = create_hermes_backend(model="kimi-k2.6", profile="fin", use_file_mode=True)
        response = backend.complete('Reply ONLY this JSON: {"file_mode": true}')
        assert "file_mode" in response


@pytest.mark.llm
class TestLLMExtraction:
    def test_extract_from_evidence(self):
        backend = HermesBackend(model="kimi-k2.6", profile="fin")
        extractor = LLMClaimExtractor(backend=backend)

        evidence = Evidence(
            evidence_id="test:ev:1",
            source_id="zsxq",
            document_id="test:doc:1",
            evidence_type="text_chunk",
            content="华为在2026年获得信创认证，受益于国产算力替代趋势。",
            metadata={"title": "华为信创认证", "score": 8.5},
        )

        claims = extractor.extract(evidence)
        assert isinstance(claims, list)
        # Should extract at least one claim about 华为
        huawei_claims = [c for c in claims if "华为" in c.subject]
        assert len(huawei_claims) >= 1

    def test_extract_rich_claims(self):
        backend = HermesBackend(model="kimi-k2.6", profile="fin")
        extractor = LLMClaimExtractor(backend=backend)

        content = """
        宁德时代发布2026年Q1财报，营收同比增长45%，净利润增长38%。
        公司宣布与特斯拉签订新的电池供应协议，订单金额超200亿元。
        同时面临碳酸锂价格波动风险，原材料成本上升15%。
        """

        evidence = Evidence(
            evidence_id="test:ev:2",
            source_id="zsxq",
            document_id="test:doc:2",
            evidence_type="text_chunk",
            content=content,
            metadata={"title": "宁德时代Q1财报", "score": 9.0},
        )

        claims = extractor.extract(evidence)
        assert len(claims) > 0

        # Check claim structure
        for claim in claims:
            assert claim.subject
            assert claim.predicate
            assert claim.claim_type in (
                "company_mention",
                "industry_signal",
                "event_impact",
                "risk_warning",
            )
            assert claim.polarity in ("positive", "negative", "neutral")
            assert 0 <= claim.confidence <= 1
