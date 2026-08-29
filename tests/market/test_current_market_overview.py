from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from fin_analyse.market.current_overview import (
    AshareMarketOverviewRequest,
    AshareMarketOverviewService,
    EastmoneyCurrentMarketOverviewSource,
    EastmoneyMarketOverviewSnapshot,
    MarketOverviewDeadlineReachedError,
    MarketOverviewRankedPageCoverage,
    ThreadSafeEastmoneyOverviewHttpClient,
)
from fin_analyse.market.trading_calendar import AShareTradingCalendar

_CN_TZ = ZoneInfo("Asia/Shanghai")
_CALENDAR_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "market" / "a_share_calendar_2026.json"
)
_FRIDAY_CLOSE = datetime(2026, 7, 24, 15, 30, tzinfo=_CN_TZ)
_SUNDAY_QUERY = datetime(2026, 7, 26, 19, 33, tzinfo=_CN_TZ)


def test_request_distinguishes_current_from_aware_point_in_time() -> None:
    assert AshareMarketOverviewRequest().as_of is None
    with pytest.raises(ValueError, match="as_of must be timezone-aware"):
        AshareMarketOverviewRequest(as_of=datetime(2026, 7, 24, 10, 0))


def _board(
    code: str,
    name: str,
    *,
    change_pct: float,
    turnover: float,
    advancers: int,
    decliners: int,
    leader: str,
) -> dict[str, object]:
    return {
        "f3": change_pct,
        "f6": turnover,
        "f8": 3.5,
        "f12": code,
        "f14": name,
        "f104": advancers,
        "f105": decliners,
        "f124": int(_FRIDAY_CLOSE.timestamp()),
        "f128": leader,
        "f136": 10.0,
    }


def _equity(code: str, name: str, *, change_pct: float, turnover: float) -> dict[str, object]:
    return {
        "f3": change_pct,
        "f6": turnover,
        "f8": 2.0,
        "f12": code,
        "f14": name,
        "f124": int(_FRIDAY_CLOSE.timestamp()),
    }


def _index(
    code: str,
    name: str,
    *,
    change_pct: float,
    turnover: float,
    advancers: int,
    decliners: int,
    unchanged: int,
) -> dict[str, object]:
    return {
        "f3": change_pct,
        "f6": turnover,
        "f12": code,
        "f14": name,
        "f104": advancers,
        "f105": decliners,
        "f106": unchanged,
        "f124": int(_FRIDAY_CLOSE.timestamp()),
    }


@dataclass
class _RecordingSource:
    snapshot: EastmoneyMarketOverviewSnapshot
    calls: list[datetime | None] = field(default_factory=list)

    def fetch(self, *, deadline_at: datetime | None) -> EastmoneyMarketOverviewSnapshot:
        self.calls.append(deadline_at)
        return self.snapshot


def _snapshot(*, provider_updated_at: datetime = _FRIDAY_CLOSE) -> EastmoneyMarketOverviewSnapshot:
    provider_epoch = int(provider_updated_at.timestamp())
    industries = (
        _board(
            "BK1326",
            "半导体设备",
            change_pct=3.18,
            turnover=64_500_000_000,
            advancers=20,
            decliners=4,
            leader="至纯科技",
        ),
        _board(
            "BK1328",
            "集成电路封测",
            change_pct=1.87,
            turnover=51_800_000_000,
            advancers=7,
            decliners=6,
            leader="华岭股份",
        ),
    )
    concepts = (
        _board(
            "BK1675",
            "历史新高",
            change_pct=2.96,
            turnover=12_800_000_000,
            advancers=5,
            decliners=1,
            leader="示例公司",
        ),
        _board(
            "BK1152",
            "高带宽内存",
            change_pct=0.79,
            turnover=74_000_000_000,
            advancers=14,
            decliners=14,
            leader="至纯科技",
        ),
    )
    equities = (
        _equity("603986", "兆易创新", change_pct=1.12, turnover=22_800_000_000),
        _equity("002156", "通富微电", change_pct=9.77, turnover=19_800_000_000),
        _equity("300308", "中际旭创", change_pct=-2.43, turnover=22_900_000_000),
    )
    indices = (
        _index(
            "000001",
            "上证指数",
            change_pct=-1.61,
            turnover=30_000_000_000,
            advancers=1,
            decliners=1,
            unchanged=0,
        ),
        _index(
            "399001",
            "深证成指",
            change_pct=-2.47,
            turnover=35_500_000_000,
            advancers=1,
            decliners=0,
            unchanged=0,
        ),
        _index(
            "399006",
            "创业板指",
            change_pct=-2.65,
            turnover=0,
            advancers=0,
            decliners=0,
            unchanged=0,
        ),
        _index(
            "000688",
            "科创50",
            change_pct=-0.14,
            turnover=0,
            advancers=0,
            decliners=0,
            unchanged=0,
        ),
    )
    for row in (*industries, *concepts, *indices, *equities):
        row["f124"] = provider_epoch
    return EastmoneyMarketOverviewSnapshot(
        industries=industries,
        concepts=concepts,
        indices=indices,
        equities=equities,
        captured_at=_SUNDAY_QUERY,
        ranked_page_coverage=(
            _coverage("industry_change", industries, provider_updated_at),
            _coverage("industry_turnover", industries, provider_updated_at),
            _coverage("concept_change", concepts, provider_updated_at),
            _coverage("concept_turnover", concepts, provider_updated_at),
            _coverage("equity_turnover", equities, provider_updated_at),
        ),
    )


def _coverage(
    section: str,
    rows: tuple[dict[str, object], ...],
    provider_updated_at: datetime,
) -> MarketOverviewRankedPageCoverage:
    return MarketOverviewRankedPageCoverage(
        section=section,
        total=len(rows),
        returned=len(rows),
        requested_page_size=100,
        provider_timestamps=(provider_updated_at.astimezone(UTC),) * len(rows),
        valid_projected_rows=len(rows),
    )


def test_weekend_query_uses_latest_completed_trading_day_and_names_evidence_leaders() -> None:
    source = _RecordingSource(_snapshot())
    service = AshareMarketOverviewService(
        source=source,
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: _SUNDAY_QUERY,
    )

    result = service.read(
        AshareMarketOverviewRequest(
            as_of=_SUNDAY_QUERY,
            deadline_at=_SUNDAY_QUERY + timedelta(seconds=10),
        )
    )

    assert result.status == "PARTIAL"
    assert result.effective_trade_date == date(2026, 7, 24)
    assert result.observation_mode == "LATEST_COMPLETED_SESSION"
    assert result.session_phase == "CLOSED_DAY"
    assert result.provider_mode == "EASTMONEY_DELAYED_REFERENCE"
    assert result.reference_only is True
    assert result.realtime_eligible is False
    assert result.provider_updated_at is not None
    assert result.provider_observation_age_seconds is not None
    assert result.provider_observation_age_seconds > 0
    assert result.provider_updated_at.astimezone(_CN_TZ).date() == date(2026, 7, 24)
    assert result.breadth is not None
    assert result.breadth.covered_instruments == 3
    assert result.breadth.advancers == 2
    assert result.breadth.decliners == 1
    assert result.breadth.total_turnover_yuan == 65_500_000_000
    assert [item.name for item in result.industry_leaders_by_change] == [
        "半导体设备",
        "集成电路封测",
    ]
    assert result.industry_leaders_by_turnover[0].name == "半导体设备"
    assert result.concept_leaders_by_turnover[0].name == "高带宽内存"
    assert [item.name for item in result.concept_leaders_by_change] == ["高带宽内存"]
    assert result.turnover_leaders[0].name == "中际旭创"
    assert result.data_gaps == (
        "MARKET_OVERVIEW_SINGLE_SOURCE",
        "MARKET_OVERVIEW_PERSISTENCE_NOT_EVALUATED",
        "MARKET_OVERVIEW_BJ_NOT_COVERED",
        "MARKET_OVERVIEW_PROVIDER_CONCEPT_TAXONOMY_LIMITED",
        "MARKET_OVERVIEW_DELAYED_REFERENCE",
    )
    assert len(source.calls) == 1


@pytest.mark.parametrize("capture_offset_seconds", (0, 2, 3))
def test_current_query_accepts_evidence_captured_within_inclusive_local_read_window(
    capture_offset_seconds: int,
) -> None:
    read_started_at = datetime(2026, 7, 24, 10, 0, tzinfo=_CN_TZ)
    captured_at = read_started_at + timedelta(seconds=capture_offset_seconds)
    provider_updated_at = captured_at - timedelta(seconds=1)
    fetch_returned_at = read_started_at + timedelta(seconds=3)
    clock_values = iter((read_started_at, fetch_returned_at))
    service = AshareMarketOverviewService(
        source=_RecordingSource(
            replace(
                _snapshot(provider_updated_at=provider_updated_at),
                captured_at=captured_at,
            )
        ),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: next(clock_values),
    )

    result = service.read(AshareMarketOverviewRequest())

    assert result.status == "PARTIAL"
    assert result.queried_at == captured_at
    assert result.provider_updated_at == provider_updated_at.astimezone(UTC)
    assert result.provider_observation_age_seconds == 1


def test_current_query_rejects_provider_timestamp_after_capture() -> None:
    read_started_at = datetime(2026, 7, 24, 10, 0, tzinfo=_CN_TZ)
    captured_at = read_started_at + timedelta(seconds=2)
    provider_updated_at = captured_at + timedelta(seconds=1)
    fetch_returned_at = captured_at + timedelta(seconds=2)
    clock_values = iter((read_started_at, fetch_returned_at))
    service = AshareMarketOverviewService(
        source=_RecordingSource(
            replace(
                _snapshot(provider_updated_at=provider_updated_at),
                captured_at=captured_at,
            )
        ),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: next(clock_values),
    )

    result = service.read(AshareMarketOverviewRequest())

    assert result.status == "UNKNOWN"
    assert result.queried_at == captured_at
    assert result.provider_observation_age_seconds is None
    assert "MARKET_OVERVIEW_PROVIDER_TIME_AFTER_QUERY" in result.data_gaps


@pytest.mark.parametrize("capture_offset_seconds", (-1, 4))
def test_current_query_rejects_capture_outside_local_read_window(
    capture_offset_seconds: int,
) -> None:
    read_started_at = datetime(2026, 7, 24, 10, 0, tzinfo=_CN_TZ)
    fetch_returned_at = read_started_at + timedelta(seconds=3)
    captured_at = read_started_at + timedelta(seconds=capture_offset_seconds)
    clock_values = iter((read_started_at, fetch_returned_at))
    service = AshareMarketOverviewService(
        source=_RecordingSource(
            replace(
                _snapshot(provider_updated_at=read_started_at),
                captured_at=captured_at,
            )
        ),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: next(clock_values),
    )

    result = service.read(AshareMarketOverviewRequest())

    assert result.status == "UNKNOWN"
    assert result.queried_at == read_started_at
    assert result.data_gaps == (
        "MARKET_OVERVIEW_PAYLOAD_INVALID",
        "MARKET_OVERVIEW_UNAVAILABLE",
    )


def test_current_query_measures_intraday_age_at_capture() -> None:
    read_started_at = datetime(2026, 7, 24, 10, 30, tzinfo=_CN_TZ)
    provider_updated_at = read_started_at - timedelta(minutes=30) + timedelta(seconds=1)
    captured_at = read_started_at + timedelta(seconds=2)
    fetch_returned_at = captured_at + timedelta(seconds=1)
    clock_values = iter((read_started_at, fetch_returned_at))
    service = AshareMarketOverviewService(
        source=_RecordingSource(
            replace(
                _snapshot(provider_updated_at=provider_updated_at),
                captured_at=captured_at,
            )
        ),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: next(clock_values),
    )

    result = service.read(AshareMarketOverviewRequest())

    assert result.status == "UNKNOWN"
    assert result.queried_at == captured_at
    assert result.provider_observation_age_seconds is None
    assert "MARKET_OVERVIEW_INTRADAY_REFERENCE_STALE" in result.data_gaps


def test_provider_trade_date_mismatch_fails_closed_instead_of_calling_old_data_current() -> None:
    source = _RecordingSource(
        _snapshot(provider_updated_at=datetime(2026, 7, 23, 15, 30, tzinfo=_CN_TZ))
    )
    service = AshareMarketOverviewService(
        source=source,
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
    )

    result = service.read(AshareMarketOverviewRequest(as_of=_SUNDAY_QUERY))

    assert result.status == "UNKNOWN"
    assert result.effective_trade_date == date(2026, 7, 24)
    assert result.breadth is None
    assert "MARKET_OVERVIEW_SECTION_TRADE_DATE_MISMATCH" in result.data_gaps


def test_mixed_section_trade_dates_fail_closed_even_when_indices_are_current() -> None:
    snapshot = _snapshot()
    stale_date = date(2026, 7, 23)
    stale_coverages = tuple(
        replace(
            coverage,
            provider_timestamps=(
                datetime.combine(
                    stale_date,
                    datetime.min.time(),
                    tzinfo=_CN_TZ,
                ).astimezone(UTC),
            )
            * coverage.returned,
        )
        if coverage.section == "industry_change"
        else coverage
        for coverage in snapshot.ranked_page_coverage
    )
    service = AshareMarketOverviewService(
        source=_RecordingSource(replace(snapshot, ranked_page_coverage=stale_coverages)),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
    )

    result = service.read(AshareMarketOverviewRequest(as_of=_SUNDAY_QUERY))

    assert result.status == "UNKNOWN"
    assert result.breadth is None
    assert "MARKET_OVERVIEW_SECTION_TRADE_DATE_MISMATCH" in result.data_gaps


def test_completed_session_rejects_section_rows_that_never_reached_the_close() -> None:
    snapshot = _snapshot()
    early = datetime(2026, 7, 24, 9, 31, tzinfo=_CN_TZ)
    industries = tuple(dict(row, f124=int(early.timestamp())) for row in snapshot.industries)
    service = AshareMarketOverviewService(
        source=_RecordingSource(
            replace(
                snapshot,
                industries=industries,
            )
        ),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
    )

    result = service.read(AshareMarketOverviewRequest(as_of=_SUNDAY_QUERY))

    assert result.status == "UNKNOWN"
    assert result.breadth is None
    assert "MARKET_OVERVIEW_SESSION_INCOMPLETE" in result.data_gaps


def test_provider_evidence_later_than_query_time_fails_closed() -> None:
    query = datetime(2026, 7, 24, 10, 0, tzinfo=_CN_TZ)
    future_update = query + timedelta(minutes=1)
    service = AshareMarketOverviewService(
        source=_RecordingSource(_snapshot(provider_updated_at=future_update)),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: query,
    )

    result = service.read(AshareMarketOverviewRequest(as_of=query))

    assert result.status == "UNKNOWN"
    assert result.provider_observation_age_seconds is None
    assert "MARKET_OVERVIEW_PROVIDER_TIME_AFTER_QUERY" in result.data_gaps


def test_intraday_reference_uses_oldest_section_timestamp_and_rejects_stale_snapshot() -> None:
    query = datetime(2026, 7, 24, 10, 30, tzinfo=_CN_TZ)
    stale_update = query - timedelta(minutes=31)
    service = AshareMarketOverviewService(
        source=_RecordingSource(_snapshot(provider_updated_at=stale_update)),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: query,
    )

    result = service.read(AshareMarketOverviewRequest(as_of=query))

    assert result.status == "UNKNOWN"
    assert result.provider_observation_age_seconds is None
    assert "MARKET_OVERVIEW_INTRADAY_REFERENCE_STALE" in result.data_gaps


def test_truncated_ranked_page_and_missing_major_index_fail_closed() -> None:
    snapshot = _snapshot()
    truncated = tuple(
        replace(coverage, total=coverage.returned + 1)
        if coverage.section == "concept_turnover"
        else coverage
        for coverage in snapshot.ranked_page_coverage
    )
    calendar = AShareTradingCalendar.from_file(_CALENDAR_PATH)

    truncated_result = AshareMarketOverviewService(
        source=_RecordingSource(replace(snapshot, ranked_page_coverage=truncated)),
        calendar=calendar,
    ).read(AshareMarketOverviewRequest(as_of=_SUNDAY_QUERY))
    missing_index_result = AshareMarketOverviewService(
        source=_RecordingSource(replace(snapshot, indices=snapshot.indices[:-1])),
        calendar=calendar,
    ).read(AshareMarketOverviewRequest(as_of=_SUNDAY_QUERY))

    assert truncated_result.status == "UNKNOWN"
    assert "MARKET_OVERVIEW_SECTION_COVERAGE_INVALID" in truncated_result.data_gaps
    assert [item.to_dict() for item in truncated_result.coverage_diagnostics] == [
        {
            "section": "concept_turnover",
            "total": truncated[3].total,
            "returned": truncated[3].returned,
            "expected_returned": min(
                truncated[3].total,
                truncated[3].requested_page_size,
            ),
            "requested_page_size": truncated[3].requested_page_size,
            "timestamp_count": len(truncated[3].provider_timestamps),
            "missing_timestamp_count": 0,
            "valid_projected_rows": truncated[3].valid_projected_rows,
            "reasons": ["RETURNED_COUNT_MISMATCH"],
        }
    ]
    assert truncated_result.to_capability_value()["coverage_diagnostics"] == [
        truncated_result.coverage_diagnostics[0].to_dict()
    ]
    assert missing_index_result.status == "UNKNOWN"
    assert "MARKET_OVERVIEW_INDEX_COVERAGE_INVALID" in missing_index_result.data_gaps


def test_ranked_page_projected_row_mismatch_has_content_free_diagnostic() -> None:
    snapshot = _snapshot()
    malformed = tuple(
        replace(coverage, valid_projected_rows=coverage.returned - 1)
        if coverage.section == "equity_turnover"
        else coverage
        for coverage in snapshot.ranked_page_coverage
    )

    result = AshareMarketOverviewService(
        source=_RecordingSource(replace(snapshot, ranked_page_coverage=malformed)),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
    ).read(AshareMarketOverviewRequest(as_of=_SUNDAY_QUERY))

    assert result.status == "UNKNOWN"
    assert len(result.coverage_diagnostics) == 1
    diagnostic = result.coverage_diagnostics[0]
    assert diagnostic.section == "equity_turnover"
    assert diagnostic.valid_projected_rows == diagnostic.returned - 1
    assert diagnostic.reasons == ("PROJECTED_ROWS_MISMATCH",)


def test_malformed_ranked_rows_and_index_fail_closed_without_rewriting_leaders() -> None:
    snapshot = _snapshot()
    malformed_industries = (
        dict(snapshot.industries[0], f3="-"),
        *snapshot.industries[1:],
    )
    malformed_equities = (
        dict(snapshot.equities[0], f6="-"),
        *snapshot.equities[1:],
    )
    malformed_indices = (
        *snapshot.indices[:2],
        dict(snapshot.indices[2], f3="-"),
        *snapshot.indices[3:],
    )
    calendar = AShareTradingCalendar.from_file(_CALENDAR_PATH)

    industry_result = AshareMarketOverviewService(
        source=_RecordingSource(replace(snapshot, industries=malformed_industries)),
        calendar=calendar,
    ).read(AshareMarketOverviewRequest(as_of=_SUNDAY_QUERY))
    equity_result = AshareMarketOverviewService(
        source=_RecordingSource(replace(snapshot, equities=malformed_equities)),
        calendar=calendar,
    ).read(AshareMarketOverviewRequest(as_of=_SUNDAY_QUERY))
    index_result = AshareMarketOverviewService(
        source=_RecordingSource(replace(snapshot, indices=malformed_indices)),
        calendar=calendar,
    ).read(AshareMarketOverviewRequest(as_of=_SUNDAY_QUERY))

    assert industry_result.status == "UNKNOWN"
    assert "MARKET_OVERVIEW_SECTION_PAYLOAD_INVALID" in industry_result.data_gaps
    assert equity_result.status == "UNKNOWN"
    assert "MARKET_OVERVIEW_SECTION_PAYLOAD_INVALID" in equity_result.data_gaps
    assert index_result.status == "UNKNOWN"
    assert "MARKET_OVERVIEW_INDEX_PAYLOAD_INVALID" in index_result.data_gaps


def test_deadline_expiring_during_fetch_prevents_late_result_publication() -> None:
    current = [_SUNDAY_QUERY]
    deadline = _SUNDAY_QUERY + timedelta(seconds=1)

    @dataclass
    class _LateSource:
        snapshot: EastmoneyMarketOverviewSnapshot

        def fetch(self, *, deadline_at: datetime | None) -> EastmoneyMarketOverviewSnapshot:
            assert deadline_at == deadline
            current[0] = deadline + timedelta(microseconds=1)
            return self.snapshot

    service = AshareMarketOverviewService(
        source=_LateSource(_snapshot()),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: current[0],
    )

    result = service.read(AshareMarketOverviewRequest(as_of=_SUNDAY_QUERY, deadline_at=deadline))

    assert result.status == "UNKNOWN"
    assert result.data_gaps == ("CONSULTATION_DEADLINE_REACHED",)


def test_expired_deadline_returns_typed_unknown_without_touching_provider() -> None:
    source = _RecordingSource(_snapshot())
    service = AshareMarketOverviewService(
        source=source,
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: _SUNDAY_QUERY,
    )

    result = service.read(
        AshareMarketOverviewRequest(
            as_of=_SUNDAY_QUERY,
            deadline_at=_SUNDAY_QUERY - timedelta(seconds=1),
        )
    )

    assert result.status == "UNKNOWN"
    assert result.data_gaps == ("CONSULTATION_DEADLINE_REACHED",)
    assert source.calls == []


@dataclass
class _Response:
    payload: dict[str, Any]

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload

    @property
    def text(self) -> str:
        value = self.payload.get("text")
        return value if isinstance(value, str) else ""


@dataclass
class _HttpError(RuntimeError):
    pass


@dataclass
class _BlockingSession:
    entered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    headers: dict[str, str] = field(default_factory=dict)

    def get(
        self,
        url: str,
        *,
        params: dict[str, object],
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> _Response:
        del url, params, timeout, headers
        self.entered.set()
        assert self.release.wait(timeout=1)
        return _Response({"data": {"total": 0, "diff": []}})


def test_source_owned_http_session_serializes_concurrent_calls_with_bounded_wait() -> None:
    session = _BlockingSession()
    client = ThreadSafeEastmoneyOverviewHttpClient(
        session=session,
        min_interval_seconds=0,
    )
    errors: list[BaseException] = []

    def first_call() -> None:
        try:
            client("https://example.invalid", params={}, timeout=1)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    first = Thread(target=first_call)
    first.start()
    assert session.entered.wait(timeout=1)

    with pytest.raises(MarketOverviewDeadlineReachedError):
        client("https://example.invalid", params={}, timeout=0.01)

    session.release.set()
    first.join(timeout=1)
    assert not first.is_alive()
    assert errors == []


def test_eastmoney_source_uses_delay_read_endpoint_and_bounded_pages() -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    def http_get(
        url: str,
        *,
        params: dict[str, object],
        timeout: float,
    ) -> _Response:
        calls.append((url, dict(params), timeout))
        if url.endswith("/ulist.np/get"):
            rows = [
                _index(
                    "000001",
                    "上证指数",
                    change_pct=-1.61,
                    turnover=30_000_000_000,
                    advancers=1,
                    decliners=1,
                    unchanged=0,
                ),
                _index(
                    "399001",
                    "深证成指",
                    change_pct=-2.47,
                    turnover=35_500_000_000,
                    advancers=1,
                    decliners=0,
                    unchanged=0,
                ),
            ]
        elif "t:6" in str(params["fs"]):
            rows = [_equity("603986", "兆易创新", change_pct=1.12, turnover=22_800_000_000)]
        elif params["fid"] == "f6":
            rows = [
                _board(
                    "BK9999",
                    "电子",
                    change_pct=-2.0,
                    turnover=200_000_000_000,
                    advancers=10,
                    decliners=100,
                    leader="示例公司",
                )
            ]
        else:
            rows = [
                _board(
                    "BK1326",
                    "半导体设备",
                    change_pct=3.18,
                    turnover=64_500_000_000,
                    advancers=20,
                    decliners=4,
                    leader="至纯科技",
                )
            ]
        return _Response({"data": {"total": len(rows), "diff": rows}})

    source = EastmoneyCurrentMarketOverviewSource(
        http_get=http_get,
        clock=lambda: _SUNDAY_QUERY.astimezone(UTC),
    )

    snapshot = source.fetch(deadline_at=_SUNDAY_QUERY.astimezone(UTC) + timedelta(seconds=10))

    assert len(snapshot.industries) == 2
    assert len(snapshot.concepts) == 2
    assert {row["f14"] for row in snapshot.industries} == {"半导体设备", "电子"}
    assert {row["f14"] for row in snapshot.concepts} == {"半导体设备", "电子"}
    assert len(snapshot.indices) == 2
    assert len(snapshot.equities) == 1
    assert [item.section for item in snapshot.ranked_page_coverage] == [
        "industry_change",
        "industry_turnover",
        "concept_change",
        "concept_turnover",
        "equity_turnover",
    ]
    assert all(
        coverage.valid_projected_rows == coverage.returned
        for coverage in snapshot.ranked_page_coverage
    )
    assert len(calls) == 6
    assert [url for url, _, _ in calls] == [
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
        "https://push2delay.eastmoney.com/api/qt/clist/get",
    ]
    assert [params["pz"] for _, params, _ in calls if "pz" in params] == [
        "100",
        "100",
        "100",
        "100",
        "100",
    ]
    assert [params.get("fid") for _, params, _ in calls] == [
        "f3",
        "f6",
        "f3",
        "f6",
        None,
        "f6",
    ]
    assert all(0 < timeout <= 5 for _, _, timeout in calls)


def _tencent_index_text() -> str:
    now = _FRIDAY_CLOSE.astimezone(_CN_TZ)
    stamp = now.strftime("%Y%m%d%H%M%S")
    rows = (
        ("sh000001", "000001", "上证指数", "3946.68", "3934.09", "12.59", "0.32"),
        ("sz399001", "399001", "深证成指", "14414.43", "14259.44", "154.99", "1.09"),
        ("sz399006", "399006", "创业板指", "3602.08", "3549.16", "52.92", "1.49"),
        ("sh000688", "000688", "科创50", "1736.99", "1709.50", "27.49", "1.61"),
    )
    lines = []
    for quote_code, code, name, price, prev, change, change_pct in rows:
        fields = [""] * 38
        fields[1] = name
        fields[2] = code
        fields[3] = price
        fields[4] = prev
        fields[30] = stamp
        fields[31] = change
        fields[32] = change_pct
        fields[36] = "1000000"
        fields[37] = "100000"
        # 真实 qt.gtimg.cn 行格式：v_sh000001="..." 行尾带分号
        lines.append(f'v_{quote_code}="{"~".join(fields)}";')
    return "\n".join(lines)


def test_source_prefers_tencent_realtime_indices_and_degrades_breadth() -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    def http_get(
        url: str,
        *,
        params: dict[str, object],
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> _Response:
        del headers
        calls.append((url, dict(params), timeout))
        if url.startswith("https://qt.gtimg.cn/q="):
            return _Response({"data": {"total": 0, "diff": []}, "text": _tencent_index_text()})
        if url.endswith("/ulist.np/get"):
            rows = [
                _index(
                    "000001",
                    "上证指数",
                    change_pct=-1.61,
                    turnover=30_000_000_000,
                    advancers=1,
                    decliners=1,
                    unchanged=0,
                ),
                _index(
                    "399001",
                    "深证成指",
                    change_pct=-2.47,
                    turnover=35_500_000_000,
                    advancers=1,
                    decliners=0,
                    unchanged=0,
                ),
                _index(
                    "399006",
                    "创业板指",
                    change_pct=-2.65,
                    turnover=0,
                    advancers=0,
                    decliners=0,
                    unchanged=0,
                ),
                _index(
                    "000688",
                    "科创50",
                    change_pct=-0.14,
                    turnover=0,
                    advancers=0,
                    decliners=0,
                    unchanged=0,
                ),
            ]
        elif "t:6" in str(params["fs"]):
            rows = [_equity("603986", "兆易创新", change_pct=1.12, turnover=22_800_000_000)]
        elif params["fid"] == "f6":
            rows = [
                _board(
                    "BK9999",
                    "电子",
                    change_pct=-2.0,
                    turnover=200_000_000_000,
                    advancers=10,
                    decliners=100,
                    leader="示例公司",
                )
            ]
        else:
            rows = [
                _board(
                    "BK1326",
                    "半导体设备",
                    change_pct=3.18,
                    turnover=64_500_000_000,
                    advancers=20,
                    decliners=4,
                    leader="至纯科技",
                )
            ]
        return _Response({"data": {"total": len(rows), "diff": rows}})

    source = EastmoneyCurrentMarketOverviewSource(
        http_get=http_get,
        clock=lambda: _SUNDAY_QUERY.astimezone(UTC),
    )

    snapshot = source.fetch(deadline_at=_SUNDAY_QUERY.astimezone(UTC) + timedelta(seconds=10))

    assert snapshot.indices_provider == "TENCENT"
    assert len(snapshot.indices) == 4
    assert {row["f12"] for row in snapshot.indices} == {"000001", "399001", "399006", "000688"}
    assert "f104" not in snapshot.indices[0]

    service = AshareMarketOverviewService(
        source=_RecordingSource(snapshot),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: _SUNDAY_QUERY,
    )
    result = service.read(
        AshareMarketOverviewRequest(
            as_of=_SUNDAY_QUERY,
            deadline_at=_SUNDAY_QUERY + timedelta(seconds=10),
        )
    )

    assert result.status == "PARTIAL"
    assert result.provider_mode == "TENCENT_REALTIME_INDICES"
    assert result.breadth is None
    assert "MARKET_OVERVIEW_BREADTH_UNAVAILABLE" in result.data_gaps
    assert {index.code for index in result.major_indices} == {"000001", "399001", "399006", "000688"}
    assert all(index.advancers is None for index in result.major_indices)
    assert all(index.change_pct is not None for index in result.major_indices)


def test_source_falls_back_to_eastmoney_when_tencent_indices_unavailable() -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    def http_get(
        url: str,
        *,
        params: dict[str, object],
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> _Response:
        del headers
        calls.append((url, dict(params), timeout))
        if url.startswith("https://qt.gtimg.cn/q="):
            raise _HttpError("tencent unavailable")
        if url.endswith("/ulist.np/get"):
            rows = [
                _index(
                    "000001",
                    "上证指数",
                    change_pct=-1.61,
                    turnover=30_000_000_000,
                    advancers=1,
                    decliners=1,
                    unchanged=0,
                ),
                _index(
                    "399001",
                    "深证成指",
                    change_pct=-2.47,
                    turnover=35_500_000_000,
                    advancers=1,
                    decliners=0,
                    unchanged=0,
                ),
                _index(
                    "399006",
                    "创业板指",
                    change_pct=-2.65,
                    turnover=0,
                    advancers=0,
                    decliners=0,
                    unchanged=0,
                ),
                _index(
                    "000688",
                    "科创50",
                    change_pct=-0.14,
                    turnover=0,
                    advancers=0,
                    decliners=0,
                    unchanged=0,
                ),
            ]
        elif "t:6" in str(params["fs"]):
            rows = [_equity("603986", "兆易创新", change_pct=1.12, turnover=22_800_000_000)]
        elif params["fid"] == "f6":
            rows = [
                _board(
                    "BK9999",
                    "电子",
                    change_pct=-2.0,
                    turnover=200_000_000_000,
                    advancers=10,
                    decliners=100,
                    leader="示例公司",
                )
            ]
        else:
            rows = [
                _board(
                    "BK1326",
                    "半导体设备",
                    change_pct=3.18,
                    turnover=64_500_000_000,
                    advancers=20,
                    decliners=4,
                    leader="至纯科技",
                )
            ]
        return _Response({"data": {"total": len(rows), "diff": rows}})

    source = EastmoneyCurrentMarketOverviewSource(
        http_get=http_get,
        clock=lambda: _SUNDAY_QUERY.astimezone(UTC),
    )

    snapshot = source.fetch(deadline_at=_SUNDAY_QUERY.astimezone(UTC) + timedelta(seconds=10))

    assert snapshot.indices_provider == "EASTMONEY"
    assert len(snapshot.indices) == 4
    assert "f104" in snapshot.indices[0]

    service = AshareMarketOverviewService(
        source=_RecordingSource(snapshot),
        calendar=AShareTradingCalendar.from_file(_CALENDAR_PATH),
        clock=lambda: _SUNDAY_QUERY,
    )
    result = service.read(
        AshareMarketOverviewRequest(
            as_of=_SUNDAY_QUERY,
            deadline_at=_SUNDAY_QUERY + timedelta(seconds=10),
        )
    )

    assert result.status == "PARTIAL"
    assert result.provider_mode == "EASTMONEY_DELAYED_REFERENCE"
    assert result.breadth is not None
    assert "MARKET_OVERVIEW_BREADTH_UNAVAILABLE" not in result.data_gaps
    assert all(index.advancers is not None for index in result.major_indices)
