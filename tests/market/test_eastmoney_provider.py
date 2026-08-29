"""Smoke tests for EastMoneyProvider and BaoStockProvider structure."""

from fin_analyse.market.providers.baostock_provider import BaoStockProvider
from fin_analyse.market.providers.eastmoney import EastMoneyProvider, _parse_kline_row, _secid


def test_secid_shanghai():
    assert _secid("600519") == "1.600519"


def test_secid_shenzhen():
    assert _secid("000001") == "0.000001"


def test_secid_gem():
    assert _secid("300750") == "0.300750"


def test_parse_kline_row():
    row = "2024-01-15,100.50,102.30,103.00,99.80,1000000,102500000.00,3.2,1.8,1.80,0.5"
    ohlcv = _parse_kline_row(row)
    assert ohlcv.date == "2024-01-15"
    assert ohlcv.close == 102.30
    assert ohlcv.volume == 1000000.0
    assert ohlcv.turnover == 102500000.0


def test_eastmoney_priority():
    p = EastMoneyProvider()
    assert p.name == "eastmoney"
    assert p.priority == 10


def test_baostock_priority():
    p = BaoStockProvider()
    assert p.name == "baostock"
    assert p.priority == 20
