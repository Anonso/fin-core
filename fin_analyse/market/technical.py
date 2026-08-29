"""Technical indicator calculations — pure functions, no network I/O."""

import math

from .providers.base import OHLCV


def compute_ma(
    klines: list[OHLCV], periods: list[int] | None = None
) -> dict[str, list[float | None]]:
    """Simple Moving Average for each period. First N-1 values are None."""
    if periods is None:
        periods = [5, 10, 20, 60]
    closes = [k.close for k in klines]
    result: dict[str, list[float | None]] = {}
    for p in periods:
        ma: list[float | None] = [None] * len(closes)
        for i in range(p - 1, len(closes)):
            ma[i] = round(sum(closes[i - p + 1 : i + 1]) / p, 3)
        result[f"ma{p}"] = ma
    return result


def _ema(series: list[float], period: int) -> list[float]:
    """Exponential Moving Average. Handles leading NaN values."""
    if len(series) < period:
        return [float("nan")] * len(series)
    result = [float("nan")] * len(series)
    multiplier = 2.0 / (period + 1)

    # Find first non-NaN window of `period` values
    start = 0
    while start <= len(series) - period:
        window = series[start : start + period]
        if not any(math.isnan(v) for v in window):
            result[start + period - 1] = sum(window) / period
            break
        start += 1

    # EMA from seed point
    for i in range(start + period, len(series)):
        if math.isnan(series[i]) or math.isnan(result[i - 1]):
            continue
        result[i] = (series[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def compute_macd(klines: list[OHLCV], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD indicator. Returns {macd_dif, macd_dea, macd_histogram}."""
    closes = [k.close for k in klines]
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif: list[float | None] = [None] * len(closes)
    dea: list[float | None] = [None] * len(closes)
    histogram: list[float | None] = [None] * len(closes)

    for i in range(slow - 1, len(closes)):
        if not math.isnan(ema_fast[i]) and not math.isnan(ema_slow[i]):
            dif[i] = round(ema_fast[i] - ema_slow[i], 4)

    dif_clean = [d if d is not None else float("nan") for d in dif]
    dea_raw = _ema(dif_clean, signal)
    for i in range(len(closes)):
        if not math.isnan(dea_raw[i]):
            dea[i] = round(dea_raw[i], 4)

    for i in range(len(closes)):
        d = dif[i]
        e = dea[i]
        if d is not None and e is not None:
            histogram[i] = round((d - e) * 2, 4)

    return {"macd_dif": dif, "macd_dea": dea, "macd_histogram": histogram}


def compute_rsi(klines: list[OHLCV], period: int = 14) -> list[float | None]:
    """Relative Strength Index (Wilder's smoothing)."""
    if len(klines) < period + 1:
        return [None] * len(klines)
    closes = [k.close for k in klines]
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]

    result: list[float | None] = [None] * len(closes)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = round(100.0 - 100.0 / (1.0 + rs), 2)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = round(100.0 - 100.0 / (1.0 + rs), 2)

    return result


def compute_bollinger(klines: list[OHLCV], period: int = 20, std: int = 2) -> dict:
    """Bollinger Bands. Returns {boll_upper, boll_middle, boll_lower}."""
    closes = [k.close for k in klines]
    upper: list[float | None] = [None] * len(closes)
    middle: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)

    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        sma = sum(window) / period
        variance = sum((x - sma) ** 2 for x in window) / period
        sigma = math.sqrt(variance)
        middle[i] = round(sma, 3)
        upper[i] = round(sma + std * sigma, 3)
        lower[i] = round(sma - std * sigma, 3)

    return {"boll_upper": upper, "boll_middle": middle, "boll_lower": lower}


def compute_volume_ratio(klines: list[OHLCV], period: int = 5) -> list[float | None]:
    """Volume ratio: current volume / average volume over period."""
    volumes = [k.volume for k in klines]
    result: list[float | None] = [None] * len(volumes)

    for i in range(period - 1, len(volumes)):
        avg_vol = sum(volumes[i - period + 1 : i + 1]) / period
        if avg_vol > 0:
            result[i] = round(volumes[i] / avg_vol, 3)

    return result


def compute_atr(klines: list[OHLCV], period: int = 14) -> list[float | None]:
    """Average True Range (Wilder's smoothing)."""
    if len(klines) < period + 1:
        return [None] * len(klines)

    true_ranges: list[float] = []
    for i in range(1, len(klines)):
        high, low = klines[i].high, klines[i].low
        prev_close = klines[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    result: list[float | None] = [None] * len(klines)
    atr_val = sum(true_ranges[:period]) / period
    result[period] = round(atr_val, 4)

    for i in range(period, len(true_ranges)):
        atr_val = (atr_val * (period - 1) + true_ranges[i]) / period
        result[i + 1] = round(atr_val, 4)

    return result


def compute_all(klines: list[OHLCV]) -> dict:
    """Compute all technical indicators at once. Returns flat dict."""
    result = {}
    result.update(compute_ma(klines))
    result.update(compute_macd(klines))
    result["rsi14"] = compute_rsi(klines, period=14)
    result.update(compute_bollinger(klines))
    result["vol_ratio"] = compute_volume_ratio(klines)
    result["atr"] = compute_atr(klines)
    return result
