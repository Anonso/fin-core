"""Platform-neutral priority article event system.

Interprets gated evidence and writes deduped events to a JSONL outbox
for any delivery adapter (Hermes, webhook, etc.) to consume.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from fin_analyse.cognition.models import EvidenceItem
from fin_analyse.cognition.persona_gate import (
    METHODOLOGY_MARKERS,
    _column,
    _is_good_question,
    _is_star_column,
    _marker_hits,
)
from fin_analyse.utils.ids import stable_id

PRIORITY_OUTBOX_NAME = "priority_events.jsonl"


def _write_jsonl_record(handle: BinaryIO, value: dict[str, Any]) -> None:
    """Append one durable record through an already locked file handle."""
    record = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    handle.seek(0, os.SEEK_END)
    handle.write(record)
    handle.flush()
    os.fsync(handle.fileno())


def _committed_jsonl_lines(raw: bytes) -> list[str]:
    """Return newline-committed frames; an unterminated tail is not durable."""
    if raw and not raw.endswith(b"\n"):
        final_newline = raw.rfind(b"\n")
        raw = raw[: final_newline + 1] if final_newline >= 0 else b""
    return raw.decode("utf-8").splitlines()


def _require_appendable_jsonl(raw: bytes) -> None:
    if raw and not raw.endswith(b"\n"):
        raise ValueError("priority outbox has a torn final record")


def _is_aware_iso_datetime(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _require_outbox_path_identity(
    filename: str,
    handle: BinaryIO,
    parent_descriptor: int,
) -> None:
    try:
        opened = os.fstat(handle.fileno())
        named = os.stat(
            filename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ValueError("priority outbox path identity drifted") from error
    if (
        not stat.S_ISREG(opened.st_mode)
        or
        not stat.S_ISREG(named.st_mode)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise ValueError("priority outbox path identity drifted")


def _open_outbox_parent(path: Path, *, create: bool) -> tuple[int, str] | None:
    """Open every parent component without following links."""
    absolute = Path(os.path.abspath(path))
    filename = absolute.name
    if not filename:
        raise ValueError("priority outbox is unsafe")

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in absolute.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise ValueError("priority outbox is unsafe") from error
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                raise
            except OSError as error:
                raise ValueError("priority outbox is unsafe") from error
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, filename


def _read_jsonl_bytes(path: Path) -> bytes:
    """Read committed JSONL through one shared-locked, path-bound fd."""
    parent = _open_outbox_parent(path, create=False)
    if parent is None:
        return b""
    parent_descriptor, filename = parent
    try:
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return b""
        except OSError as error:
            raise ValueError("priority outbox is unsafe") from error
        with os.fdopen(descriptor, "rb") as handle:
            _require_outbox_path_identity(filename, handle, parent_descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            _require_outbox_path_identity(filename, handle, parent_descriptor)
            raw = handle.read()
            _require_outbox_path_identity(filename, handle, parent_descriptor)
            return raw
    finally:
        os.close(parent_descriptor)


def _append_jsonl(
    path: Path,
    value: dict[str, Any],
    *,
    admit: Callable[[bytes], bool] | None = None,
) -> bool:
    """Append and fsync one newline-committed frame through a safe fd."""
    parent = _open_outbox_parent(path, create=True)
    assert parent is not None
    parent_descriptor, filename = parent
    try:
        try:
            descriptor = os.open(
                filename,
                os.O_RDWR
                | os.O_APPEND
                | os.O_CREAT
                | os.O_CLOEXEC
                | os.O_NOFOLLOW
                | os.O_NONBLOCK,
                0o600,
                dir_fd=parent_descriptor,
            )
        except OSError as error:
            raise ValueError("priority outbox is unsafe") from error
        # BUG-014：追加路径补 chmod——已存在文件不因创建 mode 收权，
        # 漂移（如 0664）在下一次写入时收敛回 owner-only。
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            _require_outbox_path_identity(filename, handle, parent_descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _require_outbox_path_identity(filename, handle, parent_descriptor)
            handle.seek(0)
            raw = handle.read()
            _require_outbox_path_identity(filename, handle, parent_descriptor)
            _require_appendable_jsonl(raw)
            if admit is not None and not admit(raw):
                return False
            _write_jsonl_record(handle, value)
            _require_outbox_path_identity(filename, handle, parent_descriptor)
            os.fsync(parent_descriptor)
            _require_outbox_path_identity(filename, handle, parent_descriptor)
            return True
    finally:
        os.close(parent_descriptor)


@dataclass(frozen=True)
class PriorityArticleEvent:
    """A platform-neutral priority article event for delivery routing."""

    event_id: str
    article_id: str
    title: str
    priority_tier: str
    push_policy: str
    push_reason: str
    source_classification: str
    persona_eligible: bool
    requires_deep_read: bool
    half_life_class: str
    created_at: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "article_id": self.article_id,
            "title": self.title,
            "priority_tier": self.priority_tier,
            "push_policy": self.push_policy,
            "push_reason": self.push_reason,
            "source_classification": self.source_classification,
            "persona_eligible": self.persona_eligible,
            "requires_deep_read": self.requires_deep_read,
            "half_life_class": self.half_life_class,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PriorityArticleEvent:
        fields = {
            "event_id",
            "article_id",
            "title",
            "priority_tier",
            "push_policy",
            "push_reason",
            "source_classification",
            "persona_eligible",
            "requires_deep_read",
            "half_life_class",
            "created_at",
            "metadata",
        }
        string_fields = fields - {
            "persona_eligible",
            "requires_deep_read",
            "metadata",
        }
        if (
            not isinstance(data, dict)
            or set(data) != fields
            or any(type(data[field]) is not str for field in string_fields)
            or not data["article_id"]
            or data["event_id"]
            != stable_id("priority_article", data["article_id"], prefix="pa:")
            or data["priority_tier"] not in {"T0", "T1"}
            or data["push_policy"] not in {"always_push", "judge_positive"}
            or type(data["persona_eligible"]) is not bool
            or type(data["requires_deep_read"]) is not bool
            or not isinstance(data["metadata"], dict)
        ):
            raise ValueError("priority article event is invalid")
        return cls(
            event_id=data["event_id"],
            article_id=data["article_id"],
            title=data["title"],
            priority_tier=data["priority_tier"],
            push_policy=data["push_policy"],
            push_reason=data["push_reason"],
            source_classification=data["source_classification"],
            persona_eligible=data["persona_eligible"],
            requires_deep_read=data["requires_deep_read"],
            half_life_class=data["half_life_class"],
            created_at=data["created_at"],
            metadata=dict(data["metadata"]),
        )


class RuleBasedGoodQuestionJudge:
    """Judge whether a Q&A article has enough methodology value to push."""

    # Minimum character count for a "detailed reply" in a Q&A article
    DETAILED_REPLY_CHAR_THRESHOLD = 800

    # Industry chain analysis markers — suggesting deep analysis content
    INDUSTRY_CHAIN_MARKERS = (
        "产业链",
        "上游",
        "中游",
        "下游",
        "供给",
        "供需",
        "产能",
        "扩产",
        "涨价",
        "降价",
        "毛利率",
        "净利率",
        "市场份额",
        "国产替代",
        "卡脖子",
        "技术路线",
        "成本",
        "竞争格局",
        "行业集中度",
        "政策",
        "壁垒",
        "门槛",
        "景气",
    )

    def should_push(self, evidence: EvidenceItem) -> tuple[bool, list[str]]:
        """Return (should_push, reason_labels)."""
        text = f"{evidence.title}\n{evidence.content}"
        methodology_hits = _marker_hits(text, METHODOLOGY_MARKERS)
        reasons: list[str] = []

        if len(methodology_hits) >= 2:
            reasons.append("methodology")

        # S-028: Check for detailed teacher reply in Q&A articles
        # These are "普通" column articles where the teacher gave a long,
        # detailed industry chain / methodology analysis in response to a question
        char_count = evidence.metadata.get("char_count", 0)
        if isinstance(char_count, str):
            try:
                char_count = int(char_count)
            except (ValueError, TypeError):
                char_count = 0

        if char_count >= self.DETAILED_REPLY_CHAR_THRESHOLD:
            chain_hits = _marker_hits(text, self.INDUSTRY_CHAIN_MARKERS)
            if len(chain_hits) >= 3:
                reasons.append("detailed_reply")
                reasons.append(f"industry_chain({len(chain_hits)} markers)")
            elif len(methodology_hits) >= 1 and len(chain_hits) >= 1:
                reasons.append("detailed_reply")
                reasons.append("methodology+chain")

        if reasons:
            return True, reasons
        return False, []


class PriorityEventOutbox:
    """Deduped JSONL outbox for priority article events."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @staticmethod
    def _identities(raw: bytes) -> dict[str, tuple[object, ...]]:
        seen: dict[str, tuple[object, ...]] = {}
        for line in _committed_jsonl_lines(raw):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
                event = PriorityArticleEvent.from_dict(data)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            identity = _priority_event_identity(event)
            previous = seen.get(event.event_id)
            if previous is not None and previous != identity:
                raise ValueError("priority article event identity conflicts")
            seen[event.event_id] = identity
        return seen

    def append(self, event: PriorityArticleEvent) -> bool:
        """Append an event. Returns False if it's a duplicate."""
        PriorityArticleEvent.from_dict(event.to_dict())
        identity = _priority_event_identity(event)

        def admit(raw: bytes) -> bool:
            previous = self._identities(raw).get(event.event_id)
            if previous is not None:
                if previous != identity:
                    raise ValueError("priority article event identity conflicts")
                return False
            return True

        return _append_jsonl(self._path, event.to_dict(), admit=admit)

    def list_events(self) -> list[PriorityArticleEvent]:
        """Return all events currently in the outbox."""
        events: list[PriorityArticleEvent] = []
        for line in _committed_jsonl_lines(_read_jsonl_bytes(self._path)):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
                events.append(PriorityArticleEvent.from_dict(data))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return events


def _priority_event_identity(event: PriorityArticleEvent) -> tuple[object, ...]:
    return (
        event.article_id,
        event.title,
        event.priority_tier,
        event.push_policy,
        event.push_reason,
        event.source_classification,
        event.persona_eligible,
        event.requires_deep_read,
        event.half_life_class,
        json.dumps(event.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


class PriorityArticleInterpreter:
    """Interpret gated evidence and produce priority article events."""

    def interpret(self, evidence: EvidenceItem) -> PriorityArticleEvent | None:
        """Return a PriorityArticleEvent if the evidence qualifies, else None."""
        metadata = evidence.metadata
        source_id = evidence.source_id
        column = _column(evidence)

        # Tier 0: 星大派 columns — always push, requires deep read
        if _is_star_column(column):
            return PriorityArticleEvent(
                event_id=stable_id("priority_article", source_id, prefix="pa:"),
                article_id=source_id,
                title=evidence.title,
                priority_tier="T0",
                push_policy="always_push",
                push_reason=f"星大派 column: {column}",
                source_classification=metadata.get("source_classification", "teacher_original"),
                persona_eligible=metadata.get("persona_eligible", True),
                requires_deep_read=True,
                half_life_class=metadata.get("half_life_class", "medium_logic"),
                created_at=evidence.collected_at,
                metadata=dict(metadata),
            )

        # Tier 1: score >= 9.0 — research reference, not persona-eligible
        score = _score(evidence)
        if score is not None and score >= 9.0:
            return PriorityArticleEvent(
                event_id=stable_id("priority_article", source_id, prefix="pa:"),
                article_id=source_id,
                title=evidence.title,
                priority_tier="T1",
                push_policy="always_push",
                push_reason=f"score >= 9.0+ (score={score})",
                source_classification=metadata.get("source_classification", "research_reference"),
                persona_eligible=False,
                requires_deep_read=True,
                half_life_class=metadata.get("half_life_class", "medium_logic"),
                created_at=evidence.collected_at,
                metadata=dict(metadata),
            )

        # Good questions: subject to content-based judgement
        if _is_good_question(evidence, column):
            judge = RuleBasedGoodQuestionJudge()
            should_push, reasons = judge.should_push(evidence)
            if should_push:
                return PriorityArticleEvent(
                    event_id=stable_id("priority_article", source_id, prefix="pa:"),
                    article_id=source_id,
                    title=evidence.title,
                    priority_tier="T0",
                    push_policy="judge_positive",
                    push_reason="好问题; " + "; ".join(reasons),
                    source_classification=metadata.get(
                        "source_classification", "teacher_methodology"
                    ),
                    persona_eligible=metadata.get("persona_eligible", True),
                    requires_deep_read=True,
                    half_life_class=metadata.get("half_life_class", "medium_logic"),
                    created_at=evidence.collected_at,
                    metadata=dict(metadata),
                )

        return None


def scan_articles_for_priority(
    article_dir: Path,
    *,
    date_prefix: str = "",
    limit: int = 100,
    dry_run: bool = False,
    outbox_path: str | Path | None = None,
) -> dict[str, object]:
    """Scan article markdown files and produce priority events.

    Args:
        article_dir: Path to knowledge-base/articles/.
        date_prefix: Only scan articles whose filename starts with this prefix.
        limit: Maximum articles to scan.
        dry_run: If True, dont write to outbox.
        outbox_path: Path to priority_events.jsonl. Required if not dry_run.

    Returns:
        Dict with scanned, events_created, duplicates, skipped, events.
    """
    from fin_analyse.cognition.backfill import _markdown_to_evidence, _parse_markdown
    from fin_analyse.cognition.persona_gate import apply_persona_gate

    interpreter = PriorityArticleInterpreter()
    outbox: PriorityEventOutbox | None = None
    if not dry_run and outbox_path:
        outbox = PriorityEventOutbox(Path(outbox_path))

    scanned = 0
    events_created = 0
    duplicates = 0
    skipped = 0
    events: list[dict[str, object]] = []

    # Normalize date prefix: support both "2026-06-29" and "20260629"
    date_prefixes: list[str] = []
    if date_prefix:
        date_prefixes.append(date_prefix)
        if "-" in date_prefix:
            date_prefixes.append(date_prefix.replace("-", ""))
        else:
            # Try inserting hyphens for YYYYMMDD format
            if len(date_prefix) == 8 and date_prefix.isdigit():
                date_prefixes.append(f"{date_prefix[:4]}-{date_prefix[4:6]}-{date_prefix[6:8]}")

    if article_dir.is_dir():
        files = sorted(article_dir.glob("*.md"), reverse=True)
        for filepath in files:
            if date_prefixes and not any(filepath.name.startswith(dp) for dp in date_prefixes):
                continue
            if scanned >= limit:
                break
            scanned += 1
            data = _parse_markdown(filepath)
            if data is None:
                skipped += 1
                continue
            evidence = _markdown_to_evidence(filepath, data, teacher_id="guo")
            if evidence is None:
                skipped += 1
                continue
            gated = apply_persona_gate(evidence)
            event = interpreter.interpret(gated)
            if event is None:
                skipped += 1
                continue

            if outbox and outbox.append(event):
                events_created += 1
            elif outbox:
                duplicates += 1
            else:
                events_created += 1

            events.append(event.to_dict())

    return {
        "scanned": scanned,
        "events_created": events_created,
        "duplicates": duplicates,
        "skipped": skipped,
        "dry_run": dry_run,
        "events": events,
    }


def _score(evidence: EvidenceItem) -> float | None:
    """Extract a numeric score from evidence metadata, or None."""
    raw = evidence.metadata.get("score")
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


# ── Priority Analysis Job Outbox ──────────────────────────────

ANALYSIS_JOBS_OUTBOX_NAME = "priority_analysis_jobs.jsonl"

# Default analysis steps for a T0 priority article
DEFAULT_T0_ANALYSIS_STEPS = [
    "notify_first",
    "deep_read",
    "cross_article_synthesis",
    "portfolio_advice",
]


@dataclass
class PriorityAnalysisJob:
    """A single analysis job created from a priority article event."""

    job_id: str
    event_id: str
    article_id: str
    title: str = ""
    user_id: str = "ypk"
    urgency: str = "T0"
    steps: list[str] = field(default_factory=lambda: list(DEFAULT_T0_ANALYSIS_STEPS))
    created_at: str = ""
    column: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "event_id": self.event_id,
            "article_id": self.article_id,
            "title": self.title,
            "user_id": self.user_id,
            "urgency": self.urgency,
            "steps": list(self.steps),
            "created_at": self.created_at,
            "column": self.column,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PriorityAnalysisJob:
        fields = {
            "job_id",
            "event_id",
            "article_id",
            "title",
            "user_id",
            "urgency",
            "steps",
            "created_at",
            "column",
            "metadata",
        }
        string_fields = fields - {"steps", "metadata"}
        if (
            not isinstance(data, dict)
            or set(data) != fields
            or any(type(data[field]) is not str for field in string_fields)
            or not data["article_id"]
            or not data["user_id"]
            or data["event_id"]
            != stable_id("priority_article", data["article_id"], prefix="pa:")
            or data["job_id"] != _priority_analysis_job_id(data["event_id"], data["user_id"])
            or data["urgency"] not in {"T0", "T1"}
            or not _is_aware_iso_datetime(data["created_at"])
            or not isinstance(data["steps"], list)
            or not data["steps"]
            or any(type(step) is not str or not step for step in data["steps"])
            or not isinstance(data["metadata"], dict)
        ):
            raise ValueError("priority analysis job is invalid")
        return cls(
            job_id=data["job_id"],
            event_id=data["event_id"],
            article_id=data["article_id"],
            title=data["title"],
            user_id=data["user_id"],
            urgency=data["urgency"],
            steps=list(data["steps"]),
            created_at=data["created_at"],
            column=data["column"],
            metadata=dict(data["metadata"]),
        )

    @classmethod
    def from_event(
        cls,
        event: PriorityArticleEvent,
        user_id: str = "ypk",
    ) -> PriorityAnalysisJob:
        """Create a job from a priority article event."""
        from datetime import datetime, timedelta, timezone

        tz = timezone(timedelta(hours=8))
        created_at = datetime.now(tz).isoformat()

        return cls(
            job_id=_priority_analysis_job_id(event.event_id, user_id),
            event_id=event.event_id,
            article_id=event.article_id,
            title=event.title,
            user_id=user_id,
            urgency=event.priority_tier,
            steps=list(DEFAULT_T0_ANALYSIS_STEPS),
            created_at=created_at,
            column=event.metadata.get("column", ""),
            metadata=dict(event.metadata),
        )


class PriorityAnalysisJobOutbox:
    """Deduped JSONL outbox for priority analysis jobs."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @staticmethod
    def _identities(raw: bytes) -> dict[str, tuple[object, ...]]:
        seen: dict[str, tuple[object, ...]] = {}
        for line in _committed_jsonl_lines(raw):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
                job = PriorityAnalysisJob.from_dict(data)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError("priority analysis job record is invalid") from error
            # Dedup by event_id + user_id, but never let a valid conflicting
            # row silently suppress the expected repair job.
            key = f"{job.event_id}:{job.user_id}"
            identity = _priority_job_identity(job)
            previous = seen.get(key)
            if previous is not None and previous != identity:
                raise ValueError("priority analysis job identity conflicts")
            seen[key] = identity
        return seen

    def append(self, job: PriorityAnalysisJob) -> bool:
        """Append a job. Returns False if it's a duplicate."""
        PriorityAnalysisJob.from_dict(job.to_dict())
        key = f"{job.event_id}:{job.user_id}"
        identity = _priority_job_identity(job)

        def admit(raw: bytes) -> bool:
            previous = self._identities(raw).get(key)
            if previous is not None:
                if previous != identity:
                    raise ValueError("priority analysis job identity conflicts")
                return False
            return True

        return _append_jsonl(self._path, job.to_dict(), admit=admit)

    def list_jobs(self) -> list[PriorityAnalysisJob]:
        """Return all jobs currently in the outbox."""
        raw = _read_jsonl_bytes(self._path)
        self._identities(raw)
        jobs: list[PriorityAnalysisJob] = []
        seen: set[str] = set()
        for line in _committed_jsonl_lines(raw):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
                job = PriorityAnalysisJob.from_dict(data)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError("priority analysis job record is invalid") from error
            key = f"{job.event_id}:{job.user_id}"
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job)
        return jobs


def _priority_job_identity(job: PriorityAnalysisJob) -> tuple[object, ...]:
    """Stable replay identity; creation time is intentionally not part of dedupe."""
    return (
        job.article_id,
        job.title,
        job.urgency,
        tuple(job.steps),
        job.column,
        json.dumps(job.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _priority_analysis_job_id(event_id: str, user_id: str) -> str:
    return f"job_{event_id.replace(':', '_')}_{user_id}"


# ── Priority Job Status (FIN↔Hermes shared contract) ────────────────────────

JOB_STATUS_SINK_NAME = "priority_analysis_job_status.jsonl"

# Valid status transitions
VALID_JOB_STATUSES = frozenset(
    {
        "notified",
        "analysis_started",
        "analysis_succeeded",
        "push_succeeded",
        "failed",
    }
)

TERMINAL_STATUSES = frozenset({"push_succeeded", "failed"})

# Fields required in every status entry
REQUIRED_STATUS_FIELDS = frozenset(
    {
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
    }
)


# BUG-009 read-side tolerance (design priority-status-read-tolerance): the
# retired Hermes-side v2 consumer appended structured fields the repo contract
# never declared.  Registered here so the 39 historical lines (20× six-field
# generation + 19× seven-field, read-only probe 2026-08-30) stay readable; any
# OTHER extra key still rejects the entry — fail-closed to new drift, new
# writers must register first.  Values are ignored on read (observed shapes:
# null / bool / str / list); ``result_classification`` is a writer self-label
# and never feeds health aggregation.  ``to_dict()`` emits the core ten keys
# only (lossy for extensions) and ``append()`` never writes them.
_V2_EXTENSION_FIELDS: frozenset[str] = frozenset(
    {
        "result_status",
        "article_analysis_status",
        "data_gaps",
        "operation_advice_blocked",
        "operation_advice_block_reason",
        "portfolio_advice_status",
        "result_classification",
    }
)
# Registered v2 delivery-target form ("feishu:" + open-chat id), verified
# against every historical line (39/39 probe).  ``is_hermes_feishu`` still
# recognises only ("hermes", "feishu"), so v2 push claims are never counted
# as Hermes delivery evidence.
_V2_DELIVERY_TARGET_RE = re.compile(r"^feishu:oc_[0-9a-f]+$")


@dataclass
class PriorityJobStatus:
    """A single status/ack entry written by Hermes (or FIN internal).

    Key contract: Hermes appends one status line per key step.
    FIN reads all status lines to judge pending/failed/done.
    """

    job_id: str
    event_id: str = ""
    article_id: str = ""
    user_id: str = "ypk"
    status: str = "notified"
    attempt: int = 1
    updated_at: str = ""
    consumer: str = ""  # "hermes" | "fin"
    delivery_target: str = ""  # e.g. "feishu" | "mcp" | "internal"
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in VALID_JOB_STATUSES:
            raise ValueError(
                f"Invalid job status: {self.status!r}, must be one of {sorted(VALID_JOB_STATUSES)}"
            )
        text_values = (
            self.job_id,
            self.event_id,
            self.article_id,
            self.user_id,
            self.status,
            self.updated_at,
            self.consumer,
            self.delivery_target,
            self.error,
        )
        try:
            updated_at = datetime.fromisoformat(self.updated_at)
        except (TypeError, ValueError):
            updated_at = None
        if (
            any(type(value) is not str for value in text_values)
            or not self.article_id
            or not self.user_id
            or self.event_id
            != stable_id("priority_article", self.article_id, prefix="pa:")
            or self.job_id != _priority_analysis_job_id(self.event_id, self.user_id)
            or type(self.attempt) is not int
            or self.attempt <= 0
            or updated_at is None
            or updated_at.tzinfo is None
            or updated_at.utcoffset() is None
            or (
                (self.consumer, self.delivery_target)
                not in {("hermes", "feishu"), ("fin", "internal")}
                and not (
                    self.consumer == "priority_analysis_consumer_v2"
                    and _V2_DELIVERY_TARGET_RE.fullmatch(self.delivery_target) is not None
                )
            )
        ):
            raise ValueError("priority job status entry is invalid")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def matches_job(self, job: PriorityAnalysisJob) -> bool:
        """Return whether this acknowledgement names the exact durable job."""
        return (
            self.job_id == job.job_id
            and self.event_id == job.event_id
            and self.article_id == job.article_id
            and self.user_id == job.user_id
        )

    @property
    def is_hermes_feishu(self) -> bool:
        return self.consumer == "hermes" and self.delivery_target == "feishu"

    @property
    def reports_feishu_push_succeeded(self) -> bool:
        """Return a local Hermes status claim, not live delivery evidence."""
        return self.status == "push_succeeded" and self.is_hermes_feishu

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "event_id": self.event_id,
            "article_id": self.article_id,
            "user_id": self.user_id,
            "status": self.status,
            "attempt": self.attempt,
            "updated_at": self.updated_at,
            "consumer": self.consumer,
            "delivery_target": self.delivery_target,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PriorityJobStatus:
        # BUG-009: required keys must all be present; registered v2 extension
        # keys may ride along (ignored); anything else still rejects.
        if not isinstance(data, dict) or not REQUIRED_STATUS_FIELDS <= set(data):
            raise ValueError("priority job status entry is invalid")
        if not set(data) <= REQUIRED_STATUS_FIELDS | _V2_EXTENSION_FIELDS:
            raise ValueError("priority job status entry is invalid")
        return cls(
            job_id=data["job_id"],
            event_id=data["event_id"],
            article_id=data["article_id"],
            user_id=data["user_id"],
            status=data["status"],
            attempt=data["attempt"],
            updated_at=data["updated_at"],
            consumer=data["consumer"],
            delivery_target=data["delivery_target"],
            error=data["error"],
        )


class PriorityJobStatusSink:
    """Append-only JSONL sink for priority job status/ack entries.

    Hermes writes status lines here; FIN reads them to determine
    dispatch health (pending/failed/done).

    Usage::

        sink = PriorityJobStatusSink(Path("knowledge-base/runtime/cognition/priority_analysis_job_status.jsonl"))
        status = PriorityJobStatus(
            job_id="job_pa:art_001_ypk", event_id="pa:art_001",
            article_id="art_001", user_id="ypk", status="notified",
            attempt=1, consumer="hermes", delivery_target="feishu",
        )
        sink.append(status)
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def append(self, status: PriorityJobStatus) -> None:
        """Append a status entry. Always appends — idempotency is the
        caller's responsibility (Hermes tracks its own retries)."""
        PriorityJobStatus.from_dict(status.to_dict())
        _append_jsonl(self._path, status.to_dict())

    def list_statuses_with_health(self) -> tuple[list[PriorityJobStatus], int]:
        """Return ``(entries, bad_count)`` with per-line typed isolation.

        Bad = JSON decode failure, missing required keys, unregistered
        extension keys, or value-validation failure — each isolated to its own
        line so one bad record cannot poison the whole file (BUG-009: the old
        whole-file atomic reject made a single unreadable line hide all 39).
        Empty lines are not counted; the torn (newline-less) tail frame dropped
        by ``_committed_jsonl_lines`` is not counted either (pre-existing
        behaviour, unchanged).
        """
        entries: list[PriorityJobStatus] = []
        bad = 0
        for line in _committed_jsonl_lines(_read_jsonl_bytes(self._path)):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
                entries.append(PriorityJobStatus.from_dict(data))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                bad += 1
        return entries, bad

    def list_statuses(self) -> list[PriorityJobStatus]:
        """Return all status entries (bad lines skipped; see ``_with_health``)."""
        return self.list_statuses_with_health()[0]

    def latest_for_job(self, job_id: str) -> PriorityJobStatus | None:
        """Return the most recent status entry for a job_id."""
        matches = [s for s in self.list_statuses() if s.job_id == job_id]
        return matches[-1] if matches else None


# ── Priority dispatch health check ────────────────────────────────────────────


@dataclass
class PriorityDispatchHealth:
    """Health check result for the priority dispatch pipeline.

    Dispatch statuses:
    - pending: job exists but no push_succeeded yet
    - analysis_partial_but_pushed: analysis_succeeded but push has not confirmed
    - local_push_ack: bound Hermes status reports push_succeeded
    - failed: terminal failure

    ``completed_jobs`` counts locally acknowledged workflow jobs only. It is
    not evidence of platform acceptance, a message ID, display, or live delivery.

    ``bad_status_entries`` counts status lines skipped as unparseable (see
    ``PriorityJobStatusSink.list_statuses_with_health``).  Residual risk when
    it is > 0: a job's latest line may have been skipped, so latest-wins
    aggregation falls back to an older line — the count is the tripwire, the
    per-job view is not trustworthy until the bad lines are investigated.
    """

    total_jobs: int = 0
    pending_jobs: int = 0
    failed_jobs: int = 0
    completed_jobs: int = 0
    analysis_partial_but_pushed: int = 0
    priority_dispatch_pending: bool = False
    bad_status_entries: int = 0
    pending_job_ids: list[str] = field(default_factory=list)
    failed_job_ids: list[str] = field(default_factory=list)
    analysis_partial_job_ids: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_jobs": self.total_jobs,
            "pending_jobs": self.pending_jobs,
            "failed_jobs": self.failed_jobs,
            "completed_jobs": self.completed_jobs,
            "analysis_partial_but_pushed": self.analysis_partial_but_pushed,
            "priority_dispatch_pending": self.priority_dispatch_pending,
            "bad_status_entries": self.bad_status_entries,
            "pending_job_ids": list(self.pending_job_ids),
            "failed_job_ids": list(self.failed_job_ids),
            "analysis_partial_job_ids": list(self.analysis_partial_job_ids),
            "details": list(self.details),
        }


def check_priority_dispatch_health(
    jobs_path: str | Path | None = None,
    status_path: str | Path | None = None,
) -> PriorityDispatchHealth:
    """Read jobs and status sinks, compute dispatch health.

    Returns PriorityDispatchHealth with:
    - total_jobs: all jobs in the outbox
    - pending_jobs: jobs with no bound local push acknowledgement
    - failed_jobs: jobs whose latest status is 'failed'
    - priority_dispatch_pending: True if any job awaits a local acknowledgement
    """
    from datetime import UTC, datetime

    if jobs_path is None:
        jobs_path = Path("knowledge-base/runtime/cognition/priority_analysis_jobs.jsonl")
    if status_path is None:
        status_path = Path("knowledge-base/runtime/cognition/priority_analysis_job_status.jsonl")

    job_outbox = PriorityAnalysisJobOutbox(Path(jobs_path))
    status_sink = PriorityJobStatusSink(Path(status_path))

    jobs = job_outbox.list_jobs()
    all_statuses, bad_status_entries = status_sink.list_statuses_with_health()

    # Preserve every acknowledgement so a reused job_id cannot smuggle a
    # foreign article/event into the delivery state of a real job.
    status_by_job: dict[str, list[PriorityJobStatus]] = {}
    for s in all_statuses:
        status_by_job.setdefault(s.job_id, []).append(s)

    total = len(jobs)
    pending_ids: list[str] = []
    failed_ids: list[str] = []
    partial_ids: list[str] = []
    completed = 0
    partial_but_pushed = 0
    details: list[dict[str, Any]] = []

    now = datetime.now(UTC).isoformat()

    for job in jobs:
        bound_statuses = [
            status for status in status_by_job.get(job.job_id, []) if status.matches_job(job)
        ]
        latest = bound_statuses[-1] if bound_statuses else None
        local_push_ack = next(
            (
                status
                for status in reversed(bound_statuses)
                if status.reports_feishu_push_succeeded
            ),
            None,
        )
        if latest is None:
            pending_ids.append(job.job_id)
            details.append(
                {
                    "job_id": job.job_id,
                    "article_id": job.article_id,
                    "title": job.title,
                    "status": "pending",
                    "dispatch_status": "pending",
                    "last_attempt": 0,
                    "last_error": "",
                    "checked_at": now,
                }
            )
        elif local_push_ack is not None:
            completed += 1
            details.append(
                {
                    "job_id": job.job_id,
                    "article_id": job.article_id,
                    "title": job.title,
                    "status": "push_succeeded",
                    "dispatch_status": "local_push_ack",
                    "last_attempt": local_push_ack.attempt,
                    "last_error": "",
                    "checked_at": now,
                }
            )
        elif latest.status == "failed":
            failed_ids.append(job.job_id)
            details.append(
                {
                    "job_id": job.job_id,
                    "article_id": job.article_id,
                    "title": job.title,
                    "status": "failed",
                    "dispatch_status": "failed",
                    "last_attempt": latest.attempt,
                    "last_error": latest.error,
                    "checked_at": now,
                }
            )
        elif latest.status == "analysis_succeeded":
            # Analysis done but push hasn't completed → analysis_partial_but_pushed
            partial_but_pushed += 1
            partial_ids.append(job.job_id)
            details.append(
                {
                    "job_id": job.job_id,
                    "article_id": job.article_id,
                    "title": job.title,
                    "status": latest.status,
                    "dispatch_status": "analysis_partial_but_pushed",
                    "last_attempt": latest.attempt,
                    "last_error": latest.error,
                    "checked_at": now,
                }
            )
        else:
            # notified / analysis_started → still pending
            pending_ids.append(job.job_id)
            details.append(
                {
                    "job_id": job.job_id,
                    "article_id": job.article_id,
                    "title": job.title,
                    "status": latest.status,
                    "dispatch_status": "pending",
                    "last_attempt": latest.attempt,
                    "last_error": latest.error,
                    "checked_at": now,
                }
            )

    total_pending_and_partial = len(pending_ids) + partial_but_pushed

    return PriorityDispatchHealth(
        total_jobs=total,
        pending_jobs=len(pending_ids),
        failed_jobs=len(failed_ids),
        completed_jobs=completed,
        analysis_partial_but_pushed=partial_but_pushed,
        priority_dispatch_pending=total_pending_and_partial > 0,
        bad_status_entries=bad_status_entries,
        pending_job_ids=pending_ids,
        failed_job_ids=failed_ids,
        analysis_partial_job_ids=partial_ids,
        details=details,
    )
