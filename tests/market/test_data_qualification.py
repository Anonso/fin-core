"""Behavior tests for the isolated market-data qualification observer."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from fin_analyse.market.data_qualification import (
    CheckpointTimingStatus,
    DataQualificationRequest,
    DataQualificationService,
    LegacyQuoteQualificationAdapter,
    ObservationEvidenceOrigin,
    QualificationArtifactConflictError,
    QualificationCheckpoint,
    QualificationDataset,
    QualificationNormalizedRecord,
    QualificationSample,
    QualificationSourceCapture,
    QualificationUsageScope,
    QualificationVerdict,
    RawReplayStatus,
    RiskDisposition,
    SampleManifest,
    TradingStatus,
)

SCHEDULED_AT = datetime(2026, 7, 17, 6, 39, tzinfo=UTC)
DEADLINE_AT = datetime(2026, 7, 17, 6, 39, 30, tzinfo=UTC)
SAMPLE = QualificationSample(symbol="600519", venue="sh")


class _StaticSource:
    source_id = "qualification_reference"
    adapter_version = "reference-v1"
    evidence_origin = ObservationEvidenceOrigin.TEST_ONLY

    def __init__(self, capture: QualificationSourceCapture) -> None:
        self._capture = capture
        self.calls = 0

    def capture(self, sample: QualificationSample) -> QualificationSourceCapture:
        assert sample == SAMPLE
        self.calls += 1
        return self._capture

    def replay_normalize(
        self,
        sample: QualificationSample,
        raw_payload: bytes,
    ) -> QualificationNormalizedRecord:
        payload = json.loads(raw_payload)
        source_event_at = payload.get("source_event_at")
        price = payload.get("price")
        upper_limit_price = payload.get("upper_limit_price")
        lower_limit_price = payload.get("lower_limit_price")
        return QualificationNormalizedRecord(
            symbol=str(payload["symbol"]),
            venue=(str(payload["venue"]) if payload.get("venue") is not None else None),
            source_event_at=(
                datetime.fromisoformat(str(source_event_at))
                if source_event_at is not None
                else None
            ),
            price=str(price) if price is not None else None,
            trading_status=TradingStatus(str(payload["trading_status"])),
            upper_limit_price=(str(upper_limit_price) if upper_limit_price is not None else None),
            lower_limit_price=(str(lower_limit_price) if lower_limit_price is not None else None),
        )


class _TencentPaperReferenceSource(_StaticSource):
    source_id = "tencent_raw"
    adapter_version = "tencent_raw_qualification.v1"
    evidence_origin = ObservationEvidenceOrigin.LIVE_CAPTURE


class _EastmoneyPaperPrimarySource(_StaticSource):
    source_id = "eastmoney_raw"
    adapter_version = "eastmoney_raw_qualification.v1"
    evidence_origin = ObservationEvidenceOrigin.LIVE_CAPTURE


def _complete_capture(**changes) -> QualificationSourceCapture:
    capture = QualificationSourceCapture(
        symbol=SAMPLE.symbol,
        venue=SAMPLE.venue,
        requested_at=SCHEDULED_AT,
        received_at=datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
        fetch_duration_ms=1000,
        source_event_at=datetime(2026, 7, 17, 6, 39, 0, 500000, tzinfo=UTC),
        price="1500.00",
        trading_status=TradingStatus.TRADING,
        upper_limit_price="1650.00",
        lower_limit_price="1350.00",
        raw_payload=(
            b'{"symbol":"600519","venue":"sh",'
            b'"source_event_at":"2026-07-17T06:39:00.500000+00:00",'
            b'"price":"1500.00","trading_status":"trading",'
            b'"upper_limit_price":"1650.00","lower_limit_price":"1350.00"}'
        ),
        raw_payload_kind="source_response",
    )
    return replace(capture, **changes)


def _capture_with_replay(**changes) -> QualificationSourceCapture:
    capture = replace(_complete_capture(), **changes)
    payload = {
        "symbol": capture.symbol,
        "venue": capture.venue,
        "source_event_at": (
            capture.source_event_at.isoformat() if capture.source_event_at is not None else None
        ),
        "price": capture.price,
        "trading_status": capture.trading_status.value,
        "upper_limit_price": capture.upper_limit_price,
        "lower_limit_price": capture.lower_limit_price,
    }
    return replace(capture, raw_payload=json.dumps(payload, sort_keys=True).encode())


def _manifest(suffix: str) -> SampleManifest:
    return SampleManifest.build(
        manifest_id=f"manifest-{suffix}",
        created_at=datetime(2026, 7, 17, 6, 30, tzinfo=UTC),
        selection_cutoff_at=datetime(2026, 7, 17, 6, 35, tzinfo=UTC),
        samples=(SAMPLE,),
    )


def _request(suffix: str, manifest: SampleManifest, **changes) -> DataQualificationRequest:
    request = DataQualificationRequest(
        run_id=f"run-{suffix}",
        campaign_id="paper-five-day-20260720",
        source_policy_id="reference-primary-v1",
        trade_date=date(2026, 7, 17),
        dataset=QualificationDataset.REALTIME_QUOTE,
        usage_scope=QualificationUsageScope.LIVE_EXECUTION_REFERENCE,
        checkpoint=QualificationCheckpoint.EXECUTION_REFRESH_1439,
        scheduled_at=SCHEDULED_AT,
        target_ready_by=DEADLINE_AT,
        deadline_at=DEADLINE_AT,
        manifest=manifest,
        clock_sync_status="synchronized",
        collector_clock_offset_ms=25,
    )
    return replace(request, **changes)


def _observe(tmp_path, *, suffix: str, capture=None, request_changes=None):
    source_capture = capture or _complete_capture()
    source = _StaticSource(source_capture)
    manifest = _manifest(suffix)
    request = _request(suffix, manifest, **(request_changes or {}))
    result = DataQualificationService(
        source=source,
        output_root=tmp_path,
        clock=lambda: source_capture.received_at + timedelta(milliseconds=100),
    ).observe(request)
    return result, source


def test_complete_capture_writes_scoped_qualification_artifacts(tmp_path):
    result, source = _observe(tmp_path, suffix="complete")

    assert source.calls == 1
    assert result.verdict is QualificationVerdict.QUALIFIED
    assert result.execution_allowed is False
    assert result.affects_provider_routing is False
    assert result.data_gaps == ()

    run_root = tmp_path / "run-complete"
    manifest_payload = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    capture_payload = json.loads((run_root / "captures.jsonl").read_text(encoding="utf-8").strip())
    report_payload = json.loads((run_root / "latest_report.json").read_text(encoding="utf-8"))

    assert manifest_payload["manifest_hash"] == result.manifest_hash
    assert capture_payload["scheduled_at"] == "2026-07-17T06:39:00+00:00"
    assert capture_payload["target_ready_by"] == "2026-07-17T06:39:30+00:00"
    assert capture_payload["deadline_at"] == "2026-07-17T06:39:30+00:00"
    assert capture_payload["timing_status"] == CheckpointTimingStatus.TARGET_MET.value
    assert capture_payload["clock_sync_status"] == "synchronized"
    assert capture_payload["collector_clock_offset_ms"] == 25
    assert capture_payload["dataset"] == "realtime_quote"
    assert capture_payload["campaign_id"] == "paper-five-day-20260720"
    assert capture_payload["trade_date"] == "2026-07-17"
    assert capture_payload["evidence_origin"] == "test_only"
    assert capture_payload["max_clock_offset_ms"] == 2_000
    assert capture_payload["max_source_age_ms"] == 3000
    assert capture_payload["fetch_duration_ms"] == 1000
    assert capture_payload["schema_version"] == "data_capture.v2"
    assert capture_payload["clock_corrected_received_at"] == ("2026-07-17T06:39:01.025000+00:00")
    assert capture_payload["source_age_at_receive_ms"] == 525
    assert capture_payload["source_event_at"] == "2026-07-17T06:39:00.500000+00:00"
    assert capture_payload["raw_payload_sha256"]
    assert capture_payload["raw_replay_status"] == RawReplayStatus.VERIFIED.value
    assert capture_payload["normalized_payload_sha256"]
    raw_payload = (run_root / capture_payload["raw_payload_ref"]).read_bytes()
    assert json.loads(raw_payload)["trading_status"] == "trading"
    assert report_payload["verdict"] == "qualified"
    assert report_payload["dataset"] == "realtime_quote"
    assert report_payload["campaign_id"] == "paper-five-day-20260720"
    assert report_payload["source_id"] == "qualification_reference"
    assert report_payload["adapter_version"] == "reference-v1"
    assert report_payload["observation_eligible"] is False
    assert report_payload["trade_date"] == "2026-07-17"
    assert report_payload["evidence_origin"] == "test_only"
    assert report_payload["max_clock_offset_ms"] == 2_000
    assert report_payload["max_source_age_ms"] == 3000
    assert report_payload["execution_allowed"] is False
    json.dumps(result.to_dict())


def test_fixed_tencent_reference_unknown_status_counts_only_as_data_reliability(
    tmp_path,
):
    capture = _complete_capture(
        trading_status=TradingStatus.UNKNOWN,
        raw_payload=(
            b'{"symbol":"600519","venue":"sh",'
            b'"source_event_at":"2026-07-17T06:39:00.500000+00:00",'
            b'"price":"1500.00","trading_status":"unknown",'
            b'"upper_limit_price":"1650.00","lower_limit_price":"1350.00"}'
        ),
    )
    request = _request(
        "tencent-paper-reference",
        _manifest("tencent-paper-reference"),
        source_policy_id="tencent-paper-reference-v1",
        usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
    )

    result = DataQualificationService(
        source=_TencentPaperReferenceSource(capture),
        output_root=tmp_path,
        clock=lambda: capture.received_at + timedelta(milliseconds=100),
    ).observe(request)

    assert result.verdict is QualificationVerdict.QUALIFIED_WITH_DEGRADATION
    assert result.captures[0].trading_status is TradingStatus.UNKNOWN
    assert result.data_gaps == ("sh:600519:trading_status_unknown",)
    assert result.observation_eligible is True
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is True
    assert result.risk_disposition is RiskDisposition.NO_NEW_RISK
    assert result.execution_allowed is False


def test_fixed_tencent_reference_missing_limits_remain_unknown_but_can_count(
    tmp_path,
) -> None:
    capture = _capture_with_replay(
        trading_status=TradingStatus.UNKNOWN,
        upper_limit_price=None,
        lower_limit_price=None,
    )
    result = DataQualificationService(
        source=_TencentPaperReferenceSource(capture),
        output_root=tmp_path,
        clock=lambda: capture.received_at + timedelta(milliseconds=100),
    ).observe(
        _request(
            "tencent-paper-reference-missing-limits",
            _manifest("tencent-paper-reference-missing-limits"),
            source_policy_id="tencent-paper-reference-v1",
            usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
        )
    )

    assert result.captures[0].upper_limit_price is None
    assert result.captures[0].lower_limit_price is None
    assert result.verdict is QualificationVerdict.QUALIFIED_WITH_DEGRADATION
    assert set(result.data_gaps) == {
        "sh:600519:trading_status_unknown",
        "sh:600519:price_limits_missing",
    }
    assert result.observation_eligible is True
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is True
    assert result.execution_allowed is False


def test_fixed_reference_cannot_count_with_caller_relaxed_freshness_policy(tmp_path) -> None:
    capture = _capture_with_replay(
        trading_status=TradingStatus.UNKNOWN,
        source_event_at=SCHEDULED_AT - timedelta(hours=1),
    )
    result = DataQualificationService(
        source=_TencentPaperReferenceSource(capture),
        output_root=tmp_path,
        clock=lambda: capture.received_at + timedelta(milliseconds=100),
    ).observe(
        _request(
            "tencent-reference-relaxed-freshness",
            _manifest("tencent-reference-relaxed-freshness"),
            source_policy_id="tencent-paper-reference-v1",
            usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
            max_source_age_ms=86_400_000,
        )
    )

    assert result.verdict is QualificationVerdict.QUALIFIED_WITH_DEGRADATION
    assert result.observation_eligible is False
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is False


@pytest.mark.parametrize(
    "request_changes",
    [
        {"observer_version": "caller-observer.v99"},
        {"threshold_version": "caller-thresholds.v99"},
        {"max_clock_offset_ms": 86_400_000},
        {"max_receive_age_ms": 86_400_000},
    ],
)
def test_fixed_primary_cannot_count_with_caller_policy_contract_drift(
    tmp_path,
    request_changes: dict[str, object],
) -> None:
    capture = _complete_capture()
    result = DataQualificationService(
        source=_EastmoneyPaperPrimarySource(capture),
        output_root=tmp_path,
        clock=lambda: capture.received_at + timedelta(milliseconds=100),
    ).observe(
        _request(
            f"eastmoney-primary-drift-{next(iter(request_changes))}",
            _manifest(f"eastmoney-primary-drift-{next(iter(request_changes))}"),
            source_policy_id="eastmoney-paper-primary-v1",
            usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
            **request_changes,
        )
    )

    assert result.verdict is QualificationVerdict.QUALIFIED
    assert result.observation_eligible is False
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is False


@pytest.mark.parametrize(
    ("capture_changes", "request_changes", "expected_gap"),
    [
        (
            {"source_event_at": datetime(2026, 7, 17, 6, 38, 50, tzinfo=UTC)},
            {},
            "source_age_at_ready_exceeded",
        ),
        ({"price": None}, {}, "price_missing"),
        ({"symbol": "000001"}, {}, "symbol_mismatch"),
        ({"venue": "sz"}, {}, "venue_mismatch"),
        ({}, {"clock_sync_status": "unknown"}, "collector_clock_unsynchronized"),
        (
            {},
            {
                "target_ready_by": datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
                "deadline_at": DEADLINE_AT,
            },
            "checkpoint_target_missed",
        ),
    ],
)
def test_fixed_reference_non_role_gaps_never_count(
    tmp_path,
    capture_changes,
    request_changes,
    expected_gap,
) -> None:
    capture = _capture_with_replay(
        trading_status=TradingStatus.UNKNOWN,
        **capture_changes,
    )
    result = DataQualificationService(
        source=_TencentPaperReferenceSource(capture),
        output_root=tmp_path,
        clock=lambda: capture.received_at + timedelta(milliseconds=100),
    ).observe(
        _request(
            f"tencent-reference-{expected_gap}",
            _manifest(f"tencent-reference-{expected_gap}"),
            source_policy_id="tencent-paper-reference-v1",
            usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
            **request_changes,
        )
    )

    assert any(gap.endswith(f":{expected_gap}") for gap in result.data_gaps)
    assert result.observation_eligible is False
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is False
    assert result.execution_allowed is False


def test_reference_counting_policy_cannot_be_reused_by_another_source(tmp_path) -> None:
    capture = _capture_with_replay(trading_status=TradingStatus.UNKNOWN)
    source = _TencentPaperReferenceSource(capture)
    source.source_id = "caller_controlled_reference"
    result = DataQualificationService(
        source=source,
        output_root=tmp_path,
        clock=lambda: capture.received_at + timedelta(milliseconds=100),
    ).observe(
        _request(
            "caller-controlled-reference",
            _manifest("caller-controlled-reference"),
            source_policy_id="tencent-paper-reference-v1",
            usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
        )
    )

    assert result.verdict is QualificationVerdict.QUALIFIED_WITH_DEGRADATION
    assert result.observation_eligible is False
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is False


def test_fixed_reference_raw_replay_failure_never_counts(tmp_path) -> None:
    capture = _capture_with_replay(trading_status=TradingStatus.UNKNOWN)
    capture = replace(
        capture,
        raw_payload=capture.raw_payload.replace(b'"price": "1500.00"', b'"price": "1499.00"'),
    )
    result = DataQualificationService(
        source=_TencentPaperReferenceSource(capture),
        output_root=tmp_path,
        clock=lambda: capture.received_at + timedelta(milliseconds=100),
    ).observe(
        _request(
            "tencent-reference-replay-mismatch",
            _manifest("tencent-reference-replay-mismatch"),
            source_policy_id="tencent-paper-reference-v1",
            usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
        )
    )

    assert "sh:600519:raw_replay_mismatch" in result.data_gaps
    assert result.observation_eligible is False
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is False


def test_test_only_reference_provenance_never_counts(tmp_path) -> None:
    capture = _capture_with_replay(trading_status=TradingStatus.UNKNOWN)
    source = _TencentPaperReferenceSource(capture)
    source.evidence_origin = ObservationEvidenceOrigin.TEST_ONLY

    result = DataQualificationService(
        source=source,
        output_root=tmp_path,
        clock=lambda: capture.received_at + timedelta(milliseconds=100),
    ).observe(
        _request(
            "tencent-reference-test-only",
            _manifest("tencent-reference-test-only"),
            source_policy_id="tencent-paper-reference-v1",
            usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
        )
    )

    assert result.verdict is QualificationVerdict.QUALIFIED_WITH_DEGRADATION
    assert result.evidence_origin is ObservationEvidenceOrigin.TEST_ONLY
    assert result.observation_eligible is False
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is False


def test_primary_unknown_status_remains_strictly_not_countable(tmp_path) -> None:
    capture = _capture_with_replay(trading_status=TradingStatus.UNKNOWN)
    result = DataQualificationService(
        source=_EastmoneyPaperPrimarySource(capture),
        output_root=tmp_path,
        clock=lambda: capture.received_at + timedelta(milliseconds=100),
    ).observe(
        _request(
            "eastmoney-primary-unknown-status",
            _manifest("eastmoney-primary-unknown-status"),
            source_policy_id="eastmoney-paper-primary-v1",
            usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
        )
    )

    assert result.verdict is QualificationVerdict.QUALIFIED_WITH_DEGRADATION
    assert result.observation_eligible is False
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is False
    assert result.risk_disposition is RiskDisposition.NO_NEW_RISK
    assert result.execution_allowed is False


def test_fetch_duration_uses_monotonic_evidence_when_wall_clock_rolls_back(tmp_path):
    result, _ = _observe(
        tmp_path,
        suffix="wall-clock-rollback",
        capture=_complete_capture(
            requested_at=datetime(2026, 7, 17, 6, 39, 2, tzinfo=UTC),
            received_at=datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
            fetch_duration_ms=125,
        ),
    )

    payload = result.captures[0].to_dict()
    assert payload["fetch_duration_ms"] == 125
    assert "sh:600519:received_before_request" in result.data_gaps


def test_zero_price_is_recorded_but_never_qualified_or_replaced(tmp_path):
    result, _ = _observe(
        tmp_path,
        suffix="zero-price",
        capture=_complete_capture(
            price="0",
            raw_payload=b'{"symbol":"600519","price":0}',
            raw_payload_kind="normalized_provider_result",
        ),
    )

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert result.risk_disposition is RiskDisposition.NO_NEW_RISK
    assert result.captures[0].price == "0"
    assert "sh:600519:price_non_positive" in result.data_gaps


def test_raw_replay_mismatch_is_preserved_and_blocks_qualification(tmp_path):
    result, _ = _observe(
        tmp_path,
        suffix="raw-replay-mismatch",
        capture=_complete_capture(
            raw_payload=(
                b'{"symbol":"600519","venue":"sh",'
                b'"source_event_at":"2026-07-17T06:39:00.500000+00:00",'
                b'"price":"1499.00","trading_status":"trading",'
                b'"upper_limit_price":"1650.00","lower_limit_price":"1350.00"}'
            )
        ),
    )

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert result.risk_disposition is RiskDisposition.NO_NEW_RISK
    assert "sh:600519:raw_replay_mismatch" in result.data_gaps
    assert result.captures[0].raw_replay_status is RawReplayStatus.MISMATCH


def test_source_freshness_uses_measured_clock_offset_before_declaring_future_data(
    tmp_path,
) -> None:
    source_event_at = datetime(2026, 7, 17, 6, 39, 1, 500000, tzinfo=UTC)
    result, _ = _observe(
        tmp_path,
        suffix="offset-corrected-source-time",
        capture=_capture_with_replay(source_event_at=source_event_at),
        request_changes={"collector_clock_offset_ms": 595},
    )

    assert result.verdict is QualificationVerdict.QUALIFIED
    assert result.data_gaps == ()
    payload = result.captures[0].to_dict()
    assert payload["clock_corrected_received_at"] == "2026-07-17T06:39:01.595000+00:00"
    assert payload["source_age_at_receive_ms"] == 95


def test_unaccepted_clock_offset_is_never_persisted_as_a_correction(tmp_path) -> None:
    result, _ = _observe(
        tmp_path,
        suffix="unaccepted-clock-correction",
        request_changes={"collector_clock_offset_ms": 2_001},
    )

    payload = result.captures[0].to_dict()
    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert "sh:600519:collector_clock_offset_exceeded" in result.data_gaps
    assert payload["clock_correction_applied"] is False
    assert payload["accepted_clock_offset_ms"] is None
    assert payload["clock_corrected_received_at"] is None
    assert payload["source_age_at_receive_ms"] is None


def test_boolean_clock_offset_is_invalid_and_never_uses_raw_wall_as_canonical(tmp_path) -> None:
    result, _ = _observe(
        tmp_path,
        suffix="boolean-clock-offset",
        request_changes={"collector_clock_offset_ms": True},
    )

    payload = result.captures[0].to_dict()
    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert "sh:600519:collector_clock_offset_invalid" in result.data_gaps
    assert payload["clock_correction_applied"] is False
    assert payload["accepted_clock_offset_ms"] is None
    assert payload["clock_corrected_received_at"] is None
    assert payload["scheduler_lag_ms"] is None
    assert payload["ready_lag_ms"] is None


def test_sensitive_raw_material_is_sanitized_before_persistence(tmp_path):
    secret = "Bearer must-not-be-written"
    payload = json.loads(_complete_capture().raw_payload)
    payload["authorization"] = secret
    result, _ = _observe(
        tmp_path,
        suffix="sensitive-raw",
        capture=_complete_capture(raw_payload=json.dumps(payload).encode()),
    )

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert "sh:600519:raw_payload_sensitive_material" in result.data_gaps
    raw_path = tmp_path / "run-sensitive-raw" / result.captures[0].raw_payload_ref
    persisted = raw_path.read_text(encoding="utf-8")
    assert secret not in persisted
    assert result.captures[0].raw_payload_kind == "sanitized_sensitive_payload"


@pytest.mark.parametrize(
    ("changes", "expected_gap"),
    [
        ({"source_event_at": None}, "source_event_at_missing"),
        ({"symbol": "000001"}, "symbol_mismatch"),
        ({"trading_status": TradingStatus.UNKNOWN}, "trading_status_unknown"),
        ({"upper_limit_price": "0"}, "price_limits_invalid"),
        (
            {
                "requested_at": datetime(2026, 7, 17, 6, 39, 2, tzinfo=UTC),
                "received_at": datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
            },
            "received_before_request",
        ),
    ],
)
def test_critical_fact_failure_is_scoped_no_new_risk(tmp_path, changes, expected_gap):
    result, _ = _observe(
        tmp_path,
        suffix=expected_gap,
        capture=_complete_capture(**changes),
    )

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert result.risk_disposition is RiskDisposition.NO_NEW_RISK
    assert f"sh:600519:{expected_gap}" in result.data_gaps
    assert result.execution_allowed is False
    if expected_gap == "source_event_at_missing":
        assert result.captures[0].to_dict()["source_event_at"] is None


@pytest.mark.parametrize(
    ("capture_changes", "request_changes", "expected_gap"),
    [
        ({}, {"clock_sync_status": "unknown"}, "collector_clock_unsynchronized"),
        ({}, {"collector_clock_offset_ms": 2_001}, "collector_clock_offset_exceeded"),
        (
            {"received_at": datetime(2026, 7, 17, 6, 39, 31, tzinfo=UTC)},
            {},
            "checkpoint_hard_deadline_missed",
        ),
        (
            {
                "requested_at": datetime(2026, 7, 17, 6, 38, 58, tzinfo=UTC),
                "received_at": datetime(2026, 7, 17, 6, 38, 59, tzinfo=UTC),
                "source_event_at": datetime(2026, 7, 17, 6, 38, 58, 500000, tzinfo=UTC),
            },
            {},
            "received_before_checkpoint",
        ),
    ],
)
def test_time_gate_failure_is_scoped_no_new_risk(
    tmp_path, capture_changes, request_changes, expected_gap
):
    result, _ = _observe(
        tmp_path,
        suffix=expected_gap,
        capture=_complete_capture(**capture_changes),
        request_changes=request_changes,
    )

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert f"sh:600519:{expected_gap}" in result.data_gaps


def test_morning_window_target_miss_remains_auditable_before_hard_deadline(tmp_path):
    result, _ = _observe(
        tmp_path,
        suffix="morning-target-missed",
        request_changes={
            "usage_scope": QualificationUsageScope.PAPER_TACTICAL_DECISION,
            "checkpoint": QualificationCheckpoint.MORNING_WINDOW_1003,
            "target_ready_by": datetime(2026, 7, 17, 6, 39, 0, 500000, tzinfo=UTC),
        },
    )

    assert result.verdict is QualificationVerdict.QUALIFIED_WITH_DEGRADATION
    assert result.timing_status is CheckpointTimingStatus.TARGET_MISSED
    assert "sh:600519:checkpoint_target_missed" in result.data_gaps
    assert "sh:600519:checkpoint_hard_deadline_missed" not in result.data_gaps


def test_identical_replay_is_idempotent_and_raw_conflict_cannot_overwrite(tmp_path):
    source = _StaticSource(_complete_capture())
    manifest = _manifest("idempotent")
    request = _request("idempotent", manifest)
    service = DataQualificationService(
        source=source,
        output_root=tmp_path,
        clock=lambda: datetime(2026, 7, 17, 6, 39, 1, 100000, tzinfo=UTC),
        monotonic_ns=lambda: 0,
    )

    first = service.observe(request)
    second = service.observe(request)

    assert first.to_dict() == second.to_dict()
    captures_path = tmp_path / "run-idempotent" / "captures.jsonl"
    assert len(captures_path.read_text(encoding="utf-8").splitlines()) == 1

    raw_ref = first.captures[0].raw_payload_ref
    (tmp_path / "run-idempotent" / raw_ref).write_bytes(b"tampered")
    with pytest.raises(QualificationArtifactConflictError):
        service.observe(request)
    assert (tmp_path / "run-idempotent" / raw_ref).read_bytes() == b"tampered"


def test_run_envelope_and_report_cannot_be_reused_with_different_configuration(tmp_path):
    source = _StaticSource(_complete_capture())
    request = _request("frozen-run", _manifest("frozen-run"))
    service = DataQualificationService(
        source=source,
        output_root=tmp_path,
        clock=lambda: datetime(2026, 7, 17, 6, 39, 2, tzinfo=UTC),
    )

    first = service.observe(request)
    report_path = tmp_path / request.run_id / "latest_report.json"
    first_report = report_path.read_bytes()

    with pytest.raises(QualificationArtifactConflictError):
        service.observe(replace(request, max_source_age_ms=9999))

    assert source.calls == 1
    assert report_path.read_bytes() == first_report
    run_spec = json.loads((tmp_path / request.run_id / "run_spec.json").read_text())
    assert run_spec["run_spec_hash"] == first.run_spec_hash


def test_ready_at_not_network_receive_time_drives_sla_and_latency_metrics(tmp_path):
    capture = _complete_capture(
        received_at=datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
    )
    request = _request(
        "slow-normalization",
        _manifest("slow-normalization"),
        target_ready_by=datetime(2026, 7, 17, 6, 39, 2, tzinfo=UTC),
        deadline_at=datetime(2026, 7, 17, 6, 39, 5, tzinfo=UTC),
        usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
    )
    result = DataQualificationService(
        source=_StaticSource(capture),
        output_root=tmp_path,
        clock=lambda: datetime(2026, 7, 17, 6, 39, 2, 1000, tzinfo=UTC),
    ).observe(request)

    assert result.timing_status is CheckpointTimingStatus.TARGET_MISSED
    assert result.ready_at == datetime(2026, 7, 17, 6, 39, 2, 1000, tzinfo=UTC)
    artifact = result.captures[0].to_dict()
    assert artifact["normalization_lag_ms"] == 1001
    assert artifact["ready_lag_ms"] == 2026
    assert "checkpoint_target_missed" in result.data_gaps[0]


def test_paper_quote_source_age_is_qualified_at_ready_time(tmp_path):
    ready_at = datetime(2026, 7, 17, 6, 39, 4, tzinfo=UTC)
    result = DataQualificationService(
        source=_StaticSource(_complete_capture()),
        output_root=tmp_path,
        clock=lambda: ready_at,
    ).observe(
        _request(
            "stale-paper",
            _manifest("stale-paper"),
            usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
            target_ready_by=ready_at,
            deadline_at=ready_at,
        )
    )

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert "sh:600519:source_age_at_ready_exceeded" in result.data_gaps


def test_publication_receipt_uses_post_fsync_time_and_monotonic_duration(tmp_path):
    wall_times = iter(
        (
            datetime(2026, 7, 17, 6, 39, 29, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 31, tzinfo=UTC),
        )
    )
    monotonic_times = iter((1_000_000_000, 1_100_000_000, 1_300_000_000, 3_500_000_000))
    result = DataQualificationService(
        source=_StaticSource(
            _complete_capture(
                received_at=datetime(2026, 7, 17, 6, 39, 28, 500000, tzinfo=UTC),
                source_event_at=datetime(2026, 7, 17, 6, 39, 28, tzinfo=UTC),
            )
        ),
        output_root=tmp_path,
        clock=lambda: next(wall_times),
        monotonic_ns=lambda: next(monotonic_times),
    ).observe(_request("late-publication", _manifest("late-publication")))

    receipt = result.publication_receipt
    assert receipt is not None
    assert result.timing_status is CheckpointTimingStatus.TARGET_MET
    assert receipt.timing_status is CheckpointTimingStatus.HARD_DEADLINE_MISSED
    assert receipt.qualification_count_eligible is False
    assert receipt.processing_duration_ms == 2500
    assert result.captures[0].normalization_duration_ms == 200
    persisted = json.loads((tmp_path / result.run_id / "publication_receipt.json").read_text())
    assert persisted == receipt.to_dict()


def test_publication_wall_clock_rollback_cannot_count_as_eligible(tmp_path):
    """A post-fsync timestamp before ready_at is explicit, fail-closed evidence."""
    ready_at = datetime(2026, 7, 17, 6, 39, 2, tzinfo=UTC)
    published_at = datetime(2026, 7, 17, 6, 39, 1, 500000, tzinfo=UTC)
    wall_times = iter((ready_at, published_at))
    monotonic_times = iter((1_000_000_000, 1_100_000_000, 1_300_000_000, 1_500_000_000))
    source = _StaticSource(_complete_capture())
    source.evidence_origin = ObservationEvidenceOrigin.LIVE_CAPTURE

    result = DataQualificationService(
        source=source,
        output_root=tmp_path,
        clock=lambda: next(wall_times),
        monotonic_ns=lambda: next(monotonic_times),
    ).observe(_request("publication-clock-rollback", _manifest("publication-clock-rollback")))

    receipt = result.publication_receipt
    assert result.observation_eligible is True
    assert receipt is not None
    assert receipt.wall_clock_order_valid is False
    assert receipt.qualification_count_eligible is False
    assert receipt.timing_status is CheckpointTimingStatus.TARGET_MET


def test_source_failure_keeps_frozen_manifest_and_sanitized_failure_evidence(tmp_path):
    class FailingSource:
        source_id = "failing-reference"
        adapter_version = "failure-v1"
        evidence_origin = ObservationEvidenceOrigin.TEST_ONLY

        def __init__(self) -> None:
            self.manifest_was_frozen = False

        def capture(self, sample: QualificationSample) -> QualificationSourceCapture:
            self.manifest_was_frozen = (tmp_path / "run-source-failure" / "manifest.json").exists()
            raise OSError("sensitive upstream detail must not be persisted")

    source = FailingSource()
    request = _request("source-failure", _manifest("source-failure"))
    result = DataQualificationService(
        source=source,
        output_root=tmp_path,
        clock=lambda: SCHEDULED_AT,
    ).observe(request)

    assert source.manifest_was_frozen is True
    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert "sh:600519:source_capture_failed" in result.data_gaps
    capture = result.captures[0]
    assert capture.raw_payload_kind == "sanitized_capture_error"
    raw = (tmp_path / "run-source-failure" / capture.raw_payload_ref).read_text()
    assert json.loads(raw)["error_type"] == "OSError"
    assert "sensitive upstream detail" not in raw
    assert (tmp_path / "run-source-failure" / "latest_report.json").exists()


def test_legacy_quote_adapter_is_degraded_for_research_and_rejected_for_live(tmp_path):
    from fin_analyse.market.providers.base import QuoteResult

    class LegacyProvider:
        name = "legacy_fake"

        def __init__(self) -> None:
            self.calls = 0

        def get_quote(self, ticker: str) -> QuoteResult:
            self.calls += 1
            assert ticker == SAMPLE.symbol
            return QuoteResult(
                ticker=ticker,
                name="贵州茅台",
                price=1500.0,
                change_pct=1.25,
                volume=12345.0,
                turnover=67890.0,
            )

    source_times = iter(
        [
            SCHEDULED_AT,
            datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
            SCHEDULED_AT,
            datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
        ]
    )
    ready_times = iter(
        [
            datetime(2026, 7, 17, 6, 39, 1, 100000, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, 100000, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, 100000, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, 100000, tzinfo=UTC),
        ]
    )
    provider = LegacyProvider()
    source = LegacyQuoteQualificationAdapter(
        provider=provider,
        clock=lambda: next(source_times),
    )
    manifest = _manifest("legacy-research")
    request = _request(
        "legacy-research",
        manifest,
        usage_scope=QualificationUsageScope.RESEARCH_ONLY,
    )

    service = DataQualificationService(
        source=source,
        output_root=tmp_path,
        clock=lambda: next(ready_times),
    )
    result = service.observe(request)
    live_result = service.observe(
        _request(
            "legacy-live",
            _manifest("legacy-live"),
            usage_scope=QualificationUsageScope.LIVE_EXECUTION_REFERENCE,
        )
    )

    assert provider.calls == 2
    assert result.verdict is QualificationVerdict.QUALIFIED_WITH_DEGRADATION
    assert result.execution_allowed is False
    assert result.captures[0].source_event_at is None
    assert result.captures[0].raw_payload_kind == "normalized_provider_result"
    assert "sh:600519:source_event_at_missing" in result.data_gaps
    assert "sh:600519:upstream_raw_payload_unavailable" in result.data_gaps
    assert live_result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert live_result.risk_disposition is RiskDisposition.NO_NEW_RISK
