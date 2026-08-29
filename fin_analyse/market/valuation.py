"""Valuation & financial depth analysis.

PE/PB percentile, ROE trend, revenue growth, cash flow quality.
Uses akshare for financial statements + yfinance for price history.

Trend-aware signals (v2): all metrics output as ValuationSignal with
trend state, strength, and direction. Growth metrics use inflection-point
detection instead of static 3-year CAGR.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .providers.base import OHLCV

logger = logging.getLogger(__name__)


# ── Unified valuation signal ──────────────────────────────────────────────


@dataclass
class ValuationSignal:
    """Unified valuation signal with trend state, strength, and interpretation."""

    key: str
    label: str
    category: str  # "valuation" | "growth" | "quality" | "risk" | "per_share"
    value: float | None
    unit: str  # "%" | "x" | "元"
    trend: str  # extreme_low|extreme_high|recovering|accelerating|decelerating|declining|stable|improving|deteriorating|unknown
    strength: float  # 0.0-1.0, higher = more noteworthy
    direction: str  # "positive" | "negative" | "neutral"
    summary: str = ""  # LLM-generated one-liner, filled later


# ── Direction mapping for growth/trend states ──────────────────────────────

_TREND_DIRECTION: dict[str, str] = {
    "extreme_low": "positive",
    "extreme_high": "negative",
    "recovering": "positive",
    "accelerating": "positive",
    "decelerating": "neutral",
    "declining": "negative",
    "stable": "neutral",
    "improving": "positive",
    "deteriorating": "negative",
    "unknown": "neutral",
}

# ── Strength presets for trend-based signals ───────────────────────────────

_TREND_STRENGTH: dict[str, float] = {
    "extreme_low": 0.90,
    "extreme_high": 0.90,
    "recovering": 0.75,
    "accelerating": 0.85,
    "decelerating": 0.40,
    "declining": 0.80,
    "stable": 0.10,
    "improving": 0.60,
    "deteriorating": 0.65,
    "unknown": 0.0,
}


# ── Helper functions ───────────────────────────────────────────────────────


def _compute_percentile(values: list[float], current: float) -> float:
    """What percentage of historical values are below current? Returns 0-100."""
    if not values:
        return 50.0
    below = sum(1 for v in values if v < current)
    return round(below / len(values) * 100, 1)


def _compute_cagr(start_val: float, end_val: float, years: float) -> float | None:
    """Compound annual growth rate."""
    if start_val <= 0 or end_val <= 0 or years <= 0:
        return None
    cagr: float = (end_val / start_val) ** (1.0 / years) - 1.0
    return float(round(cagr, 4))


def _safe_float(val: Any) -> float | None:
    """Convert akshare value to float, handling units like '亿' or '%'."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        if not val or val in ("False", "True"):
            return None
        # Remove unit suffixes
        multiplier = 1.0
        if "亿" in val:
            multiplier = 1e8
            val = val.replace("亿", "")
        elif "万" in val:
            multiplier = 1e4
            val = val.replace("万", "")
        if "%" in val:
            val = val.replace("%", "")
            try:
                return float(val) / 100.0
            except ValueError:
                return None
        try:
            return float(val) * multiplier
        except ValueError:
            return None
    return None


def _compute_growth_trend(reports: list[dict], field_prefix: str) -> dict:
    """Detect growth inflection point and trend direction from quarterly YoY data.

    Uses the last 8 quarters of {field_prefix}_yoy from reports to find:
    1. Inflection point: earliest quarter after which >=2 consecutive QoQ improvements
    2. Trend classification: recovering | accelerating | decelerating | declining | stable

    Parameters
    ----------
    reports : list[dict]
        Financial reports sorted by period ascending (oldest first).
        Each report must have "{field_prefix}_yoy" key.
    field_prefix : str
        "revenue" or "net_profit".

    Returns
    -------
    dict
        {"trend": str, "latest_yoy": float|None, "consecutive_quarters": int,
         "inflection_period": str|None, "strength": float, "direction": str,
         "yoy_sequence": list[float|None]}
    """
    default: dict = {
        "trend": "unknown",
        "latest_yoy": None,
        "consecutive_quarters": 0,
        "inflection_period": None,
        "strength": 0.0,
        "direction": "neutral",
        "yoy_sequence": [],
    }
    yoy_key = f"{field_prefix}_yoy"

    # Extract last 8 quarters of YoY (with period for inflection tracking)
    yoy_pairs: list[tuple[str, float]] = []
    for r in reports:
        period = r.get("period", "")
        val = r.get(yoy_key)
        if period and val is not None:
            yoy_pairs.append((period, float(val)))

    if len(yoy_pairs) < 4:
        return default

    recent = yoy_pairs[-8:]  # last 8 quarters
    yoy_seq = [v for _, v in recent]
    default["yoy_sequence"] = yoy_seq
    default["latest_yoy"] = yoy_seq[-1] if yoy_seq else None

    # Find inflection point: last quarter where next 2 quarters improve QoQ
    # (search backwards to find the most recent turnaround)
    inflection_idx: int | None = None
    _min_delta = 2.0  # minimum 2pp improvement to count as real inflection (filters noise)
    for i in range(len(recent) - 3, -1, -1):
        d1 = yoy_seq[i + 1] - yoy_seq[i]
        d2 = yoy_seq[i + 2] - yoy_seq[i + 1]
        if d1 >= _min_delta and d2 >= _min_delta:
            inflection_idx = i
            break

    if inflection_idx is not None:
        default["inflection_period"] = recent[inflection_idx][0]
        # Count consecutive improving quarters from inflection
        improving = 1
        for j in range(inflection_idx + 1, len(recent)):
            if yoy_seq[j] > yoy_seq[j - 1]:
                improving += 1
            else:
                break
        default["consecutive_quarters"] = improving

    # Classify trend
    latest = yoy_seq[-1]
    if (
        inflection_idx is not None
        and inflection_idx >= len(recent) - 5
        and (latest is not None and latest > 0)
    ):
        # Recent inflection + positive latest → recovering or accelerating
        if len(yoy_seq) >= 3:
            recent_delta = yoy_seq[-1] - yoy_seq[-2]
            prev_delta = yoy_seq[-2] - yoy_seq[-3]
            if recent_delta > prev_delta and recent_delta > 0:
                default["trend"] = "accelerating"
            else:
                default["trend"] = "recovering"
        else:
            default["trend"] = "recovering"
    elif inflection_idx is None:
        # No clear inflection — check direction of recent movement
        if len(yoy_seq) >= 3:
            if yoy_seq[-1] < yoy_seq[-2] < yoy_seq[-3]:
                # Persistent sequential decline
                if yoy_seq[-1] >= 0:
                    default["trend"] = "decelerating"  # positive but slowing
                else:
                    default["trend"] = "declining"
            else:
                default["trend"] = "stable"
        else:
            default["trend"] = "stable"
    elif latest is not None and latest > 0:
        # Old inflection, still positive — check if decelerating
        if len(yoy_seq) >= 2 and yoy_seq[-1] < yoy_seq[-2]:
            default["trend"] = "decelerating"
        else:
            default["trend"] = "stable"
    elif latest is not None and latest < 0:
        # Old inflection, now negative — declining
        default["trend"] = "declining"
    else:
        default["trend"] = "stable"

    default["strength"] = _TREND_STRENGTH.get(default["trend"], 0.1)
    default["direction"] = _TREND_DIRECTION.get(default["trend"], "neutral")
    return default


def _compute_delta_trend(reports: list[dict], field: str) -> dict:
    """Compute simple half-vs-half trend for non-YoY metrics (net_margin, debt_ratio, eps, bps).

    Parameters
    ----------
    reports : list[dict]
        Financial reports sorted by period ascending.
    field : str
        Field name in report dict (e.g. "net_margin", "debt_ratio", "eps").

    Returns
    -------
    dict
        {"trend": str, "strength": float, "direction": str}
    """
    values = [r.get(field) for r in reports[-8:] if r.get(field) is not None]
    if len(values) < 4:
        return {"trend": "unknown", "strength": 0.0, "direction": "neutral"}

    mid = len(values) // 2
    first_half_vals = [v for v in values[:mid] if v]
    second_half_vals = [v for v in values[mid:] if v]
    if not first_half_vals or not second_half_vals:
        return {"trend": "unknown", "strength": 0.0, "direction": "neutral"}

    first_half = sum(first_half_vals) / len(first_half_vals)
    second_half = sum(second_half_vals) / len(second_half_vals)

    if first_half == 0:
        return {"trend": "stable", "strength": 0.1, "direction": "neutral"}

    ratio = second_half / first_half
    if ratio > 1.10:
        # For debt_ratio, increasing is bad; for net_margin/eps, increasing is good
        return {"trend": "improving", "strength": 0.60, "direction": "positive"}
    elif ratio < 0.90:
        return {"trend": "deteriorating", "strength": 0.65, "direction": "negative"}
    else:
        return {"trend": "stable", "strength": 0.10, "direction": "neutral"}


def get_financial_time_series(ticker: str) -> dict[str, Any]:
    """Fetch full financial history from akshare.

    Returns dict with lists keyed by report period.
    """
    try:
        import akshare as ak

        df = ak.stock_financial_abstract_ths(symbol=ticker, indicator="按报告期")
        if df.empty:
            return {"ticker": ticker, "reports": []}

        reports = []
        for _, row in df.iterrows():
            report = {
                "period": str(row.get("报告期", "")),
                "revenue": _safe_float(row.get("营业总收入")),
                "revenue_yoy": _safe_float(row.get("营业总收入同比增长率")),
                "net_profit": _safe_float(row.get("净利润")),
                "net_profit_yoy": _safe_float(row.get("净利润同比增长率")),
                "eps": _safe_float(row.get("基本每股收益")),
                "bps": _safe_float(row.get("每股净资产")),  # book value per share
                "cf_per_share": _safe_float(row.get("每股经营现金流")),
                "roe": _safe_float(row.get("净资产收益率")),
                "net_margin": _safe_float(row.get("销售净利率")),
                "debt_ratio": _safe_float(row.get("资产负债率")),
                "current_ratio": _safe_float(row.get("流动比率")),
                "quick_ratio": _safe_float(row.get("速动比率")),
            }
            reports.append(report)

        return {"ticker": ticker, "reports": reports}
    except Exception as exc:
        logger.warning("Failed to fetch financials for %s: %s", ticker, exc)
        return {"ticker": ticker, "reports": []}


def _add_signal(signals: list[ValuationSignal], s: ValuationSignal) -> None:
    """Round strength to 2 decimals, clamp to [0,1], and append."""
    s.strength = round(min(max(s.strength, 0.0), 1.0), 2)
    signals.append(s)


def _build_signals(
    reports: list[dict],
    latest: dict,
    pe: float | None,
    pe_percentile: float | None,
    pb: float | None,
    pb_percentile: float | None,
    roe_trend: str,
    revenue_trend: dict,
    profit_trend: dict,
    cash_flow_quality: str,
    debt_ratio: float | None,
    net_margin: float | None,
    eps: float | None,
    bps: float | None,
) -> tuple[list[ValuationSignal], dict[str, ValuationSignal]]:
    """Build sorted signal list + lookup map from computed metrics."""
    roe = latest.get("roe")
    debt_trend = _compute_delta_trend(reports, "debt_ratio")
    # Invert trend for debt_ratio: rising debt is bad, falling debt is good
    if debt_trend["trend"] == "improving":
        debt_trend = {**debt_trend, "trend": "deteriorating", "direction": "negative"}
    elif debt_trend["trend"] == "deteriorating":
        debt_trend = {**debt_trend, "trend": "improving", "direction": "positive"}
    margin_trend = _compute_delta_trend(reports, "net_margin")
    eps_trend = _compute_delta_trend(reports, "eps")

    # Direction/strength for ratio-based metrics (value-dependent)
    if debt_ratio is not None:
        debt_direction = "negative" if debt_ratio > 50 else "positive"
        debt_strength = min(abs(debt_ratio - 50) / 30, 1.0)
    else:
        debt_direction = "neutral"
        debt_strength = 0.0

    if net_margin is not None:
        margin_direction = "positive" if net_margin > 10 else "negative"
        margin_strength = min(abs(net_margin - 10) / 20, 1.0)
    else:
        margin_direction = "neutral"
        margin_strength = 0.0

    signals: list[ValuationSignal] = []

    # ── valuation ──
    _add_signal(
        signals,
        ValuationSignal(
            key="pe_percentile",
            label="PE 分位",
            category="valuation",
            value=pe_percentile,
            unit="%",
            trend="extreme_low"
            if (pe_percentile is not None and pe_percentile < 10)
            else "extreme_high"
            if (pe_percentile is not None and pe_percentile > 90)
            else "stable",
            strength=abs(pe_percentile - 50) / 50 if pe_percentile is not None else 0.0,
            direction="positive"
            if (pe_percentile is not None and pe_percentile < 30)
            else "negative"
            if (pe_percentile is not None and pe_percentile > 70)
            else "neutral",
            summary="",
        ),
    )
    _add_signal(
        signals,
        ValuationSignal(
            key="pe",
            label="PE(TTM)",
            category="valuation",
            value=pe,
            unit="x",
            trend="stable",
            strength=0.15 if pe is not None else 0.0,
            direction="neutral",
            summary="",
        ),
    )
    _add_signal(
        signals,
        ValuationSignal(
            key="pb_percentile",
            label="PB 分位",
            category="valuation",
            value=pb_percentile,
            unit="%",
            trend="extreme_low"
            if (pb_percentile is not None and pb_percentile < 10)
            else "extreme_high"
            if (pb_percentile is not None and pb_percentile > 90)
            else "stable",
            strength=abs(pb_percentile - 50) / 50 if pb_percentile is not None else 0.0,
            direction="positive"
            if (pb_percentile is not None and pb_percentile < 30)
            else "negative"
            if (pb_percentile is not None and pb_percentile > 70)
            else "neutral",
            summary="",
        ),
    )
    _add_signal(
        signals,
        ValuationSignal(
            key="pb",
            label="PB",
            category="valuation",
            value=pb,
            unit="x",
            trend="stable",
            strength=0.10 if pb is not None else 0.0,
            direction="neutral",
            summary="",
        ),
    )

    # ── growth ──
    _add_signal(
        signals,
        ValuationSignal(
            key="revenue_trend",
            label="营收趋势",
            category="growth",
            value=revenue_trend["latest_yoy"],
            unit="%",
            trend=revenue_trend["trend"],
            strength=revenue_trend["strength"],
            direction=revenue_trend["direction"],
            summary="",
        ),
    )
    _add_signal(
        signals,
        ValuationSignal(
            key="profit_trend",
            label="净利趋势",
            category="growth",
            value=profit_trend["latest_yoy"],
            unit="%",
            trend=profit_trend["trend"],
            strength=profit_trend["strength"],
            direction=profit_trend["direction"],
            summary="",
        ),
    )
    _add_signal(
        signals,
        ValuationSignal(
            key="roe_trend",
            label="ROE 趋势",
            category="growth",
            value=roe,
            unit="%",
            trend=roe_trend,
            strength=_TREND_STRENGTH.get(roe_trend, 0.1),
            direction=_TREND_DIRECTION.get(roe_trend, "neutral"),
            summary="",
        ),
    )

    # ── quality ──
    cq_strength = {"strong": 0.5, "adequate": 0.2, "weak": 0.8, "unknown": 0.0}.get(
        cash_flow_quality, 0.0
    )
    cq_direction = {
        "strong": "positive",
        "adequate": "neutral",
        "weak": "negative",
        "unknown": "neutral",
    }.get(cash_flow_quality, "neutral")
    _add_signal(
        signals,
        ValuationSignal(
            key="cash_flow_quality",
            label="现金流质量",
            category="quality",
            value=None,
            unit="",
            trend=cash_flow_quality,
            strength=cq_strength,
            direction=cq_direction,
            summary="",
        ),
    )
    _add_signal(
        signals,
        ValuationSignal(
            key="net_margin",
            label="净利率",
            category="quality",
            value=net_margin,
            unit="%",
            trend=margin_trend["trend"],
            strength=margin_strength,
            direction=margin_direction,
            summary="",
        ),
    )

    # ── risk ──
    _add_signal(
        signals,
        ValuationSignal(
            key="debt_ratio",
            label="资产负债率",
            category="risk",
            value=debt_ratio,
            unit="%",
            trend=debt_trend["trend"],
            strength=debt_strength,
            direction=debt_direction,
            summary="",
        ),
    )

    # ── per_share ──
    _add_signal(
        signals,
        ValuationSignal(
            key="eps",
            label="每股收益",
            category="per_share",
            value=eps,
            unit="元",
            trend=eps_trend["trend"],
            strength=eps_trend["strength"],
            direction=eps_trend["direction"],
            summary="",
        ),
    )
    _add_signal(
        signals,
        ValuationSignal(
            key="bps",
            label="每股净资产",
            category="per_share",
            value=bps,
            unit="元",
            trend="stable",
            strength=0.05,
            direction="neutral",
            summary="",
        ),
    )

    # Sort by strength descending
    signals.sort(key=lambda s: s.strength, reverse=True)
    signal_map = {s.key: s for s in signals}
    return signals, signal_map


def compute_valuation(
    ticker: str,
    klines: list[OHLCV] | None = None,
    financials: dict | None = None,
) -> dict:
    """Compute valuation metrics: PE/PB percentile, ROE trend, revenue CAGR.

    Parameters
    ----------
    ticker : str
        Stock ticker code.
    klines : list[OHLCV] | None
        Historical price data for PE/PB percentile calculation.
    financials : dict | None
        Result from get_financial_time_series().

    Returns
    -------
    dict
        Valuation result with PE/PB percentiles, ROE trend, growth, quality.
        New in v2: ``signals`` (trend-aware, sorted by strength),
        ``signal_map`` (key → signal lookup), ``valuation_narrative``.
    """

    if financials is None:
        financials = get_financial_time_series(ticker)

    reports = financials.get("reports", [])

    # ── Empty reports: return defaults ──
    if not reports:
        return {
            "ticker": ticker,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "pe": None,
            "pe_percentile": None,
            "pb": None,
            "pb_percentile": None,
            "roe": None,
            "roe_trend": "unknown",
            "revenue_growth_3y": None,
            "net_profit_growth_3y": None,
            "cash_flow_quality": "unknown",
            "debt_ratio": None,
            "net_margin": None,
            "eps": None,
            "bps": None,
            "signals": [],
            "signal_map": {},
            "valuation_narrative": "",
            "_revenue_trend": {},
            "_profit_trend": {},
        }

    latest = reports[-1]
    current_price = None
    if klines and len(klines) > 0:
        current_price = klines[-1].close

    eps = latest.get("eps")
    bps = latest.get("bps")
    roe = latest.get("roe")
    debt_ratio = latest.get("debt_ratio")
    net_margin = latest.get("net_margin")
    cf_per_share = latest.get("cf_per_share")

    # PE / PB
    pe = round(current_price / eps, 2) if (current_price and eps and eps > 0) else None
    pb = round(current_price / bps, 2) if (current_price and bps and bps > 0) else None

    # PE/PB percentile (using historical EPS and price data)
    pe_percentile = None
    pb_percentile = None
    if klines and len(klines) >= 60:
        hist_eps = [r.get("eps") for r in reports if r.get("eps") and r["eps"] > 0]
        if hist_eps and eps:
            hist_prices = (
                [k.close for k in klines[-len(hist_eps) :]]
                if len(klines) >= len(hist_eps)
                else [k.close for k in klines]
            )
            hist_pe = [
                p / e
                for p, e in zip(
                    hist_prices[-len(hist_eps) :], hist_eps[-len(hist_prices) :], strict=False
                )
                if e is not None
            ]
            if pe and hist_pe:
                pe_percentile = _compute_percentile(hist_pe, pe)
        if bps and bps > 0 and hist_eps:
            hist_pb = [
                p / b
                for p, b in zip(
                    hist_prices[-len(hist_eps) :],
                    [r.get("bps", bps) for r in reports[-len(hist_prices) :]],
                    strict=False,
                )
                if b and b > 0
            ]
            if pb and hist_pb:
                pb_percentile = _compute_percentile(hist_pb, pb)

    # ROE trend (3-year: direction of last 12 quarters)
    roe_trend_old = "unknown"
    roe_values = [r.get("roe") for r in reports[-12:] if r.get("roe") is not None]
    if len(roe_values) >= 6:
        mid = len(roe_values) // 2
        first_half_vals = [v for v in roe_values[:mid] if v]
        second_half_vals = [v for v in roe_values[mid:] if v]
        if first_half_vals and second_half_vals:
            first_half = sum(first_half_vals) / len(first_half_vals)
            second_half = sum(second_half_vals) / len(second_half_vals)
            if first_half != 0:
                ratio = second_half / first_half
                if ratio > 1.05:
                    roe_trend_old = "up"
                elif ratio < 0.95:
                    roe_trend_old = "down"
                else:
                    roe_trend_old = "flat"
            else:
                roe_trend_old = "up" if second_half > 0 else "down"

    # ── NEW: Growth trend (inflection-point detection, replaces CAGR for decision-making) ──
    revenue_trend = _compute_growth_trend(reports, "revenue")
    profit_trend = _compute_growth_trend(reports, "net_profit")

    # ── DEPRECATED: 3-year CAGR (kept for backward compat only) ──
    revenue_growth_3y = None
    rev_values = [(r.get("period", ""), r.get("revenue")) for r in reports if r.get("revenue")]
    if len(rev_values) >= 12:
        annual = [v for p, v in rev_values if p.endswith("-12-31") or p.endswith("-03-31")]
        if len(annual) >= 3:
            start_val = annual[-4] if len(annual) >= 4 else annual[0]
            end_val = annual[-1]
            if start_val is not None and end_val is not None:
                revenue_growth_3y = _compute_cagr(float(start_val), float(end_val), 3.0)
        elif len(rev_values) >= 12:
            s = rev_values[-12][1]
            e = rev_values[-1][1]
            if s is not None and e is not None:
                revenue_growth_3y = _compute_cagr(float(s), float(e), 3.0)

    net_profit_growth_3y = None
    profit_vals = [
        (r.get("period", ""), r.get("net_profit")) for r in reports if r.get("net_profit")
    ]
    if len(profit_vals) >= 12:
        s = profit_vals[-12][1]
        e = profit_vals[-1][1]
        if s is not None and e is not None:
            net_profit_growth_3y = _compute_cagr(float(s), float(e), 3.0)

    # Cash flow quality
    cash_flow_quality = "unknown"
    if cf_per_share is not None and eps is not None and eps > 0:
        r = cf_per_share / eps
        if r >= 0.8:
            cash_flow_quality = "strong"
        elif r >= 0.3:
            cash_flow_quality = "adequate"
        else:
            cash_flow_quality = "weak"

    # ── NEW: Build trend-aware signals ──
    # Map old ROE trend values to new trend enum
    roe_trend_mapped = {
        "up": "improving",
        "down": "deteriorating",
        "flat": "stable",
        "unknown": "unknown",
    }.get(roe_trend_old, roe_trend_old)

    signals, signal_map = _build_signals(
        reports=reports,
        latest=latest,
        pe=pe,
        pe_percentile=pe_percentile,
        pb=pb,
        pb_percentile=pb_percentile,
        roe_trend=roe_trend_mapped,
        revenue_trend=revenue_trend,
        profit_trend=profit_trend,
        cash_flow_quality=cash_flow_quality,
        debt_ratio=debt_ratio,
        net_margin=net_margin,
        eps=eps,
        bps=bps,
    )

    return {
        # ── DEPRECATED: old flat fields (kept for backward compat, remove after 2027-06) ──
        "ticker": ticker,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "pe": pe,
        "pe_percentile": pe_percentile,
        "pb": pb,
        "pb_percentile": pb_percentile,
        "roe": roe,
        "roe_trend": roe_trend_old,
        "revenue_growth_3y": revenue_growth_3y,
        "net_profit_growth_3y": net_profit_growth_3y,
        "cash_flow_quality": cash_flow_quality,
        "debt_ratio": debt_ratio,
        "net_margin": net_margin,
        "eps": eps,
        "bps": bps,
        # ── NEW: trend-aware signals ──
        "signals": signals,
        "signal_map": signal_map,
        "valuation_narrative": "",  # filled by enrich_signals_with_llm
        # ── Growth trend metadata (for debugging/display) ──
        "_revenue_trend": revenue_trend,
        "_profit_trend": profit_trend,
    }


# ── LLM enrichment (P0: narrative, P1: per-signal summary) ──────────────────


def _build_summary_prompt(
    key: str, label: str, value: float | None, unit: str, trend: str, category: str
) -> str:
    """Build a one-shot prompt for LLM to write a signal summary."""
    val_str = f"{value}{unit}" if value is not None else "N/A"
    return (
        f"用一句中文解读以下财务指标（15字以内，客观准确）：\n"
        f"指标: {label}，当前值: {val_str}，趋势: {trend}，分类: {category}\n"
        f"只返回解读文本，不要其他内容。"
    )


def _build_narrative_prompt(ticker: str, signals: list[ValuationSignal]) -> str:
    """Build a prompt for LLM to write a valuation narrative."""
    signal_lines = []
    for s in signals[:5]:  # top 5 by strength
        val_str = f"{s.value}{s.unit}" if s.value is not None else "N/A"
        signal_lines.append(
            f"- {s.label}({s.category}): {val_str}, 趋势={s.trend}, 方向={s.direction}"
        )
    signal_text = "\n".join(signal_lines)

    return (
        f"你是一位资深投资分析师。根据以下估值指标，写一段3-5句的综合判断（中文），"
        f"指出关键信号、矛盾之处（如有）、以及需要关注的风险点。\n\n"
        f"股票: {ticker}\n"
        f"核心指标（按信号强度排序）:\n{signal_text}\n\n"
        f"要求:\n"
        f"1. 如果存在矛盾信号（如低估+恶化），明确指出可能是价值陷阱\n"
        f"2. 如果增长趋势改善，指出拐点和连续改善季数\n"
        f"3. 客观、简洁，3-5句话\n"
        f"只返回分析文本，不要其他格式。"
    )


def _fallback_summary(key: str, trend: str, value: float | None, unit: str) -> str:
    """Rule-based fallback when LLM is unavailable."""
    if value is None:
        return f"{key}: 数据缺失"
    templates: dict[str, str] = {
        "extreme_low": f"处于历史极端低位（{value}{unit}）",
        "extreme_high": f"处于历史极端高位（{value}{unit}）",
        "recovering": f"拐点确认，最新 {value}{unit}，趋势改善",
        "accelerating": f"加速增长，最新 {value}{unit}",
        "decelerating": f"增速放缓，最新 {value}{unit}",
        "declining": f"持续下滑，最新 {value}{unit}",
        "stable": f"保持稳定，当前 {value}{unit}",
        "improving": f"边际改善，当前 {value}{unit}",
        "deteriorating": f"边际恶化，当前 {value}{unit}",
    }
    return templates.get(trend, f"当前 {value}{unit}")


def _fallback_narrative(ticker: str, signals: list[ValuationSignal]) -> str:
    """Fallback narrative from top-3 signal summaries."""
    top3 = signals[:3]
    parts = [f"{ticker}估值关键信号："]
    for s in top3:
        parts.append(f"- {s.label}: {s.summary}")
    return "\n".join(parts)


def enrich_signals_with_llm(
    result: dict,
    backend=None,
    timeout: float = 30.0,
) -> dict:
    """Enrich valuation result with LLM-generated summaries and narrative.

    Parameters
    ----------
    result : dict
        Output from compute_valuation().
    backend : LLMBackend | None
        LLM backend implementing .complete(prompt) -> str.
        If None, uses rule-based fallbacks.
    timeout : float
        Seconds per LLM call (unused currently, reserved for future).

    Returns
    -------
    dict
        Same dict with signals[*].summary and valuation_narrative filled.
    """
    signals: list[ValuationSignal] = result.get("signals", [])
    if not signals:
        result["valuation_narrative"] = "无财务数据"
        return result

    ticker = result.get("ticker", "?")

    # ── P1: Individual signal summaries ──
    for s in signals:
        if backend is not None:
            try:
                prompt = _build_summary_prompt(
                    s.key,
                    s.label,
                    s.value,
                    s.unit,
                    s.trend,
                    s.category,
                )
                s.summary = backend.complete(prompt).strip()
            except Exception:
                s.summary = _fallback_summary(s.key, s.trend, s.value, s.unit)
        else:
            s.summary = _fallback_summary(s.key, s.trend, s.value, s.unit)

    # ── P0: Valuation narrative ──
    if backend is not None:
        try:
            prompt = _build_narrative_prompt(ticker, signals)
            result["valuation_narrative"] = backend.complete(prompt).strip()
        except Exception:
            result["valuation_narrative"] = _fallback_narrative(ticker, signals)
    else:
        result["valuation_narrative"] = _fallback_narrative(ticker, signals)

    return result
