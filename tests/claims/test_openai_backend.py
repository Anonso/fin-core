"""Tests for OpenAICompatibleBackend failure diagnostics propagation."""

from __future__ import annotations

from unittest.mock import patch

import httpx
from openai import InternalServerError

from fin_analyse.claims.openai_backend import OpenAICompatibleBackend

# Save the original complete method at module-import time (before the
# conftest autouse fixture patches it to _blocked_complete).
_ORIGINAL_COMPLETE = OpenAICompatibleBackend.complete
_ORIGINAL_COMPLETE_BOUNDED = OpenAICompatibleBackend.complete_bounded


def _choice_response(content: str, finish_reason: str):
    """Minimal OpenAI response stub with one choice."""
    return type(
        "Response",
        (),
        {
            "choices": [
                type(
                    "Choice",
                    (),
                    {
                        "message": type("Message", (), {"content": content})(),
                        "finish_reason": finish_reason,
                    },
                )()
            ]
        },
    )()


def test_complete_bounded_caps_each_wire_call_and_disables_sdk_retries(monkeypatch):
    monkeypatch.setattr(
        OpenAICompatibleBackend,
        "complete_bounded",
        _ORIGINAL_COMPLETE_BOUNDED,
    )
    options: list[dict[str, object]] = []
    heartbeats: list[str] = []

    class _Completions:
        @staticmethod
        def create(**_kwargs):
            return "bounded answer"

    class _Client:
        chat = type("Chat", (), {"completions": _Completions()})()

        def with_options(self, **kwargs):
            options.append(kwargs)
            return self

    backend = OpenAICompatibleBackend(
        model="gpt-5.6-sol",
        api_key="sk-test",
        timeout=600,
    )
    monkeypatch.setattr(backend, "_get_client", lambda _index=0: _Client())

    result = backend.complete_bounded(
        "prompt",
        total_timeout_seconds=120,
        wire_timeout_seconds=90,
        before_attempt=lambda: heartbeats.append("beat"),
    )

    assert result == "bounded answer"
    assert heartbeats == ["beat"]
    assert options == [{"timeout": 60.0, "max_retries": 0}]

    options.clear()
    heartbeats.clear()
    backend.timeout = 15
    assert (
        backend.complete_bounded(
            "prompt",
            total_timeout_seconds=120,
            wire_timeout_seconds=90,
            before_attempt=lambda: heartbeats.append("beat"),
        )
        == "bounded answer"
    )
    assert heartbeats == ["beat"]
    assert options == [{"timeout": 15.0, "max_retries": 0}]


def test_complete_bounded_stops_before_wire_when_monotonic_budget_is_spent(monkeypatch):
    monkeypatch.setattr(
        OpenAICompatibleBackend,
        "complete_bounded",
        _ORIGINAL_COMPLETE_BOUNDED,
    )
    backend = OpenAICompatibleBackend(model="gpt-5.6-sol", api_key="sk-test")
    client = type(
        "Client",
        (),
        {
            "with_options": lambda self, **_kwargs: self,
            "chat": type(
                "Chat",
                (),
                {
                    "completions": type(
                        "Completions",
                        (),
                        {
                            "create": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                                AssertionError("wire call escaped total budget")
                            )
                        },
                    )()
                },
            )(),
        },
    )()
    monkeypatch.setattr(backend, "_get_client", lambda _index=0: client)
    monotonic_values = iter((100.0, 161.0))
    monkeypatch.setattr(
        "fin_analyse.claims.openai_backend.time.monotonic",
        lambda: next(monotonic_values),
    )

    result = backend.complete_bounded(
        "prompt",
        total_timeout_seconds=60,
        wire_timeout_seconds=60,
        before_attempt=lambda: None,
    )

    assert result == "[]"
    assert backend.last_failure is not None
    assert backend.last_failure["error_type"] == "LLMCompletionDeadlineExceeded"


def test_complete_bounded_heartbeats_before_each_application_retry(monkeypatch):
    monkeypatch.setattr(
        OpenAICompatibleBackend,
        "complete_bounded",
        _ORIGINAL_COMPLETE_BOUNDED,
    )
    attempts = 0
    options: list[dict[str, object]] = []
    heartbeats: list[str] = []

    class _Completions:
        @staticmethod
        def create(**_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("first wire timed out")
            return "recovered"

    class _Client:
        chat = type("Chat", (), {"completions": _Completions()})()

        def with_options(self, **kwargs):
            options.append(kwargs)
            return self

    backend = OpenAICompatibleBackend(model="gpt-5.6-sol", api_key="sk-test")
    monkeypatch.setattr(backend, "_get_client", lambda _index=0: _Client())
    monkeypatch.setattr("fin_analyse.claims.openai_backend.time.sleep", lambda _seconds: None)

    result = backend.complete_bounded(
        "prompt",
        total_timeout_seconds=120,
        wire_timeout_seconds=60,
        before_attempt=lambda: heartbeats.append("beat"),
    )

    assert result == "recovered"
    assert heartbeats == ["beat", "beat"]
    assert options == [
        {"timeout": 60.0, "max_retries": 0},
        {"timeout": 60.0, "max_retries": 0},
    ]


class TestBackendFailureRecording:
    """OpenAICompatibleBackend records sanitized failure metadata on exception."""

    def test_complete_records_last_failure_on_http_error(self, monkeypatch):
        """When the OpenAI client raises InternalServerError, complete()
        records sanitized failure metadata in last_failure."""
        # Undo the conftest autouse block so the real complete() runs
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)

        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol",
            api_key="sk-test-dummy-key-12345",
            base_url="https://ai.codesonline.dev/v1",
        )

        # Build a realistic HTTP 500 error
        request = httpx.Request("POST", "https://ai.codesonline.dev/v1/chat/completions")
        response = httpx.Response(status_code=500, request=request)
        error = InternalServerError(
            message="Upstream gateway error",
            response=response,
            body={
                "error": {
                    "message": "Upstream gateway error",
                    "type": "api_error",
                    "code": "internal_error",
                }
            },
        )

        # Simulate client raising the error
        with patch.object(backend, "_get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.chat.completions.create.side_effect = error

            result = backend.complete("test prompt")

        # Backward compatible: returns "[]" on failure
        assert result == "[]", f"Expected '[]' on failure, got {result!r}"

        # NEW: last_failure is recorded
        assert backend.last_failure is not None, (
            "Expected last_failure to be populated after HTTP error"
        )
        failure = backend.last_failure

        # Sanitized: must NOT contain api_key
        failure_str = str(failure)
        assert "sk-test" not in failure_str, f"last_failure must not contain api_key: {failure_str}"
        assert "api_key" not in failure, f"last_failure must not contain api_key field: {failure}"

        # Contains backend logical name (model)
        assert failure.get("backend_name") == "gpt-5.6-sol", (
            f"Expected backend_name=gpt-5.6-sol, got {failure.get('backend_name')!r}"
        )

        # Contains model name
        assert failure.get("model") == "gpt-5.6-sol", (
            f"Expected model=gpt-5.6-sol, got {failure.get('model')!r}"
        )

        # Contains base_url host
        assert "base_url" in failure, (
            f"Expected base_url in last_failure, got keys: {list(failure.keys())}"
        )
        assert "ai.codesonline.dev" in str(failure["base_url"]), (
            f"Expected base_url host in failure, got {failure['base_url']!r}"
        )

        # Contains HTTP status code
        assert failure.get("http_status") == 500, (
            f"Expected http_status=500, got {failure.get('http_status')!r}"
        )

        # Contains error type
        assert "error_type" in failure, (
            f"Expected error_type in last_failure, got keys: {list(failure.keys())}"
        )

        assert "Upstream gateway error" in str(failure.get("error_message", "")), (
            f"Expected 'Upstream gateway error' in error_message, "
            f"got {failure.get('error_message')!r}"
        )

    def test_raw_string_response_is_success_without_backup_or_cooldown(self, monkeypatch):
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)
        monkeypatch.setattr(
            OpenAICompatibleBackend,
            "_get_client",
            lambda self, _index=0: type(
                "Client",
                (),
                {
                    "chat": type(
                        "Chat",
                        (),
                        {
                            "completions": type(
                                "Completions",
                                (),
                                {"create": lambda *_args, **_kwargs: "plain answer"},
                            )()
                        },
                    )()
                },
            )(),
        )
        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol",
            api_key="sk-test",
            endpoints=[
                {"name": "endpoint-a", "api_key": "sk-a", "base_url": "https://a"},
                {"name": "codesonline", "api_key": "sk-b", "base_url": "https://b"},
            ],
        )
        assert backend.complete("prompt") == "plain answer"
        assert backend.last_failure is None

    def test_endpoint_overrides_model_and_reasoning_effort(self, monkeypatch):
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)
        calls = []

        def create(*_args, **kwargs):
            calls.append(kwargs)
            return "answer"

        monkeypatch.setattr(
            OpenAICompatibleBackend,
            "_get_client",
            lambda self, _index=0: type(
                "Client",
                (),
                {
                    "chat": type(
                        "Chat",
                        (),
                        {"completions": type("Completions", (), {"create": create})()},
                    )()
                },
            )(),
        )
        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol",
            reasoning_effort="max",
            api_key="sk-test",
            endpoints=[
                {
                    "name": "proxy-b",
                    "api_key": "sk-b",
                    "base_url": "https://b",
                    "model": "grok-4.6",
                    "reasoning_effort": "high",
                }
            ],
        )

        assert backend.complete("prompt") == "answer"
        assert calls[0]["model"] == "grok-4.6"
        assert calls[0]["reasoning_effort"] == "high"

    def test_empty_primary_uses_backup_and_only_all_failures_record_cooldown(self, monkeypatch):
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)
        calls = iter(["", "backup answer"])
        monkeypatch.setattr(
            OpenAICompatibleBackend,
            "_get_client",
            lambda self, _index=0: type(
                "Client",
                (),
                {
                    "chat": type(
                        "Chat",
                        (),
                        {
                            "completions": type(
                                "Completions", (), {"create": lambda *_args, **_kwargs: next(calls)}
                            )()
                        },
                    )()
                },
            )(),
        )
        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol",
            api_key="sk-test",
            endpoints=[{"name": "endpoint-a"}, {"name": "codesonline"}],
        )
        assert backend.complete("prompt") == "backup answer"
        assert backend.last_failure is None

    def test_chat_with_tools_accepts_raw_string(self, monkeypatch):
        monkeypatch.setattr(
            OpenAICompatibleBackend,
            "_get_client",
            lambda self, _index=0: type(
                "Client",
                (),
                {
                    "chat": type(
                        "Chat",
                        (),
                        {
                            "completions": type(
                                "Completions",
                                (),
                                {"create": lambda *_args, **_kwargs: "final answer"},
                            )()
                        },
                    )()
                },
            )(),
        )
        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol", api_key="sk-test", endpoints=[{"name": "endpoint-a"}]
        )
        assert backend.chat_with_tools([{"role": "user", "content": "hi"}], []) == "final answer"

    def test_tool_result_is_not_replayed_when_backup_continues(self, monkeypatch):
        calls = []
        tool_calls = [
            type(
                "TC",
                (),
                {"id": "1", "function": type("Fn", (), {"name": "lookup", "arguments": "{}"})()},
            )()
        ]
        primary_response = type(
            "Resp",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Msg", (), {"tool_calls": tool_calls, "content": None})()},
                    )()
                ]
            },
        )()
        backup_response = type(
            "Resp",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Msg", (), {"tool_calls": [], "content": "done"})()},
                    )()
                ]
            },
        )()

        def make_client(index):
            class Completions:
                def create(self, **kwargs):
                    calls.append((index, kwargs["messages"]))
                    if index == 0:
                        if len(calls) == 1:
                            return primary_response
                        raise RuntimeError("primary down")
                    return backup_response

            return type(
                "Client", (), {"chat": type("Chat", (), {"completions": Completions()})()}
            )()

        monkeypatch.setattr(
            OpenAICompatibleBackend, "_get_client", lambda self, index=0: make_client(index)
        )
        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol", api_key="sk-test", endpoints=[{"name": "h"}, {"name": "c"}]
        )
        executed = []
        result = backend.chat_with_tools(
            [{"role": "user", "content": "hi"}],
            [],
            tool_executor=lambda name, args: executed.append(name) or "value",
        )
        assert result == "done"
        assert executed == ["lookup"]
        assert calls[-1][0] == 1
        assert len(calls[-1][1]) == 3

    def test_complete_records_last_failure_with_flat_body(self, monkeypatch):
        """When InternalServerError body is flat (no nested "error" key),
        _sanitize_failure extracts message and type from the top-level body."""
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)

        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol",
            api_key="sk-test-dummy-key-12345",
            base_url="https://ai.codesonline.dev/v1",
        )

        # Flat body as seen in real GPT5 smoke tests
        request = httpx.Request("POST", "https://ai.codesonline.dev/v1/chat/completions")
        response = httpx.Response(status_code=500, request=request)
        error = InternalServerError(
            message="Upstream gateway error",
            response=response,
            body={"message": "Upstream gateway error", "type": "api_error"},
        )

        with patch.object(backend, "_get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.chat.completions.create.side_effect = error

            result = backend.complete("test prompt")

        assert result == "[]"
        assert backend.last_failure is not None

        failure = backend.last_failure

        # Must NOT expose api_key
        failure_str = str(failure)
        assert "sk-test" not in failure_str, f"last_failure must not contain api_key: {failure_str}"

        # Flat body: message extracted from top-level body["message"]
        assert failure.get("error_message") == "Upstream gateway error", (
            f"Expected error_message='Upstream gateway error', got {failure.get('error_message')!r}"
        )

        # Flat body: type extracted from top-level body["type"]
        assert failure.get("error_type") == "api_error", (
            f"Expected error_type='api_error', got {failure.get('error_type')!r}"
        )

    def test_last_failure_is_none_after_successful_complete(self, monkeypatch):
        """After a successful completion, last_failure should be None."""
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)

        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol",
            api_key="sk-test-dummy-key-12345",
            base_url="https://ai.codesonline.dev/v1",
        )

        with patch.object(backend, "_get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            # Simulate a successful response
            mock_response = mock_client.chat.completions.create.return_value
            mock_response.choices = [
                type(
                    "Choice",
                    (),
                    {
                        "message": type("Message", (), {"content": '{"ok": true}'})(),
                        "finish_reason": "stop",
                    },
                )()
            ]

            result = backend.complete("test prompt")

        assert result == '{"ok": true}'
        assert backend.last_failure is None, (
            f"Expected last_failure=None after success, got {backend.last_failure!r}"
        )

    def test_last_failure_handles_connection_error(self, monkeypatch):
        """Connection errors (no HTTP response) are also recorded."""
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)

        backend = OpenAICompatibleBackend(
            model="gpt-5.4",
            api_key="sk-test-dummy-key-67890",
            base_url="https://ai.codesonline.dev/v1",
        )

        with patch.object(backend, "_get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            from openai import APIConnectionError

            mock_client.chat.completions.create.side_effect = APIConnectionError(
                message="Connection refused",
                request=httpx.Request("POST", "https://ai.codesonline.dev/v1/chat/completions"),
            )

            result = backend.complete("test prompt")

        assert result == "[]"
        assert backend.last_failure is not None
        failure = backend.last_failure

        # Should not have http_status for connection errors
        assert failure.get("http_status") is None, (
            f"Connection errors should not have http_status, got {failure.get('http_status')!r}"
        )
        assert failure.get("error_type") == "APIConnectionError", (
            f"Expected error_type=APIConnectionError, got {failure.get('error_type')!r}"
        )
        assert failure.get("model") == "gpt-5.4"
        # Must not expose api_key
        assert "sk-test" not in str(failure)

    def test_last_failure_cleared_on_new_call(self, monkeypatch):
        """last_failure is cleared when a subsequent call succeeds."""
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)

        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol",
            api_key="sk-test-dummy-key-12345",
            base_url="https://ai.codesonline.dev/v1",
        )

        # First call: fail
        with patch.object(backend, "_get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            request = httpx.Request("POST", "https://ai.codesonline.dev/v1/chat/completions")
            response = httpx.Response(status_code=500, request=request)
            mock_client.chat.completions.create.side_effect = InternalServerError(
                message="Upstream gateway error",
                response=response,
                body={"error": {"message": "Upstream gateway error", "type": "api_error"}},
            )
            backend.complete("fail prompt")

        assert backend.last_failure is not None, "Should have failure after first call"

        # Second call: succeed
        with patch.object(backend, "_get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_response = mock_client.chat.completions.create.return_value
            mock_response.choices = [
                type(
                    "Choice",
                    (),
                    {
                        "message": type("Message", (), {"content": '{"ok": true}'})(),
                        "finish_reason": "stop",
                    },
                )()
            ]
            backend.complete("success prompt")

        assert backend.last_failure is None, (
            f"Expected last_failure=None after successful call, got {backend.last_failure!r}"
        )

    def test_complete_retries_retryable_5xx_then_succeeds(self, monkeypatch):
        """Transient 5xx errors are retried before returning a successful response."""
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)
        monkeypatch.setattr(
            OpenAICompatibleBackend,
            "_ERROR_RETRY_BASE_DELAY_SECONDS",
            0,
        )

        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol",
            api_key="sk-test-dummy-key-12345",
            base_url="https://ai.codesonline.dev/v1",
        )

        request = httpx.Request("POST", "https://ai.codesonline.dev/v1/chat/completions")
        response = httpx.Response(status_code=500, request=request)
        error = InternalServerError(
            message="Upstream gateway error",
            response=response,
            body={"error": {"message": "Upstream gateway error", "type": "api_error"}},
        )
        ok_response = type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type("Message", (), {"content": '{"ok": true}'})(),
                            "finish_reason": "stop",
                        },
                    )()
                ]
            },
        )()

        with patch.object(backend, "_get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.chat.completions.create.side_effect = [error, ok_response]

            result = backend.complete("test prompt")

        assert result == '{"ok": true}'
        assert mock_client.chat.completions.create.call_count == 2
        assert backend.last_failure is None

    def test_complete_records_final_failure_in_circuit_breaker(self, monkeypatch):
        """Final completion failure is recorded against the logical backend name."""
        import fin_analyse.claims.openai_backend as openai_backend

        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)
        monkeypatch.setattr(
            OpenAICompatibleBackend,
            "_ERROR_RETRY_BASE_DELAY_SECONDS",
            0,
        )

        class FakeBreaker:
            def __init__(self) -> None:
                self.failures: list[tuple[str, dict]] = []
                self.successes: list[str] = []

            def record_failure(self, backend_name: str, reason: dict) -> None:
                self.failures.append((backend_name, reason))

            def record_success(self, backend_name: str) -> None:
                self.successes.append(backend_name)

        breaker = FakeBreaker()
        monkeypatch.setattr(openai_backend, "get_backend_circuit_breaker", lambda: breaker)

        backend = OpenAICompatibleBackend(
            model="gpt-5.6-sol",
            api_key="sk-test-dummy-key-12345",
            base_url="https://ai.codesonline.dev/v1",
            backend_name="gpt5",
        )

        request = httpx.Request("POST", "https://ai.codesonline.dev/v1/chat/completions")
        response = httpx.Response(status_code=500, request=request)
        error = InternalServerError(
            message="Upstream gateway error",
            response=response,
            body={"error": {"message": "Upstream gateway error", "type": "api_error"}},
        )

        with patch.object(backend, "_get_client") as mock_get_client:
            mock_client = mock_get_client.return_value
            mock_client.chat.completions.create.side_effect = error

            result = backend.complete("test prompt")

        assert result == "[]"
        assert len(breaker.failures) == 1
        assert breaker.failures[0][0] == "gpt5"
        assert breaker.failures[0][1]["model"] == "gpt-5.6-sol"

    def test_complete_doubles_budget_when_reasoning_exhausts_length_with_empty_content(
        self, monkeypatch
    ):
        """推理预算耗尽（finish_reason=length + 空 content）走倍增恢复，不落哨兵。

        2026-08-30 空坍塌实证：推理模型把 max_tokens 全耗在 reasoning_content，
        content 为空——修复前 _response_text 抛 ValueError 短路倍增逻辑，
        重试耗尽返回 "[]"；修复后 4096→8192 重试拿到可见答案。
        """
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)
        calls: list[int] = []

        def create(*_args, **kwargs):
            max_tokens = kwargs["max_tokens"]
            calls.append(max_tokens)
            if max_tokens == 4096:
                return _choice_response("", "length")
            return _choice_response('{"units": [{"evidence": "原文"}]}', "stop")

        monkeypatch.setattr(
            OpenAICompatibleBackend,
            "_get_client",
            lambda self, _index=0: type(
                "Client",
                (),
                {
                    "chat": type(
                        "Chat",
                        (),
                        {"completions": type("Completions", (), {"create": create})()},
                    )()
                },
            )(),
        )
        backend = OpenAICompatibleBackend(model="gpt-5.6-sol", api_key="sk-test")

        result = backend.complete("prompt")

        assert result == '{"units": [{"evidence": "原文"}]}'
        assert backend.last_failure is None
        assert calls == [4096, 8192]

    def test_complete_caps_empty_length_reasoning_after_one_doubling(self, monkeypatch):
        """推理耗尽签名（length+空 content）只倍增一次即终态截断——
        2026-08-30 实证 glm53/deepseek 推理随预算线性增长，无界倍增只烧钱。"""
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)
        calls: list[int] = []

        def create(*_args, **kwargs):
            calls.append(kwargs["max_tokens"])
            return _choice_response("", "length")

        monkeypatch.setattr(
            OpenAICompatibleBackend,
            "_get_client",
            lambda self, _index=0: type(
                "Client",
                (),
                {
                    "chat": type(
                        "Chat",
                        (),
                        {"completions": type("Completions", (), {"create": create})()},
                    )()
                },
            )(),
        )
        backend = OpenAICompatibleBackend(model="gpt-5.6-sol", api_key="sk-test")

        result = backend.complete("prompt")

        assert result == "[]"
        assert calls == [4096, 8192]
        assert backend.last_failure is not None
        assert backend.last_failure["error_type"] == "LLMResponseTruncated"

    def test_complete_full_doubles_when_answer_content_is_truncated(self, monkeypatch):
        """答案非空但 length 截断 → 保留全档倍增（4096→8192→16384）直至出活。"""
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)
        calls: list[int] = []

        def create(*_args, **kwargs):
            max_tokens = kwargs["max_tokens"]
            calls.append(max_tokens)
            if max_tokens < 16384:
                return _choice_response('{"units": [{"evidence": "原文', "length")
            return _choice_response(
                '{"units": [{"evidence": "原文完整句"}]}', "stop"
            )

        monkeypatch.setattr(
            OpenAICompatibleBackend,
            "_get_client",
            lambda self, _index=0: type(
                "Client",
                (),
                {
                    "chat": type(
                        "Chat",
                        (),
                        {"completions": type("Completions", (), {"create": create})()},
                    )()
                },
            )(),
        )
        backend = OpenAICompatibleBackend(model="gpt-5.6-sol", api_key="sk-test")

        result = backend.complete("prompt")

        assert result == '{"units": [{"evidence": "原文完整句"}]}'
        assert calls == [4096, 8192, 16384]
        assert backend.last_failure is None

    def test_endpoint_fallback_on_401_uses_next_endpoint_key(self, monkeypatch):
        """主端点 401 → 换下一端点（auth.json 降级键）出活，不落哨兵。"""
        monkeypatch.setattr(OpenAICompatibleBackend, "complete", _ORIGINAL_COMPLETE)
        from openai import BadRequestError

        request = httpx.Request("POST", "https://x/v1/chat/completions")
        response = httpx.Response(status_code=401, request=request)
        error = BadRequestError(
            message="invalid api key",
            response=response,
            body={
                "error": {
                    "message": "invalid api key",
                    "type": "invalid_request_error",
                }
            },
        )

        class _PrimaryClient:
            chat = type(
                "Chat",
                (),
                {
                    "completions": type(
                        "Completions",
                        (),
                        {
                            "create": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                                error
                            )
                        },
                    )()
                },
            )()

        class _FallbackClient:
            chat = type(
                "Chat",
                (),
                {
                    "completions": type(
                        "Completions",
                        (),
                        {"create": lambda *_args, **_kwargs: "fallback answer"},
                    )()
                },
            )()

        backend = OpenAICompatibleBackend(
            model="ds-flash",
            api_key="sk-primary",
            endpoints=[
                {"name": "primary", "api_key": "sk-primary", "base_url": "https://x"},
                {"name": "fallback", "api_key": "sk-fallback", "base_url": "https://x"},
            ],
        )
        seen: list[int] = []

        def _client(index: int = 0):
            seen.append(index)
            return _PrimaryClient() if index == 0 else _FallbackClient()

        monkeypatch.setattr(backend, "_get_client", _client)

        result = backend.complete("prompt")

        assert result == "fallback answer"
        assert seen == [0, 1]
        assert backend.last_failure is None
