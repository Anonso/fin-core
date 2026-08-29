"""Tests for technical indicator calculations."""

import pytest

from fin_analyse.market.providers.base import OHLCV
from fin_analyse.market.technical import (
    compute_all,
    compute_atr,
    compute_bollinger,
    compute_ma,
    compute_macd,
    compute_rsi,
    compute_volume_ratio,
)


def make_klines(prices: list[float], volumes: list[float] | None = None) -> list[OHLCV]:
    """Helper: create OHLCV list. OHLC all = price for simplicity."""
    if volumes is None:
        volumes = [1_000_000] * len(prices)
    results = []
    for i, p in enumerate(prices):
        results.append(
            OHLCV(
                date=f"2026-06-{i + 1:02d}",
                open=p,
                high=p * 1.02,
                low=p * 0.98,
                close=p,
                volume=volumes[i],
            )
        )
    return results


class TestMA:
    def test_sma_basic(self):
        klines = make_klines([10.0, 11.0, 12.0, 13.0, 14.0])
        result = compute_ma(klines, periods=[3, 5])
        assert "ma3" in result
        assert "ma5" in result
        assert result["ma3"][0] is None
        assert result["ma3"][1] is None
        assert result["ma3"][2] == pytest.approx(11.0)
        assert result["ma3"][3] == pytest.approx(12.0)
        assert result["ma5"][4] == pytest.approx(12.0)

    def test_default_periods(self):
        klines = make_klines(list(range(100, 200)))
        result = compute_ma(klines)
        assert "ma5" in result
        assert "ma10" in result
        assert "ma20" in result
        assert "ma60" in result


class TestMACD:
    def test_macd_basic(self):
        # 30 constant prices → DIF ≈ 0, DEA ≈ 0, histogram ≈ 0
        klines = make_klines([10.0] * 40)
        result = compute_macd(klines)
        assert result["macd_dif"][-1] is not None
        assert abs(result["macd_dif"][-1]) < 0.01

    def test_macd_trending(self):
        # Upward trend → positive DIF
        klines = make_klines([10.0 + i * 0.1 for i in range(40)])
        result = compute_macd(klines)
        assert result["macd_dif"][-1] is not None
        # DIF should be positive in uptrend
        assert result["macd_dif"][-1] > 0


class TestRSI:
    def test_rsi_all_up(self):
        prices = list(range(100, 116))  # 15 up days, 0 down
        klines = make_klines(prices)
        result = compute_rsi(klines, period=14)
        assert result[14] == pytest.approx(100.0, abs=0.01)

    def test_rsi_mixed(self):
        prices = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100]
        klines = make_klines(prices)
        result = compute_rsi(klines, period=14)
        assert 45 < result[-1] < 55

    def test_rsi_insufficient_data(self):
        klines = make_klines([10.0] * 10)
        result = compute_rsi(klines, period=14)
        assert all(r is None for r in result)


class TestBollinger:
    def test_bollinger_constant(self):
        klines = make_klines([10.0] * 20)
        result = compute_bollinger(klines, period=20, std=2)
        assert result["boll_upper"][-1] == pytest.approx(10.0)
        assert result["boll_middle"][-1] == pytest.approx(10.0)
        assert result["boll_lower"][-1] == pytest.approx(10.0)

    def test_bollinger_separation(self):
        prices = [10.0 + i for i in range(20)]
        klines = make_klines(prices)
        result = compute_bollinger(klines, period=20, std=2)
        assert result["boll_upper"][-1] > result["boll_middle"][-1]
        assert result["boll_lower"][-1] < result["boll_middle"][-1]


class TestVolumeRatio:
    def test_volume_ratio(self):
        volumes = [1_000_000, 1_000_000, 1_000_000, 1_000_000, 2_000_000]
        klines = make_klines([10.0] * 5, volumes=volumes)
        result = compute_volume_ratio(klines, period=5)
        assert result[4] == pytest.approx(2_000_000 / 1_200_000, rel=0.01)


class TestATR:
    def test_atr_basic(self):
        klines = make_klines([10.0] * 16)
        result = compute_atr(klines, period=14)
        # ATR = max(H-L, |H-prevC|, |L-prevC|) ≈ 0.04 * 10 = 0.4 per bar
        assert result[14] is not None
        assert result[14] > 0

    def test_atr_insufficient(self):
        klines = make_klines([10.0] * 10)
        result = compute_atr(klines, period=14)
        assert all(r is None for r in result)


class TestComputeAll:
    def test_compute_all_keys(self):
        klines = make_klines(list(range(100, 200)))
        result = compute_all(klines)
        for key in ("ma5", "macd_histogram", "rsi14", "boll_upper", "vol_ratio", "atr"):
            assert key in result, f"Missing key: {key}"
