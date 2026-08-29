"""Tests for ProviderRegistry with fallback."""

from contextlib import suppress

import pytest

from fin_analyse.market.providers.base import OHLCV, BaseMarketProvider, QuoteResult
from fin_analyse.market.registry import AllProvidersFailedError, ProviderRegistry


class _MockOK(BaseMarketProvider):
    """A provider that always succeeds."""

    def __init__(self, name="ok_provider", priority=1):
        self._name = name
        self._priority = priority

    @property
    def name(self):
        return self._name

    @property
    def priority(self):
        return self._priority

    def search_stock(self, n):
        return {"name": n, "ticker": "000001", "market": "A股"}

    def get_quote(self, t):
        return QuoteResult(ticker=t, name="test", price=10.0, change_pct=1.0)

    def get_history(self, t, days=120):
        return [
            OHLCV(date="2026-06-20", open=10.0, high=11.0, low=9.5, close=10.5, volume=1_000_000)
        ]

    def get_financials(self, t):
        return {"ticker": t, "latest": {"eps": 2.0, "roe": 15.0}}

    def get_capital_flow(self, t, days=60):
        return []


class _MockFail(BaseMarketProvider):
    """A provider that always fails."""

    @property
    def name(self):
        return "fail_provider"

    @property
    def priority(self):
        return 2

    def search_stock(self, n):
        raise RuntimeError("fail")

    def get_quote(self, t):
        raise RuntimeError("fail")

    def get_history(self, t, days=120):
        raise RuntimeError("fail")

    def get_financials(self, t):
        raise RuntimeError("fail")

    def get_capital_flow(self, t, days=60):
        raise RuntimeError("fail")


class _MockPartial(BaseMarketProvider):
    """A provider that only supports get_quote, raises NotImplementedError for others."""

    def __init__(self, name="partial", priority=1):
        self._name = name
        self._priority = priority

    @property
    def name(self):
        return self._name

    @property
    def priority(self):
        return self._priority

    def search_stock(self, n):
        raise NotImplementedError("search not supported")

    def get_quote(self, t):
        return QuoteResult(ticker=t, name="partial_test", price=42.0, change_pct=3.0)

    def get_history(self, t, days=120):
        raise NotImplementedError("history not supported")

    def get_financials(self, t):
        raise NotImplementedError("financials not supported")

    def get_capital_flow(self, t, days=60):
        raise NotImplementedError("capital flow not supported")


class TestProviderRegistry:
    def test_execute_uses_first_provider(self):
        p1 = _MockOK(name="primary", priority=1)
        p2 = _MockOK(name="fallback", priority=2)
        registry = ProviderRegistry([p1, p2])
        result = registry.execute("get_quote", "000001")
        assert result.price == 10.0

    def test_fallback_on_failure(self):
        fail = _MockFail()
        ok = _MockOK(name="fallback", priority=2)
        registry = ProviderRegistry([fail, ok])
        result = registry.execute("get_quote", "000001")
        assert result.price == 10.0

    def test_all_failed_raises_error(self):
        fail1 = _MockFail()
        fail2 = _MockFail()
        registry = ProviderRegistry([fail1, fail2])
        with pytest.raises(AllProvidersFailedError) as exc:
            registry.execute("get_quote", "000001")
        assert "get_quote" in str(exc.value)
        assert "000001" in str(exc.value)

    def test_providers_sorted_by_priority(self):
        p3 = _MockOK(name="p3", priority=3)
        p1 = _MockOK(name="p1", priority=1)
        p2 = _MockOK(name="p2", priority=2)
        registry = ProviderRegistry([p3, p1, p2])
        names = [p.name for p in registry._providers]
        assert names == ["p1", "p2", "p3"]

    def test_fallback_skips_to_next_on_error(self):
        """When primary fails, fallback succeeds without re-trying primary."""
        fail = _MockFail()
        ok = _MockOK(name="backup", priority=2)
        registry = ProviderRegistry([fail, ok])
        # Should not raise
        result = registry.execute("get_history", "000001", days=120)
        assert len(result) == 1
        assert result[0].close == 10.5

    def test_not_implemented_falls_through_immediately(self):
        """NotImplementedError skips immediately — no retry, no circuit increment."""
        partial = _MockPartial(name="quote_only", priority=1)
        ok = _MockOK(name="full", priority=2)
        registry = ProviderRegistry([partial, ok])

        # get_quote hits partial (supports it)
        result = registry.execute("get_quote", "000001")
        assert result.price == 42.0

        # get_history: partial raises NotImplementedError → falls through to ok
        result = registry.execute("get_history", "000001", days=120)
        assert len(result) == 1
        assert result[0].close == 10.5

    def test_not_implemented_does_not_trigger_circuit_breaker(self):
        """NotImplementedError should NOT count as a failure for circuit breaker."""
        partial = _MockPartial(name="quote_only", priority=1)
        registry = ProviderRegistry([partial])

        # Call an unsupported method several times
        for _ in range(5):
            with suppress(AllProvidersFailedError):
                registry.execute("get_history", "000001")

        # Circuit should still be closed (NotImplementedError doesn't count)
        health = registry.health()
        cs = health["providers"]["quote_only"]
        assert cs["circuit_open"] is False
        assert cs["consecutive_failures"] == 0
