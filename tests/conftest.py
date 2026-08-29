"""Shared test fixtures for fin-core (keep-set selection; see docs/migration-manifest.md)."""

import pytest


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
