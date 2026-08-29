"""Behavior tests for the source-only Tencent qualification adapter."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from operator import attrgetter
from pathlib import Path

import pytest

from fin_analyse.market.data_qualification import (
    DataQualificationRequest,
    DataQualificationService,
    ObservationEvidenceOrigin,
    QualificationCheckpoint,
    QualificationDataset,
    QualificationNormalizedRecord,
    QualificationSample,
    QualificationUsageScope,
    QualificationVerdict,
    RawReplayStatus,
    SampleManifest,
    TradingStatus,
)
from fin_analyse.market.qualification_sources.tencent_raw import TencentRawQualificationSource

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "market" / "tencent_raw"
SAMPLE = QualificationSample(symbol="600519", venue="sh")


class _Response:
    def __init__(self, content: bytes, *, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code


class _NoTruthinessCallable:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.truth_checks = 0

    def __bool__(self) -> bool:
        self.truth_checks += 1
        raise AssertionError("collaborator truthiness must not be evaluated")

    def __call__(self, *args, **kwargs):
        return self.callback(*args, **kwargs)


def test_injected_transport_cannot_claim_live_evidence() -> None:
    """Only the adapter-owned default transport may emit LIVE_CAPTURE provenance."""
    with pytest.raises(ValueError, match="injected Tencent transport must use TEST_ONLY"):
        TencentRawQualificationSource(
            http_get=lambda *args, **kwargs: _Response(b""),
            evidence_origin=ObservationEvidenceOrigin.LIVE_CAPTURE,
        )


def test_capture_authority_is_immutable_after_construction() -> None:
    raw_payload = (FIXTURE_ROOT / "sh600519_complete.gbk").read_bytes()
    calls = 0

    def bound_http_get(*args, **kwargs) -> _Response:
        nonlocal calls
        calls += 1
        return _Response(raw_payload)

    times = iter(
        [
            datetime(2026, 7, 17, 6, 38, 59, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
        ]
    )
    monotonic_values = iter([1_000_000_000, 1_125_000_000])
    http_get = _NoTruthinessCallable(bound_http_get)
    clock = _NoTruthinessCallable(lambda: next(times))
    monotonic_ns = _NoTruthinessCallable(lambda: next(monotonic_values))
    source = TencentRawQualificationSource(
        http_get=http_get,
        clock=clock,
        monotonic_ns=monotonic_ns,
    )
    live_source = TencentRawQualificationSource(
        evidence_origin=ObservationEvidenceOrigin.LIVE_CAPTURE
    )

    for field in ("_configuration_sealed", "_evidence_origin", "_http_get"):
        with pytest.raises(AttributeError, match="configuration is immutable"):
            delattr(source, field)
    with pytest.raises(AttributeError, match="configuration is immutable"):
        source.evidence_origin = ObservationEvidenceOrigin.LIVE_CAPTURE
    with pytest.raises(AttributeError, match="configuration is immutable"):
        source._evidence_origin = ObservationEvidenceOrigin.LIVE_CAPTURE
    with pytest.raises(AttributeError, match="configuration is immutable"):
        source._http_get = lambda *args, **kwargs: _Response(b"")
    with pytest.raises(AttributeError, match="configuration is immutable"):
        live_source._http_get = bound_http_get

    assert source.capture(SAMPLE).price == "1500.00"
    assert calls == 1
    assert (http_get.truth_checks, clock.truth_checks, monotonic_ns.truth_checks) == (0, 0, 0)

    authority_fields = (
        "_http_get",
        "_clock",
        "_monotonic_ns",
        "evidence_origin",
        "_timeout_seconds",
    )
    before = attrgetter(*authority_fields)(live_source)
    boom_clock = _NoTruthinessCallable(lambda: None)
    with pytest.raises(RuntimeError, match="already initialized"):
        TencentRawQualificationSource.__init__(
            live_source,
            http_get=bound_http_get,
            clock=boom_clock,
            monotonic_ns=lambda: 0,
            evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
            timeout_seconds=1.0,
        )
    assert attrgetter(*authority_fields)(live_source) == before
    assert boom_clock.truth_checks == 0

    with pytest.raises(TypeError, match="evidence_origin must be"):
        TencentRawQualificationSource(evidence_origin="LIVE_CAPTURE")  # type: ignore[arg-type]


def test_capture_preserves_upstream_bytes_and_replays_normalized_facts() -> None:
    raw_payload = (FIXTURE_ROOT / "sh600519_complete.gbk").read_bytes()
    assert "贵州茅台" in raw_payload.decode("gb18030", errors="strict")
    assert all(
        marker not in raw_payload.lower()
        for marker in (b"authorization", b"cookie", b"password", b"secret", b"token")
    )
    calls: list[tuple[str, dict[str, str], float, bool]] = []

    def http_get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _Response:
        calls.append((url, headers, timeout, allow_redirects))
        return _Response(raw_payload)

    times = iter(
        [
            datetime(2026, 7, 17, 6, 38, 59, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
        ]
    )
    monotonic_values = iter([1_000_000_000, 1_125_000_000])
    source = TencentRawQualificationSource(
        http_get=http_get,
        clock=lambda: next(times),
        monotonic_ns=lambda: next(monotonic_values),
    )

    capture = source.capture(SAMPLE)

    assert calls == [
        (
            "https://qt.gtimg.cn/q=sh600519",
            {
                "Referer": "https://gu.qq.com/",
                "User-Agent": "fin-analyse-market-data-qualification/1",
            },
            10.0,
            False,
        )
    ]
    assert source.source_id == "tencent_raw"
    assert source.evidence_origin is ObservationEvidenceOrigin.TEST_ONLY
    assert capture.raw_payload == raw_payload
    assert capture.raw_payload_kind == "upstream_http_response"
    assert capture.fetch_duration_ms == 125
    assert capture.symbol == "600519"
    assert capture.venue == "sh"
    assert capture.price == "1500.00"
    assert capture.source_event_at is not None
    assert capture.source_event_at.isoformat() == "2026-07-17T14:39:00+08:00"
    assert capture.trading_status is TradingStatus.UNKNOWN
    assert capture.upper_limit_price == "1650.00"
    assert capture.lower_limit_price == "1350.00"

    replayed = source.replay_normalize(SAMPLE, capture.raw_payload)
    assert replayed == QualificationNormalizedRecord.from_capture(capture)


def test_malformed_response_is_preserved_as_a_failed_capture() -> None:
    raw_payload = b'v_sh600519="1~truncated";\n'

    def http_get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _Response:
        assert allow_redirects is False
        return _Response(raw_payload)

    times = iter(
        [
            datetime(2026, 7, 17, 6, 38, 59, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
        ]
    )
    source = TencentRawQualificationSource(http_get=http_get, clock=lambda: next(times))

    capture = source.capture(SAMPLE)

    assert capture.raw_payload == raw_payload
    assert capture.raw_payload_kind == "upstream_http_response"
    assert capture.symbol == SAMPLE.symbol
    assert capture.venue is None
    assert capture.price is None
    assert capture.trading_status is TradingStatus.UNKNOWN
    assert capture.data_gaps == ("source_payload_parse_failed",)
    with pytest.raises(ValueError, match="truncated Tencent quote response"):
        source.replay_normalize(SAMPLE, capture.raw_payload)


def test_non_success_http_body_is_preserved_and_fails_closed() -> None:
    raw_payload = b"upstream unavailable"

    def http_get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _Response:
        assert allow_redirects is False
        return _Response(raw_payload, status_code=503)

    times = iter(
        [
            datetime(2026, 7, 17, 6, 38, 59, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
        ]
    )
    source = TencentRawQualificationSource(http_get=http_get, clock=lambda: next(times))

    capture = source.capture(SAMPLE)

    assert capture.raw_payload == raw_payload
    assert capture.raw_payload_kind == "upstream_http_response"
    assert capture.venue is None
    assert capture.price is None
    assert capture.data_gaps == ("http_status_503",)


@pytest.mark.parametrize(
    "sample",
    [
        QualificationSample(symbol="600519&q=sz000001", venue="sh"),
        QualificationSample(symbol="920001", venue="bj"),
    ],
)
def test_invalid_sample_is_rejected_before_network(sample: QualificationSample) -> None:
    def forbidden_http_get(*args, **kwargs):
        raise AssertionError("invalid sample unexpectedly reached the network")

    source = TencentRawQualificationSource(http_get=forbidden_http_get)

    with pytest.raises(ValueError, match="six digits and venue sh/sz"):
        source.capture(sample)


def test_complete_test_fixture_runs_through_observer_without_qualification_credit(
    tmp_path: Path,
) -> None:
    raw_payload = (FIXTURE_ROOT / "sh600519_complete.gbk").read_bytes()
    result = _observe_raw(
        tmp_path,
        raw_payload,
        run_id="run-tencent-fixture",
    )

    assert result.verdict is QualificationVerdict.QUALIFIED
    assert result.data_gaps == ()
    assert result.observation_eligible is False
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is False
    capture = result.captures[0]
    assert capture.raw_replay_status is RawReplayStatus.VERIFIED
    assert capture.raw_payload_sha256 == hashlib.sha256(raw_payload).hexdigest()
    persisted_raw = (tmp_path / "run-tencent-fixture" / capture.raw_payload_ref).read_bytes()
    assert persisted_raw == raw_payload
    persisted = json.loads(
        (tmp_path / "run-tencent-fixture" / "captures.jsonl").read_text(encoding="utf-8")
    )
    assert persisted["normalized_payload_sha256"] == capture.normalized_payload_sha256


def test_wrong_symbol_and_venue_are_retained_and_fail_closed(tmp_path: Path) -> None:
    raw_payload = (FIXTURE_ROOT / "sh600519_complete.gbk").read_bytes()
    raw_payload = raw_payload.replace(b"v_sh600519=", b"v_sz000001=").replace(
        b"~600519~", b"~000001~", 1
    )

    result = _observe_raw(tmp_path, raw_payload, run_id="run-wrong-security")

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert result.captures[0].symbol == "000001"
    assert result.captures[0].venue == "sz"
    assert "sh:600519:symbol_mismatch" in result.data_gaps
    assert "sh:600519:venue_mismatch" in result.data_gaps
    assert result.captures[0].raw_replay_status is RawReplayStatus.VERIFIED


def test_invalid_gb18030_is_preserved_and_fails_replay(tmp_path: Path) -> None:
    raw_payload = (
        (FIXTURE_ROOT / "sh600519_complete.gbk")
        .read_bytes()
        .replace("贵州茅台".encode("gb18030"), b"\xff", 1)
    )

    result = _observe_raw(tmp_path, raw_payload, run_id="run-invalid-charset")

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    capture = result.captures[0]
    assert capture.raw_payload_sha256 == hashlib.sha256(raw_payload).hexdigest()
    assert capture.raw_replay_status is RawReplayStatus.FAILED
    assert "sh:600519:source_payload_parse_failed" in result.data_gaps
    assert "sh:600519:raw_replay_failed" in result.data_gaps
    assert (tmp_path / result.run_id / capture.raw_payload_ref).read_bytes() == raw_payload


def test_utf8_body_that_gb18030_can_misdecode_still_fails_closed(tmp_path: Path) -> None:
    raw_payload = (
        (FIXTURE_ROOT / "sh600519_complete.gbk")
        .read_bytes()
        .replace("贵州茅台".encode("gb18030"), "贵州茅台".encode(), 1)
    )
    assert raw_payload.decode("gb18030", errors="strict")

    result = _observe_raw(tmp_path, raw_payload, run_id="run-wrong-utf8-charset")

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    capture = result.captures[0]
    assert capture.raw_replay_status is RawReplayStatus.FAILED
    assert "sh:600519:source_payload_parse_failed" in result.data_gaps
    assert (tmp_path / result.run_id / capture.raw_payload_ref).read_bytes() == raw_payload


def test_missing_price_limit_is_unknown_and_fails_capital_scope(tmp_path: Path) -> None:
    raw_payload = (
        (FIXTURE_ROOT / "sh600519_complete.gbk")
        .read_bytes()
        .replace(b'~1650.00~1350.00";', b'~~1350.00";', 1)
    )

    result = _observe_raw(
        tmp_path,
        raw_payload,
        run_id="run-missing-limit",
        usage_scope=QualificationUsageScope.LIVE_EXECUTION_REFERENCE,
    )

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert result.captures[0].upper_limit_price is None
    assert result.captures[0].lower_limit_price == "1350.00"
    assert result.captures[0].raw_replay_status is RawReplayStatus.VERIFIED
    assert "sh:600519:price_limits_missing" in result.data_gaps
    assert "sh:600519:trading_status_unknown" in result.data_gaps


def test_missing_last_price_is_typed_unknown_and_fails_capital_scope(tmp_path: Path) -> None:
    raw_payload = (
        (FIXTURE_ROOT / "sh600519_complete.gbk")
        .read_bytes()
        .replace(b"~1500.00~1490.00~", b"~~1490.00~", 1)
    )

    result = _observe_raw(
        tmp_path,
        raw_payload,
        run_id="run-missing-last-price",
        usage_scope=QualificationUsageScope.LIVE_EXECUTION_REFERENCE,
    )

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    assert result.captures[0].price is None
    assert result.captures[0].raw_replay_status is RawReplayStatus.VERIFIED
    assert "sh:600519:price_missing" in result.data_gaps


def test_stale_upstream_event_time_fails_closed_instead_of_using_receive_time(
    tmp_path: Path,
) -> None:
    raw_payload = (
        (FIXTURE_ROOT / "sh600519_complete.gbk")
        .read_bytes()
        .replace(b"20260717143900", b"20260717143850", 1)
    )

    result = _observe_raw(
        tmp_path,
        raw_payload,
        run_id="run-stale-source-time",
        usage_scope=QualificationUsageScope.LIVE_EXECUTION_REFERENCE,
    )

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    capture = result.captures[0]
    assert capture.source_event_at is not None
    assert capture.source_event_at.isoformat() == "2026-07-17T14:38:50+08:00"
    assert capture.received_at.isoformat() == "2026-07-17T06:39:01+00:00"
    assert "sh:600519:source_age_at_ready_exceeded" in result.data_gaps


def _observe_raw(
    tmp_path: Path,
    raw_payload: bytes,
    *,
    run_id: str,
    usage_scope: QualificationUsageScope = QualificationUsageScope.RESEARCH_ONLY,
):
    def http_get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _Response:
        assert allow_redirects is False
        return _Response(raw_payload)

    capture_times = iter(
        [
            datetime(2026, 7, 17, 6, 39, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
        ]
    )
    observer_times = iter(
        [
            datetime(2026, 7, 17, 6, 39, 1, 100000, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, 200000, tzinfo=UTC),
        ]
    )
    source = TencentRawQualificationSource(http_get=http_get, clock=lambda: next(capture_times))
    manifest = SampleManifest.build(
        manifest_id=f"manifest-{run_id}",
        created_at=datetime(2026, 7, 17, 6, 30, tzinfo=UTC),
        selection_cutoff_at=datetime(2026, 7, 17, 6, 35, tzinfo=UTC),
        samples=(SAMPLE,),
    )
    return DataQualificationService(
        source=source,
        output_root=tmp_path,
        clock=lambda: next(observer_times),
    ).observe(
        DataQualificationRequest(
            run_id=run_id,
            campaign_id="candidate-shape-only",
            source_policy_id="tencent-raw-candidate-v1",
            trade_date=date(2026, 7, 17),
            dataset=QualificationDataset.REALTIME_QUOTE,
            usage_scope=usage_scope,
            checkpoint=QualificationCheckpoint.EXECUTION_REFRESH_1439,
            scheduled_at=datetime(2026, 7, 17, 6, 39, tzinfo=UTC),
            target_ready_by=datetime(2026, 7, 17, 6, 39, 30, tzinfo=UTC),
            deadline_at=datetime(2026, 7, 17, 6, 39, 30, tzinfo=UTC),
            manifest=manifest,
            clock_sync_status="synchronized",
            collector_clock_offset_ms=10,
        )
    )
