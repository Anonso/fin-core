"""Tests for cross-source market data consensus models."""

from fin_analyse.market.consensus import (
    SourceObservation,
    consensus_field,
    normalize_percent,
    normalize_ticker,
)


def test_normalize_ticker_beijing_prefix():
    assert normalize_ticker("BJ832000") == "832000"


def test_normalize_ticker_rejects_short_code():
    import pytest

    with pytest.raises(ValueError, match="must be exactly 6 digits"):
        normalize_ticker("SH123")


def test_normalize_ticker_keeps_six_digit_code():
    assert normalize_ticker("SH688017") == "688017"
    assert normalize_ticker("688017.SH") == "688017"
    assert normalize_ticker("000001") == "000001"


def test_normalize_percent_accepts_numeric_and_string_percent():
    assert normalize_percent("1.23%") == 1.23
    assert normalize_percent(1.23) == 1.23


def test_consensus_field_high_confidence_for_close_prices():
    observations = [
        SourceObservation(
            provider="mootdx", field="price", value=100.0, observed_at="2026-06-23T09:30:00"
        ),
        SourceObservation(
            provider="eastmoney", field="price", value=100.2, observed_at="2026-06-23T09:30:01"
        ),
    ]

    result = consensus_field("price", observations)

    assert result.value == 100.0
    assert result.confidence >= 0.85
    assert result.disagreement is not None
    assert result.warnings == []


def test_consensus_field_low_confidence_for_large_price_gap():
    observations = [
        SourceObservation(
            provider="mootdx", field="price", value=100.0, observed_at="2026-06-23T09:30:00"
        ),
        SourceObservation(
            provider="eastmoney", field="price", value=103.0, observed_at="2026-06-23T09:30:01"
        ),
    ]

    result = consensus_field("price", observations)

    assert result.confidence <= 0.50
    assert any("disagreement" in warning for warning in result.warnings)


def test_consensus_field_single_source_is_limited_confidence():
    observations = [
        SourceObservation(
            provider="mootdx", field="price", value=100.0, observed_at="2026-06-23T09:30:00"
        ),
    ]

    result = consensus_field("price", observations)

    assert result.value == 100.0
    assert result.confidence <= 0.60
    assert "single_source" in result.warnings


# ── MarketConsensusService tests ──


class FakeProvider:
    def __init__(self, name, quote=None, history=None, exc=None):
        self.name = name
        self.priority = 1
        self._quote = quote
        self._history = history or []
        self._exc = exc

    def get_quote(self, ticker):
        if self._exc:
            raise self._exc
        return self._quote

    def get_history(self, ticker, days=120):
        if self._exc:
            raise self._exc
        return self._history


def test_validate_quote_returns_consensus_for_two_sources():
    from fin_analyse.market.consensus import MarketConsensusService
    from fin_analyse.market.providers.base import QuoteResult

    service = MarketConsensusService(
        providers=[
            FakeProvider(
                "mootdx",
                quote=QuoteResult(
                    ticker="600519", name="茅台", price=100.0, change_pct=1.0, volume=1000
                ),
            ),
            FakeProvider(
                "eastmoney",
                quote=QuoteResult(
                    ticker="600519", name="茅台", price=100.2, change_pct=1.1, volume=1001
                ),
            ),
        ]
    )

    result = service.validate_quote("600519")

    assert result.ticker == "600519"
    assert result.kind == "quote"
    assert result.fields["price"].confidence >= 0.85
    assert result.provider_health == {"mootdx": "ok", "eastmoney": "ok"}


def test_validate_quote_records_provider_error():
    from fin_analyse.market.consensus import MarketConsensusService
    from fin_analyse.market.providers.base import QuoteResult

    service = MarketConsensusService(
        providers=[
            FakeProvider("mootdx", quote=QuoteResult(ticker="600519", name="茅台", price=100.0)),
            FakeProvider("eastmoney", exc=RuntimeError("blocked")),
        ]
    )

    result = service.validate_quote("600519")

    assert result.fields["price"].confidence <= 0.60
    assert result.provider_health["eastmoney"].startswith("error:")


def test_validate_history_compares_latest_close():
    from fin_analyse.market.consensus import MarketConsensusService
    from fin_analyse.market.providers.base import OHLCV

    service = MarketConsensusService(
        providers=[
            FakeProvider(
                "mootdx",
                history=[OHLCV(date="2026-06-23", open=9, high=11, low=8, close=10.0, volume=1000)],
            ),
            FakeProvider(
                "baostock",
                history=[
                    OHLCV(date="2026-06-23", open=9, high=11, low=8, close=10.02, volume=1001)
                ],
            ),
        ]
    )

    result = service.validate_history("600519", days=5)

    assert result.kind == "history"
    assert result.fields["close"].confidence >= 0.85
