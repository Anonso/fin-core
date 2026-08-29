"""Tests for claims LLM extractor."""

from unittest.mock import Mock

import pytest

from fin_analyse.claims.llm_extractor import PROMPT_TEMPLATE, LLMBackend, LLMClaimExtractor
from fin_analyse.ingestion.models import Evidence


class TestLLMBackend:
    def test_backend_is_protocol(self):
        # LLMBackend is a Protocol, cannot instantiate directly
        with pytest.raises(TypeError):
            LLMBackend()


class TestLLMClaimExtractor:
    def test_extractor_initialization(self):
        extractor = LLMClaimExtractor(backend=None)
        assert extractor.backend is None

    def test_extract_with_none_backend(self):
        extractor = LLMClaimExtractor(backend=None)
        evidence = Evidence(
            evidence_id="e1",
            source_id="s1",
            document_id="doc1",
            evidence_type="text",
            content="华为发布5G芯片",
            metadata={"title": "Test"},
        )
        claims = extractor.extract(evidence)
        assert len(claims) == 0

    def test_extract_with_mock_backend(self):
        mock_backend = Mock()
        mock_backend.complete.return_value = """[
            {"subject": "华为", "predicate": "benefits_from", "object_value": "5G", "claim_type": "company_mention", "confidence": 0.9}
        ]"""

        extractor = LLMClaimExtractor(backend=mock_backend)
        evidence = Evidence(
            evidence_id="e1",
            source_id="s1",
            document_id="doc1",
            evidence_type="text",
            content="华为发布5G芯片",
            metadata={"title": "Test"},
        )
        claims = extractor.extract(evidence)

        assert len(claims) == 1
        assert claims[0].subject == "华为"
        assert claims[0].claim_type == "company_mention"
        mock_backend.complete.assert_called_once()

    def test_extract_empty_response(self):
        mock_backend = Mock()
        mock_backend.complete.return_value = "[]"

        extractor = LLMClaimExtractor(backend=mock_backend)
        evidence = Evidence(
            evidence_id="e1",
            source_id="s1",
            document_id="doc1",
            evidence_type="text",
            content="some content",
            metadata={"title": "Test"},
        )
        claims = extractor.extract(evidence)

        assert len(claims) == 0

    def test_extract_invalid_json(self):
        mock_backend = Mock()
        mock_backend.complete.return_value = "not json"

        extractor = LLMClaimExtractor(backend=mock_backend)
        evidence = Evidence(
            evidence_id="e1",
            source_id="s1",
            document_id="doc1",
            evidence_type="text",
            content="some content",
            metadata={"title": "Test"},
        )
        claims = extractor.extract(evidence)

        assert len(claims) == 0

    def test_extract_with_code_block(self):
        mock_backend = Mock()
        mock_backend.complete.return_value = '```json\n[{"subject": "华为", "predicate": "benefits_from", "object_value": "5G", "claim_type": "company_mention", "confidence": 0.9}]\n```'

        extractor = LLMClaimExtractor(backend=mock_backend)
        evidence = Evidence(
            evidence_id="e1",
            source_id="s1",
            document_id="doc1",
            evidence_type="text",
            content="华为发布5G芯片",
            metadata={"title": "Test"},
        )
        claims = extractor.extract(evidence)

        assert len(claims) == 1
        assert claims[0].subject == "华为"

    def test_prompt_template_format(self):
        prompt = PROMPT_TEMPLATE.format(title="Test Title", content="Test Content")
        assert "Test Title" in prompt
        assert "Test Content" in prompt
        assert "subject" in prompt
        assert "predicate" in prompt
