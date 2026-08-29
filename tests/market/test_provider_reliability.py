"""Test provider registry retry and circuit breaker."""

from fin_analyse.market.registry import AllProvidersFailedError, ProviderRegistry


class FakeFailingProvider:
    name = "failing"
    priority = 10

    def __init__(self) -> None:
        self.call_count = 0

    def get_quote(self, ticker: str):
        self.call_count += 1
        raise ConnectionError("simulated failure")


class FakeWorkingProvider:
    name = "working"
    priority = 20

    def get_quote(self, ticker: str):
        return {"price": 42.0, "ticker": ticker}


def test_registry_falls_back_to_second_provider():
    registry = ProviderRegistry([FakeFailingProvider(), FakeWorkingProvider()])
    result = registry.execute("get_quote", "000001", max_retries=1)
    assert result["price"] == 42.0


def test_registry_raises_when_all_fail():
    registry = ProviderRegistry([FakeFailingProvider()])
    try:
        registry.execute("get_quote", "000001", max_retries=1)
        raise AssertionError("should have raised")
    except AllProvidersFailedError:
        pass


def test_health_reports_providers():
    registry = ProviderRegistry([FakeWorkingProvider()])
    h = registry.health()
    assert h["total_providers"] == 1
    assert "working" in h["providers"]


def test_circuit_opens_after_consecutive_failures():
    import contextlib

    provider = FakeFailingProvider()
    registry = ProviderRegistry([provider])
    for _ in range(3):
        with contextlib.suppress(AllProvidersFailedError):
            registry.execute("get_quote", "000001", max_retries=1)
    h = registry.health()
    assert h["providers"]["failing"]["circuit_open"] is True
    assert h["providers"]["failing"]["consecutive_failures"] >= 3
