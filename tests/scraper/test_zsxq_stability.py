"""R1-6 auditable capture→ingest→G stability evidence."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime

from fin_analyse.scraper.capture_artifact import load_capture_artifact
from fin_analyse.scraper.cdp_scraper import TZ
from fin_analyse.scraper.zsxq_stability import (
    assess_three_trading_day_campaign,
    build_capture_ingest_audit,
)
from tests.scraper.capture_fixtures import (
    build_cursor_artifact_payload,
    content_hash,
    write_artifact,
)


def _ready_g_receipt() -> dict[str, object]:
    return {
        "published": True,
        "status": "READY",
        "generation": "a" * 64,
        "evaluated_at": "2026-07-23T10:00:00+08:00",
        "source_coverage_sha256": "b" * 64,
        "data_gaps": [],
    }


def _index_rows() -> list[dict[str, object]]:
    return [
        {
            "id": "zsxq-700000000000001",
            "topic_id": "700000000000001",
            "column": "星大派特刊",
            "source_family": "星大派",
            "content_type": "特刊",
            "source_usage": "systematic_framework",
            "priority_label": None,
        },
        {
            "id": "zsxq-700000000000002",
            "topic_id": "700000000000002",
            "column": "凤仙郡小故事",
            "source_family": "凤仙郡小故事",
            "content_type": "长期故事",
            "source_usage": "long_term_framework",
            "priority_label": "重中之重",
        },
    ]


def test_capture_ingest_audit_proves_cursor_denominator_and_canonical_identity(tmp_path) -> None:
    now = datetime(2026, 7, 23, 16, 0, tzinfo=TZ)
    artifact = load_capture_artifact(
        write_artifact(tmp_path, build_cursor_artifact_payload(now))
    )

    audit = build_capture_ingest_audit(
        artifact,
        ingest_status="succeeded",
        completion_status="ready",
        g_working_set=_ready_g_receipt(),
        index_articles=_index_rows(),
    )

    assert audit["integrity_status"] == "PROVEN"
    assert audit["chain"]["ready"] is True
    assert audit["coverage"]["cursor_page_count"] == 1
    assert audit["coverage"]["proven"] is True
    assert audit["denominator"] == {
        "expected_teacher_item_count": 2,
        "ingested_teacher_item_count": 2,
        "missing_expected_topic_uids": [],
        "duplicate_canonical_identities": [],
        "status": "PROVEN",
    }
    assert audit["expected_teacher_item_ids"] == ["700000000000001", "700000000000002"]
    assert audit["items"][0]["canonical_duplicate_identity"] == "zsxq-topic:700000000000001"
    assert audit["items"][1]["source_usage"] == "long_term_framework"
    assert "valid_until" not in audit["items"][1]


def test_no_change_cannot_claim_zero_loss_without_native_coverage(tmp_path) -> None:
    now = datetime(2026, 7, 23, 16, 0, tzinfo=TZ)
    artifact = load_capture_artifact(write_artifact(tmp_path, build_cursor_artifact_payload(now)))
    without_cursor = deepcopy(artifact)
    object.__setattr__(without_cursor, "cursor_pages", ())

    audit = build_capture_ingest_audit(
        without_cursor,
        ingest_status="no_change",
        completion_status="ready",
        g_working_set=_ready_g_receipt(),
        index_articles=[],
    )

    assert audit["integrity_status"] == "UNKNOWN"
    assert audit["denominator"]["status"] == "UNKNOWN"
    assert "zsxq_audit_expected_teacher_denominator_unavailable" in audit["data_gaps"]


def test_partial_capture_cannot_count_as_a_successful_stability_chain(tmp_path) -> None:
    now = datetime(2026, 7, 23, 16, 0, tzinfo=TZ)
    payload = build_cursor_artifact_payload(now)
    payload["final_status"] = "partial"
    payload["content_sha256"] = content_hash(payload)
    artifact = load_capture_artifact(
        write_artifact(tmp_path, payload)
    )

    audit = build_capture_ingest_audit(
        artifact,
        ingest_status="succeeded",
        completion_status="ready",
        g_working_set=_ready_g_receipt(),
        index_articles=_index_rows(),
    )

    assert audit["coverage"]["proven"] is True
    assert audit["chain"]["ready"] is False
    assert "zsxq_audit_capture_not_complete" in audit["data_gaps"]


def test_campaign_requires_exactly_three_distinct_authoritative_trading_days(tmp_path) -> None:
    now = datetime(2026, 7, 23, 16, 0, tzinfo=TZ)
    artifact = load_capture_artifact(write_artifact(tmp_path, build_cursor_artifact_payload(now)))
    baseline = build_capture_ingest_audit(
        artifact,
        ingest_status="succeeded",
        completion_status="ready",
        g_working_set=_ready_g_receipt(),
        index_articles=_index_rows(),
    )
    days = (date(2026, 7, 21), date(2026, 7, 22), date(2026, 7, 23))
    receipts: list[dict[str, object]] = []
    for index, day in enumerate(days):
        receipt = deepcopy(baseline)
        receipt["trading_day"] = day.isoformat()
        receipt["captured_at"] = datetime(day.year, day.month, day.day, 16, 0, tzinfo=TZ).isoformat()
        receipt["run_id"] = f"run-{index}"
        receipt["artifact_content_sha256"] = f"{index + 1}" * 64
        receipts.append(receipt)

    result = assess_three_trading_day_campaign(
        receipts,
        expected_trading_days=days,
    )

    assert result["status"] == "COMPLETE"
    assert result["accepted_run_ids"] == ["run-0", "run-1", "run-2"]

    replay = deepcopy(receipts)
    replay[2]["trading_day"] = days[1].isoformat()
    failed = assess_three_trading_day_campaign(replay, expected_trading_days=days)
    assert failed["status"] == "PARTIAL"
    assert "zsxq_campaign_duplicate_trading_day" in failed["data_gaps"]


def test_campaign_rejects_incomplete_self_asserted_audit_shape() -> None:
    days = (date(2026, 7, 21), date(2026, 7, 22), date(2026, 7, 23))
    receipts = [
        {
            "schema_version": "fin.zsxq-capture-ingest-audit/v1",
            "chain_id": "zsxq-capture-artifact-to-ingest-to-g-working-set/v1",
            "run_id": f"run-{index}",
            "trading_day": day.isoformat(),
            "artifact_content_sha256": f"{index + 1}" * 64,
            "ingest_status": "no_change",
            "coverage": {"proven": True},
            "denominator": {"status": "PROVEN"},
            "chain": {"ready": True},
            "integrity_status": "PROVEN",
        }
        for index, day in enumerate(days)
    ]

    result = assess_three_trading_day_campaign(receipts, expected_trading_days=days)

    assert result["status"] == "PARTIAL"
    assert "zsxq_campaign_chain_not_ready" in result["data_gaps"]
