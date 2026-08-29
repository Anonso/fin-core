"""Typed A-share margin evidence without a technical score or trading authority."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from fin_analyse.portfolio.actual_advisory import (
    ActualAdvisoryPortfolioRead,
    ActualAdvisoryPortfolioSnapshot,
    ActualAdvisoryPortfolioStore,
    actual_advisory_snapshot_ref,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_EXPECTED_VENUES = ("SH", "SZ")
_METRICS = ("financing_balance", "short_balance", "total_balance")
_RELATIVE_ARTIFACT_ROOT = Path("fin-analyse/margin-evidence-v1")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _ActualPortfolioReader(Protocol):
    def read(self) -> ActualAdvisoryPortfolioRead: ...


@dataclass(frozen=True, slots=True)
class MarginEvidenceRequest:
    """One bounded, read-only margin-evidence scope."""

    instruments: tuple[str, ...]
    as_of: datetime
    deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MarginObservation:
    """One completed, source-native margin record for a market or stock."""

    trade_date: date
    financing_balance: Decimal
    short_balance: Decimal
    total_balance: Decimal
    source_id: str
    source_revision: str
    captured_at: datetime
    free_float_market_cap: Decimal | None = None
    turnover: Decimal | None = None
    close: Decimal | None = None
    volume: Decimal | None = None
    denominator_trade_date: date | None = None
    denominator_source_id: str | None = None
    denominator_source_revision: str | None = None
    denominator_captured_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MarginEvidenceSourceResult:
    """External-source records before FIN computes trends and safe projections."""

    markets: Mapping[str, tuple[MarginObservation, ...]]
    instruments: Mapping[str, tuple[MarginObservation, ...]]
    data_gaps: tuple[str, ...] = ()


class _MarginEvidenceSource(Protocol):
    def read(self, request: MarginEvidenceRequest) -> MarginEvidenceSourceResult: ...


@dataclass(frozen=True, slots=True)
class MarginEvidence:
    status: str
    as_of: datetime
    account: dict[str, object]
    valid_until: datetime | None = None
    markets: tuple[dict[str, object], ...] = ()
    instruments: tuple[dict[str, object], ...] = ()
    data_gaps: tuple[str, ...] = ()

    def to_agent_dict(self) -> dict[str, object]:
        return {
            "schema_version": "fin.margin-evidence/v1",
            "source_boundary": "a_share_margin_evidence",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "status": self.status,
            "as_of": self.as_of.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until is not None else None,
            "account": self.account,
            "markets": list(self.markets),
            "instruments": list(self.instruments),
            "data_gaps": list(self.data_gaps),
        }


class MarginEvidenceReader(Protocol):
    def read(self, request: MarginEvidenceRequest) -> MarginEvidence: ...


class MarginEvidenceService:
    """Return account, market, and stock margin facts through one deep read seam."""

    def __init__(
        self,
        *,
        account_reader: _ActualPortfolioReader,
        source: _MarginEvidenceSource | None = None,
    ) -> None:
        self._account_reader = account_reader
        self._source = source

    def read(self, request: MarginEvidenceRequest) -> MarginEvidence:
        _validate_request(request)
        account_read = self._account_reader.read()
        account = _account_projection(account_read)
        markets: tuple[dict[str, object], ...] = ()
        instruments: tuple[dict[str, object], ...] = ()
        source_gaps: tuple[str, ...] = ()
        if self._source is None:
            source_gaps = ("MARGIN_EVIDENCE_SOURCE_UNAVAILABLE",)
        else:
            try:
                source_result = self._source.read(request)
            except Exception:
                source_gaps = ("MARGIN_EVIDENCE_SOURCE_UNAVAILABLE",)
            else:
                if not isinstance(source_result, MarginEvidenceSourceResult):
                    source_gaps = ("MARGIN_EVIDENCE_SOURCE_RESULT_INVALID",)
                else:
                    markets, market_gaps = _market_projection(source_result, request=request)
                    instruments, instrument_gaps = _instrument_projection(
                        source_result,
                        request=request,
                    )
                    source_gaps = tuple(
                        dict.fromkeys((*source_result.data_gaps, *market_gaps, *instrument_gaps))
                    )
        gaps = tuple(
            dict.fromkeys(
                (
                    *_string_values(account.get("data_gaps")),
                    *source_gaps,
                    "MARGIN_EVIDENCE_BJ_NOT_COVERED",
                )
            )
        )
        return MarginEvidence(
            status=_overall_status(
                account_status=str(account["status"]),
                markets=markets,
                instruments=instruments,
                expected_instruments=request.instruments,
                has_source_gaps=bool(source_gaps),
            ),
            as_of=request.as_of,
            account=account,
            markets=markets,
            instruments=instruments,
            data_gaps=gaps,
        )


def build_default_margin_evidence(
    *,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MarginEvidenceService:
    """Build the production reader without creating state until the first read."""

    effective_clock = clock or (lambda: datetime.now(UTC))
    environment = os.environ if environ is None else environ
    try:
        from fin_analyse.margin.eastmoney import EastmoneyMarginEvidenceSource
        from fin_analyse.market.data_qualification import ObservationEvidenceOrigin

        source: _MarginEvidenceSource | None = EastmoneyMarginEvidenceSource(
            artifact_root=margin_evidence_artifact_root(environ=environment),
            evidence_origin=ObservationEvidenceOrigin.LIVE_CAPTURE,
            clock=effective_clock,
        )
    except (OSError, ValueError):
        source = None
    return MarginEvidenceService(
        account_reader=ActualAdvisoryPortfolioStore(
            environ=environment,
            clock=effective_clock,
        ),
        source=source,
    )


def margin_evidence_artifact_root(
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    configured = environment.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    root = (base / _RELATIVE_ARTIFACT_ROOT).resolve()
    project = _PROJECT_ROOT.resolve()
    if (
        not root.is_absolute()
        or root == project
        or project in root.parents
        or root in project.parents
    ):
        raise ValueError("margin evidence artifact root must be outside the checkout")
    return root


def _market_projection(
    source: MarginEvidenceSourceResult,
    *,
    request: MarginEvidenceRequest,
) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
    markets: list[dict[str, object]] = []
    gaps: list[str] = []
    for venue in _EXPECTED_VENUES:
        projection, item_gaps, _ = _series_projection(
            source.markets.get(venue, ()),
            as_of=request.as_of,
            unavailable_gap=f"MARGIN_EVIDENCE_{venue}_UNAVAILABLE",
        )
        scoped_source_gaps = _scoped_source_gaps(source.data_gaps, venue)
        item_data_gaps = tuple(dict.fromkeys((*item_gaps, *scoped_source_gaps)))
        projection = {
            **projection,
            "status": _status_from_gaps(projection["status"], item_data_gaps),
            "data_gaps": list(item_data_gaps),
        }
        markets.append({"venue": venue, **projection})
        gaps.extend(f"{gap}:{venue}" for gap in item_gaps)
    gaps.append("MARGIN_EVIDENCE_BJ_NOT_COVERED")
    return tuple(markets), tuple(dict.fromkeys(gaps))


def _instrument_projection(
    source: MarginEvidenceSourceResult,
    *,
    request: MarginEvidenceRequest,
) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
    instruments: list[dict[str, object]] = []
    gaps: list[str] = []
    for symbol in request.instruments:
        if symbol.endswith(".BJ"):
            instruments.append(_unknown_instrument(symbol, "MARGIN_EVIDENCE_BJ_NOT_COVERED"))
            gaps.append(f"MARGIN_EVIDENCE_BJ_NOT_COVERED:{symbol}")
            continue
        projection, item_gaps, points = _series_projection(
            source.instruments.get(symbol, ()),
            as_of=request.as_of,
            unavailable_gap="MARGIN_EVIDENCE_STOCK_UNAVAILABLE",
        )
        latest = points[-1] if points else None
        denominators, denominator_gaps = _denominator_projection(latest)
        relationship, relationship_gaps = _price_volume_relationship(points)
        generated_gaps = (*item_gaps, *denominator_gaps, *relationship_gaps)
        item_gaps = (*generated_gaps, *_scoped_source_gaps(source.data_gaps, symbol))
        status = _status_from_gaps(projection["status"], item_gaps)
        instruments.append(
            {
                "symbol": symbol,
                **projection,
                "status": status,
                "denominators": denominators,
                "price_volume_relationship": relationship,
                "data_gaps": list(dict.fromkeys(item_gaps)),
            }
        )
        gaps.extend(f"{gap}:{symbol}" for gap in generated_gaps)
    return tuple(instruments), tuple(dict.fromkeys(gaps))


def _series_projection(
    raw_points: object,
    *,
    as_of: datetime,
    unavailable_gap: str,
) -> tuple[dict[str, object], tuple[str, ...], tuple[MarginObservation, ...]]:
    cutoff = as_of.astimezone(_SHANGHAI).date()
    if not isinstance(raw_points, tuple):
        return _unknown_series(unavailable_gap)
    points = tuple(
        sorted(
            (
                point
                for point in raw_points
                if _valid_observation(point) and point.trade_date < cutoff
            ),
            key=lambda point: point.trade_date,
        )
    )
    if not points or len({point.trade_date for point in points}) != len(points):
        return _unknown_series(unavailable_gap)
    latest = points[-1]
    gaps: list[str] = []
    for window in (1, 5, 20):
        if len(points) <= window:
            gaps.append(f"MARGIN_EVIDENCE_{window}D_HISTORY_INCOMPLETE")
    if _one_year_values(points, metric="total_balance") is None:
        gaps.append("MARGIN_EVIDENCE_ONE_YEAR_COVERAGE_INCOMPLETE")
    status = "READY" if not gaps else "PARTIAL"
    return (
        {
            "status": status,
            "latest": {
                "trade_date": latest.trade_date.isoformat(),
                "financing_balance": _decimal_text(latest.financing_balance),
                "short_balance": _decimal_text(latest.short_balance),
                "total_balance": _decimal_text(latest.total_balance),
            },
            "trends": {metric: _metric_trend(points, metric=metric) for metric in _METRICS},
            "source": {
                "provider": latest.source_id,
                "revision": latest.source_revision,
                "captured_at": latest.captured_at.isoformat(),
            },
            "data_gaps": list(gaps),
        },
        tuple(gaps),
        points,
    )


def _unknown_series(
    gap: str,
) -> tuple[dict[str, object], tuple[str, ...], tuple[MarginObservation, ...]]:
    return (
        {
            "status": "UNKNOWN",
            "latest": None,
            "trends": {},
            "source": None,
            "data_gaps": [gap],
        },
        (gap,),
        (),
    )


def _unknown_instrument(symbol: str, gap: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "status": "UNKNOWN",
        "latest": None,
        "trends": {},
        "source": None,
        "denominators": {
            "trade_date": None,
            "free_float_market_cap": None,
            "turnover": None,
            "margin_to_free_float_market_cap": None,
            "margin_to_turnover": None,
            "source": None,
        },
        "price_volume_relationship": {
            "window": "5d",
            "margin_balance_change": None,
            "price_change": None,
            "turnover_change": None,
            "volume_change": None,
            "relationship": "UNKNOWN",
        },
        "data_gaps": [gap],
    }


def _metric_trend(
    points: tuple[MarginObservation, ...],
    *,
    metric: str,
) -> dict[str, object]:
    latest = _metric(points[-1], metric)
    changes: dict[str, object] = {}
    for window in (1, 5, 20):
        changes[f"{window}d"] = (
            _change(_metric(points[-1 - window], metric), latest) if len(points) > window else None
        )
    values = _one_year_values(points, metric=metric)
    percentile = _percentile(latest, values) if values is not None else None
    return {
        "changes": changes,
        "one_year_level_percentile": _decimal_text(percentile),
    }


def _denominator_projection(
    latest: MarginObservation | None,
) -> tuple[dict[str, object], tuple[str, ...]]:
    empty: dict[str, object] = {
        "trade_date": None,
        "free_float_market_cap": None,
        "turnover": None,
        "margin_to_free_float_market_cap": None,
        "margin_to_turnover": None,
        "source": None,
    }
    if latest is None:
        return empty, ("MARGIN_EVIDENCE_STOCK_DENOMINATORS_UNAVAILABLE",)
    valid = (
        latest.denominator_trade_date == latest.trade_date
        and latest.free_float_market_cap is not None
        and latest.free_float_market_cap > 0
        and latest.turnover is not None
        and latest.turnover > 0
        and bool(latest.denominator_source_id)
        and bool(latest.denominator_source_revision)
        and latest.denominator_captured_at is not None
        and latest.denominator_captured_at.tzinfo is not None
        and latest.denominator_captured_at.utcoffset() is not None
    )
    if not valid:
        return empty, ("MARGIN_EVIDENCE_STOCK_DENOMINATORS_UNAVAILABLE",)
    assert latest.free_float_market_cap is not None
    assert latest.turnover is not None
    assert latest.denominator_trade_date is not None
    assert latest.denominator_captured_at is not None
    return (
        {
            "trade_date": latest.denominator_trade_date.isoformat(),
            "free_float_market_cap": _decimal_text(latest.free_float_market_cap),
            "turnover": _decimal_text(latest.turnover),
            "margin_to_free_float_market_cap": _decimal_text(
                latest.financing_balance / latest.free_float_market_cap
            ),
            "margin_to_turnover": _decimal_text(latest.financing_balance / latest.turnover),
            "source": {
                "provider": latest.denominator_source_id,
                "revision": latest.denominator_source_revision,
                "captured_at": latest.denominator_captured_at.isoformat(),
            },
        },
        (),
    )


def _price_volume_relationship(
    points: tuple[MarginObservation, ...],
) -> tuple[dict[str, object], tuple[str, ...]]:
    unknown: dict[str, object] = {
        "window": "5d",
        "margin_balance_change": None,
        "price_change": None,
        "turnover_change": None,
        "volume_change": None,
        "relationship": "UNKNOWN",
    }
    if len(points) <= 5:
        return unknown, ("MARGIN_EVIDENCE_PRICE_VOLUME_HISTORY_INCOMPLETE",)
    earlier, latest = points[-6], points[-1]
    values = (
        earlier.close,
        latest.close,
        earlier.turnover,
        latest.turnover,
        earlier.volume,
        latest.volume,
    )
    if any(value is None for value in values):
        return unknown, ("MARGIN_EVIDENCE_PRICE_VOLUME_UNAVAILABLE",)
    assert earlier.close is not None and latest.close is not None
    assert earlier.turnover is not None and latest.turnover is not None
    assert earlier.volume is not None and latest.volume is not None
    margin_change = _change(earlier.financing_balance, latest.financing_balance)
    price_change = _change(earlier.close, latest.close)
    turnover_change = _change(earlier.turnover, latest.turnover)
    volume_change = _change(earlier.volume, latest.volume)
    assert margin_change is not None and price_change is not None
    margin_absolute = latest.financing_balance - earlier.financing_balance
    price_absolute = latest.close - earlier.close
    relationship = (
        "SAME_DIRECTION"
        if margin_absolute * price_absolute > 0
        else "OPPOSITE_DIRECTION"
        if margin_absolute * price_absolute < 0
        else "FLAT_OR_UNCLEAR"
    )
    return (
        {
            "window": "5d",
            "margin_balance_change": margin_change,
            "price_change": price_change,
            "turnover_change": turnover_change,
            "volume_change": volume_change,
            "relationship": relationship,
        },
        (),
    )


def _one_year_values(
    points: tuple[MarginObservation, ...],
    *,
    metric: str,
) -> tuple[Decimal, ...] | None:
    latest_date = points[-1].trade_date
    first_required_date = latest_date - timedelta(days=365)
    values = tuple(
        _metric(point, metric) for point in points if point.trade_date >= first_required_date
    )
    if not values or points[0].trade_date > first_required_date:
        return None
    return values


def _percentile(value: Decimal, population: tuple[Decimal, ...]) -> Decimal:
    return (
        Decimal(sum(item <= value for item in population))
        * Decimal("100")
        / Decimal(len(population))
    ).quantize(Decimal("0.01"))


def _change(before: Decimal, after: Decimal) -> dict[str, str | None]:
    percent = (
        ((after - before) * Decimal("100") / abs(before)).quantize(Decimal("0.01"))
        if before != 0
        else None
    )
    return {
        "absolute": _decimal_text(after - before),
        "percent": _decimal_text(percent),
    }


def _metric(point: MarginObservation, metric: str) -> Decimal:
    if metric == "financing_balance":
        return point.financing_balance
    if metric == "short_balance":
        return point.short_balance
    if metric == "total_balance":
        return point.total_balance
    raise ValueError("margin metric is invalid")


def _valid_observation(point: object) -> bool:
    return (
        isinstance(point, MarginObservation)
        and point.captured_at.tzinfo is not None
        and point.captured_at.utcoffset() is not None
        and all(
            isinstance(value, Decimal) and value >= 0
            for value in (
                point.financing_balance,
                point.short_balance,
                point.total_balance,
            )
        )
        and bool(point.source_id)
        and bool(point.source_revision)
    )


def _scoped_source_gaps(gaps: Sequence[object], scope: str) -> tuple[str, ...]:
    suffix = f":{scope}"
    return tuple(
        dict.fromkeys(gap for gap in gaps if isinstance(gap, str) and gap.endswith(suffix))
    )


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _status_from_gaps(status: object, gaps: Sequence[str]) -> str:
    if status == "UNKNOWN":
        return "UNKNOWN"
    return "READY" if not gaps else "PARTIAL"


def _overall_status(
    *,
    account_status: str,
    markets: tuple[dict[str, object], ...],
    instruments: tuple[dict[str, object], ...],
    expected_instruments: tuple[str, ...],
    has_source_gaps: bool,
) -> str:
    status_by_venue = {item.get("venue"): item.get("status") for item in markets}
    status_by_symbol = {item.get("symbol"): item.get("status") for item in instruments}
    statuses = [account_status]
    statuses.extend(str(status_by_venue.get(venue, "UNKNOWN")) for venue in _EXPECTED_VENUES)
    statuses.extend(str(status_by_symbol.get(symbol, "UNKNOWN")) for symbol in expected_instruments)
    if statuses and all(status == "READY" for status in statuses) and not has_source_gaps:
        return "READY"
    if any(status in {"READY", "PARTIAL"} for status in statuses):
        return "PARTIAL"
    return "UNKNOWN"


def _validate_request(request: MarginEvidenceRequest) -> None:
    if not isinstance(request, MarginEvidenceRequest):
        raise TypeError("margin evidence request is invalid")
    if request.as_of.tzinfo is None or request.as_of.utcoffset() is None:
        raise ValueError("margin evidence as_of must be timezone-aware")
    if request.deadline_at is not None and (
        request.deadline_at.tzinfo is None or request.deadline_at.utcoffset() is None
    ):
        raise ValueError("margin evidence deadline must be timezone-aware")
    if len(request.instruments) > 5 or len(set(request.instruments)) != len(request.instruments):
        raise ValueError("margin evidence supports up to five distinct instruments")


def _account_projection(read: ActualAdvisoryPortfolioRead) -> dict[str, object]:
    snapshot = read.snapshot
    if snapshot is None:
        return {
            "status": "UNKNOWN",
            "account_snapshot_ref": None,
            "confirmed_at": None,
            "margin_debt": None,
            "net_assets": None,
            "leverage_ratio": None,
            "risk_increase_allowed": False,
            "data_gaps": [reason.value for reason in read.reason_codes],
        }
    return _snapshot_account_projection(
        snapshot, status=str(read.status), reasons=read.reason_codes
    )


def _snapshot_account_projection(
    snapshot: ActualAdvisoryPortfolioSnapshot,
    *,
    status: str,
    reasons: Sequence[object],
) -> dict[str, object]:
    margin_debt = snapshot.margin_debt
    net_assets = snapshot.net_assets
    ratio = (
        margin_debt / net_assets
        if margin_debt is not None and net_assets is not None and net_assets > 0
        else None
    )
    return {
        "status": status,
        "account_snapshot_ref": actual_advisory_snapshot_ref(snapshot.revision),
        "confirmed_at": snapshot.as_of.isoformat(),
        "margin_debt": _decimal_text(snapshot.margin_debt),
        "net_assets": _decimal_text(snapshot.net_assets),
        "leverage_ratio": _decimal_text(ratio),
        "risk_increase_allowed": (
            status == "READY" and ratio is not None and bool(snapshot.positions)
        ),
        "data_gaps": [getattr(reason, "value", str(reason)) for reason in reasons],
    }


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None
