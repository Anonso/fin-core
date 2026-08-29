"""Typed ingestion data models."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SourceInfo:
    source_id: str
    name: str
    source_type: str
    reliability: float
    freshness_policy: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawDocument:
    source_id: str
    external_id: str
    title: str
    content: str
    url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)

    @property
    def document_id(self) -> str:
        return f"{self.source_id}:{self.external_id}"


@dataclass(frozen=True)
class DocumentVersion:
    document_id: str
    version_id: str
    content_checksum: str
    created_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParseArtifact:
    artifact_id: str
    source_id: str
    document_id: str
    artifact_type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_id: str
    document_id: str
    evidence_type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class IngestionRun:
    run_id: str
    source_id: str
    mode: str
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    status: str = "running"
    fetched_count: int = 0
    parsed_count: int = 0
    error_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
