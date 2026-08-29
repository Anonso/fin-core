"""Immutable CNInfo captures for official company-record evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from fin_analyse.market.data_qualification import ObservationEvidenceOrigin
from fin_analyse.official_records.evidence import (
    OfficialRecordCapture,
    OfficialRecordDocument,
    OfficialRecordEvidenceRequest,
    OfficialRecordSourceResult,
)

_CNINFO_QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
_CNINFO_BASE = "https://static.cninfo.com.cn/"
_CNINFO_REFERER = "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"
_CNINFO_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": "https://www.cninfo.com.cn",
    "Referer": _CNINFO_REFERER,
    "User-Agent": "fin-analyse-official-record-evidence/1",
    "X-Requested-With": "XMLHttpRequest",
}
_SOURCE_ID = "cninfo"
_ARTIFACT_SCHEMA = "cninfo_official_record_evidence_artifact.v1"
_MAX_RAW_BYTES = 4 * 1024 * 1024
_MAX_DOCUMENTS_PER_INSTRUMENT = 30
_MAX_DOCUMENT_ID_CHARS = 128
_MAX_DOCUMENT_TITLE_CHARS = 512
_MAX_DOCUMENT_URL_CHARS = 2_048
_QUERY_LOOKBACK = timedelta(days=365)
_A_SHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class CninfoOfficialRecordError(RuntimeError):
    """A safe, typed CNInfo adapter failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


class CninfoOfficialRecordHttpPost(Protocol):
    def __call__(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool,
    ) -> _HttpResponse: ...


@dataclass(frozen=True, slots=True)
class _RawCapture:
    raw_payload: bytes
    revision: str
    retrieved_at: datetime
    stale: bool = False


class CninfoOfficialRecordSource:
    """Capture bounded official disclosure metadata with exact replay bytes."""

    def __init__(
        self,
        *,
        artifact_root: Path,
        http_post: CninfoOfficialRecordHttpPost | None = None,
        evidence_origin: ObservationEvidenceOrigin = ObservationEvidenceOrigin.TEST_ONLY,
        timeout_seconds: float = 12.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(artifact_root, Path):
            raise TypeError("official record artifact root must be pathlib.Path")
        if type(evidence_origin) is not ObservationEvidenceOrigin:
            raise TypeError("official record evidence origin is invalid")
        if http_post is not None and evidence_origin is not ObservationEvidenceOrigin.TEST_ONLY:
            raise CninfoOfficialRecordError(
                "OFFICIAL_RECORD_EVIDENCE_INJECTED_TRANSPORT_MUST_BE_TEST_ONLY"
            )
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("official record timeout must be finite and positive")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("official record timeout must be finite and positive")
        self._store = _OfficialRecordArtifactStore(artifact_root)
        self._http_post = requests.post if http_post is None else http_post
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def provider_id(self) -> str:
        return _SOURCE_ID

    def read(self, request: OfficialRecordEvidenceRequest) -> OfficialRecordSourceResult:
        _validate_request(request)
        documents: dict[str, tuple[OfficialRecordDocument, ...]] = {}
        captures: dict[str, OfficialRecordCapture] = {}
        gaps: list[str] = []
        for symbol in request.instruments:
            if _symbol_parts(symbol) is None:
                _extend(gaps, (f"OFFICIAL_RECORD_EVIDENCE_SYMBOL_UNSUPPORTED:{symbol}",))
                continue
            try:
                capture = self._capture(symbol, request=request)
                parsed, parse_gaps = _parse_documents(capture, symbol=symbol)
            except CninfoOfficialRecordError as error:
                _extend(gaps, (f"{error.code}:{symbol}",))
                continue
            documents[symbol] = parsed
            captures[symbol] = OfficialRecordCapture(
                provider=_SOURCE_ID,
                revision=capture.revision,
                retrieved_at=capture.retrieved_at,
                stale=capture.stale,
            )
            _extend(gaps, tuple(f"{gap}:{symbol}" for gap in parse_gaps))
            if capture.stale:
                _extend(gaps, (f"OFFICIAL_RECORD_EVIDENCE_STALE_CACHE:{symbol}",))
        return OfficialRecordSourceResult(
            documents=documents,
            captures=captures,
            data_gaps=tuple(gaps),
        )

    def _capture(
        self,
        symbol: str,
        *,
        request: OfficialRecordEvidenceRequest,
    ) -> _RawCapture:
        scope = (_SOURCE_ID, symbol, _date_range(request.as_of))
        now = _aware_now(self._clock)
        timeout = self._timeout_seconds
        if request.deadline_at is not None:
            remaining = (request.deadline_at - now).total_seconds()
            if remaining <= 0:
                raise CninfoOfficialRecordError("OFFICIAL_RECORD_EVIDENCE_DEADLINE_REACHED")
            timeout = min(timeout, remaining)
        try:
            response = self._http_post(
                _CNINFO_QUERY,
                data=_request_payload(symbol, as_of=request.as_of),
                headers=_CNINFO_HEADERS,
                timeout=timeout,
                allow_redirects=False,
            )
            raw_payload = response.content
            if (
                response.status_code != 200
                or not isinstance(raw_payload, bytes)
                or len(raw_payload) > _MAX_RAW_BYTES
            ):
                raise CninfoOfficialRecordError("OFFICIAL_RECORD_EVIDENCE_HTTP_RESPONSE_INVALID")
            return self._store.capture(
                scope,
                raw_payload=raw_payload,
                retrieved_at=_aware_now(self._clock),
            )
        except CninfoOfficialRecordError:
            raise
        except Exception as error:
            cached = self._store.load_latest(scope)
            if cached is not None:
                return _RawCapture(
                    raw_payload=cached.raw_payload,
                    revision=cached.revision,
                    retrieved_at=cached.retrieved_at,
                    stale=True,
                )
            raise CninfoOfficialRecordError(
                "OFFICIAL_RECORD_EVIDENCE_TRANSPORT_UNAVAILABLE"
            ) from error


def _request_payload(symbol: str, *, as_of: datetime) -> dict[str, str]:
    code, venue = _require_symbol(symbol)
    return {
        "pageNum": "1",
        "pageSize": str(_MAX_DOCUMENTS_PER_INSTRUMENT),
        "column": "sse" if venue == "SH" else "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": f"{code},{_org_id(code, venue)}",
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": _date_range(as_of),
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }


def _date_range(as_of: datetime) -> str:
    end = as_of.astimezone(_A_SHARE_TIMEZONE).date()
    start = end - _QUERY_LOOKBACK
    return f"{start.isoformat()}~{end.isoformat()}"


def _org_id(code: str, venue: str) -> str:
    return ("gssh0" if venue == "SH" else "gssz0") + code


def _parse_documents(
    capture: _RawCapture,
    *,
    symbol: str,
) -> tuple[tuple[OfficialRecordDocument, ...], tuple[str, ...]]:
    try:
        payload = json.loads(capture.raw_payload)
    except (TypeError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise CninfoOfficialRecordError("OFFICIAL_RECORD_EVIDENCE_PAYLOAD_INVALID") from error
    if not isinstance(payload, Mapping):
        raise CninfoOfficialRecordError("OFFICIAL_RECORD_EVIDENCE_PAYLOAD_INVALID")
    rows = _announcement_rows(payload)
    if rows is None:
        raise CninfoOfficialRecordError("OFFICIAL_RECORD_EVIDENCE_PAYLOAD_INVALID")
    code, _venue = _require_symbol(symbol)
    documents: list[OfficialRecordDocument] = []
    gaps: list[str] = []
    for row in rows:
        parsed = _parse_document(row, symbol=symbol, code=code)
        if parsed is None:
            _extend(gaps, ("OFFICIAL_RECORD_EVIDENCE_DOCUMENT_INVALID",))
            continue
        documents.append(parsed)
    if _records_truncated(payload):
        _extend(gaps, ("OFFICIAL_RECORD_EVIDENCE_DOCUMENTS_TRUNCATED",))
    documents.sort(key=lambda item: (item.source_event_at, item.document_id), reverse=True)
    return tuple(documents[:_MAX_DOCUMENTS_PER_INSTRUMENT]), tuple(gaps)


def _announcement_rows(
    payload: Mapping[object, object],
) -> tuple[Mapping[object, object], ...] | None:
    announcements = payload.get("announcements")
    classified = payload.get("classifiedAnnouncements")
    if announcements is None and classified is None:
        return ()
    if announcements is not None:
        if not isinstance(announcements, list):
            return None
        announcement_rows: list[Mapping[object, object]] = []
        for row in announcements:
            if not isinstance(row, Mapping):
                return None
            announcement_rows.append(row)
        if announcement_rows or classified is None:
            return tuple(announcement_rows)
    if not isinstance(classified, list):
        return None
    rows: list[Mapping[object, object]] = []
    for group in classified:
        if isinstance(group, Mapping):
            rows.append(group)
        elif isinstance(group, list):
            for row in group:
                if not isinstance(row, Mapping):
                    return None
                rows.append(row)
        else:
            return None
    return tuple(rows)


def _records_truncated(payload: Mapping[object, object]) -> bool:
    total = payload.get("totalRecordNum")
    if isinstance(total, int) and not isinstance(total, bool):
        return total > _MAX_DOCUMENTS_PER_INSTRUMENT
    if isinstance(total, str) and total.isdecimal():
        return int(total) > _MAX_DOCUMENTS_PER_INSTRUMENT
    return False


def _parse_document(
    row: Mapping[object, object],
    *,
    symbol: str,
    code: str,
) -> OfficialRecordDocument | None:
    row_code = _text(row.get("secCode"))
    if row_code is not None and row_code != code:
        return None
    document_id = _bounded_text(row.get("announcementId"), _MAX_DOCUMENT_ID_CHARS) or _bounded_text(
        row.get("id"), _MAX_DOCUMENT_ID_CHARS
    )
    title = _bounded_text(row.get("announcementTitle"), _MAX_DOCUMENT_TITLE_CHARS)
    event_at = _timestamp(row.get("announcementTime"))
    adjunct_url = _bounded_text(row.get("adjunctUrl"), _MAX_DOCUMENT_URL_CHARS)
    url = _official_document_url(adjunct_url)
    if not document_id or not title or event_at is None or url is None:
        return None
    return OfficialRecordDocument(
        symbol=symbol,
        document_id=document_id,
        document_kind=_document_kind(title),
        title=title,
        source_event_at=event_at,
        url=url,
        content_hash=hashlib.sha256(_json_bytes(row)).hexdigest(),
    )


def _official_document_url(value: str | None) -> str | None:
    if value is None or any(ord(character) < 32 for character in value):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        try:
            trusted = (
                parsed.scheme == "https"
                and parsed.hostname == "static.cninfo.com.cn"
                and parsed.username is None
                and parsed.password is None
                and parsed.port in {None, 443}
                and bool(parsed.path)
            )
        except ValueError:
            return None
        return value if trusted else None
    return _CNINFO_BASE + value.lstrip("/") if parsed.path else None


def _document_kind(title: str) -> str:
    return (
        "financial_report"
        if any(marker in title for marker in ("年度报告", "季度报告", "半年度报告"))
        else "announcement"
    )


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(numeric / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class _OfficialRecordArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def capture(
        self,
        scope: tuple[str, ...],
        *,
        raw_payload: bytes,
        retrieved_at: datetime,
    ) -> _RawCapture:
        _ensure_root(self._root)
        key = _scope_key(scope)
        revision = hashlib.sha256(raw_payload).hexdigest()
        with _artifact_lock(self._root, key):
            scope_root = self._root / "artifacts" / key
            _ensure_directory(scope_root)
            artifact = scope_root / revision
            if artifact.exists():
                if hashlib.sha256(_read_regular(artifact / "raw.bin")).hexdigest() != revision:
                    raise CninfoOfficialRecordError("OFFICIAL_RECORD_EVIDENCE_ARTIFACT_INVALID")
            else:
                _ensure_directory(artifact)
                _write_atomic(artifact, "raw.bin", raw_payload)
                _write_atomic(
                    artifact,
                    "manifest.json",
                    _json_bytes(
                        {
                            "schema_version": _ARTIFACT_SCHEMA,
                            "scope": list(scope),
                            "revision": revision,
                            "raw_payload_sha256": revision,
                        }
                    ),
                )
            _write_atomic(
                self._root / "latest",
                f"{key}.json",
                _json_bytes(
                    {
                        "schema_version": _ARTIFACT_SCHEMA,
                        "scope": list(scope),
                        "revision": revision,
                        "retrieved_at": retrieved_at.isoformat(),
                    }
                ),
            )
        return _RawCapture(raw_payload=raw_payload, revision=revision, retrieved_at=retrieved_at)

    def load_latest(self, scope: tuple[str, ...]) -> _RawCapture | None:
        try:
            _ensure_root(self._root)
            key = _scope_key(scope)
            payload = _read_regular(self._root / "latest" / f"{key}.json")
            manifest = json.loads(payload)
            if (
                not isinstance(manifest, Mapping)
                or manifest.get("schema_version") != _ARTIFACT_SCHEMA
                or manifest.get("scope") != list(scope)
                or not isinstance(manifest.get("revision"), str)
                or not isinstance(manifest.get("retrieved_at"), str)
            ):
                return None
            revision = manifest["revision"]
            retrieved_at = datetime.fromisoformat(manifest["retrieved_at"])
            if not _aware(retrieved_at):
                return None
            scope_root = self._root / "artifacts" / key
            _ensure_directory(scope_root)
            raw_payload = _read_regular(scope_root / revision / "raw.bin")
            if hashlib.sha256(raw_payload).hexdigest() != revision:
                return None
            return _RawCapture(
                raw_payload=raw_payload, revision=revision, retrieved_at=retrieved_at
            )
        except CninfoOfficialRecordError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None


def _ensure_root(root: Path) -> None:
    _ensure_directory(root)
    _ensure_directory(root / "artifacts")
    _ensure_directory(root / "latest")
    _ensure_directory(root / "locks")


def _ensure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise CninfoOfficialRecordError("OFFICIAL_RECORD_EVIDENCE_ARTIFACT_ROOT_INVALID")


@contextmanager
def _artifact_lock(root: Path, key: str) -> Iterator[None]:
    path = root / "locks" / f"{key}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_atomic(directory: Path, name: str, payload: bytes) -> None:
    _ensure_directory(directory)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / name)
    except Exception:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _read_regular(path: Path) -> bytes:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError("official record artifact is not a regular file")
    with path.open("rb") as handle:
        return handle.read()


def _scope_key(scope: tuple[str, ...]) -> str:
    return hashlib.sha256(_json_bytes(list(scope))).hexdigest()


def _symbol_parts(symbol: object) -> tuple[str, str] | None:
    if not isinstance(symbol, str) or len(symbol) != 9 or symbol[6] != ".":
        return None
    code, venue = symbol[:6], symbol[7:]
    if not code.isdigit() or venue not in {"SH", "SZ"}:
        return None
    return code, venue


def _require_symbol(symbol: str) -> tuple[str, str]:
    parts = _symbol_parts(symbol)
    if parts is None:
        raise CninfoOfficialRecordError("OFFICIAL_RECORD_EVIDENCE_SYMBOL_UNSUPPORTED")
    return parts


def _validate_request(request: OfficialRecordEvidenceRequest) -> None:
    if (
        not isinstance(request, OfficialRecordEvidenceRequest)
        or not _aware(request.as_of)
        or (request.deadline_at is not None and not _aware(request.deadline_at))
        or not isinstance(request.instruments, tuple)
        or not request.instruments
        or len(request.instruments) > 5
        or len(set(request.instruments)) != len(request.instruments)
    ):
        raise ValueError("official record evidence request is invalid")


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if not _aware(now):
        raise CninfoOfficialRecordError("OFFICIAL_RECORD_EVIDENCE_CLOCK_INVALID")
    return now


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _text(value: object) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _bounded_text(value: object, maximum: int) -> str | None:
    text = _text(value)
    return text if text is not None and len(text) <= maximum else None


def _extend(target: list[str], values: Sequence[object]) -> None:
    for value in values:
        if isinstance(value, str) and value not in target:
            target.append(value)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


__all__ = ["CninfoOfficialRecordError", "CninfoOfficialRecordSource"]
