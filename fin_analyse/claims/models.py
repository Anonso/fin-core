"""Claim data model."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Claim:
    claim_id: str
    source_id: str
    document_id: str
    subject: str
    predicate: str
    object_value: str
    claim_type: str
    polarity: str
    horizon: str
    confidence: float
    evidence_ids: list[str]
    status: str = "active"
    extracted_by: str = "rule"
    metadata: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=utc_now)
