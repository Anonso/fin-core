"""Base classes and shared dataclasses for market data providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class OHLCV:
    """Single-period OHLCV candle."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float | None = None


@dataclass(frozen=True)
class CapitalFlow:
    """Single-day capital flow data."""

    date: str
    northbound_net: float | None = None  # 北向净买入（亿）
    main_net: float | None = None  # 主力净流入（亿）
    margin_balance: float | None = None  # 融资余额（亿）
    short_balance: float | None = None  # 融券余额（亿）


@dataclass(frozen=True)
class QuoteResult:
    """Real-time quote snapshot."""

    ticker: str
    name: str
    price: float | None = None
    change_pct: float | None = None
    volume: float | None = None
    turnover: float | None = None


class BaseMarketProvider(ABC):
    """行情数据提供者抽象基类。

    每个子类代表一个数据源（akshare / tushare / eastmoney）。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider display name, e.g. 'akshare'."""
        ...

    @property
    @abstractmethod
    def priority(self) -> int:
        """Lower = higher priority in fallback chain."""
        ...

    @abstractmethod
    def search_stock(self, name_or_ticker: str) -> dict:
        """Search for a stock by name or ticker. Returns {name, ticker, market}."""
        ...

    @abstractmethod
    def get_quote(self, ticker: str) -> QuoteResult:
        """Get real-time quote for a ticker."""
        ...

    @abstractmethod
    def get_history(self, ticker: str, days: int = 120) -> list[OHLCV]:
        """Get historical OHLCV data."""
        ...

    @abstractmethod
    def get_financials(self, ticker: str) -> dict:
        """Get latest financial data."""
        ...

    @abstractmethod
    def get_capital_flow(self, ticker: str, days: int = 60) -> list[CapitalFlow]:
        """Get capital flow data."""
        ...

    # ── Optional interfaces ──

    def get_margin_history(self, ticker: str, days: int = 60) -> list[dict]:
        """Get margin trading history. Optional."""
        return []

    def get_block_trades(self, ticker: str, days: int = 60) -> list[dict]:
        """Get block trade / 龙虎榜 data. Optional."""
        return []

    def health_check(self) -> bool:
        """Check if provider is accessible. Default: assume yes."""
        return True
