from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from fin_analyse.scraper.cdp_runtime import (
    GWorkingSetPublicationReceipt,
    ProductionCdpCompletionReceipt,
)
from fin_analyse.scraper.contracts import ZsxqRunRequest, ZsxqRunResult


def _result(status: str, *, changed_count: int | None = 0) -> ZsxqRunResult:
    return ZsxqRunResult(
        status=status,
        request_id="request-1",
        intent="sync",
        trigger="schedule",
        run_id="run-1",
        changed_count=changed_count,
        attempt=1,
        started_at="2026-07-15T08:00:00+00:00",
        finished_at="2026-07-15T08:01:00+00:00",
    )


def _completion(
    status: str,
    *,
    completion_status: str | None = None,
    g_status: str = "READY",
    changed_count: int | None = None,
) -> ProductionCdpCompletionReceipt:
    if changed_count is None:
        changed_count = 1 if status == "succeeded" else 0
    run = _result(status, changed_count=changed_count)
    if completion_status is None:
        if status == "coalesced":
            completion_status = "coalesced"
        elif status in {"failed", "deadline_exceeded", "interrupted"}:
            completion_status = "failed"
        elif status == "partial" or g_status != "READY":
            completion_status = "partial"
        else:
            completion_status = "ready"
    source_coverage_sha256 = "b" * 64
    receipt = (
        None
        if status == "coalesced"
        else GWorkingSetPublicationReceipt(
            published=True,
            status=g_status,
            generation="a" * 64,
            evaluated_at="2026-07-15T08:01:00+00:00",
            source_refs=("article-1",),
            data_gaps=() if g_status == "READY" else ("g_working_set_not_ready",),
            freshness="FRESH" if g_status == "READY" else "UNKNOWN",
            source_coverage_sha256=source_coverage_sha256,
            producer_id="fin.zsxq-production-cdp/v1",
            producer_run_id=run.run_id,
            producer_run_status=run.status,
            publication_mode="NO_CHANGE" if status == "no_change" else "CURRENT_RUN",
            prior_generation="a" * 64 if status == "no_change" else None,
            prior_source_refs=("article-1",) if status == "no_change" else None,
            prior_source_coverage_sha256=(
                source_coverage_sha256 if status == "no_change" else None
            ),
            prior_evaluated_at=("2026-07-15T07:59:59+00:00" if status == "no_change" else None),
            prior_freshness="FRESH" if status == "no_change" else None,
        )
    )
    return ProductionCdpCompletionReceipt(
        run=run,
        completion_status=completion_status,
        g_working_set=receipt,
    )


def _seed_empty_g_sources(knowledge_base_root: Path) -> None:
    knowledge_base_root.mkdir(parents=True)
    (knowledge_base_root / "index.json").write_text(
        json.dumps(
            {
                "articles": [],
                "total": 0,
                "updated": "2026-07-23T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    event_path = knowledge_base_root / "runtime" / "cognition" / "priority_events.jsonl"
    event_path.parent.mkdir(parents=True)
    event_path.write_text("", encoding="utf-8")


def _valid_g_assessment(
    knowledge_base_root: Path,
    *,
    status: str,
    deep_read_content_hash: str = "c" * 64,
    article_ids: tuple[str, ...] = ("zsxq-2", "zsxq-1"),
    evaluated_at: datetime = datetime(2026, 7, 15, 8, 2, tzinfo=UTC),
):
    from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService

    knowledge_base_root.mkdir(parents=True, exist_ok=True)

    class Pair:
        generation_id = "deep-read-generation-1"
        generated_at = "2026-07-15T08:00:45+00:00"
        content_hash = deep_read_content_hash
        compact_raw_sha256 = "d" * 64

    class DeepReadReader:
        def load_fresh_pair(self, _article_id, _article_file):
            return None if status == "PARTIAL" else Pair()

    service = GWorkingSetService(
        kb_root=knowledge_base_root,
        deep_read_reader=DeepReadReader(),
    )
    now = evaluated_at
    if status == "MISSING":
        return service.reconcile(now=now)

    articles = []
    events = []
    for offset, article_id in enumerate(article_ids):
        article_path = knowledge_base_root / "articles" / f"{article_id}.md"
        article_path.parent.mkdir(parents=True, exist_ok=True)
        article_path.write_text("老师原文", encoding="utf-8")
        minute = 56 - offset
        articles.append(
            {
                "id": article_id,
                "date": f"2026-07-15T07:{minute:02d}:00+00:00",
                "column": "星大派锐评",
                "title": f"当前 G 主线 {article_id}",
                "file": article_path.name,
            }
        )
        events.append(
            {
                "event_id": f"pa:{article_id}",
                "article_id": article_id,
                "title": f"当前 G 主线 {article_id}",
                "source_classification": "teacher_original",
                "requires_deep_read": True,
                "created_at": "2026-07-15T08:00:30+00:00",
                "metadata": {"column": "星大派锐评"},
            }
        )
    (knowledge_base_root / "index.json").write_text(
        json.dumps(
            {
                "articles": articles,
                "updated": "2026-07-15T08:00:00+00:00",
                "total": len(articles),
            }
        ),
        encoding="utf-8",
    )
    event_path = knowledge_base_root / "runtime" / "cognition" / "priority_events.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text(
        "".join(f"{json.dumps(event)}\n" for event in events),
        encoding="utf-8",
    )
    if status == "STALE":
        service.reconcile_and_publish(now=now)
        return service.evaluate(now=now.replace(day=16, hour=9))
    return service.reconcile(now=now)


def _assert_legacy_no_change_without_journal(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    production_calls = 0

    def run_once(**_kwargs):
        nonlocal production_calls
        production_calls += 1
        return _completion("no_change")

    monkeypatch.setattr(scheduled_run, "run_production_cdp_once", run_once)
    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text(
        '{"articles": [], "total": 0}',
        encoding="utf-8",
    )

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(streams.out)["status"] == "no_change"
    assert streams.err == ""
    assert production_calls == 1


def test_scheduled_cli_flushes_start_receipt_before_handoff_lock_can_be_killed(
    tmp_path: Path,
) -> None:
    runtime_db = (tmp_path / "private-runtime-path" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "private-knowledge-path").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text(
        '{"articles": [], "total": 0}',
        encoding="utf-8",
    )
    child_source = """
import os
import signal
import sys
from contextlib import contextmanager

from fin_analyse.scraper import scheduled_run


@contextmanager
def block_handoff_lock(*_args, **_kwargs):
    barrier_fd = int(sys.argv[3])
    os.write(barrier_fd, b"READY\\n")
    os.close(barrier_fd)
    signal.pause()
    yield


scheduled_run.hold_scheduler_handoff_lock = block_handoff_lock
scheduled_run.run_production_cdp_once = lambda **_kwargs: (_ for _ in ()).throw(
    AssertionError("production must remain unreachable")
)
raise SystemExit(
    scheduled_run.main(
        [
            "--runtime-db",
            sys.argv[1],
            "--knowledge-base-root",
            sys.argv[2],
        ]
    )
)
"""
    barrier_read_fd, barrier_write_fd = os.pipe()
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_source,
                str(runtime_db),
                str(knowledge_base_root),
                str(barrier_write_fd),
            ],
            cwd=Path(__file__).resolve().parents[2],
            pass_fds=(barrier_write_fd,),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(barrier_write_fd)
        barrier_write_fd = -1
        try:
            with os.fdopen(barrier_read_fd) as barrier:
                barrier_read_fd = -1
                assert barrier.readline().strip() == "READY"
            os.kill(child.pid, signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=5)
            assert child.returncode == -signal.SIGKILL
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    finally:
        if barrier_read_fd >= 0:
            os.close(barrier_read_fd)
        if barrier_write_fd >= 0:
            os.close(barrier_write_fd)

    assert stdout == ""
    events = [json.loads(line) for line in stderr.splitlines()]
    assert len(events) == 1
    started = events[0]
    assert started["schema_version"] == "fin.zsxq-scheduled-invocation-event/v1"
    assert started["phase"] == "started"
    assert UUID(started["invocation_id"]).version == 4
    assert started["intent"] == "sync"
    assert started["trigger"] == "schedule"
    assert started["deadline_seconds"] == 900.0
    started_at = datetime.fromisoformat(started["started_at"])
    deadline_at = datetime.fromisoformat(started["deadline_at"])
    assert started_at.tzinfo is not None
    assert (deadline_at - started_at).total_seconds() == pytest.approx(900.0)
    assert "runtime_db" not in started
    assert "knowledge_base_root" not in started
    assert "private-runtime-path" not in stderr
    assert "private-knowledge-path" not in stderr
    assert not runtime_db.parent.exists()


def test_scheduled_cli_requires_ready_g_receipt_for_zero_exit(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from fin_analyse.scraper import scheduled_run
    from fin_analyse.scraper.cdp_runtime import (
        GWorkingSetPublicationReceipt,
        ProductionCdpCompletionReceipt,
    )

    completion = ProductionCdpCompletionReceipt(
        run=_result("no_change"),
        completion_status="partial",
        g_working_set=GWorkingSetPublicationReceipt(
            published=True,
            status="PARTIAL",
            generation="a" * 64,
            evaluated_at="2026-07-15T08:01:00+00:00",
            source_refs=(),
            data_gaps=("g_working_set_active_g_empty",),
        ),
    )
    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: completion,
    )
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles": [], "total": 0}')

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str((tmp_path / "runtime.sqlite3").resolve()),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["schema_version"] == "fin.zsxq-scheduled-run/v3"
    assert payload["completion_status"] == "partial"
    assert payload["g_working_set"]["schema_version"] == ("fin.zsxq-g-working-set-publication/v1")
    assert payload["g_working_set"] == completion.g_working_set.to_dict()


def test_scheduled_cli_projects_narrow_failure_reason(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """A failed manual sync must say WHY (transport/tab/login/...) in the CLI JSON."""
    from fin_analyse.scraper import scheduled_run

    completion = replace(
        _completion("failed"),
        run=replace(_completion("failed").run, failure_reason="transport_unavailable"),
    )
    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: completion,
    )
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles": [], "total": 0}')

    exit_code = scheduled_run.main(
        [
            "--trigger",
            "manual",
            "--runtime-db",
            str((tmp_path / "runtime.sqlite3").resolve()),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["failure_reason"] == "transport_unavailable"


def test_scheduled_cli_stale_g_reports_partial_not_fresh(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Old or published-but-stale G must never project as FRESH/ready."""
    from fin_analyse.scraper import scheduled_run

    completion = _completion("succeeded")
    assert completion.g_working_set is not None
    stale = replace(
        completion,
        g_working_set=replace(
            completion.g_working_set,
            status="STALE",
            freshness="STALE",
            data_gaps=("g_working_set_manifest_stale",),
        ),
    )
    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: stale,
    )
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles": [], "total": 0}')

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str((tmp_path / "runtime.sqlite3").resolve()),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["completion_status"] == "partial"
    assert payload["g_working_set"]["freshness"] == "STALE"


def test_scheduled_cli_rejects_caller_reported_ready_without_producer_binding(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from fin_analyse.scraper import scheduled_run

    completion = _completion("succeeded")
    assert completion.g_working_set is not None
    caller_reported = replace(
        completion,
        g_working_set=replace(completion.g_working_set, producer_id=None),
    )
    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: caller_reported,
    )
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles": [], "total": 0}')

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str((tmp_path / "runtime.sqlite3").resolve()),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["completion_status"] == "failed"
    assert payload["completion_data_gaps"] == [
        "g_working_set_terminal_fact_invalid",
    ]


@pytest.mark.parametrize(
    "completion",
    [
        replace(
            _completion("succeeded", changed_count=1),
            run=replace(
                _completion("succeeded", changed_count=1).run,
                started_at=None,
            ),
        ),
        replace(
            _completion("no_change"),
            run=replace(
                _completion("no_change").run,
                started_at="2026-07-15T08:02:00+00:00",
                finished_at="2026-07-15T08:01:00+00:00",
            ),
        ),
    ],
    ids=("succeeded-without-start", "no-change-inverted-interval"),
)
def test_scheduled_cli_rejects_ready_without_an_ordered_run_interval(
    completion: ProductionCdpCompletionReceipt,
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: completion,
    )
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles": [], "total": 0}')

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str((tmp_path / "runtime.sqlite3").resolve()),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["completion_status"] == "failed"
    assert payload["completion_data_gaps"] == [
        "g_working_set_terminal_fact_invalid",
    ]


@pytest.mark.parametrize(
    ("status", "changed_count"),
    [
        ("succeeded", 0),
        ("no_change", 9),
    ],
)
def test_scheduled_cli_rejects_ready_with_noncanonical_status_count(
    status: str,
    changed_count: int,
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    completion = _completion(status, changed_count=changed_count)
    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: completion,
    )
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles": [], "total": 0}')

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str((tmp_path / "runtime.sqlite3").resolve()),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["completion_status"] == "failed"
    assert payload["completion_data_gaps"] == [
        "g_working_set_terminal_fact_invalid",
    ]


def test_verified_no_change_completion_rejects_prior_evaluated_after_run_started() -> None:
    completion = _completion("no_change")
    assert completion.g_working_set is not None
    later_prior = replace(
        completion,
        g_working_set=replace(
            completion.g_working_set,
            evaluated_at="2026-07-15T08:02:00+00:00",
            prior_evaluated_at="2026-07-15T08:01:01+00:00",
        ),
    )

    assert later_prior.verified_completion_status() == "failed"
    assert later_prior.verified_completion_data_gaps() == (
        "g_working_set_no_change_prior_evaluated_after_run_started",
    )


def test_production_once_publishes_g_working_set_only_after_terminal_close(
    monkeypatch, tmp_path: Path
) -> None:
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("no_change")
    knowledge_base_root = tmp_path / "knowledge-base"
    _seed_empty_g_sources(knowledge_base_root)
    manifest_path = (
        knowledge_base_root / "runtime" / "operations" / "g_working_set" / "manifest.v1.json"
    )

    class FakeRepository:
        closed = False
        lease_released = False

        def close(self) -> None:
            assert self.lease_released is True
            assert not manifest_path.exists()
            self.closed = True

    repository = FakeRepository()

    class FakeModule:
        def run(self, request):
            assert request.intent == "sync"
            assert repository.closed is False
            repository.lease_released = True
            return expected

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), repository),
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=knowledge_base_root,
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert observed.run is expected
    assert observed.completion_status == "partial"
    assert observed.g_working_set is not None
    assert observed.g_working_set.published is True
    assert observed.g_working_set.status == "PARTIAL"
    assert "g_working_set_active_g_empty" in observed.g_working_set.data_gaps
    assert repository.closed is True
    assert manifest_path.is_file()


def test_production_once_consumes_pre_run_setup_from_the_request_deadline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest, ZsxqRunResult

    clock_values = iter((100.0, 110.0))
    monkeypatch.setattr(
        cdp_runtime,
        "monotonic",
        lambda: next(clock_values),
        raising=False,
    )
    captured: dict[str, object] = {}

    class FakeModule:
        def run(self, request):
            captured["request"] = request
            return ZsxqRunResult(
                status="coalesced",
                request_id=request.request_id,
                intent=request.intent,
                trigger=request.trigger,
                coalesced=True,
                active_run_id="active-run",
            )

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(
            intent="sync",
            trigger="schedule",
            deadline_seconds=900,
        ),
    )

    request = cast(ZsxqRunRequest, captured["request"])
    assert request.deadline_seconds == 890
    assert observed.run.status == "coalesced"
    assert observed.completion_status == "coalesced"


def test_production_once_returns_ready_generation_and_bound_source_refs(
    monkeypatch, tmp_path: Path
) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import (
        GWorkingSetService,
        GWorkingSetStatus,
    )
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("succeeded", changed_count=2)

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    assessment = _valid_g_assessment(tmp_path / "knowledge-base", status="READY")
    assert assessment.status is GWorkingSetStatus.READY
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: assessment,
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(intent="sync", trigger="manual"),
    )

    assert observed.run is expected
    assert observed.completion_status == "ready"
    evidence = assessment.to_publication_evidence()
    assert observed.g_working_set is not None
    assert observed.g_working_set.freshness == "FRESH"
    assert observed.g_working_set.source_coverage_sha256 == evidence.source_coverage_sha256
    assert observed.g_working_set.producer_id == "fin.zsxq-production-cdp/v1"
    assert observed.g_working_set.producer_run_id == expected.run_id
    assert observed.g_working_set.producer_run_status == expected.status
    assert observed.g_working_set.publication_mode == "CURRENT_RUN"
    assert observed.g_working_set.prior_generation is None
    assert observed.g_working_set.prior_source_coverage_sha256 is None
    assert observed.g_working_set == GWorkingSetPublicationReceipt(
        published=True,
        status="READY",
        generation=assessment.canonical_sha256,
        evaluated_at=assessment.evaluated_at,
        source_refs=("zsxq-2", "zsxq-1"),
        data_gaps=(),
        freshness="FRESH",
        source_coverage_sha256=evidence.source_coverage_sha256,
        producer_id="fin.zsxq-production-cdp/v1",
        producer_run_id=expected.run_id,
        producer_run_status=expected.status,
        publication_mode="CURRENT_RUN",
    )


def test_production_once_rejects_ready_assessment_with_unbound_source_refs(
    monkeypatch, tmp_path: Path
) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import (
        GWorkingSetAssessment,
        GWorkingSetService,
        GWorkingSetStatus,
    )
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("succeeded", changed_count=1)

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: GWorkingSetAssessment(
            status=GWorkingSetStatus.READY,
            canonical_sha256="b" * 64,
            evaluated_at="2026-07-15T08:02:00+00:00",
            manifest={"articles": [{"article_id": "forged-source-ref"}]},
        ),
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert observed.run is expected
    assert observed.completion_status == "failed"
    assert observed.completion_data_gaps == ("g_working_set_publication_evidence_invalid",)
    assert observed.g_working_set == GWorkingSetPublicationReceipt(
        published=False,
        status="UNAVAILABLE",
        generation=None,
        evaluated_at=None,
        data_gaps=("g_working_set_publication_evidence_invalid",),
        producer_id="fin.zsxq-production-cdp/v1",
        producer_run_id=expected.run_id,
        producer_run_status=expected.status,
        publication_mode="CURRENT_RUN",
    )


def test_production_once_rejects_g_evidence_evaluated_before_run_finished(
    monkeypatch, tmp_path: Path
) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("succeeded", changed_count=1)
    assessment = _valid_g_assessment(
        tmp_path / "knowledge-base",
        status="READY",
        evaluated_at=datetime(2026, 7, 15, 8, 0, 50, tzinfo=UTC),
    )

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: assessment,
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert observed.completion_status == "failed"
    assert observed.completion_data_gaps == ("g_working_set_evaluated_before_run_finished",)


def test_production_once_accepts_post_run_g_evidence_later_in_same_second(
    monkeypatch, tmp_path: Path
) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = replace(
        _result("succeeded", changed_count=1),
        finished_at="2026-07-15T08:01:00.100000+00:00",
    )
    assessment = _valid_g_assessment(
        tmp_path / "knowledge-base",
        status="READY",
        evaluated_at=datetime(2026, 7, 15, 8, 1, 0, 200000, tzinfo=UTC),
    )

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: assessment,
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert assessment.evaluated_at == "2026-07-15T08:01:00.200000+00:00"
    assert observed.completion_status == "ready"
    assert observed.completion_data_gaps == ()


def test_no_change_without_prior_g_manifest_cannot_report_ready(
    monkeypatch, tmp_path: Path
) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import (
        GWorkingSetAssessment,
        GWorkingSetService,
        GWorkingSetStatus,
    )
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("no_change")
    knowledge_base_root = tmp_path / "knowledge-base"
    article_path = knowledge_base_root / "articles" / "zsxq-1.md"
    article_path.parent.mkdir(parents=True)
    article_path.write_text("老师原文", encoding="utf-8")
    (knowledge_base_root / "index.json").write_text(
        json.dumps(
            {
                "articles": [
                    {
                        "id": "zsxq-1",
                        "date": "2026-07-15T07:55:00+00:00",
                        "column": "星大派锐评",
                        "title": "当前 G 主线",
                        "file": article_path.name,
                    }
                ],
                "updated": "2026-07-15T08:00:00+00:00",
                "total": 1,
            }
        ),
        encoding="utf-8",
    )
    event_path = knowledge_base_root / "runtime" / "cognition" / "priority_events.jsonl"
    event_path.parent.mkdir(parents=True)
    event_path.write_text(
        json.dumps(
            {
                "event_id": "pa:zsxq-1",
                "article_id": "zsxq-1",
                "title": "当前 G 主线",
                "source_classification": "teacher_original",
                "requires_deep_read": True,
                "created_at": "2026-07-15T08:00:30+00:00",
                "metadata": {"column": "星大派锐评"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class Pair:
        generation_id = "deep-read-generation-1"
        generated_at = "2026-07-15T08:00:45+00:00"
        content_hash = "c" * 64
        compact_raw_sha256 = "d" * 64

    class DeepReadReader:
        def load_fresh_pair(self, article_id, article_file):
            assert article_id == "zsxq-1"
            assert article_file == article_path
            return Pair()

    ready_assessment = GWorkingSetService(
        kb_root=knowledge_base_root,
        deep_read_reader=DeepReadReader(),
    ).reconcile(now=datetime(2026, 7, 15, 8, 2, tzinfo=UTC))
    assert ready_assessment.status is GWorkingSetStatus.READY

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    monkeypatch.setattr(
        GWorkingSetService,
        "evaluate",
        lambda _self: GWorkingSetAssessment.missing("g_working_set_manifest_missing"),
    )
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: ready_assessment,
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=knowledge_base_root,
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert observed.run is expected
    assert observed.completion_status == "failed"
    assert observed.completion_data_gaps == ("g_working_set_no_change_prior_manifest_missing",)
    assert observed.g_working_set is not None
    assert observed.g_working_set.status == "READY"


def test_no_change_rejects_a_different_post_run_g_generation(monkeypatch, tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import (
        GWorkingSetService,
        GWorkingSetStatus,
    )
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = replace(
        _result("no_change"),
        started_at="2026-07-15T08:02:00+00:00",
        finished_at="2026-07-15T08:02:00+00:00",
    )
    prior = _valid_g_assessment(
        tmp_path / "prior-knowledge-base",
        status="READY",
        deep_read_content_hash="c" * 64,
    )
    post = _valid_g_assessment(
        tmp_path / "post-knowledge-base",
        status="READY",
        deep_read_content_hash="e" * 64,
    )
    assert prior.status is GWorkingSetStatus.READY
    assert post.status is GWorkingSetStatus.READY
    assert prior.canonical_sha256 != post.canonical_sha256

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    monkeypatch.setattr(GWorkingSetService, "evaluate", lambda _self: prior)
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: post,
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert observed.run is expected
    assert observed.completion_status == "failed"
    assert observed.completion_data_gaps == ("g_working_set_no_change_generation_drift",)


def test_no_change_rejects_post_run_source_ref_drift(monkeypatch, tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import (
        GWorkingSetService,
        GWorkingSetStatus,
    )
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = replace(
        _result("no_change"),
        started_at="2026-07-15T08:02:00+00:00",
        finished_at="2026-07-15T08:02:00+00:00",
    )
    prior = _valid_g_assessment(
        tmp_path / "prior-knowledge-base",
        status="READY",
        article_ids=("zsxq-2", "zsxq-1"),
    )
    post = _valid_g_assessment(
        tmp_path / "post-knowledge-base",
        status="READY",
        article_ids=("zsxq-3", "zsxq-1"),
    )
    assert prior.status is GWorkingSetStatus.READY
    assert post.status is GWorkingSetStatus.READY

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    monkeypatch.setattr(GWorkingSetService, "evaluate", lambda _self: prior)
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: post,
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert observed.completion_status == "failed"
    assert observed.completion_data_gaps == ("g_working_set_no_change_source_refs_drift",)


def test_no_change_rejects_a_stale_prior_g_generation(monkeypatch, tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import (
        GWorkingSetService,
        GWorkingSetStatus,
    )
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("no_change")
    post = _valid_g_assessment(tmp_path / "knowledge-base", status="READY")
    prior = replace(
        post,
        status=GWorkingSetStatus.STALE,
        data_gaps=("g_working_set_manifest_stale",),
    )

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    monkeypatch.setattr(GWorkingSetService, "evaluate", lambda _self: prior)
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: post,
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert observed.completion_status == "failed"
    assert observed.completion_data_gaps == ("g_working_set_no_change_prior_stale",)


def test_production_once_rejects_no_change_prior_evaluated_after_run_started(
    monkeypatch, tmp_path: Path
) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("no_change")
    assessment = _valid_g_assessment(tmp_path / "knowledge-base", status="READY")

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    monkeypatch.setattr(GWorkingSetService, "evaluate", lambda _self: assessment)
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: assessment,
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert observed.completion_status == "failed"
    assert observed.verified_completion_status() == "failed"
    assert observed.completion_data_gaps == (
        "g_working_set_no_change_prior_evaluated_after_run_started",
    )
    assert observed.verified_completion_data_gaps() == observed.completion_data_gaps


def test_no_change_ready_receipt_binds_verified_prior_continuity(
    monkeypatch, tmp_path: Path
) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = replace(
        _result("no_change"),
        started_at="2026-07-15T08:02:00+00:00",
        finished_at="2026-07-15T08:02:00+00:00",
    )
    assessment = _valid_g_assessment(tmp_path / "knowledge-base", status="READY")
    evidence = assessment.to_publication_evidence()

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    monkeypatch.setattr(GWorkingSetService, "evaluate", lambda _self: assessment)
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: assessment,
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert observed.completion_status == "ready"
    assert observed.completion_data_gaps == ()
    assert observed.g_working_set is not None
    assert observed.g_working_set.publication_mode == "NO_CHANGE"
    assert observed.g_working_set.prior_generation == evidence.generation
    assert observed.g_working_set.prior_source_refs == evidence.source_refs
    assert observed.g_working_set.prior_source_coverage_sha256 == evidence.source_coverage_sha256
    assert observed.g_working_set.prior_evaluated_at == evidence.evaluated_at
    assert observed.g_working_set.prior_freshness == "FRESH"


def test_production_once_isolates_g_publisher_failure_with_sanitized_status(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("succeeded", changed_count=1)
    secret = "private-customer-path-do-not-log"
    missing_knowledge_base_root = tmp_path / secret / "knowledge-base"

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        closed = False

        def close(self) -> None:
            self.closed = True

    repository = FakeRepository()
    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), repository),
    )

    with caplog.at_level("WARNING", logger="fin_analyse.scraper.cdp_runtime"):
        observed = cdp_runtime.run_production_cdp_once(
            runtime_db_path=tmp_path / "runtime.sqlite3",
            knowledge_base_root=missing_knowledge_base_root,
            request=ZsxqRunRequest(intent="sync", trigger="schedule"),
        )

    assert observed.run is expected
    assert observed.completion_status == "failed"
    assert observed.g_working_set == GWorkingSetPublicationReceipt(
        published=False,
        status="UNAVAILABLE",
        generation=None,
        evaluated_at=None,
        data_gaps=("g_working_set_publish_failed",),
        producer_id="fin.zsxq-production-cdp/v1",
        producer_run_id=expected.run_id,
        producer_run_status=expected.status,
        publication_mode="CURRENT_RUN",
    )
    assert repository.closed is True
    assert [record.getMessage() for record in caplog.records] == [
        "g_working_set_completion_status=publish_failed"
    ]
    assert secret not in caplog.text


def test_production_once_skips_g_publication_for_coalesced_result(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = ZsxqRunResult(
        status="coalesced",
        request_id="request-1",
        intent="sync",
        trigger="schedule",
        coalesced=True,
        active_run_id="active-run-1",
    )

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        closed = False

        def close(self) -> None:
            self.closed = True

    repository = FakeRepository()
    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), repository),
    )
    knowledge_base_root = tmp_path / "absent-knowledge-base"

    with caplog.at_level("INFO", logger="fin_analyse.scraper.cdp_runtime"):
        observed = cdp_runtime.run_production_cdp_once(
            runtime_db_path=tmp_path / "runtime.sqlite3",
            knowledge_base_root=knowledge_base_root,
            request=ZsxqRunRequest(intent="sync", trigger="schedule"),
        )

    assert observed.run is expected
    assert observed.completion_status == "coalesced"
    assert observed.g_working_set is None
    assert repository.closed is True
    assert not knowledge_base_root.exists()
    assert "g_working_set_completion_status" not in caplog.text


def test_live_proof_never_publishes_g_working_set(monkeypatch, tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqHealth

    observed_health = ZsxqHealth(
        state="healthy",
        page_state="ready",
        reason_code="ready",
        observed_at="2026-07-23T00:00:00+00:00",
        health_episode_id="episode-1",
    )

    class FakeRepository:
        closed = False
        observed = False

        def health_observation_count(self) -> int:
            return int(self.observed)

        def get_active_lease(self):
            return None

        def close(self) -> None:
            self.closed = True

    repository = FakeRepository()

    class FakeModule:
        def health(self, _request):
            repository.observed = True
            return observed_health

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), repository),
    )

    def fail_if_published(_self, **_kwargs):
        raise AssertionError("live proof must not publish the G working set")

    monkeypatch.setattr(GWorkingSetService, "reconcile_and_publish", fail_if_published)

    result = cdp_runtime.run_production_cdp_live_proof(
        runtime_db_path=tmp_path / "live-proof.sqlite3",
        deadline_at=datetime.now(UTC),
    )

    assert result["status"] == "passed"
    assert repository.closed is True


@pytest.mark.parametrize("publisher_status", ("PARTIAL", "STALE", "MISSING"))
def test_non_collecting_terminal_run_never_publishes_g_working_set(
    monkeypatch, tmp_path: Path, publisher_status: str
) -> None:
    """A failed run must not publish/re-stamp G: transport 失败不得标为 G fresh。

    The failed run's receipt carries no G claim at all, so the CLI can never
    project ``g_working_set.freshness=FRESH`` next to ``status=failed``.
    """
    from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("failed")

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    assessment = _valid_g_assessment(
        tmp_path / f"knowledge-base-{publisher_status.lower()}",
        status=publisher_status,
    )
    publish_calls = []
    monkeypatch.setattr(
        GWorkingSetService,
        "reconcile_and_publish",
        lambda _self: (publish_calls.append("publish") or assessment),
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path,
        request=ZsxqRunRequest(intent="sync", trigger="schedule"),
    )

    assert observed.run is expected
    assert observed.completion_status == "failed"
    assert observed.g_working_set is None
    assert publish_calls == []


def test_no_change_retries_g_working_set_backlog_after_prior_publish_failure(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("no_change")
    knowledge_base_root = tmp_path / "knowledge-base"

    class FakeModule:
        def run(self, _request):
            return expected

    class FakeRepository:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), FakeRepository()),
    )
    request = ZsxqRunRequest(intent="sync", trigger="schedule")

    with caplog.at_level("INFO", logger="fin_analyse.scraper.cdp_runtime"):
        first = cdp_runtime.run_production_cdp_once(
            runtime_db_path=tmp_path / "runtime.sqlite3",
            knowledge_base_root=knowledge_base_root,
            request=request,
        )
        _seed_empty_g_sources(knowledge_base_root)
        second = cdp_runtime.run_production_cdp_once(
            runtime_db_path=tmp_path / "runtime.sqlite3",
            knowledge_base_root=knowledge_base_root,
            request=request,
        )

    manifest_path = (
        knowledge_base_root / "runtime" / "operations" / "g_working_set" / "manifest.v1.json"
    )
    assert first.run is expected
    assert first.completion_status == "failed"
    assert first.g_working_set is not None
    assert first.g_working_set.published is False
    assert second.run is expected
    assert second.completion_status == "partial"
    assert second.g_working_set is not None
    assert second.g_working_set.status == "PARTIAL"
    assert manifest_path.is_file()
    assert [record.getMessage() for record in caplog.records] == [
        "g_working_set_completion_status=publish_failed",
        "g_working_set_completion_status=published_partial",
    ]


def test_production_once_owns_repository_lifecycle(monkeypatch, tmp_path: Path) -> None:
    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.contracts import ZsxqRunRequest

    expected = _result("no_change")

    class FakeModule:
        def run(self, request):
            assert request.intent == "sync"
            return expected

    class FakeRepository:
        closed = False

        def close(self) -> None:
            self.closed = True

    repository = FakeRepository()
    monkeypatch.setattr(
        cdp_runtime,
        "_build_production_cdp_components",
        lambda **_kwargs: (FakeModule(), repository),
    )

    observed = cdp_runtime.run_production_cdp_once(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=tmp_path / "knowledge-base",
        request=ZsxqRunRequest(intent="sync", trigger="manual"),
    )

    assert observed.run is expected
    assert observed.completion_status == "failed"
    assert repository.closed is True


def test_production_factory_binds_explicit_mutable_knowledge_base(tmp_path: Path) -> None:
    from fin_analyse.scraper.cdp_runtime import build_production_cdp_module

    knowledge_base_root = tmp_path / "production-kb"
    module = build_production_cdp_module(
        runtime_db_path=tmp_path / "runtime.sqlite3",
        knowledge_base_root=knowledge_base_root,
    )
    try:
        assert module._adapter._knowledge_base_root == knowledge_base_root
        assert module._adapter.scraper_builds == 0
    finally:
        module._repo.close()


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        ("succeeded", 0),
        ("no_change", 0),
        ("partial", 4),
        ("coalesced", 75),
        ("failed", 1),
        ("deadline_exceeded", 2),
        ("interrupted", 3),
    ],
)
def test_scheduled_cli_maps_run_status_to_stable_exit(
    monkeypatch, tmp_path: Path, capsys, status: str, expected_exit: int
) -> None:
    from fin_analyse.scraper import scheduled_run

    captured: dict[str, object] = {}

    def fake_run_once(*, runtime_db_path, knowledge_base_root, request):
        captured["runtime_db_path"] = runtime_db_path
        captured["knowledge_base_root"] = knowledge_base_root
        captured["request"] = request
        return _completion(status)

    monkeypatch.setattr(scheduled_run, "run_production_cdp_once", fake_run_once)
    db_path = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "production" / "knowledge-base").resolve()
    knowledge_base_root.mkdir(parents=True)
    (knowledge_base_root / "index.json").write_text('{"articles": [], "total": 0}')

    exit_code = scheduled_run.main(
        [
            "--intent",
            "sync",
            "--trigger",
            "schedule",
            "--deadline-seconds",
            "900",
            "--runtime-db",
            str(db_path),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    streams = capsys.readouterr()
    payload = json.loads(streams.out)
    assert exit_code == expected_exit
    expected_payload = {
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": status,
        "completion_status": _completion(status).verified_completion_status(),
        "completion_data_gaps": list(_completion(status).verified_completion_data_gaps()),
        "intent": "sync",
        "trigger": "schedule",
        "coalesced": False,
        "run_id": "run-1",
        "changed_count": _completion(status).run.changed_count,
        "attempt": 1,
        "started_at": "2026-07-15T08:00:00+00:00",
        "finished_at": "2026-07-15T08:01:00+00:00",
    }
    completion = _completion(status)
    if completion.g_working_set is not None:
        expected_payload["g_working_set"] = completion.g_working_set.to_dict()
    assert payload == expected_payload
    request = cast(ZsxqRunRequest, captured["request"])
    assert request.intent == "sync"
    assert request.trigger == "schedule"
    assert 0 < request.deadline_seconds <= 900
    assert captured["runtime_db_path"] == db_path
    assert captured["knowledge_base_root"] == knowledge_base_root
    assert db_path.parent.stat().st_mode & 0o777 == 0o700
    invocation_events = [json.loads(line) for line in streams.err.splitlines() if line.strip()]
    assert [event["phase"] for event in invocation_events] == ["started", "finished"]
    started_event, finished_event = invocation_events
    assert finished_event["schema_version"] == ("fin.zsxq-scheduled-invocation-event/v1")
    assert finished_event["invocation_id"] == started_event["invocation_id"]
    assert finished_event["status"] == status
    assert finished_event["exit_code"] == expected_exit
    assert datetime.fromisoformat(finished_event["finished_at"]) >= datetime.fromisoformat(
        started_event["started_at"]
    )


def test_scheduled_cli_passes_only_the_recorded_invocation_budget_remaining(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    clock_values = iter(
        (
            datetime(2026, 7, 29, 8, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 29, 8, 0, 10, tzinfo=UTC),
            datetime(2026, 7, 29, 8, 0, 11, tzinfo=UTC),
        )
    )
    monotonic_values = iter((100.0, 110.0, 111.0))
    monkeypatch.setattr(scheduled_run, "_utc_now", lambda: next(clock_values))
    monkeypatch.setattr(
        scheduled_run,
        "monotonic",
        lambda: next(monotonic_values),
    )
    captured: dict[str, object] = {}

    def fake_run_once(*, runtime_db_path, knowledge_base_root, request):
        captured["request"] = request
        return _completion("no_change")

    monkeypatch.setattr(scheduled_run, "run_production_cdp_once", fake_run_once)
    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text(
        '{"articles": [], "total": 0}',
        encoding="utf-8",
    )

    exit_code = scheduled_run.main(
        [
            "--deadline-seconds",
            "900",
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 0
    request = cast(ZsxqRunRequest, captured["request"])
    assert request.deadline_seconds == 890
    events = [json.loads(line) for line in streams.err.splitlines()]
    assert events[0]["started_at"] == "2026-07-29T08:00:00+00:00"
    assert events[0]["deadline_at"] == "2026-07-29T08:15:00+00:00"
    assert events[1]["finished_at"] == "2026-07-29T08:00:11+00:00"


def test_scheduled_cli_fails_closed_when_invocation_budget_is_spent_before_run(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    wall_clock_values = iter(
        (
            datetime(2026, 7, 29, 8, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 29, 8, 15, 0, tzinfo=UTC),
            datetime(2026, 7, 29, 8, 15, 1, tzinfo=UTC),
        )
    )
    monotonic_values = iter((100.0, 1000.0))
    monkeypatch.setattr(scheduled_run, "_utc_now", lambda: next(wall_clock_values))
    monkeypatch.setattr(
        scheduled_run,
        "monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("spent invocation budget must not reach production")
        ),
    )
    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text(
        '{"articles": [], "total": 0}',
        encoding="utf-8",
    )

    exit_code = scheduled_run.main(
        [
            "--deadline-seconds",
            "900",
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 70
    assert json.loads(streams.out) == {
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "internal_error",
        "error_code": "invocation_deadline_exceeded_before_run",
    }
    events = [json.loads(line) for line in streams.err.splitlines()]
    assert [event["phase"] for event in events] == ["started", "finished"]
    assert events[1]["invocation_id"] == events[0]["invocation_id"]
    assert events[1]["status"] == "internal_error"
    assert events[1]["exit_code"] == 70


@pytest.mark.parametrize("terminal_monotonic", (1000.0, 1001.0))
def test_scheduled_cli_fails_closed_when_production_returns_at_or_after_deadline(
    monkeypatch,
    tmp_path: Path,
    capsys,
    terminal_monotonic: float,
) -> None:
    from fin_analyse.scraper import scheduled_run

    wall_clock_values = iter(
        (
            datetime(2026, 7, 29, 8, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 29, 8, 0, 10, tzinfo=UTC),
            datetime(2026, 7, 29, 8, 15, 1, tzinfo=UTC),
        )
    )
    monotonic_values = iter((100.0, 110.0, terminal_monotonic))
    monkeypatch.setattr(scheduled_run, "_utc_now", lambda: next(wall_clock_values))
    monkeypatch.setattr(scheduled_run, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: _completion("no_change"),
    )
    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text(
        '{"articles": [], "total": 0}',
        encoding="utf-8",
    )

    exit_code = scheduled_run.main(
        [
            "--deadline-seconds",
            "900",
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 70
    assert json.loads(streams.out) == {
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "internal_error",
        "error_code": "invocation_deadline_exceeded_after_run",
    }
    events = [json.loads(line) for line in streams.err.splitlines()]
    assert [event["phase"] for event in events] == ["started", "finished"]
    assert events[1]["status"] == "internal_error"
    assert events[1]["exit_code"] == 70


def test_scheduled_cli_wall_clock_rollback_cannot_restore_spent_invocation_budget(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    wall_clock_values = iter(
        (
            datetime(2026, 7, 29, 8, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 29, 7, 59, 0, tzinfo=UTC),
            datetime(2026, 7, 29, 8, 0, 11, tzinfo=UTC),
        )
    )
    monotonic_values = iter((100.0, 110.0, 111.0))
    monkeypatch.setattr(scheduled_run, "_utc_now", lambda: next(wall_clock_values))
    monkeypatch.setattr(
        scheduled_run,
        "monotonic",
        lambda: next(monotonic_values),
    )
    captured: dict[str, object] = {}

    def fake_run_once(*, runtime_db_path, knowledge_base_root, request):
        captured["request"] = request
        return _completion("no_change")

    monkeypatch.setattr(scheduled_run, "run_production_cdp_once", fake_run_once)
    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text(
        '{"articles": [], "total": 0}',
        encoding="utf-8",
    )

    exit_code = scheduled_run.main(
        [
            "--deadline-seconds",
            "900",
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 0
    request = cast(ZsxqRunRequest, captured["request"])
    assert request.deadline_seconds == 890
    events = [json.loads(line) for line in streams.err.splitlines()]
    assert events[0]["started_at"] == "2026-07-29T08:00:00+00:00"
    assert events[0]["deadline_at"] == "2026-07-29T08:15:00+00:00"
    assert events[1]["finished_at"] == "2026-07-29T08:00:11+00:00"


def test_scheduled_cli_assigns_a_unique_id_to_each_legal_invocation(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: _completion("no_change"),
    )
    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text(
        '{"articles": [], "total": 0}',
        encoding="utf-8",
    )
    argv = [
        "--runtime-db",
        str(runtime_db),
        "--knowledge-base-root",
        str(knowledge_base_root),
    ]

    invocation_ids: list[str] = []
    for _ in range(2):
        assert scheduled_run.main(argv) == 0
        streams = capsys.readouterr()
        assert json.loads(streams.out)["status"] == "no_change"
        events = [json.loads(line) for line in streams.err.splitlines()]
        assert [event["phase"] for event in events] == ["started", "finished"]
        invocation_ids.append(events[0]["invocation_id"])

    assert len(set(invocation_ids)) == 2


@pytest.mark.parametrize("failure_point", ("uuid", "stderr"))
def test_scheduled_cli_journal_start_failure_does_not_restore_spent_budget(
    monkeypatch,
    tmp_path: Path,
    capsys,
    failure_point: str,
) -> None:
    from fin_analyse.scraper import scheduled_run

    call_order: list[str] = []
    wall_clock_values = iter(
        (
            datetime(2026, 7, 29, 8, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 29, 8, 0, 10, tzinfo=UTC),
        )
    )
    monotonic_values = iter((100.0, 110.0, 111.0))

    def wall_clock_now():
        call_order.append("wall_clock")
        return next(wall_clock_values)

    def monotonic_now():
        call_order.append("monotonic")
        return next(monotonic_values)

    monkeypatch.setattr(scheduled_run, "_utc_now", wall_clock_now)
    monkeypatch.setattr(
        scheduled_run,
        "monotonic",
        monotonic_now,
    )
    if failure_point == "uuid":

        def fail_uuid():
            call_order.append("journal_failure")
            raise OSError("private UUID failure")

        monkeypatch.setattr(scheduled_run, "uuid4", fail_uuid)
    else:

        class FailingStderr:
            def write(self, _text: str) -> int:
                call_order.append("journal_failure")
                raise OSError("private stderr failure")

            def flush(self) -> None:
                return None

        monkeypatch.setattr(scheduled_run.sys, "stderr", FailingStderr())
    captured: dict[str, object] = {}

    def fake_run_once(*, runtime_db_path, knowledge_base_root, request):
        captured["request"] = request
        return _completion("no_change")

    monkeypatch.setattr(scheduled_run, "run_production_cdp_once", fake_run_once)
    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text(
        '{"articles": [], "total": 0}',
        encoding="utf-8",
    )

    exit_code = scheduled_run.main(
        [
            "--deadline-seconds",
            "900",
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(streams.out)["status"] == "no_change"
    assert streams.err == ""
    request = cast(ZsxqRunRequest, captured["request"])
    assert request.deadline_seconds == 890
    assert call_order[:3] == ["monotonic", "wall_clock", "journal_failure"]


def test_scheduled_cli_preserves_legacy_result_when_invocation_id_is_unavailable(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    def fail_uuid():
        raise OSError("private UUID failure")

    monkeypatch.setattr(scheduled_run, "uuid4", fail_uuid)

    _assert_legacy_no_change_without_journal(monkeypatch, tmp_path, capsys)


def test_scheduled_cli_preserves_legacy_result_when_start_clock_is_unavailable(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    def fail_clock():
        raise RuntimeError("private clock failure")

    monkeypatch.setattr(scheduled_run, "_utc_now", fail_clock)

    _assert_legacy_no_change_without_journal(monkeypatch, tmp_path, capsys)


def test_scheduled_cli_preserves_legacy_result_when_start_stderr_fails(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    class FailingStderr:
        def write(self, _text: str) -> int:
            raise OSError("private stderr failure")

        def flush(self) -> None:
            return None

    monkeypatch.setattr(scheduled_run.sys, "stderr", FailingStderr())

    _assert_legacy_no_change_without_journal(monkeypatch, tmp_path, capsys)


@pytest.mark.parametrize("failure_point", ("clock", "stderr"))
def test_scheduled_cli_preserves_legacy_result_when_finished_journal_fails(
    monkeypatch,
    tmp_path: Path,
    capsys,
    failure_point: str,
) -> None:
    from fin_analyse.scraper import scheduled_run

    if failure_point == "clock":
        clock_calls = 0

        def fail_on_finished_clock():
            nonlocal clock_calls
            clock_calls += 1
            if clock_calls <= 2:
                return datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
            raise OSError("private finished clock failure")

        monkeypatch.setattr(scheduled_run, "_utc_now", fail_on_finished_clock)
    else:
        captured_stderr = scheduled_run.sys.stderr

        class FailOnFinishedStderr:
            def write(self, text: str) -> int:
                if '"phase": "finished"' in text:
                    raise OSError("private finished stderr failure")
                captured_stderr.write(text)
                return len(text)

            def flush(self) -> None:
                captured_stderr.flush()

        monkeypatch.setattr(scheduled_run.sys, "stderr", FailOnFinishedStderr())
    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: _completion("no_change"),
    )
    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text(
        '{"articles": [], "total": 0}',
        encoding="utf-8",
    )

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(streams.out)["status"] == "no_change"
    events = [json.loads(line) for line in streams.err.splitlines()]
    assert [event["phase"] for event in events] == ["started"]
    assert "private finished" not in streams.err


def test_scheduled_cli_rejects_relative_runtime_db(capsys) -> None:
    from fin_analyse.scraper import scheduled_run

    exit_code = scheduled_run.main(["--runtime-db", "relative.sqlite3"])

    streams = capsys.readouterr()
    payload = json.loads(streams.out)
    assert exit_code == 64
    assert payload == {
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "invalid_request",
        "error_code": "runtime_db_must_be_absolute",
    }
    assert streams.err == ""


def test_scheduled_cli_does_not_journal_an_invalid_deadline(
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    runtime_db = (tmp_path / "runtime.sqlite3").resolve()

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str(runtime_db),
            "--deadline-seconds",
            "29",
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 64
    assert json.loads(streams.out) == {
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "invalid_request",
        "error_code": "deadline_out_of_range",
    }
    assert streams.err == ""
    assert not runtime_db.exists()
    assert not (runtime_db.parent / "scheduler-handoff.lock").exists()


def test_scheduled_cli_admits_then_finishes_missing_knowledge_root_as_invalid(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run

    monkeypatch.setattr(
        scheduled_run,
        "run_production_cdp_once",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("production must remain unreachable")
        ),
    )
    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "missing-knowledge-base").resolve()

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 64
    assert json.loads(streams.out) == {
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "invalid_request",
        "error_code": "knowledge_base_root_must_be_directory",
    }
    events = [json.loads(line) for line in streams.err.splitlines()]
    assert [event["phase"] for event in events] == ["started", "finished"]
    assert events[1]["invocation_id"] == events[0]["invocation_id"]
    assert events[1]["status"] == "invalid_request"
    assert events[1]["exit_code"] == 64
    assert not runtime_db.exists()
    assert (runtime_db.parent / "scheduler-handoff.lock").is_file()


def test_scheduled_cli_finishes_journal_when_handoff_lock_coalesces(
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run
    from fin_analyse.scraper.scheduler_handoff_lock import (
        HandoffLockMode,
        hold_scheduler_handoff_lock,
        scheduler_handoff_lock_path,
    )

    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    lock_path = scheduler_handoff_lock_path(runtime_db)

    with hold_scheduler_handoff_lock(lock_path, mode=HandoffLockMode.EXCLUSIVE):
        exit_code = scheduled_run.main(
            [
                "--runtime-db",
                str(runtime_db),
                "--knowledge-base-root",
                str(knowledge_base_root),
            ]
        )

    streams = capsys.readouterr()
    assert exit_code == 75
    assert json.loads(streams.out) == {
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "coalesced",
        "completion_status": "coalesced",
        "completion_data_gaps": [],
        "intent": "sync",
        "trigger": "schedule",
        "coalesced": True,
        "error_code": "scheduler_handoff_locked",
    }
    events = [json.loads(line) for line in streams.err.splitlines()]
    assert [event["phase"] for event in events] == ["started", "finished"]
    assert events[1]["invocation_id"] == events[0]["invocation_id"]
    assert events[1]["status"] == "coalesced"
    assert events[1]["exit_code"] == 75
    assert not knowledge_base_root.exists()


def test_scheduled_cli_finishes_journal_for_nonbusy_handoff_lock_error(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from fin_analyse.scraper import scheduled_run
    from fin_analyse.scraper.scheduler_handoff_lock import SchedulerHandoffLockError

    @contextmanager
    def fail_lock(*_args, **_kwargs):
        raise SchedulerHandoffLockError("scheduler_handoff_lock_unavailable")
        yield

    monkeypatch.setattr(scheduled_run, "hold_scheduler_handoff_lock", fail_lock)
    runtime_db = (tmp_path / "runtime" / "runtime.sqlite3").resolve()
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text(
        '{"articles": [], "total": 0}',
        encoding="utf-8",
    )

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 70
    assert json.loads(streams.out) == {
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "internal_error",
        "error_code": "scheduler_handoff_lock_unavailable",
    }
    events = [json.loads(line) for line in streams.err.splitlines()]
    assert [event["phase"] for event in events] == ["started", "finished"]
    assert events[1]["invocation_id"] == events[0]["invocation_id"]
    assert events[1]["status"] == "internal_error"
    assert events[1]["exit_code"] == 70
    assert not runtime_db.parent.exists()


def test_runtime_db_is_owner_only_before_module_run(monkeypatch, tmp_path: Path) -> None:
    from fin_analyse.scraper import scheduled_run

    runtime_db = (tmp_path / "runtime.sqlite3").resolve()
    runtime_db.write_bytes(b"")
    runtime_db.chmod(0o644)
    knowledge_base_root = (tmp_path / "knowledge-base").resolve()
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles": [], "total": 0}')

    def assert_mode_then_stop(**_kwargs):
        assert runtime_db.stat().st_mode & 0o777 == 0o600
        raise RuntimeError("stop after checking mode")

    monkeypatch.setattr(scheduled_run, "run_production_cdp_once", assert_mode_then_stop)

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(knowledge_base_root),
        ]
    )

    assert exit_code == 70


def test_scheduled_cli_sanitizes_internal_error(monkeypatch, tmp_path: Path, capsys) -> None:
    from fin_analyse.scraper import scheduled_run

    secret = "must-not-escape"

    def fail(**_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(scheduled_run, "run_production_cdp_once", fail)
    knowledge_base_root = tmp_path / "knowledge-base"
    knowledge_base_root.mkdir()
    (knowledge_base_root / "index.json").write_text('{"articles": [], "total": 0}')

    exit_code = scheduled_run.main(
        [
            "--runtime-db",
            str((tmp_path / "runtime.sqlite3").resolve()),
            "--knowledge-base-root",
            str(knowledge_base_root.resolve()),
        ]
    )

    streams = capsys.readouterr()
    output = streams.out
    assert exit_code == 70
    assert secret not in output
    assert secret not in streams.err
    assert json.loads(output) == {
        "schema_version": "fin.zsxq-scheduled-run/v3",
        "status": "internal_error",
        "error_code": "scheduled_run_failed",
    }
    events = [json.loads(line) for line in streams.err.splitlines()]
    assert [event["phase"] for event in events] == ["started", "finished"]
    assert events[1]["invocation_id"] == events[0]["invocation_id"]
    assert events[1]["status"] == "internal_error"
    assert events[1]["exit_code"] == 70


def test_scheduled_run_default_knowledge_root_respects_xdg_data_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from fin_analyse.scraper import scheduled_run

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    args = scheduled_run._parser().parse_args([])

    assert args.knowledge_base_root == (tmp_path / "fin-analyse" / "shared" / "knowledge-base")


def test_scraper_uses_injected_knowledge_base_root(tmp_path: Path) -> None:
    from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

    root = tmp_path / "production-kb"
    root.mkdir()
    (root / "index.json").write_text(
        json.dumps(
            {
                "articles": [
                    {
                        "id": "existing-id",
                        "title": "existing",
                        "path": "articles/existing.md",
                    }
                ],
                "total": 1,
            }
        )
    )

    scraper = CdpBridgeScraper(knowledge_base_root=root)
    scraper._load_index()
    scraper._index["new-id"] = {
        "id": "new-id",
        "title": "new",
        "path": "articles/new.md",
    }
    scraper._save_index()

    saved = json.loads((root / "index.json").read_text())
    assert saved["total"] == 2
    assert {article["id"] for article in saved["articles"]} == {"existing-id", "new-id"}
