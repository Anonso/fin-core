"""Tests for priority analysis job outbox."""

import json

import pytest

from fin_analyse.cognition.priority_articles import (
    PriorityAnalysisJob,
    PriorityAnalysisJobOutbox,
    PriorityArticleEvent,
)
from fin_analyse.utils.ids import stable_id

_ARTICLE_ID = "article_1"
_EVENT_ID = stable_id("priority_article", _ARTICLE_ID, prefix="pa:")
_JOB_ID = f"job_{_EVENT_ID.replace(':', '_')}_ypk"
_CREATED_AT = "2026-07-01T14:00:00+08:00"


class TestPriorityAnalysisJob:
    def test_job_has_required_steps(self):
        job = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first", "deep_read", "cross_article_synthesis", "portfolio_advice"],
        )
        assert "notify_first" in job.steps
        assert "deep_read" in job.steps
        assert job.urgency == "T0"

    def test_to_dict(self):
        job = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            title="星大派特刊: 半导体",
            user_id="ypk",
            urgency="T0",
            steps=["notify_first", "deep_read", "portfolio_advice"],
            column="星大派特刊",
        )
        d = job.to_dict()
        assert d["job_id"] == _JOB_ID
        assert d["urgency"] == "T0"
        assert "notify_first" in d["steps"]

    def test_from_dict(self):
        data = {
            "job_id": _JOB_ID,
            "event_id": _EVENT_ID,
            "article_id": _ARTICLE_ID,
            "title": "Test",
            "user_id": "ypk",
            "urgency": "T0",
            "steps": ["notify_first", "deep_read"],
            "created_at": "2026-07-01T14:00:00+08:00",
            "column": "星大派特刊",
            "metadata": {},
        }
        job = PriorityAnalysisJob.from_dict(data)
        assert job.job_id == _JOB_ID
        assert job.event_id == _EVENT_ID

    def test_from_dict_rejects_naive_created_at(self):
        data = {
            "job_id": _JOB_ID,
            "event_id": _EVENT_ID,
            "article_id": _ARTICLE_ID,
            "title": "Test",
            "user_id": "ypk",
            "urgency": "T0",
            "steps": ["notify_first", "deep_read"],
            "created_at": "2026-07-01T14:00:00",
            "column": "星大派特刊",
            "metadata": {},
        }

        with pytest.raises(ValueError, match="job is invalid"):
            PriorityAnalysisJob.from_dict(data)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("job_id", "job_wrong"),
            ("event_id", "pa:deadbeef0000"),
        ],
    )
    def test_from_dict_rejects_noncanonical_identity(self, field, value):
        data = {
            "job_id": _JOB_ID,
            "event_id": _EVENT_ID,
            "article_id": _ARTICLE_ID,
            "title": "Test",
            "user_id": "ypk",
            "urgency": "T0",
            "steps": ["notify_first", "deep_read"],
            "created_at": "2026-07-01T14:00:00+08:00",
            "column": "星大派特刊",
            "metadata": {},
        }
        data[field] = value

        with pytest.raises(ValueError, match="job is invalid"):
            PriorityAnalysisJob.from_dict(data)


class TestPriorityAnalysisJobOutbox:
    def test_append_and_list(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        outbox = PriorityAnalysisJobOutbox(outbox_path)

        job = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first", "deep_read"],
            created_at=_CREATED_AT,
        )
        assert outbox.append(job) is True

        jobs = outbox.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].job_id == _JOB_ID

    def test_dedup_by_event_user(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        outbox = PriorityAnalysisJobOutbox(outbox_path)

        job1 = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        job2 = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,  # Same event_id + user_id
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        assert outbox.append(job1) is True
        assert outbox.append(job2) is False  # Duplicate

    def test_different_user_same_event_allowed(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        outbox = PriorityAnalysisJobOutbox(outbox_path)

        job1 = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        job2 = PriorityAnalysisJob(
            job_id=f"job_{_EVENT_ID.replace(':', '_')}_other_user",
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="other_user",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        assert outbox.append(job1) is True
        assert outbox.append(job2) is True  # Different user

    def test_same_event_user_with_different_identity_fails_closed(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        outbox = PriorityAnalysisJobOutbox(outbox_path)
        first = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            title="original",
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        conflict = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            title="different",
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        assert outbox.append(first) is True

        with pytest.raises(ValueError, match="identity conflicts"):
            PriorityAnalysisJobOutbox(outbox_path).append(conflict)

    def test_list_jobs_rejects_committed_identity_conflict(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        first = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            title="original",
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        conflict = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            title="different",
            user_id="ypk",
            urgency="T0",
            steps=["notify_first", "deep_read"],
            created_at=_CREATED_AT,
        )
        outbox_path.write_text(
            "\n".join(
                json.dumps(job.to_dict(), ensure_ascii=False)
                for job in (first, conflict)
            )
            + "\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="identity conflicts"):
            PriorityAnalysisJobOutbox(outbox_path).list_jobs()

    def test_list_jobs_collapses_exact_committed_replay(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        job = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        line = json.dumps(job.to_dict(), ensure_ascii=False)
        outbox_path.write_text(f"{line}\n{line}\n", encoding="utf-8")

        assert PriorityAnalysisJobOutbox(outbox_path).list_jobs() == [job]

    def test_persistence_across_instances(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        outbox1 = PriorityAnalysisJobOutbox(outbox_path)
        event_id = stable_id("priority_article", "a1", prefix="pa:")
        job = PriorityAnalysisJob(
            job_id=f"job_{event_id.replace(':', '_')}_ypk",
            event_id=event_id,
            article_id="a1",
            user_id="ypk",
            urgency="T0",
            steps=["deep_read"],
            created_at=_CREATED_AT,
        )
        outbox1.append(job)

        # New instance reads from disk
        outbox2 = PriorityAnalysisJobOutbox(outbox_path)
        jobs = outbox2.list_jobs()
        assert len(jobs) == 1

    def test_append_fails_closed_when_final_record_is_torn(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        torn = b'{"event_id":"pa:abc123"'
        outbox_path.write_bytes(torn)
        outbox = PriorityAnalysisJobOutbox(outbox_path)
        job = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )

        with pytest.raises(ValueError, match="torn final record"):
            outbox.append(job)

        assert outbox_path.read_bytes() == torn
        assert outbox.list_jobs() == []

    def test_append_rejects_complete_json_without_commit_newline(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        job = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        raw = json.dumps(job.to_dict(), ensure_ascii=False).encode("utf-8")
        outbox_path.write_bytes(raw)
        outbox = PriorityAnalysisJobOutbox(outbox_path)

        with pytest.raises(ValueError, match="torn final record"):
            outbox.append(job)

        assert outbox_path.read_bytes() == raw
        assert outbox.list_jobs() == []

    def test_append_reloads_under_lock_before_deduping(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        job = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        first = PriorityAnalysisJobOutbox(outbox_path)
        stale = PriorityAnalysisJobOutbox(outbox_path)

        assert first.append(job) is True
        assert stale.append(job) is False
        assert len(PriorityAnalysisJobOutbox(outbox_path).list_jobs()) == 1

    def test_append_rejects_path_replacement_after_lock(self, tmp_path, monkeypatch):
        from fin_analyse.cognition import priority_articles

        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        job = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )
        outbox = PriorityAnalysisJobOutbox(outbox_path)
        flock = priority_articles.fcntl.flock
        replaced = False

        def replace_after_lock(descriptor, operation):
            nonlocal replaced
            result = flock(descriptor, operation)
            if operation & priority_articles.fcntl.LOCK_EX and not replaced:
                replaced = True
                replacement = outbox_path.with_name("replacement.jsonl")
                replacement.write_bytes(b"")
                replacement.replace(outbox_path)
            return result

        monkeypatch.setattr(priority_articles.fcntl, "flock", replace_after_lock)

        with pytest.raises(ValueError, match="path identity drifted"):
            outbox.append(job)

        assert outbox_path.read_bytes() == b""
        assert outbox.list_jobs() == []

    def test_outbox_does_not_follow_a_dangling_symlink(self, tmp_path):
        outbox_path = tmp_path / "priority_analysis_jobs.jsonl"
        outbox_path.symlink_to(tmp_path / "missing-jobs.jsonl")
        outbox = PriorityAnalysisJobOutbox(outbox_path)
        job = PriorityAnalysisJob(
            job_id=_JOB_ID,
            event_id=_EVENT_ID,
            article_id=_ARTICLE_ID,
            user_id="ypk",
            urgency="T0",
            steps=["notify_first"],
            created_at=_CREATED_AT,
        )

        with pytest.raises(ValueError, match="outbox is unsafe"):
            outbox.list_jobs()
        with pytest.raises(ValueError, match="outbox is unsafe"):
            outbox.append(job)
        assert outbox_path.is_symlink()
        assert not (tmp_path / "missing-jobs.jsonl").exists()


class TestCreateJobFromEvent:
    def test_creates_job_for_t0_event(self):
        event = PriorityArticleEvent(
            event_id="pa:test123",
            article_id="article_1",
            title="星大派特刊: AI芯片",
            priority_tier="T0",
            push_policy="always_push",
            push_reason="星大派 column: 星大派特刊",
            source_classification="teacher_original",
            persona_eligible=True,
            requires_deep_read=True,
            half_life_class="medium_logic",
            created_at="2026-07-01T14:00:00+08:00",
            metadata={"column": "星大派特刊", "score": 9.5},
        )

        job = PriorityAnalysisJob.from_event(
            event=event,
            user_id="ypk",
        )
        assert job.event_id == "pa:test123"
        assert job.urgency == "T0"
        assert "notify_first" in job.steps
        assert "deep_read" in job.steps
        assert "cross_article_synthesis" in job.steps
        assert "portfolio_advice" in job.steps
