"""Isolated, read-only market-data qualification observer.

This module records qualification evidence under an explicit output root.  A
qualification verdict is evidence about a data source; it is never a trading
permission and never changes provider routing or production caches.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from time import monotonic_ns as _system_monotonic_ns
from typing import Protocol

from fin_analyse.market.index_symbols import MAJOR_INDEX_SYMBOLS
from fin_analyse.market.providers.base import QuoteResult
from fin_analyse.market.system_clock_evidence import (
    A_SHARE_CLOCK_MAX_OFFSET_MS,
    accepted_clock_offset_ms,
    clock_corrected_time,
)
from fin_analyse.utils.jsonl import read_jsonl

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SENSITIVE_RAW_FIELDS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
QUALIFICATION_CAPTURE_SCHEMA_VERSION = "data_capture.v2"
QUALIFICATION_REPORT_SCHEMA_VERSION = "data_qualification_report.v2"
QUALIFICATION_PUBLICATION_RECEIPT_SCHEMA_VERSION = "data_qualification_publication_receipt.v2"


class QualificationCheckpoint(StrEnum):
    """Supported observation checkpoints for the first qualification slice."""

    MORNING_WINDOW_1003 = "morning_window_1003"
    CLOSING_PRECOMPUTE_1435 = "closing_precompute_1435"
    EXECUTION_REFRESH_1439 = "execution_refresh_1439"
    DECISION_BIND_1440 = "decision_bind_1440"


class CheckpointTimingStatus(StrEnum):
    """Observed readiness against a checkpoint target and hard deadline."""

    TARGET_MET = "target_met"
    TARGET_MISSED = "target_missed"
    HARD_DEADLINE_MISSED = "hard_deadline_missed"


class QualificationDataset(StrEnum):
    """Dataset identity kept separate from provider and usage scope."""

    REALTIME_QUOTE = "realtime_quote"


class QualificationUsageScope(StrEnum):
    """A scope for which captured data is being assessed."""

    RESEARCH_ONLY = "research_only"
    PAPER_TACTICAL_DECISION = "paper_tactical_decision"
    LIVE_TACTICAL_DECISION = "live_tactical_decision"
    LIVE_EXECUTION_REFERENCE = "live_execution_reference"


class QualificationVerdict(StrEnum):
    """Deterministic verdict for one requested usage scope."""

    QUALIFIED = "qualified"
    QUALIFIED_WITH_DEGRADATION = "qualified_with_degradation"
    NOT_QUALIFIED = "not_qualified"
    INCONCLUSIVE = "inconclusive"


class TradingStatus(StrEnum):
    """Minimal tradability state required by the capital-safety observer."""

    TRADING = "trading"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


class RiskDisposition(StrEnum):
    """Safety recommendation; never an executable authorization."""

    OBSERVATION_ONLY = "observation_only"
    NO_NEW_RISK = "no_new_risk"


class RawReplayStatus(StrEnum):
    """Whether persisted source bytes deterministically reproduce normalization."""

    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    MISMATCH = "mismatch"


class ObservationEvidenceOrigin(StrEnum):
    """Provenance that prevents fixtures from counting as live observations."""

    TEST_ONLY = "test_only"
    LIVE_CAPTURE = "live_capture"


@dataclass(frozen=True)
class PaperQualificationSubjectPolicy:
    """FIN-fixed source role and the gaps it may count for data reliability."""

    role: str
    provider: str
    source_policy_id: str
    source_id: str
    adapter_version: str
    observer_version: str
    threshold_version: str
    max_clock_offset_ms: int
    max_source_age_ms: int
    max_receive_age_ms: int
    allowed_reliability_gaps: frozenset[str]


A_SHARE_PAPER_PRIMARY_POLICY = PaperQualificationSubjectPolicy(
    role="primary",
    provider="eastmoney-raw",
    source_policy_id="eastmoney-paper-primary-v1",
    source_id="eastmoney_raw",
    adapter_version="eastmoney_raw_qualification.v1",
    observer_version="data-qualification.v3",
    threshold_version="a-share-data-thresholds.v2",
    max_clock_offset_ms=A_SHARE_CLOCK_MAX_OFFSET_MS,
    max_source_age_ms=3_000,
    max_receive_age_ms=2_000,
    allowed_reliability_gaps=frozenset(),
)
A_SHARE_PAPER_REFERENCE_POLICY = PaperQualificationSubjectPolicy(
    role="reference",
    provider="tencent-raw",
    source_policy_id="tencent-paper-reference-v1",
    source_id="tencent_raw",
    adapter_version="tencent_raw_qualification.v1",
    observer_version="data-qualification.v3",
    threshold_version="a-share-data-thresholds.v2",
    max_clock_offset_ms=A_SHARE_CLOCK_MAX_OFFSET_MS,
    max_source_age_ms=3_000,
    max_receive_age_ms=2_000,
    allowed_reliability_gaps=frozenset(
        {
            "trading_status_unknown",
            "price_limits_missing",
        }
    ),
)


class QualificationArtifactConflictError(RuntimeError):
    """Raised when an immutable artifact ID is reused with different content."""


@dataclass(frozen=True)
class QualificationSample:
    """One symbol/venue pair frozen into a sample manifest."""

    symbol: str
    venue: str

    def __post_init__(self) -> None:
        if not self.symbol or not self.venue:
            raise ValueError("qualification sample requires symbol and venue")

    def to_dict(self) -> dict[str, str]:
        return {"symbol": self.symbol, "venue": self.venue}


@dataclass(frozen=True)
class SampleManifest:
    """Immutable, pre-observation sample selection."""

    manifest_id: str
    created_at: datetime
    selection_cutoff_at: datetime
    samples: tuple[QualificationSample, ...]
    manifest_hash: str
    schema_version: str = "sample_manifest.v1"

    @classmethod
    def build(
        cls,
        *,
        manifest_id: str,
        created_at: datetime,
        selection_cutoff_at: datetime,
        samples: tuple[QualificationSample, ...],
    ) -> SampleManifest:
        _validate_identifier("manifest_id", manifest_id)
        _require_aware("created_at", created_at)
        _require_aware("selection_cutoff_at", selection_cutoff_at)
        if selection_cutoff_at < created_at:
            raise ValueError("selection_cutoff_at must not precede created_at")
        if not samples:
            raise ValueError("sample manifest must contain at least one sample")
        if len({(sample.symbol, sample.venue) for sample in samples}) != len(samples):
            raise ValueError("sample manifest contains duplicate symbol/venue pairs")
        payload = {
            "schema_version": "sample_manifest.v1",
            "manifest_id": manifest_id,
            "created_at": created_at.isoformat(),
            "selection_cutoff_at": selection_cutoff_at.isoformat(),
            "samples": [sample.to_dict() for sample in samples],
        }
        return cls(
            manifest_id=manifest_id,
            created_at=created_at,
            selection_cutoff_at=selection_cutoff_at,
            samples=samples,
            manifest_hash=_sha256(_canonical_bytes(payload)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "created_at": self.created_at.isoformat(),
            "selection_cutoff_at": self.selection_cutoff_at.isoformat(),
            "samples": [sample.to_dict() for sample in self.samples],
            "manifest_hash": self.manifest_hash,
        }


@dataclass(frozen=True)
class QualificationSourceCapture:
    """One adapter capture before FIN-owned qualification evaluation."""

    symbol: str
    venue: str | None
    requested_at: datetime
    received_at: datetime
    fetch_duration_ms: int
    source_event_at: datetime | None
    price: str | None
    trading_status: TradingStatus
    upper_limit_price: str | None
    lower_limit_price: str | None
    raw_payload: bytes
    raw_payload_kind: str
    data_gaps: tuple[str, ...] = ()
    volume: str | None = None
    turnover: str | None = None

    def __post_init__(self) -> None:
        _require_aware("requested_at", self.requested_at)
        _require_aware("received_at", self.received_at)
        if self.fetch_duration_ms < 0:
            raise ValueError("fetch_duration_ms must be non-negative monotonic evidence")
        if self.source_event_at is not None:
            _require_aware("source_event_at", self.source_event_at)


@dataclass(frozen=True)
class QualificationNormalizedRecord:
    """Deterministic normalized facts that must be reproducible from raw bytes."""

    symbol: str
    venue: str | None
    source_event_at: datetime | None
    price: str | None
    trading_status: TradingStatus
    upper_limit_price: str | None
    lower_limit_price: str | None
    volume: str | None = None
    turnover: str | None = None

    def __post_init__(self) -> None:
        if self.source_event_at is not None:
            _require_aware("source_event_at", self.source_event_at)

    @classmethod
    def from_capture(cls, capture: QualificationSourceCapture) -> QualificationNormalizedRecord:
        return cls(
            symbol=capture.symbol,
            venue=capture.venue,
            source_event_at=capture.source_event_at,
            price=capture.price,
            trading_status=capture.trading_status,
            upper_limit_price=capture.upper_limit_price,
            lower_limit_price=capture.lower_limit_price,
            volume=capture.volume,
            turnover=capture.turnover,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "source_event_at": (
                self.source_event_at.isoformat() if self.source_event_at is not None else None
            ),
            "price": self.price,
            "trading_status": self.trading_status.value,
            "upper_limit_price": self.upper_limit_price,
            "lower_limit_price": self.lower_limit_price,
            "volume": self.volume,
            "turnover": self.turnover,
        }


class QualificationSourcePort(Protocol):
    """Internal seam for one explicit data-source adapter."""

    source_id: str
    adapter_version: str

    @property
    def evidence_origin(self) -> ObservationEvidenceOrigin:
        """Return the immutable evidence authority bound by the source."""
        ...

    def capture(self, sample: QualificationSample) -> QualificationSourceCapture:
        """Capture one sample without provider fallback."""
        ...

    def replay_normalize(
        self,
        sample: QualificationSample,
        raw_payload: bytes,
    ) -> QualificationNormalizedRecord:
        """Rebuild normalized facts from the exact persisted bytes."""
        ...


class LegacyQuoteProvider(Protocol):
    """Narrow view of an existing research-grade quote provider."""

    @property
    def name(self) -> str:
        """Stable provider identity."""
        ...

    def get_quote(self, ticker: str) -> QuoteResult:
        """Return the provider's normalized quote result."""
        ...


class LegacyQuoteQualificationAdapter:
    """Observe one legacy provider without registry fallback or cache writes.

    Existing ``QuoteResult`` values do not expose an upstream source timestamp,
    verified venue, trading status, price limits, or exact upstream payload.  The
    adapter records those gaps instead of fabricating execution-grade facts.
    """

    adapter_version = "legacy_quote_qualification.v1"

    def __init__(
        self,
        *,
        provider: LegacyQuoteProvider,
        clock: Callable[[], datetime] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
        evidence_origin: ObservationEvidenceOrigin = ObservationEvidenceOrigin.TEST_ONLY,
    ) -> None:
        self._provider = provider
        self._clock = clock or _utc_now
        self._monotonic_ns = monotonic_ns or _system_monotonic_ns
        self.evidence_origin = evidence_origin
        self.source_id = provider.name

    def capture(self, sample: QualificationSample) -> QualificationSourceCapture:
        started_ns = self._monotonic_ns()
        requested_at = self._clock()
        quote = self._provider.get_quote(sample.symbol)
        received_at = self._clock()
        fetch_duration_ms = (self._monotonic_ns() - started_ns) // 1_000_000
        normalized = {
            "provider": self._provider.name,
            "requested_symbol": sample.symbol,
            "returned": {
                "ticker": quote.ticker,
                "name": quote.name,
                "price": quote.price,
                "change_pct": quote.change_pct,
                "volume": quote.volume,
                "turnover": quote.turnover,
            },
        }
        return QualificationSourceCapture(
            symbol=quote.ticker,
            venue=None,
            requested_at=requested_at,
            received_at=received_at,
            fetch_duration_ms=fetch_duration_ms,
            source_event_at=None,
            price=_decimal_text(quote.price),
            trading_status=TradingStatus.UNKNOWN,
            upper_limit_price=None,
            lower_limit_price=None,
            raw_payload=_canonical_bytes(normalized),
            raw_payload_kind="normalized_provider_result",
            data_gaps=(
                "venue_unverified",
                "source_event_at_missing",
                "trading_status_unknown",
                "price_limits_missing",
                "upstream_raw_payload_unavailable",
            ),
        )

    def replay_normalize(
        self,
        sample: QualificationSample,
        raw_payload: bytes,
    ) -> QualificationNormalizedRecord:
        payload = json.loads(raw_payload)
        returned = payload["returned"]
        if not isinstance(returned, dict):
            raise ValueError("legacy normalized payload has no returned quote")
        price = returned.get("price")
        return QualificationNormalizedRecord(
            symbol=str(returned.get("ticker", sample.symbol)),
            venue=None,
            source_event_at=None,
            price=_decimal_text(float(price)) if price is not None else None,
            trading_status=TradingStatus.UNKNOWN,
            upper_limit_price=None,
            lower_limit_price=None,
        )


@dataclass(frozen=True)
class DataQualificationRequest:
    """Request one immutable checkpoint observation."""

    run_id: str
    campaign_id: str
    source_policy_id: str
    trade_date: date
    dataset: QualificationDataset
    usage_scope: QualificationUsageScope
    checkpoint: QualificationCheckpoint
    scheduled_at: datetime
    target_ready_by: datetime
    deadline_at: datetime
    manifest: SampleManifest
    clock_sync_status: str
    collector_clock_offset_ms: int | None
    observer_version: str = "data-qualification.v3"
    threshold_version: str = "a-share-data-thresholds.v2"
    max_clock_offset_ms: int = A_SHARE_CLOCK_MAX_OFFSET_MS
    max_source_age_ms: int = 3_000
    max_receive_age_ms: int = 2_000

    def __post_init__(self) -> None:
        _validate_identifier("run_id", self.run_id)
        _validate_identifier("campaign_id", self.campaign_id)
        _validate_identifier("source_policy_id", self.source_policy_id)
        _validate_identifier("observer_version", self.observer_version)
        _validate_identifier("threshold_version", self.threshold_version)
        _require_aware("scheduled_at", self.scheduled_at)
        _require_aware("target_ready_by", self.target_ready_by)
        _require_aware("deadline_at", self.deadline_at)
        if self.target_ready_by < self.scheduled_at:
            raise ValueError("target_ready_by must not precede scheduled_at")
        if self.deadline_at < self.target_ready_by:
            raise ValueError("deadline_at must not precede target_ready_by")
        if self.manifest.selection_cutoff_at > self.scheduled_at:
            raise ValueError("sample manifest must be frozen before scheduled_at")
        if (
            self.max_clock_offset_ms < 0
            or self.max_source_age_ms < 0
            or self.max_receive_age_ms < 0
        ):
            raise ValueError("qualification time limits must be non-negative")

    def run_spec_payload(
        self,
        *,
        source_id: str,
        adapter_version: str,
        evidence_origin: ObservationEvidenceOrigin,
    ) -> dict[str, object]:
        """Return the complete immutable identity of one observation run."""
        return {
            "schema_version": "data_qualification_run_spec.v1",
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "manifest_hash": self.manifest.manifest_hash,
            "source_policy_id": self.source_policy_id,
            "source_id": source_id,
            "adapter_version": adapter_version,
            "evidence_origin": evidence_origin.value,
            "trade_date": self.trade_date.isoformat(),
            "dataset": self.dataset.value,
            "usage_scope": self.usage_scope.value,
            "checkpoint": self.checkpoint.value,
            "scheduled_at": self.scheduled_at.isoformat(),
            "target_ready_by": self.target_ready_by.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "clock_sync_status": self.clock_sync_status,
            "collector_clock_offset_ms": self.collector_clock_offset_ms,
            "observer_version": self.observer_version,
            "threshold_version": self.threshold_version,
            "max_clock_offset_ms": self.max_clock_offset_ms,
            "max_source_age_ms": self.max_source_age_ms,
            "max_receive_age_ms": self.max_receive_age_ms,
        }


@dataclass(frozen=True)
class QualificationCaptureArtifact:
    """JSON-safe immutable evidence for one captured sample."""

    capture_id: str
    run_spec_hash: str
    run_id: str
    campaign_id: str
    manifest_hash: str
    source_policy_id: str
    trade_date: date
    evidence_origin: ObservationEvidenceOrigin
    dataset: QualificationDataset
    source_id: str
    adapter_version: str
    observer_version: str
    threshold_version: str
    usage_scope: QualificationUsageScope
    checkpoint: QualificationCheckpoint
    symbol: str
    venue: str | None
    scheduled_at: datetime
    target_ready_by: datetime
    deadline_at: datetime
    timing_status: CheckpointTimingStatus
    clock_sync_status: str
    collector_clock_offset_ms: int | None
    max_clock_offset_ms: int
    max_source_age_ms: int
    max_receive_age_ms: int
    requested_at: datetime
    received_at: datetime
    ready_at: datetime
    fetch_duration_ms: int
    normalization_duration_ms: int
    source_event_at: datetime | None
    price: str | None
    trading_status: TradingStatus
    upper_limit_price: str | None
    lower_limit_price: str | None
    raw_payload_sha256: str
    raw_payload_ref: str
    raw_payload_kind: str
    raw_replay_status: RawReplayStatus
    normalized_payload_sha256: str
    verdict: QualificationVerdict
    risk_disposition: RiskDisposition
    data_gaps: tuple[str, ...]
    schema_version: str = QUALIFICATION_CAPTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        accepted_offset_ms = accepted_clock_offset_ms(
            clock_sync_status=self.clock_sync_status,
            collector_clock_offset_ms=self.collector_clock_offset_ms,
            max_clock_offset_ms=self.max_clock_offset_ms,
        )
        corrected_requested_at = (
            clock_corrected_time(self.requested_at, accepted_offset_ms)
            if accepted_offset_ms is not None
            else None
        )
        corrected_received_at = (
            clock_corrected_time(self.received_at, accepted_offset_ms)
            if accepted_offset_ms is not None
            else None
        )
        corrected_ready_at = (
            clock_corrected_time(self.ready_at, accepted_offset_ms)
            if accepted_offset_ms is not None
            else None
        )
        return {
            "schema_version": self.schema_version,
            "capture_id": self.capture_id,
            "run_spec_hash": self.run_spec_hash,
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "manifest_hash": self.manifest_hash,
            "source_policy_id": self.source_policy_id,
            "trade_date": self.trade_date.isoformat(),
            "evidence_origin": self.evidence_origin.value,
            "dataset": self.dataset.value,
            "source_id": self.source_id,
            "adapter_version": self.adapter_version,
            "observer_version": self.observer_version,
            "threshold_version": self.threshold_version,
            "usage_scope": self.usage_scope.value,
            "checkpoint": self.checkpoint.value,
            "symbol": self.symbol,
            "venue": self.venue,
            "scheduled_at": self.scheduled_at.isoformat(),
            "target_ready_by": self.target_ready_by.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "timing_status": self.timing_status.value,
            "clock_sync_status": self.clock_sync_status,
            "collector_clock_offset_ms": self.collector_clock_offset_ms,
            "max_clock_offset_ms": self.max_clock_offset_ms,
            "max_source_age_ms": self.max_source_age_ms,
            "max_receive_age_ms": self.max_receive_age_ms,
            "requested_at": self.requested_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "ready_at": self.ready_at.isoformat(),
            "clock_correction_applied": accepted_offset_ms is not None,
            "accepted_clock_offset_ms": accepted_offset_ms,
            "clock_corrected_requested_at": (
                corrected_requested_at.isoformat() if corrected_requested_at else None
            ),
            "clock_corrected_received_at": (
                corrected_received_at.isoformat() if corrected_received_at else None
            ),
            "clock_corrected_ready_at": (
                corrected_ready_at.isoformat() if corrected_ready_at else None
            ),
            "fetch_duration_ms": self.fetch_duration_ms,
            "normalization_duration_ms": self.normalization_duration_ms,
            "scheduler_lag_ms": (
                _duration_ms(self.scheduled_at, corrected_requested_at)
                if corrected_requested_at is not None
                else None
            ),
            "normalization_lag_ms": _duration_ms(self.received_at, self.ready_at),
            "ready_lag_ms": (
                _duration_ms(self.scheduled_at, corrected_ready_at)
                if corrected_ready_at is not None
                else None
            ),
            "source_event_at": (
                self.source_event_at.isoformat() if self.source_event_at is not None else None
            ),
            "source_age_at_receive_ms": (
                _duration_ms(self.source_event_at, corrected_received_at)
                if self.source_event_at is not None and corrected_received_at is not None
                else None
            ),
            "source_age_at_ready_ms": (
                _duration_ms(self.source_event_at, corrected_ready_at)
                if self.source_event_at is not None and corrected_ready_at is not None
                else None
            ),
            "price": self.price,
            "trading_status": self.trading_status.value,
            "upper_limit_price": self.upper_limit_price,
            "lower_limit_price": self.lower_limit_price,
            "raw_payload_sha256": self.raw_payload_sha256,
            "raw_payload_ref": self.raw_payload_ref,
            "raw_payload_kind": self.raw_payload_kind,
            "raw_replay_status": self.raw_replay_status.value,
            "normalized_payload_sha256": self.normalized_payload_sha256,
            "verdict": self.verdict.value,
            "risk_disposition": self.risk_disposition.value,
            "data_gaps": list(self.data_gaps),
        }


@dataclass(frozen=True)
class QualificationPublicationReceipt:
    """Post-fsync evidence of when a report became available to consumers."""

    run_id: str
    run_spec_hash: str
    report_hash: str
    published_at: datetime
    timing_status: CheckpointTimingStatus
    processing_duration_ms: int
    wall_clock_order_valid: bool
    qualification_count_eligible: bool
    receipt_hash: str
    schema_version: str = QUALIFICATION_PUBLICATION_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_aware("published_at", self.published_at)
        if self.processing_duration_ms < 0:
            raise ValueError("processing_duration_ms must be non-negative")

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "run_spec_hash": self.run_spec_hash,
            "report_hash": self.report_hash,
            "published_at": self.published_at.isoformat(),
            "timing_status": self.timing_status.value,
            "processing_duration_ms": self.processing_duration_ms,
            "wall_clock_order_valid": self.wall_clock_order_valid,
            "qualification_count_eligible": self.qualification_count_eligible,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "receipt_hash": self.receipt_hash}


@dataclass(frozen=True)
class DataQualificationResult:
    """Checkpoint result with explicit non-execution boundary fields."""

    run_id: str
    run_spec_hash: str
    report_hash: str
    campaign_id: str
    manifest_hash: str
    source_policy_id: str
    source_id: str
    adapter_version: str
    observer_version: str
    threshold_version: str
    trade_date: date
    evidence_origin: ObservationEvidenceOrigin
    dataset: QualificationDataset
    usage_scope: QualificationUsageScope
    checkpoint: QualificationCheckpoint
    target_ready_by: datetime
    deadline_at: datetime
    ready_at: datetime
    timing_status: CheckpointTimingStatus
    verdict: QualificationVerdict
    risk_disposition: RiskDisposition
    captures: tuple[QualificationCaptureArtifact, ...]
    data_gaps: tuple[str, ...]
    observation_eligible: bool
    max_clock_offset_ms: int
    max_source_age_ms: int
    max_receive_age_ms: int
    execution_allowed: bool = False
    affects_provider_routing: bool = False
    writes_production_cache: bool = False
    publication_receipt: QualificationPublicationReceipt | None = None
    schema_version: str = QUALIFICATION_REPORT_SCHEMA_VERSION

    def report_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "run_spec_hash": self.run_spec_hash,
            "report_hash": self.report_hash,
            "campaign_id": self.campaign_id,
            "manifest_hash": self.manifest_hash,
            "source_policy_id": self.source_policy_id,
            "source_id": self.source_id,
            "adapter_version": self.adapter_version,
            "observer_version": self.observer_version,
            "threshold_version": self.threshold_version,
            "trade_date": self.trade_date.isoformat(),
            "evidence_origin": self.evidence_origin.value,
            "dataset": self.dataset.value,
            "usage_scope": self.usage_scope.value,
            "checkpoint": self.checkpoint.value,
            "target_ready_by": self.target_ready_by.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "ready_at": self.ready_at.isoformat(),
            "timing_status": self.timing_status.value,
            "verdict": self.verdict.value,
            "risk_disposition": self.risk_disposition.value,
            "captures": [capture.to_dict() for capture in self.captures],
            "data_gaps": list(self.data_gaps),
            "observation_eligible": self.observation_eligible,
            "max_clock_offset_ms": self.max_clock_offset_ms,
            "max_source_age_ms": self.max_source_age_ms,
            "max_receive_age_ms": self.max_receive_age_ms,
            "execution_allowed": self.execution_allowed,
            "affects_provider_routing": self.affects_provider_routing,
            "writes_production_cache": self.writes_production_cache,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.report_dict(),
            "publication_receipt": (
                self.publication_receipt.to_dict() if self.publication_receipt else None
            ),
        }


class DataQualificationService:
    """Deep module for capture, evaluation, and isolated evidence persistence."""

    def __init__(
        self,
        *,
        source: QualificationSourcePort,
        output_root: str | Path,
        clock: Callable[[], datetime] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
    ) -> None:
        self._source = source
        self._store = _QualificationArtifactStore(Path(output_root))
        self._clock = clock or _utc_now
        self._monotonic_ns = monotonic_ns or _system_monotonic_ns

    def observe(self, request: DataQualificationRequest) -> DataQualificationResult:
        """Capture every frozen sample and return a scoped qualification verdict."""
        observation_started_ns = self._monotonic_ns()
        run_spec_payload = request.run_spec_payload(
            source_id=self._source.source_id,
            adapter_version=self._source.adapter_version,
            evidence_origin=self._source.evidence_origin,
        )
        run_spec_hash = _sha256(_canonical_bytes(run_spec_payload))
        frozen_run_spec = {**run_spec_payload, "run_spec_hash": run_spec_hash}
        self._store.persist_run_spec(request.run_id, frozen_run_spec)
        self._store.persist_manifest(request.run_id, request.manifest)
        captures_with_raw: list[tuple[QualificationCaptureArtifact, bytes]] = []
        all_gaps: list[str] = []
        for sample in request.manifest.samples:
            try:
                source_capture = self._source.capture(sample)
            except Exception as exc:
                source_capture = self._failure_capture(sample, exc)
            normalization_started_ns = self._monotonic_ns()
            source_capture = _sanitize_sensitive_raw_capture(
                source_capture,
                source_id=self._source.source_id,
                adapter_version=self._source.adapter_version,
            )
            raw_replay_status, normalized_hash, replay_gaps = _verify_raw_replay(
                self._source,
                sample,
                source_capture,
            )
            normalization_duration_ms = (
                self._monotonic_ns() - normalization_started_ns
            ) // 1_000_000
            ready_at = self._clock()
            _require_aware("ready_at", ready_at)
            gaps = tuple(
                dict.fromkeys(
                    (
                        *_evaluate_capture(request, sample, source_capture, ready_at),
                        *replay_gaps,
                    )
                )
            )
            raw_hash = _sha256(source_capture.raw_payload)
            timing_status = _checkpoint_timing_status(request, ready_at)
            capture_id = _capture_id(
                request,
                sample,
                source_capture,
                ready_at,
                raw_hash,
                normalized_hash,
                raw_replay_status,
                evidence_origin=self._source.evidence_origin,
                source_id=self._source.source_id,
                adapter_version=self._source.adapter_version,
            )
            verdict = _verdict_for_scope(request.usage_scope, gaps)
            risk_disposition = (
                RiskDisposition.OBSERVATION_ONLY
                if verdict is QualificationVerdict.QUALIFIED
                else RiskDisposition.NO_NEW_RISK
            )
            artifact = QualificationCaptureArtifact(
                capture_id=capture_id,
                run_spec_hash=run_spec_hash,
                run_id=request.run_id,
                campaign_id=request.campaign_id,
                manifest_hash=request.manifest.manifest_hash,
                source_policy_id=request.source_policy_id,
                trade_date=request.trade_date,
                evidence_origin=self._source.evidence_origin,
                dataset=request.dataset,
                source_id=self._source.source_id,
                adapter_version=self._source.adapter_version,
                observer_version=request.observer_version,
                threshold_version=request.threshold_version,
                usage_scope=request.usage_scope,
                checkpoint=request.checkpoint,
                symbol=source_capture.symbol,
                venue=source_capture.venue,
                scheduled_at=request.scheduled_at,
                target_ready_by=request.target_ready_by,
                deadline_at=request.deadline_at,
                timing_status=timing_status,
                clock_sync_status=request.clock_sync_status,
                collector_clock_offset_ms=request.collector_clock_offset_ms,
                max_clock_offset_ms=request.max_clock_offset_ms,
                max_source_age_ms=request.max_source_age_ms,
                max_receive_age_ms=request.max_receive_age_ms,
                requested_at=source_capture.requested_at,
                received_at=source_capture.received_at,
                ready_at=ready_at,
                fetch_duration_ms=source_capture.fetch_duration_ms,
                normalization_duration_ms=normalization_duration_ms,
                source_event_at=source_capture.source_event_at,
                price=source_capture.price,
                trading_status=source_capture.trading_status,
                upper_limit_price=source_capture.upper_limit_price,
                lower_limit_price=source_capture.lower_limit_price,
                raw_payload_sha256=raw_hash,
                raw_payload_ref=f"raw/{capture_id}.bin",
                raw_payload_kind=source_capture.raw_payload_kind,
                raw_replay_status=raw_replay_status,
                normalized_payload_sha256=normalized_hash,
                verdict=verdict,
                risk_disposition=risk_disposition,
                data_gaps=gaps,
            )
            captures_with_raw.append((artifact, source_capture.raw_payload))
            all_gaps.extend(f"{sample.venue}:{sample.symbol}:{gap}" for gap in gaps)

        captures = tuple(artifact for artifact, _ in captures_with_raw)
        overall_verdict = _aggregate_verdict(captures)
        timing_status = _aggregate_timing_status(captures)
        ready_at = max(capture.ready_at for capture in captures)
        result = DataQualificationResult(
            run_id=request.run_id,
            run_spec_hash=run_spec_hash,
            report_hash="",
            campaign_id=request.campaign_id,
            manifest_hash=request.manifest.manifest_hash,
            source_policy_id=request.source_policy_id,
            source_id=self._source.source_id,
            adapter_version=self._source.adapter_version,
            observer_version=request.observer_version,
            threshold_version=request.threshold_version,
            trade_date=request.trade_date,
            evidence_origin=self._source.evidence_origin,
            dataset=request.dataset,
            usage_scope=request.usage_scope,
            checkpoint=request.checkpoint,
            target_ready_by=request.target_ready_by,
            deadline_at=request.deadline_at,
            ready_at=ready_at,
            timing_status=timing_status,
            verdict=overall_verdict,
            risk_disposition=(
                RiskDisposition.OBSERVATION_ONLY
                if overall_verdict is QualificationVerdict.QUALIFIED
                else RiskDisposition.NO_NEW_RISK
            ),
            captures=captures,
            data_gaps=tuple(all_gaps),
            observation_eligible=False,
            max_clock_offset_ms=request.max_clock_offset_ms,
            max_source_age_ms=request.max_source_age_ms,
            max_receive_age_ms=request.max_receive_age_ms,
        )
        result = replace(
            result,
            observation_eligible=_qualification_result_count_eligible(result),
        )
        result = replace(result, report_hash=_qualification_report_hash(result))
        self._store.persist(request.manifest, captures_with_raw, result)
        published_at = self._clock()
        _require_aware("published_at", published_at)
        publication_timing = _checkpoint_timing_status(request, published_at)
        wall_clock_order_valid = published_at >= result.ready_at
        publication_receipt = QualificationPublicationReceipt(
            run_id=result.run_id,
            run_spec_hash=result.run_spec_hash,
            report_hash=result.report_hash,
            published_at=published_at,
            timing_status=publication_timing,
            processing_duration_ms=(self._monotonic_ns() - observation_started_ns) // 1_000_000,
            wall_clock_order_valid=wall_clock_order_valid,
            qualification_count_eligible=(
                result.observation_eligible
                and publication_timing is CheckpointTimingStatus.TARGET_MET
                and wall_clock_order_valid
            ),
            receipt_hash="",
        )
        publication_receipt = replace(
            publication_receipt,
            receipt_hash=_publication_receipt_hash(publication_receipt),
        )
        self._store.persist_publication_receipt(result.run_id, publication_receipt)
        return replace(result, publication_receipt=publication_receipt)

    def _failure_capture(
        self,
        sample: QualificationSample,
        error: Exception,
    ) -> QualificationSourceCapture:
        observed_at = self._clock()
        raw_payload = _canonical_bytes(
            {
                "schema_version": "sanitized_capture_error.v1",
                "source_id": self._source.source_id,
                "adapter_version": self._source.adapter_version,
                "symbol": sample.symbol,
                "venue": sample.venue,
                "error_type": type(error).__name__,
            }
        )
        return QualificationSourceCapture(
            symbol=sample.symbol,
            venue=None,
            requested_at=observed_at,
            received_at=observed_at,
            fetch_duration_ms=0,
            source_event_at=None,
            price=None,
            trading_status=TradingStatus.UNKNOWN,
            upper_limit_price=None,
            lower_limit_price=None,
            raw_payload=raw_payload,
            raw_payload_kind="sanitized_capture_error",
            data_gaps=("source_capture_failed",),
        )


class _QualificationArtifactStore:
    """Append-only run evidence under a caller-supplied output root."""

    def __init__(self, output_root: Path) -> None:
        self._output_root = output_root

    def persist(
        self,
        manifest: SampleManifest,
        captures_with_raw: list[tuple[QualificationCaptureArtifact, bytes]],
        result: DataQualificationResult,
    ) -> None:
        run_root = self._output_root / result.run_id
        self.persist_manifest(result.run_id, manifest)
        report_path = run_root / "latest_report.json"
        report_bytes = _canonical_bytes(result.report_dict()) + b"\n"
        _assert_immutable_bytes(report_path, report_bytes)
        captures_path = run_root / "captures.jsonl"
        existing = {row.get("capture_id"): row for row in read_jsonl(captures_path)}
        for capture, raw_payload in captures_with_raw:
            raw_path = run_root / capture.raw_payload_ref
            _write_immutable_bytes(raw_path, raw_payload)
            payload = capture.to_dict()
            previous = existing.get(capture.capture_id)
            if previous is not None:
                if previous != payload:
                    raise QualificationArtifactConflictError(
                        f"capture_id reused with different content: {capture.capture_id}"
                    )
                continue
            _append_owner_jsonl(captures_path, payload)
            existing[capture.capture_id] = payload
        _write_immutable_bytes(report_path, report_bytes)

    def persist_run_spec(self, run_id: str, payload: Mapping[str, object]) -> None:
        """Freeze the complete observation envelope before any source call."""
        _write_immutable_bytes(
            self._output_root / run_id / "run_spec.json",
            _canonical_bytes(payload) + b"\n",
        )

    def persist_publication_receipt(
        self,
        run_id: str,
        receipt: QualificationPublicationReceipt,
    ) -> None:
        _write_immutable_bytes(
            self._output_root / run_id / "publication_receipt.json",
            _canonical_bytes(receipt.to_dict()) + b"\n",
        )

    def persist_manifest(self, run_id: str, manifest: SampleManifest) -> None:
        self._persist_manifest(
            self._output_root / run_id / "manifest.json",
            manifest.to_dict(),
        )

    @staticmethod
    def _persist_manifest(path: Path, payload: dict[str, object]) -> None:
        _write_immutable_bytes(path, _canonical_bytes(payload) + b"\n")


def _evaluate_capture(
    request: DataQualificationRequest,
    sample: QualificationSample,
    capture: QualificationSourceCapture,
    ready_at: datetime,
) -> tuple[str, ...]:
    gaps = list(capture.data_gaps)
    accepted_offset_ms = accepted_clock_offset_ms(
        clock_sync_status=request.clock_sync_status,
        collector_clock_offset_ms=request.collector_clock_offset_ms,
        max_clock_offset_ms=request.max_clock_offset_ms,
    )
    corrected_received_at = (
        clock_corrected_time(capture.received_at, accepted_offset_ms)
        if accepted_offset_ms is not None
        else None
    )
    corrected_ready_at = (
        clock_corrected_time(ready_at, accepted_offset_ms)
        if accepted_offset_ms is not None
        else None
    )
    parsed_price: Decimal | None = None
    if capture.symbol != sample.symbol:
        gaps.append("symbol_mismatch")
    if capture.venue is None:
        gaps.append("venue_unverified")
    elif capture.venue != sample.venue:
        gaps.append("venue_mismatch")
    if capture.price is None:
        gaps.append("price_missing")
    else:
        parsed_price = _positive_decimal(capture.price)
        if parsed_price is None:
            try:
                price = Decimal(capture.price)
            except InvalidOperation:
                gaps.append("price_invalid")
            else:
                if not price.is_finite():
                    gaps.append("price_invalid")
                else:
                    gaps.append("price_non_positive")
    if not capture.raw_payload:
        gaps.append("raw_payload_missing")
    if capture.received_at < capture.requested_at:
        gaps.append("received_before_request")
    if ready_at < capture.received_at:
        gaps.append("ready_before_receive")
    if corrected_received_at is not None and corrected_received_at < request.scheduled_at:
        gaps.append("received_before_checkpoint")
    timing_status = _checkpoint_timing_status(request, ready_at)
    if timing_status is CheckpointTimingStatus.HARD_DEADLINE_MISSED:
        gaps.append("checkpoint_hard_deadline_missed")
    elif timing_status is CheckpointTimingStatus.TARGET_MISSED:
        gaps.append("checkpoint_target_missed")
    if request.clock_sync_status != "synchronized":
        gaps.append("collector_clock_unsynchronized")
    if request.collector_clock_offset_ms is None:
        gaps.append("collector_clock_offset_unknown")
    elif isinstance(request.collector_clock_offset_ms, bool) or not isinstance(
        request.collector_clock_offset_ms, int
    ):
        gaps.append("collector_clock_offset_invalid")
    elif abs(request.collector_clock_offset_ms) > request.max_clock_offset_ms:
        gaps.append("collector_clock_offset_exceeded")

    if request.usage_scope in {
        QualificationUsageScope.PAPER_TACTICAL_DECISION,
        QualificationUsageScope.LIVE_TACTICAL_DECISION,
        QualificationUsageScope.LIVE_EXECUTION_REFERENCE,
    }:
        receive_age_ms = (ready_at - capture.received_at).total_seconds() * 1000
        if receive_age_ms > request.max_receive_age_ms:
            gaps.append("receive_age_at_ready_exceeded")
        if capture.source_event_at is None:
            gaps.append("source_event_at_missing")
        else:
            if corrected_ready_at is not None and corrected_received_at is not None:
                age_ms = (corrected_ready_at - capture.source_event_at).total_seconds() * 1000
                if age_ms < 0:
                    gaps.append("source_event_at_after_ready")
                elif age_ms > request.max_source_age_ms:
                    gaps.append("source_age_at_ready_exceeded")
                if capture.source_event_at > corrected_received_at:
                    gaps.append("source_event_at_after_receive")
        if capture.trading_status is TradingStatus.UNKNOWN:
            gaps.append("trading_status_unknown")
        if capture.upper_limit_price is None or capture.lower_limit_price is None:
            # 主指数无涨跌停（腾讯行以 -1 哨兵表缺）：语义性缺失不报 gap
            # （snapshot-index-support §2.3）；个股缺失语义不变。
            if f"{capture.symbol}.{(capture.venue or sample.venue).upper()}" not in MAJOR_INDEX_SYMBOLS:
                gaps.append("price_limits_missing")
        else:
            upper_limit = _positive_decimal(capture.upper_limit_price)
            lower_limit = _positive_decimal(capture.lower_limit_price)
            if upper_limit is None or lower_limit is None or lower_limit >= upper_limit:
                gaps.append("price_limits_invalid")
            elif parsed_price is not None and not (lower_limit <= parsed_price <= upper_limit):
                gaps.append("price_outside_limits")
    return tuple(dict.fromkeys(gaps))


def _verdict_for_scope(
    scope: QualificationUsageScope,
    gaps: tuple[str, ...],
) -> QualificationVerdict:
    if not gaps:
        return QualificationVerdict.QUALIFIED
    degradable = {
        "venue_unverified",
        "source_event_at_missing",
        "trading_status_unknown",
        "price_limits_missing",
        "upstream_raw_payload_unavailable",
        "checkpoint_target_missed",
    }
    if scope in {
        QualificationUsageScope.RESEARCH_ONLY,
        QualificationUsageScope.PAPER_TACTICAL_DECISION,
    } and set(gaps).issubset(degradable):
        return QualificationVerdict.QUALIFIED_WITH_DEGRADATION
    return QualificationVerdict.NOT_QUALIFIED


def _aggregate_verdict(
    captures: tuple[QualificationCaptureArtifact, ...],
) -> QualificationVerdict:
    verdicts = {capture.verdict for capture in captures}
    if verdicts == {QualificationVerdict.QUALIFIED}:
        return QualificationVerdict.QUALIFIED
    if QualificationVerdict.NOT_QUALIFIED in verdicts:
        return QualificationVerdict.NOT_QUALIFIED
    if QualificationVerdict.INCONCLUSIVE in verdicts:
        return QualificationVerdict.INCONCLUSIVE
    return QualificationVerdict.QUALIFIED_WITH_DEGRADATION


def _qualification_result_count_eligible(result: DataQualificationResult) -> bool:
    return qualification_report_count_policy_eligible(result.report_dict())


def qualification_report_count_policy_eligible(report: Mapping[str, object]) -> bool:
    """Recompute whether an immutable report may count toward data reliability.

    This is not a capital permission.  The only degraded case is the FIN-fixed
    Tencent PAPER reference identity, and only for status/limit facts that the
    primary source and Day 0 own.
    """
    captures = report.get("captures")
    if (
        report.get("schema_version") != QUALIFICATION_REPORT_SCHEMA_VERSION
        or not isinstance(captures, list)
        or not captures
        or any(
            not isinstance(capture, Mapping)
            or bool(qualification_capture_v2_projection_gaps(capture))
            for capture in captures
        )
        or report.get("evidence_origin") != ObservationEvidenceOrigin.LIVE_CAPTURE.value
        or report.get("timing_status") != CheckpointTimingStatus.TARGET_MET.value
    ):
        return False
    paper_policy: PaperQualificationSubjectPolicy | None = None
    if report.get("usage_scope") == QualificationUsageScope.PAPER_TACTICAL_DECISION.value:
        paper_policy = _matching_paper_subject_policy(report)
        if paper_policy is None:
            return False
    if report.get("verdict") == QualificationVerdict.QUALIFIED.value:
        return True
    policy = paper_policy
    if (
        policy is not A_SHARE_PAPER_REFERENCE_POLICY
        or report.get("verdict") != QualificationVerdict.QUALIFIED_WITH_DEGRADATION.value
        or report.get("risk_disposition") != RiskDisposition.NO_NEW_RISK.value
        or report.get("execution_allowed") is not False
        or report.get("affects_provider_routing") is not False
        or report.get("writes_production_cache") is not False
        or not isinstance(captures, list)
        or not captures
    ):
        return False
    saw_allowed_gap = False
    for capture in captures:
        if not isinstance(capture, Mapping):
            return False
        raw_gaps = capture.get("data_gaps")
        if not isinstance(raw_gaps, list) or any(not isinstance(gap, str) for gap in raw_gaps):
            return False
        gaps = set(raw_gaps)
        if not gaps:
            if (
                capture.get("verdict") != QualificationVerdict.QUALIFIED.value
                or capture.get("risk_disposition") != RiskDisposition.OBSERVATION_ONLY.value
                or capture.get("timing_status") != CheckpointTimingStatus.TARGET_MET.value
                or capture.get("raw_replay_status") != RawReplayStatus.VERIFIED.value
            ):
                return False
            continue
        if (
            not gaps.issubset(policy.allowed_reliability_gaps)
            or capture.get("verdict") != QualificationVerdict.QUALIFIED_WITH_DEGRADATION.value
            or capture.get("risk_disposition") != RiskDisposition.NO_NEW_RISK.value
            or capture.get("timing_status") != CheckpointTimingStatus.TARGET_MET.value
            or capture.get("raw_replay_status") != RawReplayStatus.VERIFIED.value
        ):
            return False
        if (capture.get("trading_status") == TradingStatus.UNKNOWN.value) != (
            "trading_status_unknown" in gaps
        ):
            return False
        if (
            capture.get("upper_limit_price") is None or capture.get("lower_limit_price") is None
        ) != ("price_limits_missing" in gaps):
            return False
        saw_allowed_gap = True
    return saw_allowed_gap


def qualification_capture_v2_projection_gaps(
    capture: Mapping[str, object],
) -> tuple[str, ...]:
    """Recompute every cross-clock field in one persisted v2 capture.

    Historical qualification consumes immutable JSON rather than live Python
    objects.  This verifier prevents a self-consistent legacy or edited artifact
    from acquiring v2 semantics merely by recomputing its surrounding hashes.
    """

    if capture.get("schema_version") != QUALIFICATION_CAPTURE_SCHEMA_VERSION:
        return ("schema_unsupported",)
    derived_fields = {
        "clock_correction_applied",
        "accepted_clock_offset_ms",
        "clock_corrected_requested_at",
        "clock_corrected_received_at",
        "clock_corrected_ready_at",
        "scheduler_lag_ms",
        "normalization_lag_ms",
        "ready_lag_ms",
        "source_age_at_receive_ms",
        "source_age_at_ready_ms",
        "timing_status",
    }
    if not derived_fields.issubset(capture):
        return ("clock_projection_invalid",)
    try:
        scheduled_at = _aware_payload_datetime(capture, "scheduled_at")
        target_ready_by = _aware_payload_datetime(capture, "target_ready_by")
        deadline_at = _aware_payload_datetime(capture, "deadline_at")
        requested_at = _aware_payload_datetime(capture, "requested_at")
        received_at = _aware_payload_datetime(capture, "received_at")
        ready_at = _aware_payload_datetime(capture, "ready_at")
        source_event_at = _optional_aware_payload_datetime(capture, "source_event_at")
        clock_sync_status = capture["clock_sync_status"]
        collector_clock_offset_ms = capture["collector_clock_offset_ms"]
        max_clock_offset_ms = capture["max_clock_offset_ms"]
        if not isinstance(clock_sync_status, str):
            raise ValueError("clock_sync_status invalid")
        if collector_clock_offset_ms is not None and (
            isinstance(collector_clock_offset_ms, bool)
            or not isinstance(collector_clock_offset_ms, int)
        ):
            raise ValueError("collector_clock_offset_ms invalid")
        if (
            isinstance(max_clock_offset_ms, bool)
            or not isinstance(max_clock_offset_ms, int)
            or max_clock_offset_ms < 0
        ):
            raise ValueError("max_clock_offset_ms invalid")
    except (KeyError, TypeError, ValueError):
        return ("clock_projection_invalid",)

    accepted_offset_ms = accepted_clock_offset_ms(
        clock_sync_status=clock_sync_status,
        collector_clock_offset_ms=collector_clock_offset_ms,
        max_clock_offset_ms=max_clock_offset_ms,
    )
    corrected_requested_at = (
        clock_corrected_time(requested_at, accepted_offset_ms)
        if accepted_offset_ms is not None
        else None
    )
    corrected_received_at = (
        clock_corrected_time(received_at, accepted_offset_ms)
        if accepted_offset_ms is not None
        else None
    )
    corrected_ready_at = (
        clock_corrected_time(ready_at, accepted_offset_ms)
        if accepted_offset_ms is not None
        else None
    )
    if corrected_ready_at is None or corrected_ready_at > deadline_at:
        timing_status = CheckpointTimingStatus.HARD_DEADLINE_MISSED.value
    elif corrected_ready_at > target_ready_by:
        timing_status = CheckpointTimingStatus.TARGET_MISSED.value
    else:
        timing_status = CheckpointTimingStatus.TARGET_MET.value
    expected = {
        "clock_correction_applied": accepted_offset_ms is not None,
        "accepted_clock_offset_ms": accepted_offset_ms,
        "clock_corrected_requested_at": (
            corrected_requested_at.isoformat() if corrected_requested_at is not None else None
        ),
        "clock_corrected_received_at": (
            corrected_received_at.isoformat() if corrected_received_at is not None else None
        ),
        "clock_corrected_ready_at": (
            corrected_ready_at.isoformat() if corrected_ready_at is not None else None
        ),
        "scheduler_lag_ms": (
            _duration_ms(scheduled_at, corrected_requested_at)
            if corrected_requested_at is not None
            else None
        ),
        "normalization_lag_ms": _duration_ms(received_at, ready_at),
        "ready_lag_ms": (
            _duration_ms(scheduled_at, corrected_ready_at)
            if corrected_ready_at is not None
            else None
        ),
        "source_age_at_receive_ms": (
            _duration_ms(source_event_at, corrected_received_at)
            if source_event_at is not None and corrected_received_at is not None
            else None
        ),
        "source_age_at_ready_ms": (
            _duration_ms(source_event_at, corrected_ready_at)
            if source_event_at is not None and corrected_ready_at is not None
            else None
        ),
        "timing_status": timing_status,
    }
    if any(capture.get(field_name) != value for field_name, value in expected.items()):
        return ("clock_projection_invalid",)
    return ()


def _matching_paper_subject_policy(
    report: Mapping[str, object],
) -> PaperQualificationSubjectPolicy | None:
    for policy in (A_SHARE_PAPER_PRIMARY_POLICY, A_SHARE_PAPER_REFERENCE_POLICY):
        if (
            report.get("source_policy_id"),
            report.get("source_id"),
            report.get("adapter_version"),
            report.get("observer_version"),
            report.get("threshold_version"),
            report.get("max_clock_offset_ms"),
            report.get("max_source_age_ms"),
            report.get("max_receive_age_ms"),
            report.get("dataset"),
        ) == (
            policy.source_policy_id,
            policy.source_id,
            policy.adapter_version,
            policy.observer_version,
            policy.threshold_version,
            policy.max_clock_offset_ms,
            policy.max_source_age_ms,
            policy.max_receive_age_ms,
            QualificationDataset.REALTIME_QUOTE.value,
        ):
            return policy
    return None


def _capture_id(
    request: DataQualificationRequest,
    sample: QualificationSample,
    capture: QualificationSourceCapture,
    ready_at: datetime,
    raw_hash: str,
    normalized_hash: str,
    raw_replay_status: RawReplayStatus,
    *,
    source_id: str,
    adapter_version: str,
    evidence_origin: ObservationEvidenceOrigin,
) -> str:
    identity = {
        "capture_schema_version": QUALIFICATION_CAPTURE_SCHEMA_VERSION,
        "run_id": request.run_id,
        "campaign_id": request.campaign_id,
        "manifest_hash": request.manifest.manifest_hash,
        "source_policy_id": request.source_policy_id,
        "trade_date": request.trade_date.isoformat(),
        "evidence_origin": evidence_origin.value,
        "dataset": request.dataset.value,
        "usage_scope": request.usage_scope.value,
        "checkpoint": request.checkpoint.value,
        "scheduled_at": request.scheduled_at.isoformat(),
        "target_ready_by": request.target_ready_by.isoformat(),
        "deadline_at": request.deadline_at.isoformat(),
        "clock_sync_status": request.clock_sync_status,
        "collector_clock_offset_ms": request.collector_clock_offset_ms,
        "max_clock_offset_ms": request.max_clock_offset_ms,
        "max_source_age_ms": request.max_source_age_ms,
        "max_receive_age_ms": request.max_receive_age_ms,
        "source_id": source_id,
        "adapter_version": adapter_version,
        "observer_version": request.observer_version,
        "threshold_version": request.threshold_version,
        "sample": sample.to_dict(),
        "requested_at": capture.requested_at.isoformat(),
        "received_at": capture.received_at.isoformat(),
        "ready_at": ready_at.isoformat(),
        "fetch_duration_ms": capture.fetch_duration_ms,
        "raw_payload_sha256": raw_hash,
        "normalized_payload_sha256": normalized_hash,
        "raw_replay_status": raw_replay_status.value,
    }
    return _sha256(_canonical_bytes(identity))


def _verify_raw_replay(
    source: QualificationSourcePort,
    sample: QualificationSample,
    capture: QualificationSourceCapture,
) -> tuple[RawReplayStatus, str, tuple[str, ...]]:
    normalized = QualificationNormalizedRecord.from_capture(capture)
    normalized_bytes = _canonical_bytes(normalized.to_dict())
    normalized_hash = _sha256(normalized_bytes)
    replay = getattr(source, "replay_normalize", None)
    if replay is None:
        return RawReplayStatus.UNAVAILABLE, normalized_hash, ("raw_replay_unavailable",)
    try:
        replayed = replay(sample, capture.raw_payload)
    except Exception:
        return RawReplayStatus.FAILED, normalized_hash, ("raw_replay_failed",)
    if replayed.to_dict() != normalized.to_dict():
        return RawReplayStatus.MISMATCH, normalized_hash, ("raw_replay_mismatch",)
    return RawReplayStatus.VERIFIED, normalized_hash, ()


def _sanitize_sensitive_raw_capture(
    capture: QualificationSourceCapture,
    *,
    source_id: str,
    adapter_version: str,
) -> QualificationSourceCapture:
    detected = _sensitive_raw_fields(capture.raw_payload)
    if not detected:
        return capture
    sanitized = _canonical_bytes(
        {
            "schema_version": "sanitized_sensitive_payload.v1",
            "source_id": source_id,
            "adapter_version": adapter_version,
            "symbol": capture.symbol,
            "venue": capture.venue,
            "detected_fields": list(detected),
        }
    )
    return replace(
        capture,
        raw_payload=sanitized,
        raw_payload_kind="sanitized_sensitive_payload",
        data_gaps=tuple(dict.fromkeys((*capture.data_gaps, "raw_payload_sensitive_material"))),
    )


def _sensitive_raw_fields(raw_payload: bytes) -> tuple[str, ...]:
    try:
        payload = json.loads(raw_payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        lowered = raw_payload.lower()
        return tuple(sorted(field for field in _SENSITIVE_RAW_FIELDS if field.encode() in lowered))

    found: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in _SENSITIVE_RAW_FIELDS:
                    found.add(normalized)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return tuple(sorted(found))


def _write_immutable_bytes(path: Path, payload: bytes) -> None:
    _ensure_owner_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _validate_owner_file(path)
        if path.read_bytes() != payload:
            raise QualificationArtifactConflictError(
                f"immutable artifact conflict: {path.name}"
            ) from None
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise


def _assert_immutable_bytes(path: Path, payload: bytes) -> None:
    if path.exists():
        _validate_owner_file(path)
    if path.exists() and path.read_bytes() != payload:
        raise QualificationArtifactConflictError(f"immutable artifact conflict: {path.name}")


def _ensure_owner_directory(path: Path) -> None:
    """Create one artifact directory with explicit mode, never repair an old one."""

    missing: list[Path] = []
    cursor = path
    while True:
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise QualificationArtifactConflictError(
                    "artifact directory has no valid parent"
                ) from None
            cursor = parent
            continue
        except OSError as error:
            raise QualificationArtifactConflictError(
                f"artifact directory unavailable: {path.name}"
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise QualificationArtifactConflictError(
                f"artifact directory is not owner-only: {cursor.name}"
            )
        break
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        except FileExistsError:
            _ensure_owner_directory(directory)


def _validate_owner_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise QualificationArtifactConflictError(
            f"artifact file unavailable: {path.name}"
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or path.resolve(strict=True) != path
    ):
        raise QualificationArtifactConflictError(f"artifact file is not owner-only: {path.name}")


def _append_owner_jsonl(path: Path, record: dict[str, object]) -> None:
    """Append one immutable capture row using an explicit 0600 file mode."""

    _ensure_owner_directory(path.parent)
    if path.exists():
        _validate_owner_file(path)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise QualificationArtifactConflictError(f"capture log unavailable: {path.name}") from error
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        raise
    _validate_owner_file(path)


def _checkpoint_timing_status(
    request: DataQualificationRequest,
    received_at: datetime,
) -> CheckpointTimingStatus:
    accepted_offset_ms = accepted_clock_offset_ms(
        clock_sync_status=request.clock_sync_status,
        collector_clock_offset_ms=request.collector_clock_offset_ms,
        max_clock_offset_ms=request.max_clock_offset_ms,
    )
    if accepted_offset_ms is None:
        return CheckpointTimingStatus.HARD_DEADLINE_MISSED
    corrected_received_at = clock_corrected_time(received_at, accepted_offset_ms)
    if corrected_received_at > request.deadline_at:
        return CheckpointTimingStatus.HARD_DEADLINE_MISSED
    if corrected_received_at > request.target_ready_by:
        return CheckpointTimingStatus.TARGET_MISSED
    return CheckpointTimingStatus.TARGET_MET


def _aggregate_timing_status(
    captures: tuple[QualificationCaptureArtifact, ...],
) -> CheckpointTimingStatus:
    statuses = {capture.timing_status for capture in captures}
    if CheckpointTimingStatus.HARD_DEADLINE_MISSED in statuses:
        return CheckpointTimingStatus.HARD_DEADLINE_MISSED
    if CheckpointTimingStatus.TARGET_MISSED in statuses:
        return CheckpointTimingStatus.TARGET_MISSED
    return CheckpointTimingStatus.TARGET_MET


def _validate_identifier(field_name: str, value: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} contains unsafe characters")


def _require_aware(field_name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _qualification_report_hash(result: DataQualificationResult) -> str:
    payload = result.report_dict()
    payload.pop("report_hash", None)
    return _sha256(_canonical_bytes(payload))


def _publication_receipt_hash(receipt: QualificationPublicationReceipt) -> str:
    return _sha256(_canonical_bytes(receipt.unsigned_dict()))


def _duration_ms(start: datetime, end: datetime) -> int:
    delta = end - start
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _aware_payload_datetime(payload: Mapping[str, object], field_name: str) -> datetime:
    raw = payload[field_name]
    if not isinstance(raw, str):
        raise ValueError(f"{field_name} must be an ISO timestamp")
    value = datetime.fromisoformat(raw)
    _require_aware(field_name, value)
    return value


def _optional_aware_payload_datetime(
    payload: Mapping[str, object],
    field_name: str,
) -> datetime | None:
    raw = payload[field_name]
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{field_name} must be an ISO timestamp or null")
    value = datetime.fromisoformat(raw)
    _require_aware(field_name, value)
    return value


def _decimal_text(value: float | None) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)))


def _positive_decimal(value: str) -> Decimal | None:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _utc_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


__all__ = [
    "A_SHARE_PAPER_PRIMARY_POLICY",
    "A_SHARE_PAPER_REFERENCE_POLICY",
    "CheckpointTimingStatus",
    "DataQualificationRequest",
    "DataQualificationResult",
    "DataQualificationService",
    "LegacyQuoteQualificationAdapter",
    "LegacyQuoteProvider",
    "ObservationEvidenceOrigin",
    "PaperQualificationSubjectPolicy",
    "QualificationArtifactConflictError",
    "QualificationCheckpoint",
    "QUALIFICATION_CAPTURE_SCHEMA_VERSION",
    "QUALIFICATION_PUBLICATION_RECEIPT_SCHEMA_VERSION",
    "QUALIFICATION_REPORT_SCHEMA_VERSION",
    "QualificationDataset",
    "QualificationNormalizedRecord",
    "QualificationPublicationReceipt",
    "QualificationSample",
    "QualificationSourceCapture",
    "QualificationSourcePort",
    "QualificationUsageScope",
    "QualificationVerdict",
    "qualification_report_count_policy_eligible",
    "RiskDisposition",
    "RawReplayStatus",
    "SampleManifest",
    "TradingStatus",
]
