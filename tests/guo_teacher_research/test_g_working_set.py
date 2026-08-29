from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.g_working_set import (
    GWorkingSetAssessment,
    GWorkingSetPublicationDisposition,
    GWorkingSetPublicationPlan,
    GWorkingSetService,
    GWorkingSetStatus,
    select_active_g_working_set,
)
from fin_analyse.guo_teacher_research.runtime_context import (
    AgentRuntimeContextProvider,
    AgentRuntimeContextRequest,
)

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 23, 10, 0, tzinfo=CST)


@dataclass(frozen=True)
class _Pair:
    generation_id: str = "generation-1"
    generated_at: str = "2026-07-23T09:00:00+08:00"
    content_hash: str = "a" * 64
    compact_raw_sha256: str = "b" * 64


class _DeepReadReader:
    def __init__(self, available: set[str]) -> None:
        self.available = available

    def load_fresh_pair(self, article_id: str, article_path: str | Path) -> _Pair | None:
        del article_path
        return _Pair() if article_id in self.available else None


class _OneShotDeepReadReader:
    def __init__(self) -> None:
        self.calls = 0

    def load_fresh_pair(self, article_id: str, article_path: str | Path) -> _Pair | None:
        del article_id, article_path
        self.calls += 1
        return _Pair() if self.calls == 1 else None


class _ThirdReadDriftReader:
    def __init__(self) -> None:
        self.calls = 0

    def load_fresh_pair(self, article_id: str, article_path: str | Path) -> _Pair | None:
        del article_id, article_path
        self.calls += 1
        return _Pair() if self.calls < 3 else None


def _publish_plan_worker(kb_root: str, plan_payload: dict[str, str], queue) -> None:
    service = GWorkingSetService(
        kb_root=Path(kb_root),
        deep_read_reader=_DeepReadReader({"g-current"}),
    )
    result = service.compare_and_publish(GWorkingSetPublicationPlan.from_dict(plan_payload))
    queue.put(result.disposition.value)


class _ExtractionFailureReader:
    """Models DeepReadArtifactService withholding a failed extraction pair."""

    def load_fresh_pair(self, article_id: str, article_path: str | Path) -> None:
        del article_id, article_path
        return None


def _write_index(
    kb_root: Path,
    *,
    article_id: str = "g-current",
    column: str = "星大派锐评",
    published_at: str = "2026-07-23 09:00",
) -> bytes:
    article_name = f"20260723_{article_id}.md"
    article = kb_root / "articles" / article_name
    article.parent.mkdir(parents=True, exist_ok=True)
    article.write_text(f"---\nid: {article_id}\n---\n\n老师原文\n", encoding="utf-8")
    payload = {
        "articles": [
            {
                "id": article_id,
                "date": published_at,
                "column": column,
                "title": "当前 G 主线",
                "file": article_name,
                "path": str(article),
            }
        ],
        "updated": "2026-07-23T09:30:00+08:00",
        "total": 1,
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    (kb_root / "index.json").write_bytes(raw)
    return raw


def _write_events(
    kb_root: Path,
    *,
    article_id: str = "g-current",
    column: str = "星大派锐评",
) -> bytes:
    path = kb_root / "runtime" / "cognition" / "priority_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "event_id": f"pa:{article_id}",
        "article_id": article_id,
        "title": "当前 G 主线",
        "priority_tier": "T0",
        "push_policy": "always_push",
        "source_classification": "teacher_original",
        "persona_eligible": True,
        "requires_deep_read": True,
        "created_at": "2026-07-23T09:31:00+08:00",
        "metadata": {"column": column},
    }
    raw = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    return raw


def _ready_service(tmp_path: Path) -> tuple[GWorkingSetService, Path]:
    kb_root = tmp_path / "knowledge-base"
    _write_index(kb_root)
    _write_events(kb_root)
    service = GWorkingSetService(
        kb_root=kb_root,
        deep_read_reader=_DeepReadReader({"g-current"}),
    )
    return service, kb_root


def test_reconcile_publish_is_canonical_owner_only_and_deterministic(tmp_path: Path) -> None:
    service, _ = _ready_service(tmp_path)

    first = service.reconcile(now=NOW)
    second = service.reconcile(now=NOW)

    assert first.status is GWorkingSetStatus.READY
    assert first.canonical_sha256 == second.canonical_sha256
    assert first.manifest == second.manifest
    later = service.reconcile(now=NOW + timedelta(minutes=5))
    assert later.evaluated_at != first.evaluated_at
    assert later.canonical_sha256 == first.canonical_sha256

    published = service.reconcile_and_publish(now=NOW)
    assert published.status is GWorkingSetStatus.READY
    assert service.manifest_path.stat().st_mode & 0o777 == 0o600
    assert service.manifest_path.parent.stat().st_mode & 0o777 == 0o700
    on_disk = json.loads(service.manifest_path.read_text(encoding="utf-8"))
    assert on_disk["source_boundary"] == "operational_evidence_not_teacher_cognition"
    assert on_disk["canonical_sha256"] == first.canonical_sha256


def test_expired_unconfirmed_qa_does_not_block_the_active_g_set() -> None:
    entry = {
        "id": "g-question",
        "date": "2026-07-02 09:00",
        "column": "星大派好问题",
        "title": "历史问题",
    }

    # BUG-006③：特刊窗口 20→30 天后，"过期"对照组须真正落到 30 天外
    # （2026-07-02 距 NOW=07-23 仅 21 天，已在新窗口内，会如实产出
    # QA 未确认 gap 而非静默过期）。
    expired = select_active_g_working_set(
        index_articles=[{**entry, "date": "2026-06-01 09:00"}],
        priority_events=[],
        now=NOW,
    )
    fresh = select_active_g_working_set(
        index_articles=[{**entry, "date": "2026-07-23 09:00"}],
        priority_events=[],
        now=NOW,
    )
    unrelated = select_active_g_working_set(
        index_articles=[
            # 非 G 栏目（分类None，含 owner 撤项后的普通栏）静默跳过、零 gap。
            {**entry, "id": "non-g-invalid", "column": "版本强势英雄", "date": "invalid"},
            {**entry, "id": "ordinary-invalid", "column": "普通", "date": "invalid"},
            {**entry, "id": "ordinary-future", "column": "普通", "date": "2026-07-24"},
        ],
        priority_events=[],
        now=NOW,
    )

    assert expired is not None
    assert expired.candidates == ()
    assert expired.data_gaps == ()
    assert fresh is not None
    assert fresh.data_gaps == ("g_source_question_answer_unconfirmed",)
    assert unrelated is not None
    assert unrelated.data_gaps == ()


def test_publication_plan_exact_retry_is_zero_write(tmp_path: Path) -> None:
    service, _ = _ready_service(tmp_path)
    plan = service.prepare_publication(publication_at=NOW)

    first = service.compare_and_publish(plan)
    raw_before = service.manifest_path.read_bytes()
    stat_before = service.manifest_path.stat()
    second = service.compare_and_publish(plan)
    stat_after = service.manifest_path.stat()

    assert first.disposition is GWorkingSetPublicationDisposition.PUBLISHED
    assert second.disposition is GWorkingSetPublicationDisposition.ALREADY_PUBLISHED
    assert service.manifest_path.read_bytes() == raw_before
    assert (stat_after.st_ino, stat_after.st_mtime_ns) == (
        stat_before.st_ino,
        stat_before.st_mtime_ns,
    )


def test_same_publication_plan_is_single_writer_across_processes(tmp_path: Path) -> None:
    service, kb_root = _ready_service(tmp_path)
    plan = service.prepare_publication(publication_at=NOW)
    context = mp.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_publish_plan_worker,
            args=(str(kb_root), plan.to_dict(), queue),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    results = sorted(queue.get(timeout=20) for _ in processes)
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert results == ["ALREADY_PUBLISHED", "PUBLISHED"]


def test_publication_plan_from_dict_rejects_string_coercion(tmp_path: Path) -> None:
    service, _ = _ready_service(tmp_path)
    payload: dict[str, object] = service.prepare_publication(publication_at=NOW).to_dict()
    payload["prior_manifest_identity"] = Path("a" * 64)

    with pytest.raises(ValueError, match="publication plan is invalid"):
        GWorkingSetPublicationPlan.from_dict(payload)


def test_publication_plan_rejects_source_drift_without_writing(tmp_path: Path) -> None:
    service, kb_root = _ready_service(tmp_path)
    plan = service.prepare_publication(publication_at=NOW)
    index = json.loads((kb_root / "index.json").read_text(encoding="utf-8"))
    index["updated"] = "2026-07-23T09:45:00+08:00"
    (kb_root / "index.json").write_text(json.dumps(index), encoding="utf-8")

    result = service.compare_and_publish(plan)

    assert result.disposition is GWorkingSetPublicationDisposition.REJECTED
    assert result.reason == "SOURCE_DRIFT"
    assert not service.manifest_path.exists()


def test_publication_plan_rejects_owner_root_drift_without_writing(tmp_path: Path) -> None:
    first, _ = _ready_service(tmp_path / "first")
    second, _ = _ready_service(tmp_path / "second")
    plan = first.prepare_publication(publication_at=NOW)

    result = second.compare_and_publish(plan)

    assert result.disposition is GWorkingSetPublicationDisposition.REJECTED
    assert result.reason == "OWNER_DRIFT"
    assert not second.manifest_path.exists()


def test_older_plan_cannot_overwrite_same_generation_newer_manifest(tmp_path: Path) -> None:
    service, _ = _ready_service(tmp_path)
    older = service.prepare_publication(publication_at=NOW)
    newer = service.prepare_publication(publication_at=NOW + timedelta(minutes=5))
    assert older.expected_generation == newer.expected_generation

    published = service.compare_and_publish(newer)
    newer_raw = service.manifest_path.read_bytes()
    replayed = service.compare_and_publish(older)

    assert published.disposition is GWorkingSetPublicationDisposition.PUBLISHED
    assert replayed.disposition is GWorkingSetPublicationDisposition.REJECTED
    assert replayed.reason == "NEWER_MANIFEST"
    assert service.manifest_path.read_bytes() == newer_raw


def test_plan_prepared_after_newer_manifest_cannot_backdate_it(tmp_path: Path) -> None:
    service, _ = _ready_service(tmp_path)
    service.reconcile_and_publish(now=NOW + timedelta(minutes=5))
    newer_raw = service.manifest_path.read_bytes()
    newer_stat = service.manifest_path.stat()
    backdated = service.prepare_publication(publication_at=NOW)

    result = service.compare_and_publish(backdated)

    assert result.disposition is GWorkingSetPublicationDisposition.REJECTED
    assert result.reason == "NEWER_MANIFEST"
    assert service.manifest_path.read_bytes() == newer_raw
    after = service.manifest_path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (
        newer_stat.st_ino,
        newer_stat.st_mtime_ns,
    )


def test_publication_fails_if_manifest_directory_identity_drifts(
    tmp_path: Path, monkeypatch
) -> None:
    service, _ = _ready_service(tmp_path)
    plan = service.prepare_publication(publication_at=NOW)
    publish = service._publish_raw
    moved = service.manifest_path.parent.with_name("g_working_set.moved")

    def drift_then_publish(directory_fd: int, raw: bytes) -> None:
        service.manifest_path.parent.rename(moved)
        service.manifest_path.parent.mkdir(mode=0o700)
        publish(directory_fd, raw)

    monkeypatch.setattr(service, "_publish_raw", drift_then_publish)

    with pytest.raises(ValueError, match="directory identity drifted"):
        service.compare_and_publish(plan)

    assert not service.manifest_path.exists()


def test_evaluate_rejects_self_consistent_ready_manifest_without_source_coverage(
    tmp_path: Path,
) -> None:
    service, _ = _ready_service(tmp_path)
    service.reconcile_and_publish(now=NOW)
    forged = json.loads(service.manifest_path.read_text(encoding="utf-8"))
    forged["articles"] = []
    forged["sources"]["knowledge_index"]["active_article_count"] = 0
    forged["sources"]["deep_read"] = {
        "required_count": 0,
        "available_count": 0,
    }
    generation_projection = dict(forged)
    generation_projection.pop("canonical_sha256")
    generation_projection.pop("evaluated_at")
    forged["canonical_sha256"] = hashlib.sha256(
        json.dumps(
            generation_projection,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    service.manifest_path.write_text(
        json.dumps(
            forged,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = service.evaluate(now=NOW)

    assert result.status is GWorkingSetStatus.MISSING
    assert result.data_gaps == ("g_working_set_manifest_invalid",)


def test_evaluate_rejects_self_consistent_ready_manifest_with_duplicate_source_refs(
    tmp_path: Path,
) -> None:
    service, _ = _ready_service(tmp_path)
    service.reconcile_and_publish(now=NOW)
    forged = json.loads(service.manifest_path.read_text(encoding="utf-8"))
    forged["articles"] = [forged["articles"][0], dict(forged["articles"][0])]
    forged["sources"]["knowledge_index"]["active_article_count"] = 2
    forged["sources"]["deep_read"] = {
        "required_count": 2,
        "available_count": 2,
    }
    generation_projection = dict(forged)
    generation_projection.pop("canonical_sha256")
    generation_projection.pop("evaluated_at")
    forged["canonical_sha256"] = hashlib.sha256(
        json.dumps(
            generation_projection,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    service.manifest_path.write_text(
        json.dumps(
            forged,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = service.evaluate(now=NOW)

    assert result.status is GWorkingSetStatus.MISSING
    assert result.data_gaps == ("g_working_set_manifest_invalid",)


def test_evaluate_rejects_ready_manifest_without_teacher_original_event_binding(
    tmp_path: Path,
) -> None:
    service, _ = _ready_service(tmp_path)
    service.reconcile_and_publish(now=NOW)
    forged = json.loads(service.manifest_path.read_text(encoding="utf-8"))
    forged["articles"][0]["priority_event_id"] = None
    forged["articles"][0]["priority_event_sha256"] = None
    generation_projection = dict(forged)
    generation_projection.pop("canonical_sha256")
    generation_projection.pop("evaluated_at")
    forged["canonical_sha256"] = hashlib.sha256(
        json.dumps(
            generation_projection,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    service.manifest_path.write_text(
        json.dumps(
            forged,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = service.evaluate(now=NOW)

    assert result.status is GWorkingSetStatus.MISSING
    assert result.data_gaps == ("g_working_set_manifest_invalid",)


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("index_entry_sha256", "a" * 64),
        ("priority_event_id", "pa:forged-event"),
        ("priority_event_sha256", "b" * 64),
    ],
)
def test_evaluate_rebinds_ready_manifest_articles_to_owner_sources(
    field: str,
    forged_value: str,
    tmp_path: Path,
) -> None:
    service, _ = _ready_service(tmp_path)
    service.reconcile_and_publish(now=NOW)
    forged = json.loads(service.manifest_path.read_text(encoding="utf-8"))
    forged["articles"][0][field] = forged_value
    generation_projection = dict(forged)
    generation_projection.pop("canonical_sha256")
    generation_projection.pop("evaluated_at")
    forged["canonical_sha256"] = hashlib.sha256(
        json.dumps(
            generation_projection,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    service.manifest_path.write_text(
        json.dumps(
            forged,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = service.evaluate(now=NOW + timedelta(minutes=1))

    assert result.status is GWorkingSetStatus.MISSING
    assert result.data_gaps == ("g_working_set_manifest_invalid",)
    with pytest.raises(ValueError, match="publication evidence is invalid"):
        result.to_publication_evidence()


def test_publication_evidence_rejects_assessment_gap_drift(tmp_path: Path) -> None:
    service, _ = _ready_service(tmp_path)
    ready = service.reconcile(now=NOW)
    forged = replace(ready, data_gaps=("caller_claimed_gap",))

    with pytest.raises(ValueError, match="publication evidence is invalid"):
        forged.to_publication_evidence()


def test_evaluate_reports_missing_stale_and_changed_sources(tmp_path: Path) -> None:
    service, kb_root = _ready_service(tmp_path)
    missing = service.evaluate(now=NOW)
    assert missing.status is GWorkingSetStatus.MISSING
    assert missing.data_gaps == ("g_working_set_manifest_missing",)

    service.reconcile_and_publish(now=NOW)
    stale = service.evaluate(now=NOW + timedelta(hours=25))
    assert stale.status is GWorkingSetStatus.STALE
    assert "g_working_set_manifest_stale" in stale.data_gaps

    index = json.loads((kb_root / "index.json").read_text(encoding="utf-8"))
    index["updated"] = "2026-07-23T10:01:00+08:00"
    (kb_root / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    changed = service.evaluate(now=NOW + timedelta(minutes=5))
    assert changed.status is GWorkingSetStatus.STALE
    assert "g_working_set_sources_changed" in changed.data_gaps


def test_reconcile_is_partial_for_missing_event_and_deep_read(tmp_path: Path) -> None:
    kb_root = tmp_path / "knowledge-base"
    _write_index(kb_root)
    service = GWorkingSetService(
        kb_root=kb_root,
        deep_read_reader=_DeepReadReader(set()),
    )

    result = service.reconcile(now=NOW)

    assert result.status is GWorkingSetStatus.PARTIAL
    assert result.data_gaps == (
        "g_working_set_priority_events_missing",
        "g_working_set_priority_event_coverage_partial",
        "g_working_set_deep_read_coverage_partial",
    )
    [article] = result.manifest["articles"]
    assert article["priority_event_id"] is None
    assert article["deep_read"]["available"] is False


def test_reconcile_does_not_mark_extraction_failure_ready(tmp_path: Path) -> None:
    kb_root = tmp_path / "knowledge-base"
    _write_index(kb_root)
    _write_events(kb_root)
    service = GWorkingSetService(
        kb_root=kb_root,
        deep_read_reader=_ExtractionFailureReader(),
    )

    result = service.reconcile(now=NOW)

    assert result.status is GWorkingSetStatus.PARTIAL
    assert "g_working_set_deep_read_coverage_partial" in result.data_gaps
    assert result.manifest["articles"][0]["deep_read"]["available"] is False


def test_reconcile_reports_ambiguous_star_label_without_promoting_it_to_g(tmp_path: Path) -> None:
    kb_root = tmp_path / "knowledge-base"
    _write_index(kb_root, column="星大派")
    _write_events(kb_root, column="星大派")
    service = GWorkingSetService(
        kb_root=kb_root,
        deep_read_reader=_DeepReadReader({"g-current"}),
    )

    result = service.reconcile(now=NOW)

    assert result.status is GWorkingSetStatus.PARTIAL
    assert "g_source_type_ambiguous" in result.data_gaps
    assert result.manifest["articles"] == []


def test_reconcile_preserves_exact_fengxianjun_source_axes_in_manifest(tmp_path: Path) -> None:
    kb_root = tmp_path / "knowledge-base"
    _write_index(kb_root, column="凤仙郡小故事")
    _write_events(kb_root, column="凤仙郡小故事")
    service = GWorkingSetService(
        kb_root=kb_root,
        deep_read_reader=_DeepReadReader({"g-current"}),
    )

    result = service.reconcile(now=NOW)

    assert result.status is GWorkingSetStatus.READY
    [article] = result.manifest["articles"]
    assert article["source_family"] == "凤仙郡小故事"
    assert article["content_type"] == "长期故事"
    assert article["source_usage"] == "long_term_framework"
    assert article["priority_label"] is None


def test_reconcile_ignores_unbound_event_without_mutating_cognition(tmp_path: Path) -> None:
    kb_root = tmp_path / "knowledge-base"
    index_before = _write_index(kb_root)
    events_before = _write_events(kb_root, article_id="unknown-g")
    service = GWorkingSetService(
        kb_root=kb_root,
        deep_read_reader=_DeepReadReader(set()),
    )

    first = service.reconcile(now=NOW)
    second = service.reconcile(now=NOW)

    assert first.status is GWorkingSetStatus.PARTIAL
    assert "g_working_set_priority_event_contract_mismatch" not in first.data_gaps
    assert "g_working_set_priority_event_coverage_partial" in first.data_gaps
    assert first.canonical_sha256 == second.canonical_sha256
    assert (kb_root / "index.json").read_bytes() == index_before
    assert (
        kb_root / "runtime" / "cognition" / "priority_events.jsonl"
    ).read_bytes() == events_before
    assert not (kb_root / "runtime" / "cognition" / "persona.json").exists()


def test_historical_articles_outside_active_window_do_not_hold_ready_open(
    tmp_path: Path,
) -> None:
    service, kb_root = _ready_service(tmp_path)
    index = json.loads((kb_root / "index.json").read_text(encoding="utf-8"))
    old_name = "20260501_old-g.md"
    (kb_root / "articles" / old_name).write_text("历史文章", encoding="utf-8")
    index["articles"].append(
        {
            "id": "old-g",
            "date": "2026-05-01 09:00",
            "column": "星大派特刊",
            "title": "历史 G",
            "file": old_name,
            "path": str(kb_root / "articles" / old_name),
        }
    )
    index["total"] = 2
    (kb_root / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    events_path = kb_root / "runtime" / "cognition" / "priority_events.jsonl"
    old_event = {
        "event_id": "pa:old-g",
        "article_id": "old-g",
        "title": "历史 G",
        "source_classification": "teacher_original",
        "requires_deep_read": True,
        "created_at": "2026-07-23T09:40:00+08:00",
        "metadata": {"column": "星大派特刊"},
    }
    with events_path.open("ab") as handle:
        encoded = (json.dumps(old_event, ensure_ascii=False) + "\n").encode()
        handle.write(encoded)
        handle.write(encoded)
        handle.write(b"{historical-malformed-event\n")

    result = service.reconcile(now=NOW)

    assert result.status is GWorkingSetStatus.READY
    assert [item["article_id"] for item in result.manifest["articles"]] == ["g-current"]


def test_publish_rejects_deep_read_drift_before_replacing_manifest(tmp_path: Path) -> None:
    kb_root = tmp_path / "knowledge-base"
    _write_index(kb_root)
    _write_events(kb_root)
    reader = _OneShotDeepReadReader()
    service = GWorkingSetService(kb_root=kb_root, deep_read_reader=reader)

    with pytest.raises(RuntimeError, match="SOURCE_DRIFT"):
        service.reconcile_and_publish(now=NOW)

    assert reader.calls == 2
    assert not service.manifest_path.exists()


def test_publish_rejects_drift_in_the_final_reconcile(tmp_path: Path) -> None:
    kb_root = tmp_path / "knowledge-base"
    _write_index(kb_root)
    _write_events(kb_root)
    reader = _ThirdReadDriftReader()
    service = GWorkingSetService(kb_root=kb_root, deep_read_reader=reader)
    plan = service.prepare_publication(publication_at=NOW)

    result = service.compare_and_publish(plan)

    assert result.disposition is GWorkingSetPublicationDisposition.REJECTED
    assert result.reason == "SOURCE_DRIFT"
    assert reader.calls == 3
    assert not service.manifest_path.exists()


def test_constructor_rejects_parent_traversal_but_delays_root_io(tmp_path: Path) -> None:
    kb_root = tmp_path / "knowledge-base"
    kb_root.mkdir()
    alias = tmp_path / "knowledge-base-alias"
    alias.symlink_to(kb_root, target_is_directory=True)

    with pytest.raises(ValueError, match="parent traversal"):
        GWorkingSetService(
            kb_root=kb_root / ".." / "knowledge-base",
            deep_read_reader=_DeepReadReader(set()),
        )
    service = GWorkingSetService(kb_root=alias, deep_read_reader=_DeepReadReader(set()))

    result = service.evaluate(now=NOW)

    assert result.status is GWorkingSetStatus.MISSING
    assert result.data_gaps == ("g_working_set_manifest_invalid",)
    with pytest.raises((OSError, ValueError)):
        service.reconcile_and_publish(now=NOW)


def test_runtime_context_rejects_release_style_runtime_symlink(tmp_path: Path) -> None:
    release_root = tmp_path / "release" / "knowledge-base"
    shared_root = tmp_path / "shared" / "knowledge-base"
    _write_index(release_root)
    _write_events(shared_root)
    (release_root / "runtime").symlink_to(
        shared_root / "runtime",
        target_is_directory=True,
    )

    result = AgentRuntimeContextProvider(
        kb_root=release_root,
        pinned_sources=(),
        knowledge_documents=[],
    ).resolve(
        AgentRuntimeContextRequest(
            agent_id="guo_teacher",
            question="最近 G 主线有什么变化",
            now=NOW.isoformat(),
        )
    )

    assert result.llm_context["g_context"] == []
    assert "g_working_set_manifest_invalid" in result.data_gaps
    assert "fresh_g_context_cache_empty" in result.data_gaps


def test_missing_root_evaluates_typed_missing_and_publish_does_not_create_it(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "missing-knowledge-base"
    service = GWorkingSetService(
        kb_root=missing_root,
        deep_read_reader=_DeepReadReader(set()),
    )

    result = service.evaluate(now=NOW)

    assert result.status is GWorkingSetStatus.MISSING
    assert result.data_gaps == ("g_working_set_manifest_missing",)
    with pytest.raises(FileNotFoundError):
        service.reconcile_and_publish(now=NOW)
    assert not missing_root.exists()


def test_default_runtime_missing_kb_root_degrades_without_creating_it(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()

    result = AgentRuntimeContextProvider(
        kb_root=project_root / "knowledge-base",
        pinned_sources=(),
        knowledge_documents=[],
    ).resolve(
        AgentRuntimeContextRequest(
            agent_id="guo_teacher",
            question="最近 G 主线有什么变化",
            now=NOW.isoformat(),
        )
    )

    assert result.available is False
    assert "g_working_set_manifest_missing" in result.data_gaps
    assert "fresh_g_knowledge_index_unavailable" in result.data_gaps
    assert result.audit_context["fresh_g"]["working_set_freshness"]["status"] == "MISSING"
    assert not (project_root / "knowledge-base").exists()


def test_publish_rejects_symlink_manifest_directory(tmp_path: Path) -> None:
    service, kb_root = _ready_service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (kb_root / "runtime" / "operations").symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        service.reconcile_and_publish(now=NOW)

    assert list(outside.iterdir()) == []


def test_publish_rejects_non_owner_only_manifest_directory(tmp_path: Path) -> None:
    service, kb_root = _ready_service(tmp_path)
    operations = kb_root / "runtime" / "operations"
    operations.mkdir(mode=0o700)
    operations.chmod(0o755)

    with pytest.raises(ValueError, match="directory boundary"):
        service.reconcile_and_publish(now=NOW)

    assert not service.manifest_path.exists()


@pytest.mark.parametrize("target_kind", ["symlink", "hardlink"])
def test_publish_rejects_linked_existing_target_without_touching_victim(
    tmp_path: Path,
    target_kind: str,
) -> None:
    service, _ = _ready_service(tmp_path)
    service.reconcile_and_publish(now=NOW)
    service.manifest_path.unlink()
    victim = tmp_path / "victim.json"
    victim.write_bytes(b"outside-victim")
    victim.chmod(0o600)
    if target_kind == "symlink":
        service.manifest_path.symlink_to(victim)
    else:
        os.link(victim, service.manifest_path)

    with pytest.raises(ValueError, match="manifest target"):
        service.reconcile_and_publish(now=NOW)

    assert victim.read_bytes() == b"outside-victim"


def test_publish_rejects_existing_target_with_wrong_mode(tmp_path: Path) -> None:
    service, _ = _ready_service(tmp_path)
    service.reconcile_and_publish(now=NOW)
    original = service.manifest_path.read_bytes()
    service.manifest_path.chmod(0o644)

    with pytest.raises(ValueError, match="manifest target"):
        service.reconcile_and_publish(now=NOW)

    assert service.manifest_path.read_bytes() == original
    assert stat.S_IMODE(service.manifest_path.stat().st_mode) == 0o644


def test_reader_rejects_non_owner_only_manifest(tmp_path: Path) -> None:
    service, _ = _ready_service(tmp_path)
    service.reconcile_and_publish(now=NOW)
    service.manifest_path.chmod(0o644)

    result = service.evaluate(now=NOW)

    assert result.status is GWorkingSetStatus.MISSING
    assert result.data_gaps == ("g_working_set_manifest_invalid",)
    assert stat.S_IMODE(service.manifest_path.stat().st_mode) == 0o644


def test_evaluate_marks_changed_deep_read_stale(tmp_path: Path) -> None:
    kb_root = tmp_path / "knowledge-base"
    _write_index(kb_root)
    _write_events(kb_root)
    reader = _DeepReadReader({"g-current"})
    service = GWorkingSetService(kb_root=kb_root, deep_read_reader=reader)
    service.reconcile_and_publish(now=NOW)
    reader.available.clear()

    result = service.evaluate(now=NOW + timedelta(minutes=1))

    assert result.status is GWorkingSetStatus.STALE
    assert "g_working_set_deep_read_changed" in result.data_gaps


def test_assessment_projection_is_bounded() -> None:
    result = GWorkingSetAssessment.missing("g_working_set_manifest_missing")

    assert result.to_runtime_context() == {
        "status": "MISSING",
        "canonical_sha256": "",
        "evaluated_at": "",
        "bound_article_ids": [],
        "data_gaps": ["g_working_set_manifest_missing"],
    }


class _PartialWorkingSetReader:
    def evaluate(self, *, now: datetime | None = None) -> GWorkingSetAssessment:
        del now
        return GWorkingSetAssessment(
            status=GWorkingSetStatus.PARTIAL,
            data_gaps=("g_working_set_priority_event_coverage_partial",),
            canonical_sha256="c" * 64,
            evaluated_at=NOW.isoformat(),
        )


class _ReadyWorkingSetReader:
    def __init__(self, bound_article_ids: tuple[str, ...]) -> None:
        self._bound_article_ids = bound_article_ids

    def evaluate(self, *, now: datetime | None = None) -> GWorkingSetAssessment:
        del now
        return GWorkingSetAssessment(
            status=GWorkingSetStatus.READY,
            canonical_sha256="d" * 64,
            evaluated_at=NOW.isoformat(),
            manifest={
                "articles": [{"article_id": article_id} for article_id in self._bound_article_ids]
            },
        )


def test_runtime_context_keeps_old_g_while_exposing_freshness_gap(
    tmp_path: Path,
) -> None:
    kb_root = tmp_path / "knowledge-base"
    _write_index(kb_root)
    _write_events(kb_root)
    result = AgentRuntimeContextProvider(
        kb_root=kb_root,
        pinned_sources=(),
        knowledge_documents=[],
        g_working_set_reader=_PartialWorkingSetReader(),
    ).resolve(
        AgentRuntimeContextRequest(
            agent_id="guo_teacher",
            question="最近 G 主线有什么变化",
            now=NOW.isoformat(),
        )
    )

    assert result.llm_context["g_context"][0]["source_ref"] == "g-current"
    assert "g_working_set_priority_event_coverage_partial" in result.data_gaps
    assert result.audit_context["fresh_g"]["working_set_freshness"]["status"] == "PARTIAL"


def test_runtime_does_not_make_old_article_fresh_from_new_event(
    tmp_path: Path,
) -> None:
    kb_root = tmp_path / "knowledge-base"
    _write_index(
        kb_root,
        article_id="g-old",
        column="星大派特刊",
        published_at="2026-05-01 09:00",
    )
    _write_events(kb_root, article_id="g-old", column="星大派特刊")
    result = AgentRuntimeContextProvider(
        kb_root=kb_root,
        pinned_sources=(),
        knowledge_documents=[],
        g_working_set_reader=_PartialWorkingSetReader(),
    ).resolve(
        AgentRuntimeContextRequest(
            agent_id="guo_teacher",
            question="最近 G 主线有什么变化",
            now=NOW.isoformat(),
        )
    )

    assert all(
        item.get("article_id") != "g-old" for item in result.llm_context.get("g_context", [])
    )
    assert "fresh_g_context_cache_empty" in result.data_gaps


def test_runtime_excludes_future_and_invalid_candidate_times() -> None:
    def candidate(article_id: str, published_at: str) -> dict[str, object]:
        return {
            "article_id": article_id,
            "title": f"G 主题 {article_id}",
            "column": "星大派特刊",
            "source_classification": "teacher_original",
            "published_at": published_at,
            "persona_eligible": True,
            "theme_clusters": ["主题"],
            "guidance_brief": "老师原文背景，只作认知参考。",
        }

    result = AgentRuntimeContextProvider(
        kb_root=Path("/nonexistent/fin-runtime-context-tests"),
        pinned_sources=(),
        knowledge_documents=[],
        fresh_g_candidates=(
            candidate("g-valid", "2026-07-23T09:00:00+08:00"),
            candidate("g-future", "2026-07-23T11:00:00+08:00"),
            candidate("g-invalid", "not-a-time"),
        ),
    ).resolve(
        AgentRuntimeContextRequest(
            agent_id="guo_teacher",
            question="G 主题有什么变化",
            now=NOW.isoformat(),
        )
    )

    assert [item["source_ref"] for item in result.llm_context["g_context"]] == ["g-valid"]
    assert "fresh_g_candidate_time_invalid" in result.data_gaps


def test_runtime_keeps_exact_long_term_source_but_never_promotes_ambiguous_label() -> None:
    candidates = (
        {
            "article_id": "ambiguous",
            "title": "星大派：未细分",
            "column": "星大派",
            "source_classification": "teacher_original",
            "published_at": "2026-07-23T09:00:00+08:00",
            "guidance_brief": "不应进入 G。",
        },
        {
            "article_id": "long-term",
            "title": "凤仙郡小故事：产业链的长期演进",
            "column": "凤仙郡小故事",
            "source_classification": "teacher_original",
            "published_at": "2026-07-23T09:10:00+08:00",
            "guidance_brief": "长期产业框架。",
        },
    )
    result = AgentRuntimeContextProvider(
        kb_root=Path("/nonexistent/fin-runtime-context-tests"),
        pinned_sources=(),
        knowledge_documents=[],
        fresh_g_candidates=candidates,
    ).resolve(
        AgentRuntimeContextRequest(
            agent_id="guo_teacher",
            question="产业链的长期演进怎么看？",
            now=NOW.isoformat(),
        )
    )

    [item] = result.llm_context["g_context"]
    assert item["source_ref"] == "long-term"
    assert item["source_family"] == "凤仙郡小故事"
    assert item["content_type"] == "长期故事"
    assert item["source_usage"] == "long_term_framework"
    assert item["priority_label"] is None


def test_ready_runtime_rejects_manifest_selection_drift_before_filtering(
    tmp_path: Path,
) -> None:
    kb_root = tmp_path / "knowledge-base"
    _write_index(
        kb_root,
        article_id="g-bound",
        column="星大派特刊",
        published_at="2026-07-22 09:00",
    )
    index_path = kb_root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    outside_name = "20260723_g-outside.md"
    (kb_root / "articles" / outside_name).write_text("未绑定 G", encoding="utf-8")
    index["articles"].append(
        {
            "id": "g-outside",
            "date": "2026-07-23 08:00",
            "column": "星大派特刊",
            "title": "未绑定主题",
            "file": outside_name,
            "path": str(kb_root / "articles" / outside_name),
        }
    )
    index["total"] = 2
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    events_path = kb_root / "runtime" / "cognition" / "priority_events.jsonl"
    _write_events(kb_root, article_id="g-bound", column="星大派特刊")
    outside_event = {
        "event_id": "pa:g-outside",
        "article_id": "g-outside",
        "title": "未绑定主题",
        "source_classification": "teacher_original",
        "persona_eligible": True,
        "requires_deep_read": True,
        "created_at": "2026-07-23T09:31:00+08:00",
        "metadata": {"column": "星大派特刊"},
    }
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outside_event, ensure_ascii=False) + "\n")
    result = AgentRuntimeContextProvider(
        kb_root=kb_root,
        pinned_sources=(),
        knowledge_documents=[],
        g_working_set_reader=_ReadyWorkingSetReader(("g-bound",)),
    ).resolve(
        AgentRuntimeContextRequest(
            agent_id="guo_teacher",
            question="最近主题有什么变化",
            now=NOW.isoformat(),
        )
    )

    assert result.llm_context.get("g_context", []) == []
    assert "g_working_set_sources_changed" in result.data_gaps
