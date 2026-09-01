"""BUG-022 回归：盘后收盘价合格化（close-reference qualification）。

盘后（AFTER_CLOSE/CLOSED_DAY）双源同价且事件日期等于最近完成交易日时，
read_market_snapshot 必须返回 READY + 可投影价格 + data_gaps=()，并显式标注
CLOSE_REFERENCE；真实缺料（当日 daily bar 未完成）进 context_limitations。
非 close 场景（PRE_OPEN 等）行为不变。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from fin_analyse.market.data_qualification import (
    QualificationSample,
    QualificationSourceCapture,
    TradingStatus,
)
from fin_analyse.market.on_demand_tactical_context import (
    OnDemandTacticalContextRequest,
    OnDemandTacticalContextService,
)
from fin_analyse.market.providers.base import OHLCV
from fin_analyse.market.qualified_daily_bars import QualifiedDailyBarSeries
from fin_analyse.market.trading_calendar import (
    CalendarEvidenceTier,
    NextOpenDateDecision,
    PreviousOpenDateDecision,
    TradingSessionDecision,
    TradingSessionPhase,
    TradingSessionStatus,
)


def _session(
    *,
    at: datetime,
    status: TradingSessionStatus,
    phase: TradingSessionPhase,
) -> TradingSessionDecision:
    return TradingSessionDecision(
        decision_id="test",
        calendar_snapshot_id="test",
        calendar_snapshot_hash="test",
        calendar_version="test",
        source_policy_id="test",
        phase_policy_version="test",
        evidence_tier=CalendarEvidenceTier.TEST_ONLY,
        queried_at=at,
        trade_date=at.astimezone(timezone(timedelta(hours=8))).date(),
        status=status,
        phase=phase,
        execution_allowed=False,
    )


class _FakeCalendar:
    def __init__(
        self,
        *,
        status: TradingSessionStatus,
        phase: TradingSessionPhase,
        previous_open: date,
    ) -> None:
        self._status = status
        self._phase = phase
        self._previous_open = previous_open

    def session_at(self, at: datetime) -> TradingSessionDecision:
        return _session(at=at, status=self._status, phase=self._phase)

    def next_open_date(self, *, after, known_at: datetime) -> NextOpenDateDecision:
        return NextOpenDateDecision(
            decision_id="test",
            calendar_snapshot_id="test",
            calendar_snapshot_hash="test",
            calendar_version="test",
            source_policy_id="test",
            evidence_tier=CalendarEvidenceTier.TEST_ONLY,
            after=after,
            known_at=known_at,
            next_open_date=date(2026, 9, 2),
        )

    def previous_open_date(self, *, before, known_at: datetime) -> PreviousOpenDateDecision:
        return PreviousOpenDateDecision(
            decision_id="test",
            calendar_snapshot_id="test",
            calendar_snapshot_hash="test",
            calendar_version="test",
            source_policy_id="test",
            evidence_tier=CalendarEvidenceTier.TEST_ONLY,
            before=before,
            known_at=known_at,
            previous_open_date=self._previous_open,
        )


class _FakeQuoteSource:
    source_id: str

    def __init__(self, source_id: str, price: str, event_at: datetime) -> None:
        self.source_id = source_id
        self._price = price
        self._event_at = event_at

    def capture(
        self,
        sample: QualificationSample,
        *,
        timeout_seconds: float | None = None,
    ) -> QualificationSourceCapture:
        received = self._event_at + timedelta(minutes=5)
        return QualificationSourceCapture(
            symbol=sample.symbol,
            venue=sample.venue,
            requested_at=received,
            received_at=received,
            fetch_duration_ms=1,
            source_event_at=self._event_at,
            price=self._price,
            trading_status=TradingStatus.UNKNOWN,
            upper_limit_price="72.38",
            lower_limit_price="59.22",
            raw_payload=b"{}",
            raw_payload_kind="test",
        )


class _FakeBars:
    def __init__(self, last_date: date) -> None:
        start = last_date - timedelta(days=64)
        self._bars = tuple(
            OHLCV(
                date=(start + timedelta(days=index)).isoformat(),
                open=10.0,
                high=11.0,
                low=9.0,
                close=10.5,
                volume=1000.0,
            )
            for index in range(65)
        )

    def read(self, request) -> QualifiedDailyBarSeries:
        return QualifiedDailyBarSeries(
            symbol=request.symbol,
            provider_id="fake",
            provider_version="v1",
            completed_bars=self._bars,
            adjustment="FORWARD_ADJUSTED_QFQ",
            source_revision="test",
        )


def _service(
    *,
    calendar: _FakeCalendar,
    event_at: datetime,
) -> OnDemandTacticalContextService:
    return OnDemandTacticalContextService(
        primary_quote=_FakeQuoteSource("eastmoney_raw", "62.33", event_at),
        reference_quote=_FakeQuoteSource("tencent_raw", "62.33", event_at),
        daily_bars=_FakeBars(last_date=date(2026, 8, 31)),
        calendar=calendar,
    )


def test_after_close_dual_agree_returns_ready_close_reference() -> None:
    as_of = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)  # 22:30 CST
    event_at = datetime(2026, 9, 1, 7, 34, tzinfo=UTC)  # 15:34 CST
    service = _service(
        calendar=_FakeCalendar(
            status=TradingSessionStatus.CLOSED,
            phase=TradingSessionPhase.AFTER_CLOSE,
            previous_open=date(2026, 9, 1),
        ),
        event_at=event_at,
    )

    context = service.read(
        OnDemandTacticalContextRequest(
            instruments=("000657.SZ",),
            as_of=as_of,
        )
    )

    item = context.instruments[0]
    assert context.status == "READY"
    assert context.data_gaps == ()
    assert item.status == "READY"
    assert item.quote_price == "62.33"
    assert item.observation_mode == "CLOSE_REFERENCE"
    assert item.reference_only is True
    assert item.manual_review_eligible is False
    assert "MARKET_SESSION_REFERENCE_ONLY" in item.context_limitations
    assert "CURRENT_TRADING_DAY_BAR_NOT_INCLUDED" in item.context_limitations
    agent = item.to_agent_dict()
    assert agent["price"] == "62.33"
    assert agent["observation_mode"] == "CLOSE_REFERENCE"
    assert "CURRENT_TRADING_DAY_BAR_NOT_INCLUDED" in agent["context_limitations"]
    assert agent["data_gaps"] == []


def test_after_close_stale_quote_date_is_not_close_qualified() -> None:
    as_of = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)
    stale_event_at = datetime(2026, 8, 31, 7, 34, tzinfo=UTC)  # 昨日
    service = _service(
        calendar=_FakeCalendar(
            status=TradingSessionStatus.CLOSED,
            phase=TradingSessionPhase.AFTER_CLOSE,
            previous_open=date(2026, 9, 1),
        ),
        event_at=stale_event_at,
    )

    context = service.read(
        OnDemandTacticalContextRequest(
            instruments=("000657.SZ",),
            as_of=as_of,
        )
    )

    item = context.instruments[0]
    assert item.status == "PARTIAL"
    assert item.observation_mode == "REFERENCE_ONLY"
    assert item.context_limitations == ()
    assert "PRIMARY_TRADING_STATUS_UNKNOWN" in item.data_gaps
    assert "MARKET_SESSION_REFERENCE_ONLY" in item.data_gaps


def test_pre_open_is_not_close_qualified() -> None:
    as_of = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)  # 09:00 CST
    event_at = datetime(2026, 8, 31, 7, 34, tzinfo=UTC)
    service = _service(
        calendar=_FakeCalendar(
            status=TradingSessionStatus.CLOSED,
            phase=TradingSessionPhase.PRE_OPEN,
            previous_open=date(2026, 9, 1),
        ),
        event_at=event_at,
    )

    context = service.read(
        OnDemandTacticalContextRequest(
            instruments=("000657.SZ",),
            as_of=as_of,
        )
    )

    item = context.instruments[0]
    assert item.status == "PARTIAL"
    assert item.observation_mode == "REFERENCE_ONLY"
    assert "MARKET_SESSION_REFERENCE_ONLY" in item.data_gaps
    assert "PRIMARY_TRADING_STATUS_UNKNOWN" in item.data_gaps
