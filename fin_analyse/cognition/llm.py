"""Provider-neutral LLM helpers for cognition workflows."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fin_analyse.common.execution_control import ExecutionFence

logger = logging.getLogger(__name__)

# Regex to extract the first JSON object (balanced { }) or array ([ ]) from text.
_JSON_RE = re.compile(
    r"""
    ( \{ (?: [^{}] | \{(?: [^{}] | \{[^{}]*\})*\} )*  \}  # match balanced { ... }
    | \[ (?: [^\[\]] | \[(?: [^\[\]] | \[[^\[\]]*\])*\])* \]  # match balanced [ ... ]
    )
    """,
    re.VERBOSE,
)

# Fenced code block: ```json ... ```
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Extract a JSON object or array from text, preferring fenced code blocks."""
    m = _FENCED_JSON_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _JSON_RE.search(text)
    if m:
        return m.group(0)
    return text.strip()


@dataclass(frozen=True)
class LLMResult:
    """Outcome of an LLM JSON completion call."""

    ok: bool
    data: Any | None
    raw: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CognitionCompletionControl:
    """Optional deadline/heartbeat control for background cognition calls."""

    fence: ExecutionFence
    checkpoint: Callable[[], None]
    wire_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.wire_timeout_seconds <= 0:
            raise ValueError("wire_timeout_seconds must be positive")

    def checkpoint_or_raise(self) -> None:
        self.checkpoint()
        if not self.fence.is_open(at=datetime.now(UTC)):
            raise TimeoutError("cognition completion deadline exhausted")

    def remaining_seconds(self) -> float:
        return self.fence.remaining_seconds(at=datetime.now(UTC))


class CognitionLLM:
    """Small wrapper around existing project LLM backends.

    The backend only needs a ``complete(prompt: str) -> str`` method.
    This keeps cognition provider-neutral and testable with fake backends.
    """

    def __init__(self, backend: object | None = None) -> None:
        self.backend = backend

    @classmethod
    def from_config(cls, preferred: tuple[str, ...] | None = None) -> CognitionLLM:
        from fin_analyse.claims.config_loader import create_backends_from_config

        backends = create_backends_from_config()
        order = preferred or ("glm53", "deepseek", "qwen", "claude")
        for name in order:
            backend = backends.get(name)
            if backend is not None:
                return cls(backend=backend)
        for backend in backends.values():
            return cls(backend=backend)
        return cls(backend=None)

    @classmethod
    def from_config_multi(cls, count: int = 3) -> list[CognitionLLM]:
        """Return up to *count* distinct LLM instances from available backends.

        Order: glm53, deepseek, qwen, claude.
        Used by ConsensusReasoningExtractor for dual-LLM extraction + aggregation.
        """
        from fin_analyse.claims.config_loader import create_backends_from_config

        backends = create_backends_from_config()
        instances: list[CognitionLLM] = []
        for name in ("glm53", "deepseek", "qwen", "claude"):
            if len(instances) >= count:
                break
            backend = backends.get(name)
            if backend is not None:
                instances.append(cls(backend=backend))
        return instances

    def complete_text(
        self,
        prompt: str,
        *,
        control: CognitionCompletionControl | None = None,
    ) -> str:
        if self.backend is None:
            return ""
        if control is not None:
            complete = getattr(self.backend, "complete_bounded", None)
            if complete is None:
                logger.warning("Cognition LLM backend has no bounded completion method")
                return ""
            try:
                control.checkpoint_or_raise()
                return str(
                    complete(
                        prompt,
                        total_timeout_seconds=control.remaining_seconds(),
                        wire_timeout_seconds=control.wire_timeout_seconds,
                        before_attempt=control.checkpoint_or_raise,
                    )
                )
            except Exception as exc:
                logger.warning("Cognition LLM bounded text completion failed: %s", exc)
                return ""

        complete = getattr(self.backend, "complete", None)
        if complete is None:
            return ""
        try:
            return str(complete(prompt))
        except Exception as exc:
            logger.warning("Cognition LLM text completion failed: %s", exc)
            return ""

    def complete_json(
        self,
        prompt: str,
        *,
        expected_type: str,
        control: CognitionCompletionControl | None = None,
    ) -> LLMResult:
        if self.backend is None:
            return LLMResult(False, None, "", "LLM backend unavailable")

        raw = self.complete_text(prompt, control=control)
        if not raw:
            logger.warning(
                "Cognition LLM completion returned empty response for %s",
                expected_type,
            )
            return LLMResult(False, None, raw, "LLM returned empty response")

        try:
            return LLMResult(True, json.loads(_extract_json(raw)), raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Cognition LLM JSON parse failed for %s: %s",
                expected_type,
                exc,
            )
            return LLMResult(False, None, raw, f"JSON parse failed for {expected_type}: {exc}")
