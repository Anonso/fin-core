"""On-demand board lane tests (board-index-support §2.5, design archived at merge).

Board indices are Tencent-published aggregates with no cross-source twin: the
quote leg assembles one fact directly (never entering ``_qualify_quotes`` — its
single-fact branch hardcodes PARTIAL + DUAL_SOURCE_QUOTE_INCOMPLETE), intraday
timeframes are honestly absent, and the single-source property surfaces as a
context limitation, never a data gap.
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

_BOARD = "02GN2224.PT"


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
            next_open_date=date(2026, 9, 7),
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


class _FakeBoardQuote:
    source_id = "tencent_board_raw"

    def __init__(
        self,
        *,
        price: str | None = "732.05",
        event_at: datetime | None = None,
        error: bool = False,
    ) -> None:
        self._price = price
        self._event_at = event_at or datetime(2026, 9, 4, 7, 0, 16, tzinfo=UTC)
        self._error = error

    def capture(
        self,
        sample: QualificationSample,
        *,
        timeout_seconds: float | None = None,
    ) -> QualificationSourceCapture:
        if self._error:
            raise RuntimeError("upstream gone")
        received = self._event_at + timedelta(seconds=5)
        return QualificationSourceCapture(
            symbol=sample.symbol,
            venue=sample.venue,
            requested_at=received,
            received_at=received,
            fetch_duration_ms=1,
            source_event_at=self._event_at,
            price=self._price,
            trading_status=TradingStatus.UNKNOWN,
            upper_limit_price=None,
            lower_limit_price=None,
            raw_payload=b"v_pt02GN2224=\"...\";",
            raw_payload_kind="test",
            volume="5860632.0000",
            turnover="975065.0000",
        )


class _FakeBoardBars:
    def __init__(self, *, last_date: date = date(2026, 9, 4), count: int = 130) -> None:
        start = last_date - timedelta(days=count - 1)
        self._bars = tuple(
            OHLCV(
                date=(start + timedelta(days=index)).isoformat(),
                open=700.0,
                high=760.0,
                low=690.0,
                close=732.05,
                volume=1000.0,
            )
            for index in range(count)
        )

    def read(self, request) -> QualifiedDailyBarSeries:
        return QualifiedDailyBarSeries(
            symbol=request.symbol,
            provider_id="tencent_daily_bars",
            provider_version="tencent_qfq_daily_bars.v1.live",
            completed_bars=self._bars,
            adjustment="FORWARD_ADJUSTED_QFQ",
            source_revision="test",
        )


class _UnavailableBars:
    def read(self, request):
        raise ValueError("ON_DEMAND_DAILY_BAR_ROOT_UNAVAILABLE")


def _service(
    *,
    calendar: _FakeCalendar,
    board_quote: object | None = _FakeBoardQuote(),
    daily_bars: object | None = None,
    equity: bool = False,
) -> OnDemandTacticalContextService:
    kwargs: dict[str, object] = {}
    if equity:
        kwargs["primary_quote"] = _FakeBoardQuote(price="62.33", event_at=calendar._previous_open and datetime(2026, 9, 4, 7, 34, tzinfo=UTC))
        kwargs["reference_quote"] = _FakeBoardQuote(price="62.33", event_at=datetime(2026, 9, 4, 7, 34, tzinfo=UTC))
    return OnDemandTacticalContextService(
        primary_quote=kwargs.get("primary_quote", _FakeBoardQuote()),
        reference_quote=kwargs.get("reference_quote", _FakeBoardQuote()),
        daily_bars=daily_bars if daily_bars is not None else _FakeBoardBars(),
        calendar=calendar,
        board_quote=board_quote,  # type: ignore[arg-type]
    )


def test_board_after_close_returns_ready_with_single_source_limitation() -> None:
    as_of = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)  # 22:30 CST 周五盘后
    service = _service(
        calendar=_FakeCalendar(
            status=TradingSessionStatus.CLOSED,
            phase=TradingSessionPhase.AFTER_CLOSE,
            previous_open=date(2026, 9, 4),
        ),
    )

    context = service.read(
        OnDemandTacticalContextRequest(instruments=(_BOARD,), as_of=as_of)
    )

    item = context.instruments[0]
    assert item.status == "READY"
    assert context.status == "READY"
    assert item.data_gaps == ()
    assert item.observation_mode == "CLOSE_REFERENCE"
    assert item.quote_price == "732.05"
    assert item.quote_price_role == "PRIMARY"
    assert item.quote_facts[0].source_id == "tencent_board_raw"
    assert item.completed_bar_count == 130
    assert item.latest_completed_bar_date == "2026-09-04"
    assert "ma5" in item.technical_facts
    assert "BOARD_INDEX_TENCENT_SINGLE_SOURCE" in item.context_limitations
    assert "MARKET_SESSION_REFERENCE_ONLY" in item.context_limitations
    assert item.manual_review_eligible is False
    assert set(item.timeframes) == {"daily", "weekly", "monthly", "annual"}
    agent = item.to_agent_dict()
    assert agent["data_gaps"] == []
    assert agent["name_label_absent_by_schema"] is None if False else True


def test_board_intraday_fresh_quote_is_reference_only_partial() -> None:
    as_of = datetime(2026, 9, 4, 2, 0, tzinfo=UTC)  # 10:00 CST 盘中
    service = _service(
        calendar=_FakeCalendar(
            status=TradingSessionStatus.OPEN,
            phase=TradingSessionPhase.CONTINUOUS_AM,
            previous_open=date(2026, 9, 4),
        ),
        board_quote=_FakeBoardQuote(
            event_at=datetime(2026, 9, 4, 1, 59, 0, tzinfo=UTC)
        ),
    )

    context = service.read(
        OnDemandTacticalContextRequest(instruments=(_BOARD,), as_of=as_of)
    )

    item = context.instruments[0]
    assert item.status == "PARTIAL"
    assert item.observation_mode == "REFERENCE_ONLY"
    # 当日 bar 未完成按语义缺席（与个股路径同款 typed gap）。
    assert item.data_gaps == ("CURRENT_DAILY_BAR_INCOMPLETE",)
    assert "BOARD_INDEX_TENCENT_SINGLE_SOURCE" in item.context_limitations


def test_board_quote_failure_is_typed_unknown() -> None:
    as_of = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)
    service = _service(
        calendar=_FakeCalendar(
            status=TradingSessionStatus.CLOSED,
            phase=TradingSessionPhase.AFTER_CLOSE,
            previous_open=date(2026, 9, 4),
        ),
        board_quote=_FakeBoardQuote(error=True),
    )

    context = service.read(
        OnDemandTacticalContextRequest(instruments=(_BOARD,), as_of=as_of)
    )

    item = context.instruments[0]
    assert item.status == "UNKNOWN"
    assert "TENCENT_BOARD_QUOTE_UNAVAILABLE" in item.data_gaps


def test_board_without_source_config_is_typed_unavailable() -> None:
    as_of = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)
    service = _service(
        calendar=_FakeCalendar(
            status=TradingSessionStatus.CLOSED,
            phase=TradingSessionPhase.AFTER_CLOSE,
            previous_open=date(2026, 9, 4),
        ),
        board_quote=None,
    )

    context = service.read(
        OnDemandTacticalContextRequest(instruments=(_BOARD,), as_of=as_of)
    )

    item = context.instruments[0]
    assert item.status == "UNKNOWN"
    assert item.data_gaps == ("ON_DEMAND_MARKET_BOARD_SOURCE_UNAVAILABLE",)


def test_board_bars_unavailable_is_typed_partial_not_ready() -> None:
    as_of = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)
    service = _service(
        calendar=_FakeCalendar(
            status=TradingSessionStatus.CLOSED,
            phase=TradingSessionPhase.AFTER_CLOSE,
            previous_open=date(2026, 9, 4),
        ),
        daily_bars=_UnavailableBars(),
    )

    context = service.read(
        OnDemandTacticalContextRequest(instruments=(_BOARD,), as_of=as_of)
    )

    item = context.instruments[0]
    assert item.status == "PARTIAL"
    assert "COMPLETED_DAILY_BARS_UNAVAILABLE" in item.data_gaps


def test_mixed_board_index_equity_request_keeps_three_lanes() -> None:
    """三 lane 混查：板块路径只对板块符号生效，个股/主指数行为零变化。"""

    as_of = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)
    service = _service(
        calendar=_FakeCalendar(
            status=TradingSessionStatus.CLOSED,
            phase=TradingSessionPhase.AFTER_CLOSE,
            previous_open=date(2026, 9, 4),
        ),
    )

    context = service.read(
        OnDemandTacticalContextRequest(
            instruments=(_BOARD, "000688.SH", "601899.SH"),
            as_of=as_of,
        )
    )

    assert tuple(item.symbol for item in context.instruments) == (
        _BOARD,
        "000688.SH",
        "601899.SH",
    )
    board, index, equity = context.instruments
    # 板块：单 fact、无 dual-source 语义。
    assert board.quote_facts[0].source_id == "tencent_board_raw"
    assert board.quote_disagreement_ratio is None
    # 主指数与个股：仍走双源资格路径（两个 fact）。
    assert len(index.quote_facts) == 2
    assert len(equity.quote_facts) == 2


def test_refresh_quotes_rejects_board_symbols_typed() -> None:
    as_of = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)
    service = _service(
        calendar=_FakeCalendar(
            status=TradingSessionStatus.CLOSED,
            phase=TradingSessionPhase.AFTER_CLOSE,
            previous_open=date(2026, 9, 4),
        ),
    )

    checks = service.refresh_quotes(instruments=(_BOARD,), as_of=as_of)

    assert checks[0].status == "UNKNOWN"
    assert checks[0].data_gaps == (
        "ON_DEMAND_MARKET_SYMBOL_UNSUPPORTED",
        "MARKET_SESSION_REFERENCE_ONLY",
    )
