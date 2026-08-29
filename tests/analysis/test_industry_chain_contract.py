"""IndustryChain analysis contract tests — strict backend policy, no fallback."""

from __future__ import annotations

import json

import pytest

from fin_analyse.analysis.industry_chain import (
    IndustryChainAnalyzer,
)

# ── factories ───────────────────────────────────────────────────────────────

# Save reference to the *original* analyze method before conftest patches it.
# The conftest uses monkeypatch.setattr which replaces the class attribute;
# we restore the original in our autouse fixture below.
_ORIGINAL_ANALYZE = IndustryChainAnalyzer.analyze


class _FakeKnowledgeStore:
    """Minimal KnowledgeStore stub for IndustryChain tests."""

    def __init__(self):
        self.documents: list = []


def _make_analyzer() -> IndustryChainAnalyzer:
    return IndustryChainAnalyzer(_FakeKnowledgeStore())


@pytest.fixture(autouse=True)
def _restore_real_analyze(_disable_real_industry_chain_llm_in_tests):
    """Restore the real IndustryChainAnalyzer.analyze (undo conftest stub).

    Depends on the conftest fixture to ensure the stub is applied first,
    then restores the original method captured at module-import time.
    """
    IndustryChainAnalyzer.analyze = _ORIGINAL_ANALYZE


# ── backend-unavailable contract tests ──────────────────────────────────────


class TestIndustryChainBackendUnavailable:
    """IndustryChain must return unavailable when no LLM backend is configured."""

    def test_industry_chain_without_backends_returns_unavailable_not_live_fallback(
        self,
    ):
        """When no backend is configured, result must be unavailable, not fallback."""
        analyzer = _make_analyzer()

        # conftest blocks create_backends_from_config → {}
        # so _get_default_backend() naturally returns None
        result = analyzer.analyze("测试公司", ticker="000001")
        payload = result.to_dict()

        # Must signal unavailability
        assert payload.get("status") in {"unavailable", "error"}, (
            f"Expected unavailable/error status, got {payload.get('status')}"
        )

        # Must not be usable for downstream trading decisions
        assert payload.get("trading_decision", True) is False, (
            "unavailable result must not be tradeable"
        )

    def test_get_default_backend_does_not_create_openai_last_resort(self):
        """_get_default_backend must not silently create deepseek-chat fallback."""
        # Default backend resolution should return None when no backends
        # are configured (conftest blocks create_backends_from_config → {}).
        # The key test: the last-resort OpenAICompatibleBackend must NOT be
        # created.
        backend = IndustryChainAnalyzer._get_default_backend()

        # conftest blocks all backends → must return None, not a live backend
        from fin_analyse.claims.openai_backend import OpenAICompatibleBackend

        assert not isinstance(backend, OpenAICompatibleBackend), (
            "_get_default_backend must not fall back to OpenAICompatibleBackend "
            "when no backends are configured"
        )

    def test_analyze_with_none_backend_returns_unavailable(self):
        """Analyze with backend=None must return unavailable result."""
        analyzer = _make_analyzer()

        result = analyzer.analyze("测试公司", ticker="000001", backend=None)

        # When backend is explicitly None and _get_default_backend returns None,
        # the result should signal unavailability.
        payload = result.to_dict()

        # The result should have a stable data gap id indicating LLM unavailability
        assert isinstance(payload.get("data_gaps"), list)
        assert "industry_chain_llm_unavailable" in payload.get("data_gaps", [])

        # Must not claim to write cognition or affect confidence
        assert payload.get("writes_cognition", True) is False, (
            "unavailable result must not write cognition"
        )
        assert payload.get("affects_confidence", True) is False, (
            "unavailable result must not affect confidence"
        )


# ── dual-reference / aggregator contract tests ──────────────────────────────


class _FakeBackend:
    """A completing backend that returns a fixed JSON string."""

    def __init__(self, json_str: str = "{}"):
        self._json = json_str
        self.name = "fake-backend"

    def complete(self, prompt: str) -> str:
        return self._json


def _industry_chain_json(
    confidence: float = 0.8,
    industry: str = "半导体",
    chain_segment: str = "中游制造",
) -> str:
    return json.dumps(
        {
            "industry": industry,
            "chain_segment": chain_segment,
            "role": "测试角色",
            "key_products": ["产品A", "产品B"],
            "key_customers": ["客户X"],
            "key_suppliers": ["供应商Y"],
            "strategic_importance": 7.0,
            "substitution_difficulty": "高",
            "bargaining_power": "中",
            "moat_summary": "测试护城河总结。",
            "confidence": confidence,
            "evidence_sources": ["来源1"],
            "data_gaps": [],
            "failure_conditions": [],
            "catalysts": ["催化剂1"],
            "methodology_note": "双LLM参考测试。",
        },
        ensure_ascii=False,
    )


class _CountingBackend:
    """A completing backend that records how many times it is called."""

    def __init__(self, json_str: str = "{}", name: str = "ref"):
        self._json = json_str
        self.name = name
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return self._json


class TestIndustryChainDualReference:
    """IndustryChain must implement dual-reference + independent aggregator."""

    def test_industry_chain_single_backend_is_not_enough_for_ok_result(self):
        """A single backend must not produce an ok result (strict contract)."""
        analyzer = _make_analyzer()

        result = analyzer.analyze(
            "测试公司",
            ticker="000001",
            backend=_FakeBackend(_industry_chain_json(confidence=0.85)),
        )
        payload = result.to_dict()

        assert payload.get("status") != "ok", (
            f"single backend must not be ok, got {payload.get('status')}"
        )
        assert (
            "industry_chain_consensus_backends_unavailable"
            in payload.get("data_gaps", [])
        )

    def test_industry_chain_matching_reference_outputs_merge_successfully(self):
        """Two matching references merge to ok; aggregator is NOT called."""
        analyzer = _make_analyzer()

        ref_a = _CountingBackend(_industry_chain_json(confidence=0.85), name="ref-a")
        ref_b = _CountingBackend(_industry_chain_json(confidence=0.70), name="ref-b")
        aggregator = _CountingBackend(_industry_chain_json(industry="不应被使用"))

        result = analyzer.analyze(
            "测试公司",
            ticker="000001",
            reference_backends=[ref_a, ref_b],
            aggregator_backend=aggregator,
        )
        payload = result.to_dict()

        assert ref_a.calls == 1, "reference A must be called exactly once"
        assert ref_b.calls == 1, "reference B must be called exactly once"
        assert aggregator.calls == 0, "matching references must not call aggregator"
        assert payload.get("status") == "ok"
        assert payload.get("industry") == "半导体"
        # Merge keeps the higher-confidence reference
        assert payload.get("confidence") == 0.85

    def test_industry_chain_divergent_reference_outputs_require_aggregator(self):
        """Divergent references call the aggregator, and its output is used."""
        analyzer = _make_analyzer()

        ref_a = _CountingBackend(
            _industry_chain_json(industry="半导体", chain_segment="上游原材料"),
            name="ref-a",
        )
        ref_b = _CountingBackend(
            _industry_chain_json(industry="新能源", chain_segment="下游应用"),
            name="ref-b",
        )
        aggregator = _CountingBackend(
            _industry_chain_json(industry="聚合行业", chain_segment="中游制造"),
            name="agg",
        )

        result = analyzer.analyze(
            "测试公司",
            ticker="000001",
            reference_backends=[ref_a, ref_b],
            aggregator_backend=aggregator,
        )
        payload = result.to_dict()

        assert aggregator.calls == 1, "divergent references must call aggregator once"
        assert payload.get("status") == "ok"
        assert payload.get("industry") == "聚合行业", (
            "divergent result must use aggregator output"
        )

    def test_industry_chain_divergent_references_without_aggregator_return_data_gap(
        self,
    ):
        """Divergent references without aggregator → no ok, stable data gap."""
        analyzer = _make_analyzer()

        ref_a = _CountingBackend(_industry_chain_json(industry="半导体"), name="ref-a")
        ref_b = _CountingBackend(_industry_chain_json(industry="新能源"), name="ref-b")

        result = analyzer.analyze(
            "测试公司",
            ticker="000001",
            reference_backends=[ref_a, ref_b],
        )
        payload = result.to_dict()

        assert payload.get("status") != "ok"
        assert "industry_chain_aggregator_unavailable" in payload.get("data_gaps", [])

    def test_industry_chain_aggregator_parse_failure_returns_data_gap(self):
        """Aggregator returns unparseable JSON → no ok, aggregation-parse data gap."""
        analyzer = _make_analyzer()

        ref_a = _CountingBackend(_industry_chain_json(industry="半导体"), name="ref-a")
        ref_b = _CountingBackend(_industry_chain_json(industry="新能源"), name="ref-b")
        aggregator = _CountingBackend("not valid json {{{", name="agg")

        result = analyzer.analyze(
            "测试公司",
            ticker="000001",
            reference_backends=[ref_a, ref_b],
            aggregator_backend=aggregator,
        )
        payload = result.to_dict()

        assert payload.get("status") != "ok"
        gaps = payload.get("data_gaps", [])
        assert (
            "industry_chain_aggregation_parse_failed" in gaps
            or "industry_chain_aggregation_failed" in gaps
        ), f"expected aggregation failure gap, got {gaps}"

    def test_industry_chain_reference_parse_failure_returns_error_with_data_gap(self):
        """A reference returning invalid JSON → error status with data gap."""
        analyzer = _make_analyzer()

        result = analyzer.analyze(
            "测试公司",
            ticker="000001",
            reference_backends=[
                _FakeBackend("not valid json {{{"),
                _FakeBackend(_industry_chain_json()),
            ],
        )
        payload = result.to_dict()

        assert "industry_chain_llm_parse_failed" in payload.get("data_gaps", [])
        assert payload.get("status") == "error"

    def test_industry_chain_reference_exception_returns_error_with_data_gap(self):
        """A reference that raises → error with data gap, not exception propagation."""
        analyzer = _make_analyzer()

        class _CrashBackend:
            name = "crash"

            def complete(self, prompt: str) -> str:
                raise RuntimeError("backend crash")

        result = analyzer.analyze(
            "测试公司",
            ticker="000001",
            reference_backends=[
                _CrashBackend(),
                _FakeBackend(_industry_chain_json()),
            ],
        )
        payload = result.to_dict()

        assert "industry_chain_llm_call_failed" in payload.get("data_gaps", [])
        assert payload.get("status") == "error"


# ── boundary flags contract tests ───────────────────────────────────────────


class TestIndustryChainBoundaryFlags:
    """IndustryChain results must carry correct source/cognition/risk boundaries."""

    def test_industry_chain_result_marks_analysis_synthesis_boundary(self):
        """Result must indicate it is analysis synthesis, not teacher cognition."""
        analyzer = _make_analyzer()

        result = analyzer.analyze(
            "测试公司",
            ticker="000001",
            reference_backends=[
                _FakeBackend(_industry_chain_json()),
                _FakeBackend(_industry_chain_json()),
            ],
        )
        payload = result.to_dict()

        # IndustryChain is analysis synthesis — NOT teacher cognition
        assert payload.get("writes_cognition") is not True, (
            "IndustryChain analysis must not write teacher cognition"
        )
        assert payload.get("affects_confidence") is not True, (
            "IndustryChain analysis must not affect confidence"
        )
        assert payload.get("trading_decision") is not True, (
            "IndustryChain analysis is not a trading decision"
        )
        assert payload.get("advisory_only") is True, (
            "IndustryChain analysis must remain advisory-only"
        )
        assert payload.get("execution_allowed") is False, (
            "IndustryChain analysis must never enable execution"
        )
