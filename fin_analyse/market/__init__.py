"""Market data layer — multi-source, cached, with technical indicators."""

from .adapter import MarketDataAdapter
from .cache import TTLCache
from .providers.akshare import AKShareProvider
from .providers.base import OHLCV, BaseMarketProvider, CapitalFlow, QuoteResult
from .providers.easyquotation import EasyQuotationProvider
from .providers.mootdx import MootdxProvider
from .registry import AllProvidersFailedError, ProviderRegistry
from .snapshot import MarketSnapshotRequest, MarketSnapshotResult, MarketSnapshotService
from .technical import compute_all, compute_bollinger, compute_ma, compute_macd, compute_rsi
from .valuation import (
    ValuationSignal,
    compute_valuation,
    enrich_signals_with_llm,
    get_financial_time_series,
)


def create_default_registry() -> ProviderRegistry:
    """Return a ProviderRegistry with the canonical fallback chain.

    Ordered by speed & timeliness:
      easyquotation(5,秒级) → eastmoney(10,<500ms) → mootdx(15,TCP)
      → baostock(20,历史最准) → akshare(30,大全兜底)

    This is the SINGLE SOURCE OF TRUTH for the provider chain.
    All callers that need a ProviderRegistry should use this function
    instead of constructing their own list.
    """
    from .providers.baostock_provider import BaoStockProvider
    from .providers.eastmoney import EastMoneyProvider

    return ProviderRegistry(
        [
            EasyQuotationProvider(),
            EastMoneyProvider(),
            MootdxProvider(),
            BaoStockProvider(),
            AKShareProvider(),
        ]
    )


__all__ = [
    "MarketDataAdapter",
    "BaseMarketProvider",
    "AKShareProvider",
    "EasyQuotationProvider",
    "MootdxProvider",
    "OHLCV",
    "CapitalFlow",
    "QuoteResult",
    "TTLCache",
    "ProviderRegistry",
    "AllProvidersFailedError",
    "create_default_registry",
    "compute_all",
    "compute_ma",
    "compute_macd",
    "compute_rsi",
    "compute_bollinger",
    "get_financial_time_series",
    "compute_valuation",
    "enrich_signals_with_llm",
    "ValuationSignal",
    "MarketSnapshotRequest",
    "MarketSnapshotResult",
    "MarketSnapshotService",
]
