"""Tests for cognition LLM helper."""

from datetime import UTC, datetime, timedelta

from fin_analyse.cognition.llm import CognitionCompletionControl, CognitionLLM
from fin_analyse.common.execution_control import ExecutionFence


class FakeBackend:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def test_complete_json_parses_plain_json_object():
    backend = FakeBackend('{"label": "teacher_original", "confidence": 0.8}')
    llm = CognitionLLM(backend=backend)

    result = llm.complete_json("classify", expected_type="source_label")

    assert result.ok is True
    assert result.data == {"label": "teacher_original", "confidence": 0.8}
    assert result.raw == '{"label": "teacher_original", "confidence": 0.8}'
    assert backend.prompts == ["classify"]


def test_complete_json_extracts_fenced_json():
    backend = FakeBackend('说明\n```json\n[{"topic": "液冷"}]\n```')
    llm = CognitionLLM(backend=backend)

    result = llm.complete_json("extract", expected_type="reasoning_traces")

    assert result.ok is True
    assert result.data == [{"topic": "液冷"}]


def test_complete_json_returns_error_for_invalid_json():
    backend = FakeBackend("not json")
    llm = CognitionLLM(backend=backend)

    result = llm.complete_json("extract", expected_type="reasoning_traces")

    assert result.ok is False
    assert result.data is None
    assert "JSON" in result.error


def test_complete_json_logs_warning_on_empty_response(caplog):
    backend = FakeBackend("")
    llm = CognitionLLM(backend=backend)

    with caplog.at_level("WARNING", logger="fin_analyse.cognition.llm"):
        result = llm.complete_json("extract", expected_type="reasoning_traces")

    assert result.ok is False
    assert any("empty response" in record.message for record in caplog.records)


def test_complete_json_logs_warning_on_parse_failure(caplog):
    backend = FakeBackend("not json")
    llm = CognitionLLM(backend=backend)

    with caplog.at_level("WARNING", logger="fin_analyse.cognition.llm"):
        result = llm.complete_json("extract", expected_type="reasoning_traces")

    assert result.ok is False
    assert any("JSON parse failed" in record.message for record in caplog.records)


def test_unavailable_llm_returns_failed_result():
    llm = CognitionLLM(backend=None)

    result = llm.complete_json("extract", expected_type="reasoning_traces")

    assert result.ok is False
    assert result.data is None
    assert result.error == "LLM backend unavailable"


def test_complete_text_returns_response_from_backend():
    backend = FakeBackend("plain text response")
    llm = CognitionLLM(backend=backend)

    result = llm.complete_text("hello")

    assert result == "plain text response"
    assert backend.prompts == ["hello"]


def test_complete_text_returns_empty_when_backend_is_none():
    llm = CognitionLLM(backend=None)

    result = llm.complete_text("hello")

    assert result == ""


def test_complete_text_returns_empty_when_backend_has_no_complete_method():
    llm = CognitionLLM(backend=object())

    result = llm.complete_text("hello")

    assert result == ""


def test_complete_json_extracts_leading_json_object_amidst_noise():
    """JSON extraction should find the first { } or [ ] in the response."""
    backend = FakeBackend('Here is the result:\n{"label": "teacher_original"}\nHope this helps.')
    llm = CognitionLLM(backend=backend)

    result = llm.complete_json("classify", expected_type="source_label")

    assert result.ok is True
    assert result.data == {"label": "teacher_original"}


def test_complete_text_handles_backend_exception():
    class FailingBackend:
        def complete(self, prompt: str) -> str:
            raise RuntimeError("connection refused")

    llm = CognitionLLM(backend=FailingBackend())

    result = llm.complete_text("hello")

    assert result == ""


def test_controlled_completion_never_falls_back_to_unbounded_backend():
    class UnboundedOnlyBackend:
        called = False

        def complete(self, _prompt: str) -> str:
            self.called = True
            return "must not be used"

    backend = UnboundedOnlyBackend()
    llm = CognitionLLM(backend=backend)
    control = CognitionCompletionControl(
        fence=ExecutionFence(datetime.now(UTC) + timedelta(minutes=1)),
        checkpoint=lambda: None,
    )

    result = llm.complete_text("hello", control=control)

    assert result == ""
    assert backend.called is False


def test_controlled_completion_delegates_bounded_budget_and_heartbeat():
    calls: list[dict[str, object]] = []
    heartbeats: list[str] = []

    class BoundedBackend:
        def complete(self, _prompt: str) -> str:
            raise AssertionError("controlled path used unbounded complete")

        def complete_bounded(self, prompt: str, **kwargs: object) -> str:
            calls.append({"prompt": prompt, **kwargs})
            return "bounded"

    llm = CognitionLLM(backend=BoundedBackend())
    control = CognitionCompletionControl(
        fence=ExecutionFence(datetime.now(UTC) + timedelta(minutes=1)),
        checkpoint=lambda: heartbeats.append("beat"),
        wire_timeout_seconds=30,
    )

    result = llm.complete_text("hello", control=control)

    assert result == "bounded"
    assert len(calls) == 1
    assert calls[0]["prompt"] == "hello"
    assert 0 < float(calls[0]["total_timeout_seconds"]) <= 60
    assert calls[0]["wire_timeout_seconds"] == 30
    before_attempt = calls[0]["before_attempt"]
    assert callable(before_attempt)
    before_attempt()
    assert heartbeats
