"""Provider registry with fallback chain, retry, and circuit breaker."""

from __future__ import annotations

import logging
import time
from typing import Any

from .providers.base import BaseMarketProvider

logger = logging.getLogger(__name__)


class AllProvidersFailedError(Exception):
    """Raised when all providers fail for a given method call."""

    def __init__(self, method: str, ticker: str = ""):
        self.method = method
        self.ticker = ticker
        super().__init__(f"All providers failed for {method}('{ticker}')")


class ProviderRegistry:
    """Manages multiple market data providers with priority-based fallback.

    Features:
    - Per-provider retry with exponential backoff
    - Circuit breaker: skip providers that fail 3 consecutive times for 5 min
    - health() summary for monitoring
    """

    def __init__(self, providers: list[BaseMarketProvider]) -> None:
        self._providers = sorted(providers, key=lambda p: p.priority)
        self._circuit_state: dict[str, CircuitState] = {}

    @property
    def providers(self) -> tuple[BaseMarketProvider, ...]:
        """Providers in priority order for read-only consumers."""
        return tuple(self._providers)

    def execute(self, method: str, *args: Any, max_retries: int = 3, **kwargs: Any) -> Any:
        """Call *method* on providers in priority order with retry + fallback.

        ``NotImplementedError`` is treated as an immediate skip (no retry,
        no circuit breaker increment) so that providers which genuinely
        don't support a method are bypassed instantly.

        Raises AllProvidersFailedError if no provider succeeds.
        """
        for provider in self._providers:
            if self._is_open(provider.name):
                ticker = args[0] if args else ""
                logger.info(
                    "[%s] circuit open, skipping %s('%s')",
                    provider.name,
                    method,
                    ticker,
                )
                continue
            fn = getattr(provider, method)
            last_exc: Exception | None = None
            for attempt in range(max_retries):
                try:
                    result = fn(*args, **kwargs)
                    self._record_success(provider.name)
                    return result
                except NotImplementedError:
                    # Provider doesn't support this method — skip immediately,
                    # don't retry, don't count as failure.
                    logger.debug(
                        "[%s] %s not supported, falling through",
                        provider.name,
                        method,
                    )
                    last_exc = NotImplementedError()
                    break  # break out of retry loop, continue to next provider
                except Exception as exc:
                    last_exc = exc
                    delay = 2**attempt
                    if attempt < max_retries - 1:
                        logger.debug(
                            "[%s] %s attempt %d failed, retry in %ds",
                            provider.name,
                            method,
                            attempt + 1,
                            delay,
                        )
                        time.sleep(delay)
            if isinstance(last_exc, NotImplementedError):
                # Not an error — provider simply doesn't implement this method.
                logger.debug(
                    "[%s] %s not implemented, skipping",
                    provider.name,
                    method,
                )
            else:
                self._record_failure(provider.name, str(last_exc or "unknown"))
                ticker = args[0] if args else ""
                logger.warning(
                    "[%s] %s('%s') failed after %d retries: %s",
                    provider.name,
                    method,
                    ticker,
                    max_retries,
                    last_exc,
                )

        ticker = args[0] if args else ""
        raise AllProvidersFailedError(method, str(ticker))

    def health(self) -> dict[str, Any]:
        """Return health summary for all providers."""
        providers_health: dict[str, Any] = {}
        for p in self._providers:
            cs = self._circuit_state.get(p.name)
            providers_health[p.name] = {
                "priority": p.priority,
                "circuit_open": cs.open if cs else False,
                "consecutive_failures": cs.failures if cs else 0,
                "last_failure": cs.last_failure_at if cs else None,
                "last_success": cs.last_success_at if cs else None,
            }
        return {
            "total_providers": len(self._providers),
            "providers": providers_health,
        }

    def _is_open(self, name: str) -> bool:
        cs = self._circuit_state.get(name)
        if cs is None or not cs.open:
            return False
        if time.time() - cs.opened_at > cs.cooldown_seconds:
            cs.open = False
            cs.failures = 0
            logger.info("[%s] circuit closed (cooldown elapsed)", name)
            return False
        return True

    def _record_failure(self, name: str, error: str) -> None:
        cs = self._circuit_state.setdefault(name, CircuitState())
        cs.failures += 1
        cs.last_failure_at = time.time()
        cs.last_error = error
        if cs.failures >= 3 and not cs.open:
            cs.open = True
            cs.opened_at = time.time()
            logger.warning(
                "[%s] circuit OPEN (%d consecutive failures, cooldown %ds): %s",
                name,
                cs.failures,
                cs.cooldown_seconds,
                error,
            )

    def _record_success(self, name: str) -> None:
        cs = self._circuit_state.setdefault(name, CircuitState())
        cs.failures = 0
        cs.open = False
        cs.last_success_at = time.time()


class CircuitState:
    """Per-provider circuit breaker state."""

    def __init__(self) -> None:
        self.failures: int = 0
        self.open: bool = False
        self.opened_at: float = 0.0
        self.last_failure_at: float = 0.0
        self.last_success_at: float = 0.0
        self.last_error: str = ""
        self.cooldown_seconds: float = 300.0  # 5 minutes
