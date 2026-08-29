"""FIN-owned A-share calendar authority backed by frozen source evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fin_analyse.market.trading_calendar import (
    AShareTradingCalendar,
    CalendarArtifactError,
    CalendarCoverageError,
    CalendarEvidenceTier,
    TradingSessionPhase,
    TradingSessionStatus,
)

CN_TZ = ZoneInfo("Asia/Shanghai")
REPO_ROOT = Path(__file__).resolve().parents[2]
OFFICIAL_2026 = REPO_ROOT / "config/market/a_share_calendar_2026.json"


def _canonical_hash(payload: dict[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _payload(*, tier: str = "test_only") -> dict[str, object]:
    closed_dates = ["2026-09-25", "2026-10-01", "2026-10-02"]
    payload: dict[str, object] = {
        "schema_version": "a_share_trading_calendar.v1",
        "calendar_version": "sse-szse-2026-test-v1",
        "source_policy_id": "official-notice-dual-exchange-v1",
        "phase_policy_version": "a-share-order-entry-hours-2023-v1",
        "valid_from": "2026-01-01",
        "valid_through": "2026-12-31",
        "timezone": "Asia/Shanghai",
        "evidence_tier": tier,
        "verified_at": "2026-07-18T08:00:00+08:00",
        "session_intervals": [
            {"phase": "CONTINUOUS_AM", "start": "09:30", "end": "11:30"},
            {"phase": "CONTINUOUS_PM", "start": "13:00", "end": "15:00"},
        ],
        "sources": [
            {
                "venue": "SSE",
                "source_id": "sse-official-closure-notice",
                "adapter_version": "official-notice-file.v1",
                "notice_id": "SSE-2026-ANNUAL",
                "official_url": "https://www.sse.com.cn/official-calendar",
                "published_at": "2025-12-22T18:00:00+08:00",
                "closed_weekdays": closed_dates,
            },
            {
                "venue": "SZSE",
                "source_id": "szse-official-closure-notice",
                "adapter_version": "official-notice-file.v1",
                "notice_id": "SZSE-2026-ANNUAL",
                "official_url": "https://www.szse.cn/official-calendar",
                "published_at": "2025-12-22T18:00:00+08:00",
                "closed_weekdays": closed_dates,
            },
        ],
    }
    payload["artifact_sha256"] = _canonical_hash(payload)
    return payload


def _write_artifact(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_official_2026_calendar_closes_exchange_holidays_and_weekends() -> None:
    calendar = AShareTradingCalendar.from_file(
        OFFICIAL_2026,
        required_evidence_tier=CalendarEvidenceTier.PAPER_CANDIDATE,
    )

    holiday = calendar.session_at(
        datetime(2026, 9, 25, 14, 40, tzinfo=CN_TZ),
    )
    weekend = calendar.session_at(
        datetime(2026, 7, 18, 10, 3, tzinfo=CN_TZ),
    )
    open_day = calendar.session_at(
        datetime(2026, 9, 28, 14, 40, tzinfo=CN_TZ),
    )

    assert holiday.status is TradingSessionStatus.CLOSED
    assert holiday.phase is TradingSessionPhase.CLOSED_DAY
    assert weekend.status is TradingSessionStatus.CLOSED
    assert open_day.status is TradingSessionStatus.OPEN
    assert open_day.execution_allowed is True
    assert open_day.calendar_snapshot_hash == calendar.snapshot_hash
    assert {source.venue for source in calendar.sources} == {"SSE", "SZSE"}


@pytest.mark.parametrize(
    ("hour", "minute", "expected_phase", "allowed"),
    [
        (9, 29, TradingSessionPhase.PRE_OPEN, False),
        (9, 30, TradingSessionPhase.CONTINUOUS_AM, True),
        (11, 29, TradingSessionPhase.CONTINUOUS_AM, True),
        (11, 30, TradingSessionPhase.BREAK, False),
        (13, 0, TradingSessionPhase.CONTINUOUS_PM, True),
        (14, 40, TradingSessionPhase.CONTINUOUS_PM, True),
        (15, 0, TradingSessionPhase.AFTER_CLOSE, False),
    ],
)
def test_session_intervals_are_left_closed_and_right_open(
    tmp_path: Path,
    hour: int,
    minute: int,
    expected_phase: TradingSessionPhase,
    allowed: bool,
) -> None:
    calendar = AShareTradingCalendar.from_file(
        _write_artifact(tmp_path, _payload()),
        required_evidence_tier=CalendarEvidenceTier.TEST_ONLY,
    )

    decision = calendar.session_at(datetime(2026, 7, 20, hour, minute, tzinfo=CN_TZ))

    assert decision.phase is expected_phase
    assert decision.execution_allowed is allowed


def test_next_open_date_skips_official_holiday_and_never_guesses_past_coverage(
    tmp_path: Path,
) -> None:
    calendar = AShareTradingCalendar.from_file(
        _write_artifact(tmp_path, _payload()),
        required_evidence_tier=CalendarEvidenceTier.TEST_ONLY,
    )

    decision = calendar.next_open_date(
        after=date(2026, 9, 24),
        known_at=datetime(2026, 9, 24, 15, 0, tzinfo=CN_TZ),
    )

    assert decision.next_open_date == date(2026, 9, 28)
    assert decision.calendar_snapshot_id == calendar.snapshot_id
    with pytest.raises(CalendarCoverageError, match="CALENDAR_COVERAGE_EXHAUSTED"):
        calendar.next_open_date(
            after=date(2026, 12, 31),
            known_at=datetime(2026, 12, 31, 15, 0, tzinfo=CN_TZ),
        )


def test_previous_open_date_uses_same_generation_and_never_guesses_before_coverage(
    tmp_path: Path,
) -> None:
    calendar = AShareTradingCalendar.from_file(
        _write_artifact(tmp_path, _payload()),
        required_evidence_tier=CalendarEvidenceTier.TEST_ONLY,
    )

    decision = calendar.previous_open_date(
        before=date(2026, 9, 28),
        known_at=datetime(2026, 9, 28, 14, 40, tzinfo=CN_TZ),
    )

    assert decision.previous_open_date == date(2026, 9, 24)
    assert decision.calendar_snapshot_id == calendar.snapshot_id
    with pytest.raises(CalendarCoverageError, match="CALENDAR_COVERAGE_EXHAUSTED"):
        calendar.previous_open_date(
            before=date(2026, 1, 1),
            known_at=datetime(2026, 7, 20, 14, 40, tzinfo=CN_TZ),
        )


def test_calendar_rejects_hash_drift_exchange_disagreement_and_weak_evidence(
    tmp_path: Path,
) -> None:
    hash_drift = _payload()
    hash_drift["calendar_version"] = "tampered"
    with pytest.raises(CalendarArtifactError, match="CALENDAR_ARTIFACT_HASH_MISMATCH"):
        AShareTradingCalendar.from_file(
            _write_artifact(tmp_path, hash_drift),
            required_evidence_tier=CalendarEvidenceTier.TEST_ONLY,
        )

    disagreement = _payload()
    sources = disagreement["sources"]
    assert isinstance(sources, list)
    assert isinstance(sources[1], dict)
    sources[1]["closed_weekdays"] = ["2026-09-25"]
    disagreement["artifact_sha256"] = _canonical_hash(disagreement)
    with pytest.raises(CalendarArtifactError, match="CALENDAR_SOURCE_DISAGREEMENT"):
        AShareTradingCalendar.from_file(
            _write_artifact(tmp_path, disagreement),
            required_evidence_tier=CalendarEvidenceTier.TEST_ONLY,
        )

    test_only = _payload(tier="test_only")
    with pytest.raises(CalendarArtifactError, match="CALENDAR_EVIDENCE_TIER_INSUFFICIENT"):
        AShareTradingCalendar.from_file(_write_artifact(tmp_path, test_only))
    with pytest.raises(CalendarArtifactError, match="CALENDAR_EVIDENCE_TIER_INSUFFICIENT"):
        AShareTradingCalendar.from_file(
            _write_artifact(tmp_path, test_only),
            required_evidence_tier=CalendarEvidenceTier.PAPER_CANDIDATE,
        )


def test_calendar_rejects_policy_drift_and_direct_unvalidated_construction(
    tmp_path: Path,
) -> None:
    drifted = _payload()
    intervals = drifted["session_intervals"]
    assert isinstance(intervals, list)
    assert isinstance(intervals[0], dict)
    intervals[0]["start"] = "00:00"
    drifted["artifact_sha256"] = _canonical_hash(drifted)

    with pytest.raises(
        CalendarArtifactError,
        match="CALENDAR_SESSION_INTERVALS_POLICY_MISMATCH",
    ):
        AShareTradingCalendar.from_file(
            _write_artifact(tmp_path, drifted),
            required_evidence_tier=CalendarEvidenceTier.TEST_ONLY,
        )
    with pytest.raises(TypeError, match="from_file"):
        AShareTradingCalendar()
