"""Artifact-backed A-share trading calendar and intraday session authority.

The module never fetches a provider during authorization.  It validates one
frozen, dual-exchange artifact and returns typed point-in-time decisions.  A
calendar OPEN result is market evidence only; it is never capital permission.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

_CN_TZ = ZoneInfo("Asia/Shanghai")
_SCHEMA_VERSION = "a_share_trading_calendar.v1"
_SUPPORTED_PHASE_POLICY_VERSION = "a-share-order-entry-hours-2023-v1"
_EXPECTED_VENUES = frozenset({"SSE", "SZSE"})
_EVIDENCE_TIER_RANK = {
    "test_only": 0,
    "paper_candidate": 1,
    "qualified": 2,
}


class CalendarArtifactError(ValueError):
    """A frozen calendar artifact cannot be trusted or interpreted."""


class CalendarCoverageError(CalendarArtifactError):
    """A next-open-date request exceeds the frozen calendar coverage."""


class CalendarEvidenceTier(StrEnum):
    """Qualification level of the frozen source evidence."""

    TEST_ONLY = "test_only"
    PAPER_CANDIDATE = "paper_candidate"
    QUALIFIED = "qualified"


class TradingSessionStatus(StrEnum):
    """Whether order entry is available at the queried instant."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class TradingSessionPhase(StrEnum):
    """A-share order-entry phase using half-open time intervals."""

    PRE_OPEN = "PRE_OPEN"
    CONTINUOUS_AM = "CONTINUOUS_AM"
    BREAK = "BREAK"
    CONTINUOUS_PM = "CONTINUOUS_PM"
    AFTER_CLOSE = "AFTER_CLOSE"
    CLOSED_DAY = "CLOSED_DAY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CalendarSourceEvidence:
    """One exchange's annual closure notice frozen into the artifact."""

    venue: Literal["SSE", "SZSE"]
    source_id: str
    adapter_version: str
    notice_id: str
    official_url: str
    published_at: datetime
    closed_weekdays: tuple[date, ...]


@dataclass(frozen=True)
class TradingSessionInterval:
    """One configured order-entry interval."""

    phase: Literal[
        TradingSessionPhase.CONTINUOUS_AM,
        TradingSessionPhase.CONTINUOUS_PM,
    ]
    start: time
    end: time


@dataclass(frozen=True)
class _ValidatedCalendarArtifact:
    """Parsed values that can only be produced after the full artifact gate."""

    calendar_version: str
    source_policy_id: str
    phase_policy_version: str
    valid_from: date
    valid_through: date
    evidence_tier: CalendarEvidenceTier
    verified_at: datetime
    sources: tuple[CalendarSourceEvidence, ...]
    session_intervals: tuple[TradingSessionInterval, ...]
    snapshot_hash: str


@dataclass(frozen=True)
class TradingSessionDecision:
    """Typed, auditable calendar decision for one instant."""

    decision_id: str
    calendar_snapshot_id: str
    calendar_snapshot_hash: str
    calendar_version: str
    source_policy_id: str
    phase_policy_version: str
    evidence_tier: CalendarEvidenceTier
    queried_at: datetime
    trade_date: date
    status: TradingSessionStatus
    phase: TradingSessionPhase
    execution_allowed: bool
    data_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class NextOpenDateDecision:
    """Typed next-open-date result from the same calendar generation."""

    decision_id: str
    calendar_snapshot_id: str
    calendar_snapshot_hash: str
    calendar_version: str
    source_policy_id: str
    evidence_tier: CalendarEvidenceTier
    after: date
    known_at: datetime
    next_open_date: date


@dataclass(frozen=True)
class PreviousOpenDateDecision:
    """Typed previous-open-date result from the same calendar generation."""

    decision_id: str
    calendar_snapshot_id: str
    calendar_snapshot_hash: str
    calendar_version: str
    source_policy_id: str
    evidence_tier: CalendarEvidenceTier
    before: date
    known_at: datetime
    previous_open_date: date


class AShareTradingCalendar:
    """Deep calendar module shared by capital, authorization and PAPER broker."""

    def __init__(
        self,
        *,
        _validated: _ValidatedCalendarArtifact | None = None,
    ) -> None:
        if _validated is None:
            raise TypeError("use AShareTradingCalendar.from_file()")
        self._calendar_version = _validated.calendar_version
        self._source_policy_id = _validated.source_policy_id
        self._phase_policy_version = _validated.phase_policy_version
        self._valid_from = _validated.valid_from
        self._valid_through = _validated.valid_through
        self._evidence_tier = _validated.evidence_tier
        self._verified_at = _validated.verified_at
        self._sources = _validated.sources
        self._session_intervals = _validated.session_intervals
        self._closed_weekdays = frozenset(_validated.sources[0].closed_weekdays)
        self._snapshot_hash = _validated.snapshot_hash
        self._snapshot_id = _stable_id(
            "a-share-trading-calendar",
            _validated.calendar_version,
            _validated.source_policy_id,
            _validated.phase_policy_version,
            _validated.snapshot_hash,
        )

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        required_evidence_tier: CalendarEvidenceTier = CalendarEvidenceTier.PAPER_CANDIDATE,
    ) -> AShareTradingCalendar:
        """Load and validate one immutable calendar artifact without network access."""
        try:
            raw_payload = Path(path).read_bytes()
        except OSError as error:
            raise CalendarArtifactError("CALENDAR_ARTIFACT_UNAVAILABLE") from error
        return cls.from_bytes(
            raw_payload,
            required_evidence_tier=required_evidence_tier,
        )

    @classmethod
    def from_bytes(
        cls,
        raw_payload: bytes,
        *,
        required_evidence_tier: CalendarEvidenceTier = CalendarEvidenceTier.PAPER_CANDIDATE,
    ) -> AShareTradingCalendar:
        """Validate exact persisted calendar bytes without consulting a path."""
        try:
            payload = json.loads(raw_payload.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise CalendarArtifactError("CALENDAR_ARTIFACT_UNAVAILABLE") from error
        if not isinstance(payload, dict):
            raise CalendarArtifactError("CALENDAR_ARTIFACT_INVALID")
        return cls._from_payload(
            payload,
            required_evidence_tier=required_evidence_tier,
        )

    @classmethod
    def _from_payload(
        cls,
        payload: dict[str, object],
        *,
        required_evidence_tier: CalendarEvidenceTier,
    ) -> AShareTradingCalendar:
        expected_hash = payload.get("artifact_sha256")
        if not isinstance(expected_hash, str) or expected_hash != _canonical_hash(payload):
            raise CalendarArtifactError("CALENDAR_ARTIFACT_HASH_MISMATCH")
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise CalendarArtifactError("CALENDAR_SCHEMA_UNSUPPORTED")
        if payload.get("timezone") != _CN_TZ.key:
            raise CalendarArtifactError("CALENDAR_TIMEZONE_INVALID")
        try:
            calendar_version = _required_text(payload, "calendar_version")
            source_policy_id = _required_text(payload, "source_policy_id")
            phase_policy_version = _required_text(payload, "phase_policy_version")
            valid_from = date.fromisoformat(_required_text(payload, "valid_from"))
            valid_through = date.fromisoformat(_required_text(payload, "valid_through"))
            evidence_tier = CalendarEvidenceTier(_required_text(payload, "evidence_tier"))
            verified_at = datetime.fromisoformat(_required_text(payload, "verified_at"))
        except (TypeError, ValueError) as error:
            raise CalendarArtifactError("CALENDAR_ARTIFACT_INVALID") from error
        _require_aware("verified_at", verified_at)
        if valid_through < valid_from:
            raise CalendarArtifactError("CALENDAR_COVERAGE_INVALID")
        if _tier_rank(evidence_tier) < _tier_rank(required_evidence_tier):
            raise CalendarArtifactError("CALENDAR_EVIDENCE_TIER_INSUFFICIENT")

        sources_raw = payload.get("sources")
        if not isinstance(sources_raw, list):
            raise CalendarArtifactError("CALENDAR_SOURCES_INVALID")
        sources = tuple(
            _parse_source(item, valid_from=valid_from, valid_through=valid_through)
            for item in sources_raw
        )
        if len(sources) != 2 or {source.venue for source in sources} != _EXPECTED_VENUES:
            raise CalendarArtifactError("CALENDAR_SOURCES_INVALID")
        if sources[0].closed_weekdays != sources[1].closed_weekdays:
            raise CalendarArtifactError("CALENDAR_SOURCE_DISAGREEMENT")
        if any(source.published_at > verified_at for source in sources):
            raise CalendarArtifactError("CALENDAR_VERIFIED_BEFORE_PUBLICATION")

        intervals_raw = payload.get("session_intervals")
        if not isinstance(intervals_raw, list):
            raise CalendarArtifactError("CALENDAR_SESSION_INTERVALS_INVALID")
        intervals = tuple(_parse_interval(item) for item in intervals_raw)
        _validate_intervals(
            intervals,
            phase_policy_version=phase_policy_version,
        )
        return cls(
            _validated=_ValidatedCalendarArtifact(
                calendar_version=calendar_version,
                source_policy_id=source_policy_id,
                phase_policy_version=phase_policy_version,
                valid_from=valid_from,
                valid_through=valid_through,
                evidence_tier=evidence_tier,
                verified_at=verified_at.astimezone(_CN_TZ),
                sources=sources,
                session_intervals=intervals,
                snapshot_hash=expected_hash,
            )
        )

    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def snapshot_hash(self) -> str:
        return self._snapshot_hash

    @property
    def calendar_version(self) -> str:
        return self._calendar_version

    @property
    def source_policy_id(self) -> str:
        return self._source_policy_id

    @property
    def phase_policy_version(self) -> str:
        return self._phase_policy_version

    @property
    def evidence_tier(self) -> CalendarEvidenceTier:
        return self._evidence_tier

    @property
    def sources(self) -> tuple[CalendarSourceEvidence, ...]:
        return self._sources

    def require_evidence_tier(self, required: CalendarEvidenceTier) -> None:
        """Reject a calendar generation weaker than the consumer's usage scope."""
        if _tier_rank(self._evidence_tier) < _tier_rank(required):
            raise CalendarArtifactError("CALENDAR_EVIDENCE_TIER_INSUFFICIENT")

    def session_at(self, at: datetime) -> TradingSessionDecision:
        """Classify one aware instant using artifact coverage and half-open intervals."""
        _require_aware("at", at)
        local = at.astimezone(_CN_TZ)
        trade_date = local.date()
        gaps: tuple[str, ...] = ()
        if local < self._verified_at:
            status = TradingSessionStatus.UNKNOWN
            phase = TradingSessionPhase.UNKNOWN
            gaps = ("CALENDAR_NOT_KNOWN_AT_QUERY",)
        elif not (self._valid_from <= trade_date <= self._valid_through):
            status = TradingSessionStatus.UNKNOWN
            phase = TradingSessionPhase.UNKNOWN
            gaps = ("CALENDAR_DATE_OUTSIDE_COVERAGE",)
        elif not self._is_open_date(trade_date):
            status = TradingSessionStatus.CLOSED
            phase = TradingSessionPhase.CLOSED_DAY
        else:
            phase = self._phase_at(local.time())
            status = (
                TradingSessionStatus.OPEN
                if phase
                in {
                    TradingSessionPhase.CONTINUOUS_AM,
                    TradingSessionPhase.CONTINUOUS_PM,
                }
                else TradingSessionStatus.CLOSED
            )
        decision_id = _stable_id(
            "a-share-session-decision",
            self._snapshot_id,
            local.isoformat(),
            status.value,
            phase.value,
            *gaps,
        )
        return TradingSessionDecision(
            decision_id=decision_id,
            calendar_snapshot_id=self._snapshot_id,
            calendar_snapshot_hash=self._snapshot_hash,
            calendar_version=self._calendar_version,
            source_policy_id=self._source_policy_id,
            phase_policy_version=self._phase_policy_version,
            evidence_tier=self._evidence_tier,
            queried_at=local,
            trade_date=trade_date,
            status=status,
            phase=phase,
            execution_allowed=status is TradingSessionStatus.OPEN,
            data_gaps=gaps,
        )

    def next_open_date(self, *, after: date, known_at: datetime) -> NextOpenDateDecision:
        """Return the next explicit open date or fail instead of guessing."""
        _require_aware("known_at", known_at)
        local_known_at = known_at.astimezone(_CN_TZ)
        if local_known_at < self._verified_at:
            raise CalendarCoverageError("CALENDAR_NOT_KNOWN_AT_QUERY")
        candidate = after + timedelta(days=1)
        if candidate < self._valid_from:
            candidate = self._valid_from
        while candidate <= self._valid_through:
            if self._is_open_date(candidate):
                return NextOpenDateDecision(
                    decision_id=_stable_id(
                        "a-share-next-open-date",
                        self._snapshot_id,
                        after.isoformat(),
                        local_known_at.isoformat(),
                        candidate.isoformat(),
                    ),
                    calendar_snapshot_id=self._snapshot_id,
                    calendar_snapshot_hash=self._snapshot_hash,
                    calendar_version=self._calendar_version,
                    source_policy_id=self._source_policy_id,
                    evidence_tier=self._evidence_tier,
                    after=after,
                    known_at=local_known_at,
                    next_open_date=candidate,
                )
            candidate += timedelta(days=1)
        raise CalendarCoverageError("CALENDAR_COVERAGE_EXHAUSTED")

    def previous_open_date(
        self,
        *,
        before: date,
        known_at: datetime,
    ) -> PreviousOpenDateDecision:
        """Return the previous explicit open date or fail instead of guessing."""

        _require_aware("known_at", known_at)
        local_known_at = known_at.astimezone(_CN_TZ)
        if local_known_at < self._verified_at:
            raise CalendarCoverageError("CALENDAR_NOT_KNOWN_AT_QUERY")
        if before > self._valid_through + timedelta(days=1):
            raise CalendarCoverageError("CALENDAR_COVERAGE_EXHAUSTED")
        candidate = before - timedelta(days=1)
        if candidate > self._valid_through:
            candidate = self._valid_through
        while candidate >= self._valid_from:
            if self._is_open_date(candidate):
                return PreviousOpenDateDecision(
                    decision_id=_stable_id(
                        "a-share-previous-open-date",
                        self._snapshot_id,
                        before.isoformat(),
                        local_known_at.isoformat(),
                        candidate.isoformat(),
                    ),
                    calendar_snapshot_id=self._snapshot_id,
                    calendar_snapshot_hash=self._snapshot_hash,
                    calendar_version=self._calendar_version,
                    source_policy_id=self._source_policy_id,
                    evidence_tier=self._evidence_tier,
                    before=before,
                    known_at=local_known_at,
                    previous_open_date=candidate,
                )
            candidate -= timedelta(days=1)
        raise CalendarCoverageError("CALENDAR_COVERAGE_EXHAUSTED")

    def _is_open_date(self, value: date) -> bool:
        return value.weekday() < 5 and value not in self._closed_weekdays

    def _phase_at(self, value: time) -> TradingSessionPhase:
        first, second = self._session_intervals
        if value < first.start:
            return TradingSessionPhase.PRE_OPEN
        if first.start <= value < first.end:
            return TradingSessionPhase.CONTINUOUS_AM
        if value < second.start:
            return TradingSessionPhase.BREAK
        if second.start <= value < second.end:
            return TradingSessionPhase.CONTINUOUS_PM
        return TradingSessionPhase.AFTER_CLOSE


def _parse_source(
    raw: object,
    *,
    valid_from: date,
    valid_through: date,
) -> CalendarSourceEvidence:
    if not isinstance(raw, dict):
        raise CalendarArtifactError("CALENDAR_SOURCES_INVALID")
    venue = raw.get("venue")
    if venue not in _EXPECTED_VENUES:
        raise CalendarArtifactError("CALENDAR_SOURCES_INVALID")
    official_url = _required_text(raw, "official_url")
    host = (urlparse(official_url).hostname or "").lower()
    expected_domain = "sse.com.cn" if venue == "SSE" else "szse.cn"
    if host != expected_domain and not host.endswith(f".{expected_domain}"):
        raise CalendarArtifactError("CALENDAR_SOURCE_DOMAIN_INVALID")
    try:
        published_at = datetime.fromisoformat(_required_text(raw, "published_at"))
    except ValueError as error:
        raise CalendarArtifactError("CALENDAR_SOURCES_INVALID") from error
    _require_aware("published_at", published_at)
    closed_raw = raw.get("closed_weekdays")
    if not isinstance(closed_raw, list) or not all(isinstance(item, str) for item in closed_raw):
        raise CalendarArtifactError("CALENDAR_CLOSED_DATES_INVALID")
    try:
        closed = tuple(date.fromisoformat(item) for item in closed_raw)
    except ValueError as error:
        raise CalendarArtifactError("CALENDAR_CLOSED_DATES_INVALID") from error
    if (
        tuple(sorted(closed)) != closed
        or len(set(closed)) != len(closed)
        or any(
            value.weekday() >= 5 or not (valid_from <= value <= valid_through) for value in closed
        )
    ):
        raise CalendarArtifactError("CALENDAR_CLOSED_DATES_INVALID")
    return CalendarSourceEvidence(
        venue=cast(Literal["SSE", "SZSE"], venue),
        source_id=_required_text(raw, "source_id"),
        adapter_version=_required_text(raw, "adapter_version"),
        notice_id=_required_text(raw, "notice_id"),
        official_url=official_url,
        published_at=published_at.astimezone(_CN_TZ),
        closed_weekdays=closed,
    )


def _parse_interval(raw: object) -> TradingSessionInterval:
    if not isinstance(raw, dict):
        raise CalendarArtifactError("CALENDAR_SESSION_INTERVALS_INVALID")
    try:
        phase = TradingSessionPhase(_required_text(raw, "phase"))
        start = time.fromisoformat(_required_text(raw, "start"))
        end = time.fromisoformat(_required_text(raw, "end"))
    except ValueError as error:
        raise CalendarArtifactError("CALENDAR_SESSION_INTERVALS_INVALID") from error
    if phase not in {
        TradingSessionPhase.CONTINUOUS_AM,
        TradingSessionPhase.CONTINUOUS_PM,
    }:
        raise CalendarArtifactError("CALENDAR_SESSION_INTERVALS_INVALID")
    return TradingSessionInterval(
        phase=cast(
            Literal[
                TradingSessionPhase.CONTINUOUS_AM,
                TradingSessionPhase.CONTINUOUS_PM,
            ],
            phase,
        ),
        start=start,
        end=end,
    )


def _validate_intervals(
    intervals: tuple[TradingSessionInterval, ...],
    *,
    phase_policy_version: str,
) -> None:
    if phase_policy_version != _SUPPORTED_PHASE_POLICY_VERSION:
        raise CalendarArtifactError("CALENDAR_PHASE_POLICY_UNSUPPORTED")
    expected = (
        TradingSessionInterval(
            phase=TradingSessionPhase.CONTINUOUS_AM,
            start=time(9, 30),
            end=time(11, 30),
        ),
        TradingSessionInterval(
            phase=TradingSessionPhase.CONTINUOUS_PM,
            start=time(13, 0),
            end=time(15, 0),
        ),
    )
    if intervals != expected:
        raise CalendarArtifactError("CALENDAR_SESSION_INTERVALS_POLICY_MISMATCH")


def _canonical_hash(payload: dict[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _stable_id(namespace: str, *parts: object) -> str:
    encoded = "\x1f".join(str(part) for part in parts).encode()
    return f"{namespace}:{hashlib.sha256(encoded).hexdigest()[:32]}"


def _tier_rank(value: CalendarEvidenceTier) -> int:
    return _EVIDENCE_TIER_RANK[value.value]


def _required_text(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CalendarArtifactError("CALENDAR_ARTIFACT_INVALID")
    return value.strip()


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CalendarArtifactError(f"{name.upper()}_MUST_BE_TIMEZONE_AWARE")
