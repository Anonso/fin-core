"""Operational freshness evidence for the local Guo (G) working set.

The manifest produced here binds three already-existing local publications:
the knowledge index, priority-event outbox, and fresh deep-read pairs.  It is
operational evidence only.  It never creates a teacher event, generates a
deep-read artifact, or writes teacher cognition.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol
from uuid import uuid4

from fin_analyse.guo_teacher_research.source_contract import (
    GSourceClassification,
    classify_g_source,
)
from fin_analyse.guo_teacher_research.window_config import (
    calendar_artifact_available,
    g_window_natural_days,
    load_g_window_config,
    trading_window_cutoff,
)

_SCHEMA_VERSION = "g-working-set-manifest.v2"
_SOURCE_BOUNDARY = "operational_evidence_not_teacher_cognition"
_SOURCE_COVERAGE_SCHEMA = "g-working-set-source-coverage.v2"
_MANIFEST_RELATIVE_PATH = Path("runtime/operations/g_working_set/manifest.v1.json")
_INDEX_RELATIVE_PATH = Path("index.json")
_EVENTS_RELATIVE_PATH = Path("runtime/cognition/priority_events.jsonl")
_COMMENTARY_COLUMNS = frozenset({"星大派锐评", "星大派每日热点"})
_STRICT_G_COLUMNS = frozenset(
    {"星大派锐评", "星大派特刊", "星大派好问题", "星大派每日热点", "星大派人脉", "凤仙郡小故事"}
)


def _active_window_projection() -> dict[str, int]:
    """Manifest ``active_window`` values from the single window config.

    Keys are frozen manifest schema (``_validated_manifest``); only the values
    follow the config so a redeploy regenerates the manifest canonically.
    """

    config = load_g_window_config()
    return {
        "commentary_days": config.commentary_trading_days,
        "general_days": config.special_report_days,
    }
_DEFAULT_MAX_AGE = timedelta(hours=24)
_MAX_INDEX_BYTES = 8 * 1024 * 1024
_MAX_EVENTS_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_INDEX_ARTICLES = 20_000
_MAX_EVENTS = 20_000
_MAX_ACTIVE_ARTICLES = 128
_MAX_GAPS = 32
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CST = timezone(timedelta(hours=8))
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class GWorkingSetStatus(StrEnum):
    READY = "READY"
    STALE = "STALE"
    MISSING = "MISSING"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class GWorkingSetAssessment:
    """A bounded runtime-facing assessment plus its audit manifest."""

    status: GWorkingSetStatus
    data_gaps: tuple[str, ...] = ()
    canonical_sha256: str = ""
    evaluated_at: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def missing(cls, gap: str) -> GWorkingSetAssessment:
        return cls(status=GWorkingSetStatus.MISSING, data_gaps=(gap,))

    def to_runtime_context(self) -> dict[str, object]:
        raw_articles = self.manifest.get("articles", [])
        bound_article_ids = [
            str(article["article_id"])
            for article in raw_articles
            if isinstance(article, Mapping) and article.get("article_id")
        ]
        return {
            "status": self.status.value,
            "canonical_sha256": self.canonical_sha256,
            "evaluated_at": self.evaluated_at,
            "bound_article_ids": bound_article_ids,
            "data_gaps": list(self.data_gaps),
        }

    def to_publication_evidence(self) -> GWorkingSetPublicationEvidence:
        """Return only manifest-validated evidence for the completion producer."""
        normalized = _validated_manifest(self.manifest)
        manifest_gaps = (
            tuple(str(gap) for gap in normalized["data_gaps"]) if normalized is not None else ()
        )
        stale_gap_codes = {
            "g_working_set_manifest_future",
            "g_working_set_manifest_stale",
            "g_working_set_sources_changed",
            "g_working_set_deep_read_changed",
        }
        gaps_match = (
            set(manifest_gaps).issubset(self.data_gaps)
            and any(gap in stale_gap_codes for gap in self.data_gaps)
            if self.status is GWorkingSetStatus.STALE
            else self.data_gaps == manifest_gaps
        )
        if (
            normalized is None
            or self.canonical_sha256 != normalized["canonical_sha256"]
            or self.evaluated_at != normalized["evaluated_at"]
            or not gaps_match
            or (
                self.status is not GWorkingSetStatus.STALE
                and self.status.value != normalized["status"]
            )
        ):
            raise ValueError("g working-set publication evidence is invalid")
        source_refs = tuple(str(article["article_id"]) for article in normalized["articles"])
        coverage = {
            "schema_version": _SOURCE_COVERAGE_SCHEMA,
            "articles": [
                {
                    "article_id": article["article_id"],
                    "column": article["column"],
                    "source_classification": article["source_classification"],
                    "source_family": article["source_family"],
                    "content_type": article["content_type"],
                    "source_usage": article["source_usage"],
                    "priority_label": article["priority_label"],
                    "is_qa": article["is_qa"],
                    "published_at": article["published_at"],
                    "index_entry_sha256": article["index_entry_sha256"],
                    "priority_event_id": article["priority_event_id"],
                    "priority_event_sha256": article["priority_event_sha256"],
                    "deep_read_generation_id": article["deep_read"]["generation_id"],
                    "deep_read_content_hash": article["deep_read"]["content_hash"],
                    "deep_read_compact_raw_sha256": article["deep_read"]["compact_raw_sha256"],
                }
                for article in normalized["articles"]
            ],
        }
        return GWorkingSetPublicationEvidence(
            status=self.status,
            generation=self.canonical_sha256,
            evaluated_at=self.evaluated_at,
            source_refs=source_refs,
            source_coverage_sha256=_canonical_sha256(coverage),
            data_gaps=self.data_gaps,
        )


@dataclass(frozen=True)
class GWorkingSetPublicationEvidence:
    """Validated, bounded evidence exported to the incremental producer."""

    status: GWorkingSetStatus
    generation: str
    evaluated_at: str
    source_refs: tuple[str, ...]
    source_coverage_sha256: str
    data_gaps: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status, GWorkingSetStatus)
            or _SHA256.fullmatch(self.generation) is None
            or _parse_datetime(self.evaluated_at) is None
            or type(self.source_refs) is not tuple
            or len(self.source_refs) > _MAX_ACTIVE_ARTICLES
            or len(self.source_refs) != len(set(self.source_refs))
            or any(not _strict_text(source_ref, 512) for source_ref in self.source_refs)
            or _SHA256.fullmatch(self.source_coverage_sha256) is None
            or type(self.data_gaps) is not tuple
            or len(self.data_gaps) > _MAX_GAPS
            or len(self.data_gaps) != len(set(self.data_gaps))
            or any(not _strict_text(gap, 256) for gap in self.data_gaps)
        ):
            raise ValueError("g working-set publication evidence is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "generation": self.generation,
            "evaluated_at": self.evaluated_at,
            "source_refs": list(self.source_refs),
            "source_coverage_sha256": self.source_coverage_sha256,
            "data_gaps": list(self.data_gaps),
        }

    @classmethod
    def from_dict(cls, value: object) -> GWorkingSetPublicationEvidence:
        fields = {
            "status",
            "generation",
            "evaluated_at",
            "source_refs",
            "source_coverage_sha256",
            "data_gaps",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != fields
            or any(
                type(value[field]) is not str
                for field in {
                    "status",
                    "generation",
                    "evaluated_at",
                    "source_coverage_sha256",
                }
            )
            or not isinstance(value["source_refs"], list)
            or any(type(item) is not str for item in value["source_refs"])
            or not isinstance(value["data_gaps"], list)
            or any(type(item) is not str for item in value["data_gaps"])
        ):
            raise ValueError("g working-set publication evidence is invalid")
        try:
            status = GWorkingSetStatus(value["status"])
        except ValueError as error:
            raise ValueError("g working-set publication evidence is invalid") from error
        return cls(
            status=status,
            generation=value["generation"],
            evaluated_at=value["evaluated_at"],
            source_refs=tuple(value["source_refs"]),
            source_coverage_sha256=value["source_coverage_sha256"],
            data_gaps=tuple(value["data_gaps"]),
        )

    @property
    def identity_sha256(self) -> str:
        """Hash the complete exported evidence, including gaps and source identities."""
        return _canonical_sha256(self.to_dict())


class GWorkingSetPublicationDisposition(StrEnum):
    PUBLISHED = "PUBLISHED"
    ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class GWorkingSetPublicationPlan:
    """One fixed-time, compare-and-publish intent safe to persist and retry."""

    publication_at: str
    expected_owner_id: str
    prior_manifest_identity: str
    expected_generation: str
    expected_source_coverage_sha256: str
    expected_evidence_sha256: str

    def __post_init__(self) -> None:
        _validate_publication_plan(self)

    def to_dict(self) -> dict[str, str]:
        return {
            "publication_at": self.publication_at,
            "expected_owner_id": self.expected_owner_id,
            "prior_manifest_identity": self.prior_manifest_identity,
            "expected_generation": self.expected_generation,
            "expected_source_coverage_sha256": self.expected_source_coverage_sha256,
            "expected_evidence_sha256": self.expected_evidence_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> GWorkingSetPublicationPlan:
        fields = {
            "publication_at",
            "expected_owner_id",
            "prior_manifest_identity",
            "expected_generation",
            "expected_source_coverage_sha256",
            "expected_evidence_sha256",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != fields
            or any(type(value[field]) is not str for field in fields)
        ):
            raise ValueError("g working-set publication plan is invalid")
        return cls(
            publication_at=value["publication_at"],
            expected_owner_id=value["expected_owner_id"],
            prior_manifest_identity=value["prior_manifest_identity"],
            expected_generation=value["expected_generation"],
            expected_source_coverage_sha256=value["expected_source_coverage_sha256"],
            expected_evidence_sha256=value["expected_evidence_sha256"],
        )


@dataclass(frozen=True)
class GWorkingSetPublicationResult:
    disposition: GWorkingSetPublicationDisposition
    assessment: GWorkingSetAssessment | None = None
    reason: str | None = None


class GWorkingSetReader(Protocol):
    """Small read-only interface used by semantic runtime context."""

    def evaluate(self, *, now: datetime | None = None) -> GWorkingSetAssessment: ...


class _DeepReadReader(Protocol):
    def load_fresh_pair(self, article_id: str, article_path: str | Path) -> object | None: ...


@dataclass(frozen=True)
class _FileSnapshot:
    raw: bytes
    sha256: str
    modified_at: str
    mode: int


@dataclass(frozen=True)
class ActiveGWorkingSetCandidate:
    """One index-timed active G article and its optional valid event."""

    article_id: str
    column: str
    title: str
    published_at: str
    published: datetime
    entry_sha256: str
    index_entry: dict[str, object]
    source_classification: str
    source_family: str
    content_type: str
    source_usage: str
    priority_label: str | None
    is_qa: bool
    priority_event: dict[str, object] | None

    def to_runtime_candidate(self) -> dict[str, object] | None:
        if self.priority_event is None:
            return None
        candidate = dict(self.priority_event)
        metadata = candidate.get("metadata")
        canonical_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        canonical_metadata.update(
            {
                "column": self.column,
                "source_family": self.source_family,
                "content_type": self.content_type,
                "source_usage": self.source_usage,
                "priority_label": self.priority_label,
                "is_qa": self.is_qa,
            }
        )
        candidate.update(
            {
                "article_id": self.article_id,
                "title": self.title,
                "column": self.column,
                "published_at": self.published_at,
                "source_classification": self.source_classification,
                "source_family": self.source_family,
                "content_type": self.content_type,
                "source_usage": self.source_usage,
                "priority_label": self.priority_label,
                "is_qa": self.is_qa,
                "metadata": canonical_metadata,
            }
        )
        return candidate


@dataclass(frozen=True)
class ActiveGWorkingSetSelection:
    """Shared, pure selection result for manifest and cache-backed runtime."""

    candidates: tuple[ActiveGWorkingSetCandidate, ...]
    data_gaps: tuple[str, ...]

    @property
    def bound_article_ids(self) -> tuple[str, ...]:
        return tuple(candidate.article_id for candidate in self.candidates)

    def runtime_candidates(self) -> tuple[dict[str, object], ...]:
        projected = (candidate.to_runtime_candidate() for candidate in self.candidates)
        return tuple(candidate for candidate in projected if candidate is not None)


def select_active_g_working_set(
    *,
    index_articles: Sequence[Mapping[str, object]],
    priority_events: Sequence[Mapping[str, object]],
    now: datetime,
) -> ActiveGWorkingSetSelection | None:
    """Select the active G set from article time, then bind valid events.

    Event creation time can prove when an event became available, but it can
    never make an old article fresh.  Malformed or duplicate events only affect
    readiness when they target an article in the current active set.
    """
    evaluated = _aware(now)
    gaps: list[str] = []
    all_ids: set[str] = set()
    active: list[tuple[datetime, str, str, dict[str, object], GSourceClassification, bool]] = []
    for raw_entry in index_articles:
        entry = dict(raw_entry)
        article_id = _strict_text(entry.get("id"), 160)
        if not article_id or article_id in all_ids:
            return None
        all_ids.add(article_id)
        column = _strict_text(entry.get("column"), 80)
        is_qa = entry.get("is_qa") is True
        published = _parse_datetime(_strict_text(entry.get("date"), 80), default_tz=_CST)
        # 窗口单源（BUG-006③）：锐评=交易日语义，其余=special。日历工件
        # 缺失响一声（gap）；无管辖权时段静默回落自然日旧语义，不打 PARTIAL。
        window_config = load_g_window_config()
        if column in _COMMENTARY_COLUMNS:
            cutoff, _used = trading_window_cutoff(
                evaluated, window_config.commentary_trading_days
            )
            if not calendar_artifact_available():
                _append_gap(gaps, "g_window_calendar_unavailable")
            if published is not None and published <= evaluated and published < cutoff:
                continue
        else:
            window = timedelta(days=g_window_natural_days(column, window_config))
            if published is not None and published <= evaluated and evaluated - published > window:
                continue
        source_decision = classify_g_source(
            column,
            teacher_original=True,
            is_qa=is_qa,
            priority_label=entry.get("priority_label"),
        )
        if source_decision.classification is None:
            if source_decision.data_gap:
                _append_gap(gaps, source_decision.data_gap)
            continue
        if not source_decision.eligible:
            assert source_decision.data_gap is not None
            _append_gap(gaps, source_decision.data_gap)
            continue
        source = source_decision.classification
        if column not in _STRICT_G_COLUMNS or not _index_source_contract_matches(
            entry,
            source=source,
            is_qa=is_qa,
        ):
            _append_gap(gaps, "g_working_set_source_contract_mismatch")
            continue
        if published is None or published > evaluated:
            _append_gap(gaps, "g_working_set_index_time_invalid")
            continue
        title = _strict_text(entry.get("title"), 500)
        active.append((published, article_id, title, entry, source, is_qa))

    # Same title is one working-set identity. The latest indexed publication
    # wins deterministically, matching the runtime's historical dedupe policy.
    active.sort(key=lambda item: (item[0], item[1]), reverse=True)
    deduped: list[tuple[datetime, str, str, dict[str, object], GSourceClassification, bool]] = []
    seen_titles: set[str] = set()
    for item in active:
        title_key = item[2] or f"article:{item[1]}"
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        deduped.append(item)
    if len(deduped) > _MAX_ACTIVE_ARTICLES:
        deduped = deduped[:_MAX_ACTIVE_ARTICLES]
        _append_gap(gaps, "g_working_set_active_g_truncated")

    active_by_id = {item[1]: item for item in deduped}
    events_by_id: dict[str, list[dict[str, object]]] = {}
    for raw_event in priority_events:
        event = dict(raw_event)
        article_id = _strict_text(event.get("article_id"), 160)
        if article_id in active_by_id:
            events_by_id.setdefault(article_id, []).append(event)

    selected: list[ActiveGWorkingSetCandidate] = []
    for published, article_id, title, entry, source, is_qa in deduped:
        column = _strict_text(entry.get("column"), 80)
        matching = events_by_id.get(article_id, [])
        selected_event: dict[str, object] | None = None
        if len(matching) == 1 and _event_matches_index_article(
            matching[0],
            article_id=article_id,
            column=column,
            published=published,
            now=evaluated,
            source=source,
            is_qa=is_qa,
        ):
            selected_event = matching[0]
        elif matching:
            _append_gap(gaps, "g_working_set_priority_event_contract_mismatch")
        selected.append(
            ActiveGWorkingSetCandidate(
                article_id=article_id,
                column=column,
                title=title,
                published_at=published.isoformat(),
                published=published,
                entry_sha256=_canonical_sha256(entry),
                index_entry=entry,
                source_classification="teacher_original",
                source_family=source.source_family,
                content_type=source.content_type,
                source_usage=source.usage,
                priority_label=source.priority_label,
                is_qa=is_qa,
                priority_event=selected_event,
            )
        )
    return ActiveGWorkingSetSelection(
        candidates=tuple(selected),
        data_gaps=tuple(gaps),
    )


class GWorkingSetService:
    """Build, atomically publish, and read one local G freshness manifest."""

    def __init__(
        self,
        *,
        kb_root: Path,
        deep_read_reader: _DeepReadReader | None = None,
        max_age: timedelta = _DEFAULT_MAX_AGE,
    ) -> None:
        supplied_root = Path(kb_root)
        if ".." in supplied_root.parts:
            raise ValueError("knowledge-base root cannot contain parent traversal")
        root = supplied_root.absolute()
        if max_age <= timedelta(0):
            raise ValueError("g working-set max_age must be positive")
        self._kb_root = root
        self._manifest_path = root / _MANIFEST_RELATIVE_PATH
        self._max_age = max_age
        if deep_read_reader is None:
            from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

            deep_read_reader = DeepReadArtifactService(root)
        self._deep_read_reader = deep_read_reader

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    @property
    def owner_id(self) -> str:
        return _canonical_sha256(
            {
                "schema_version": "fin.g-working-set-owner/v1",
                "knowledge_base_root": str(self._kb_root),
            }
        )

    def reconcile(self, *, now: datetime | None = None) -> GWorkingSetAssessment:
        """Deterministically assess current local publications without writing."""
        evaluated = _aware(now or datetime.now(UTC))
        gaps: list[str] = []
        index_path = self._kb_root / _INDEX_RELATIVE_PATH
        event_path = self._kb_root / _EVENTS_RELATIVE_PATH
        index_snapshot, index_problem = _read_source_file(index_path, max_bytes=_MAX_INDEX_BYTES)
        event_snapshot, event_problem = _read_source_file(event_path, max_bytes=_MAX_EVENTS_BYTES)

        sources: dict[str, Any] = {
            "knowledge_index": _source_projection(index_snapshot),
            "priority_events": _source_projection(event_snapshot),
            "deep_read": {"required_count": 0, "available_count": 0},
        }
        active: list[ActiveGWorkingSetCandidate] = []
        index_articles: list[dict[str, object]] = []
        index_updated_at = ""
        fatal_index = False
        if index_problem == "missing":
            _append_gap(gaps, "g_working_set_index_missing")
            fatal_index = True
        elif index_problem:
            _append_gap(gaps, "g_working_set_index_invalid")
            fatal_index = True
        else:
            assert index_snapshot is not None
            parsed = decode_g_knowledge_index(index_snapshot.raw, now=evaluated)
            if parsed is None:
                _append_gap(gaps, "g_working_set_index_invalid")
                fatal_index = True
            else:
                index_articles, index_updated_at, index_gaps = parsed
                _extend_gaps(gaps, index_gaps)

        event_rows: list[dict[str, object]] = []
        if event_problem == "missing":
            _append_gap(gaps, "g_working_set_priority_events_missing")
        elif event_problem:
            _append_gap(gaps, "g_working_set_priority_events_invalid")
        else:
            assert event_snapshot is not None
            parsed_events = decode_priority_event_rows(event_snapshot.raw)
            if parsed_events is None:
                _append_gap(gaps, "g_working_set_priority_events_invalid")
            else:
                event_rows = parsed_events
        if not fatal_index:
            selected = select_active_g_working_set(
                index_articles=index_articles,
                priority_events=event_rows,
                now=evaluated,
            )
            if selected is None:
                _append_gap(gaps, "g_working_set_index_invalid")
                fatal_index = True
            else:
                active = list(selected.candidates)
                _extend_gaps(gaps, selected.data_gaps)
        sources["knowledge_index"].update(
            {
                "declared_updated_at": index_updated_at,
                "active_article_count": len(active),
            }
        )
        sources["priority_events"].update({"event_count": len(event_rows)})

        articles: list[dict[str, Any]] = []
        event_coverage = 0
        deep_read_coverage = 0
        for article in active:
            event = article.priority_event
            event_id: str | None = None
            event_sha256: str | None = None
            if event is not None:
                event_id = str(event["event_id"])
                event_sha256 = _canonical_sha256(event)
                event_coverage += 1

            article_path = _resolve_article_path(self._kb_root, article.index_entry)
            if article_path is None:
                _append_gap(gaps, "g_working_set_article_file_unavailable")
            deep_read = _deep_read_projection(
                self._deep_read_reader,
                article_id=article.article_id,
                article_path=article_path,
                now=evaluated,
            )
            if deep_read["available"]:
                deep_read_coverage += 1
            articles.append(
                {
                    "article_id": article.article_id,
                    "column": article.column,
                    "source_classification": article.source_classification,
                    "source_family": article.source_family,
                    "content_type": article.content_type,
                    "source_usage": article.source_usage,
                    "priority_label": article.priority_label,
                    "is_qa": article.is_qa,
                    "title": article.title,
                    "published_at": article.published_at,
                    "index_entry_sha256": article.entry_sha256,
                    "priority_event_id": event_id,
                    "priority_event_sha256": event_sha256,
                    "deep_read": deep_read,
                }
            )

        sources["deep_read"] = {
            "required_count": len(active),
            "available_count": deep_read_coverage,
        }
        if not fatal_index and not active:
            _append_gap(gaps, "g_working_set_active_g_empty")
        if active and event_coverage != len(active):
            _append_gap(gaps, "g_working_set_priority_event_coverage_partial")
        if active and deep_read_coverage != len(active):
            _append_gap(gaps, "g_working_set_deep_read_coverage_partial")

        status = (
            GWorkingSetStatus.MISSING
            if fatal_index
            else GWorkingSetStatus.PARTIAL
            if gaps
            else GWorkingSetStatus.READY
        )
        manifest: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "source_boundary": _SOURCE_BOUNDARY,
            "status": status.value,
            "evaluated_at": evaluated.isoformat(),
            "active_window": _active_window_projection(),
            "sources": sources,
            "articles": articles,
            "data_gaps": list(gaps),
        }
        generation_projection = dict(manifest)
        generation_projection.pop("evaluated_at", None)
        manifest["canonical_sha256"] = _canonical_sha256(generation_projection)
        return _assessment_from_manifest(manifest)

    def prepare_publication(self, *, publication_at: datetime) -> GWorkingSetPublicationPlan:
        """Freeze a zero-write publication plan against the raw prior manifest."""
        evaluated = _aware(publication_at)
        prior_identity = self._current_manifest_identity()
        assessment = self.reconcile(now=evaluated)
        evidence = assessment.to_publication_evidence()
        return GWorkingSetPublicationPlan(
            publication_at=evaluated.isoformat(),
            expected_owner_id=self.owner_id,
            prior_manifest_identity=prior_identity,
            expected_generation=evidence.generation,
            expected_source_coverage_sha256=evidence.source_coverage_sha256,
            expected_evidence_sha256=evidence.identity_sha256,
        )

    def compare_and_publish(self, plan: GWorkingSetPublicationPlan) -> GWorkingSetPublicationResult:
        """Publish exactly one frozen plan, or reject drift without writing."""
        _validate_publication_plan(plan)
        if plan.expected_owner_id != self.owner_id:
            return GWorkingSetPublicationResult(
                disposition=GWorkingSetPublicationDisposition.REJECTED,
                reason="OWNER_DRIFT",
            )
        publication_at = _parse_datetime(plan.publication_at)
        assert publication_at is not None
        directory_fd = self._open_manifest_directory(create=True)
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            self._require_manifest_directory_bound(directory_fd)
            _validate_existing_target(directory_fd, self._manifest_path.name)
            current, problem = _read_file_at(
                directory_fd,
                self._manifest_path.name,
                max_bytes=_MAX_MANIFEST_BYTES,
                required_mode=0o600,
            )
            if problem not in {"", "missing"}:
                raise ValueError("g working-set manifest target is invalid")
            current_identity = current.sha256 if current is not None else "MISSING"
            if current is not None:
                try:
                    current_payload = json.loads(current.raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError("g working-set manifest target is invalid") from error
                current_manifest = _validated_manifest(current_payload)
                current_evaluated_at = (
                    _parse_datetime(str(current_manifest["evaluated_at"]))
                    if current_manifest is not None
                    else None
                )
                if current_evaluated_at is None:
                    raise ValueError("g working-set manifest target is invalid")
                if current_evaluated_at > publication_at:
                    return GWorkingSetPublicationResult(
                        disposition=GWorkingSetPublicationDisposition.REJECTED,
                        reason="NEWER_MANIFEST",
                    )

            candidate = self.reconcile(now=publication_at)
            candidate_raw = _publication_raw(candidate)
            evidence = candidate.to_publication_evidence()
            if (
                evidence.generation != plan.expected_generation
                or evidence.source_coverage_sha256 != plan.expected_source_coverage_sha256
                or evidence.identity_sha256 != plan.expected_evidence_sha256
            ):
                return GWorkingSetPublicationResult(
                    disposition=GWorkingSetPublicationDisposition.REJECTED,
                    reason="SOURCE_DRIFT",
                )

            if current is not None and current.raw == candidate_raw:
                stable = self.reconcile(now=publication_at)
                if _publication_raw(stable) != candidate_raw:
                    return GWorkingSetPublicationResult(
                        disposition=GWorkingSetPublicationDisposition.REJECTED,
                        reason="SOURCE_DRIFT",
                    )
                self._require_manifest_directory_bound(directory_fd)
                return GWorkingSetPublicationResult(
                    disposition=GWorkingSetPublicationDisposition.ALREADY_PUBLISHED,
                    assessment=candidate,
                )

            if current_identity != plan.prior_manifest_identity:
                return GWorkingSetPublicationResult(
                    disposition=GWorkingSetPublicationDisposition.REJECTED,
                    reason="PRIOR_MANIFEST_DRIFT",
                )

            stable = self.reconcile(now=publication_at)
            if _publication_raw(stable) != candidate_raw:
                return GWorkingSetPublicationResult(
                    disposition=GWorkingSetPublicationDisposition.REJECTED,
                    reason="SOURCE_DRIFT",
                )
            self._require_manifest_directory_bound(directory_fd)
            self._publish_raw(directory_fd, candidate_raw)
            self._require_manifest_directory_bound(directory_fd)
            published, published_problem = _read_file_at(
                directory_fd,
                self._manifest_path.name,
                max_bytes=_MAX_MANIFEST_BYTES,
                required_mode=0o600,
            )
            if published_problem or published is None or published.raw != candidate_raw:
                raise OSError("g working-set publication verification failed")
            return GWorkingSetPublicationResult(
                disposition=GWorkingSetPublicationDisposition.PUBLISHED,
                assessment=candidate,
            )
        finally:
            with suppress(OSError):
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            os.close(directory_fd)

    def verify_published_plan(
        self,
        plan: GWorkingSetPublicationPlan,
    ) -> GWorkingSetPublicationEvidence:
        """Read one manifest under the owner lock and match the frozen plan."""
        _validate_publication_plan(plan)
        if plan.expected_owner_id != self.owner_id:
            raise ValueError("g working-set publication owner drifted")
        directory_fd = self._open_manifest_directory(create=False)
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_SH)
            self._require_manifest_directory_bound(directory_fd)
            _validate_existing_target(directory_fd, self._manifest_path.name)
            snapshot, problem = _read_file_at(
                directory_fd,
                self._manifest_path.name,
                max_bytes=_MAX_MANIFEST_BYTES,
                required_mode=0o600,
            )
            self._require_manifest_directory_bound(directory_fd)
            if problem or snapshot is None:
                raise ValueError("g working-set published manifest is missing")
            try:
                payload = json.loads(snapshot.raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("g working-set published manifest is invalid") from error
            normalized = _validated_manifest(payload)
            if normalized is None:
                raise ValueError("g working-set published manifest is invalid")
            evidence = _assessment_from_manifest(normalized).to_publication_evidence()
            if (
                evidence.generation != plan.expected_generation
                or evidence.evaluated_at != plan.publication_at
                or evidence.source_coverage_sha256
                != plan.expected_source_coverage_sha256
                or evidence.identity_sha256 != plan.expected_evidence_sha256
            ):
                raise ValueError("g working-set published manifest differs from plan")
            return evidence
        finally:
            with suppress(OSError):
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            os.close(directory_fd)

    def reconcile_and_publish(self, *, now: datetime | None = None) -> GWorkingSetAssessment:
        """Compatibility facade over the single compare-and-publish seam."""
        publication_at = _aware(now or datetime.now(UTC))
        result = self.compare_and_publish(self.prepare_publication(publication_at=publication_at))
        if result.disposition is GWorkingSetPublicationDisposition.REJECTED:
            raise RuntimeError(f"g working-set publication rejected: {result.reason}")
        if result.assessment is None:
            raise RuntimeError("g working-set publication assessment is missing")
        return result.assessment

    def _current_manifest_identity(self) -> str:
        try:
            directory_fd = self._open_manifest_directory(create=False)
        except FileNotFoundError:
            return "MISSING"
        try:
            _validate_existing_target(directory_fd, self._manifest_path.name)
            snapshot, problem = _read_file_at(
                directory_fd,
                self._manifest_path.name,
                max_bytes=_MAX_MANIFEST_BYTES,
                required_mode=0o600,
            )
        finally:
            os.close(directory_fd)
        if problem == "missing":
            return "MISSING"
        if problem or snapshot is None:
            raise ValueError("g working-set manifest target is invalid")
        return snapshot.sha256

    def _publish_raw(self, directory_fd: int, raw: bytes) -> None:
        temp_name = f".manifest.v1.{uuid4().hex}.tmp"
        temp_fd = -1
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            temp_fd = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
            os.fchmod(temp_fd, 0o600)
            _write_all(temp_fd, raw)
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            os.replace(
                temp_name,
                self._manifest_path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            with suppress(FileNotFoundError):
                os.unlink(temp_name, dir_fd=directory_fd)

    def read(self) -> GWorkingSetAssessment:
        """Read and validate the owner-only manifest without scanning sources."""
        try:
            directory_fd = self._open_manifest_directory(create=False)
        except FileNotFoundError:
            return GWorkingSetAssessment.missing("g_working_set_manifest_missing")
        except (OSError, ValueError):
            return GWorkingSetAssessment.missing("g_working_set_manifest_invalid")
        try:
            snapshot, problem = _read_file_at(
                directory_fd,
                self._manifest_path.name,
                max_bytes=_MAX_MANIFEST_BYTES,
                required_mode=0o600,
            )
        finally:
            os.close(directory_fd)
        if problem == "missing":
            return GWorkingSetAssessment.missing("g_working_set_manifest_missing")
        if problem or snapshot is None:
            return GWorkingSetAssessment.missing("g_working_set_manifest_invalid")
        try:
            payload = json.loads(snapshot.raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return GWorkingSetAssessment.missing("g_working_set_manifest_invalid")
        normalized = _validated_manifest(payload)
        if normalized is None:
            return GWorkingSetAssessment.missing("g_working_set_manifest_invalid")
        return _assessment_from_manifest(normalized)

    def evaluate(self, *, now: datetime | None = None) -> GWorkingSetAssessment:
        """Read, age-check, and compare source fingerprints; never write."""
        stored = self.read()
        if not stored.manifest:
            return stored
        evaluated = _aware(now or datetime.now(UTC))
        manifest_time = _parse_datetime(stored.evaluated_at)
        gaps = list(stored.data_gaps)
        evaluation_time = evaluated.astimezone(UTC)
        future = manifest_time is not None and manifest_time > evaluation_time
        stale = manifest_time is None or future or evaluation_time - manifest_time > self._max_age
        if future:
            _append_gap(gaps, "g_working_set_manifest_future")
        elif stale:
            _append_gap(gaps, "g_working_set_manifest_stale")

        sources = stored.manifest["sources"]
        current_index, _ = _read_source_file(
            self._kb_root / _INDEX_RELATIVE_PATH, max_bytes=_MAX_INDEX_BYTES
        )
        current_events, _ = _read_source_file(
            self._kb_root / _EVENTS_RELATIVE_PATH, max_bytes=_MAX_EVENTS_BYTES
        )
        expected_index = str(sources["knowledge_index"].get("sha256", ""))
        expected_events = str(sources["priority_events"].get("sha256", ""))
        if expected_index != (current_index.sha256 if current_index else "") or expected_events != (
            current_events.sha256 if current_events else ""
        ):
            stale = True
            _append_gap(gaps, "g_working_set_sources_changed")
        elif stored.status is GWorkingSetStatus.READY and (
            current_index is None
            or current_events is None
            or manifest_time is None
            or not _manifest_article_source_bindings_match(
                stored.manifest,
                index_snapshot=current_index,
                event_snapshot=current_events,
                manifest_time=manifest_time,
            )
        ):
            return GWorkingSetAssessment.missing("g_working_set_manifest_invalid")
        elif current_index is not None and _deep_read_evidence_changed(
            stored.manifest,
            index_snapshot=current_index,
            reader=self._deep_read_reader,
            kb_root=self._kb_root,
            now=evaluation_time,
        ):
            stale = True
            _append_gap(gaps, "g_working_set_deep_read_changed")
        if not stale:
            return stored
        return GWorkingSetAssessment(
            status=GWorkingSetStatus.STALE,
            data_gaps=tuple(gaps),
            canonical_sha256=stored.canonical_sha256,
            evaluated_at=stored.evaluated_at,
            manifest=stored.manifest,
        )

    def _open_manifest_directory(self, *, create: bool) -> int:
        current_fd = _open_owned_directory(self._kb_root, owner_only=False)
        try:
            parts = _MANIFEST_RELATIVE_PATH.parent.parts
            for index, part in enumerate(parts):
                owner_only = index >= 1  # operations/ and its child are private
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=current_fd)
                        os.fsync(current_fd)
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                info = os.fstat(next_fd)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid()
                    or (owner_only and stat.S_IMODE(info.st_mode) != 0o700)
                ):
                    os.close(next_fd)
                    raise ValueError("g working-set manifest directory boundary is invalid")
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    def _require_manifest_directory_bound(self, directory_fd: int) -> None:
        try:
            named_fd = self._open_manifest_directory(create=False)
        except (OSError, ValueError) as error:
            raise ValueError("g working-set manifest directory identity drifted") from error
        try:
            opened = os.fstat(directory_fd)
            named = os.fstat(named_fd)
        finally:
            os.close(named_fd)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError("g working-set manifest directory identity drifted")


def decode_g_knowledge_index(
    raw: bytes,
    *,
    now: datetime,
) -> tuple[list[dict[str, object]], str, tuple[str, ...]] | None:
    """Decode the bounded index envelope without selecting G candidates."""
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
        return None
    raw_articles = payload["articles"]
    if len(raw_articles) > _MAX_INDEX_ARTICLES:
        return None
    articles: list[dict[str, object]] = []
    gaps: list[str] = []
    for raw_entry in raw_articles:
        if not isinstance(raw_entry, dict):
            return None
        articles.append(dict(raw_entry))
    updated = _strict_text(payload.get("updated"), 80)
    parsed_updated = _parse_datetime(updated, default_tz=_CST)
    if parsed_updated is None or parsed_updated > now.astimezone(UTC):
        _append_gap(gaps, "g_working_set_index_time_invalid")
        updated = ""
    else:
        updated = parsed_updated.isoformat()
    return articles, updated, tuple(gaps)


def decode_priority_event_rows(raw: bytes) -> list[dict[str, object]] | None:
    """Decode usable rows; isolated historical noise is not a live gap."""
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    if len(lines) > _MAX_EVENTS:
        return None
    events: list[dict[str, object]] = []
    nonblank = 0
    for line in lines:
        if not line.strip():
            continue
        nonblank += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(dict(event))
    if nonblank and not events:
        return None
    return events


def _index_source_contract_matches(
    entry: Mapping[str, object],
    *,
    source: GSourceClassification,
    is_qa: bool,
) -> bool:
    """Accept legacy index rows, but reject any stored contract drift.

    The exact column is still the canonical classification input.  New ingests
    persist the independent axes; when an older row lacks those optional fields
    the selection remains source-safe because the column is classified above.
    """

    expected: dict[str, object] = {
        "source_classification": "teacher_original",
        "source_family": source.source_family,
        "content_type": source.content_type,
        "source_usage": source.usage,
        "priority_label": source.priority_label,
        "is_qa": is_qa,
    }
    return all(key not in entry or entry.get(key) == value for key, value in expected.items())


def _event_matches_index_article(
    event: Mapping[str, object],
    *,
    article_id: str,
    column: str,
    published: datetime,
    now: datetime,
    source: GSourceClassification,
    is_qa: bool,
) -> bool:
    raw_metadata = event.get("metadata")
    metadata: Mapping[str, object] = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    created = _parse_datetime(_strict_text(event.get("created_at"), 80), default_tz=_CST)
    expected_metadata: dict[str, object] = {
        "column": column,
        "source_family": source.source_family,
        "content_type": source.content_type,
        "source_usage": source.usage,
        "priority_label": source.priority_label,
        "is_qa": is_qa,
    }
    return (
        _strict_text(event.get("event_id"), 200) != ""
        and event.get("article_id") == article_id
        and event.get("source_classification") == "teacher_original"
        and event.get("requires_deep_read") is True
        and all(
            key not in metadata or metadata.get(key) == value
            for key, value in expected_metadata.items()
        )
        and metadata.get("column") == column
        and created is not None
        and published <= created <= now.astimezone(UTC)
    )


def _deep_read_projection(
    reader: _DeepReadReader,
    *,
    article_id: str,
    article_path: Path | None,
    now: datetime,
) -> dict[str, object]:
    pair = None
    if article_path is not None:
        try:
            pair = reader.load_fresh_pair(article_id, article_path)
        except Exception:
            pair = None
    if pair is None:
        return {
            "available": False,
            "generation_id": None,
            "generated_at": None,
            "content_hash": None,
            "compact_raw_sha256": None,
        }
    generation_id = _strict_text(getattr(pair, "generation_id", None), 200)
    generated_at = _strict_text(getattr(pair, "generated_at", None), 80)
    content_hash = _strict_text(getattr(pair, "content_hash", None), 64)
    compact_hash = _strict_text(getattr(pair, "compact_raw_sha256", None), 64)
    if (
        not generation_id
        or (generated := _parse_datetime(generated_at)) is None
        or generated > now.astimezone(UTC)
        or not _SHA256.fullmatch(content_hash)
        or not _SHA256.fullmatch(compact_hash)
    ):
        return {
            "available": False,
            "generation_id": None,
            "generated_at": None,
            "content_hash": None,
            "compact_raw_sha256": None,
        }
    return {
        "available": True,
        "generation_id": generation_id,
        "generated_at": generated_at,
        "content_hash": content_hash,
        "compact_raw_sha256": compact_hash,
    }


def _resolve_article_path(kb_root: Path, entry: Mapping[str, object]) -> Path | None:
    raw_file = entry.get("file")
    if isinstance(raw_file, str) and _plain_markdown_filename(raw_file):
        filename = raw_file
    else:
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        filename = PurePosixPath(raw_path).name
        if not _plain_markdown_filename(filename):
            return None
    candidate = kb_root / "articles" / filename
    snapshot, problem = _read_source_file(candidate, max_bytes=4 * 1024 * 1024)
    return candidate if snapshot is not None and not problem else None


def _deep_read_evidence_changed(
    manifest: Mapping[str, object],
    *,
    index_snapshot: _FileSnapshot,
    reader: _DeepReadReader,
    kb_root: Path,
    now: datetime,
) -> bool:
    """Compare bound pairs with current local pairs, without generating them."""
    try:
        index = json.loads(index_snapshot.raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return True
    raw_entries = index.get("articles") if isinstance(index, dict) else None
    raw_articles = manifest.get("articles")
    if not isinstance(raw_entries, list) or not isinstance(raw_articles, list):
        return True
    entries: dict[str, Mapping[str, object]] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            return True
        article_id = _strict_text(raw_entry.get("id"), 160)
        if not article_id or article_id in entries:
            return True
        entries[article_id] = raw_entry
    for raw_article in raw_articles:
        if not isinstance(raw_article, Mapping):
            return True
        article_id = _strict_text(raw_article.get("article_id"), 160)
        entry = entries.get(article_id)
        if entry is None:
            return True
        current = _deep_read_projection(
            reader,
            article_id=article_id,
            article_path=_resolve_article_path(kb_root, entry),
            now=now,
        )
        if current != raw_article.get("deep_read"):
            return True
    return False


def _manifest_article_source_bindings_match(
    manifest: Mapping[str, object],
    *,
    index_snapshot: _FileSnapshot,
    event_snapshot: _FileSnapshot,
    manifest_time: datetime,
) -> bool:
    """Rebuild each READY article binding from the owner source snapshots."""
    decoded_index = decode_g_knowledge_index(index_snapshot.raw, now=manifest_time)
    event_rows = decode_priority_event_rows(event_snapshot.raw)
    if decoded_index is None or event_rows is None:
        return False
    index_articles, index_updated_at, index_gaps = decoded_index
    selected = select_active_g_working_set(
        index_articles=index_articles,
        priority_events=event_rows,
        now=manifest_time,
    )
    if selected is None or index_gaps or selected.data_gaps:
        return False
    expected_articles: list[dict[str, object]] = []
    for candidate in selected.candidates:
        event = candidate.priority_event
        expected_articles.append(
            {
                "article_id": candidate.article_id,
                "column": candidate.column,
                "source_classification": candidate.source_classification,
                "source_family": candidate.source_family,
                "content_type": candidate.content_type,
                "source_usage": candidate.source_usage,
                "priority_label": candidate.priority_label,
                "is_qa": candidate.is_qa,
                "title": candidate.title,
                "published_at": candidate.published_at,
                "index_entry_sha256": candidate.entry_sha256,
                "priority_event_id": str(event["event_id"]) if event is not None else None,
                "priority_event_sha256": (_canonical_sha256(event) if event is not None else None),
            }
        )
    raw_articles = manifest.get("articles")
    sources = manifest.get("sources")
    if not isinstance(raw_articles, list) or not isinstance(sources, Mapping):
        return False
    index_source = sources.get("knowledge_index")
    event_source = sources.get("priority_events")
    if not isinstance(index_source, Mapping) or not isinstance(event_source, Mapping):
        return False
    actual_articles: list[dict[str, object]] = []
    source_binding_keys = tuple(expected_articles[0]) if expected_articles else ()
    for article in raw_articles:
        if not isinstance(article, Mapping):
            return False
        actual_articles.append({key: article.get(key) for key in source_binding_keys})
    return (
        actual_articles == expected_articles
        and index_source.get("declared_updated_at") == index_updated_at
        and event_source.get("event_count") == len(event_rows)
    )


def _plain_markdown_filename(value: str) -> bool:
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    return (
        value not in {"", ".", ".."}
        and "\x00" not in value
        and posix.parts == (value,)
        and windows.parts == (value,)
        and not posix.is_absolute()
        and not windows.is_absolute()
        and not windows.drive
        and posix.suffix.lower() == ".md"
    )


def _validated_manifest(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    normalized = dict(value)
    if set(normalized) != {
        "schema_version",
        "source_boundary",
        "status",
        "evaluated_at",
        "active_window",
        "sources",
        "articles",
        "data_gaps",
        "canonical_sha256",
    }:
        return None
    if (
        normalized.get("schema_version") != _SCHEMA_VERSION
        or normalized.get("source_boundary") != _SOURCE_BOUNDARY
        or normalized.get("status")
        not in {
            GWorkingSetStatus.READY.value,
            GWorkingSetStatus.MISSING.value,
            GWorkingSetStatus.PARTIAL.value,
        }
        or _parse_datetime(_strict_text(normalized.get("evaluated_at"), 80)) is None
        or not isinstance(normalized.get("sources"), dict)
        or not isinstance(normalized.get("articles"), list)
        or len(normalized["articles"]) > _MAX_ACTIVE_ARTICLES
        or not isinstance(normalized.get("data_gaps"), list)
        or len(normalized["data_gaps"]) > _MAX_GAPS
    ):
        return None
    gaps = normalized["data_gaps"]
    if any(not _strict_text(gap, 200) for gap in gaps) or len(gaps) != len(set(gaps)):
        return None
    sources = normalized["sources"]
    if set(sources) != {"knowledge_index", "priority_events", "deep_read"}:
        return None
    index_source = sources["knowledge_index"]
    event_source = sources["priority_events"]
    deep_source = sources["deep_read"]
    if (
        not isinstance(index_source, dict)
        or set(index_source)
        != {"sha256", "modified_at", "declared_updated_at", "active_article_count"}
        or not isinstance(event_source, dict)
        or set(event_source) != {"sha256", "modified_at", "event_count"}
        or not isinstance(deep_source, dict)
        or set(deep_source) != {"required_count", "available_count"}
    ):
        return None
    for source in (index_source, event_source):
        source_hash = source.get("sha256", "")
        if not isinstance(source_hash, str) or source_hash and not _SHA256.fullmatch(source_hash):
            return None
    articles = normalized["articles"]
    if any(not _valid_manifest_article(article) for article in articles):
        return None
    article_ids = [str(article["article_id"]) for article in articles]
    if len(article_ids) != len(set(article_ids)):
        return None
    article_count = len(articles)
    if (
        index_source.get("active_article_count") != article_count
        or not isinstance(event_source.get("event_count"), int)
        or deep_source.get("required_count") != article_count
        or deep_source.get("available_count")
        != sum(bool(article["deep_read"]["available"]) for article in articles)
    ):
        return None
    status = normalized["status"]
    if (
        (status == GWorkingSetStatus.READY.value) != (not gaps)
        or (status == GWorkingSetStatus.READY.value and not articles)
        or (
            status == GWorkingSetStatus.READY.value
            and any(
                article["priority_event_id"] is None
                or article["deep_read"]["available"] is not True
                for article in articles
            )
        )
    ):
        return None
    claimed = _strict_text(normalized.get("canonical_sha256"), 64)
    without_hash = dict(normalized)
    without_hash.pop("canonical_sha256", None)
    without_hash.pop("evaluated_at", None)
    if not _SHA256.fullmatch(claimed) or claimed != _canonical_sha256(without_hash):
        return None
    return normalized


def _valid_manifest_article(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "article_id",
        "column",
        "source_classification",
        "source_family",
        "content_type",
        "source_usage",
        "priority_label",
        "is_qa",
        "title",
        "published_at",
        "index_entry_sha256",
        "priority_event_id",
        "priority_event_sha256",
        "deep_read",
    }:
        return False
    is_qa = value.get("is_qa")
    if (
        not _strict_text(value.get("article_id"), 160)
        or value.get("column") not in _STRICT_G_COLUMNS
        or value.get("source_classification") != "teacher_original"
        or type(is_qa) is not bool
        or _parse_datetime(_strict_text(value.get("published_at"), 80)) is None
        or not _SHA256.fullmatch(_strict_text(value.get("index_entry_sha256"), 64))
    ):
        return False
    source_decision = classify_g_source(
        value.get("column"),
        teacher_original=True,
        is_qa=is_qa,
        priority_label=value.get("priority_label"),
    )
    source = source_decision.classification
    if (
        not source_decision.eligible
        or source is None
        or (
            value.get("source_family") != source.source_family
            or value.get("content_type") != source.content_type
            or value.get("source_usage") != source.usage
            or value.get("priority_label") != source.priority_label
        )
    ):
        return False
    event_id = value.get("priority_event_id")
    event_hash = value.get("priority_event_sha256")
    if (event_id is None) != (event_hash is None):
        return False
    if event_id is not None and (
        not _strict_text(event_id, 200) or not _SHA256.fullmatch(_strict_text(event_hash, 64))
    ):
        return False
    deep_read = value.get("deep_read")
    if not isinstance(deep_read, dict) or set(deep_read) != {
        "available",
        "generation_id",
        "generated_at",
        "content_hash",
        "compact_raw_sha256",
    }:
        return False
    if deep_read.get("available") is True:
        return bool(
            _strict_text(deep_read.get("generation_id"), 200)
            and _parse_datetime(_strict_text(deep_read.get("generated_at"), 80))
            and _SHA256.fullmatch(_strict_text(deep_read.get("content_hash"), 64))
            and _SHA256.fullmatch(_strict_text(deep_read.get("compact_raw_sha256"), 64))
        )
    return deep_read.get("available") is False and all(
        deep_read.get(key) is None
        for key in ("generation_id", "generated_at", "content_hash", "compact_raw_sha256")
    )


def _assessment_from_manifest(manifest: dict[str, Any]) -> GWorkingSetAssessment:
    return GWorkingSetAssessment(
        status=GWorkingSetStatus(str(manifest["status"])),
        data_gaps=tuple(str(gap) for gap in manifest["data_gaps"]),
        canonical_sha256=str(manifest["canonical_sha256"]),
        evaluated_at=str(manifest["evaluated_at"]),
        manifest=manifest,
    )


def _publication_raw(assessment: GWorkingSetAssessment) -> bytes:
    raw = _canonical_json(assessment.manifest) + b"\n"
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError("g working-set manifest is oversized")
    return raw


def _validate_publication_plan(plan: GWorkingSetPublicationPlan) -> None:
    if type(plan) is not GWorkingSetPublicationPlan:
        raise ValueError("g working-set publication plan is invalid")
    publication_at = _parse_datetime(plan.publication_at)
    if publication_at is None or publication_at.isoformat() != plan.publication_at:
        raise ValueError("g working-set publication time is invalid")
    if plan.prior_manifest_identity != "MISSING" and not _SHA256.fullmatch(
        plan.prior_manifest_identity
    ):
        raise ValueError("g working-set prior manifest identity is invalid")
    if (
        not _SHA256.fullmatch(plan.expected_owner_id)
        or
        not _SHA256.fullmatch(plan.expected_generation)
        or not _SHA256.fullmatch(plan.expected_source_coverage_sha256)
        or not _SHA256.fullmatch(plan.expected_evidence_sha256)
    ):
        raise ValueError("g working-set publication evidence is invalid")


def _open_owned_directory(path: Path, *, owner_only: bool) -> int:
    """Open an absolute directory without following any path component."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory path is not canonical")
    current_fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        info = os.fstat(current_fd)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or (owner_only and mode != 0o700)
        ):
            raise ValueError("owned directory boundary is invalid")
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _read_file_at(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int,
    required_mode: int | None = None,
) -> tuple[_FileSnapshot | None, str]:
    if not name or "/" in name or name in {".", ".."}:
        return None, "invalid"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "invalid"
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > max_bytes
            or (required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode)
        ):
            return None, "invalid"
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                return None, "invalid"
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            return None, "invalid"
        after = os.fstat(fd)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            stat.S_IMODE(before.st_mode),
        )
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_uid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            stat.S_IMODE(after.st_mode),
        ):
            return None, "invalid"
        raw = b"".join(chunks)
        return (
            _FileSnapshot(
                raw=raw,
                sha256=hashlib.sha256(raw).hexdigest(),
                modified_at=datetime.fromtimestamp(before.st_mtime, tz=UTC).isoformat(),
                mode=stat.S_IMODE(before.st_mode),
            ),
            "",
        )
    finally:
        os.close(fd)


def _read_source_file(path: Path, *, max_bytes: int) -> tuple[_FileSnapshot | None, str]:
    try:
        directory_fd = _open_owned_directory(path.absolute().parent, owner_only=False)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "invalid"
    try:
        return _read_file_at(directory_fd, path.name, max_bytes=max_bytes)
    finally:
        os.close(directory_fd)


def _source_projection(snapshot: _FileSnapshot | None) -> dict[str, object]:
    return {
        "sha256": snapshot.sha256 if snapshot else "",
        "modified_at": snapshot.modified_at if snapshot else "",
    }


def _validate_existing_target(directory_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("g working-set manifest target is invalid")


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("g working-set manifest write failed")
        view = view[written:]


def _parse_datetime(value: str, *, default_tz: timezone | None = None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        if default_tz is None:
            return None
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed.astimezone(UTC)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("g working-set time must be timezone-aware")
    return value.astimezone(UTC)


def _strict_text(value: object, limit: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > limit:
        return ""
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    if isinstance(value, Mapping):
        value = dict(value)
        value.pop("canonical_sha256", None)
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _append_gap(gaps: list[str], gap: str) -> None:
    if gap not in gaps and len(gaps) < _MAX_GAPS:
        gaps.append(gap)


def _extend_gaps(gaps: list[str], values: tuple[str, ...]) -> None:
    for gap in values:
        _append_gap(gaps, gap)


__all__ = [
    "GWorkingSetAssessment",
    "GWorkingSetPublicationDisposition",
    "GWorkingSetPublicationPlan",
    "GWorkingSetPublicationResult",
    "GWorkingSetReader",
    "GWorkingSetService",
    "GWorkingSetStatus",
]
