"""Shared test fixtures and factories for fin-analyse."""

from datetime import UTC, datetime

import pytest


@pytest.fixture(autouse=True)
def _disable_real_industry_chain_llm_in_tests(monkeypatch):
    """Block real LLM calls from IndustryChainAnalyzer in all tests.

    ResearchPackageBuilder._industry_chain_position calls IndustryChainAnalyzer.analyze
    which creates an OpenAICompatibleBackend and makes live HTTP calls.  This fixture
    replaces analyze with a deterministic stub that returns a minimal valid result so
    every test gets a safe default — tests that need real industry chain analysis should
    override this fixture with their own monkeypatch.
    """
    from fin_analyse.analysis.industry_chain import IndustryChainResult

    def _stub_analyze(self, company, ticker="", *, backend=None):
        return IndustryChainResult(
            company=company,
            ticker=ticker,
            industry="测试行业",
            chain_segment="中游制造",
            role="测试角色",
            strategic_importance=5.0,
            substitution_difficulty="中",
            moat_summary="测试护城河总结。",
            methodology_note="测试桩：未调用真实 LLM。",
        )

    monkeypatch.setattr(
        "fin_analyse.analysis.industry_chain.IndustryChainAnalyzer.analyze",
        _stub_analyze,
    )


@pytest.fixture(autouse=True)
def _block_real_llm_backends_in_tests(monkeypatch):
    """Prevent real LLM/HTTP calls across all tests by blocking backend creation.

    Tests that specifically need LLM backends should override this fixture
    with their own monkeypatch.
    """
    monkeypatch.setattr(
        "fin_analyse.claims.config_loader.create_backends_from_config",
        lambda config_path=None: {},
    )

    # Also block direct backend.complete() calls to catch any path
    # that bypasses create_backends_from_config (e.g. _get_default_backend fallback).
    def _blocked_complete(self, prompt, **kwargs):
        raise RuntimeError(
            "LLM call blocked by test conftest. "
            "Use a mock backend or mark the test with @pytest.mark.llm."
        )

    from fin_analyse.claims.claude_backend import ClaudeBackend
    from fin_analyse.claims.hermes_backend import HermesBackend, HermesFileBackend
    from fin_analyse.claims.openai_backend import OpenAICompatibleBackend

    monkeypatch.setattr(OpenAICompatibleBackend, "complete", _blocked_complete)
    monkeypatch.setattr(OpenAICompatibleBackend, "complete_bounded", _blocked_complete)
    monkeypatch.setattr(ClaudeBackend, "complete", _blocked_complete)
    monkeypatch.setattr(HermesBackend, "complete", _blocked_complete)
    monkeypatch.setattr(HermesFileBackend, "complete", _blocked_complete)


@pytest.fixture
def fixed_now() -> datetime:
    """Fixed reference datetime for deterministic tests."""
    return datetime(2026, 6, 20, 9, 0, tzinfo=UTC)


def make_signal(company: str = "宁德时代", score: float = 70.0, **kwargs) -> dict:
    """Factory for a minimal signal dict used across test modules."""
    return {
        "signal_id": "sig-1",
        "company": company,
        "composite_score": score,
        "technical_score": score,
        "valuation_score": 50,
        "flow_score": 50,
        **kwargs,
    }
