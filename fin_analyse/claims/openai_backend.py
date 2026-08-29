"""OpenAI-compatible backend for claim extraction.

Supports: OpenAI, DeepSeek, Qwen, Groq, local models via vLLM/Ollama,
or any OpenAI-compatible API endpoint.
"""

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from .backend_health import get_backend_circuit_breaker
from .llm_extractor import LLMBackend

logger = logging.getLogger(__name__)


class OpenAICompatibleBackend:
    """OpenAI-compatible LLM backend. Works with any provider that exposes
    a /v1/chat/completions endpoint."""

    _MAX_RETRY_TOKENS = 16384
    _MAX_ERROR_RETRIES = 2
    _ERROR_RETRY_BASE_DELAY_SECONDS = 0.1

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int = 4096,
        timeout: float | None = None,
        backend_name: str | None = None,
        endpoints: list[dict[str, Any]] | None = None,
    ):
        self.model = model
        self.backend_name = backend_name or model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._client: Any = None
        self.last_failure: dict[str, Any] | None = None
        self.records_backend_health = True
        configured = endpoints if isinstance(endpoints, list) else []
        self.endpoints = tuple(
            {
                "name": str(item.get("name") or f"endpoint-{index}"),
                "api_key": item.get("api_key") or self.api_key,
                "base_url": item.get("base_url") or self.base_url,
                "model": item.get("model"),
                "reasoning_effort": item.get("reasoning_effort"),
            }
            for index, item in enumerate(configured)
            if isinstance(item, dict)
        )
        self._clients: dict[int, Any] = {}

    def _get_client(self, endpoint_index: int = 0):
        if endpoint_index == 0 and not self.endpoints and self._client is not None:
            return self._client
        endpoint = (
            self.endpoints[endpoint_index]
            if endpoint_index < len(self.endpoints)
            else {
                "api_key": self.api_key,
                "base_url": self.base_url,
            }
        )
        client = self._clients.get(endpoint_index)
        if client is None and endpoint.get("api_key"):
            from openai import OpenAI

            kwargs: dict[str, Any] = {"api_key": endpoint["api_key"]}
            if endpoint.get("base_url"):
                kwargs["base_url"] = endpoint["base_url"]
            if self.timeout is not None:
                kwargs["timeout"] = self.timeout
            client = OpenAI(**kwargs)
            self._clients[endpoint_index] = client
            if endpoint_index == 0 and not self.endpoints:
                self._client = client
        return client

    @staticmethod
    def _sanitize_failure(exc: Exception) -> dict[str, Any]:
        """Extract sanitized failure metadata from an exception.

        Never includes api_key, raw HTTP headers, or secret material.
        """
        info: dict[str, Any] = {
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "http_status": None,
        }
        # Extract HTTP status from OpenAI API status errors
        try:
            status = getattr(exc, "status_code", None)
            if status is not None:
                info["http_status"] = int(status)
        except (TypeError, ValueError):
            pass
        # Extract error details from body if available.
        # Supports two body shapes seen in the wild:
        #   1. Nested:   {"error": {"message": "...", "type": "..."}}
        #   2. Flat:     {"message": "...", "type": "..."}
        try:
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                err = body.get("error", {})
                if isinstance(err, dict):
                    if err.get("message"):
                        info["error_message"] = str(err["message"])
                    if err.get("type"):
                        info["error_type"] = str(err["type"])
                # Flat body: only used when no nested "error" dict is present
                if not isinstance(body.get("error"), dict):
                    if body.get("message"):
                        info["error_message"] = str(body["message"])
                    if body.get("type"):
                        info["error_type"] = str(body["type"])
        except Exception:
            pass
        return info

    @staticmethod
    def _looks_truncated(text: str) -> bool:
        """Heuristic: response looks like JSON/code and ended before close."""
        if not text:
            return False
        stripped = text.rstrip()
        # Only inspect structured/marked content; plain text is not truncated.
        if not any(marker in stripped for marker in ("{", "[", "```")):
            return False
        return not stripped.endswith(("}", "]", "```"))

    @staticmethod
    def _is_retryable_failure(failure: dict[str, Any]) -> bool:
        """Return True for transient provider failures worth one more attempt."""
        status = failure.get("http_status")
        if isinstance(status, int):
            return status in {408, 409, 429} or status >= 500
        error_type = str(failure.get("error_type") or "")
        return error_type in {
            "APIConnectionError",
            "APITimeoutError",
            "APIStatusError",
            "TimeoutError",
            "ConnectError",
            "ReadTimeout",
        }

    def _failure_from_exception(
        self, exc: Exception, *, base_url: str | None = None
    ) -> dict[str, Any]:
        failure = self._sanitize_failure(exc)
        failure["backend_name"] = self.model
        failure["model"] = self.model
        # Safe base_url: host only (no path with potential secrets)
        safe_base_url = base_url or self.base_url
        if safe_base_url:
            try:
                from urllib.parse import urlparse

                parsed = urlparse(safe_base_url)
                failure["base_url"] = f"{parsed.scheme}://{parsed.netloc}"
            except Exception:
                failure["base_url"] = safe_base_url
        return failure

    @staticmethod
    def _response_text(response: Any) -> str:
        """Normalize either a raw provider string or OpenAI response shape."""
        if isinstance(response, str):
            text = response.strip()
            if text:
                return text
            raise ValueError("empty LLM response")
        choices = getattr(response, "choices", None)
        if not choices:
            raise ValueError("invalid LLM response shape")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty or invalid LLM response shape")
        return content

    def complete(self, prompt: str) -> str:
        self.last_failure = None
        endpoint_specs = self.endpoints or ({"base_url": self.base_url},)
        failures: list[dict[str, Any]] = []
        for endpoint_index, endpoint in enumerate(endpoint_specs):
            try:
                client = self._get_client(endpoint_index)
            except Exception as exc:
                failures.append(
                    self._failure_from_exception(
                        exc, base_url=str(endpoint.get("base_url") or self.base_url or "")
                    )
                )
                continue
            if client is None:
                failures.append(
                    {
                        "backend_name": self.backend_name,
                        "model": self.model,
                        "error_type": "LLMClientUnavailable",
                        "error_message": "OpenAI-compatible client unavailable",
                        "http_status": None,
                    }
                )
                continue
            max_tokens = self.max_tokens
            last_truncated: dict[str, Any] | None = None
            for attempt in range(self._MAX_ERROR_RETRIES + 1):
                try:
                    endpoint_model = str(endpoint.get("model") or self.model)
                    endpoint_reasoning_effort = endpoint.get(
                        "reasoning_effort", self.reasoning_effort
                    )
                    kwargs: dict[str, Any] = {
                        "model": endpoint_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "temperature": 0.3,
                    }
                    if endpoint_reasoning_effort:
                        kwargs["reasoning_effort"] = endpoint_reasoning_effort
                    response = client.chat.completions.create(**kwargs)
                    content = self._response_text(response)
                    finish_reason = (
                        getattr(response.choices[0], "finish_reason", None)
                        if not isinstance(response, str)
                        else None
                    )
                    if finish_reason != "length" and not self._looks_truncated(content):
                        self.last_failure = None
                        get_backend_circuit_breaker().record_success(self.backend_name)
                        return content
                    last_truncated = {
                        "backend_name": self.backend_name,
                        "model": self.model,
                        "error_type": "LLMResponseTruncated",
                        "error_message": "response truncated at max_tokens",
                        "http_status": None,
                        "base_url": str(endpoint.get("base_url") or self.base_url or ""),
                    }
                    if max_tokens >= self._MAX_RETRY_TOKENS:
                        failures.append(last_truncated)
                        last_truncated = None
                        break
                    max_tokens = min(max_tokens * 2, self._MAX_RETRY_TOKENS)
                except Exception as exc:
                    failure = self._failure_from_exception(
                        exc,
                        base_url=str(endpoint.get("base_url") or self.base_url or ""),
                    )
                    failure["retryable"] = self._is_retryable_failure(failure)
                    failure["attempt"] = attempt + 1
                    failures.append(failure)
                    if failure["retryable"] and attempt < self._MAX_ERROR_RETRIES:
                        time.sleep(self._ERROR_RETRY_BASE_DELAY_SECONDS * (2**attempt))
                        continue
                    break
            if last_truncated is not None:
                failures.append(last_truncated)
        final_failure = (
            failures[-1]
            if failures
            else {
                "backend_name": self.backend_name,
                "model": self.model,
                "error_type": "LLMClientUnavailable",
                "error_message": "OpenAI-compatible client unavailable",
                "http_status": None,
            }
        )
        self.last_failure = final_failure
        get_backend_circuit_breaker().record_failure(self.backend_name, final_failure)
        return "[]"

    def complete_bounded(
        self,
        prompt: str,
        *,
        total_timeout_seconds: float,
        wire_timeout_seconds: float,
        before_attempt: Callable[[], None],
    ) -> str:
        """Complete within one monotonic budget without SDK-owned retries."""
        if total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be positive")
        if wire_timeout_seconds <= 0:
            raise ValueError("wire_timeout_seconds must be positive")

        self.last_failure = None
        deadline = time.monotonic() + total_timeout_seconds
        endpoint_specs = self.endpoints or ({"base_url": self.base_url},)
        failures: list[dict[str, Any]] = []
        budget_exhausted = False

        for endpoint_index, endpoint in enumerate(endpoint_specs):
            try:
                client = self._get_client(endpoint_index)
            except Exception as exc:
                failures.append(
                    self._failure_from_exception(
                        exc,
                        base_url=str(endpoint.get("base_url") or self.base_url or ""),
                    )
                )
                continue
            if client is None:
                failures.append(
                    {
                        "backend_name": self.backend_name,
                        "model": self.model,
                        "error_type": "LLMClientUnavailable",
                        "error_message": "OpenAI-compatible client unavailable",
                        "http_status": None,
                    }
                )
                continue

            max_tokens = self.max_tokens
            last_truncated: dict[str, Any] | None = None
            for attempt in range(self._MAX_ERROR_RETRIES + 1):
                before_attempt()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    budget_exhausted = True
                    break
                timeout = min(float(wire_timeout_seconds), remaining, 60.0)
                if self.timeout is not None:
                    timeout = min(timeout, float(self.timeout))
                try:
                    bounded_client = client.with_options(timeout=timeout, max_retries=0)
                    endpoint_model = str(endpoint.get("model") or self.model)
                    endpoint_reasoning_effort = endpoint.get(
                        "reasoning_effort", self.reasoning_effort
                    )
                    kwargs: dict[str, Any] = {
                        "model": endpoint_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "temperature": 0.3,
                    }
                    if endpoint_reasoning_effort:
                        kwargs["reasoning_effort"] = endpoint_reasoning_effort
                    response = bounded_client.chat.completions.create(**kwargs)
                    content = self._response_text(response)
                    finish_reason = (
                        getattr(response.choices[0], "finish_reason", None)
                        if not isinstance(response, str)
                        else None
                    )
                    if finish_reason != "length" and not self._looks_truncated(content):
                        self.last_failure = None
                        get_backend_circuit_breaker().record_success(self.backend_name)
                        return content
                    last_truncated = {
                        "backend_name": self.backend_name,
                        "model": self.model,
                        "error_type": "LLMResponseTruncated",
                        "error_message": "response truncated at max_tokens",
                        "http_status": None,
                        "base_url": str(endpoint.get("base_url") or self.base_url or ""),
                    }
                    if max_tokens >= self._MAX_RETRY_TOKENS:
                        failures.append(last_truncated)
                        last_truncated = None
                        break
                    max_tokens = min(max_tokens * 2, self._MAX_RETRY_TOKENS)
                except Exception as exc:
                    failure = self._failure_from_exception(
                        exc,
                        base_url=str(endpoint.get("base_url") or self.base_url or ""),
                    )
                    failure["retryable"] = self._is_retryable_failure(failure)
                    failure["attempt"] = attempt + 1
                    failures.append(failure)
                    if failure["retryable"] and attempt < self._MAX_ERROR_RETRIES:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            budget_exhausted = True
                            break
                        time.sleep(
                            min(
                                self._ERROR_RETRY_BASE_DELAY_SECONDS * (2**attempt),
                                remaining,
                            )
                        )
                        continue
                    break
            if budget_exhausted:
                break
            if last_truncated is not None:
                failures.append(last_truncated)

        if budget_exhausted:
            final_failure: dict[str, Any] = {
                "backend_name": self.backend_name,
                "model": self.model,
                "error_type": "LLMCompletionDeadlineExceeded",
                "error_message": "bounded completion exhausted its total timeout",
                "http_status": None,
            }
        else:
            final_failure = (
                failures[-1]
                if failures
                else {
                    "backend_name": self.backend_name,
                    "model": self.model,
                    "error_type": "LLMClientUnavailable",
                    "error_message": "OpenAI-compatible client unavailable",
                    "http_status": None,
                }
            )
        self.last_failure = final_failure
        get_backend_circuit_breaker().record_failure(self.backend_name, final_failure)
        return "[]"

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_executor=None,
        max_turns: int = 10,
    ) -> str:
        """Multi-turn conversation with tool calling.

        Parameters
        ----------
        messages : list[dict]
            Conversation history in OpenAI format
            [{"role": "system"|"user"|"assistant"|"tool", "content": ...}].
        tools : list[dict]
            Tool definitions in OpenAI function-calling format.
        tool_executor : callable(name, arguments) -> str | None
            Function to execute tool calls. Receives tool name and parsed
            arguments dict, returns result string. If None, no tools can
            be called (useful for testing).
        max_turns : int
            Maximum number of LLM calls before forced return.

        Returns
        -------
        str
            Final text response from the LLM, or empty string on error.
        """
        self.last_failure = None
        endpoint_specs = self.endpoints or ({"base_url": self.base_url},)
        failures: list[dict[str, Any]] = []
        msgs = list(messages)
        start_index = 0
        for _turn in range(max_turns):
            tool_completed = False
            for endpoint_index in range(start_index, len(endpoint_specs)):
                endpoint = endpoint_specs[endpoint_index]
                try:
                    client = self._get_client(endpoint_index)
                    if client is None:
                        raise RuntimeError("OpenAI-compatible client unavailable")
                    endpoint_model = str(endpoint.get("model") or self.model)
                    endpoint_reasoning_effort = endpoint.get(
                        "reasoning_effort", self.reasoning_effort
                    )
                    kwargs: dict[str, Any] = {
                        "model": endpoint_model,
                        "messages": msgs,
                        "tools": tools,
                        "tool_choice": "auto",
                        "max_tokens": self.max_tokens,
                        "temperature": 0.3,
                    }
                    if endpoint_reasoning_effort:
                        kwargs["reasoning_effort"] = endpoint_reasoning_effort
                    response = client.chat.completions.create(**kwargs)
                    if isinstance(response, str):
                        if response.strip():
                            get_backend_circuit_breaker().record_success(self.backend_name)
                            return response.strip()
                        raise ValueError("empty LLM response")
                    choices = getattr(response, "choices", None)
                    if not choices:
                        raise ValueError("invalid LLM response shape")
                    msg = choices[0].message
                    if getattr(msg, "tool_calls", None):
                        tc_list = [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in msg.tool_calls
                        ]
                        msgs.append({"role": "assistant", "content": None, "tool_calls": tc_list})
                        for tc in msg.tool_calls:
                            try:
                                args = json.loads(tc.function.arguments)
                            except json.JSONDecodeError:
                                args = {}
                            result = ""
                            if tool_executor is not None:
                                try:
                                    result = tool_executor(tc.function.name, args)
                                except Exception as exc:
                                    result = f"Error: {exc}"
                            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                        start_index = endpoint_index
                        tool_completed = True
                        break
                    answer = (
                        getattr(msg, "content", None)
                        or getattr(msg, "reasoning_content", None)
                        or ""
                    )
                    if isinstance(answer, str) and answer.strip():
                        get_backend_circuit_breaker().record_success(self.backend_name)
                        return answer.strip()
                    raise ValueError("empty LLM response")
                except Exception as exc:
                    failures.append(
                        self._failure_from_exception(
                            exc, base_url=str(endpoint.get("base_url") or self.base_url or "")
                        )
                    )
                    start_index = endpoint_index + 1
                    logger.warning("chat_with_tools endpoint failure: %s", exc)
            if not tool_completed:
                break
        failure = (
            failures[-1]
            if failures
            else {
                "backend_name": self.backend_name,
                "model": self.model,
                "error_type": "LLMClientUnavailable",
                "error_message": "all configured endpoints failed",
                "http_status": None,
            }
        )
        self.last_failure = failure
        get_backend_circuit_breaker().record_failure(self.backend_name, failure)
        return ""


def create_backends_from_env() -> dict[str, LLMBackend]:
    """Discover configured LLM backends from environment variables.

    Reads:
    - ANTHROPIC_API_KEY → claude backend
    - OPENAI_API_KEY → gpt backend
    - DEEPSEEK_API_KEY → deepseek backend
    - QWEN_API_KEY → qwen backend
    - CUSTOM_LLM_API_KEY + CUSTOM_LLM_BASE_URL + CUSTOM_LLM_MODEL → custom backend
    """
    from .claude_backend import ClaudeBackend

    backends: dict[str, LLMBackend] = {}

    if os.environ.get("ANTHROPIC_API_KEY"):
        backends["claude"] = ClaudeBackend(model="claude-sonnet-4-6")

    if os.environ.get("OPENAI_API_KEY"):
        backends["gpt"] = OpenAICompatibleBackend(model="gpt-4o", backend_name="gpt")

    # Kimi Coding Plan key (sk-kimi-iN...) only works via Hermes agent,
    # not direct OpenAI API. Use HermesBackend instead.
    from .hermes_backend import create_hermes_backend

    try:  # noqa: SIM105
        backends["kimi"] = create_hermes_backend(
            model=os.environ.get("KIMI_MODEL", "kimi-k2.6"),
            profile="fin",
        )
    except RuntimeError:
        pass  # Hermes not installed

    if os.environ.get("DEEPSEEK_API_KEY"):
        backends["deepseek"] = OpenAICompatibleBackend(
            model="deepseek-chat",
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com/v1",
            backend_name="deepseek",
        )

    if os.environ.get("QWEN_API_KEY"):
        backends["qwen"] = OpenAICompatibleBackend(
            model="qwen-max",
            api_key=os.environ["QWEN_API_KEY"],
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            backend_name="qwen",
        )

    custom_key = os.environ.get("CUSTOM_LLM_API_KEY")
    if custom_key:
        backends["custom"] = OpenAICompatibleBackend(
            model=os.environ.get("CUSTOM_LLM_MODEL", "llama-3"),
            api_key=custom_key,
            base_url=os.environ.get("CUSTOM_LLM_BASE_URL"),
            backend_name="custom",
        )

    return get_backend_circuit_breaker().filter_available(backends)
