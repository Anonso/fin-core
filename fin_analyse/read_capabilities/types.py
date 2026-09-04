"""Leaf type definitions shared by FIN read capabilities.

These five types previously lived in ``production_runtime`` and
``capability_broker``; moving them here gives every read-only consumer a
stdlib-only import closure.  The read-capability thin server must not pull
either of those modules (they drag the moa engine and the full production
composition), so this module deliberately depends on nothing but the
standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SourceKind(StrEnum):
    """Source identity retained across capability calls."""

    G = "g"
    Z = "z"
    EXTERNAL_REFERENCE = "external_reference"
    USER_CONTEXT = "user_context"
    USER_PORTFOLIO = "user_portfolio"
    MOA_FINDING = "moa_finding"


class SourceTrust(StrEnum):
    """Only FIN may attach the trusted-G marker."""

    FIN_TRUSTED_G = "fin_trusted_g"
    NON_G = "non_g"


@dataclass(frozen=True)
class CapabilitySource:
    ref: str
    kind: SourceKind
    trust: SourceTrust


@dataclass(frozen=True, slots=True)
class ProductionReadRequest:
    """Bounded input shared by the three common semantic read capabilities."""

    question: str
    instruments: tuple[str, ...] = ()
    article_id: str | None = None
    # read_article_search 枚举模式（BUG-029）：YYYY-MM-DD，任一存在即按
    # 日期范围全量枚举（时间升序），绕过 TF-IDF——「按时刻找文章」的口子。
    date_from: str | None = None
    date_to: str | None = None
    as_of: datetime | None = None
    deadline_at: datetime | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.question.strip() or len(self.question) > 8_192:
            raise ValueError("production_read_request_invalid")
        if len(self.instruments) > 64:
            raise ValueError("production_read_request_invalid")
        if any(not item.strip() or len(item) > 128 for item in self.instruments):
            raise ValueError("production_read_request_invalid")
        if self.article_id is not None and (
            not isinstance(self.article_id, str)
            or not self.article_id.strip()
            or len(self.article_id) > 160
        ):
            raise ValueError("production_read_request_invalid")
        if self.as_of is not None and (
            not isinstance(self.as_of, datetime)
            or self.as_of.tzinfo is None
            or self.as_of.utcoffset() is None
        ):
            raise ValueError("production_read_request_invalid")
        if self.deadline_at is not None and (
            not isinstance(self.deadline_at, datetime)
            or self.deadline_at.tzinfo is None
            or self.deadline_at.utcoffset() is None
        ):
            raise ValueError("production_read_request_invalid")


@dataclass(frozen=True, slots=True)
class ProductionReadResult:
    """FIN-owned result before broker source/effect validation."""

    value: dict[str, object]
    sources: tuple[CapabilitySource, ...] = ()
    data_gaps: tuple[str, ...] = ()
