"""Deterministic, read-only reference evidence for FIN knowledge products.

This module owns retrieval projection once for every product that needs local
ZSXQ material.  It never calls an Agent, performs network I/O, writes state, or
promotes a knowledge document to teacher cognition.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Literal, Protocol, TypeGuard
from zoneinfo import ZoneInfo

from fin_analyse.ingestion.models import RawDocument
from fin_analyse.knowledge.query import (
    KnowledgeQueryRequest,
    KnowledgeQueryResult,
    KnowledgeQueryService,
)
from fin_analyse.knowledge.store import KnowledgeStore

KnowledgeReferenceClass = Literal[
    "ZSXQ_QA_REFERENCE",
    "ZSXQ_CURATED_REFERENCE",
    "ZSXQ_REFERENCE",
]
KnowledgeReferenceStatus = Literal["READY", "EMPTY", "UNKNOWN"]

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_DOCUMENTS = 10
_MAX_QUERY_CHARS = 4_000
_MAX_TITLE_CHARS = 300
_MAX_DOCUMENT_CONTENT_CHARS = 8_000
_MAX_BUNDLE_CHARS = 32_000
_MAX_SOURCE_REF_CHARS = 256
_SUPPORTED_WINDOWS = frozenset({"3d", "7d", "14d", "30d", "60d", "90d", "180d", "365d", "all"})
_ADMIN_MARKERS = (
    "新人必看",
    "提问关闭",
    "置顶",
    "星球规则",
    "返回 大锅饭与小伙伴的进步空间",
)
_CURATED_COLUMNS = frozenset(
    {
        "星大派特刊",
        "星大派锐评",
        "研报",
        "研报分享",
        "知识星球精华",
    }
)
_GAP_CODE = re.compile(r"^[a-z0-9_.:-]{1,80}$")


@dataclass(frozen=True, slots=True)
class KnowledgeReferenceRequest:
    """One bounded local-knowledge retrieval request."""

    query: str
    window: str = "180d"
    as_of: datetime | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeReferenceItem:
    """One source-bound, non-G reference item."""

    source_ref: str
    source_class: KnowledgeReferenceClass
    source_trust: Literal["NON_G_REFERENCE"]
    reference_only: Literal[True]
    instruction_authority: Literal["none"]
    title: str
    published_at: str | None
    available_at: str | None
    content: str
    content_sha256: str

    def to_runtime_context(self) -> dict[str, object]:
        """Project a bounded untrusted context item for a downstream Agent."""

        return {
            "source_ref": self.source_ref,
            "source_kind": "knowledge_document",
            "source_class": self.source_class,
            "trust": "reference_evidence",
            "source_trust": self.source_trust,
            "reference_only": self.reference_only,
            "instruction_authority": self.instruction_authority,
            "title": self.title,
            "date": self.published_at or "",
            "published_at": self.published_at,
            "available_at": self.available_at,
            "content": self.content,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeReferenceBundle:
    """A complete deterministic retrieval outcome."""

    status: KnowledgeReferenceStatus
    window: str
    items: tuple[KnowledgeReferenceItem, ...] = ()
    data_gaps: tuple[str, ...] = ()


class _KnowledgeQueryPort(Protocol):
    def query(self, request: KnowledgeQueryRequest) -> KnowledgeQueryResult: ...


class _KnowledgeSnapshotProvider(Protocol):
    def resolve(self) -> tuple[KnowledgeStore, _KnowledgeQueryPort]: ...


@dataclass(slots=True)
class _FixedKnowledgeSnapshot:
    store: KnowledgeStore
    query: _KnowledgeQueryPort

    def resolve(self) -> tuple[KnowledgeStore, _KnowledgeQueryPort]:
        return self.store, self.query


class _RootKnowledgeSnapshot:
    """Fingerprint-aware process cache for one canonical knowledge root."""

    def __init__(self, kb_root: Path) -> None:
        self._root = kb_root.expanduser().resolve(strict=True)
        self._lock = RLock()
        self._fingerprint: str | None = None
        self._store: KnowledgeStore | None = None
        self._query: KnowledgeQueryService | None = None

    def resolve(self) -> tuple[KnowledgeStore, _KnowledgeQueryPort]:
        with self._lock:
            fingerprint = self._read_fingerprint()
            if (
                fingerprint == self._fingerprint
                and self._store is not None
                and self._query is not None
            ):
                return self._store, self._query
            for _attempt in range(2):
                before = self._read_fingerprint()
                store = self._load_store()
                after = self._read_fingerprint()
                if before != after:
                    continue
                query = KnowledgeQueryService(store)
                self._fingerprint = after
                self._store = store
                self._query = query
                return store, query
        raise OSError("knowledge reference generation changed during load")

    def _load_store(self) -> KnowledgeStore:
        from fin_analyse.claims import RuleBasedClaimExtractor
        from fin_analyse.ingestion.zsxq_adapter import ZsxqMarkdownAdapter

        return KnowledgeStore.from_adapter(
            ZsxqMarkdownAdapter(root=self._root),
            RuleBasedClaimExtractor(),
        )

    def _read_fingerprint(self) -> str:
        index_path = self._root / "index.json"
        articles_root = self._root / "articles"
        index_stat = index_path.lstat()
        articles_stat = articles_root.lstat()
        if (
            not stat.S_ISREG(index_stat.st_mode)
            or index_path.is_symlink()
            or index_stat.st_size > 8 * 1024 * 1024
            or not stat.S_ISDIR(articles_stat.st_mode)
            or articles_root.is_symlink()
        ):
            raise OSError("knowledge reference root is invalid")
        digest = hashlib.sha256(index_path.read_bytes())
        for path in sorted(articles_root.glob("*.md")):
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
                raise OSError("knowledge reference article is invalid")
            digest.update(path.name.encode("utf-8"))
            digest.update(str(metadata.st_size).encode("ascii"))
            digest.update(str(metadata.st_mtime_ns).encode("ascii"))
        return digest.hexdigest()


class KnowledgeReferenceReader:
    """Deep read-only module for bounded local reference retrieval."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        query_service: _KnowledgeQueryPort | None = None,
    ) -> None:
        self._snapshot: _KnowledgeSnapshotProvider = _FixedKnowledgeSnapshot(
            store=store,
            query=query_service or KnowledgeQueryService(store),
        )

    @classmethod
    def from_root(cls, kb_root: Path) -> KnowledgeReferenceReader:
        """Build a reader that refreshes only when the source generation changes."""

        instance = cls.__new__(cls)
        instance._snapshot = _RootKnowledgeSnapshot(kb_root)
        return instance

    def read(self, request: KnowledgeReferenceRequest) -> KnowledgeReferenceBundle:
        """Return source-classified reference evidence or a typed failure."""

        if not isinstance(request, KnowledgeReferenceRequest):
            return _unknown("180d", "knowledge_reference_request_invalid")
        query = request.query.strip() if isinstance(request.query, str) else ""
        if not query or len(query) > _MAX_QUERY_CHARS:
            return _unknown("180d", "knowledge_reference_query_invalid")
        if request.window not in _SUPPORTED_WINDOWS:
            return _unknown("180d", "knowledge_reference_window_invalid")
        if request.as_of is not None and (
            request.as_of.tzinfo is None or request.as_of.utcoffset() is None
        ):
            return _unknown(request.window, "knowledge_reference_as_of_invalid")

        try:
            store, query_port = self._snapshot.resolve()
            result = query_port.query(
                KnowledgeQueryRequest(
                    query=query,
                    window=request.window,
                    include_external_context=False,
                    ticker=None,
                    limit=_MAX_DOCUMENTS,
                )
            )
        except Exception:
            return _unknown(request.window, "knowledge_reference_retrieval_unavailable")
        if not isinstance(result, KnowledgeQueryResult) or not isinstance(result.hits, list):
            return _unknown(request.window, "knowledge_reference_result_invalid")

        gaps = list(_bounded_gap_codes(result.data_gaps))
        items: list[KnowledgeReferenceItem] = []
        seen: set[str] = set()
        for hit in result.hits[:_MAX_DOCUMENTS]:
            if not isinstance(hit, dict):
                continue
            source_ref = hit.get("document_id")
            if not _valid_source_ref(source_ref) or source_ref in seen:
                continue
            document = store.get_document(source_ref)
            if (
                document is None
                or not isinstance(document.content, str)
                or not document.content.strip()
                or _is_admin_or_pinned(document)
            ):
                continue
            source_time = _source_time(document.metadata.get("date"))
            if source_time is None:
                _append_gap(gaps, "knowledge_reference_date_missing")
            elif request.as_of is not None and source_time > request.as_of.astimezone(_SHANGHAI):
                _append_gap(gaps, "knowledge_reference_future_excluded")
                continue
            items.append(
                _project_document(document, source_ref=source_ref, source_time=source_time)
            )
            seen.add(source_ref)

        bounded = _bound_bundle(items)
        if len(bounded) < len(items) or any(
            bounded_item.content != original.content
            for bounded_item, original in zip(bounded, items, strict=False)
        ):
            _append_gap(gaps, "knowledge_reference_context_truncated")
        if not bounded:
            _append_gap(gaps, "knowledge_reference_evidence_unavailable")
            return KnowledgeReferenceBundle(
                status="EMPTY",
                window=request.window,
                data_gaps=tuple(gaps),
            )
        return KnowledgeReferenceBundle(
            status="READY",
            window=request.window,
            items=tuple(bounded),
            data_gaps=tuple(gaps),
        )


def _unknown(window: str, gap: str) -> KnowledgeReferenceBundle:
    return KnowledgeReferenceBundle(status="UNKNOWN", window=window, data_gaps=(gap,))


def _valid_source_ref(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_SOURCE_REF_CHARS
        and not any(unicodedata.category(char) == "Cc" for char in value)
    )


def _is_admin_or_pinned(document: RawDocument) -> bool:
    text = f"{document.title}\n{document.content[:500]}"
    return sum(marker in text for marker in _ADMIN_MARKERS) >= 2


def _project_document(
    document: RawDocument,
    *,
    source_ref: str,
    source_time: datetime | None,
) -> KnowledgeReferenceItem:
    published_at = source_time.isoformat() if source_time is not None else None
    return KnowledgeReferenceItem(
        source_ref=source_ref,
        source_class=_source_class(document),
        source_trust="NON_G_REFERENCE",
        reference_only=True,
        instruction_authority="none",
        title=_bounded_text(document.title, _MAX_TITLE_CHARS),
        published_at=published_at,
        available_at=published_at,
        content=_bounded_text(document.content, _MAX_DOCUMENT_CONTENT_CHARS),
        content_sha256=hashlib.sha256(document.content.encode("utf-8")).hexdigest(),
    )


def _source_class(document: RawDocument) -> KnowledgeReferenceClass:
    is_qa = document.metadata.get("is_qa")
    if is_qa is True or "提问" in document.title:
        return "ZSXQ_QA_REFERENCE"
    column = str(document.metadata.get("column", "")).strip()
    if column in _CURATED_COLUMNS:
        return "ZSXQ_CURATED_REFERENCE"
    return "ZSXQ_REFERENCE"


def _source_time(value: object) -> datetime | None:
    if not isinstance(value, (str, int, float)) or not str(value).strip():
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed.astimezone(_SHANGHAI)


def _bound_bundle(items: list[KnowledgeReferenceItem]) -> list[KnowledgeReferenceItem]:
    bounded = list(items)
    while bounded:
        without_content = [asdict(replace(item, content="")) for item in bounded]
        fixed_chars = _json_chars(without_content)
        if fixed_chars < _MAX_BUNDLE_CHARS:
            break
        bounded.pop()
    if not bounded:
        return []

    remaining = max(0, _MAX_BUNDLE_CHARS - fixed_chars - 16)
    projected: list[KnowledgeReferenceItem] = []
    for index, item in enumerate(bounded):
        remaining_items = len(bounded) - index
        allowance = remaining // remaining_items if remaining_items else 0
        content = item.content[:allowance]
        projected.append(replace(item, content=content))
        remaining -= len(content)

    while projected and _json_chars([asdict(item) for item in projected]) > _MAX_BUNDLE_CHARS:
        overflow = _json_chars([asdict(item) for item in projected]) - _MAX_BUNDLE_CHARS
        for index in range(len(projected) - 1, -1, -1):
            content = projected[index].content
            if not content:
                continue
            projected[index] = replace(
                projected[index],
                content=content[: max(0, len(content) - max(1, overflow))],
            )
            break
        else:
            projected.pop()
    return [item for item in projected if item.content]


def _bounded_text(value: object, max_chars: int) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return str(value).strip()[:max_chars]


def _json_chars(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


def _bounded_gap_codes(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    bounded: list[str] = []
    for value in values[:10]:
        if isinstance(value, str) and _GAP_CODE.fullmatch(value) and value not in bounded:
            bounded.append(value)
    return tuple(bounded)


def _append_gap(gaps: list[str], gap: str) -> None:
    if gap not in gaps:
        gaps.append(gap)


__all__ = [
    "KnowledgeReferenceBundle",
    "KnowledgeReferenceClass",
    "KnowledgeReferenceItem",
    "KnowledgeReferenceReader",
    "KnowledgeReferenceRequest",
]
