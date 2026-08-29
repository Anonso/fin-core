"""Tests for MarketDataAdapter."""

from fin_analyse.market.adapter import MarketDataAdapter
from fin_analyse.market.consensus import MarketConsensusService


def test_market_adapter_exposes_consensus_service():
    adapter = MarketDataAdapter()
    assert isinstance(adapter.consensus, MarketConsensusService)
