"""Tests for SelectiveTraceVerifier and low-confidence trace selection."""

from fin_analyse.cognition.llm import CognitionLLM
from fin_analyse.cognition.models import EvidenceItem, ReasoningTrace, SourceLabel
from fin_analyse.cognition.trace_verifier import (
    SelectiveTraceVerifier,
    TraceVerificationError,
    select_low_confidence_traces,
)


class FakeBackend:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _make_evidence() -> EvidenceItem:
    return EvidenceItem(
        evidence_id="ev-001",
        source_type="zsxq_article",
        source_id="art-001",
        title="铜相关投资工具选择",
        content="我更倾向于观察铜价和资源股联动，不要把短期价格波动直接当成长期结论。",
        author="郭老师",
        published_at="2026-06-01",
        collected_at="2026-06-01",
        companies=["紫金矿业"],
        topics=["铜"],
        source_label=SourceLabel("teacher_original", "guo", 0.8, []),
        reliability=0.8,
    )


def _make_trace(trace_id: str = "trace-001", confidence: float = 0.42) -> ReasoningTrace:
    return ReasoningTrace(
        trace_id=trace_id,
        teacher_id="guo",
        source_evidence_id="ev-001",
        topic="铜相关投资工具选择",
        companies=["紫金矿业"],
        premises=["铜价波动影响资源股"],
        observed_variables=["铜价", "资源股联动"],
        inferred_relationships=["铜价上涨→资源股受益"],
        conclusion="关注铜相关投资工具，但不追高",
        stance="watch",
        time_horizon="mid",
        risk_boundaries=["短期价格波动可能误导"],
        invalidation_conditions=["铜价无法持续"],
        action_implications=["跟踪铜价与资源股联动"],
        extraction_confidence=confidence,
    )


def test_select_low_confidence_traces_sorts_and_filters():
    high = _make_trace("trace-high", 0.8)
    low = _make_trace("trace-low", 0.42)
    mid = _make_trace("trace-mid", 0.5)

    selected = select_low_confidence_traces([high, low, mid], threshold=0.5, limit=10)

    assert [t.trace_id for t in selected] == ["trace-low", "trace-mid"]


def test_select_low_confidence_traces_respects_limit():
    traces = [_make_trace(f"trace-{i}", 0.4 + i * 0.01) for i in range(5)]

    selected = select_low_confidence_traces(traces, threshold=0.5, limit=2)

    assert len(selected) == 2
    assert [t.trace_id for t in selected] == ["trace-0", "trace-1"]


def test_verifier_returns_keep_verification():
    backend = FakeBackend(
        '{"verdict":"keep","verified_confidence":0.72,"confidence_adjustment":0.3,'
        '"issues":[],"suggested_revision":{},"reason":"trace 与原文一致"}'
    )
    verifier = SelectiveTraceVerifier(CognitionLLM(backend=backend), verifier_backend="gpt5")

    verification = verifier.verify(_make_trace(), _make_evidence())

    assert verification.trace_id == "trace-001"
    assert verification.source_evidence_id == "ev-001"
    assert verification.verdict == "keep"
    assert verification.verified_confidence == 0.72
    assert verification.confidence_adjustment == 0.3
    assert verification.verifier_backend == "gpt5"
    assert verification.verification_id.startswith("tv-")
    assert backend.prompts
    assert "铜相关投资工具选择" in backend.prompts[0]


def test_verifier_returns_revise_with_suggested_revision():
    backend = FakeBackend(
        '{"verdict":"revise","verified_confidence":0.61,"confidence_adjustment":0.19,'
        '"issues":["结论偏强"],'
        '"suggested_revision":{"conclusion":"关注但等待验证","stance":"watch"},'
        '"reason":"原文支持关注，不支持强买入"}'
    )
    verifier = SelectiveTraceVerifier(CognitionLLM(backend=backend), verifier_backend="gpt5")

    verification = verifier.verify(_make_trace(), _make_evidence())

    assert verification.verdict == "revise"
    assert verification.issues == ["结论偏强"]
    assert verification.suggested_revision["conclusion"] == "关注但等待验证"


def test_verifier_rejects_invalid_json():
    backend = FakeBackend("not json")
    verifier = SelectiveTraceVerifier(CognitionLLM(backend=backend), verifier_backend="gpt5")

    try:
        verifier.verify(_make_trace(), _make_evidence())
    except TraceVerificationError as exc:
        assert "JSON" in str(exc) or "LLM" in str(exc)
    else:
        raise AssertionError("expected TraceVerificationError")


def test_verifier_rejects_invalid_verdict():
    backend = FakeBackend(
        '{"verdict":"maybe","verified_confidence":0.5,"confidence_adjustment":0,'
        '"issues":[],"suggested_revision":{},"reason":"invalid"}'
    )
    verifier = SelectiveTraceVerifier(CognitionLLM(backend=backend), verifier_backend="gpt5")

    try:
        verifier.verify(_make_trace(), _make_evidence())
    except TraceVerificationError as exc:
        assert "Invalid verdict" in str(exc)
    else:
        raise AssertionError("expected TraceVerificationError")


def test_verifier_clamps_confidence_fields():
    backend = FakeBackend(
        '{"verdict":"keep","verified_confidence":2,"confidence_adjustment":-2,'
        '"issues":[],"suggested_revision":{},"reason":"clamp"}'
    )
    verifier = SelectiveTraceVerifier(CognitionLLM(backend=backend), verifier_backend="gpt5")

    verification = verifier.verify(_make_trace(), _make_evidence())

    assert verification.verified_confidence == 1.0
    assert verification.confidence_adjustment == -1.0
