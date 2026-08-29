"""Cross-source market data consensus helpers."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class SourceObservation:
    provider: str
    field: str
    value: float | str | None
    observed_at: str
    stale: bool = False
    error: str | None = None
    raw: dict | None = None


@dataclass(frozen=True)
class FieldConsensus:
    field: str
    value: float | str | None
    confidence: float
    sources: list[str]
    disagreement: float | None = None
    warnings: list[str] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class ConsensusResult:
    ticker: str
    kind: str
    fields: dict[str, FieldConsensus]
    provider_health: dict[str, str]
    generated_at: str


_PRICE_THRESHOLDS = {
    "price": (0.003, 0.01),
    "close": (0.005, 0.01),
}
_ABS_THRESHOLDS = {
    "change_pct": (0.2, 1.0),
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_ticker(ticker: str) -> str:
    text = ticker.strip().upper()
    text = text.removeprefix("SH").removeprefix("SZ").removeprefix("BJ")
    if "." in text:
        text = text.split(".")[0]
    if not text.isdigit() or len(text) != 6:
        raise ValueError(f"normalized ticker must be exactly 6 digits, got {text!r}")
    return text


def normalize_percent(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace("%", "")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: float | str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return None


def _relative_gap(values: list[float]) -> float:
    if not values:
        return 0.0
    baseline = min(abs(v) for v in values if v != 0) if any(v != 0 for v in values) else 1.0
    return (max(values) - min(values)) / baseline


def _absolute_gap(values: list[float]) -> float:
    if not values:
        return 0.0
    return max(values) - min(values)


def consensus_field(field: str, observations: list[SourceObservation]) -> FieldConsensus:
    valid = [obs for obs in observations if obs.error is None and obs.value is not None]
    warnings: list[str] = []
    sources = [obs.provider for obs in valid]

    if not valid:
        return FieldConsensus(
            field=field, value=None, confidence=0.0, sources=[], warnings=["no_valid_source"]
        )

    if len(valid) == 1:
        if valid[0].stale:
            warnings.append("stale_source")
        warnings.append("single_source")
        return FieldConsensus(
            field=field, value=valid[0].value, confidence=0.55, sources=sources, warnings=warnings
        )

    numeric_values_raw = [_as_float(obs.value) for obs in valid]
    numeric_values: list[float] = [v for v in numeric_values_raw if v is not None]
    if len(numeric_values) != len(valid):
        return FieldConsensus(
            field=field,
            value=valid[0].value,
            confidence=0.70,
            sources=sources,
            warnings=["non_numeric_consensus"],
        )

    if field in _ABS_THRESHOLDS:
        good, bad = _ABS_THRESHOLDS[field]
        gap = _absolute_gap(numeric_values)
    else:
        good, bad = _PRICE_THRESHOLDS.get(field, (0.01, 0.03))
        gap = _relative_gap(numeric_values)

    if any(obs.stale for obs in valid):
        warnings.append("stale_source")

    if gap <= good:
        confidence = 0.90 if not warnings else 0.80
    elif gap <= bad:
        confidence = 0.70
        warnings.append(f"disagreement:{gap:.4f}")
    else:
        confidence = 0.45
        warnings.append(f"disagreement:{gap:.4f}")

    return FieldConsensus(
        field=field,
        value=valid[0].value,
        confidence=confidence,
        sources=sources,
        disagreement=gap,
        warnings=warnings,
    )


class MarketConsensusService:
    """Collect provider observations and compute field-level consensus."""

    def __init__(self, providers: list[Any]):
        self._providers = sorted(providers, key=lambda p: getattr(p, "priority", 999))

    def validate_quote(self, ticker: str, *, min_sources: int = 2) -> ConsensusResult:
        ticker = normalize_ticker(ticker)
        observations: dict[str, list[SourceObservation]] = {
            "price": [],
            "change_pct": [],
            "volume": [],
            "turnover": [],
        }
        health: dict[str, str] = {}

        for provider in self._providers:
            if self._valid_source_count(observations["price"]) >= min_sources:
                break
            name = getattr(provider, "name", provider.__class__.__name__)
            try:
                quote = provider.get_quote(ticker)
                health[name] = "ok"
                raw = quote.__dict__ if hasattr(quote, "__dict__") else {}
                for field_name in observations:
                    observations[field_name].append(
                        SourceObservation(
                            provider=name,
                            field=field_name,
                            value=getattr(quote, field_name, None),
                            observed_at=utc_now_iso(),
                            raw=raw,
                        )
                    )
            except Exception as exc:
                health[name] = f"error:{exc}"
                observations["price"].append(
                    SourceObservation(
                        provider=name,
                        field="price",
                        value=None,
                        observed_at=utc_now_iso(),
                        error=str(exc),
                    )
                )

        fields = {name: consensus_field(name, values) for name, values in observations.items()}
        return ConsensusResult(
            ticker=ticker,
            kind="quote",
            fields=fields,
            provider_health=health,
            generated_at=utc_now_iso(),
        )

    def validate_history(
        self, ticker: str, *, days: int = 5, min_sources: int = 2
    ) -> ConsensusResult:
        ticker = normalize_ticker(ticker)
        observations: dict[str, list[SourceObservation]] = {"close": [], "volume": []}
        health: dict[str, str] = {}

        for provider in self._providers:
            if self._valid_source_count(observations["close"]) >= min_sources:
                break
            name = getattr(provider, "name", provider.__class__.__name__)
            try:
                rows = provider.get_history(ticker, days=days)
                if not rows:
                    raise ValueError("empty history")
                latest = rows[-1]
                health[name] = "ok"
                raw = latest.__dict__ if hasattr(latest, "__dict__") else {}
                observations["close"].append(
                    SourceObservation(name, "close", latest.close, utc_now_iso(), raw=raw)
                )
                observations["volume"].append(
                    SourceObservation(name, "volume", latest.volume, utc_now_iso(), raw=raw)
                )
            except Exception as exc:
                health[name] = f"error:{exc}"
                observations["close"].append(
                    SourceObservation(name, "close", None, utc_now_iso(), error=str(exc))
                )

        fields = {name: consensus_field(name, values) for name, values in observations.items()}
        return ConsensusResult(
            ticker=ticker,
            kind="history",
            fields=fields,
            provider_health=health,
            generated_at=utc_now_iso(),
        )

    def sample_health(self, tickers: list[str]) -> dict[str, Any]:
        results = [self.validate_quote(ticker) for ticker in tickers]
        return {
            "tickers": len(tickers),
            "low_confidence": [r.ticker for r in results if r.fields["price"].confidence < 0.65],
            "provider_health": [r.provider_health for r in results],
        }

    @staticmethod
    def _valid_source_count(observations: list[SourceObservation]) -> int:
        return sum(1 for obs in observations if obs.error is None and obs.value is not None)
