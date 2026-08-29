"""Tests for market adapter."""

from fin_analyse.market.adapter import MarketDataAdapter


class TestMarketDataAdapter:
    def test_adapter_initialization(self):
        adapter = MarketDataAdapter()
        assert adapter is not None

    def test_adapter_has_methods(self):
        adapter = MarketDataAdapter()
        assert (
            hasattr(adapter, "fetch")
            or hasattr(adapter, "get_price")
            or hasattr(adapter, "get_financials")
        )
