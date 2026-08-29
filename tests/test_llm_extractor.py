import json

from fin_analyse.claims.llm_extractor import LLMClaimExtractor
from fin_analyse.ingestion.models import Evidence


class MockBackend:
    def __init__(self, response):
        self.response = response

    def complete(self, prompt: str) -> str:
        return json.dumps(self.response)


def test_llm_extractor_produces_claims_with_mock_backend():
    evidence = Evidence(
        evidence_id="zsxq:doc1:text:0",
        source_id="zsxq",
        document_id="zsxq:doc1",
        evidence_type="text_chunk",
        content="华为海思在AI芯片领域取得突破，成为国产算力的核心供应商",
        metadata={"title": "国产算力突破"},
    )

    mock = MockBackend(
        [
            {
                "subject": "华为",
                "predicate": "benefits_from",
                "object_value": "国产算力突破",
                "claim_type": "company_mention",
                "polarity": "positive",
                "horizon": "180d",
                "confidence": 0.85,
                "evidence_text": "华为海思在AI芯片领域取得突破",
            }
        ]
    )
    extractor = LLMClaimExtractor(backend=mock)
    claims = extractor.extract(evidence)

    assert len(claims) == 1
    assert claims[0].subject == "华为"
    assert claims[0].predicate == "benefits_from"
    assert claims[0].extracted_by == "llm"


def test_llm_extractor_returns_empty_without_backend():
    evidence = Evidence(
        evidence_id="e1",
        source_id="zsxq",
        document_id="zsxq:doc1",
        evidence_type="text_chunk",
        content="test",
        metadata={},
    )
    extractor = LLMClaimExtractor(backend=None)
    claims = extractor.extract(evidence)
    assert len(claims) == 0


def test_llm_extractor_handles_malformed_response():
    evidence = Evidence(
        evidence_id="e1",
        source_id="zsxq",
        document_id="zsxq:doc1",
        evidence_type="text_chunk",
        content="test",
        metadata={},
    )
    mock = MockBackend("not valid json {{{")
    extractor = LLMClaimExtractor(backend=mock)
    claims = extractor.extract(evidence)
    assert len(claims) == 0
