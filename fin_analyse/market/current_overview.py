"""Read-only A-share market overview for broad current-market questions.

The module turns one bounded Eastmoney cross-section into typed, source-labeled
evidence.  It never emits an investment conclusion and never treats a closed
day as a live session: weekend and pre-open reads are anchored to the previous
open date from FIN's frozen dual-exchange calendar.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from math import isfinite
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

import requests

from fin_analyse.market.trading_calendar import (
    AShareTradingCalendar,
    CalendarArtifactError,
    TradingSessionPhase,
    TradingSessionStatus,
)

_CN_TZ = ZoneInfo("Asia/Shanghai")
_EASTMONEY_LIST_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_EASTMONEY_INDEX_LIST_URL = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
_EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
_BOARD_FIELDS = "f3,f6,f8,f12,f14,f104,f105,f124,f128,f136"
_EQUITY_FIELDS = "f3,f6,f8,f12,f14,f124"
_INDEX_FIELDS = "f2,f3,f6,f12,f14,f104,f105,f106,f124"
_MAJOR_INDEX_SECIDS = "1.000001,0.399001,0.399006,1.000688"
_TENCENT_INDEX_QUOTE_URL = "https://qt.gtimg.cn/q="
_TENCENT_INDEX_CODES = "sh000001,sz399001,sz399006,sh000688"
_TENCENT_INDEX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}
_INDUSTRY_FILTER = "m:90 t:2 f:!50"
_CONCEPT_FILTER = "m:90 t:3 f:!50"
_A_SHARE_FILTER = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23"
_BOARD_PAGE_SIZE = 100
_EQUITY_PAGE_SIZE = 100
_LEADER_LIMIT = 12
_TURNOVER_STOCK_LIMIT = 15
_HTTP_TIMEOUT_SECONDS = 5.0
_HTTP_MIN_INTERVAL_SECONDS = 1.0
_A_SHARE_CLOSE_TIME = time(15, 0)
_INTRADAY_MAX_REFERENCE_AGE_SECONDS = 30 * 60
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}
_REQUIRED_RANKED_SECTIONS = frozenset(
    {
        "industry_change",
        "industry_turnover",
        "concept_change",
        "concept_turnover",
        "equity_turnover",
    }
)
_REQUIRED_MAJOR_INDEX_CODES = frozenset({"000001", "399001", "399006", "000688"})
_NON_THEMATIC_CONCEPT_MARKERS = (
    "昨日",
    "新高",
    "连板",
    "涨停",
    "跌停",
    "首板",
    "振幅",
    "融资融券",
    "股通",
    "msci",
    "富时",
    "标准普尔",
    "hs300",
    "上证50",
    "中证",
    "深成",
    "大盘股",
    "中盘股",
    "小盘股",
)

MarketOverviewStatus = Literal["PARTIAL", "UNKNOWN"]
MarketOverviewObservationMode = Literal[
    "INTRADAY_DELAYED_REFERENCE",
    "LATEST_COMPLETED_SESSION",
    "UNKNOWN",
]
MarketOverviewRankedSection = Literal[
    "industry_change",
    "industry_turnover",
    "concept_change",
    "concept_turnover",
    "equity_turnover",
]
MarketOverviewCoverageReason = Literal[
    "TOTAL_NON_POSITIVE",
    "RETURNED_NON_POSITIVE",
    "PAGE_SIZE_NON_POSITIVE",
    "TIMESTAMP_COUNT_MISMATCH",
    "PROJECTED_ROWS_MISMATCH",
    "RETURNED_COUNT_MISMATCH",
]


class MarketOverviewSourceError(RuntimeError):
    """The bounded provider read failed or returned an invalid envelope."""


class MarketOverviewDeadlineReachedError(MarketOverviewSourceError):
    """No provider call may start after the caller's deadline."""


class _HttpResponse(Protocol):
    text: str

    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class _HttpGet(Protocol):
    def __call__(
        self,
        url: str,
        *,
        params: dict[str, object],
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> _HttpResponse: ...


class _HttpSession(Protocol):
    headers: object

    def get(
        self,
        url: str,
        *,
        params: dict[str, object],
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> _HttpResponse: ...


class ThreadSafeEastmoneyOverviewHttpClient:
    """Serialize one source-owned Session and charge lock/throttle to timeout."""

    def __init__(
        self,
        *,
        session: _HttpSession | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
        min_interval_seconds: float = _HTTP_MIN_INTERVAL_SECONDS,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be non-negative")
        if session is None:
            owned_session = requests.Session()
            owned_session.headers.update(_HTTP_HEADERS)
            session = cast(_HttpSession, owned_session)
        self._session = session
        self._monotonic = monotonic_clock
        self._sleep = sleeper
        self._min_interval_seconds = min_interval_seconds
        self._lock = Lock()
        self._last_call_finished_at: float | None = None

    def __call__(
        self,
        url: str,
        *,
        params: dict[str, object],
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> _HttpResponse:
        started_at = self._monotonic()
        if timeout <= 0 or not self._lock.acquire(timeout=timeout):
            raise MarketOverviewDeadlineReachedError("CONSULTATION_DEADLINE_REACHED")
        try:
            remaining = timeout - (self._monotonic() - started_at)
            if remaining <= 0:
                raise MarketOverviewDeadlineReachedError("CONSULTATION_DEADLINE_REACHED")
            if self._last_call_finished_at is not None:
                wait_seconds = max(
                    0.0,
                    self._min_interval_seconds - (self._monotonic() - self._last_call_finished_at),
                )
                if wait_seconds >= remaining:
                    raise MarketOverviewDeadlineReachedError("CONSULTATION_DEADLINE_REACHED")
                if wait_seconds:
                    self._sleep(wait_seconds)
            remaining = timeout - (self._monotonic() - started_at)
            if remaining <= 0:
                raise MarketOverviewDeadlineReachedError("CONSULTATION_DEADLINE_REACHED")
            return self._session.get(url, params=params, timeout=remaining, headers=headers)
        finally:
            self._last_call_finished_at = self._monotonic()
            self._lock.release()


class MarketOverviewSource(Protocol):
    def fetch(
        self,
        *,
        deadline_at: datetime | None,
        prefer_completed_session: bool = False,
    ) -> EastmoneyMarketOverviewSnapshot: ...


@dataclass(frozen=True, slots=True)
class MarketOverviewRankedPageCoverage:
    section: MarketOverviewRankedSection
    total: int
    returned: int
    requested_page_size: int
    provider_timestamps: tuple[datetime | None, ...]
    valid_projected_rows: int


@dataclass(frozen=True, slots=True)
class MarketOverviewCoverageDiagnostic:
    """Content-free counters explaining why one ranked page was incomplete."""

    section: MarketOverviewRankedSection
    total: int
    returned: int
    expected_returned: int
    requested_page_size: int
    timestamp_count: int
    missing_timestamp_count: int
    valid_projected_rows: int
    reasons: tuple[MarketOverviewCoverageReason, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "section": self.section,
            "total": self.total,
            "returned": self.returned,
            "expected_returned": self.expected_returned,
            "requested_page_size": self.requested_page_size,
            "timestamp_count": self.timestamp_count,
            "missing_timestamp_count": self.missing_timestamp_count,
            "valid_projected_rows": self.valid_projected_rows,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class EastmoneyMarketOverviewSnapshot:
    """Raw provider rows retained only long enough for deterministic projection."""

    industries: tuple[Mapping[str, object], ...]
    concepts: tuple[Mapping[str, object], ...]
    indices: tuple[Mapping[str, object], ...]
    equities: tuple[Mapping[str, object], ...]
    captured_at: datetime
    ranked_page_coverage: tuple[MarketOverviewRankedPageCoverage, ...]
    indices_provider: Literal["TENCENT", "EASTMONEY"] = "EASTMONEY"

    def __post_init__(self) -> None:
        _require_aware("captured_at", self.captured_at)


@dataclass(frozen=True, slots=True)
class _FetchedRankedPage:
    rows: tuple[Mapping[str, object], ...]
    coverage: MarketOverviewRankedPageCoverage


@dataclass(frozen=True, slots=True)
class AshareMarketOverviewRequest:
    as_of: datetime | None = None
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.as_of is not None:
            _require_aware("as_of", self.as_of)
        if self.deadline_at is not None:
            _require_aware("deadline_at", self.deadline_at)


@dataclass(frozen=True, slots=True)
class MarketBoardObservation:
    code: str
    name: str
    change_pct: float
    turnover_yuan: float
    turnover_rate_pct: float | None
    advancers: int | None
    decliners: int | None
    leader_name: str | None
    leader_change_pct: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "change_pct": self.change_pct,
            "turnover_yuan": self.turnover_yuan,
            "turnover_rate_pct": self.turnover_rate_pct,
            "advancers": self.advancers,
            "decliners": self.decliners,
            "leader_name": self.leader_name,
            "leader_change_pct": self.leader_change_pct,
        }


@dataclass(frozen=True, slots=True)
class MarketEquityObservation:
    code: str
    name: str
    change_pct: float
    turnover_yuan: float
    turnover_rate_pct: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "change_pct": self.change_pct,
            "turnover_yuan": self.turnover_yuan,
            "turnover_rate_pct": self.turnover_rate_pct,
        }


@dataclass(frozen=True, slots=True)
class MarketIndexObservation:
    code: str
    name: str
    change_pct: float
    turnover_yuan: float
    advancers: int | None
    decliners: int | None
    unchanged: int | None
    level: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "level": self.level,
            "change_pct": self.change_pct,
            "turnover_yuan": self.turnover_yuan,
            "advancers": self.advancers,
            "decliners": self.decliners,
            "unchanged": self.unchanged,
        }


@dataclass(frozen=True, slots=True)
class AshareMarketBreadth:
    covered_instruments: int
    advancers: int
    decliners: int
    unchanged: int
    total_turnover_yuan: float

    def to_dict(self) -> dict[str, object]:
        return {
            "covered_instruments": self.covered_instruments,
            "advancers": self.advancers,
            "decliners": self.decliners,
            "unchanged": self.unchanged,
            "total_turnover_yuan": self.total_turnover_yuan,
        }


@dataclass(frozen=True, slots=True)
class AshareMarketOverviewResult:
    status: MarketOverviewStatus
    queried_at: datetime
    effective_trade_date: date | None
    observation_mode: MarketOverviewObservationMode
    session_phase: str
    provider_mode: Literal[
        "EASTMONEY_DELAYED_REFERENCE",
        "TENCENT_REALTIME_INDICES",
    ] = "EASTMONEY_DELAYED_REFERENCE"
    reference_only: bool = True
    realtime_eligible: bool = False
    provider_updated_at: datetime | None = None
    provider_observation_age_seconds: float | None = None
    breadth: AshareMarketBreadth | None = None
    major_indices: tuple[MarketIndexObservation, ...] = ()
    industry_leaders_by_change: tuple[MarketBoardObservation, ...] = ()
    industry_leaders_by_turnover: tuple[MarketBoardObservation, ...] = ()
    concept_leaders_by_change: tuple[MarketBoardObservation, ...] = ()
    concept_leaders_by_turnover: tuple[MarketBoardObservation, ...] = ()
    turnover_leaders: tuple[MarketEquityObservation, ...] = ()
    coverage_diagnostics: tuple[MarketOverviewCoverageDiagnostic, ...] = ()
    data_gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_aware("queried_at", self.queried_at)
        if self.provider_updated_at is not None:
            _require_aware("provider_updated_at", self.provider_updated_at)
        if not self.reference_only or self.realtime_eligible:
            raise ValueError("market overview must remain delayed reference-only evidence")
        if self.provider_observation_age_seconds is not None and (
            not isfinite(self.provider_observation_age_seconds)
            or self.provider_observation_age_seconds < 0
        ):
            raise ValueError("provider_observation_age_seconds must be finite and non-negative")

    def to_capability_value(self) -> dict[str, object]:
        return {
            "schema_version": "fin.a-share-market-overview/v1",
            "source_boundary": "a_share_current_market_overview",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "provider": (
                "tencent_realtime_indices+eastmoney_sections"
                if self.provider_mode == "TENCENT_REALTIME_INDICES"
                else "eastmoney"
            ),
            "status": self.status,
            "queried_at": self.queried_at.isoformat(),
            "effective_trade_date": (
                self.effective_trade_date.isoformat()
                if self.effective_trade_date is not None
                else None
            ),
            "observation_mode": self.observation_mode,
            "session_phase": self.session_phase,
            "provider_mode": self.provider_mode,
            "reference_only": self.reference_only,
            "realtime_eligible": self.realtime_eligible,
            "provider_updated_at": (
                self.provider_updated_at.isoformat()
                if self.provider_updated_at is not None
                else None
            ),
            "provider_observation_age_seconds": self.provider_observation_age_seconds,
            "breadth": self.breadth.to_dict() if self.breadth is not None else None,
            "coverage": {
                "venues": ["SSE", "SZSE"],
                "bj_included": False,
            },
            "major_indices": [item.to_dict() for item in self.major_indices],
            "industry": {
                "leaders_by_change": [item.to_dict() for item in self.industry_leaders_by_change],
                "leaders_by_turnover": [
                    item.to_dict() for item in self.industry_leaders_by_turnover
                ],
            },
            "concept": {
                "leaders_by_change": [item.to_dict() for item in self.concept_leaders_by_change],
                "leaders_by_turnover": [
                    item.to_dict() for item in self.concept_leaders_by_turnover
                ],
            },
            "turnover_leaders": [item.to_dict() for item in self.turnover_leaders],
            "coverage_diagnostics": [
                item.to_dict() for item in self.coverage_diagnostics
            ],
            "limitations": list(self.data_gaps),
        }


class EastmoneyCurrentMarketOverviewSource:
    """Fetch bounded Eastmoney cross-sections through the stable delay host."""

    def __init__(
        self,
        *,
        http_get: _HttpGet | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._http_get = http_get or ThreadSafeEastmoneyOverviewHttpClient()
        self._clock = clock or (lambda: datetime.now(UTC))

    def fetch(
        self,
        *,
        deadline_at: datetime | None,
        prefer_completed_session: bool = False,
    ) -> EastmoneyMarketOverviewSnapshot:
        industry_change = self._fetch_rows(
            section="industry_change",
            market_filter=_INDUSTRY_FILTER,
            fields=_BOARD_FIELDS,
            sort_field="f3",
            page_size=_BOARD_PAGE_SIZE,
            deadline_at=deadline_at,
        )
        industry_turnover = self._fetch_rows(
            section="industry_turnover",
            market_filter=_INDUSTRY_FILTER,
            fields=_BOARD_FIELDS,
            sort_field="f6",
            page_size=_BOARD_PAGE_SIZE,
            deadline_at=deadline_at,
        )
        concept_change = self._fetch_rows(
            section="concept_change",
            market_filter=_CONCEPT_FILTER,
            fields=_BOARD_FIELDS,
            sort_field="f3",
            page_size=_BOARD_PAGE_SIZE,
            deadline_at=deadline_at,
        )
        concept_turnover = self._fetch_rows(
            section="concept_turnover",
            market_filter=_CONCEPT_FILTER,
            fields=_BOARD_FIELDS,
            sort_field="f6",
            page_size=_BOARD_PAGE_SIZE,
            deadline_at=deadline_at,
        )
        indices, indices_provider = self._fetch_indices(
            deadline_at=deadline_at,
            prefer_completed_session=prefer_completed_session,
        )
        equity_turnover = self._fetch_rows(
            section="equity_turnover",
            market_filter=_A_SHARE_FILTER,
            fields=_EQUITY_FIELDS,
            sort_field="f6",
            page_size=_EQUITY_PAGE_SIZE,
            deadline_at=deadline_at,
        )
        industries = _merge_rows_by_code(
            industry_change.rows,
            industry_turnover.rows,
        )
        concepts = _merge_rows_by_code(
            concept_change.rows,
            concept_turnover.rows,
        )
        captured_at = self._clock()
        _require_aware("captured_at", captured_at)
        return EastmoneyMarketOverviewSnapshot(
            industries=industries,
            concepts=concepts,
            indices=indices,
            equities=equity_turnover.rows,
            indices_provider=indices_provider,
            captured_at=captured_at,
            ranked_page_coverage=(
                industry_change.coverage,
                industry_turnover.coverage,
                concept_change.coverage,
                concept_turnover.coverage,
                equity_turnover.coverage,
            ),
        )

    def _fetch_rows(
        self,
        *,
        section: MarketOverviewRankedSection,
        market_filter: str,
        fields: str,
        sort_field: str,
        page_size: int,
        deadline_at: datetime | None,
    ) -> _FetchedRankedPage:
        timeout = _bounded_timeout(
            now=self._clock(),
            deadline_at=deadline_at,
        )
        params: dict[str, object] = {
            "pn": "1",
            "pz": str(page_size),
            "po": "1",
            "np": "1",
            "ut": _EASTMONEY_UT,
            "fltt": "2",
            "invt": "2",
            "fid": sort_field,
            "fs": market_filter,
            "fields": fields,
        }
        try:
            response = self._http_get(
                _EASTMONEY_LIST_URL,
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except MarketOverviewDeadlineReachedError:
            raise
        except Exception as error:
            raise MarketOverviewSourceError("MARKET_OVERVIEW_SOURCE_UNAVAILABLE") from error
        if not isinstance(payload, Mapping):
            raise MarketOverviewSourceError("MARKET_OVERVIEW_PAYLOAD_INVALID")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise MarketOverviewSourceError("MARKET_OVERVIEW_PAYLOAD_INVALID")
        total = _non_negative_int(data.get("total"))
        raw_rows = data.get("diff")
        if total is None or not isinstance(raw_rows, (list, tuple)):
            raise MarketOverviewSourceError("MARKET_OVERVIEW_PAYLOAD_INVALID")
        rows: list[Mapping[str, object]] = []
        for raw_row in raw_rows[:page_size]:
            if not isinstance(raw_row, Mapping):
                raise MarketOverviewSourceError("MARKET_OVERVIEW_PAYLOAD_INVALID")
            rows.append(dict(raw_row))
        projected_rows = tuple(rows)
        return _FetchedRankedPage(
            rows=projected_rows,
            coverage=MarketOverviewRankedPageCoverage(
                section=section,
                total=total,
                returned=len(projected_rows),
                requested_page_size=page_size,
                provider_timestamps=tuple(_provider_timestamp(row) for row in projected_rows),
                valid_projected_rows=sum(
                    (_project_equity(row) if section == "equity_turnover" else _project_board(row))
                    is not None
                    for row in projected_rows
                ),
            ),
        )

    def _fetch_indices(
        self,
        *,
        deadline_at: datetime | None,
        prefer_completed_session: bool = False,
    ) -> tuple[tuple[Mapping[str, object], ...], Literal["TENCENT", "EASTMONEY"]]:
        """Major indices: Tencent realtime primary, Eastmoney delayed fallback.

        Tencent ``qt.gtimg.cn`` rows carry code/name/change/turnover with a
        realtime provider timestamp but no advancer/decliner breadth; those
        fields are left unset and the delayed Eastmoney host is used only
        when the realtime fetch itself fails.  After the session closes, the
        existing Eastmoney index endpoint is preferred so breadth is available
        without treating a delayed completed-session read as intraday data.
        """
        if prefer_completed_session:
            try:
                return (
                    self._fetch_eastmoney_indices(deadline_at=deadline_at),
                    "EASTMONEY",
                )
            except MarketOverviewDeadlineReachedError:
                raise
            except MarketOverviewSourceError:
                return (
                    self._fetch_tencent_indices(deadline_at=deadline_at),
                    "TENCENT",
                )
        try:
            return (
                self._fetch_tencent_indices(deadline_at=deadline_at),
                "TENCENT",
            )
        except MarketOverviewDeadlineReachedError:
            # Deadline is shared; a deadline failure must not silently fall
            # back into a second network call.
            raise
        except MarketOverviewSourceError:
            return (
                self._fetch_eastmoney_indices(deadline_at=deadline_at),
                "EASTMONEY",
            )

    def _fetch_tencent_indices(
        self,
        *,
        deadline_at: datetime | None,
    ) -> tuple[Mapping[str, object], ...]:
        timeout = _bounded_timeout(now=self._clock(), deadline_at=deadline_at)
        try:
            response = self._http_get(
                _TENCENT_INDEX_QUOTE_URL + _TENCENT_INDEX_CODES,
                params={},
                headers=_TENCENT_INDEX_HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            text = response.text
        except MarketOverviewDeadlineReachedError:
            raise
        except Exception as error:
            raise MarketOverviewSourceError("MARKET_OVERVIEW_SOURCE_UNAVAILABLE") from error
        rows = tuple(_parse_tencent_index_row(line) for line in text.splitlines())
        valid = tuple(row for row in rows if row is not None)
        if len(valid) != 4 or len(valid) != len(rows):
            raise MarketOverviewSourceError("MARKET_OVERVIEW_PAYLOAD_INVALID")
        return valid

    def _fetch_eastmoney_indices(
        self,
        *,
        deadline_at: datetime | None,
    ) -> tuple[Mapping[str, object], ...]:
        timeout = _bounded_timeout(now=self._clock(), deadline_at=deadline_at)
        params: dict[str, object] = {
            "secids": _MAJOR_INDEX_SECIDS,
            "ut": _EASTMONEY_UT,
            "fltt": "2",
            "invt": "2",
            "fields": _INDEX_FIELDS,
        }
        try:
            response = self._http_get(
                _EASTMONEY_INDEX_LIST_URL,
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except MarketOverviewDeadlineReachedError:
            raise
        except Exception as error:
            raise MarketOverviewSourceError("MARKET_OVERVIEW_SOURCE_UNAVAILABLE") from error
        if not isinstance(payload, Mapping):
            raise MarketOverviewSourceError("MARKET_OVERVIEW_PAYLOAD_INVALID")
        data = payload.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("diff"), (list, tuple)):
            raise MarketOverviewSourceError("MARKET_OVERVIEW_PAYLOAD_INVALID")
        rows = data["diff"]
        if any(not isinstance(item, Mapping) for item in rows):
            raise MarketOverviewSourceError("MARKET_OVERVIEW_PAYLOAD_INVALID")
        return tuple(dict(item) for item in rows if isinstance(item, Mapping))


class AshareMarketOverviewService:
    """Qualify provider rows against FIN's calendar and expose evidence only."""

    def __init__(
        self,
        *,
        source: MarketOverviewSource,
        calendar: AShareTradingCalendar,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._calendar = calendar
        self._clock = clock or (lambda: datetime.now(UTC))

    def read(self, request: AshareMarketOverviewRequest) -> AshareMarketOverviewResult:
        if not isinstance(request, AshareMarketOverviewRequest):
            raise TypeError("request must be AshareMarketOverviewRequest")
        read_started_at = self._clock()
        _require_aware("clock", read_started_at)
        calendar_anchor = request.as_of or read_started_at
        queried_at = calendar_anchor
        if request.deadline_at is not None and read_started_at >= request.deadline_at:
            return _unknown(
                queried_at=queried_at,
                data_gaps=("CONSULTATION_DEADLINE_REACHED",),
            )
        try:
            session = self._calendar.session_at(calendar_anchor)
        except (CalendarArtifactError, OSError, ValueError):
            return _unknown(
                queried_at=queried_at,
                data_gaps=("MARKET_OVERVIEW_CALENDAR_UNAVAILABLE",),
            )
        session_phase = session.phase.value
        if session.status is TradingSessionStatus.UNKNOWN:
            return _unknown(
                queried_at=queried_at,
                session_phase=session_phase,
                data_gaps=tuple(
                    dict.fromkeys((*session.data_gaps, "MARKET_OVERVIEW_CALENDAR_UNAVAILABLE"))
                ),
            )
        try:
            effective_trade_date, observation_mode = _effective_trade_date(
                calendar=self._calendar,
                phase=session.phase,
                trade_date=session.trade_date,
                known_at=calendar_anchor,
            )
        except (CalendarArtifactError, OSError, ValueError):
            return _unknown(
                queried_at=queried_at,
                session_phase=session_phase,
                data_gaps=("MARKET_OVERVIEW_CALENDAR_UNAVAILABLE",),
            )

        try:
            raw = self._source.fetch(
                deadline_at=request.deadline_at,
                prefer_completed_session=session_phase in {"AFTER_CLOSE", "CLOSED_DAY"},
            )
        except MarketOverviewDeadlineReachedError:
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                data_gaps=("CONSULTATION_DEADLINE_REACHED",),
            )
        except Exception:
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                data_gaps=(
                    "MARKET_OVERVIEW_SOURCE_UNAVAILABLE",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        fetch_returned_at = self._clock()
        _require_aware("clock", fetch_returned_at)
        current_capture_in_window = (
            read_started_at.astimezone(UTC)
            <= raw.captured_at.astimezone(UTC)
            <= fetch_returned_at.astimezone(UTC)
        )
        if request.as_of is None and current_capture_in_window:
            queried_at = raw.captured_at
        if request.deadline_at is not None and fetch_returned_at >= request.deadline_at:
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                data_gaps=("CONSULTATION_DEADLINE_REACHED",),
            )
        if request.as_of is None and not current_capture_in_window:
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                data_gaps=(
                    "MARKET_OVERVIEW_PAYLOAD_INVALID",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        evidence_cutoff = raw.captured_at if request.as_of is None else request.as_of
        queried_at = evidence_cutoff

        coverage_by_section = {coverage.section: coverage for coverage in raw.ranked_page_coverage}
        coverage_diagnostics = tuple(
            diagnostic
            for coverage in coverage_by_section.values()
            if (diagnostic := _ranked_page_coverage_diagnostic(coverage)).reasons
        )
        # 2026-08-31（BUG-002 结构性半边）：仅 PROJECTED_ROWS_MISMATCH 的分节
        # 不再整链拒绝——东财盘前把行情衍生列（f3/f6）置为占位 "-" 是每交易
        # 日的确定合法形态，行的标识/时间戳完好；是否降级由下方真实行投影
        # 裁定，计数器仅作诊断呈报。形状级原因（计数/时间戳数量失配等）仍是
        # provider 契约破损，整链拒绝。
        if (
            set(coverage_by_section) != _REQUIRED_RANKED_SECTIONS
            or len(coverage_by_section) != len(raw.ranked_page_coverage)
            or any(
                diagnostic.reasons != ("PROJECTED_ROWS_MISMATCH",)
                for diagnostic in coverage_diagnostics
            )
        ):
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                data_gaps=(
                    "MARKET_OVERVIEW_SECTION_COVERAGE_INVALID",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
                coverage_diagnostics=coverage_diagnostics,
            )
        projected_industries = tuple(_project_board(row) for row in raw.industries)
        projected_concepts = tuple(_project_board(row) for row in raw.concepts)
        projected_equities = tuple(_project_equity(row) for row in raw.equities)
        # 行投影失败的分节按源整体降级（显式 gap + 空榜），不整链拒绝、不静默
        # 丢行——该源全部行退出证据流（时间戳门/榜单/广度），其余证据（指数/
        # 广度）照常诚实呈现；分节级归因留在 coverage_diagnostics。
        industries_dropped = any(item is None for item in projected_industries)
        concepts_dropped = any(item is None for item in projected_concepts)
        equities_dropped = any(item is None for item in projected_equities)
        ranked_section_survives = {
            "industry_change": not industries_dropped,
            "industry_turnover": not industries_dropped,
            "concept_change": not concepts_dropped,
            "concept_turnover": not concepts_dropped,
            "equity_turnover": not equities_dropped,
        }
        ranked_timestamps = tuple(
            timestamp
            for section, coverage in coverage_by_section.items()
            if ranked_section_survives[section]
            for timestamp in coverage.provider_timestamps
        )
        raw_ranked_timestamps = tuple(
            _provider_timestamp(row)
            for row in (
                *(raw.industries if not industries_dropped else ()),
                *(raw.concepts if not concepts_dropped else ()),
                *(raw.equities if not equities_dropped else ()),
            )
        )
        provider_updated_at = _conservative_provider_update(
            ranked_timestamps,
            raw_ranked_timestamps,
            tuple(_provider_timestamp(row) for row in raw.indices),
        )
        if provider_updated_at is None:
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                data_gaps=(
                    "MARKET_OVERVIEW_TRADE_DATE_MISMATCH",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        if any(
            any(
                timestamp is None or timestamp.astimezone(_CN_TZ).date() != effective_trade_date
                for timestamp in coverage.provider_timestamps
            )
            for coverage in coverage_by_section.values()
            if ranked_section_survives[coverage.section]
        ) or any(
            timestamp is None or timestamp.astimezone(_CN_TZ).date() != effective_trade_date
            for timestamp in raw_ranked_timestamps
        ):
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                provider_updated_at=provider_updated_at,
                data_gaps=(
                    "MARKET_OVERVIEW_SECTION_TRADE_DATE_MISMATCH",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        raw_index_codes = tuple(
            code for row in raw.indices if (code := _bounded_text(row.get("f12"), max_chars=32))
        )
        if (
            len(raw_index_codes) != len(_REQUIRED_MAJOR_INDEX_CODES)
            or set(raw_index_codes) != _REQUIRED_MAJOR_INDEX_CODES
        ):
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                provider_updated_at=provider_updated_at,
                data_gaps=(
                    "MARKET_OVERVIEW_INDEX_COVERAGE_INVALID",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        if any(_provider_trade_date(row) != effective_trade_date for row in raw.indices):
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                provider_updated_at=provider_updated_at,
                data_gaps=(
                    "MARKET_OVERVIEW_INDEX_TRADE_DATE_MISMATCH",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        all_provider_timestamps = (
            *ranked_timestamps,
            *raw_ranked_timestamps,
            *(_provider_timestamp(row) for row in raw.indices),
        )
        if any(
            timestamp is None or timestamp.astimezone(UTC) > evidence_cutoff.astimezone(UTC)
            for timestamp in all_provider_timestamps
        ):
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                provider_updated_at=provider_updated_at,
                data_gaps=(
                    "MARKET_OVERVIEW_PROVIDER_TIME_AFTER_QUERY",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        qualified_timestamps = tuple(
            timestamp for timestamp in all_provider_timestamps if timestamp is not None
        )
        if observation_mode == "LATEST_COMPLETED_SESSION" and any(
            timestamp.astimezone(_CN_TZ).time() < _A_SHARE_CLOSE_TIME
            for timestamp in qualified_timestamps
        ):
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                provider_updated_at=provider_updated_at,
                data_gaps=(
                    "MARKET_OVERVIEW_SESSION_INCOMPLETE",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        provider_observation_age_seconds = (
            evidence_cutoff.astimezone(UTC) - provider_updated_at.astimezone(UTC)
        ).total_seconds()
        if (
            observation_mode == "INTRADAY_DELAYED_REFERENCE"
            and provider_observation_age_seconds > _INTRADAY_MAX_REFERENCE_AGE_SECONDS
        ):
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                provider_updated_at=provider_updated_at,
                data_gaps=(
                    "MARKET_OVERVIEW_INTRADAY_REFERENCE_STALE",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )

        projected_indices = tuple(_project_index(row) for row in raw.indices)
        if any(index is None for index in projected_indices) or len(projected_indices) != len(
            _REQUIRED_MAJOR_INDEX_CODES
        ):
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                provider_updated_at=provider_updated_at,
                data_gaps=(
                    "MARKET_OVERVIEW_INDEX_PAYLOAD_INVALID",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )
        industries = (
            ()
            if industries_dropped
            else tuple(board for board in projected_industries if board is not None)
        )
        concepts = (
            ()
            if concepts_dropped
            else tuple(
                concept
                for concept in projected_concepts
                if concept is not None and not _is_non_thematic_concept(concept.name)
            )
        )
        equities = (
            ()
            if equities_dropped
            else tuple(equity for equity in projected_equities if equity is not None)
        )
        indices = tuple(index for index in projected_indices if index is not None)
        breadth_indices = {item.code: item for item in indices if item.code in {"000001", "399001"}}
        # 空分节源仅在被显式降级时合法（BUG-002 结构性半边：盘前占位形态下
        # 榜单整体缺席但仍需诚实呈现指数证据）；未降级而空 = 载荷残缺，拒绝。
        if (
            (not industries and not industries_dropped)
            or (not concepts and not concepts_dropped)
            or (not equities and not equities_dropped)
            or set(breadth_indices) != {"000001", "399001"}
        ):
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                provider_updated_at=provider_updated_at,
                data_gaps=(
                    "MARKET_OVERVIEW_PAYLOAD_INVALID",
                    "MARKET_OVERVIEW_UNAVAILABLE",
                ),
            )

        ordered_change_industries = tuple(
            sorted(industries, key=lambda item: item.change_pct, reverse=True)[:_LEADER_LIMIT]
        )
        ordered_turnover_industries = tuple(
            sorted(industries, key=lambda item: item.turnover_yuan, reverse=True)[:_LEADER_LIMIT]
        )
        ordered_change_concepts = tuple(
            sorted(concepts, key=lambda item: item.change_pct, reverse=True)[:_LEADER_LIMIT]
        )
        ordered_turnover_concepts = tuple(
            sorted(concepts, key=lambda item: item.turnover_yuan, reverse=True)[:_LEADER_LIMIT]
        )
        turnover_leaders = tuple(
            sorted(equities, key=lambda item: item.turnover_yuan, reverse=True)[
                :_TURNOVER_STOCK_LIMIT
            ]
        )
        shanghai = breadth_indices["000001"]
        shenzhen = breadth_indices["399001"]
        breadth_gaps: list[str] = []
        if (
            shanghai.advancers is None
            or shanghai.decliners is None
            or shanghai.unchanged is None
            or shenzhen.advancers is None
            or shenzhen.decliners is None
            or shenzhen.unchanged is None
        ):
            # Tencent realtime rows carry no advancer/decliner breadth: keep
            # the realtime indices, degrade the breadth with an explicit gap.
            breadth = None
            breadth_gaps.append("MARKET_OVERVIEW_BREADTH_UNAVAILABLE")
        else:
            breadth = AshareMarketBreadth(
                covered_instruments=(
                    shanghai.advancers
                    + shanghai.decliners
                    + shanghai.unchanged
                    + shenzhen.advancers
                    + shenzhen.decliners
                    + shenzhen.unchanged
                ),
                advancers=shanghai.advancers + shenzhen.advancers,
                decliners=shanghai.decliners + shenzhen.decliners,
                unchanged=shanghai.unchanged + shenzhen.unchanged,
                total_turnover_yuan=shanghai.turnover_yuan + shenzhen.turnover_yuan,
            )
        if _deadline_reached(clock=self._clock, deadline_at=request.deadline_at):
            return _unknown(
                queried_at=queried_at,
                effective_trade_date=effective_trade_date,
                observation_mode=observation_mode,
                session_phase=session_phase,
                provider_updated_at=provider_updated_at,
                data_gaps=("CONSULTATION_DEADLINE_REACHED",),
            )
        return AshareMarketOverviewResult(
            status="PARTIAL",
            queried_at=queried_at,
            effective_trade_date=effective_trade_date,
            observation_mode=observation_mode,
            session_phase=session_phase,
            provider_mode=(
                "TENCENT_REALTIME_INDICES"
                if raw.indices_provider == "TENCENT"
                else "EASTMONEY_DELAYED_REFERENCE"
            ),
            provider_updated_at=provider_updated_at,
            provider_observation_age_seconds=provider_observation_age_seconds,
            breadth=breadth,
            major_indices=indices,
            industry_leaders_by_change=ordered_change_industries,
            industry_leaders_by_turnover=ordered_turnover_industries,
            concept_leaders_by_change=ordered_change_concepts,
            concept_leaders_by_turnover=ordered_turnover_concepts,
            turnover_leaders=turnover_leaders,
            # 降级分节的归因必须随结果出门（干净读取时为空元组，输出不变）。
            coverage_diagnostics=coverage_diagnostics,
            data_gaps=tuple(
                dict.fromkeys(
                    (
                        *(
                            ["MARKET_OVERVIEW_SECTION_ROWS_UNPROJECTABLE"]
                            if industries_dropped or concepts_dropped or equities_dropped
                            else []
                        ),
                        *breadth_gaps,
                        "MARKET_OVERVIEW_SINGLE_SOURCE",
                        "MARKET_OVERVIEW_PERSISTENCE_NOT_EVALUATED",
                        "MARKET_OVERVIEW_BJ_NOT_COVERED",
                        "MARKET_OVERVIEW_PROVIDER_CONCEPT_TAXONOMY_LIMITED",
                        "MARKET_OVERVIEW_DELAYED_REFERENCE",
                    )
                )
            ),
        )


def build_default_a_share_market_overview(
    *,
    clock: Callable[[], datetime] | None = None,
) -> AshareMarketOverviewService:
    project_root = Path(__file__).resolve().parents[2]
    calendar = AShareTradingCalendar.from_file(
        project_root / "config" / "market" / "a_share_calendar_2026.json"
    )
    return AshareMarketOverviewService(
        source=EastmoneyCurrentMarketOverviewSource(clock=clock),
        calendar=calendar,
        clock=clock,
    )


def _merge_rows_by_code(
    *groups: tuple[Mapping[str, object], ...],
) -> tuple[Mapping[str, object], ...]:
    merged: dict[str, Mapping[str, object]] = {}
    for rows in groups:
        for row in rows:
            code = _bounded_text(row.get("f12"), max_chars=32)
            if code and code not in merged:
                merged[code] = row
    return tuple(merged.values())


def _bounded_timeout(*, now: datetime, deadline_at: datetime | None) -> float:
    _require_aware("clock", now)
    if deadline_at is None:
        return _HTTP_TIMEOUT_SECONDS
    _require_aware("deadline_at", deadline_at)
    remaining = (deadline_at - now).total_seconds()
    if remaining <= 0:
        raise MarketOverviewDeadlineReachedError("CONSULTATION_DEADLINE_REACHED")
    return min(_HTTP_TIMEOUT_SECONDS, remaining)


def _deadline_reached(
    *,
    clock: Callable[[], datetime],
    deadline_at: datetime | None,
) -> bool:
    if deadline_at is None:
        return False
    now = clock()
    _require_aware("clock", now)
    return now >= deadline_at


def _ranked_page_coverage_diagnostic(
    coverage: MarketOverviewRankedPageCoverage,
) -> MarketOverviewCoverageDiagnostic:
    expected_returned = min(coverage.total, coverage.requested_page_size)
    reasons: list[MarketOverviewCoverageReason] = []
    if coverage.total <= 0:
        reasons.append("TOTAL_NON_POSITIVE")
    if coverage.returned <= 0:
        reasons.append("RETURNED_NON_POSITIVE")
    if coverage.requested_page_size <= 0:
        reasons.append("PAGE_SIZE_NON_POSITIVE")
    if len(coverage.provider_timestamps) != coverage.returned:
        reasons.append("TIMESTAMP_COUNT_MISMATCH")
    if coverage.valid_projected_rows != coverage.returned:
        reasons.append("PROJECTED_ROWS_MISMATCH")
    if coverage.returned != expected_returned:
        reasons.append("RETURNED_COUNT_MISMATCH")
    return MarketOverviewCoverageDiagnostic(
        section=coverage.section,
        total=coverage.total,
        returned=coverage.returned,
        expected_returned=expected_returned,
        requested_page_size=coverage.requested_page_size,
        timestamp_count=len(coverage.provider_timestamps),
        missing_timestamp_count=sum(
            timestamp is None for timestamp in coverage.provider_timestamps
        ),
        valid_projected_rows=coverage.valid_projected_rows,
        reasons=tuple(reasons),
    )


def _effective_trade_date(
    *,
    calendar: AShareTradingCalendar,
    phase: TradingSessionPhase,
    trade_date: date,
    known_at: datetime,
) -> tuple[date, MarketOverviewObservationMode]:
    if phase in {
        TradingSessionPhase.CONTINUOUS_AM,
        TradingSessionPhase.BREAK,
        TradingSessionPhase.CONTINUOUS_PM,
    }:
        return trade_date, "INTRADAY_DELAYED_REFERENCE"
    if phase is TradingSessionPhase.AFTER_CLOSE:
        return trade_date, "LATEST_COMPLETED_SESSION"
    if phase in {TradingSessionPhase.PRE_OPEN, TradingSessionPhase.CLOSED_DAY}:
        previous = calendar.previous_open_date(before=trade_date, known_at=known_at)
        return previous.previous_open_date, "LATEST_COMPLETED_SESSION"
    raise CalendarArtifactError("MARKET_OVERVIEW_CALENDAR_UNAVAILABLE")


def _conservative_provider_update(
    *timestamp_groups: tuple[datetime | None, ...],
) -> datetime | None:
    timestamps = tuple(
        timestamp for group in timestamp_groups for timestamp in group if timestamp is not None
    )
    return min(timestamps) if timestamps else None


def _provider_timestamp(row: Mapping[str, object]) -> datetime | None:
    epoch = _finite_float(row.get("f124"))
    if epoch is None or epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_tencent_index_row(line: str) -> Mapping[str, object] | None:
    """Map one ``qt.gtimg.cn`` index line onto the Eastmoney row shape.

    Tencent payload fields: [1]=name [3]=price [31]=change [32]=change_pct,
    [36]=volume, [37]=amount in 10,000 yuan, and [30]=YYYYMMDDHHMMSS
    timestamp.  No advancer/decliner breadth exists on the realtime quote, so
    f104/f105/f106 stay absent and the overview breadth degrades with an
    explicit gap.
    """
    if "=" not in line:
        return None
    body = line.split("=", 1)[1].strip()
    # 真实 qt.gtimg.cn 行格式为 v_sh000001="..." 且行尾带分号；按最后一个
    # 引号截取 payload，容忍尾部分号与空白。
    if not body.startswith('"'):
        return None
    payload_end = body.rfind('"')
    if payload_end <= 1:
        return None
    parts = body[1:payload_end].split("~")
    if len(parts) < 37:
        return None
    code_raw = parts[2] if len(parts) > 2 else ""
    timestamp_raw = parts[30] if len(parts) > 30 else ""
    level = _finite_float(parts[3] if len(parts) > 3 else None)
    change_pct = _finite_float(parts[32] if len(parts) > 32 else None)
    amount_10k_yuan = _finite_float(parts[37] if len(parts) > 37 else None)
    turnover_yuan = (
        amount_10k_yuan * 10_000
        if amount_10k_yuan is not None and amount_10k_yuan >= 0
        else None
    )
    timestamp = _parse_tencent_index_timestamp(timestamp_raw)
    if (
        not code_raw
        or change_pct is None
        or turnover_yuan is None
        or turnover_yuan < 0
        or timestamp is None
    ):
        return None
    return {
        "f12": code_raw,
        "f14": _bounded_text(parts[1] if len(parts) > 1 else "", max_chars=80),
        "f2": level,
        "f3": change_pct,
        "f6": turnover_yuan,
        "f124": timestamp.timestamp(),
    }


def _parse_tencent_index_timestamp(value: str) -> datetime | None:
    """Parse Tencent's compact ``YYYYMMDDHHMMSS`` quote timestamp as CST."""
    if not value or len(value) != 14 or not value.isdigit():
        return None
    try:
        naive = datetime.strptime(value, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=_CN_TZ).astimezone(UTC)


def _provider_trade_date(row: Mapping[str, object]) -> date | None:
    timestamp = _provider_timestamp(row)
    return timestamp.astimezone(_CN_TZ).date() if timestamp is not None else None


def _project_board(row: Mapping[str, object]) -> MarketBoardObservation | None:
    code = _bounded_text(row.get("f12"), max_chars=32)
    name = _bounded_text(row.get("f14"), max_chars=80)
    change_pct = _finite_float(row.get("f3"))
    turnover_yuan = _finite_float(row.get("f6"))
    if not code or not name or change_pct is None or turnover_yuan is None or turnover_yuan < 0:
        return None
    return MarketBoardObservation(
        code=code,
        name=name,
        change_pct=change_pct,
        turnover_yuan=turnover_yuan,
        turnover_rate_pct=_finite_float(row.get("f8")),
        advancers=_non_negative_int(row.get("f104")),
        decliners=_non_negative_int(row.get("f105")),
        leader_name=_bounded_text(row.get("f128"), max_chars=80) or None,
        leader_change_pct=_finite_float(row.get("f136")),
    )


def _project_equity(row: Mapping[str, object]) -> MarketEquityObservation | None:
    code = _bounded_text(row.get("f12"), max_chars=32)
    name = _bounded_text(row.get("f14"), max_chars=80)
    change_pct = _finite_float(row.get("f3"))
    turnover_yuan = _finite_float(row.get("f6"))
    if not code or not name or change_pct is None or turnover_yuan is None or turnover_yuan < 0:
        return None
    return MarketEquityObservation(
        code=code,
        name=name,
        change_pct=change_pct,
        turnover_yuan=turnover_yuan,
        turnover_rate_pct=_finite_float(row.get("f8")),
    )


def _project_index(row: Mapping[str, object]) -> MarketIndexObservation | None:
    code = _bounded_text(row.get("f12"), max_chars=32)
    name = _bounded_text(row.get("f14"), max_chars=80)
    level = _finite_float(row.get("f2"))
    change_pct = _finite_float(row.get("f3"))
    turnover_yuan = _finite_float(row.get("f6"))
    # Tencent realtime rows carry no advancer/decliner breadth; a missing
    # breadth only degrades the overview breadth, never the index itself.
    advancers = _non_negative_int(row.get("f104"))
    decliners = _non_negative_int(row.get("f105"))
    unchanged = _non_negative_int(row.get("f106"))
    if (
        not code
        or not name
        or change_pct is None
        or turnover_yuan is None
        or turnover_yuan < 0
    ):
        return None
    return MarketIndexObservation(
        code=code,
        name=name,
        change_pct=change_pct,
        turnover_yuan=turnover_yuan,
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        level=level if level is not None and level > 0 else None,
    )


def _is_non_thematic_concept(name: str) -> bool:
    normalized = name.casefold()
    return any(marker in normalized for marker in _NON_THEMATIC_CONCEPT_MARKERS)


def _unknown(
    *,
    queried_at: datetime,
    data_gaps: tuple[str, ...],
    effective_trade_date: date | None = None,
    observation_mode: MarketOverviewObservationMode = "UNKNOWN",
    session_phase: str = "UNKNOWN",
    provider_updated_at: datetime | None = None,
    coverage_diagnostics: tuple[MarketOverviewCoverageDiagnostic, ...] = (),
) -> AshareMarketOverviewResult:
    return AshareMarketOverviewResult(
        status="UNKNOWN",
        queried_at=queried_at,
        effective_trade_date=effective_trade_date,
        observation_mode=observation_mode,
        session_phase=session_phase,
        provider_updated_at=provider_updated_at,
        coverage_diagnostics=coverage_diagnostics,
        data_gaps=tuple(dict.fromkeys(data_gaps)),
    )


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if isfinite(result) else None


def _non_negative_int(value: object) -> int | None:
    number = _finite_float(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _bounded_text(value: object, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())
    return normalized[:max_chars]


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = [
    "AshareMarketBreadth",
    "AshareMarketOverviewRequest",
    "AshareMarketOverviewResult",
    "AshareMarketOverviewService",
    "EastmoneyCurrentMarketOverviewSource",
    "EastmoneyMarketOverviewSnapshot",
    "MarketBoardObservation",
    "MarketEquityObservation",
    "MarketIndexObservation",
    "MarketOverviewDeadlineReachedError",
    "MarketOverviewCoverageReason",
    "MarketOverviewCoverageDiagnostic",
    "MarketOverviewRankedPageCoverage",
    "ThreadSafeEastmoneyOverviewHttpClient",
    "build_default_a_share_market_overview",
]
