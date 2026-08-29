from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fin_analyse.market.data_qualification import ObservationEvidenceOrigin
from fin_analyse.official_records.cninfo import CninfoOfficialRecordSource
from fin_analyse.official_records.evidence import OfficialRecordEvidenceRequest


@dataclass
class _Response:
    content: bytes
    status_code: int = 200


@dataclass
class _Transport:
    title: str = "2025年年度报告"
    fail: bool = False
    calls: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, url, *, data, headers, timeout, allow_redirects):
        del url, headers, timeout, allow_redirects
        self.calls.append(dict(data))
        if self.fail:
            raise OSError("transport unavailable")
        return _Response(
            json.dumps(
                {
                    "announcements": [
                        {
                            "announcementId": "1200000001",
                            "announcementTitle": self.title,
                            "announcementTime": 1786147200000,
                            "adjunctUrl": "finalpage/2026-08-08/1200000001.PDF",
                            "secCode": "600519",
                        }
                    ]
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )


def test_cninfo_source_preserves_raw_corrections_as_new_revisions(tmp_path: Path) -> None:
    transport = _Transport()
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)
    source = CninfoOfficialRecordSource(
        artifact_root=tmp_path / "official-record-artifacts",
        http_post=transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )
    request = OfficialRecordEvidenceRequest(
        instruments=("600519.SH",),
        as_of=datetime(2026, 8, 9, 9, 30, tzinfo=UTC),
    )

    first = source.read(request)
    transport.title = "2025年年度报告（更正后）"
    second = source.read(request)

    [first_document] = first.documents["600519.SH"]
    [second_document] = second.documents["600519.SH"]
    assert first_document.document_kind == "financial_report"
    assert first_document.content_hash != second_document.content_hash
    assert first.captures["600519.SH"].revision != second.captures["600519.SH"].revision
    assert len(list((tmp_path / "official-record-artifacts" / "artifacts").rglob("raw.bin"))) == 2
    assert transport.calls == [
        {
            "pageNum": "1",
            "pageSize": "30",
            "column": "sse",
            "tabName": "fulltext",
            "plate": "",
            "stock": "600519,gssh0600519",
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": "2025-08-09~2026-08-09",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        },
        {
            "pageNum": "1",
            "pageSize": "30",
            "column": "sse",
            "tabName": "fulltext",
            "plate": "",
            "stock": "600519,gssh0600519",
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": "2025-08-09~2026-08-09",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        },
    ]


def test_cninfo_source_keeps_every_artifact_directory_owner_only(tmp_path: Path) -> None:
    root = tmp_path / "official-record-artifacts"
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)
    source = CninfoOfficialRecordSource(
        artifact_root=root,
        http_post=_Transport(),
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )

    source.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH",),
            as_of=captured_at,
        )
    )

    directories = tuple(path for path in root.rglob("*") if path.is_dir())
    assert directories
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories)


def test_cninfo_source_rejects_stale_replay_through_an_insecure_scope_directory(
    tmp_path: Path,
) -> None:
    transport = _Transport()
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)
    root = tmp_path / "official-record-artifacts"
    source = CninfoOfficialRecordSource(
        artifact_root=root,
        http_post=transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )
    request = OfficialRecordEvidenceRequest(
        instruments=("600519.SH",),
        as_of=captured_at,
    )

    source.read(request)
    scope_root = next((root / "artifacts").iterdir())
    scope_root.chmod(0o755)
    transport.fail = True

    result = source.read(request)

    assert result.documents == {}
    assert result.captures == {}
    assert result.data_gaps == ("OFFICIAL_RECORD_EVIDENCE_ARTIFACT_ROOT_INVALID:600519.SH",)


def test_cninfo_source_rejects_non_finite_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        CninfoOfficialRecordSource(
            artifact_root=tmp_path / "official-record-artifacts",
            timeout_seconds=float("nan"),
        )


def test_cninfo_source_replays_only_the_latest_verified_capture_when_transport_fails(
    tmp_path: Path,
) -> None:
    transport = _Transport()
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)
    source = CninfoOfficialRecordSource(
        artifact_root=tmp_path / "official-record-artifacts",
        http_post=transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )
    request = OfficialRecordEvidenceRequest(
        instruments=("600519.SH",),
        as_of=captured_at,
    )

    first = source.read(request)
    transport.fail = True
    replay = source.read(request)

    assert replay.captures["600519.SH"].revision == first.captures["600519.SH"].revision
    assert replay.captures["600519.SH"].retrieved_at == captured_at
    assert replay.captures["600519.SH"].stale is True
    assert replay.documents == first.documents
    assert replay.data_gaps == ("OFFICIAL_RECORD_EVIDENCE_STALE_CACHE:600519.SH",)
    assert len(list((tmp_path / "official-record-artifacts" / "artifacts").rglob("raw.bin"))) == 1


def test_cninfo_source_keeps_a_valid_empty_response_distinct_from_unavailable(
    tmp_path: Path,
) -> None:
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)

    def empty_transport(url, *, data, headers, timeout, allow_redirects):
        del url, data, headers, timeout, allow_redirects
        return _Response(
            json.dumps({"announcements": None, "classifiedAnnouncements": None}).encode()
        )

    source = CninfoOfficialRecordSource(
        artifact_root=tmp_path / "official-record-artifacts",
        http_post=empty_transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )

    result = source.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH",),
            as_of=captured_at,
        )
    )

    assert result.documents == {"600519.SH": ()}
    assert set(result.captures) == {"600519.SH"}
    assert result.data_gaps == ()


def test_cninfo_source_never_replays_a_capture_into_a_different_query_window(
    tmp_path: Path,
) -> None:
    transport = _Transport()
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)
    source = CninfoOfficialRecordSource(
        artifact_root=tmp_path / "official-record-artifacts",
        http_post=transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )
    source.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH",),
            as_of=captured_at,
        )
    )
    transport.fail = True

    result = source.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH",),
            as_of=captured_at + timedelta(days=1),
        )
    )

    assert result.documents == {}
    assert result.captures == {}
    assert result.data_gaps == ("OFFICIAL_RECORD_EVIDENCE_TRANSPORT_UNAVAILABLE:600519.SH",)


def test_cninfo_source_uses_the_a_share_calendar_date_for_the_query_window(
    tmp_path: Path,
) -> None:
    transport = _Transport()
    captured_at = datetime(2026, 8, 8, 18, 30, tzinfo=UTC)
    source = CninfoOfficialRecordSource(
        artifact_root=tmp_path / "official-record-artifacts",
        http_post=transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )

    source.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH",),
            as_of=captured_at,
        )
    )

    assert transport.calls[0]["seDate"] == "2025-08-09~2026-08-09"


def test_cninfo_source_marks_a_bounded_document_page_as_partial_when_more_records_exist(
    tmp_path: Path,
) -> None:
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)

    def truncated_transport(url, *, data, headers, timeout, allow_redirects):
        del url, data, headers, timeout, allow_redirects
        return _Response(
            json.dumps(
                {
                    "totalRecordNum": 31,
                    "announcements": [
                        {
                            "announcementId": "1200000001",
                            "announcementTitle": "2025年年度报告",
                            "announcementTime": 1786147200000,
                            "adjunctUrl": "finalpage/2026-08-08/1200000001.PDF",
                            "secCode": "600519",
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode()
        )

    source = CninfoOfficialRecordSource(
        artifact_root=tmp_path / "official-record-artifacts",
        http_post=truncated_transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )

    result = source.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH",),
            as_of=captured_at,
        )
    )

    assert len(result.documents["600519.SH"]) == 1
    assert result.data_gaps == ("OFFICIAL_RECORD_EVIDENCE_DOCUMENTS_TRUNCATED:600519.SH",)


def test_cninfo_source_rejects_an_oversized_document_title(tmp_path: Path) -> None:
    transport = _Transport(title="x" * 513)
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)
    source = CninfoOfficialRecordSource(
        artifact_root=tmp_path / "official-record-artifacts",
        http_post=transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )

    result = source.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH",),
            as_of=captured_at,
        )
    )

    assert result.documents == {"600519.SH": ()}
    assert result.data_gaps == ("OFFICIAL_RECORD_EVIDENCE_DOCUMENT_INVALID:600519.SH",)


def test_cninfo_source_uses_classified_rows_when_the_primary_list_is_empty(tmp_path: Path) -> None:
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)

    def classified_transport(url, *, data, headers, timeout, allow_redirects):
        del url, data, headers, timeout, allow_redirects
        return _Response(
            json.dumps(
                {
                    "announcements": [],
                    "classifiedAnnouncements": [
                        [
                            {
                                "announcementId": "1200000001",
                                "announcementTitle": "2025年年度报告",
                                "announcementTime": 1786147200000,
                                "adjunctUrl": "finalpage/2026-08-08/1200000001.PDF",
                                "secCode": "600519",
                            }
                        ]
                    ],
                },
                ensure_ascii=False,
            ).encode()
        )

    source = CninfoOfficialRecordSource(
        artifact_root=tmp_path / "official-record-artifacts",
        http_post=classified_transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )

    result = source.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH",),
            as_of=captured_at,
        )
    )

    assert result.documents["600519.SH"][0].document_id == "1200000001"
    assert result.data_gaps == ()


def test_cninfo_source_never_treats_a_malformed_document_list_as_confirmed_empty(
    tmp_path: Path,
) -> None:
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)

    def malformed_transport(url, *, data, headers, timeout, allow_redirects):
        del url, data, headers, timeout, allow_redirects
        return _Response(json.dumps({"announcements": [None]}).encode())

    source = CninfoOfficialRecordSource(
        artifact_root=tmp_path / "official-record-artifacts",
        http_post=malformed_transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )

    result = source.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH",),
            as_of=captured_at,
        )
    )

    assert result.documents == {}
    assert result.captures == {}
    assert result.data_gaps == ("OFFICIAL_RECORD_EVIDENCE_PAYLOAD_INVALID:600519.SH",)


def test_cninfo_source_never_projects_a_document_url_outside_the_official_host(
    tmp_path: Path,
) -> None:
    captured_at = datetime(2026, 8, 8, 9, 30, tzinfo=UTC)

    def untrusted_url_transport(url, *, data, headers, timeout, allow_redirects):
        del url, data, headers, timeout, allow_redirects
        return _Response(
            json.dumps(
                {
                    "announcements": [
                        {
                            "announcementId": "1200000001",
                            "announcementTitle": "2025年年度报告",
                            "announcementTime": 1786147200000,
                            "adjunctUrl": "https://untrusted.invalid/report.pdf",
                            "secCode": "600519",
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode()
        )

    source = CninfoOfficialRecordSource(
        artifact_root=tmp_path / "official-record-artifacts",
        http_post=untrusted_url_transport,
        evidence_origin=ObservationEvidenceOrigin.TEST_ONLY,
        clock=lambda: captured_at,
    )

    result = source.read(
        OfficialRecordEvidenceRequest(
            instruments=("600519.SH",),
            as_of=captured_at,
        )
    )

    assert result.documents == {"600519.SH": ()}
    assert set(result.captures) == {"600519.SH"}
    assert result.data_gaps == ("OFFICIAL_RECORD_EVIDENCE_DOCUMENT_INVALID:600519.SH",)
