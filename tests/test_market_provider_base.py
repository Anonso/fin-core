"""Tests for BaseMarketProvider ABC and dataclasses."""

from dataclasses import FrozenInstanceError

import pytest

from fin_analyse.market.providers.base import OHLCV, BaseMarketProvider, CapitalFlow, QuoteResult


class TestOHLCV:
    def test_create_ohlcv(self):
        o = OHLCV(
            date="2026-06-20",
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            volume=1_000_000,
            turnover=10_500_000.0,
        )
        assert o.close == 10.5
        assert o.turnover == 10_500_000.0

    def test_ohlcv_frozen(self):
        o = OHLCV(date="2026-06-20", open=10.0, high=11.0, low=9.5, close=10.5, volume=1_000_000)
        with pytest.raises(FrozenInstanceError):
            o.close = 11.0  # frozen dataclass

    def test_ohlcv_turnover_optional(self):
        o = OHLCV(date="2026-06-20", open=10.0, high=11.0, low=9.5, close=10.5, volume=1_000_000)
        assert o.turnover is None


class TestCapitalFlow:
    def test_create_flow(self):
        cf = CapitalFlow(
            date="2026-06-20",
            northbound_net=1.5,
            main_net=-0.3,
            margin_balance=500.0,
            short_balance=10.0,
        )
        assert cf.northbound_net == 1.5

    def test_flow_all_none(self):
        cf = CapitalFlow(date="2026-06-20")
        assert cf.northbound_net is None
        assert cf.main_net is None


class TestQuoteResult:
    def test_create_quote(self):
        q = QuoteResult(
            ticker="000001",
            name="平安银行",
            price=12.50,
            change_pct=2.35,
            volume=50_000_000,
            turnover=625_000_000,
        )
        assert q.price == 12.50
        assert q.name == "平安银行"

    def test_quote_minimal(self):
        q = QuoteResult(ticker="000001", name="test")
        assert q.price is None
        assert q.ticker == "000001"


class TestBaseMarketProvider:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            BaseMarketProvider()

    def test_concrete_subclass_needs_all_abstract_methods(self):
        class IncompleteProvider(BaseMarketProvider):
            @property
            def name(self):
                return "incomplete"

            @property
            def priority(self):
                return 99

            # Missing: search_stock, get_quote, get_history,
            #          get_financials, get_capital_flow

        with pytest.raises(TypeError):
            IncompleteProvider()
