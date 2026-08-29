"""Runtime health circuit breaker for LLM backends.

This module keeps a small in-process health state so repeated provider
failures do not cause every request to keep hitting the same unavailable LLM.
The state is intentionally volatile: restarting the FIN worker clears it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 600.0


@dataclass
class BackendHealthState:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_failure_reason: str = ""


class BackendCircuitBreaker:
    """Track consecutive backend failures and temporary cooldown windows."""

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self._clock = clock
        self._states: dict[str, BackendHealthState] = {}

    def can_try(self, backend_name: str) -> bool:
        state = self._states.get(backend_name)
        if state is None:
            return True
        now = self._clock()
        if state.cooldown_until <= 0:
            return True
        if now >= state.cooldown_until:
            self._states.pop(backend_name, None)
            return True
        return False

    def record_success(self, backend_name: str) -> None:
        self._states.pop(backend_name, None)

    def record_failure(self, backend_name: str, reason: Any = "") -> BackendHealthState:
        state = self._states.setdefault(backend_name, BackendHealthState())
        state.consecutive_failures += 1
        state.last_failure_reason = _compact_reason(reason)
        if state.consecutive_failures >= self.failure_threshold:
            state.cooldown_until = self._clock() + self.cooldown_seconds
        return state

    def health_status(self, backend_name: str) -> str:
        state = self._states.get(backend_name)
        if state is None:
            return "available"
        if self.can_try(backend_name):
            return "available"
        remaining = max(0.0, state.cooldown_until - self._clock())
        return (
            f"cooldown:{remaining:.0f}s:"
            f"failures={state.consecutive_failures}:"
            f"{state.last_failure_reason}"
        )

    def filter_available(self, backends: dict[str, Any]) -> dict[str, Any]:
        """Drop backends currently in cooldown."""
        return {name: backend for name, backend in backends.items() if self.can_try(name)}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = self._clock()
        return {
            name: {
                "consecutive_failures": state.consecutive_failures,
                "cooldown_remaining_seconds": max(0.0, state.cooldown_until - now),
                "last_failure_reason": state.last_failure_reason,
            }
            for name, state in self._states.items()
        }

    def clear(self) -> None:
        self._states.clear()


def _compact_reason(reason: Any) -> str:
    if isinstance(reason, dict):
        error_type = str(reason.get("error_type") or reason.get("reason") or "error")
        status = reason.get("http_status")
        if status is not None:
            return f"{error_type}:http_{status}"
        return error_type
    text = str(reason or "error").strip()
    return text[:120] if text else "error"


_GLOBAL_BREAKER: BackendCircuitBreaker | None = None


def get_backend_circuit_breaker() -> BackendCircuitBreaker:
    global _GLOBAL_BREAKER
    if _GLOBAL_BREAKER is None:
        threshold = _int_env("LLM_BACKEND_COOLDOWN_FAILURE_THRESHOLD", DEFAULT_FAILURE_THRESHOLD)
        cooldown = _float_env("LLM_BACKEND_COOLDOWN_SECONDS", DEFAULT_COOLDOWN_SECONDS)
        _GLOBAL_BREAKER = BackendCircuitBreaker(
            failure_threshold=threshold,
            cooldown_seconds=cooldown,
        )
    return _GLOBAL_BREAKER


def reset_backend_circuit_breaker_for_tests() -> None:
    global _GLOBAL_BREAKER
    _GLOBAL_BREAKER = None


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
