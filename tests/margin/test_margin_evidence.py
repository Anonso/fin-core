from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fin_analyse.margin.evidence import (
    MarginEvidenceRequest,
    MarginEvidenceService,
    MarginEvidenceSourceResult,
    MarginObservation,
    build_default_margin_evidence,
    margin_evidence_artifact_root,
)
from fin_analyse.portfolio.actual_advisory import (
    ACTUAL_ADVISORY_PORTFOLIO_SCHEMA,
    ActualAdvisoryPortfolioRead,
    ActualAdvisoryPortfolioReason,
    ActualAdvisoryPortfolioSnapshot,
    ActualAdvisoryPortfolioStatus,
    actual_advisory_snapshot_ref,
)

CN_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class _ActualPortfolioReader:
    result: ActualAdvisoryPortfolioRead

    def read(self) -> ActualAdvisoryPortfolioRead:
        return self.result


@dataclass
class _MarginSource:
    result: MarginEvidenceSourceResult
    requests: list[MarginEvidenceRequest] = field(default_factory=list)

    def read(self, request: MarginEvidenceRequest) -> MarginEvidenceSourceResult:
        self.requests.append(request)
        return self.result


def test_account_margin_unknown_blocks_increased_risk_from_same_confirmed_snapshot() -> None:
    confirmed_at = datetime(2026, 8, 7, 15, 12, tzinfo=CN_TZ)
    revision = "sha256:" + "a" * 64
    snapshot = ActualAdvisoryPortfolioSnapshot(
        schema_version=ACTUAL_ADVISORY_PORTFOLIO_SCHEMA,
        source_kind="USER_CONFIRMED_MANUAL",
        account_alias="primary",
        as_of=confirmed_at,
        valid_until=confirmed_at + timedelta(hours=24),
        net_assets=Decimal("100000.00"),
        available_cash=Decimal("10000.00"),
        margin_debt=None,
        positions=(),
        content_hash="a" * 64,
        revision=revision,
    )
    reader = MarginEvidenceService(
        account_reader=_ActualPortfolioReader(
            ActualAdvisoryPortfolioRead(
                status=ActualAdvisoryPortfolioStatus.PARTIAL,
                reason_codes=(ActualAdvisoryPortfolioReason.MARGIN_DEBT_UNKNOWN,),
                snapshot=snapshot,
            )
        )
    )

    result = reader.read(
        MarginEvidenceRequest(
            instruments=(),
            as_of=datetime(2026, 8, 8, 9, 30, tzinfo=CN_TZ),
        )
    )

    assert result.to_agent_dict()["account"] == {
        "status": "PARTIAL",
        "account_snapshot_ref": actual_advisory_snapshot_ref(revision),
        "confirmed_at": "2026-08-07T15:12:00+08:00",
        "margin_debt": None,
        "net_assets": "100000.00",
        "leverage_ratio": None,
        "risk_increase_allowed": False,
        "data_gaps": ["ACTUAL_ADVISORY_MARGIN_DEBT_UNKNOWN"],
    }
    assert "MARGIN_EVIDENCE_BJ_NOT_COVERED" in result.data_gaps
    assert "flow_score" not in str(result.to_agent_dict())


def test_stale_confirmed_account_cannot_enable_increased_risk_from_margin_evidence() -> None:
    confirmed_at = datetime(2026, 8, 7, 15, 12, tzinfo=CN_TZ)
    account = _confirmed_account(confirmed_at)
    assert account.snapshot is not None
    reader = MarginEvidenceService(
        account_reader=_ActualPortfolioReader(
            ActualAdvisoryPortfolioRead(
                status=ActualAdvisoryPortfolioStatus.PARTIAL,
                reason_codes=(ActualAdvisoryPortfolioReason.STALE,),
                snapshot=account.snapshot,
            )
        )
    )

    payload = reader.read(
        MarginEvidenceRequest(instruments=(), as_of=confirmed_at + timedelta(days=2))
    ).to_agent_dict()

    assert payload["account"]["leverage_ratio"] == "0.01"
    assert payload["account"]["risk_increase_allowed"] is False


def test_complete_empty_account_does_not_enable_increased_risk() -> None:
    confirmed_at = datetime(2026, 8, 10, 8, 0, tzinfo=CN_TZ)
    account = _confirmed_account(confirmed_at)
    assert account.snapshot is not None
    empty = replace(account.snapshot, positions=())
    reader = MarginEvidenceService(
        account_reader=_ActualPortfolioReader(
            ActualAdvisoryPortfolioRead(
                status=ActualAdvisoryPortfolioStatus.READY,
                reason_codes=(),
                snapshot=empty,
            )
        )
    )

    payload = reader.read(
        MarginEvidenceRequest(instruments=(), as_of=confirmed_at)
    ).to_agent_dict()

    assert payload["account"]["status"] == "READY"
    assert payload["account"]["risk_increase_allowed"] is False


def test_default_margin_reader_keeps_its_artifacts_outside_checkout_and_deadline_bounded(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 8, 9, 30, tzinfo=CN_TZ)
    environ = {
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    root = margin_evidence_artifact_root(environ=environ)

    result = build_default_margin_evidence(environ=environ, clock=lambda: now).read(
        MarginEvidenceRequest(instruments=(), as_of=now, deadline_at=now)
    )

    assert root == (tmp_path / "state" / "fin-analyse" / "margin-evidence-v1").resolve()
    assert not root.exists()
    assert "MARGIN_EVIDENCE_DEADLINE_REACHED:SH" in result.data_gaps
    assert "MARGIN_EVIDENCE_BJ_NOT_COVERED" in result.data_gaps
    assert "MARGIN_EVIDENCE_SOURCE_UNAVAILABLE" not in result.data_gaps


def test_reader_reports_completed_margin_trends_and_same_day_stock_denominators() -> None:
    days = _completed_weekdays(start=date(2025, 7, 1), count=270)
    as_of = datetime.combine(days[-1] + timedelta(days=1), datetime.min.time(), tzinfo=CN_TZ)
    account = _confirmed_account(as_of - timedelta(days=1))
    source = _MarginSource(_ready_source_result(days, as_of))
    request = MarginEvidenceRequest(instruments=("600519.SH",), as_of=as_of)
    reader = MarginEvidenceService(
        account_reader=_ActualPortfolioReader(account),
        source=source,
    )

    payload = reader.read(request).to_agent_dict()

    assert source.requests == [request]
    assert payload["status"] == "PARTIAL"
    assert {item["venue"] for item in payload["markets"]} == {"SH", "SZ"}
    sh = next(item for item in payload["markets"] if item["venue"] == "SH")
    assert sh["status"] == "READY"
    assert sh["latest"]["trade_date"] == days[-1].isoformat()
    assert sh["trends"]["total_balance"]["changes"]["20d"]["absolute"] == "40"
    assert sh["trends"]["total_balance"]["one_year_level_percentile"] == "100.00"
    assert sh["source"]["revision"] == "b" * 64

    [stock] = payload["instruments"]
    assert stock["symbol"] == "600519.SH"
    assert stock["denominators"]["trade_date"] == days[-1].isoformat()
    assert stock["denominators"]["free_float_market_cap"] == str(100000 + len(days) - 1)
    assert stock["denominators"]["turnover"] == str(10000 + len(days) - 1)
    assert stock["denominators"]["margin_to_turnover"] is not None
    assert stock["price_volume_relationship"]["window"] == "5d"
    assert stock["price_volume_relationship"]["margin_balance_change"]["absolute"] == "5"
    assert "flow_score" not in str(payload)


def test_stale_margin_capture_degrades_the_evidence_without_erasing_other_views() -> None:
    days = _completed_weekdays(start=date(2025, 7, 1), count=270)
    as_of = datetime.combine(days[-1] + timedelta(days=1), datetime.min.time(), tzinfo=CN_TZ)
    source = _MarginSource(
        _ready_source_result(
            days,
            as_of,
            data_gaps=("MARGIN_EVIDENCE_STALE_CACHE:SH",),
        )
    )
    reader = MarginEvidenceService(
        account_reader=_ActualPortfolioReader(_confirmed_account(as_of - timedelta(days=1))),
        source=source,
    )

    payload = reader.read(
        MarginEvidenceRequest(instruments=("600519.SH",), as_of=as_of)
    ).to_agent_dict()

    assert payload["status"] == "PARTIAL"
    assert next(item for item in payload["markets"] if item["venue"] == "SH")["status"] == "PARTIAL"
    assert payload["instruments"][0]["status"] == "READY"
    assert "MARGIN_EVIDENCE_STALE_CACHE:SH" in payload["data_gaps"]


def test_missing_stock_margin_does_not_erase_another_stock_or_market_view() -> None:
    days = _completed_weekdays(start=date(2025, 7, 1), count=270)
    as_of = datetime.combine(days[-1] + timedelta(days=1), datetime.min.time(), tzinfo=CN_TZ)
    reader = MarginEvidenceService(
        account_reader=_ActualPortfolioReader(_confirmed_account(as_of - timedelta(days=1))),
        source=_MarginSource(_ready_source_result(days, as_of)),
    )

    payload = reader.read(
        MarginEvidenceRequest(
            instruments=("600519.SH", "000001.SZ"),
            as_of=as_of,
        )
    ).to_agent_dict()

    assert next(item for item in payload["markets"] if item["venue"] == "SH")["status"] == "READY"
    statuses = {item["symbol"]: item["status"] for item in payload["instruments"]}
    assert statuses == {"600519.SH": "READY", "000001.SZ": "UNKNOWN"}


def test_current_trading_day_record_is_not_used_as_completed_margin_evidence() -> None:
    days = _completed_weekdays(start=date(2025, 7, 1), count=270)
    as_of = datetime.combine(days[-1], datetime.min.time(), tzinfo=CN_TZ)
    reader = MarginEvidenceService(
        account_reader=_ActualPortfolioReader(_confirmed_account(as_of - timedelta(days=1))),
        source=_MarginSource(_ready_source_result(days, as_of)),
    )

    payload = reader.read(MarginEvidenceRequest(instruments=(), as_of=as_of)).to_agent_dict()

    sh = next(item for item in payload["markets"] if item["venue"] == "SH")
    assert sh["latest"]["trade_date"] == days[-2].isoformat()


def _completed_weekdays(*, start: date, count: int) -> tuple[date, ...]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return tuple(days)


def _confirmed_account(confirmed_at: datetime) -> ActualAdvisoryPortfolioRead:
    snapshot = ActualAdvisoryPortfolioSnapshot(
        schema_version=ACTUAL_ADVISORY_PORTFOLIO_SCHEMA,
        source_kind="USER_CONFIRMED_MANUAL",
        account_alias="primary",
        as_of=confirmed_at,
        valid_until=confirmed_at + timedelta(hours=24),
        net_assets=Decimal("100000.00"),
        available_cash=Decimal("10000.00"),
        margin_debt=Decimal("1000.00"),
        positions=(),
        content_hash="e" * 64,
        revision="sha256:" + "e" * 64,
    )
    return ActualAdvisoryPortfolioRead(
        status=ActualAdvisoryPortfolioStatus.READY,
        reason_codes=(),
        snapshot=snapshot,
    )


def _ready_source_result(
    days: tuple[date, ...],
    as_of: datetime,
    *,
    data_gaps: tuple[str, ...] = (),
) -> MarginEvidenceSourceResult:
    return MarginEvidenceSourceResult(
        markets={
            venue: tuple(
                MarginObservation(
                    trade_date=day,
                    financing_balance=Decimal("10000") + index,
                    short_balance=Decimal("100") + index,
                    total_balance=Decimal("10100") + index * 2,
                    source_id="eastmoney",
                    source_revision="b" * 64,
                    captured_at=as_of,
                )
                for index, day in enumerate(days)
            )
            for venue in ("SH", "SZ")
        },
        instruments={
            "600519.SH": tuple(
                MarginObservation(
                    trade_date=day,
                    financing_balance=Decimal("1000") + index,
                    short_balance=Decimal("10") + index,
                    total_balance=Decimal("1010") + index * 2,
                    source_id="eastmoney",
                    source_revision="c" * 64,
                    captured_at=as_of,
                    free_float_market_cap=Decimal("100000") + index,
                    turnover=Decimal("10000") + index,
                    close=Decimal("10") + Decimal(index) / Decimal("100"),
                    volume=Decimal("500") + index,
                    denominator_trade_date=day,
                    denominator_source_id="eastmoney",
                    denominator_source_revision="d" * 64,
                    denominator_captured_at=as_of,
                )
                for index, day in enumerate(days)
            )
        },
        data_gaps=data_gaps,
    )
