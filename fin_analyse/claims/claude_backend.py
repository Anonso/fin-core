"""Claude API backend for LLM claim extraction."""

import os
from typing import Any, cast


class ClaudeBackend:
    """Uses the Anthropic Claude API for claim extraction."""

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client: Any = None

    def _get_client(self):
        if self._client is None and self.api_key:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def complete(self, prompt: str) -> str:
        client = self._get_client()
        if client is None:
            return "[]"

        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return cast(str, message.content[0].text)
        except Exception as e:
            return f"[] // ERROR: {e}"

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_executor=None,
        max_turns: int = 10,
    ) -> str:
        """Multi-turn conversation with tool calling (not supported by Claude backend)."""
        raise NotImplementedError("Claude backend does not support tool calling")
