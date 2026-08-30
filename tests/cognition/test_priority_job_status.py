"""Tests for PriorityJobStatus sink, health check, and Hermes↔FIN contract.

Covers:
- Status CRUD (append, list, latest_for_job)
- Health check detects pending/failed/done
- new 星大派 → no consumer ack → priority_dispatch_pending=True
- failed does not lose error/attempt
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from fin_analyse.utils.ids import stable_id


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_job(article_id: str = "art-test", title: str = "") -> dict:
    event_id = stable_id("priority_article", article_id, prefix="pa:")
    return {
        "job_id": f"job_{event_id.replace(':', '_')}_ypk",
        "event_id": event_id,
        "article_id": article_id,
        "title": title or "测试文章",
        "user_id": "ypk",
        "urgency": "T0",
        "steps": ["notify_first", "deep_read"],
        "created_at": _now_iso(),
        "column": "星大派特刊",
        "metadata": {},
    }


def _write_job_file(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for j in jobs:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")


# ── Status sink CRUD ──────────────────────────────────────────────────────────


def test_status_sink_append_and_list():
    """PriorityJobStatusSink must support append and list."""
    from fin_analyse.cognition.priority_articles import (
        PriorityJobStatus,
        PriorityJobStatusSink,
    )

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "status.jsonl"
        sink = PriorityJobStatusSink(path)
        job = _make_job("art-001")

        s = PriorityJobStatus(
            job_id=job["job_id"],
            event_id=job["event_id"],
            article_id=job["article_id"],
            user_id=job["user_id"],
            status="notified",
            attempt=1,
            updated_at=_now_iso(),
            consumer="hermes",
            delivery_target="feishu",
        )
        sink.append(s)

        entries = sink.list_statuses()
        assert len(entries) == 1
        assert entries[0].job_id == job["job_id"]
        assert entries[0].status == "notified"
        assert entries[0].consumer == "hermes"
        assert entries[0].delivery_target == "feishu"


def test_status_sink_latest_for_job():
    """latest_for_job returns most recent status for a given job_id."""
    from fin_analyse.cognition.priority_articles import (
        PriorityJobStatus,
        PriorityJobStatusSink,
    )

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "status.jsonl"
        sink = PriorityJobStatusSink(path)
        job = _make_job("art-002")

        # Write two status entries for same job
        sink.append(
            PriorityJobStatus(
                job_id=job["job_id"],
                event_id=job["event_id"],
                article_id=job["article_id"],
                status="notified",
                attempt=1,
                updated_at=_now_iso(),
                consumer="hermes",
                delivery_target="feishu",
            )
        )
        sink.append(
            PriorityJobStatus(
                job_id=job["job_id"],
                event_id=job["event_id"],
                article_id=job["article_id"],
                status="push_succeeded",
                attempt=1,
                updated_at=_now_iso(),
                consumer="hermes",
                delivery_target="feishu",
            )
        )

        latest = sink.latest_for_job(job["job_id"])
        assert latest is not None
        assert latest.status == "push_succeeded"

        none_result = sink.latest_for_job("nonexistent")
        assert none_result is None


def test_status_sink_requires_newline_commit_before_read_or_append(tmp_path):
    from fin_analyse.cognition.priority_articles import (
        PriorityJobStatus,
        PriorityJobStatusSink,
    )

    job = _make_job("torn-status")
    status = PriorityJobStatus(
        job_id=job["job_id"],
        event_id=job["event_id"],
        article_id=job["article_id"],
        user_id=job["user_id"],
        status="notified",
        attempt=1,
        updated_at=_now_iso(),
        consumer="hermes",
        delivery_target="feishu",
    )
    path = tmp_path / "status.jsonl"
    raw = json.dumps(status.to_dict(), ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw)
    sink = PriorityJobStatusSink(path)

    assert sink.list_statuses() == []
    with pytest.raises(ValueError, match="torn final record"):
        sink.append(status)
    assert path.read_bytes() == raw


def test_invalid_status_raises():
    """PriorityJobStatus with invalid status must raise ValueError."""
    from fin_analyse.cognition.priority_articles import PriorityJobStatus

    with pytest.raises(ValueError, match="Invalid job status"):
        PriorityJobStatus(
            job_id="job-x",
            status="not_a_real_status",
        )


def test_status_parser_allows_registered_v2_extensions_and_rejects_unknown():
    from fin_analyse.cognition.priority_articles import PriorityJobStatus

    job = _make_job("strict-status")
    valid = {
        "job_id": job["job_id"],
        "event_id": job["event_id"],
        "article_id": job["article_id"],
        "user_id": job["user_id"],
        "status": "notified",
        "attempt": 1,
        "updated_at": _now_iso(),
        "consumer": "hermes",
        "delivery_target": "feishu",
        "error": "",
    }

    assert PriorityJobStatus.from_dict(valid).to_dict() == valid
    with pytest.raises(ValueError, match="status entry is invalid"):
        PriorityJobStatus.from_dict({**valid, "unexpected": "field"})
    with pytest.raises(ValueError, match="status entry is invalid"):
        PriorityJobStatus.from_dict({**valid, "attempt": "1"})

    invalid_values = (
        {"attempt": True},
        {"attempt": 0},
        {"updated_at": "2026-08-09T12:00:00"},
        {"consumer": "hermes", "delivery_target": "internal"},
        {"event_id": stable_id("priority_article", "other", prefix="pa:")},
    )
    for invalid in invalid_values:
        with pytest.raises(ValueError, match="status entry is invalid"):
            PriorityJobStatus.from_dict({**valid, **invalid})

    # BUG-009: both registered v2 schema generations parse (six-field and
    # seven-field, matching the 20+19 production generations); synthetic
    # open-chat id — real ids never enter fixtures (rule 3).
    core_v2 = {
        **valid,
        "consumer": "priority_analysis_consumer_v2",
        "delivery_target": "feishu:oc_0123abcd",
    }
    gen7 = {
        "result_status": "ok",
        "article_analysis_status": "ok",
        "data_gaps": [],
        "operation_advice_blocked": False,
        "operation_advice_block_reason": "",
        "portfolio_advice_status": "ok",
        "result_classification": "analysis_partial_but_pushed",
    }
    gen6 = {k: gen7[k] for k in tuple(gen7)[:6]}
    gen6["operation_advice_blocked"] = None  # early generation wrote null
    parsed6 = PriorityJobStatus.from_dict({**core_v2, **gen6})
    parsed7 = PriorityJobStatus.from_dict({**core_v2, **gen7})
    assert parsed6.consumer == parsed7.consumer == "priority_analysis_consumer_v2"

    # v2 push claims never count as Hermes delivery evidence (invariant).
    assert parsed7.is_hermes_feishu is False
    assert parsed7.reports_feishu_push_succeeded is False
    pushed = PriorityJobStatus.from_dict({**core_v2, **gen7, "status": "push_succeeded"})
    assert pushed.reports_feishu_push_succeeded is False

    # to_dict() is lossy for extensions: core ten keys only, append-safe.
    assert pushed.to_dict() == {**core_v2, "status": "push_succeeded"}

    # Unregistered delivery form still rejects (fail-closed to new drift).
    with pytest.raises(ValueError, match="status entry is invalid"):
        PriorityJobStatus.from_dict({**core_v2, "delivery_target": "feishu:not_oc"})


def test_status_sink_isolates_bad_lines_and_health_surfaces_count():
    """BUG-009: one bad line no longer poisons the whole file; the count
    reaches PriorityDispatchHealth even when the skipped line was a job's
    LATEST line (latest-wins masking residual — the count is the tripwire)."""
    from fin_analyse.cognition.priority_articles import (
        PriorityJobStatus,
        PriorityJobStatusSink,
        check_priority_dispatch_health,
    )

    with TemporaryDirectory() as tmp:
        jobs_path = Path(tmp) / "jobs.jsonl"
        status_path = Path(tmp) / "status.jsonl"
        job = _make_job("art-bad")
        _write_job_file(jobs_path, [job])
        good = PriorityJobStatus(
            job_id=job["job_id"],
            event_id=job["event_id"],
            article_id=job["article_id"],
            user_id=job["user_id"],
            status="notified",
            attempt=1,
            updated_at=_now_iso(),
            consumer="hermes",
            delivery_target="feishu",
        )
        latest_unknown_key = {**good.to_dict(), "future_writer_field": 1}
        status_path.write_text(
            json.dumps(good.to_dict())
            + "\n"
            + json.dumps(latest_unknown_key)
            + "\n{not json\n"
        )
        sink = PriorityJobStatusSink(status_path)

        entries, bad = sink.list_statuses_with_health()
        assert len(entries) == 1
        assert bad == 2
        assert sink.list_statuses() == entries

        health = check_priority_dispatch_health(jobs_path=jobs_path, status_path=status_path)
        assert health.total_jobs == 1
        assert health.bad_status_entries == 2
        assert health.to_dict()["bad_status_entries"] == 2


def test_status_sink_does_not_follow_a_dangling_symlink(tmp_path):
    from fin_analyse.cognition.priority_articles import (
        PriorityJobStatus,
        PriorityJobStatusSink,
    )

    job = _make_job("symlink-status")
    status = PriorityJobStatus(
        job_id=job["job_id"],
        event_id=job["event_id"],
        article_id=job["article_id"],
        user_id=job["user_id"],
        status="notified",
        attempt=1,
        updated_at=_now_iso(),
        consumer="hermes",
        delivery_target="feishu",
    )
    path = tmp_path / "status.jsonl"
    path.symlink_to(tmp_path / "missing-target.jsonl")
    sink = PriorityJobStatusSink(path)

    with pytest.raises(ValueError, match="outbox is unsafe"):
        sink.list_statuses()
    with pytest.raises(ValueError, match="outbox is unsafe"):
        sink.append(status)
    assert path.is_symlink()
    assert not (tmp_path / "missing-target.jsonl").exists()


def test_status_sink_read_does_not_create_parent_directories(tmp_path):
    from fin_analyse.cognition.priority_articles import PriorityJobStatusSink

    parent = tmp_path / "missing" / "nested"
    sink = PriorityJobStatusSink(parent / "status.jsonl")

    assert sink.list_statuses() == []
    assert not parent.exists()


def test_status_to_dict_has_all_required_fields():
    """to_dict() must include all required fields."""
    from fin_analyse.cognition.priority_articles import PriorityJobStatus

    job = _make_job("art-003")
    s = PriorityJobStatus(
        job_id=job["job_id"],
        event_id=job["event_id"],
        article_id=job["article_id"],
        user_id=job["user_id"],
        status="analysis_succeeded",
        attempt=2,
        updated_at=_now_iso(),
        consumer="fin",
        delivery_target="internal",
        error="",
    )

    d = s.to_dict()
    for field in [
        "job_id",
        "event_id",
        "article_id",
        "user_id",
        "status",
        "attempt",
        "updated_at",
        "consumer",
        "delivery_target",
        "error",
    ]:
        assert field in d, f"Missing required field: {field}"


# ── Health check ──────────────────────────────────────────────────────────────


def test_health_no_jobs_returns_clean():
    """Empty jobs file → all zeros, no pending."""
    from fin_analyse.cognition.priority_articles import check_priority_dispatch_health

    with TemporaryDirectory() as tmp:
        jobs_path = Path(tmp) / "jobs.jsonl"
        status_path = Path(tmp) / "status.jsonl"
        jobs_path.write_text("")  # empty

        health = check_priority_dispatch_health(jobs_path=jobs_path, status_path=status_path)
        assert health.total_jobs == 0
        assert health.pending_jobs == 0
        assert health.failed_jobs == 0
        assert health.priority_dispatch_pending is False


def test_health_job_no_status_is_pending():
    """Job exists but no status → pending, priority_dispatch_pending=True."""
    from fin_analyse.cognition.priority_articles import check_priority_dispatch_health

    with TemporaryDirectory() as tmp:
        jobs_path = Path(tmp) / "jobs.jsonl"
        status_path = Path(tmp) / "status.jsonl"
        job = _make_job("art-p")
        _write_job_file(jobs_path, [job])

        health = check_priority_dispatch_health(jobs_path=jobs_path, status_path=status_path)

        assert health.total_jobs == 1
        assert health.pending_jobs == 1
        assert health.priority_dispatch_pending is True
        assert job["job_id"] in health.pending_job_ids


def test_health_job_push_succeeded_is_only_a_local_ack():
    from fin_analyse.cognition.priority_articles import (
        PriorityJobStatus,
        PriorityJobStatusSink,
        check_priority_dispatch_health,
    )

    with TemporaryDirectory() as tmp:
        jobs_path = Path(tmp) / "jobs.jsonl"
        status_path = Path(tmp) / "status.jsonl"
        job = _make_job("art-d")
        _write_job_file(jobs_path, [job])

        sink = PriorityJobStatusSink(status_path)
        sink.append(
            PriorityJobStatus(
                job_id=job["job_id"],
                event_id=job["event_id"],
                article_id=job["article_id"],
                status="push_succeeded",
                attempt=1,
                updated_at=_now_iso(),
                consumer="hermes",
                delivery_target="feishu",
            )
        )

        health = check_priority_dispatch_health(jobs_path=jobs_path, status_path=status_path)

        assert health.total_jobs == 1
        assert health.pending_jobs == 0
        assert health.completed_jobs == 1
        assert health.priority_dispatch_pending is False
        assert health.details[0]["status"] == "push_succeeded"
        assert health.details[0]["dispatch_status"] == "local_push_ack"


def test_health_job_failed_preserves_error_and_attempt():
    """Failed job must retain error and attempt count."""
    from fin_analyse.cognition.priority_articles import (
        PriorityJobStatus,
        PriorityJobStatusSink,
        check_priority_dispatch_health,
    )

    with TemporaryDirectory() as tmp:
        jobs_path = Path(tmp) / "jobs.jsonl"
        status_path = Path(tmp) / "status.jsonl"
        job = _make_job("art-f")
        _write_job_file(jobs_path, [job])

        sink = PriorityJobStatusSink(status_path)
        sink.append(
            PriorityJobStatus(
                job_id=job["job_id"],
                event_id=job["event_id"],
                article_id=job["article_id"],
                status="failed",
                attempt=3,
                updated_at=_now_iso(),
                consumer="hermes",
                delivery_target="feishu",
                error="API 500: upstream error",
            )
        )

        health = check_priority_dispatch_health(jobs_path=jobs_path, status_path=status_path)

        assert health.total_jobs == 1
        assert health.failed_jobs == 1
        assert health.priority_dispatch_pending is False  # failed is terminal
        assert job["job_id"] in health.failed_job_ids
        assert health.details[0]["status"] == "failed"
        assert health.details[0]["last_attempt"] == 3
        assert "API 500" in health.details[0]["last_error"]


def test_health_new_xingdapai_no_ack_is_pending():
    """E2E: new 星大派特刊 written to jobs → no Hermes ack →
    health check must report priority_dispatch_pending=True."""
    from fin_analyse.cognition.priority_articles import (
        PriorityAnalysisJob,
        PriorityAnalysisJobOutbox,
        PriorityArticleEvent,
        PriorityJobStatus,
        PriorityJobStatusSink,
        check_priority_dispatch_health,
    )

    with TemporaryDirectory() as tmp:
        jobs_path = Path(tmp) / "jobs.jsonl"
        status_path = Path(tmp) / "status.jsonl"

        # Simulate: new 星大派事件写入 jobs
        event = PriorityArticleEvent(
            event_id=stable_id("priority_article", "art-xdp-001", prefix="pa:"),
            article_id="art-xdp-001",
            title="星大派特刊：稀土永磁最新研判",
            priority_tier="T0",
            push_policy="always_push",
            push_reason="星大派 column: 星大派特刊",
            source_classification="teacher_original",
            persona_eligible=True,
            requires_deep_read=True,
            half_life_class="medium_logic",
            created_at=_now_iso(),
            metadata={"column": "星大派特刊"},
        )

        outbox = PriorityAnalysisJobOutbox(jobs_path)
        job = PriorityAnalysisJob.from_event(event, user_id="ypk")
        outbox.append(job)

        # No Hermes status written yet → must be pending
        health = check_priority_dispatch_health(jobs_path=jobs_path, status_path=status_path)
        assert health.priority_dispatch_pending is True
        assert health.pending_jobs == 1
        assert health.total_jobs == 1

        # Hermes acks push_succeeded → done
        sink = PriorityJobStatusSink(status_path)
        sink.append(
            PriorityJobStatus(
                job_id=job.job_id,
                event_id=event.event_id,
                article_id="art-xdp-001",
                user_id="ypk",
                status="push_succeeded",
                attempt=1,
                updated_at=_now_iso(),
                consumer="hermes",
                delivery_target="feishu",
            )
        )

        health2 = check_priority_dispatch_health(jobs_path=jobs_path, status_path=status_path)
        assert health2.priority_dispatch_pending is False
        assert health2.completed_jobs == 1


def test_health_ignores_an_uncommitted_foreign_push_status(tmp_path):
    from fin_analyse.cognition.priority_articles import check_priority_dispatch_health

    jobs_path = tmp_path / "jobs.jsonl"
    status_path = tmp_path / "status.jsonl"
    job = _make_job("real-article")
    _write_job_file(jobs_path, [job])
    foreign_article_id = "foreign-article"
    foreign_event_id = stable_id("priority_article", foreign_article_id, prefix="pa:")
    status_path.write_text(
        json.dumps(
            {
                "job_id": job["job_id"],
                "event_id": foreign_event_id,
                "article_id": foreign_article_id,
                "user_id": job["user_id"],
                "status": "push_succeeded",
                "attempt": 1,
                "updated_at": _now_iso(),
                "consumer": "hermes",
                "delivery_target": "feishu",
                "error": "",
            }
        ),
        encoding="utf-8",
    )

    health = check_priority_dispatch_health(
        jobs_path=jobs_path,
        status_path=status_path,
    )

    assert health.completed_jobs == 0
    assert health.pending_jobs == 1
    assert health.priority_dispatch_pending is True


def test_health_does_not_treat_fin_internal_status_as_feishu_delivery(tmp_path):
    from fin_analyse.cognition.priority_articles import (
        PriorityJobStatus,
        PriorityJobStatusSink,
        check_priority_dispatch_health,
    )

    jobs_path = tmp_path / "jobs.jsonl"
    status_path = tmp_path / "status.jsonl"
    job = _make_job("internal-only")
    _write_job_file(jobs_path, [job])
    PriorityJobStatusSink(status_path).append(
        PriorityJobStatus(
            job_id=job["job_id"],
            event_id=job["event_id"],
            article_id=job["article_id"],
            user_id=job["user_id"],
            status="push_succeeded",
            attempt=1,
            updated_at=_now_iso(),
            consumer="fin",
            delivery_target="internal",
        )
    )

    health = check_priority_dispatch_health(
        jobs_path=jobs_path,
        status_path=status_path,
    )

    assert health.completed_jobs == 0
    assert health.pending_jobs == 1
    assert health.priority_dispatch_pending is True


def test_health_analysis_succeeded_is_analysis_partial_but_pushed():
    """analysis_succeeded status without push_succeeded → analysis_partial_but_pushed."""
    from fin_analyse.cognition.priority_articles import (
        PriorityJobStatus,
        PriorityJobStatusSink,
        check_priority_dispatch_health,
    )

    with TemporaryDirectory() as tmp:
        jobs_path = Path(tmp) / "jobs.jsonl"
        status_path = Path(tmp) / "status.jsonl"
        job = _make_job("art-ap")
        _write_job_file(jobs_path, [job])

        sink = PriorityJobStatusSink(status_path)
        sink.append(
            PriorityJobStatus(
                job_id=job["job_id"],
                event_id=job["event_id"],
                article_id=job["article_id"],
                status="analysis_succeeded",
                attempt=1,
                updated_at=_now_iso(),
                consumer="hermes",
                delivery_target="feishu",
            )
        )

        health = check_priority_dispatch_health(jobs_path=jobs_path, status_path=status_path)
        assert health.analysis_partial_but_pushed == 1
        assert health.priority_dispatch_pending is True
        assert job["job_id"] in health.analysis_partial_job_ids


def test_health_push_succeeded_takes_priority_over_analysis_succeeded():
    """If both analysis_succeeded and push_succeeded exist, push_succeeded wins."""
    from fin_analyse.cognition.priority_articles import (
        PriorityJobStatus,
        PriorityJobStatusSink,
        check_priority_dispatch_health,
    )

    with TemporaryDirectory() as tmp:
        jobs_path = Path(tmp) / "jobs.jsonl"
        status_path = Path(tmp) / "status.jsonl"
        job = _make_job("art-d2")
        _write_job_file(jobs_path, [job])

        sink = PriorityJobStatusSink(status_path)
        sink.append(
            PriorityJobStatus(
                job_id=job["job_id"],
                event_id=job["event_id"],
                article_id=job["article_id"],
                status="notified",
                attempt=1,
                updated_at=_now_iso(),
                consumer="hermes",
                delivery_target="feishu",
            )
        )
        sink.append(
            PriorityJobStatus(
                job_id=job["job_id"],
                event_id=job["event_id"],
                article_id=job["article_id"],
                status="analysis_succeeded",
                attempt=1,
                updated_at=_now_iso(),
                consumer="hermes",
                delivery_target="feishu",
            )
        )
        sink.append(
            PriorityJobStatus(
                job_id=job["job_id"],
                event_id=job["event_id"],
                article_id=job["article_id"],
                status="push_succeeded",
                attempt=1,
                updated_at=_now_iso(),
                consumer="hermes",
                delivery_target="feishu",
            )
        )

        health = check_priority_dispatch_health(jobs_path=jobs_path, status_path=status_path)
        assert health.completed_jobs == 1
        assert health.analysis_partial_but_pushed == 0
        assert health.priority_dispatch_pending is False
