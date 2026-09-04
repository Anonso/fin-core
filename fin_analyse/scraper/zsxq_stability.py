"""Bounded R1-6 evidence for the existing ZSXQ capture→ingest→G chain.

The module writes no state and starts no browser or scheduler.  Capture ingest
owns persistence; this module only turns an already validated artifact and the
post-ingest index into a replayable receipt, then evaluates three receipts
against an explicitly supplied trading-day authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime

from fin_analyse.guo_teacher_research.source_contract import classify_g_source

from .capture_artifact import CaptureArtifact
from .cdp_scraper import TZ, _decode_topic_cursor_page

AUDIT_RECEIPT_SCHEMA_VERSION = "fin.zsxq-capture-ingest-audit/v1"
CAMPAIGN_SCHEMA_VERSION = "fin.zsxq-three-trading-day-campaign/v1"
CHAIN_ID = "zsxq-capture-artifact-to-ingest-to-g-working-set/v1"


def build_capture_ingest_audit(
    artifact: CaptureArtifact,
    *,
    ingest_status: object,
    completion_status: object,
    g_working_set: Mapping[str, object] | None,
    index_articles: Sequence[Mapping[str, object]] | None,
) -> dict[str, object]:
    """Build a bounded receipt; unavailable denominators remain explicit.

    Native cursor topic UIDs are the only denominator authority.  A DOM-only
    artifact can still ingest normally, but cannot claim ``zero loss`` for the
    R1-6 campaign because its expected teacher-item set is not independently
    reconstructible.
    """

    gaps: list[str] = []
    expected_ids, cursor_gaps = _expected_teacher_topic_ids(artifact)
    gaps.extend(cursor_gaps)
    coverage_proven = (
        bool(artifact.cursor_pages)
        and not cursor_gaps
        and (artifact.stopped_by_window_boundary or artifact.reached_page_end)
    )
    if not coverage_proven:
        _append_gap(gaps, "zsxq_audit_cursor_coverage_unproven")
    if artifact.final_status != "complete":
        _append_gap(gaps, "zsxq_audit_capture_not_complete")

    rows = _index_by_topic_uid(index_articles, gaps)
    index_proven = not any(gap.startswith("zsxq_audit_index_") for gap in gaps)
    items: list[dict[str, object]] = []
    missing: list[str] = []
    duplicate_identities: list[str] = []
    for topic_uid in expected_ids:
        matching = rows.get(topic_uid, ())
        canonical_identity = f"zsxq-topic:{topic_uid}"
        if len(matching) != 1:
            missing.append(topic_uid)
            if len(matching) > 1:
                duplicate_identities.append(canonical_identity)
            continue
        items.append(_item_projection(topic_uid, matching[0]))

    if missing:
        _append_gap(gaps, "zsxq_audit_expected_teacher_item_missing")
    if duplicate_identities:
        _append_gap(gaps, "zsxq_audit_duplicate_canonical_identity")
    denominator_proven = index_proven and coverage_proven and not missing and not duplicate_identities
    if not bool(artifact.cursor_pages):
        _append_gap(gaps, "zsxq_audit_expected_teacher_denominator_unavailable")

    g = dict(g_working_set) if isinstance(g_working_set, Mapping) else {}
    ingest = str(ingest_status)
    completion = str(completion_status)
    g_ready = bool(g.get("published")) and g.get("status") == "READY"
    chain_ready = (
        artifact.final_status == "complete"
        and ingest in {"succeeded", "no_change"}
        and completion == "ready"
        and g_ready
        and denominator_proven
    )
    denominator_status = "PROVEN" if denominator_proven else "UNKNOWN"
    integrity_status = "PROVEN" if denominator_proven else "UNKNOWN"
    return {
        "schema_version": AUDIT_RECEIPT_SCHEMA_VERSION,
        "chain_id": CHAIN_ID,
        "run_id": artifact.run_id,
        "trading_day": artifact.captured_at.astimezone(TZ).date().isoformat(),
        "captured_at": artifact.captured_at.isoformat(),
        "artifact_content_sha256": artifact.content_sha256,
        "ingest_status": ingest,
        "coverage": {
            "proven": coverage_proven,
            "boundary": (
                "cutoff"
                if artifact.stopped_by_window_boundary
                else "page_end"
                if artifact.reached_page_end
                else "unknown"
            ),
            "cursor_page_count": len(artifact.cursor_pages),
            "window_cutoff": artifact.cutoff.isoformat(),
        },
        "expected_teacher_item_ids": expected_ids,
        "items": items,
        "denominator": {
            "expected_teacher_item_count": len(expected_ids),
            "ingested_teacher_item_count": len(items),
            "missing_expected_topic_uids": missing,
            "duplicate_canonical_identities": duplicate_identities,
            "status": denominator_status,
        },
        "chain": {
            "ready": chain_ready,
            "capture_status": artifact.final_status,
            "completion_status": completion,
            "g_working_set_status": str(g.get("status") or "MISSING"),
            "g_generation": _bounded_text(g.get("generation"), maximum=64),
            "g_source_coverage_sha256": _bounded_text(
                g.get("source_coverage_sha256"), maximum=64
            ),
        },
        "integrity_status": integrity_status,
        "data_gaps": gaps,
    }


def validate_capture_ingest_audit(
    value: object,
    *,
    artifact_run_id: str,
    content_sha256: str,
    ingest_status: str,
    completion_status: str,
    g_working_set: Mapping[str, object] | None,
    g_ready: bool,
) -> dict[str, object]:
    """Validate the bounded receipt before it becomes immutable recovery state."""
    fields = {
        "schema_version",
        "chain_id",
        "run_id",
        "trading_day",
        "captured_at",
        "artifact_content_sha256",
        "ingest_status",
        "coverage",
        "expected_teacher_item_ids",
        "items",
        "denominator",
        "chain",
        "integrity_status",
        "data_gaps",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("capture ingest audit is invalid")
    audit = dict(value)
    coverage = audit["coverage"]
    denominator = audit["denominator"]
    chain = audit["chain"]
    expected_ids = audit["expected_teacher_item_ids"]
    items = audit["items"]
    gaps = audit["data_gaps"]
    if (
        audit["schema_version"] != AUDIT_RECEIPT_SCHEMA_VERSION
        or audit["chain_id"] != CHAIN_ID
        or audit["run_id"] != artifact_run_id
        or audit["artifact_content_sha256"] != content_sha256
        or audit["ingest_status"] != ingest_status
        or audit["integrity_status"] not in {"PROVEN", "UNKNOWN"}
        or not _canonical_aware_iso(audit["captured_at"])
        or not _canonical_date(audit["trading_day"])
        or not isinstance(expected_ids, list)
        or len(expected_ids) > 20_000
        or any(not _bounded_identifier(item) for item in expected_ids)
        or len(expected_ids) != len(set(expected_ids))
        or not isinstance(items, list)
        or len(items) > 20_000
        or not _valid_audit_items(items, expected_ids)
        or not _valid_string_list(gaps, maximum=32)
        or len(gaps) != len(set(gaps))
        or not _valid_audit_coverage(coverage)
        or not _valid_audit_denominator(denominator, expected_ids, items)
        or not _valid_audit_chain(chain, completion_status=completion_status)
        or denominator["status"] != audit["integrity_status"]
    ):
        raise ValueError("capture ingest audit is invalid")
    assert isinstance(coverage, dict)
    assert isinstance(denominator, dict)
    assert isinstance(chain, dict)
    assert isinstance(gaps, list)
    captured_at = datetime.fromisoformat(audit["captured_at"])
    if audit["trading_day"] != captured_at.astimezone(TZ).date().isoformat():
        raise ValueError("capture ingest audit is invalid")
    cursor_page_count = coverage["cursor_page_count"]
    boundary = coverage["boundary"]
    coverage_blocked = any(
        gap in {"zsxq_audit_cursor_page_invalid", "zsxq_audit_cursor_topic_duplicate"}
        for gap in gaps
    )
    coverage_proven = (
        cursor_page_count > 0
        and boundary in {"cutoff", "page_end"}
        and not coverage_blocked
    )
    missing = denominator["missing_expected_topic_uids"]
    duplicates = denominator["duplicate_canonical_identities"]
    index_proven = not any(gap.startswith("zsxq_audit_index_") for gap in gaps)
    denominator_proven = (
        coverage_proven and index_proven and not missing and not duplicates
    )
    denominator_status = "PROVEN" if denominator_proven else "UNKNOWN"
    g = dict(g_working_set) if g_working_set is not None else {}
    expected_chain = {
        "ready": (
            ingest_status in {"succeeded", "no_change"}
            and completion_status == "ready"
            and g_ready
            and denominator_proven
        ),
        "capture_status": "complete",
        "completion_status": completion_status,
        "g_working_set_status": str(g.get("status") or "MISSING"),
        "g_generation": _bounded_text(g.get("generation"), maximum=64),
        "g_source_coverage_sha256": _bounded_text(
            g.get("source_coverage_sha256"), maximum=64
        ),
    }
    gap_set = set(gaps)
    gap_truth = {
        "zsxq_audit_cursor_coverage_unproven": not coverage_proven,
        "zsxq_audit_expected_teacher_denominator_unavailable": cursor_page_count == 0,
        "zsxq_audit_expected_teacher_item_missing": bool(missing),
        "zsxq_audit_duplicate_canonical_identity": bool(duplicates),
        "zsxq_audit_capture_not_complete": False,
    }
    if (
        coverage["proven"] is not coverage_proven
        or denominator["status"] != denominator_status
        or audit["integrity_status"] != denominator_status
        or chain != expected_chain
        or any((name in gap_set) is not expected for name, expected in gap_truth.items())
    ):
        raise ValueError("capture ingest audit is invalid")
    return audit


def _canonical_aware_iso(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None and parsed.isoformat() == value


def _canonical_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _bounded_identifier(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable()


def _valid_string_list(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(_bounded_identifier(item) for item in value)
    )


def _valid_audit_coverage(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "proven",
        "boundary",
        "cursor_page_count",
        "window_cutoff",
    }:
        return False
    return (
        type(value["proven"]) is bool
        and value["boundary"] in {"cutoff", "page_end", "unknown"}
        and type(value["cursor_page_count"]) is int
        and value["cursor_page_count"] >= 0
        and _canonical_aware_iso(value["window_cutoff"])
    )


def _valid_audit_items(items: list[object], expected_ids: list[object]) -> bool:
    fields = {
        "topic_uid",
        "canonical_duplicate_identity",
        "source",
        "source_classification",
        "column",
        "source_family",
        "content_type",
        "source_usage",
        "priority_label",
    }
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != fields:
            return False
        topic_uid = item["topic_uid"]
        if (
            not _bounded_identifier(topic_uid)
            or topic_uid not in expected_ids
            or topic_uid in seen
            or item["canonical_duplicate_identity"] != f"zsxq-topic:{topic_uid}"
            or any(
                value is not None and not isinstance(value, str)
                for key, value in item.items()
                if key not in {"topic_uid", "canonical_duplicate_identity"}
            )
        ):
            return False
        seen.add(topic_uid)
    return True


def _valid_audit_denominator(
    value: object,
    expected_ids: list[object],
    items: list[object],
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "expected_teacher_item_count",
        "ingested_teacher_item_count",
        "missing_expected_topic_uids",
        "duplicate_canonical_identities",
        "status",
    }:
        return False
    missing = value["missing_expected_topic_uids"]
    duplicates = value["duplicate_canonical_identities"]
    item_ids = {item["topic_uid"] for item in items if isinstance(item, dict)}
    return (
        type(value["expected_teacher_item_count"]) is int
        and value["expected_teacher_item_count"] == len(expected_ids)
        and type(value["ingested_teacher_item_count"]) is int
        and value["ingested_teacher_item_count"] == len(items)
        and _valid_string_list(missing, maximum=20_000)
        and set(missing) == set(expected_ids) - item_ids
        and _valid_string_list(duplicates, maximum=20_000)
        and set(duplicates).issubset({f"zsxq-topic:{item}" for item in missing})
        and value["status"] in {"PROVEN", "UNKNOWN"}
    )


def _valid_audit_chain(value: object, *, completion_status: str) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "ready",
        "capture_status",
        "completion_status",
        "g_working_set_status",
        "g_generation",
        "g_source_coverage_sha256",
    }:
        return False
    return (
        type(value["ready"]) is bool
        and isinstance(value["capture_status"], str)
        and value["completion_status"] == completion_status
        and isinstance(value["g_working_set_status"], str)
        and isinstance(value["g_generation"], str)
        and len(value["g_generation"]) <= 64
        and isinstance(value["g_source_coverage_sha256"], str)
        and len(value["g_source_coverage_sha256"]) <= 64
    )


def assess_three_trading_day_campaign(
    receipts: Sequence[Mapping[str, object]],
    *,
    expected_trading_days: Sequence[date],
) -> dict[str, object]:
    """Evaluate three owner-bound audit receipts against an explicit calendar.

    ``expected_trading_days`` must come from the frozen trading-calendar owner.
    Keeping it explicit prevents this operational evidence layer from guessing
    Chinese market holidays or silently treating three weekdays as sufficient.
    """

    expected = tuple(expected_trading_days)
    gaps: list[str] = []
    if (
        len(expected) != 3
        or len(set(expected)) != 3
        or tuple(sorted(expected)) != expected
    ):
        return {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "status": "UNKNOWN",
            "expected_trading_days": [day.isoformat() for day in expected],
            "accepted_run_ids": [],
            "data_gaps": ["zsxq_campaign_trading_calendar_invalid"],
        }

    expected_days = {day.isoformat() for day in expected}
    seen_days: set[str] = set()
    seen_runs: set[str] = set()
    seen_artifacts: set[str] = set()
    accepted: dict[str, str] = {}
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            _append_gap(gaps, "zsxq_campaign_receipt_invalid")
            continue
        day = _bounded_text(receipt.get("trading_day"), maximum=10)
        run_id = _bounded_text(receipt.get("run_id"), maximum=200)
        artifact_hash = _bounded_text(receipt.get("artifact_content_sha256"), maximum=64)
        if day in seen_days:
            _append_gap(gaps, "zsxq_campaign_duplicate_trading_day")
            continue
        seen_days.add(day)
        if day not in expected_days:
            _append_gap(gaps, "zsxq_campaign_unexpected_trading_day")
            continue
        if not run_id or run_id in seen_runs:
            _append_gap(gaps, "zsxq_campaign_duplicate_run")
            continue
        seen_runs.add(run_id)
        if not artifact_hash or artifact_hash in seen_artifacts:
            _append_gap(gaps, "zsxq_campaign_duplicate_artifact")
            continue
        seen_artifacts.add(artifact_hash)
        if not _receipt_is_campaign_ready(receipt):
            _append_gap(gaps, "zsxq_campaign_chain_not_ready")
            continue
        accepted[day] = run_id

    missing_days = [day.isoformat() for day in expected if day.isoformat() not in accepted]
    if missing_days:
        _append_gap(gaps, "zsxq_campaign_trading_day_missing")
    status = "COMPLETE" if not gaps and not missing_days else "PARTIAL"
    return {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "status": status,
        "expected_trading_days": [day.isoformat() for day in expected],
        "accepted_run_ids": [accepted[day.isoformat()] for day in expected if day.isoformat() in accepted],
        "data_gaps": gaps,
    }


def _expected_teacher_topic_ids(artifact: CaptureArtifact) -> tuple[list[str], list[str]]:
    """Return exact native teacher UIDs inside the artifact observation window.

    跨页 topic_id 重叠是 ZSXQ 时间窗翻页的边界重叠（相邻两页共享边界帖；
    2026-09-04 实测：2 页 60 条恰好 1 个跨页重复，旧判定因此让 20/20 轮
    全部 chain_ready=false）——按集合语义去重即可。单页内重复由
    ``_decode_topic_cursor_page`` 拒绝（cursor_page_invalid），不在此重复设防。
    """

    if not artifact.cursor_pages:
        return [], ["zsxq_audit_expected_teacher_denominator_unavailable"]
    gaps: list[str] = []
    expected: list[str] = []
    seen: set[str] = set()
    for cursor_page in artifact.cursor_pages:
        decoded = _decode_topic_cursor_page(cursor_page.output)
        if decoded is None or decoded.http_status != 200 or not decoded.api_succeeded:
            _append_gap(gaps, "zsxq_audit_cursor_page_invalid")
            continue
        for topic in decoded.topics:
            if topic.created_at > artifact.captured_at or topic.created_at < artifact.cutoff:
                continue
            if not topic.is_teacher_source:
                continue
            if topic.topic_id in seen:
                continue
            seen.add(topic.topic_id)
            expected.append(topic.topic_id)
    if gaps:
        return [], gaps
    return sorted(expected), []


def _index_by_topic_uid(
    index_articles: Sequence[Mapping[str, object]] | None,
    gaps: list[str],
) -> dict[str, tuple[Mapping[str, object], ...]]:
    if index_articles is None:
        _append_gap(gaps, "zsxq_audit_index_unavailable")
        return {}
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for entry in index_articles:
        if not isinstance(entry, Mapping):
            _append_gap(gaps, "zsxq_audit_index_invalid")
            continue
        topic_uid = _topic_uid(entry.get("topic_id"))
        if topic_uid is None:
            continue
        grouped.setdefault(topic_uid, []).append(entry)
    return {topic_uid: tuple(entries) for topic_uid, entries in grouped.items()}


def _item_projection(topic_uid: str, entry: Mapping[str, object]) -> dict[str, object]:
    column = _bounded_text(entry.get("column"), maximum=80)
    source_classification = _bounded_text(entry.get("source_classification"), maximum=80)
    is_qa = entry.get("is_qa") is True
    decision = classify_g_source(
        column,
        teacher_original=source_classification == "teacher_original",
        is_qa=is_qa,
        priority_label=entry.get("priority_label"),
    )
    source = decision.classification
    return {
        "topic_uid": topic_uid,
        "canonical_duplicate_identity": f"zsxq-topic:{topic_uid}",
        "source": (
            _bounded_text(entry.get("source_family"), maximum=80)
            or source_classification
            or None
        ),
        "source_classification": source_classification or None,
        "column": column,
        "source_family": source.source_family if source else None,
        "content_type": source.content_type if source else None,
        "source_usage": source.usage if source else None,
        "priority_label": source.priority_label if source else None,
    }


def _receipt_is_campaign_ready(receipt: Mapping[str, object]) -> bool:
    denominator = receipt.get("denominator")
    coverage = receipt.get("coverage")
    chain = receipt.get("chain")
    if not all(isinstance(value, Mapping) for value in (denominator, coverage, chain)):
        return False
    assert isinstance(chain, Mapping)
    artifact_run_id = _bounded_text(receipt.get("run_id"), maximum=200)
    content_sha256 = _bounded_text(receipt.get("artifact_content_sha256"), maximum=64)
    ingest_status = _bounded_text(receipt.get("ingest_status"), maximum=32)
    completion_status = _bounded_text(chain.get("completion_status"), maximum=32)
    generation = _bounded_text(chain.get("g_generation"), maximum=64)
    coverage_sha256 = _bounded_text(
        chain.get("g_source_coverage_sha256"),
        maximum=64,
    )
    g_status = _bounded_text(chain.get("g_working_set_status"), maximum=32)
    if (
        not artifact_run_id
        or not _sha256_text(content_sha256)
        or not _sha256_text(generation)
        or not _sha256_text(coverage_sha256)
        or g_status != "READY"
    ):
        return False
    try:
        validated = validate_capture_ingest_audit(
            dict(receipt),
            artifact_run_id=artifact_run_id,
            content_sha256=content_sha256,
            ingest_status=ingest_status,
            completion_status=completion_status,
            g_working_set={
                "status": g_status,
                "generation": generation,
                "source_coverage_sha256": coverage_sha256,
            },
            g_ready=True,
        )
    except ValueError:
        return False
    validated_coverage = validated["coverage"]
    validated_denominator = validated["denominator"]
    validated_chain = validated["chain"]
    if (
        not isinstance(validated_coverage, Mapping)
        or not isinstance(validated_denominator, Mapping)
        or not isinstance(validated_chain, Mapping)
    ):
        return False
    return (
        validated["integrity_status"] == "PROVEN"
        and validated_coverage["proven"] is True
        and validated_denominator["status"] == "PROVEN"
        and validated_chain["ready"] is True
    )


def _sha256_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _topic_uid(value: object) -> str | None:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    return value


def _bounded_text(value: object, *, maximum: int) -> str:
    return value if isinstance(value, str) and 0 < len(value) <= maximum else ""


def _append_gap(gaps: list[str], gap: str) -> None:
    if gap not in gaps:
        gaps.append(gap)
