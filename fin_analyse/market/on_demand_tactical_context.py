"""Fresh, account-neutral market facts for one on-demand A-share consultation.

This module deliberately stops at evidence.  It owns symbol normalization,
dual-source quote qualification, completed daily-bar reuse and deterministic
technical facts, but it never knows about accounts, orders or trading intent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from concurrent.futures import Future, wait
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

from fin_analyse.common.execution_control import BoundedExecutor, ExecutorCapacityError
from fin_analyse.market.data_qualification import (
    ObservationEvidenceOrigin,
    QualificationSample,
    QualificationSourceCapture,
    TradingStatus,
)
from fin_analyse.market.evidence_plan import (
    DEFAULT_MARKET_EVIDENCE_PLAN,
    MarketEvidenceDriver,
)
from fin_analyse.market.providers.base import OHLCV
from fin_analyse.market.qualification_sources.eastmoney_daily_bars import (
    _build_on_demand_eastmoney_daily_bar_reader,
)
from fin_analyse.market.qualification_sources.eastmoney_http_transport import (
    _build_eastmoney_on_demand_http_get,
    _EastmoneyOnDemandHttpGet,
)
from fin_analyse.market.qualification_sources.eastmoney_intraday_bars import (
    _build_on_demand_eastmoney_thirty_minute_bar_reader,
)
from fin_analyse.market.qualification_sources.eastmoney_raw import (
    _build_on_demand_eastmoney_raw_source,
)
from fin_analyse.market.qualification_sources.tencent_daily_bars import (
    _build_on_demand_tencent_daily_bar_reader,
)
from fin_analyse.market.qualification_sources.tencent_intraday_bars import (
    _build_on_demand_tencent_intraday_bar_reader,
)
from fin_analyse.market.qualification_sources.tencent_raw import TencentRawQualificationSource
from fin_analyse.market.qualified_daily_bars import (
    MINIMUM_COMPLETED_BARS,
    QualifiedDailyBarReader,
    QualifiedDailyBarReadRequest,
    QualifiedDailyBarSeries,
)
from fin_analyse.market.qualified_intraday_bars import (
    QualifiedThirtyMinuteBarReader,
    QualifiedThirtyMinuteBarReadRequest,
    QualifiedThirtyMinuteBarSeries,
)
from fin_analyse.market.technical import compute_all
from fin_analyse.market.trading_calendar import (
    AShareTradingCalendar,
    TradingSessionPhase,
    TradingSessionStatus,
)

TacticalEvidenceStatus = Literal["READY", "PARTIAL", "UNKNOWN"]

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CANONICAL_SYMBOL = re.compile(r"^(?P<code>[0-9]{6})\.(?P<venue>SH|SZ|BJ)$")
_MAX_SYMBOLS = 5
_QUOTE_MAX_AGE_SECONDS = Decimal("15")
_REFERENCE_QUOTE_MAX_AGE_SECONDS = Decimal(str(4 * 24 * 60 * 60))
_MAX_SOURCE_CLOCK_SKEW_SECONDS = Decimal("2")
_READY_DISAGREEMENT = Decimal("0.003")
_UNKNOWN_DISAGREEMENT = Decimal("0.01")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)
_RELATIVE_ARTIFACT_ROOT = Path("fin-analyse/on-demand-tactical-context-v1/daily-bars")
_RELATIVE_INTRADAY_ARTIFACT_ROOT = Path(
    "fin-analyse/on-demand-tactical-context-v1/thirty-minute-bars"
)
_MARKET_SYMBOL_EXECUTOR = BoundedExecutor(
    max_workers=5,
    max_outstanding=10,
    thread_name_prefix="fin-tactical-market",
)
# BUG-002（2026-08-28 诊断）：每个 symbol worker 最坏向 detail executor 提交
# 3 个任务（quote+日线+30分钟线），5 worker × 3 = 15 > 旧额度 10 → 盘前上游
# 慢时自挤占，标的被误标 ON_DEMAND_MARKET_CAPACITY_EXHAUSTED。额度必须
# 配平最坏嵌套需求（15）+ 单请求缓冲，见 docs/design/market-data.md。
_MARKET_DETAIL_EXECUTOR = BoundedExecutor(
    max_workers=10,
    max_outstanding=20,
    thread_name_prefix="fin-tactical-detail",
)
_MARKET_QUOTE_EXECUTOR = BoundedExecutor(
    max_workers=10,
    max_outstanding=20,
    thread_name_prefix="fin-tactical-quote",
)


class QualificationSourcePort(Protocol):
    source_id: str

    def capture(
        self,
        sample: QualificationSample,
        *,
        timeout_seconds: float | None = None,
    ) -> QualificationSourceCapture: ...


class TradingCalendarPort(Protocol):
    def session_at(self, at: datetime): ...

    def next_open_date(self, *, after, known_at: datetime): ...

    def previous_open_date(self, *, before, known_at: datetime): ...


@dataclass(frozen=True, slots=True)
class _UnavailableSession:
    status: TradingSessionStatus = TradingSessionStatus.UNKNOWN
    phase: TradingSessionPhase = TradingSessionPhase.UNKNOWN
    data_gaps: tuple[str, ...] = ("TRADING_CALENDAR_UNAVAILABLE",)


class _UnavailableTradingCalendar:
    def session_at(self, at: datetime) -> _UnavailableSession:
        return _UnavailableSession()

    def next_open_date(self, *, after, known_at: datetime):
        raise ValueError("TRADING_CALENDAR_UNAVAILABLE")

    def previous_open_date(self, *, before, known_at: datetime):
        raise ValueError("TRADING_CALENDAR_UNAVAILABLE")


class _UnavailableDailyBarReader:
    def read(self, request: QualifiedDailyBarReadRequest) -> QualifiedDailyBarSeries:
        raise ValueError("ON_DEMAND_DAILY_BAR_ROOT_UNAVAILABLE")


class _FallbackDailyBarReader:
    """Eastmoney qfq bars first, Tencent qfq bars when the primary fails."""

    def __init__(
        self,
        *,
        primary: QualifiedDailyBarReader,
        fallback: QualifiedDailyBarReader,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    def read(self, request: QualifiedDailyBarReadRequest) -> QualifiedDailyBarSeries:
        try:
            return self._primary.read(request)
        except Exception:
            return self._fallback.read(request)


class _FallbackIntradayBarReader:
    """Tencent 30-minute bars first, Eastmoney when the primary fails."""

    def __init__(
        self,
        *,
        primary: QualifiedThirtyMinuteBarReader,
        fallback: QualifiedThirtyMinuteBarReader,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    def read(
        self,
        request: QualifiedThirtyMinuteBarReadRequest,
    ) -> QualifiedThirtyMinuteBarSeries:
        try:
            return self._primary.read(request)
        except Exception:
            return self._fallback.read(request)


@dataclass(frozen=True, slots=True)
class OnDemandTacticalContextRequest:
    instruments: tuple[str, ...]
    as_of: datetime
    deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TacticalQuoteFact:
    source_id: str
    payload_sha256: str
    price: str
    source_event_at: datetime
    trading_status: Literal["trading", "suspended", "unknown"]
    upper_limit_price: str | None
    lower_limit_price: str | None
    volume: str | None = None
    turnover: str | None = None

    def to_agent_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "payload_sha256": self.payload_sha256,
            "price": self.price,
            "source_event_at": self.source_event_at.isoformat(),
            "trading_status": self.trading_status,
            "upper_limit_price": self.upper_limit_price,
            "lower_limit_price": self.lower_limit_price,
            "volume": self.volume,
            "turnover": self.turnover,
        }


@dataclass(frozen=True, slots=True)
class TacticalInstrumentContext:
    symbol: str
    status: TacticalEvidenceStatus
    evidence_id: str
    quote_price: str | None
    quote_price_role: Literal["PRIMARY", "REFERENCE_ONLY", "NONE"]
    quote_disagreement_ratio: str | None
    quote_facts: tuple[TacticalQuoteFact, ...]
    quote_observed_at: datetime | None
    session_phase: str
    reference_only: bool
    manual_review_eligible: bool
    latest_completed_bar_date: str | None
    completed_bar_count: int
    technical_facts: Mapping[str, object]
    provider_provenance: tuple[str, ...]
    data_gaps: tuple[str, ...]
    timeframes: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    observation_mode: Literal[
        "LIVE", "CLOSE_REFERENCE", "REFERENCE_ONLY", "UNAVAILABLE"
    ] = "REFERENCE_ONLY"
    context_limitations: tuple[str, ...] = ()

    def to_agent_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "evidence_id": self.evidence_id,
            "price": self.quote_price,
            "observation_mode": self.observation_mode,
            "context_limitations": list(self.context_limitations),
            "quote": {
                "qualified_price": self.quote_price,
                "qualified_volume": self.quote_facts[0].volume if self.quote_facts else None,
                "qualified_turnover": self.quote_facts[0].turnover if self.quote_facts else None,
                "price_role": self.quote_price_role,
                "disagreement_ratio": self.quote_disagreement_ratio,
                "sources": [fact.to_agent_dict() for fact in self.quote_facts],
                "observed_at": (
                    self.quote_observed_at.isoformat()
                    if self.quote_observed_at is not None
                    else None
                ),
            },
            "session_phase": self.session_phase,
            "reference_only": self.reference_only,
            "manual_review_eligible": self.manual_review_eligible,
            "daily_bars": {
                "latest_completed_bar_date": self.latest_completed_bar_date,
                "completed_bar_count": self.completed_bar_count,
                "technical_facts": dict(self.technical_facts),
            },
            "timeframes": {timeframe: dict(facts) for timeframe, facts in self.timeframes.items()},
            "provider_provenance": list(self.provider_provenance),
            "data_gaps": list(self.data_gaps),
        }


@dataclass(frozen=True, slots=True)
class OnDemandTacticalContext:
    status: TacticalEvidenceStatus
    as_of: datetime
    valid_until: datetime
    instruments: tuple[TacticalInstrumentContext, ...]
    session_phase: str
    data_gaps: tuple[str, ...] = ()
    context_limitations: tuple[str, ...] = ()

    def to_agent_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "as_of": self.as_of.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "session_phase": self.session_phase,
            "instruments": [item.to_agent_dict() for item in self.instruments],
            "data_gaps": list(self.data_gaps),
            "context_limitations": list(self.context_limitations),
        }


@dataclass(frozen=True, slots=True)
class TerminalQuoteCheck:
    symbol: str
    status: TacticalEvidenceStatus
    quote_price: str | None
    valid_until: datetime | None
    manual_review_eligible: bool
    data_gaps: tuple[str, ...]


class OnDemandTacticalContextReader(Protocol):
    def read(self, request: OnDemandTacticalContextRequest) -> OnDemandTacticalContext: ...

    def refresh_quotes(
        self,
        instruments: tuple[str, ...],
        *,
        as_of: datetime,
        deadline_at: datetime | None = None,
    ) -> tuple[TerminalQuoteCheck, ...]: ...


class OnDemandTacticalContextService:
    """Collect one bounded market context without leaking provider mechanics."""

    def __init__(
        self,
        *,
        primary_quote: QualificationSourcePort,
        reference_quote: QualificationSourcePort,
        daily_bars: QualifiedDailyBarReader,
        thirty_minute_bars: QualifiedThirtyMinuteBarReader | None = None,
        calendar: TradingCalendarPort,
        clock: Callable[[], datetime] | None = None,
        symbol_executor: BoundedExecutor | None = None,
        detail_executor: BoundedExecutor | None = None,
        quote_executor: BoundedExecutor | None = None,
    ) -> None:
        self._primary_quote = primary_quote
        self._reference_quote = reference_quote
        self._daily_bars = daily_bars
        self._thirty_minute_bars = thirty_minute_bars
        self._calendar = calendar
        self._clock = clock or (lambda: datetime.now(UTC))
        self._symbol_executor = symbol_executor or _MARKET_SYMBOL_EXECUTOR
        self._detail_executor = detail_executor or _MARKET_DETAIL_EXECUTOR
        self._quote_executor = quote_executor or _MARKET_QUOTE_EXECUTOR

    def read(self, request: OnDemandTacticalContextRequest) -> OnDemandTacticalContext:
        symbols = _validate_request(request)
        session = self._calendar.session_at(request.as_of)
        phase = str(session.phase.value)
        session_gaps = tuple(getattr(session, "data_gaps", ()))
        if _deadline_reached(request.deadline_at, self._clock):
            return _deadline_context(request.as_of, phase, symbols)

        results: dict[str, TacticalInstrumentContext] = {}
        futures: dict[Future[TacticalInstrumentContext], str] = {}
        for symbol in symbols:
            try:
                future = self._symbol_executor.submit(
                    self._collect_symbol,
                    symbol,
                    as_of=request.as_of,
                    phase=phase,
                    session_open=session.status is TradingSessionStatus.OPEN,
                    session_gaps=session_gaps,
                    deadline_at=request.deadline_at,
                )
            except ExecutorCapacityError:
                results[symbol] = _unknown_symbol(
                    symbol,
                    phase=phase,
                    gap="ON_DEMAND_MARKET_CAPACITY_EXHAUSTED",
                )
            else:
                futures[future] = symbol
        if futures:
            done, pending = wait(
                tuple(futures),
                timeout=_deadline_wait_seconds(request.deadline_at, self._clock),
            )
            for future in done:
                symbol = futures[future]
                try:
                    results[symbol] = future.result()
                except Exception:
                    results[symbol] = _unknown_symbol(
                        symbol,
                        phase=phase,
                        gap="ON_DEMAND_MARKET_SYMBOL_UNAVAILABLE",
                    )
            for future in pending:
                symbol = futures[future]
                future.cancel()
                results[symbol] = _unknown_symbol(
                    symbol,
                    phase=phase,
                    gap="CONSULTATION_DEADLINE_REACHED",
                )
        ordered = tuple(results[symbol] for symbol in symbols)
        overall = _aggregate_status(item.status for item in ordered)
        if session_gaps and overall == "READY":
            overall = "PARTIAL"
        gaps = tuple(
            dict.fromkeys((*session_gaps, *(gap for item in ordered for gap in item.data_gaps)))
        )
        context_limitations = tuple(
            dict.fromkeys(
                limitation
                for item in ordered
                for limitation in item.context_limitations
            )
        )
        evidence_as_of = max(
            (item.quote_observed_at or request.as_of for item in ordered),
            default=request.as_of,
        )
        evidence_session = self._calendar.session_at(evidence_as_of)
        valid_until = _context_valid_until(
            self._calendar,
            evidence_session,
            as_of=evidence_as_of,
        )
        if _deadline_reached(request.deadline_at, self._clock):
            return _deadline_context(request.as_of, phase, symbols)
        return OnDemandTacticalContext(
            status=overall,
            as_of=evidence_as_of,
            valid_until=valid_until,
            instruments=ordered,
            session_phase=phase,
            data_gaps=gaps,
            context_limitations=context_limitations,
        )

    def refresh_quotes(
        self,
        instruments: tuple[str, ...],
        *,
        as_of: datetime,
        deadline_at: datetime | None = None,
    ) -> tuple[TerminalQuoteCheck, ...]:
        request = OnDemandTacticalContextRequest(
            instruments=instruments,
            as_of=as_of,
            deadline_at=deadline_at,
        )
        symbols = _validate_request(request)
        session = self._calendar.session_at(as_of)
        session_gaps = tuple(getattr(session, "data_gaps", ()))
        if _deadline_reached(deadline_at, self._clock):
            return tuple(_deadline_quote_check(symbol) for symbol in symbols)
        results: dict[str, TerminalQuoteCheck] = {}
        futures: dict[Future[_QualifiedQuotes], str] = {}
        for symbol in symbols:
            try:
                future = self._symbol_executor.submit(
                    self._collect_quotes,
                    symbol,
                    continuous=session.status is TradingSessionStatus.OPEN,
                    deadline_at=deadline_at,
                )
            except ExecutorCapacityError:
                results[symbol] = _capacity_quote_check(symbol)
            else:
                futures[future] = symbol
        if futures:
            done, pending = wait(
                tuple(futures),
                timeout=_deadline_wait_seconds(deadline_at, self._clock),
            )
            for future in done:
                symbol = futures[future]
                if _deadline_reached(deadline_at, self._clock):
                    results[symbol] = _deadline_quote_check(symbol)
                    continue
                try:
                    quote = future.result()
                except Exception:
                    quote = _QualifiedQuotes.unknown("ON_DEMAND_MARKET_QUOTE_UNAVAILABLE")
                eligible = (
                    quote.status == "READY"
                    and session.status is TradingSessionStatus.OPEN
                    and quote.primary_trading
                    and not session_gaps
                )
                terminal_status: TacticalEvidenceStatus = quote.status
                if session_gaps and terminal_status == "READY":
                    terminal_status = "PARTIAL"
                results[symbol] = TerminalQuoteCheck(
                    symbol=symbol,
                    status=terminal_status,
                    quote_price=quote.qualified_price,
                    valid_until=(
                        _context_valid_until(
                            self._calendar,
                            session,
                            as_of=quote.observed_at,
                        )
                        if quote.observed_at is not None
                        else None
                    ),
                    manual_review_eligible=eligible,
                    data_gaps=tuple(
                        dict.fromkeys(
                            (
                                *quote.data_gaps,
                                *session_gaps,
                                *(("MARKET_SESSION_REFERENCE_ONLY",) if not eligible else ()),
                            )
                        )
                    ),
                )
            for future in pending:
                future.cancel()
                results[futures[future]] = _deadline_quote_check(futures[future])
        return tuple(results.get(symbol, _deadline_quote_check(symbol)) for symbol in symbols)

    def _collect_symbol(
        self,
        symbol: str,
        *,
        as_of: datetime,
        phase: str,
        session_open: bool,
        session_gaps: tuple[str, ...],
        deadline_at: datetime | None,
    ) -> TacticalInstrumentContext:
        parsed = _parse_symbol(symbol)
        if parsed is None:
            gap = (
                "ON_DEMAND_MARKET_BJ_UNSUPPORTED"
                if symbol.endswith(".BJ")
                else "ON_DEMAND_MARKET_SYMBOL_UNSUPPORTED"
            )
            return _unknown_symbol(symbol, phase=phase, gap=gap)
        if _deadline_reached(deadline_at, self._clock):
            return _unknown_symbol(symbol, phase=phase, gap="CONSULTATION_DEADLINE_REACHED")

        quote_future: Future[_QualifiedQuotes] | None = None
        bars_future: Future[tuple[QualifiedDailyBarSeries | None, tuple[str, ...]]] | None = None
        intraday_future: (
            Future[tuple[QualifiedThirtyMinuteBarSeries | None, tuple[str, ...]]] | None
        ) = None
        quote_capacity_exhausted = False
        bars_capacity_exhausted = False
        intraday_capacity_exhausted = False
        try:
            quote_future = self._detail_executor.submit(
                self._collect_quotes,
                symbol,
                continuous=session_open,
                deadline_at=deadline_at,
            )
        except ExecutorCapacityError:
            quote_capacity_exhausted = True
        try:
            bars_future = self._detail_executor.submit(
                self._collect_bars,
                symbol,
                as_of=as_of,
                deadline_at=deadline_at,
            )
        except ExecutorCapacityError:
            bars_capacity_exhausted = True
        if self._thirty_minute_bars is not None:
            try:
                intraday_future = self._detail_executor.submit(
                    self._collect_thirty_minute_bars,
                    symbol,
                    as_of=as_of,
                    deadline_at=deadline_at,
                )
            except ExecutorCapacityError:
                intraday_capacity_exhausted = True
        pending_inputs = tuple(
            cast(Future[object], future)
            for future in (quote_future, bars_future, intraday_future)
            if future is not None
        )
        done, pending = wait(
            pending_inputs,
            timeout=_deadline_wait_seconds(deadline_at, self._clock),
        )
        for future in pending:
            future.cancel()
        try:
            quote = (
                quote_future.result()
                if quote_future is not None and quote_future in done
                else _QualifiedQuotes.unknown(
                    "ON_DEMAND_MARKET_CAPACITY_EXHAUSTED"
                    if quote_capacity_exhausted
                    else "CONSULTATION_DEADLINE_REACHED"
                )
            )
        except Exception:
            quote = _QualifiedQuotes.unknown("ON_DEMAND_MARKET_QUOTE_UNAVAILABLE")
        try:
            bars, bars_gap = (
                bars_future.result()
                if bars_future is not None and bars_future in done
                else (
                    None,
                    (
                        "ON_DEMAND_MARKET_CAPACITY_EXHAUSTED"
                        if bars_capacity_exhausted
                        else "CONSULTATION_DEADLINE_REACHED",
                    ),
                )
            )
        except Exception:
            bars, bars_gap = None, ("COMPLETED_DAILY_BARS_UNAVAILABLE",)
        try:
            intraday, intraday_gaps = (
                intraday_future.result()
                if intraday_future is not None and intraday_future in done
                else (
                    None,
                    (
                        "ON_DEMAND_MARKET_CAPACITY_EXHAUSTED"
                        if intraday_capacity_exhausted
                        else "THIRTY_MINUTE_BARS_UNAVAILABLE",
                    ),
                )
            )
        except Exception:
            intraday, intraday_gaps = None, ("THIRTY_MINUTE_BARS_UNAVAILABLE",)

        technical_facts: dict[str, object] = {}
        latest_date: str | None = None
        provider_provenance = [fact.source_id for fact in quote.facts]
        daily_adjustment_qualified = bars is not None and bars.adjustment == "FORWARD_ADJUSTED_QFQ"
        if daily_adjustment_qualified:
            assert bars is not None
            latest_date = bars.completed_bars[-1].date if bars.completed_bars else None
            technical_facts = _compact_technical_facts(bars)
            provider_provenance.append(f"{bars.provider_id}@{bars.provider_version}")
        elif bars is not None:
            bars_gap = tuple(dict.fromkeys((*bars_gap, "COMPLETED_DAILY_BARS_ADJUSTMENT_MISMATCH")))
            provider_provenance.append(f"{bars.provider_id}@{bars.provider_version}")
        timeframes = _daily_timeframes(
            bars,
            as_of=as_of,
            data_gaps=bars_gap,
        )
        timeframes.update(
            _intraday_timeframes(
                intraday,
                as_of=as_of,
                data_gaps=intraday_gaps,
            )
        )
        if intraday is not None:
            provider_provenance.append(f"{intraday.provider_id}@{intraday.provider_version}")
        close_session = phase in {
            TradingSessionPhase.AFTER_CLOSE.value,
            TradingSessionPhase.CLOSED_DAY.value,
        }
        expected_close_date = self._expected_close_trade_date(as_of) if close_session else None
        quote_facts_ok = (
            len(quote.facts) == 2
            and quote.disagreement_ratio is not None
            and Decimal(quote.disagreement_ratio) <= _READY_DISAGREEMENT
            and not any(
                gap in quote.data_gaps
                for gap in (
                    "DUAL_SOURCE_QUOTE_CONFLICT",
                    "DUAL_SOURCE_QUOTE_INCOMPLETE",
                    "DUAL_SOURCE_QUOTE_DISAGREEMENT",
                )
            )
        )
        close_qualified = (
            close_session
            and expected_close_date is not None
            and quote_facts_ok
            and quote.qualified_price is not None
            and not quote.primary_suspended
            and all(
                fact.source_event_at is not None
                and fact.source_event_at.astimezone(_SHANGHAI).date() == expected_close_date
                for fact in quote.facts
            )
        )

        gaps = [*quote.data_gaps, *bars_gap, *session_gaps]
        limitations: list[str] = []
        if close_qualified:
            gaps = [
                gap
                for gap in gaps
                if gap
                not in {
                    "PRIMARY_TRADING_STATUS_UNKNOWN",
                    "NON_CONTINUOUS_REFERENCE_QUOTE",
                }
            ]
            limitations.extend(bars_gap)
            limitations.append("MARKET_SESSION_REFERENCE_ONLY")
            if latest_date != expected_close_date.isoformat():
                limitations.append("CURRENT_TRADING_DAY_BAR_NOT_INCLUDED")
        else:
            if phase == TradingSessionPhase.AFTER_CLOSE.value and (
                session_open
                or session_gaps
                or latest_date != as_of.astimezone(_SHANGHAI).date().isoformat()
            ):
                gaps.append("CURRENT_TRADING_DAY_BAR_NOT_INCLUDED")
            if not session_open:
                gaps.append("MARKET_SESSION_REFERENCE_ONLY")
        if quote.primary_suspended:
            gaps.append("PRIMARY_SOURCE_REPORTS_SUSPENDED")

        status = quote.status
        if close_qualified:
            status = "READY"
        else:
            if (bars is None or not daily_adjustment_qualified) and status == "READY":
                status = "PARTIAL"
            if session_gaps and status == "READY":
                status = "PARTIAL"
        if quote.status == "UNKNOWN":
            status = "UNKNOWN"
        reference_only = (
            not session_open
            or bool(session_gaps)
            or quote.primary_suspended
            or not quote.primary_present
        )
        if close_qualified:
            observation_mode: Literal[
                "LIVE", "CLOSE_REFERENCE", "REFERENCE_ONLY", "UNAVAILABLE"
            ] = "CLOSE_REFERENCE"
        elif session_open and not reference_only:
            observation_mode = "LIVE"
        elif quote.qualified_price is not None:
            observation_mode = "REFERENCE_ONLY"
        else:
            observation_mode = "UNAVAILABLE"
        manual_eligible = (
            status == "READY" and quote.primary_trading and session_open and not reference_only
        )
        evidence_payload = {
            "symbol": symbol,
            "as_of": as_of.isoformat(),
            "status": status,
            "phase": phase,
            "quote": [fact.to_agent_dict() for fact in quote.facts],
            "quote_observed_at": (
                quote.observed_at.isoformat() if quote.observed_at is not None else None
            ),
            "quote_price": quote.qualified_price,
            "quote_price_role": _quote_price_role(quote),
            "disagreement": quote.disagreement_ratio,
            "latest_bar": latest_date,
            "bar_count": len(bars.completed_bars) if bars is not None else 0,
            "technical_facts": technical_facts,
            "timeframes": timeframes,
            "provider_provenance": provider_provenance,
            "gaps": list(dict.fromkeys(gaps)),
        }
        return TacticalInstrumentContext(
            symbol=symbol,
            status=status,
            evidence_id=f"market-evidence-{_canonical_hash(evidence_payload)[:24]}",
            quote_price=quote.qualified_price,
            quote_price_role=_quote_price_role(quote),
            quote_disagreement_ratio=quote.disagreement_ratio,
            quote_facts=quote.facts,
            quote_observed_at=quote.observed_at,
            session_phase=phase,
            reference_only=reference_only,
            manual_review_eligible=manual_eligible,
            latest_completed_bar_date=latest_date,
            completed_bar_count=len(bars.completed_bars) if bars is not None else 0,
            technical_facts=technical_facts,
            provider_provenance=tuple(provider_provenance),
            data_gaps=tuple(dict.fromkeys(gaps)),
            timeframes=timeframes,
            observation_mode=observation_mode,
            context_limitations=tuple(dict.fromkeys(limitations)),
        )

    def _expected_close_trade_date(self, as_of: datetime) -> date | None:
        local = as_of.astimezone(_SHANGHAI)
        try:
            decision = self._calendar.previous_open_date(
                before=local.date() + timedelta(days=1),
                known_at=as_of,
            )
        except Exception:
            return None
        return decision.previous_open_date

    def _collect_quotes(
        self,
        symbol: str,
        *,
        continuous: bool,
        deadline_at: datetime | None,
    ) -> _QualifiedQuotes:
        parsed = _parse_symbol(symbol)
        if parsed is None:
            return _QualifiedQuotes.unknown("ON_DEMAND_MARKET_SYMBOL_UNSUPPORTED")
        code, venue = parsed
        sample = QualificationSample(symbol=code, venue=venue.lower())
        sources = (self._primary_quote, self._reference_quote)
        by_source: dict[str, tuple[QualificationSourceCapture | None, str | None]] = {}
        futures: dict[Future[QualificationSourceCapture], QualificationSourcePort] = {}
        for source in sources:
            try:
                future = self._quote_executor.submit(
                    self._capture_quote,
                    source,
                    sample,
                    deadline_at=deadline_at,
                )
            except ExecutorCapacityError:
                by_source[source.source_id] = (
                    None,
                    f"{source.source_id.upper()}_CAPACITY_EXHAUSTED",
                )
            else:
                futures[future] = source
        if futures:
            done, pending = wait(
                tuple(futures),
                timeout=_deadline_wait_seconds(deadline_at, self._clock),
            )
            for future in done:
                source = futures[future]
                try:
                    by_source[source.source_id] = (future.result(), None)
                except Exception:
                    by_source[source.source_id] = (
                        None,
                        f"{source.source_id.upper()}_UNAVAILABLE",
                    )
            for future in pending:
                source = futures[future]
                future.cancel()
                by_source[source.source_id] = (
                    None,
                    f"{source.source_id.upper()}_DEADLINE_REACHED",
                )
        captures = [(source.source_id, *by_source[source.source_id]) for source in sources]
        return _qualify_quotes(
            symbol,
            captures,
            continuous=continuous,
        )

    def _capture_quote(
        self,
        source: QualificationSourcePort,
        sample: QualificationSample,
        *,
        deadline_at: datetime | None,
    ) -> QualificationSourceCapture:
        timeout_seconds = _deadline_wait_seconds(deadline_at, self._clock)
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise TimeoutError("CONSULTATION_DEADLINE_REACHED")
        return source.capture(sample, timeout_seconds=timeout_seconds)

    def _collect_bars(
        self,
        symbol: str,
        *,
        as_of: datetime,
        deadline_at: datetime | None,
    ) -> tuple[QualifiedDailyBarSeries | None, tuple[str, ...]]:
        try:
            series = self._daily_bars.read(
                QualifiedDailyBarReadRequest(
                    symbol=symbol,
                    trade_date=as_of.astimezone(_SHANGHAI).date(),
                    decision_cutoff_at=as_of,
                    minimum_completed_bars=MINIMUM_COMPLETED_BARS,
                    deadline_at=deadline_at,
                )
            )
        except Exception as error:
            if getattr(error, "code", None) in {
                "EASTMONEY_DAILY_BAR_DEADLINE_REACHED",
                "EASTMONEY_DAILY_BAR_ARTIFACT_LOCK_TIMEOUT",
            }:
                return None, ("CONSULTATION_DEADLINE_REACHED",)
            # 2026-08-03 诊断：东财不可达 + 腾讯 fallback 单测通过但 capability
            # 集成中仍 UNAVAILABLE——记录真实异常（脱敏：只记类型与 code，不记
            # 文本/URL/凭据），供 provider 观测与集成诊断。
            logger.error(
                "on-demand daily bars failed: type=%s code=%s",
                type(error).__name__,
                getattr(error, "code", None),
            )
            return None, ("COMPLETED_DAILY_BARS_UNAVAILABLE",)
        completed, completion_gaps = _completed_daily_bars(
            series.completed_bars,
            cutoff=as_of.astimezone(_SHANGHAI),
        )
        if len(completed) < MINIMUM_COMPLETED_BARS:
            return None, tuple(
                dict.fromkeys((*completion_gaps, "COMPLETED_DAILY_BARS_INSUFFICIENT"))
            )
        return replace(series, completed_bars=completed), completion_gaps

    def _collect_thirty_minute_bars(
        self,
        symbol: str,
        *,
        as_of: datetime,
        deadline_at: datetime | None,
    ) -> tuple[QualifiedThirtyMinuteBarSeries | None, tuple[str, ...]]:
        reader = self._thirty_minute_bars
        if reader is None:
            return None, ("THIRTY_MINUTE_BARS_UNAVAILABLE",)
        try:
            series = reader.read(
                QualifiedThirtyMinuteBarReadRequest(
                    symbol=symbol,
                    trade_date=as_of.astimezone(_SHANGHAI).date(),
                    decision_cutoff_at=as_of,
                    minimum_completed_bars=MINIMUM_COMPLETED_BARS,
                    deadline_at=deadline_at,
                )
            )
        except Exception as error:
            logger.error(
                "on-demand 30-minute bars failed: type=%s code=%s",
                type(error).__name__,
                getattr(error, "code", None),
            )
            return None, ("THIRTY_MINUTE_BARS_UNAVAILABLE",)
        if series.symbol != symbol:
            return None, ("THIRTY_MINUTE_BARS_IDENTITY_MISMATCH",)
        if not series.completed_bars:
            return None, ("THIRTY_MINUTE_BARS_EMPTY",)
        return series, ()


@dataclass(frozen=True, slots=True)
class _QualifiedQuotes:
    status: TacticalEvidenceStatus
    qualified_price: str | None
    disagreement_ratio: str | None
    facts: tuple[TacticalQuoteFact, ...]
    observed_at: datetime | None
    primary_present: bool
    primary_trading: bool
    primary_suspended: bool
    data_gaps: tuple[str, ...]

    @classmethod
    def unknown(cls, gap: str) -> _QualifiedQuotes:
        return cls("UNKNOWN", None, None, (), None, False, False, False, (gap,))


@dataclass(frozen=True, slots=True)
class _MarketDriverBuildContext:
    daily_artifact_root: Path | None
    intraday_artifact_root: Path | None
    eastmoney_http_get: _EastmoneyOnDemandHttpGet
    clock: Callable[[], datetime] | None


def _eastmoney_quote_driver(
    context: _MarketDriverBuildContext,
    timeout_seconds: float,
) -> QualificationSourcePort:
    return _build_on_demand_eastmoney_raw_source(
        transport=context.eastmoney_http_get,
        clock=context.clock,
        timeout_seconds=timeout_seconds,
    )


def _tencent_quote_driver(
    context: _MarketDriverBuildContext,
    timeout_seconds: float,
) -> QualificationSourcePort:
    return TencentRawQualificationSource(
        clock=context.clock,
        evidence_origin=ObservationEvidenceOrigin.LIVE_CAPTURE,
        timeout_seconds=timeout_seconds,
    )


def _eastmoney_daily_driver(
    context: _MarketDriverBuildContext,
    timeout_seconds: float,
) -> QualifiedDailyBarReader:
    if context.daily_artifact_root is None:
        raise ValueError("daily artifact root is unavailable")
    return _build_on_demand_eastmoney_daily_bar_reader(
        artifact_root=context.daily_artifact_root,
        transport=context.eastmoney_http_get,
        timeout_seconds=timeout_seconds,
    )


def _tencent_daily_driver(
    context: _MarketDriverBuildContext,
    timeout_seconds: float,
) -> QualifiedDailyBarReader:
    return _build_on_demand_tencent_daily_bar_reader(timeout_seconds=timeout_seconds)


def _eastmoney_intraday_driver(
    context: _MarketDriverBuildContext,
    timeout_seconds: float,
) -> QualifiedThirtyMinuteBarReader:
    if context.intraday_artifact_root is None:
        raise ValueError("intraday artifact root is unavailable")
    return _build_on_demand_eastmoney_thirty_minute_bar_reader(
        artifact_root=context.intraday_artifact_root,
        transport=context.eastmoney_http_get,
        timeout_seconds=timeout_seconds,
    )


def _tencent_intraday_driver(
    context: _MarketDriverBuildContext,
    timeout_seconds: float,
) -> QualifiedThirtyMinuteBarReader:
    return _build_on_demand_tencent_intraday_bar_reader(timeout_seconds=timeout_seconds)


_QUOTE_DRIVER_CATALOG = {
    "eastmoney_quote": _eastmoney_quote_driver,
    "tencent_quote": _tencent_quote_driver,
}
_DAILY_DRIVER_CATALOG = {
    "eastmoney_daily": _eastmoney_daily_driver,
    "tencent_daily": _tencent_daily_driver,
}
_INTRADAY_DRIVER_CATALOG = {
    "eastmoney_intraday": _eastmoney_intraday_driver,
    "tencent_intraday": _tencent_intraday_driver,
}


def _build_driver_lane(
    drivers: tuple[MarketEvidenceDriver, ...],
    catalog: Mapping[str, Callable[[_MarketDriverBuildContext, float], object]],
    context: _MarketDriverBuildContext,
) -> tuple[object, ...]:
    return tuple(
        catalog[driver.driver_id](context, driver.timeout_seconds) for driver in drivers
    )


def build_default_on_demand_tactical_context(
    *,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> OnDemandTacticalContextService:
    """Build the production advisory collector with FIN-fixed source policy."""

    source = os.environ if environ is None else environ
    plan = DEFAULT_MARKET_EVIDENCE_PLAN
    eastmoney_http_get = _build_eastmoney_on_demand_http_get()
    try:
        daily_artifact_root = on_demand_tactical_artifact_root(environ=source)
    except (OSError, ValueError):
        daily_artifact_root = None
    try:
        intraday_artifact_root = on_demand_tactical_intraday_artifact_root(environ=source)
    except (OSError, ValueError):
        intraday_artifact_root = None
    context = _MarketDriverBuildContext(
        daily_artifact_root=daily_artifact_root,
        intraday_artifact_root=intraday_artifact_root,
        eastmoney_http_get=eastmoney_http_get,
        clock=clock,
    )
    quote_drivers = cast(
        tuple[QualificationSourcePort, QualificationSourcePort],
        _build_driver_lane(plan.quote, _QUOTE_DRIVER_CATALOG, context),
    )
    try:
        daily_drivers = cast(
            tuple[QualifiedDailyBarReader, ...],
            _build_driver_lane(plan.daily, _DAILY_DRIVER_CATALOG, context),
        )
        daily_bars: QualifiedDailyBarReader = (
            daily_drivers[0]
            if len(daily_drivers) == 1
            else _FallbackDailyBarReader(
                primary=daily_drivers[0], fallback=daily_drivers[1]
            )
        )
    except (OSError, ValueError):
        daily_bars = _UnavailableDailyBarReader()
    try:
        intraday_drivers = cast(
            tuple[QualifiedThirtyMinuteBarReader, ...],
            _build_driver_lane(plan.intraday, _INTRADAY_DRIVER_CATALOG, context),
        )
        thirty_minute_bars: QualifiedThirtyMinuteBarReader | None = (
            intraday_drivers[0]
            if len(intraday_drivers) == 1
            else _FallbackIntradayBarReader(
                primary=intraday_drivers[0], fallback=intraday_drivers[1]
            )
        )
    except (OSError, ValueError):
        thirty_minute_bars = None
    try:
        calendar: TradingCalendarPort = AShareTradingCalendar.from_file(
            _PROJECT_ROOT / "config" / "market" / "a_share_calendar_2026.json"
        )
    except (OSError, ValueError):
        calendar = _UnavailableTradingCalendar()
    return OnDemandTacticalContextService(
        primary_quote=quote_drivers[0],
        reference_quote=quote_drivers[1],
        daily_bars=daily_bars,
        thirty_minute_bars=thirty_minute_bars,
        calendar=calendar,
        clock=clock,
    )


def on_demand_tactical_artifact_root(*, environ: Mapping[str, str] | None = None) -> Path:
    return _on_demand_tactical_artifact_root(_RELATIVE_ARTIFACT_ROOT, environ=environ)


def on_demand_tactical_intraday_artifact_root(
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    return _on_demand_tactical_artifact_root(_RELATIVE_INTRADAY_ARTIFACT_ROOT, environ=environ)


def _on_demand_tactical_artifact_root(
    relative_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    source = os.environ if environ is None else environ
    configured = source.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    root = (base / relative_root).resolve()
    project = _PROJECT_ROOT.resolve()
    if (
        not root.is_absolute()
        or root == project
        or project in root.parents
        or root in project.parents
    ):
        raise ValueError("on-demand tactical artifact root must be outside the checkout")
    return root


def _validate_request(request: OnDemandTacticalContextRequest) -> tuple[str, ...]:
    if not isinstance(request, OnDemandTacticalContextRequest):
        raise TypeError("request must be OnDemandTacticalContextRequest")
    if request.as_of.tzinfo is None or request.as_of.utcoffset() is None:
        raise ValueError("on-demand tactical as_of must be timezone-aware")
    if request.deadline_at is not None and (
        request.deadline_at.tzinfo is None or request.deadline_at.utcoffset() is None
    ):
        raise ValueError("on-demand tactical deadline must be timezone-aware")
    if not 1 <= len(request.instruments) <= _MAX_SYMBOLS:
        raise ValueError("on-demand tactical context supports one to five instruments")
    if len(set(request.instruments)) != len(request.instruments):
        raise ValueError("on-demand tactical instruments must be distinct")
    return request.instruments


def _qualify_quotes(
    symbol: str,
    captures: list[tuple[str, QualificationSourceCapture | None, str | None]],
    *,
    continuous: bool,
) -> _QualifiedQuotes:
    parsed = _parse_symbol(symbol)
    if parsed is None:
        return _QualifiedQuotes.unknown("ON_DEMAND_MARKET_SYMBOL_UNSUPPORTED")
    code, venue = parsed
    facts: list[TacticalQuoteFact] = []
    gaps: list[str] = []
    primary_trading = False
    primary_suspended = False
    for index, (source_id, capture, failure) in enumerate(captures):
        if failure is not None or capture is None:
            gaps.append(failure or f"{source_id.upper()}_UNAVAILABLE")
            continue
        if capture.data_gaps:
            gaps.extend(f"{source_id.upper()}_{gap.upper()}" for gap in capture.data_gaps)
        if capture.venue is None:
            # 2026-08-31（BUG-011）：失败捕获 venue=None 且必带自身 typed gap
            # （解析失败/http 状态码），身份比对必假阳性报 IDENTITY_MISMATCH，
            # 掩盖真因（08-28~08-30 trace 全数如此）——失败捕获以其自身 gap
            # 为完整结论，不重复报身份错配。
            continue
        if capture.symbol != code or capture.venue != venue.lower():
            gaps.append(f"{source_id.upper()}_IDENTITY_MISMATCH")
            continue
        fact = _quote_fact(source_id, capture, continuous=continuous)
        if fact is None:
            gaps.append(f"{source_id.upper()}_QUOTE_STALE_OR_INCOMPLETE")
            continue
        facts.append(fact)
        if index == 0:
            primary_trading = capture.trading_status is TradingStatus.TRADING
            primary_suspended = capture.trading_status is TradingStatus.SUSPENDED
        elif (
            index == 1
            and not primary_trading
            and not primary_suspended
            and (captures[0][1] is None or captures[0][2] is not None)
        ):
            # 2026-08-03：东财 push2 实时行情不可达（CDN 出口被干扰，git 历史
            # 8-02 已记录 push2his 同样问题）——primary 源失败时用参考源（腾讯）
            # 的交易状态，避免咨询上下文因 PRIMARY_TRADING_STATUS_UNKNOWN 无法
            # 选择（东财不可达期间咨询整体 runtime_unavailable）。
            primary_trading = capture.trading_status is TradingStatus.TRADING
            primary_suspended = capture.trading_status is TradingStatus.SUSPENDED

    if not facts:
        return _QualifiedQuotes(
            "UNKNOWN",
            None,
            None,
            (),
            None,
            False,
            primary_trading,
            primary_suspended,
            tuple(dict.fromkeys(gaps)),
        )
    observed_at = max(
        capture.received_at
        for source_id, capture, _failure in captures
        if capture is not None and any(fact.source_id == source_id for fact in facts)
    )
    primary_source_id = captures[0][0]
    primary_present = any(fact.source_id == primary_source_id for fact in facts)
    if len(facts) == 1:
        gaps.append("DUAL_SOURCE_QUOTE_INCOMPLETE")
        if not continuous:
            gaps.append("NON_CONTINUOUS_REFERENCE_QUOTE")
        return _QualifiedQuotes(
            "PARTIAL",
            facts[0].price,
            None,
            tuple(facts),
            observed_at,
            primary_present,
            primary_trading,
            primary_suspended,
            tuple(dict.fromkeys(gaps)),
        )
    first = Decimal(facts[0].price)
    second = Decimal(facts[1].price)
    denominator = max(first, second)
    disagreement = abs(first - second) / denominator
    ratio = _decimal_text(disagreement)
    primary_price = next(fact.price for fact in facts if fact.source_id == primary_source_id)
    if disagreement <= _READY_DISAGREEMENT:
        status: TacticalEvidenceStatus = "READY"
    elif disagreement <= _UNKNOWN_DISAGREEMENT:
        status = "PARTIAL"
        gaps.append("DUAL_SOURCE_QUOTE_DISAGREEMENT")
    else:
        status = "UNKNOWN"
        gaps.append("DUAL_SOURCE_QUOTE_CONFLICT")
    if primary_suspended:
        status = "UNKNOWN"
    elif not primary_trading and status == "READY":
        status = "PARTIAL"
        gaps.append("PRIMARY_TRADING_STATUS_UNKNOWN")
    if not continuous:
        gaps.append("NON_CONTINUOUS_REFERENCE_QUOTE")
    return _QualifiedQuotes(
        status,
        primary_price,
        ratio,
        tuple(facts),
        observed_at,
        primary_present,
        primary_trading,
        primary_suspended,
        tuple(dict.fromkeys(gaps)),
    )


def _quote_fact(
    source_id: str,
    capture: QualificationSourceCapture,
    *,
    continuous: bool,
) -> TacticalQuoteFact | None:
    if capture.price is None or capture.source_event_at is None:
        return None
    try:
        price = Decimal(capture.price)
    except InvalidOperation:
        return None
    age = Decimal(str((capture.received_at - capture.source_event_at).total_seconds()))
    max_age = _QUOTE_MAX_AGE_SECONDS if continuous else _REFERENCE_QUOTE_MAX_AGE_SECONDS
    if price <= 0 or age < -_MAX_SOURCE_CLOCK_SKEW_SECONDS or age > max_age:
        return None
    return TacticalQuoteFact(
        source_id=source_id,
        payload_sha256=hashlib.sha256(capture.raw_payload).hexdigest(),
        price=_decimal_text(price),
        source_event_at=capture.source_event_at,
        trading_status=capture.trading_status.value,
        upper_limit_price=capture.upper_limit_price,
        lower_limit_price=capture.lower_limit_price,
        volume=capture.volume,
        turnover=capture.turnover,
    )


def _compact_technical_facts(series: QualifiedDailyBarSeries) -> dict[str, object]:
    return _compact_bars_technical_facts(series.completed_bars)


def _compact_bars_technical_facts(bars: tuple[OHLCV, ...]) -> dict[str, object]:
    values = list(bars)
    if not values:
        return {}
    computed = compute_all(values)
    latest = values[-1]
    previous = values[-2] if len(values) >= 2 else None
    return {
        "last_completed_close": latest.close,
        "previous_completed_close": previous.close if previous is not None else None,
        "ma5": computed["ma5"][-1],
        "ma10": computed["ma10"][-1],
        "ma20": computed["ma20"][-1],
        "ma60": computed["ma60"][-1],
        "macd_dif": computed["macd_dif"][-1],
        "macd_dea": computed["macd_dea"][-1],
        "macd_histogram": computed["macd_histogram"][-1],
        "rsi14": computed["rsi14"][-1],
        "boll_upper": computed["boll_upper"][-1],
        "boll_middle": computed["boll_middle"][-1],
        "boll_lower": computed["boll_lower"][-1],
        "volume_ratio_5": computed["vol_ratio"][-1],
        "atr14": computed["atr"][-1],
    }


def _daily_timeframes(
    series: QualifiedDailyBarSeries | None,
    *,
    as_of: datetime,
    data_gaps: tuple[str, ...],
) -> dict[str, Mapping[str, object]]:
    cutoff = as_of.astimezone(_SHANGHAI)
    if series is None:
        return {
            timeframe: _unavailable_timeframe(
                timeframe,
                cutoff=cutoff,
                data_gaps=data_gaps,
            )
            for timeframe in ("daily", "weekly", "monthly", "annual")
        }
    source = _daily_timeframe_source(series)
    if series.adjustment != "FORWARD_ADJUSTED_QFQ":
        gaps = (*data_gaps, "TIMEFRAME_ADJUSTMENT_MISMATCH")
        return {
            timeframe: _unavailable_timeframe(
                timeframe,
                cutoff=cutoff,
                data_gaps=gaps,
                source=source,
            )
            for timeframe in ("daily", "weekly", "monthly", "annual")
        }
    completed, completion_gaps = _completed_daily_bars(
        series.completed_bars,
        cutoff=cutoff,
    )
    daily_gaps = tuple(dict.fromkeys((*data_gaps, *completion_gaps)))
    weekly, weekly_gaps = _aggregate_daily_bars(completed, timeframe="weekly")
    monthly, monthly_gaps = _aggregate_daily_bars(completed, timeframe="monthly")
    annual, annual_gaps = _aggregate_daily_bars(completed, timeframe="annual")
    return {
        "daily": _daily_timeframe_projection(
            "daily",
            bars=completed,
            cutoff=cutoff,
            source=source,
            data_gaps=daily_gaps,
        ),
        "weekly": _daily_timeframe_projection(
            "weekly",
            bars=weekly,
            cutoff=cutoff,
            source={**source, "derived_from": "daily"},
            data_gaps=tuple(dict.fromkeys((*daily_gaps, *weekly_gaps))),
        ),
        "monthly": _daily_timeframe_projection(
            "monthly",
            bars=monthly,
            cutoff=cutoff,
            source={**source, "derived_from": "daily"},
            data_gaps=tuple(dict.fromkeys((*daily_gaps, *monthly_gaps))),
        ),
        "annual": _daily_timeframe_projection(
            "annual",
            bars=annual,
            cutoff=cutoff,
            source={**source, "derived_from": "daily"},
            data_gaps=tuple(dict.fromkeys((*daily_gaps, *annual_gaps))),
        ),
    }


def _daily_timeframe_source(series: QualifiedDailyBarSeries) -> dict[str, object]:
    return {
        "provider_id": series.provider_id,
        "provider_version": series.provider_version,
        "adjustment": series.adjustment,
        "source_revision": series.source_revision,
    }


def _completed_daily_bars(
    bars: tuple[OHLCV, ...],
    *,
    cutoff: datetime,
) -> tuple[tuple[OHLCV, ...], tuple[str, ...]]:
    completed: list[OHLCV] = []
    gaps: list[str] = []
    previous: date | None = None
    for bar in bars:
        bar_date = _daily_bar_date(bar)
        if bar_date is None:
            gaps.append("DAILY_BAR_DATE_INVALID")
            continue
        completed_at = datetime.combine(bar_date, time(15), tzinfo=_SHANGHAI)
        if completed_at > cutoff:
            gaps.append(
                "CURRENT_DAILY_BAR_INCOMPLETE"
                if bar_date == cutoff.date()
                else "DAILY_BAR_AFTER_CUTOFF_EXCLUDED"
            )
            continue
        if previous is not None and bar_date <= previous:
            gaps.append("DAILY_BAR_DATE_UNORDERED_OR_DUPLICATE")
            continue
        previous = bar_date
        completed.append(bar)
    return tuple(completed), tuple(dict.fromkeys(gaps))


def _aggregate_daily_bars(
    bars: tuple[OHLCV, ...],
    *,
    timeframe: Literal["weekly", "monthly", "annual"],
) -> tuple[tuple[OHLCV, ...], tuple[str, ...]]:
    grouped: dict[tuple[int, ...], list[OHLCV]] = {}
    gaps: list[str] = []
    for bar in bars:
        bar_date = _daily_bar_date(bar)
        if bar_date is None:
            gaps.append("DAILY_BAR_DATE_INVALID")
            continue
        if timeframe == "weekly":
            key: tuple[int, ...] = (bar_date.isocalendar().year, bar_date.isocalendar().week)
        elif timeframe == "monthly":
            key = (bar_date.year, bar_date.month)
        else:  # annual: 按年确定性聚合(年线,与周/月同机制)
            key = (bar_date.year,)
        grouped.setdefault(key, []).append(bar)
    aggregated = tuple(_aggregate_ohlcv(group) for _key, group in sorted(grouped.items()) if group)
    return aggregated, tuple(dict.fromkeys(gaps))


def _daily_timeframe_projection(
    timeframe: str,
    *,
    bars: tuple[OHLCV, ...],
    cutoff: datetime,
    source: Mapping[str, object],
    data_gaps: tuple[str, ...],
) -> dict[str, object]:
    if not bars:
        return _unavailable_timeframe(
            timeframe,
            cutoff=cutoff,
            data_gaps=data_gaps,
            source=source,
        )
    first = _daily_bar_date(bars[0])
    latest = _daily_bar_date(bars[-1])
    if first is None or latest is None:
        return _unavailable_timeframe(
            timeframe,
            cutoff=cutoff,
            data_gaps=(*data_gaps, "DAILY_BAR_DATE_INVALID"),
            source=source,
        )
    gaps = list(data_gaps)
    revision_available = source.get("source_revision") is not None
    if not revision_available:
        gaps.append("TIMEFRAME_SOURCE_REVISION_UNAVAILABLE")
    return {
        "timeframe": timeframe,
        "status": "READY" if revision_available else "PARTIAL",
        "cutoff_at": cutoff.isoformat(),
        "coverage_start_at": datetime.combine(first, time(15), tzinfo=_SHANGHAI).isoformat(),
        "latest_completed_bar_at": datetime.combine(
            latest,
            time(15),
            tzinfo=_SHANGHAI,
        ).isoformat(),
        "completed_bar_count": len(bars),
        "source": dict(source),
        "technical_facts": _compact_bars_technical_facts(bars),
        "data_gaps": list(dict.fromkeys(gaps)),
    }


def _daily_bar_date(bar: OHLCV) -> date | None:
    try:
        parsed = date.fromisoformat(bar.date)
    except (TypeError, ValueError):
        return None
    return parsed if bar.date == parsed.isoformat() else None


def _aggregate_ohlcv(bars: list[OHLCV]) -> OHLCV:
    first = bars[0]
    last = bars[-1]
    return OHLCV(
        date=last.date,
        open=first.open,
        high=max(bar.high for bar in bars),
        low=min(bar.low for bar in bars),
        close=last.close,
        volume=sum(bar.volume for bar in bars),
        turnover=(
            sum(bar.turnover for bar in bars if bar.turnover is not None)
            if all(bar.turnover is not None for bar in bars)
            else None
        ),
    )


def _intraday_timeframes(
    series: QualifiedThirtyMinuteBarSeries | None,
    *,
    as_of: datetime,
    data_gaps: tuple[str, ...],
) -> dict[str, Mapping[str, object]]:
    cutoff = as_of.astimezone(_SHANGHAI)
    if series is None:
        return {
            timeframe: _unavailable_timeframe(
                timeframe,
                cutoff=cutoff,
                data_gaps=data_gaps,
            )
            for timeframe in ("30m", "60m")
        }
    if series.adjustment != "FORWARD_ADJUSTED_QFQ":
        gaps = (*data_gaps, "TIMEFRAME_ADJUSTMENT_MISMATCH")
        return {
            timeframe: _unavailable_timeframe(
                timeframe,
                cutoff=cutoff,
                data_gaps=gaps,
                source=_timeframe_source(series),
            )
            for timeframe in ("30m", "60m")
        }
    completed, completion_gaps = _completed_thirty_minute_bars(
        series.completed_bars,
        cutoff=cutoff,
    )
    thirty_gaps = tuple(dict.fromkeys((*data_gaps, *completion_gaps)))
    thirty = _timeframe_projection(
        "30m",
        bars=completed,
        cutoff=cutoff,
        duration=timedelta(minutes=30),
        source=_timeframe_source(series),
        data_gaps=thirty_gaps,
    )
    sixty_bars, sixty_gaps = _aggregate_sixty_minute_bars(completed)
    sixty = _timeframe_projection(
        "60m",
        bars=sixty_bars,
        cutoff=cutoff,
        duration=timedelta(hours=1),
        source={**_timeframe_source(series), "derived_from": "30m"},
        data_gaps=tuple(dict.fromkeys((*thirty_gaps, *sixty_gaps))),
    )
    return {"30m": thirty, "60m": sixty}


def _timeframe_source(series: QualifiedThirtyMinuteBarSeries) -> dict[str, object]:
    return {
        "provider_id": series.provider_id,
        "provider_version": series.provider_version,
        "adjustment": series.adjustment,
        "source_revision": series.source_revision,
    }


def _unavailable_timeframe(
    timeframe: str,
    *,
    cutoff: datetime,
    data_gaps: tuple[str, ...],
    source: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "timeframe": timeframe,
        "status": "UNKNOWN",
        "cutoff_at": cutoff.isoformat(),
        "coverage_start_at": None,
        "latest_completed_bar_at": None,
        "completed_bar_count": 0,
        "source": dict(source or {}),
        "technical_facts": {},
        "data_gaps": list(dict.fromkeys(data_gaps)),
    }


def _timeframe_projection(
    timeframe: str,
    *,
    bars: tuple[OHLCV, ...],
    cutoff: datetime,
    duration: timedelta,
    source: Mapping[str, object],
    data_gaps: tuple[str, ...],
) -> dict[str, object]:
    if not bars:
        return _unavailable_timeframe(
            timeframe,
            cutoff=cutoff,
            data_gaps=data_gaps,
            source=source,
        )
    first = _bar_start_at(bars[0])
    latest = _bar_start_at(bars[-1])
    if first is None or latest is None:
        return _unavailable_timeframe(
            timeframe,
            cutoff=cutoff,
            data_gaps=(*data_gaps, "TIMEFRAME_BAR_TIMESTAMP_INVALID"),
            source=source,
        )
    gaps = list(data_gaps)
    revision_available = source.get("source_revision") is not None
    if not revision_available:
        gaps.append("TIMEFRAME_SOURCE_REVISION_UNAVAILABLE")
    return {
        "timeframe": timeframe,
        "status": "READY" if revision_available else "PARTIAL",
        "cutoff_at": cutoff.isoformat(),
        "coverage_start_at": first.isoformat(),
        "latest_completed_bar_at": (latest + duration).isoformat(),
        "completed_bar_count": len(bars),
        "source": dict(source),
        "technical_facts": _compact_bars_technical_facts(bars),
        "data_gaps": list(dict.fromkeys(gaps)),
    }


def _completed_thirty_minute_bars(
    bars: tuple[OHLCV, ...],
    *,
    cutoff: datetime,
) -> tuple[tuple[OHLCV, ...], tuple[str, ...]]:
    completed: list[OHLCV] = []
    gaps: list[str] = []
    for bar in bars:
        start = _bar_start_at(bar)
        if start is None or not _is_thirty_minute_session_start(start):
            gaps.append("THIRTY_MINUTE_BAR_TIMESTAMP_INVALID")
            continue
        if start + timedelta(minutes=30) > cutoff:
            gaps.append("CURRENT_THIRTY_MINUTE_BAR_INCOMPLETE")
            continue
        completed.append(bar)
    return tuple(completed), tuple(dict.fromkeys(gaps))


def _aggregate_sixty_minute_bars(
    bars: tuple[OHLCV, ...],
) -> tuple[tuple[OHLCV, ...], tuple[str, ...]]:
    by_start: dict[datetime, OHLCV] = {}
    gaps: list[str] = []
    for bar in bars:
        start = _bar_start_at(bar)
        if start is None:
            gaps.append("THIRTY_MINUTE_BAR_TIMESTAMP_INVALID")
            continue
        if start in by_start:
            gaps.append("THIRTY_MINUTE_BAR_DUPLICATE")
            continue
        by_start[start] = bar
    dates = sorted({start.date() for start in by_start})
    sixty: list[OHLCV] = []
    for trading_day in dates:
        for hour, minute in ((9, 30), (10, 30), (13, 0), (14, 0)):
            first_start = datetime.combine(trading_day, time(hour, minute), tzinfo=_SHANGHAI)
            second_start = first_start + timedelta(minutes=30)
            first = by_start.get(first_start)
            second = by_start.get(second_start)
            if first is None and second is None:
                continue
            if first is None or second is None:
                gaps.append("SIXTY_MINUTE_SOURCE_GAP")
                continue
            sixty.append(
                OHLCV(
                    date=first_start.isoformat(),
                    open=first.open,
                    high=max(first.high, second.high),
                    low=min(first.low, second.low),
                    close=second.close,
                    volume=first.volume + second.volume,
                    turnover=(
                        first.turnover + second.turnover
                        if first.turnover is not None and second.turnover is not None
                        else None
                    ),
                )
            )
    return tuple(sixty), tuple(dict.fromkeys(gaps))


def _bar_start_at(bar: OHLCV) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(bar.date)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(_SHANGHAI)


def _is_thirty_minute_session_start(value: datetime) -> bool:
    return value.time() in {
        time(9, 30),
        time(10, 0),
        time(10, 30),
        time(11, 0),
        time(13, 0),
        time(13, 30),
        time(14, 0),
        time(14, 30),
    }


def _quote_price_role(
    quote: _QualifiedQuotes,
) -> Literal["PRIMARY", "REFERENCE_ONLY", "NONE"]:
    if quote.qualified_price is None:
        return "NONE"
    return "PRIMARY" if quote.primary_present else "REFERENCE_ONLY"


def _parse_symbol(symbol: str) -> tuple[str, str] | None:
    match = _CANONICAL_SYMBOL.fullmatch(symbol) if isinstance(symbol, str) else None
    if match is None or match.group("venue") == "BJ":
        return None
    return match.group("code"), match.group("venue")


def _aggregate_status(statuses) -> TacticalEvidenceStatus:
    values = tuple(statuses)
    if values and all(value == "READY" for value in values):
        return "READY"
    if any(value in {"READY", "PARTIAL"} for value in values):
        return "PARTIAL"
    return "UNKNOWN"


def _unknown_symbol(symbol: str, *, phase: str, gap: str) -> TacticalInstrumentContext:
    payload = {"symbol": symbol, "phase": phase, "gap": gap}
    return TacticalInstrumentContext(
        symbol=symbol,
        status="UNKNOWN",
        evidence_id=f"market-evidence-{_canonical_hash(payload)[:24]}",
        quote_price=None,
        quote_price_role="NONE",
        quote_disagreement_ratio=None,
        quote_facts=(),
        quote_observed_at=None,
        session_phase=phase,
        reference_only=True,
        manual_review_eligible=False,
        latest_completed_bar_date=None,
        completed_bar_count=0,
        technical_facts={},
        provider_provenance=(),
        data_gaps=(gap,),
    )


def _deadline_context(
    as_of: datetime,
    phase: str,
    symbols: tuple[str, ...],
) -> OnDemandTacticalContext:
    instruments = tuple(
        _unknown_symbol(symbol, phase=phase, gap="CONSULTATION_DEADLINE_REACHED")
        for symbol in symbols
    )
    return OnDemandTacticalContext(
        status="UNKNOWN",
        as_of=as_of,
        valid_until=as_of,
        instruments=instruments,
        session_phase=phase,
        data_gaps=("CONSULTATION_DEADLINE_REACHED",),
    )


def _deadline_quote_check(symbol: str) -> TerminalQuoteCheck:
    return TerminalQuoteCheck(
        symbol=symbol,
        status="UNKNOWN",
        quote_price=None,
        valid_until=None,
        manual_review_eligible=False,
        data_gaps=("CONSULTATION_DEADLINE_REACHED",),
    )


def _capacity_quote_check(symbol: str) -> TerminalQuoteCheck:
    return TerminalQuoteCheck(
        symbol=symbol,
        status="UNKNOWN",
        quote_price=None,
        valid_until=None,
        manual_review_eligible=False,
        data_gaps=("ON_DEMAND_MARKET_CAPACITY_EXHAUSTED",),
    )


def _context_valid_until(calendar: TradingCalendarPort, session, *, as_of: datetime) -> datetime:
    local = as_of.astimezone(_SHANGHAI)
    if session.status is TradingSessionStatus.OPEN:
        quote_expiry = as_of.replace(microsecond=0) + timedelta(seconds=int(_QUOTE_MAX_AGE_SECONDS))
        if session.phase is TradingSessionPhase.CONTINUOUS_AM:
            return min(
                quote_expiry,
                datetime.combine(local.date(), time(11, 30), tzinfo=_SHANGHAI),
            )
        if session.phase is TradingSessionPhase.CONTINUOUS_PM:
            return min(
                quote_expiry,
                datetime.combine(local.date(), time(15, 0), tzinfo=_SHANGHAI),
            )
        return as_of
    if session.phase is TradingSessionPhase.PRE_OPEN:
        return datetime.combine(local.date(), time(9, 30), tzinfo=_SHANGHAI)
    if session.phase is TradingSessionPhase.BREAK:
        return datetime.combine(local.date(), time(13, 0), tzinfo=_SHANGHAI)
    if session.phase in {TradingSessionPhase.AFTER_CLOSE, TradingSessionPhase.CLOSED_DAY}:
        try:
            next_open = calendar.next_open_date(after=local.date(), known_at=as_of)
        except Exception:
            return as_of + timedelta(hours=12)
        return datetime.combine(next_open.next_open_date, time(9, 30), tzinfo=_SHANGHAI)
    return as_of


def _deadline_reached(deadline_at: datetime | None, clock: Callable[[], datetime]) -> bool:
    if deadline_at is None:
        return False
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("on-demand tactical clock must be timezone-aware")
    return now >= deadline_at


def _deadline_wait_seconds(
    deadline_at: datetime | None,
    clock: Callable[[], datetime],
) -> float | None:
    if deadline_at is None:
        return None
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("on-demand tactical clock must be timezone-aware")
    return max(0.0, (deadline_at - now).total_seconds())


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


__all__ = [
    "OnDemandTacticalContext",
    "OnDemandTacticalContextReader",
    "OnDemandTacticalContextRequest",
    "OnDemandTacticalContextService",
    "TacticalInstrumentContext",
    "TacticalQuoteFact",
    "TerminalQuoteCheck",
    "build_default_on_demand_tactical_context",
    "on_demand_tactical_artifact_root",
    "on_demand_tactical_intraday_artifact_root",
]
