"""Typed official company records without a generic web or Agent cache."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

_QUERY_FRESHNESS = timedelta(minutes=15)
_MAX_INSTRUMENTS = 5
_SHA256_HEX_LENGTH = 64
_RELATIVE_ARTIFACT_ROOT = Path("fin-analyse/official-record-evidence-v1")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class OfficialRecordEvidenceRequest:
    """One bounded, read-only official-record lookup."""

    instruments: tuple[str, ...]
    as_of: datetime
    deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OfficialRecordCapture:
    """Immutable raw-response identity for one requested instrument."""

    provider: str
    revision: str
    retrieved_at: datetime
    stale: bool = False


@dataclass(frozen=True, slots=True)
class OfficialRecordDocument:
    """One official document metadata record; its raw bytes stay in the owner store."""

    symbol: str
    document_id: str
    document_kind: str
    title: str
    source_event_at: datetime
    url: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class OfficialRecordSourceResult:
    """Source-native records and capture identities before FIN projects them."""

    documents: Mapping[str, tuple[OfficialRecordDocument, ...]]
    captures: Mapping[str, OfficialRecordCapture]
    data_gaps: tuple[str, ...] = ()


class _OfficialRecordSource(Protocol):
    def read(self, request: OfficialRecordEvidenceRequest) -> OfficialRecordSourceResult: ...


@dataclass(frozen=True, slots=True)
class OfficialRecordEvidence:
    status: str
    as_of: datetime
    valid_until: datetime | None
    instruments: tuple[dict[str, object], ...]
    data_gaps: tuple[str, ...] = ()

    def to_agent_dict(self) -> dict[str, object]:
        return {
            "schema_version": "fin.official-record-evidence/v1",
            "source_boundary": "a_share_official_record_evidence",
            "source_kind": "external_reference",
            "source_trust": "non_g",
            "status": self.status,
            "as_of": self.as_of.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until is not None else None,
            "instruments": list(self.instruments),
            "data_gaps": list(self.data_gaps),
        }


class OfficialRecordEvidenceReader(Protocol):
    def read(self, request: OfficialRecordEvidenceRequest) -> OfficialRecordEvidence: ...


class OfficialRecordEvidenceService:
    """Project source-native official records through one small read seam."""

    def __init__(self, *, source: _OfficialRecordSource | None = None) -> None:
        self._source = source

    def read(self, request: OfficialRecordEvidenceRequest) -> OfficialRecordEvidence:
        _validate_request(request)
        if self._source is None:
            return _unavailable_evidence(request, "OFFICIAL_RECORD_EVIDENCE_SOURCE_UNAVAILABLE")
        try:
            source_result = self._source.read(request)
        except Exception:
            return _unavailable_evidence(request, "OFFICIAL_RECORD_EVIDENCE_SOURCE_UNAVAILABLE")
        if not _valid_source_result(source_result):
            return _unavailable_evidence(request, "OFFICIAL_RECORD_EVIDENCE_SOURCE_RESULT_INVALID")

        instruments: list[dict[str, object]] = []
        gaps: list[str] = []
        valid_until_values: list[datetime] = []
        for symbol in request.instruments:
            item, item_gaps, valid_until = _instrument_projection(
                symbol=symbol,
                source=source_result,
                as_of=request.as_of,
            )
            instruments.append(item)
            _extend(gaps, item_gaps)
            if valid_until is not None:
                valid_until_values.append(valid_until)
        _extend(gaps, source_result.data_gaps)
        return OfficialRecordEvidence(
            status=_overall_status(instruments, gaps),
            as_of=request.as_of,
            valid_until=min(valid_until_values) if valid_until_values else None,
            instruments=tuple(instruments),
            data_gaps=tuple(gaps),
        )


def build_default_official_record_evidence(
    *,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> OfficialRecordEvidenceService:
    """Build the production reader without creating source state until read."""

    effective_clock = clock or (lambda: datetime.now(UTC))
    environment = os.environ if environ is None else environ
    try:
        from fin_analyse.market.data_qualification import ObservationEvidenceOrigin
        from fin_analyse.official_records.cninfo import CninfoOfficialRecordSource

        source: _OfficialRecordSource | None = CninfoOfficialRecordSource(
            artifact_root=official_record_artifact_root(environ=environment),
            evidence_origin=ObservationEvidenceOrigin.LIVE_CAPTURE,
            clock=effective_clock,
        )
    except (OSError, ValueError):
        source = None
    return OfficialRecordEvidenceService(source=source)


def official_record_artifact_root(
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    configured = environment.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    root = (base / _RELATIVE_ARTIFACT_ROOT).resolve()
    project = _PROJECT_ROOT.resolve()
    if (
        not root.is_absolute()
        or root == project
        or project in root.parents
        or root in project.parents
    ):
        raise ValueError("official record artifact root must be outside the checkout")
    return root


def _instrument_projection(
    *,
    symbol: str,
    source: OfficialRecordSourceResult,
    as_of: datetime,
) -> tuple[dict[str, object], tuple[str, ...], datetime | None]:
    capture = source.captures.get(symbol)
    documents = source.documents.get(symbol)
    scoped_gaps = _scoped_gaps(source.data_gaps, symbol)
    if not _valid_capture(capture) or not isinstance(documents, tuple):
        gap = "OFFICIAL_RECORD_EVIDENCE_UNAVAILABLE"
        unavailable_gaps = tuple(dict.fromkeys((*scoped_gaps, gap)))
        return _unknown_instrument(symbol, unavailable_gaps), unavailable_gaps, None
    assert capture is not None

    projected_documents: list[dict[str, object]] = []
    invalid = False
    future_excluded = False
    for document in documents:
        if not _valid_document(document, symbol=symbol):
            invalid = True
            continue
        if document.source_event_at > as_of:
            future_excluded = True
            continue
        projected_documents.append(_document_projection(document))
    item_gaps: list[str] = list(scoped_gaps)
    if capture.stale:
        _extend(item_gaps, ("OFFICIAL_RECORD_EVIDENCE_STALE_CACHE",))
    if invalid:
        _extend(item_gaps, ("OFFICIAL_RECORD_EVIDENCE_DOCUMENT_INVALID",))
    if future_excluded:
        _extend(item_gaps, ("OFFICIAL_RECORD_EVIDENCE_FUTURE_DOCUMENT_EXCLUDED",))
    status = (
        "READY"
        if projected_documents and not item_gaps
        else "EMPTY"
        if not projected_documents and not item_gaps
        else "PARTIAL"
    )
    return (
        {
            "symbol": symbol,
            "status": status,
            "source": {
                "provider": capture.provider,
                "revision": capture.revision,
                "payload_sha256": capture.revision,
                "retrieved_at": capture.retrieved_at.isoformat(),
                "stale": capture.stale,
            },
            "documents": projected_documents,
            "data_gaps": item_gaps,
        },
        tuple(item_gaps),
        capture.retrieved_at + _QUERY_FRESHNESS,
    )


def _unavailable_evidence(
    request: OfficialRecordEvidenceRequest,
    gap: str,
) -> OfficialRecordEvidence:
    instruments = tuple(_unknown_instrument(symbol, (gap,)) for symbol in request.instruments)
    return OfficialRecordEvidence(
        status="UNKNOWN",
        as_of=request.as_of,
        valid_until=None,
        instruments=instruments,
        data_gaps=(gap,),
    )


def _unknown_instrument(symbol: str, gaps: Sequence[str]) -> dict[str, object]:
    return {
        "symbol": symbol,
        "status": "UNKNOWN",
        "source": None,
        "documents": [],
        "data_gaps": list(gaps),
    }


def _document_projection(document: OfficialRecordDocument) -> dict[str, object]:
    return {
        "document_id": document.document_id,
        "document_kind": document.document_kind,
        "title": document.title,
        "source_event_at": document.source_event_at.isoformat(),
        "url": document.url,
        "content_hash": document.content_hash,
    }


def _overall_status(instruments: Sequence[Mapping[str, object]], gaps: Sequence[str]) -> str:
    statuses = [str(item.get("status", "UNKNOWN")) for item in instruments]
    if not statuses:
        return "EMPTY"
    if all(status == "EMPTY" for status in statuses) and not gaps:
        return "EMPTY"
    if all(status in {"READY", "EMPTY"} for status in statuses) and not gaps:
        return "READY"
    if any(status in {"READY", "EMPTY", "PARTIAL"} for status in statuses):
        return "PARTIAL"
    return "UNKNOWN"


def _valid_capture(value: object) -> bool:
    return (
        isinstance(value, OfficialRecordCapture)
        and bool(value.provider)
        and _sha256(value.revision)
        and _aware(value.retrieved_at)
    )


def _valid_source_result(value: object) -> bool:
    return (
        isinstance(value, OfficialRecordSourceResult)
        and isinstance(value.documents, Mapping)
        and isinstance(value.captures, Mapping)
        and isinstance(value.data_gaps, tuple)
        and all(isinstance(gap, str) for gap in value.data_gaps)
    )


def _valid_document(value: object, *, symbol: str) -> bool:
    return (
        isinstance(value, OfficialRecordDocument)
        and value.symbol == symbol
        and bool(value.document_id)
        and value.document_kind in {"announcement", "financial_report"}
        and bool(value.title)
        and _aware(value.source_event_at)
        and value.url.startswith("https://")
        and _sha256(value.content_hash)
    )


def _scoped_gaps(gaps: Sequence[object], symbol: str) -> tuple[str, ...]:
    suffix = f":{symbol}"
    return tuple(
        dict.fromkeys(gap for gap in gaps if isinstance(gap, str) and gap.endswith(suffix))
    )


def _extend(target: list[str], values: Sequence[object]) -> None:
    for value in values:
        if isinstance(value, str) and value not in target:
            target.append(value)


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_HEX_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_request(request: OfficialRecordEvidenceRequest) -> None:
    if (
        not isinstance(request, OfficialRecordEvidenceRequest)
        or not _aware(request.as_of)
        or (request.deadline_at is not None and not _aware(request.deadline_at))
        or not isinstance(request.instruments, tuple)
        or not request.instruments
        or len(request.instruments) > _MAX_INSTRUMENTS
        or len(set(request.instruments)) != len(request.instruments)
        or any(not isinstance(symbol, str) or not symbol for symbol in request.instruments)
    ):
        raise ValueError("official record evidence request is invalid")


__all__ = [
    "OfficialRecordCapture",
    "OfficialRecordDocument",
    "OfficialRecordEvidence",
    "OfficialRecordEvidenceReader",
    "OfficialRecordEvidenceRequest",
    "OfficialRecordEvidenceService",
    "OfficialRecordSourceResult",
    "build_default_official_record_evidence",
    "official_record_artifact_root",
]
