"""Behavior tests for the source-only Eastmoney qualification adapter."""

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
from fin_analyse.market.qualification_sources.eastmoney_http_transport import (
    _build_eastmoney_on_demand_http_get,
)
from fin_analyse.market.qualification_sources.eastmoney_raw import (
    EastmoneyRawQualificationSource,
    _build_on_demand_eastmoney_raw_source,
)

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "market" / "eastmoney_raw"
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


def test_capture_uses_exact_secid_preserves_bytes_and_replays_facts() -> None:
    raw_payload = (FIXTURE_ROOT / "sh600519_complete.json").read_bytes()
    calls: list[tuple[str, dict[str, str], dict[str, str], float, bool]] = []

    def http_get(
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _Response:
        calls.append((url, params, headers, timeout, allow_redirects))
        return _Response(raw_payload)

    times = iter(
        [
            datetime(2026, 7, 17, 6, 38, 59, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
        ]
    )
    monotonic_values = iter([1_000_000_000, 1_125_000_000])
    source = EastmoneyRawQualificationSource(
        http_get=http_get,
        clock=lambda: next(times),
        monotonic_ns=lambda: next(monotonic_values),
    )

    capture = source.capture(SAMPLE)

    assert calls == [
        (
            "https://push2delay.eastmoney.com/api/qt/stock/get",
            {
                "secid": "1.600519",
                "fields": "f43,f47,f48,f51,f52,f57,f58,f59,f86,f107,f292",
                "fltt": "1",
                "invt": "2",
            },
            {
                "Referer": "https://quote.eastmoney.com/sh600519.html",
                "User-Agent": "fin-analyse-market-data-qualification/1",
            },
            10.0,
            False,
        )
    ]
    assert source.source_id == "eastmoney_raw"
    assert source.adapter_version == "eastmoney_raw_qualification.v1"
    assert source.evidence_origin is ObservationEvidenceOrigin.TEST_ONLY
    assert capture.raw_payload == raw_payload
    assert capture.raw_payload_kind == "upstream_http_response"
    assert capture.fetch_duration_ms == 125
    assert capture.symbol == "600519"
    assert capture.venue == "sh"
    assert capture.price == "1500.00"
    assert capture.volume == "123456"
    assert capture.turnover == "185184000"
    assert capture.source_event_at is not None
    assert capture.source_event_at.isoformat() == "2026-07-17T14:39:00+08:00"
    assert capture.trading_status is TradingStatus.TRADING
    assert capture.upper_limit_price == "1650.00"
    assert capture.lower_limit_price == "1350.00"

    replayed = source.replay_normalize(SAMPLE, capture.raw_payload)
    assert replayed == QualificationNormalizedRecord.from_capture(capture)


@pytest.mark.parametrize(
    "raw_payload",
    [
        b"",
        b'{"rc":0,"data":',
        b'callback({"rc":0,"data":{}})',
        b'{"rc":0,"data":{"f58":"\xff"}}',
    ],
)
def test_invalid_json_jsonp_and_encoding_are_preserved_and_fail_closed(
    raw_payload: bytes,
) -> None:
    capture = _capture_payload(raw_payload)

    assert capture.raw_payload == raw_payload
    assert capture.raw_payload_kind == "upstream_http_response"
    assert capture.venue is None
    assert capture.price is None
    assert capture.trading_status is TradingStatus.UNKNOWN
    assert capture.data_gaps == ("source_payload_parse_failed",)
    with pytest.raises((UnicodeError, ValueError, TypeError)):
        EastmoneyRawQualificationSource().replay_normalize(SAMPLE, raw_payload)


def test_non_success_http_body_is_preserved_and_fails_closed() -> None:
    raw_payload = b'{"message":"upstream unavailable"}'

    capture = _capture_payload(raw_payload, status_code=503)

    assert capture.raw_payload == raw_payload
    assert capture.raw_payload_kind == "upstream_http_response"
    assert capture.venue is None
    assert capture.price is None
    assert capture.data_gaps == ("http_status_503",)


@pytest.mark.parametrize("response_code", [1, False, "0", None])
def test_invalid_upstream_response_code_is_preserved_and_fails_closed(
    response_code: object,
) -> None:
    payload = json.loads((FIXTURE_ROOT / "sh600519_complete.json").read_text(encoding="utf-8"))
    payload["rc"] = response_code
    raw_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    capture = _capture_payload(raw_payload)

    assert capture.raw_payload == raw_payload
    assert capture.data_gaps == ("source_payload_parse_failed",)


@pytest.mark.parametrize(
    "sample",
    [
        QualificationSample(symbol="600519&secid=0.000001", venue="sh"),
        QualificationSample(symbol="920001", venue="bj"),
    ],
)
def test_invalid_sample_is_rejected_before_network(sample: QualificationSample) -> None:
    def forbidden_http_get(*args, **kwargs):
        raise AssertionError("invalid sample unexpectedly reached the network")

    source = EastmoneyRawQualificationSource(http_get=forbidden_http_get)

    with pytest.raises(ValueError, match="six digits and venue sh/sz"):
        source.capture(sample)


def test_injected_transport_cannot_claim_live_evidence_origin() -> None:
    def fake_http_get(*args, **kwargs):
        return _Response(b"{}")

    for transport in (fake_http_get, _build_eastmoney_on_demand_http_get()):
        with pytest.raises(ValueError, match="injected Eastmoney transport must use TEST_ONLY"):
            EastmoneyRawQualificationSource(
                http_get=transport,  # type: ignore[arg-type]
                evidence_origin=ObservationEvidenceOrigin.LIVE_CAPTURE,
            )


def test_capture_authority_is_immutable_after_construction() -> None:
    raw_payload = (FIXTURE_ROOT / "sh600519_complete.json").read_bytes()
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
    source = EastmoneyRawQualificationSource(
        http_get=http_get,
        clock=clock,
        monotonic_ns=monotonic_ns,
    )
    live_source = EastmoneyRawQualificationSource(
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
        source._http_get = lambda *args, **kwargs: _Response(b"{}")
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
        EastmoneyRawQualificationSource.__init__(
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
        EastmoneyRawQualificationSource(evidence_origin="LIVE_CAPTURE")  # type: ignore[arg-type]


def test_on_demand_transport_authority_is_immutable_and_capture_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_payload = (FIXTURE_ROOT / "sh600519_complete.json").read_bytes()

    def primary_get(*args, **kwargs) -> _Response:
        return _Response(raw_payload)

    monkeypatch.setattr(
        "fin_analyse.market.qualification_sources.eastmoney_http_transport.requests.get",
        primary_get,
    )
    transport = _build_eastmoney_on_demand_http_get()
    source = _build_on_demand_eastmoney_raw_source(
        transport=transport,
        clock=None,
        timeout_seconds=8.0,
    )
    transport_field = "_OnDemandEastmoneyRawQualificationSource__on_demand_transport"

    for field in ("_configuration_sealed", "_evidence_origin", transport_field):
        with pytest.raises(AttributeError, match="configuration is immutable"):
            delattr(source, field)
    with pytest.raises(AttributeError, match="configuration is immutable"):
        setattr(source, transport_field, transport)

    replacement_transport = _build_eastmoney_on_demand_http_get()
    authority_fields = (
        transport_field,
        "_http_get",
        "_clock",
        "_monotonic_ns",
        "evidence_origin",
        "_timeout_seconds",
    )
    before = attrgetter(*authority_fields)(source)
    boom_clock = _NoTruthinessCallable(lambda: None)
    with pytest.raises(RuntimeError, match="already initialized"):
        type(source).__init__(
            source,
            transport=replacement_transport,
            clock=boom_clock,
            timeout_seconds=1.0,
        )
    assert attrgetter(*authority_fields)(source) == before
    assert boom_clock.truth_checks == 0

    assert source.capture(SAMPLE).price == "1500.00"


def test_primary_payload_larger_than_the_request_contract_is_rejected() -> None:
    def oversized_http_get(*args, **kwargs):
        return _Response(b"x" * (64 * 1024 + 1))

    source = EastmoneyRawQualificationSource(http_get=oversized_http_get)

    with pytest.raises(ValueError, match="^invalid Eastmoney HTTP response$"):
        source.capture(SAMPLE)


def test_complete_fixture_runs_through_observer_without_qualification_credit(
    tmp_path: Path,
) -> None:
    raw_payload = (FIXTURE_ROOT / "sh600519_complete.json").read_bytes()

    result = _observe_raw(tmp_path, raw_payload, run_id="run-eastmoney-fixture")

    assert result.verdict is QualificationVerdict.QUALIFIED
    assert result.data_gaps == ()
    assert result.observation_eligible is False
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is False
    capture = result.captures[0]
    assert capture.raw_replay_status is RawReplayStatus.VERIFIED
    assert capture.raw_payload_sha256 == hashlib.sha256(raw_payload).hexdigest()
    assert (tmp_path / result.run_id / capture.raw_payload_ref).read_bytes() == raw_payload
    persisted = json.loads(
        (tmp_path / result.run_id / "captures.jsonl").read_text(encoding="utf-8")
    )
    assert persisted["normalized_payload_sha256"] == capture.normalized_payload_sha256


def test_wrong_symbol_and_market_are_retained_and_fail_closed(tmp_path: Path) -> None:
    raw_payload = _fixture_payload(f57="000001", f107=0)

    result = _observe_raw(tmp_path, raw_payload, run_id="run-wrong-security")

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    capture = result.captures[0]
    assert capture.symbol == "000001"
    assert capture.venue == "sz"
    assert capture.raw_replay_status is RawReplayStatus.VERIFIED
    assert "sh:600519:symbol_mismatch" in result.data_gaps
    assert "sh:600519:venue_mismatch" in result.data_gaps
    assert (tmp_path / result.run_id / capture.raw_payload_ref).read_bytes() == raw_payload


@pytest.mark.parametrize(
    "fields",
    [
        {"f59": None},
        {"f59": "2"},
        {"f59": -1},
        {"f59": 20},
        {"f43": 1500.0},
        {"f51": "165000"},
    ],
)
def test_invalid_price_scaling_is_preserved_and_fails_replay(
    tmp_path: Path,
    fields: dict[str, object],
) -> None:
    raw_payload = _fixture_payload(**fields)

    result = _observe_raw(tmp_path, raw_payload, run_id=f"run-scale-{len(str(fields))}")

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    capture = result.captures[0]
    assert capture.raw_replay_status is RawReplayStatus.FAILED
    assert "sh:600519:source_payload_parse_failed" in result.data_gaps
    assert "sh:600519:raw_replay_failed" in result.data_gaps
    assert (tmp_path / result.run_id / capture.raw_payload_ref).read_bytes() == raw_payload


@pytest.mark.parametrize(
    ("field", "expected_gap"),
    [
        ("f43", "price_missing"),
        ("f86", "source_event_at_missing"),
        ("f51", "price_limits_missing"),
        ("f52", "price_limits_missing"),
        ("f292", "trading_status_unknown"),
    ],
)
def test_missing_capital_fact_is_typed_unknown_and_fails_closed(
    tmp_path: Path,
    field: str,
    expected_gap: str,
) -> None:
    raw_payload = _fixture_payload(**{field: None})

    result = _observe_raw(tmp_path, raw_payload, run_id=f"run-missing-{field}")

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    capture = result.captures[0]
    assert capture.raw_replay_status is RawReplayStatus.VERIFIED
    assert f"sh:600519:{expected_gap}" in result.data_gaps
    assert (tmp_path / result.run_id / capture.raw_payload_ref).read_bytes() == raw_payload
    if field == "f292":
        assert capture.trading_status is TradingStatus.UNKNOWN


def test_stale_source_time_fails_closed_instead_of_using_receive_time(tmp_path: Path) -> None:
    raw_payload = _fixture_payload(f86=1784270330)

    result = _observe_raw(tmp_path, raw_payload, run_id="run-stale-source-time")

    assert result.verdict is QualificationVerdict.NOT_QUALIFIED
    capture = result.captures[0]
    assert capture.source_event_at is not None
    assert capture.source_event_at.isoformat() == "2026-07-17T14:38:50+08:00"
    assert capture.received_at.isoformat() == "2026-07-17T06:39:01+00:00"
    assert capture.raw_replay_status is RawReplayStatus.VERIFIED
    assert "sh:600519:source_age_at_ready_exceeded" in result.data_gaps


@pytest.mark.parametrize(
    ("upstream_status", "expected"),
    [
        (2, TradingStatus.TRADING),
        (6, TradingStatus.SUSPENDED),
        (14, TradingStatus.SUSPENDED),
        (13, TradingStatus.UNKNOWN),
    ],
)
def test_trading_status_maps_only_explicit_upstream_meanings(
    upstream_status: int,
    expected: TradingStatus,
) -> None:
    normalized = EastmoneyRawQualificationSource().replay_normalize(
        SAMPLE,
        _fixture_payload(f292=upstream_status),
    )

    assert normalized.trading_status is expected


def test_unknown_status_cannot_be_a_count_eligible_paper_observation(tmp_path: Path) -> None:
    result = _observe_raw(
        tmp_path,
        _fixture_payload(f292=13),
        run_id="run-paper-unknown-status",
        usage_scope=QualificationUsageScope.PAPER_TACTICAL_DECISION,
    )

    assert result.verdict is QualificationVerdict.QUALIFIED_WITH_DEGRADATION
    assert result.observation_eligible is False
    assert result.publication_receipt is not None
    assert result.publication_receipt.qualification_count_eligible is False
    assert "sh:600519:trading_status_unknown" in result.data_gaps


@pytest.mark.parametrize(
    "fields",
    [
        {"f57": None},
        {"f57": "600519X"},
        {"f107": None},
        {"f107": 2},
        {"f86": "1784270340"},
        {"f292": "2"},
    ],
)
def test_invalid_identity_time_or_status_type_is_preserved_and_fails_closed(
    fields: dict[str, object],
) -> None:
    raw_payload = _fixture_payload(**fields)

    capture = _capture_payload(raw_payload)

    assert capture.raw_payload == raw_payload
    assert capture.venue is None
    assert capture.price is None
    assert capture.trading_status is TradingStatus.UNKNOWN
    assert capture.data_gaps == ("source_payload_parse_failed",)


def _capture_payload(raw_payload: bytes, *, status_code: int = 200):
    def http_get(
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _Response:
        assert allow_redirects is False
        return _Response(raw_payload, status_code=status_code)

    times = iter(
        [
            datetime(2026, 7, 17, 6, 38, 59, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
        ]
    )
    return EastmoneyRawQualificationSource(
        http_get=http_get,
        clock=lambda: next(times),
    ).capture(SAMPLE)


def _fixture_payload(**fields: object) -> bytes:
    payload = json.loads((FIXTURE_ROOT / "sh600519_complete.json").read_text(encoding="utf-8"))
    payload["data"].update(fields)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _observe_raw(
    tmp_path: Path,
    raw_payload: bytes,
    *,
    run_id: str,
    usage_scope: QualificationUsageScope = QualificationUsageScope.LIVE_EXECUTION_REFERENCE,
):
    def http_get(
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _Response:
        assert allow_redirects is False
        return _Response(raw_payload)

    capture_times = iter(
        [
            datetime(2026, 7, 17, 6, 38, 59, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, tzinfo=UTC),
        ]
    )
    observer_times = iter(
        [
            datetime(2026, 7, 17, 6, 39, 1, 100000, tzinfo=UTC),
            datetime(2026, 7, 17, 6, 39, 1, 200000, tzinfo=UTC),
        ]
    )
    source = EastmoneyRawQualificationSource(
        http_get=http_get,
        clock=lambda: next(capture_times),
    )
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
            campaign_id="eastmoney-candidate-shape-only",
            source_policy_id="eastmoney-raw-candidate-v1",
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
