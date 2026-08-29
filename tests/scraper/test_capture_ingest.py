"""ZSXQ Windows-native capture artifact → WSL ingest 公共入口测试。

目标分支：scripts/import_zsxq_capture.py（fin_analyse.scraper.capture_ingest.main）
完成 artifact 校验 → 判重 → run_capture_ingest_once（module + ledger + KB + G）→
consumed/rejected 归档。任何失败路径不得刷新 G。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from fin_analyse.scraper.runtime_repository import ScraperRuntimeRepository
from tests.scraper.capture_fixtures import (
    build_artifact_payload,
    build_cursor_artifact_payload,
    build_image_artifact_payload,
    build_page_end_artifact_payload,
    content_hash,
    write_artifact,
)


def run_ingest(artifact_path: Path, tmp_path: Path, capsys=None) -> tuple[int, dict]:
    from fin_analyse.scraper.capture_ingest import main

    runtime_db = tmp_path / "runtime.sqlite3"
    kb_root = tmp_path / "knowledge-base"
    kb_root.mkdir(parents=True, exist_ok=True)
    index_path = kb_root / "index.json"
    if not index_path.exists():
        index_path.write_text(
            json.dumps({"articles": [], "total": 0, "updated": ""}), encoding="utf-8"
        )
    exit_code = main(
        [
            "--artifact",
            str(artifact_path),
            "--runtime-db",
            str(runtime_db),
            "--knowledge-base-root",
            str(kb_root),
            "--trigger",
            "manual",
        ],
        _canonical_runtime_db=runtime_db,
    )
    out = capsys.readouterr().out if capsys is not None else ""
    return exit_code, json.loads(out) if out else {}


def _manifest_path(kb_root: Path) -> Path:
    return kb_root / "runtime" / "operations" / "g_working_set" / "manifest.v1.json"


def _recovery_root(tmp_path: Path) -> Path:
    return tmp_path / "capture-recovery-v1"


def _prepare_recovery_root(tmp_path: Path) -> Path:
    from fin_analyse.scraper.capture_ingest import capture_runtime_owner_id

    root = _recovery_root(tmp_path)
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    owner = root / "runtime-owner.json"
    owner.write_text(
        json.dumps(
            {
                "schema_version": "fin.zsxq-capture-recovery-owner/v1",
                "runtime_owner_id": capture_runtime_owner_id(
                    tmp_path / "runtime.sqlite3"
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    owner.chmod(0o600)
    return root


def test_canonical_runtime_owner_does_not_follow_ambient_home(tmp_path, monkeypatch):
    from fin_analyse.scraper.scheduled_run import canonical_runtime_db_path

    expected = canonical_runtime_db_path()
    monkeypatch.setenv("HOME", str(tmp_path / "alternate-home"))

    assert canonical_runtime_db_path() == expected


def test_ingest_parser_accepts_scheduled_windows_capture(tmp_path):
    from fin_analyse.scraper.capture_ingest import _parse_args

    runtime_db = (tmp_path / "runtime.sqlite3").resolve()
    parsed, invalid_exit = _parse_args(
        [
            "--artifact",
            str((tmp_path / "capture.latest.json").resolve()),
            "--trigger",
            "schedule",
        ],
        canonical_runtime_db=runtime_db,
    )

    assert invalid_exit is None
    assert parsed is not None
    assert parsed.trigger == "schedule"



def test_ownerless_nonempty_recovery_root_is_never_adopted(tmp_path, capsys):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    source_before = artifact_path.read_bytes()
    recovery_root = _recovery_root(tmp_path)
    recovery_root.mkdir(mode=0o700)
    staged = recovery_root / "staged"
    staged.mkdir(mode=0o700)
    staged_path = staged / f"{payload['run_id']}.{payload['content_sha256']}.artifact.json"
    staged_path.write_bytes(source_before)
    staged_path.chmod(0o600)

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 64
    assert result == {
        "schema_version": "fin.zsxq-capture-ingest/v1",
        "status": "conflict",
        "error_code": "capture_recovery_owner_conflict",
    }
    assert not (recovery_root / "runtime-owner.json").exists()
    assert staged_path.read_bytes() == source_before
    assert artifact_path.read_bytes() == source_before
    assert not (tmp_path / "runtime.sqlite3").exists()
    assert not _manifest_path(tmp_path / "knowledge-base").exists()


def test_partial_owner_publication_temp_is_rebuilt_before_business(tmp_path, capsys):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    recovery_root = _recovery_root(tmp_path)
    recovery_root.mkdir(mode=0o700)
    owner_temp = recovery_root / ".runtime-owner.json.tmp"
    owner_temp.write_bytes(b'{"schema_version":')
    owner_temp.chmod(0o600)

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, result
    assert result["status"] == "succeeded"
    assert not owner_temp.exists()
    assert (recovery_root / "runtime-owner.json").is_file()
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)


def test_owner_publication_storage_failure_is_not_reported_as_conflict(
    tmp_path,
    capsys,
    monkeypatch,
):
    import errno
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))

    def storage_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "storage full")

    monkeypatch.setattr(capture_ingest, "_publish_create_only_at", storage_full)

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert result["status"] == "internal_error"
    assert result["error_code"] == "capture_recovery_storage_failed"
    assert not (tmp_path / "runtime.sqlite3").exists()


def test_ingest_success_imports_articles_and_publishes_g(tmp_path, capsys):
    """O2/O3 成功链：文章进 KB、ledger 落账、G receipt 发布、consumed 归档。"""
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    artifact_path = write_artifact(tmp_path, build_artifact_payload(now))
    source_before = artifact_path.read_bytes()
    source_inode_before = artifact_path.stat().st_ino

    exit_code, payload = run_ingest(artifact_path, tmp_path, capsys)

    # fixture 无星大派文章（普通栏 owner 撤项后回到非 G）→ G 如实 PARTIAL
    # （active_g_empty + priority_events_missing），文章已进 KB、ledger 已落账
    # ——真实采集的如实终态，不是伪造 fresh。
    assert exit_code == 4, payload
    assert payload["status"] == "succeeded"
    assert payload["completion_status"] == "partial"
    assert payload["artifact"]["run_id"] == "123e4567-e89b-12d3-a456-426614174000"
    g_ws = payload["g_working_set"]
    assert g_ws["published"] is True
    assert g_ws["status"] == "PARTIAL"
    assert set(g_ws["data_gaps"]) >= {"g_working_set_active_g_empty"}
    assert g_ws["evaluated_at"]

    # KB 落盘：2 篇窗口内文章（sync 路径无 topic_id → legacy id），窗口外不落盘
    kb_root = tmp_path / "knowledge-base"
    articles_dir = kb_root / "articles"
    md_files = sorted(p.name for p in articles_dir.glob("*.md"))
    assert len(md_files) == 2
    index = json.loads((kb_root / "index.json").read_text(encoding="utf-8"))
    index_ids = [a["id"] for a in index["articles"]]
    assert len(index_ids) == 2
    md1 = (articles_dir / md_files[0]).read_text(encoding="utf-8")
    md2 = (articles_dir / md_files[1]).read_text(encoding="utf-8")
    assert (
        "提问：新能源车渗透率还能提升多少？" in md1 or "提问：新能源车渗透率还能提升多少？" in md2
    )
    assert "半导体设备国产替代的节奏观察" in md1 or "半导体设备国产替代的节奏观察" in md2
    assert "旧闻测试标题" not in md1 + md2

    # ledger 落账
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        latest = repo.latest_terminal_run()
        assert latest["status"] == "succeeded"
        assert latest["changed_count"] == 2
    finally:
        repo.close()

    # consumed 归档 + receipt
    consumed = _recovery_root(tmp_path) / "consumed"
    receipts = list(consumed.glob("*.json"))
    assert any(r.name == "123e4567-e89b-12d3-a456-426614174000.json" for r in receipts)
    assert artifact_path.read_bytes() == source_before
    assert artifact_path.stat().st_ino == source_inode_before
    assert not any(
        (artifact_path.parent / name).exists()
        for name in ("staged", "consumed", "rejected")
    )
    # G manifest 已生成
    assert _manifest_path(kb_root).exists()


def test_archived_receipt_is_accepted_only_when_bound_to_complete_ledger(tmp_path, capsys):
    from datetime import datetime

    from fin_analyse.scraper.capture_ingest import read_archived_capture_receipt_pairs
    from fin_analyse.scraper.cdp_scraper import TZ
    from fin_analyse.scraper.runtime_repository import validate_archived_capture_receipt

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)
    assert exit_code == 4, result
    receipt_path = (
        _recovery_root(tmp_path) / "consumed" / f"{payload['run_id']}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    runtime_db = tmp_path / "runtime.sqlite3"
    sidecars = [
        runtime_db.with_name(f"{runtime_db.name}{suffix}")
        for suffix in ("-wal", "-shm", "-journal")
    ]
    assert not any(path.exists() for path in sidecars)

    pair = read_archived_capture_receipt_pairs(runtime_db, (receipt_path,))
    audit = validate_archived_capture_receipt(runtime_db, receipt)

    assert pair == [receipt]
    assert audit == receipt["audit"]
    assert not any(path.exists() for path in sidecars)
    forged = dict(receipt)
    forged["content_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="owner binding"):
        validate_archived_capture_receipt(runtime_db, forged)

    outside = tmp_path / receipt_path.name
    outside.write_bytes(receipt_path.read_bytes())
    outside.chmod(0o600)
    with pytest.raises(ValueError, match="archive pair is invalid"):
        read_archived_capture_receipt_pairs(runtime_db, (outside,))


@pytest.mark.parametrize("rewritten_name", ["runtime-owner.json", "receipt"])
def test_archived_pair_reader_requires_canonical_marker_bytes(
    tmp_path,
    capsys,
    rewritten_name,
):
    from datetime import datetime

    from fin_analyse.scraper.capture_ingest import read_archived_capture_receipt_pairs
    from fin_analyse.scraper.cdp_scraper import TZ

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)
    assert exit_code == 4, result
    runtime_db = tmp_path / "runtime.sqlite3"
    recovery_root = _recovery_root(tmp_path)
    receipt_path = recovery_root / "consumed" / f"{payload['run_id']}.json"
    rewritten = (
        recovery_root / "runtime-owner.json"
        if rewritten_name == "runtime-owner.json"
        else receipt_path
    )
    decoded = json.loads(rewritten.read_bytes())
    rewritten.write_text(
        json.dumps(decoded, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    rewritten.chmod(0o600)

    with pytest.raises(ValueError, match="archive pair is invalid"):
        read_archived_capture_receipt_pairs(runtime_db, (receipt_path,))


def test_archived_receipt_rejects_noncanonical_owner_schema(tmp_path, capsys):
    import sqlite3
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ
    from fin_analyse.scraper.runtime_repository import validate_archived_capture_receipt

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)
    assert exit_code == 4, result
    receipt_path = (
        _recovery_root(tmp_path) / "consumed" / f"{payload['run_id']}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    runtime_db = tmp_path / "runtime.sqlite3"
    conn = sqlite3.connect(runtime_db)
    try:
        conn.execute("CREATE TABLE unexpected_owner_state(value TEXT)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="owner binding"):
        validate_archived_capture_receipt(runtime_db, receipt)


def test_archived_receipt_rejects_active_owner_sidecars(tmp_path, capsys):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ
    from fin_analyse.scraper.runtime_repository import validate_archived_capture_receipt

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)
    assert exit_code == 4, result
    receipt_path = (
        _recovery_root(tmp_path) / "consumed" / f"{payload['run_id']}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    runtime_db = tmp_path / "runtime.sqlite3"
    active_owner = ScraperRuntimeRepository(runtime_db)
    try:
        sidecars = [
            runtime_db.with_name(f"{runtime_db.name}{suffix}")
            for suffix in ("-wal", "-shm", "-journal")
        ]
        assert any(path.exists() for path in sidecars)
        with pytest.raises(ValueError, match="owner binding"):
            validate_archived_capture_receipt(runtime_db, receipt)
    finally:
        active_owner.close()


def test_archived_receipt_batch_uses_one_frozen_owner_snapshot(
    tmp_path, capsys, monkeypatch
):
    from datetime import datetime

    from fin_analyse.scraper import runtime_repository
    from fin_analyse.scraper.cdp_scraper import TZ

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)
    assert exit_code == 4, result
    receipt_path = (
        _recovery_root(tmp_path) / "consumed" / f"{payload['run_id']}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    runtime_db = tmp_path / "runtime.sqlite3"
    connect = runtime_repository.sqlite3.connect
    owner_opens = []

    def track_connect(*args, **kwargs):
        if args and "immutable=1" in str(args[0]):
            owner_opens.append(args[0])
        return connect(*args, **kwargs)

    monkeypatch.setattr(runtime_repository.sqlite3, "connect", track_connect)

    audits = runtime_repository.validate_archived_capture_receipts(
        runtime_db,
        (receipt, receipt, receipt),
    )

    assert audits == [receipt["audit"]] * 3
    assert len(owner_opens) == 1


def test_invalid_completion_receipt_cannot_poison_complete(
    tmp_path, capsys, monkeypatch
):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    original = ScraperRuntimeRepository.complete_capture_ingest

    def inject_invalid_receipt(self, **kwargs):
        projection = json.loads(kwargs["completion_json"])
        projection["receipt"]["ingested_at"] = "not-a-time"
        projection["receipt"]["audit"] = {}
        kwargs["completion_json"] = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return original(self, **kwargs)

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "complete_capture_ingest",
        inject_invalid_receipt,
    )

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert result["error_code"] == "capture_recovery_completion_failed"
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id=payload["run_id"],
            content_sha256=payload["content_sha256"],
        )
        assert record is not None
        assert record.phase == "PUBLICATION_PREPARED"
    finally:
        repo.close()


def test_forged_ready_audit_cannot_poison_complete(tmp_path, capsys, monkeypatch):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    original = ScraperRuntimeRepository.complete_capture_ingest

    def inject_forged_ready(self, **kwargs):
        projection = json.loads(kwargs["completion_json"])
        completion_payload = projection["payload"]
        receipt = projection["receipt"]
        audit = receipt["audit"]
        completion_payload["completion_status"] = "ready"
        completion_payload["completion_data_gaps"] = []
        completion_payload["g_working_set"] = {
            "schema_version": "fin.zsxq-g-working-set-publication/v1",
            "published": True,
            "status": "READY",
            "generation": "a" * 64,
            "evaluated_at": receipt["ingested_at"],
            "source_refs": ["zsxq-topic:ready-proof"],
            "data_gaps": [],
            "freshness": "FRESH",
            "source_coverage_sha256": "b" * 64,
            "producer_id": "fin.zsxq-production-cdp/v1",
            "producer_run_id": completion_payload["run_id"],
            "producer_run_status": completion_payload["status"],
            "publication_mode": "CURRENT_RUN",
            "prior_generation": None,
            "prior_source_refs": None,
            "prior_source_coverage_sha256": None,
            "prior_evaluated_at": None,
            "prior_freshness": None,
        }
        projection["exit_code"] = 0
        projection["archive_disposition"] = "consumed"
        receipt["completion_status"] = "ready"
        audit["coverage"].update(
            {"proven": True, "boundary": "page_end", "cursor_page_count": 0}
        )
        audit["denominator"].update(
            {
                "missing_expected_topic_uids": [],
                "duplicate_canonical_identities": [],
                "status": "PROVEN",
            }
        )
        audit["chain"].update(
            {
                "ready": True,
                "completion_status": "ready",
                "g_working_set_status": "READY",
                "g_generation": "a" * 64,
                "g_source_coverage_sha256": "b" * 64,
            }
        )
        audit["integrity_status"] = "PROVEN"
        audit["data_gaps"] = []
        completion_payload["capture_audit"] = {
            "integrity_status": "PROVEN",
            "chain_ready": True,
            "denominator_status": "PROVEN",
            "data_gaps": [],
        }
        kwargs["completion_json"] = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return original(self, **kwargs)

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "complete_capture_ingest",
        inject_forged_ready,
    )

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert result["error_code"] == "capture_recovery_completion_failed"
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id=payload["run_id"],
            content_sha256=payload["content_sha256"],
        )
        assert record is not None
        assert record.phase == "PUBLICATION_PREPARED"
    finally:
        repo.close()


def test_malformed_g_receipt_cannot_poison_complete(tmp_path, capsys, monkeypatch):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    original = ScraperRuntimeRepository.complete_capture_ingest

    def inject_malformed_g(self, **kwargs):
        projection = json.loads(kwargs["completion_json"])
        projection["payload"]["g_working_set"] = {}
        kwargs["completion_json"] = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return original(self, **kwargs)

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "complete_capture_ingest",
        inject_malformed_g,
    )

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert result["error_code"] == "capture_recovery_completion_failed"
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id=payload["run_id"],
            content_sha256=payload["content_sha256"],
        )
        assert record is not None
        assert record.phase == "PUBLICATION_PREPARED"
    finally:
        repo.close()


def test_completion_g_receipt_must_match_frozen_publication_plan(
    tmp_path, capsys, monkeypatch
):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    original = ScraperRuntimeRepository.complete_capture_ingest

    def inject_foreign_g_evidence(self, **kwargs):
        projection = json.loads(kwargs["completion_json"])
        g_receipt = projection["payload"]["g_working_set"]
        g_receipt["generation"] = "a" * 64
        g_receipt["source_coverage_sha256"] = "b" * 64
        projection["receipt"]["audit"]["chain"]["g_generation"] = "a" * 64
        projection["receipt"]["audit"]["chain"]["g_source_coverage_sha256"] = "b" * 64
        kwargs["completion_json"] = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return original(self, **kwargs)

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "complete_capture_ingest",
        inject_foreign_g_evidence,
    )

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert result["error_code"] == "capture_recovery_completion_failed"
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id=payload["run_id"],
            content_sha256=payload["content_sha256"],
        )
        assert record is not None
        assert record.phase == "PUBLICATION_PREPARED"
    finally:
        repo.close()


def test_completion_g_source_refs_must_match_frozen_publication_plan(
    tmp_path, capsys, monkeypatch
):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    original = ScraperRuntimeRepository.complete_capture_ingest

    def inject_foreign_g_source_ref(self, **kwargs):
        projection = json.loads(kwargs["completion_json"])
        projection["payload"]["g_working_set"]["source_refs"].append("forged-owner-ref")
        kwargs["completion_json"] = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return original(self, **kwargs)

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "complete_capture_ingest",
        inject_foreign_g_source_ref,
    )

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert result["error_code"] == "capture_recovery_completion_failed"




def test_complete_cas_rejects_published_receipt_without_owner_manifest(
    tmp_path, capsys, monkeypatch
):
    from datetime import datetime

    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.cdp_scraper import TZ

    payload = build_cursor_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    publish = cdp_runtime._publish_g_working_set_after_terminal_run

    def remove_owner_manifest_after_publish(**kwargs):
        receipt = publish(**kwargs)
        _manifest_path(tmp_path / "knowledge-base").unlink()
        return receipt

    monkeypatch.setattr(
        cdp_runtime,
        "_publish_g_working_set_after_terminal_run",
        remove_owner_manifest_after_publish,
    )

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert result["error_code"] == "capture_recovery_completion_failed"
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id=payload["run_id"],
            content_sha256=payload["content_sha256"],
        )
        assert record is not None
        assert record.phase == "PUBLICATION_PREPARED"
    finally:
        repo.close()


def test_no_change_completion_must_match_claim_time_prior_g(tmp_path, capsys, monkeypatch):
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    first_path = write_artifact(tmp_path, build_artifact_payload(now))
    first_exit, first_result = run_ingest(first_path, tmp_path, capsys)
    assert first_exit == 4, first_result

    payload = build_artifact_payload(now)
    payload["run_id"] = "223e4567-e89b-42d3-a456-426614174000"
    payload["content_sha256"] = content_hash(payload)
    artifact_path = write_artifact(tmp_path, payload)
    original = ScraperRuntimeRepository.complete_capture_ingest
    monkeypatch.setattr(capture_ingest, "_capture_prior_g_json", lambda _root: "{}")

    def inject_invented_prior(self, **kwargs):
        projection = json.loads(kwargs["completion_json"])
        completion_payload = projection["payload"]
        assert completion_payload["status"] == "no_change"
        g_receipt = completion_payload["g_working_set"]
        g_receipt.update(
            {
                "prior_generation": "c" * 64,
                "prior_source_refs": [],
                "prior_source_coverage_sha256": "d" * 64,
                "prior_evaluated_at": completion_payload["started_at"],
                "prior_freshness": "UNKNOWN",
            }
        )
        kwargs["completion_json"] = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return original(self, **kwargs)

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "complete_capture_ingest",
        inject_invented_prior,
    )

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert result["error_code"] == "capture_recovery_completion_failed"
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id=payload["run_id"],
            content_sha256=payload["content_sha256"],
        )
        assert record is not None
        assert record.phase == "PUBLICATION_PREPARED"
    finally:
        repo.close()


def test_capture_claim_persists_only_bounded_g_publication_evidence(tmp_path, monkeypatch):
    from fin_analyse.guo_teacher_research.g_working_set import (
        GWorkingSetPublicationEvidence,
        GWorkingSetService,
        GWorkingSetStatus,
    )
    from fin_analyse.scraper import capture_ingest

    evidence = GWorkingSetPublicationEvidence(
        status=GWorkingSetStatus.READY,
        generation="a" * 64,
        evaluated_at="2026-08-09T10:00:00+00:00",
        source_refs=("article-1",),
        source_coverage_sha256="b" * 64,
        data_gaps=(),
    )

    class OversizedAssessment:
        status = GWorkingSetStatus.READY
        data_gaps: tuple[str, ...] = ()
        canonical_sha256 = "a" * 64
        evaluated_at = "2026-08-09T10:00:00+00:00"
        manifest = {"irrelevant_owner_state": "x" * (2 * 1024 * 1024)}

        def to_publication_evidence(self):
            return evidence

    monkeypatch.setattr(
        GWorkingSetService,
        "evaluate",
        lambda _self: OversizedAssessment(),
    )

    frozen = json.loads(capture_ingest._capture_prior_g_json(tmp_path))

    assert frozen == {
        "status": "READY",
        "generation": "a" * 64,
        "evaluated_at": "2026-08-09T10:00:00+00:00",
        "source_refs": ["article-1"],
        "source_coverage_sha256": "b" * 64,
        "data_gaps": [],
    }


def test_completion_audit_must_match_artifact_derived_audit(tmp_path, capsys, monkeypatch):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    original = ScraperRuntimeRepository.complete_capture_ingest

    def inject_cursor_proof(self, **kwargs):
        projection = json.loads(kwargs["completion_json"])
        audit = projection["receipt"]["audit"]
        audit["coverage"]["proven"] = True
        audit["coverage"]["cursor_page_count"] = 1
        audit["denominator"]["status"] = "PROVEN"
        audit["integrity_status"] = "PROVEN"
        audit["data_gaps"] = [
            gap
            for gap in audit["data_gaps"]
            if gap
            not in {
                "zsxq_audit_cursor_coverage_unproven",
                "zsxq_audit_expected_teacher_denominator_unavailable",
            }
        ]
        projection["payload"]["capture_audit"].update(
            {
                "integrity_status": "PROVEN",
                "denominator_status": "PROVEN",
                "data_gaps": audit["data_gaps"],
            }
        )
        kwargs["completion_json"] = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return original(self, **kwargs)

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "complete_capture_ingest",
        inject_cursor_proof,
    )

    exit_code, result = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert result["error_code"] == "capture_recovery_completion_failed"
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id=payload["run_id"],
            content_sha256=payload["content_sha256"],
        )
        assert record is not None
        assert record.phase == "PUBLICATION_PREPARED"
    finally:
        repo.close()


def test_ingest_cursor_path_imports_articles(tmp_path, capsys):
    """真实页面形态：DOM 证据为空（data-topic-id=0）→ cursor 记录驱动采集成功。"""
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    payload = build_cursor_artifact_payload(now)
    artifact_path = write_artifact(tmp_path, payload)

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out  # 如实 PARTIAL（无星大派内容）
    assert out["status"] == "succeeded"
    kb_root = tmp_path / "knowledge-base"
    md_files = sorted(p.name for p in (kb_root / "articles").glob("*.md"))
    assert len(md_files) == 2
    index = json.loads((kb_root / "index.json").read_text(encoding="utf-8"))
    index_ids = [a["id"] for a in index["articles"]]
    assert "zsxq-700000000000001" in index_ids
    assert "zsxq-700000000000002" in index_ids
    # G 发布（如实 PARTIAL）
    assert out["g_working_set"]["published"] is True
    assert out["g_working_set"]["status"] == "PARTIAL"

    receipt = json.loads(
        (_recovery_root(tmp_path) / "consumed" / "123e4567-e89b-12d3-a456-426614174000.json").read_text(
            encoding="utf-8"
        )
    )
    audit = receipt["audit"]
    assert audit["schema_version"] == "fin.zsxq-capture-ingest-audit/v1"
    assert audit["denominator"]["status"] == "PROVEN"
    assert audit["expected_teacher_item_ids"] == [
        "700000000000001",
        "700000000000002",
    ]
    assert audit["items"][0]["canonical_duplicate_identity"] == "zsxq-topic:700000000000001"
    assert audit["items"][0]["source"] == "teacher_original"
    assert audit["items"][0]["column"] == "普通"
    assert audit["chain"]["ready"] is False  # fixture G is honestly PARTIAL


def test_ingest_cursor_path_keeps_authenticated_teacher_topic_without_generic_filters(
    tmp_path, capsys
):
    """Cursor 已认证的教师原帖不受普通 DOM 内容筛选误删。"""
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    payload = build_cursor_artifact_payload(now)
    cursor = json.loads(payload["topic_cursor"][0]["output"])
    cursor["topics"][0].update(
        {
            "title": "一则日常记录",
            "content_text": "短老师原帖。",
        }
    )
    payload["topic_cursor"][0]["output"] = json.dumps(cursor, ensure_ascii=False)
    payload["content_sha256"] = content_hash(payload)
    artifact_path = write_artifact(tmp_path, payload)

    _exit_code, _out = run_ingest(artifact_path, tmp_path, capsys)

    kb_root = tmp_path / "knowledge-base"
    index = json.loads((kb_root / "index.json").read_text(encoding="utf-8"))
    assert "zsxq-700000000000001" in [article["id"] for article in index["articles"]]

    receipt = json.loads(
        (_recovery_root(tmp_path) / "consumed" / "123e4567-e89b-12d3-a456-426614174000.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["audit"]["denominator"]["status"] == "PROVEN"


def test_ingest_cursor_path_keeps_artifact_window_boundary_after_handoff_delay(tmp_path, capsys):
    """导入使用 artifact 冻结的窗口，而不是延迟后的实时 cutoff。"""
    from datetime import datetime, timedelta

    from fin_analyse.scraper.cdp_scraper import TZ

    captured_at = datetime.now(TZ) - timedelta(minutes=30)
    payload = build_cursor_artifact_payload(captured_at)
    cursor = json.loads(payload["topic_cursor"][0]["output"])
    boundary_time = captured_at - timedelta(days=3) + timedelta(minutes=10)
    cursor["topics"].insert(
        2,
        {
            "topic_id": "700000000000004",
            "legacy_topic_id": "4",
            "create_time": boundary_time.strftime("%Y-%m-%dT%H:%M:%S.000+0800"),
            "title": "半导体观察补充",
            "topic_type": "talk",
            "content_text": "能量评分 9.0 分\n半导体设备验证节奏仍需持续跟踪。" * 6,
            "source_class": "teacher",
            "answer_state": "not_applicable",
        },
    )
    payload["topic_cursor"][0]["output"] = json.dumps(cursor, ensure_ascii=False)
    payload["content_sha256"] = content_hash(payload)
    artifact_path = write_artifact(tmp_path, payload)

    _exit_code, _out = run_ingest(artifact_path, tmp_path, capsys)

    kb_root = tmp_path / "knowledge-base"
    index = json.loads((kb_root / "index.json").read_text(encoding="utf-8"))
    assert "zsxq-700000000000004" in [article["id"] for article in index["articles"]]

    receipt = json.loads(
        (_recovery_root(tmp_path) / "consumed" / "123e4567-e89b-12d3-a456-426614174000.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["audit"]["denominator"]["status"] == "PROVEN"


def test_ingest_page_end_coverage_imports_articles(tmp_path, capsys):
    """F-02：page_end 覆盖（短页全部内容在窗口内，oldest ≥ cutoff）→ 采集成功。"""
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    artifact_path = write_artifact(tmp_path, build_page_end_artifact_payload(now))

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out
    assert out["status"] == "succeeded"
    kb_root = tmp_path / "knowledge-base"
    index = json.loads((kb_root / "index.json").read_text(encoding="utf-8"))
    assert len([a for a in index["articles"] if a["id"].startswith("zsxq-70000000000000")]) == 2
    assert out["g_working_set"]["status"] == "PARTIAL"


def test_ingest_with_images_flows_to_article(tmp_path, capsys, monkeypatch):
    """O2：图片列表经 replay 流入 _process_images，文章 frontmatter 含图片路径。"""
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ, CdpBridgeScraper

    now = datetime.now(TZ)
    artifact_path = write_artifact(tmp_path, build_image_artifact_payload(now))

    calls: list[tuple[list, str]] = []
    original = CdpBridgeScraper._process_images

    def _record_process_images(self, matched_images, post_id):
        calls.append((matched_images, post_id))
        return [
            {
                "filename": "000.jpg",
                "path": f"images/{post_id}/000.jpg",
                "ocr_text": "",
                "llm_desc": "",
                "vision_provider": "test",
                "vision_model": "test",
                "fallback_chain": [],
                "error": None,
            }
        ]

    monkeypatch.setattr(CdpBridgeScraper, "_process_images", _record_process_images)
    try:
        exit_code, out = run_ingest(artifact_path, tmp_path, capsys)
    finally:
        monkeypatch.setattr(CdpBridgeScraper, "_process_images", original)

    assert exit_code == 4, out
    assert out["status"] == "succeeded"
    # _process_images 被调用且收到来自 artifact 的图片（date 匹配 post1）
    assert calls, "images 未流入 _process_images"
    matched, post_id = calls[0]
    assert len(matched) == 2
    assert all(img["src"].startswith("https://images.zsxq.com/") for img in matched)
    # 文章 frontmatter 含图片路径
    kb_root = tmp_path / "knowledge-base"
    md_files = sorted(p.name for p in (kb_root / "articles").glob("*.md"))
    joined = "".join((kb_root / "articles" / name).read_text(encoding="utf-8") for name in md_files)
    assert f"images/{post_id}/000.jpg" in joined


def test_ingest_archive_failure_exits_70_with_warning(tmp_path, capsys, monkeypatch):
    """F-04：归档失败 → archive_warning + exit 70（不静默成功）。"""
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    artifact_path = write_artifact(tmp_path, build_artifact_payload(now))
    monkeypatch.setattr(
        capture_ingest,
        "_publish_completion_archive",
        lambda *a, **k: "consumed_archive_failed:test",
    )

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert out["archive_warning"] == "consumed_archive_failed:test"


def test_ingest_failed_artifact_does_not_touch_g(tmp_path, capsys):
    """O3：capture 侧失败（登录失效）→ FAILED，不建 run 行、不生成/刷新 G。"""
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    payload = build_artifact_payload(now)
    payload["final_status"] = "failed"
    payload["failure"] = {"reason": "login_required", "detail": "登录/扫码表面"}
    payload["content_sha256"] = content_hash(payload)
    artifact_path = write_artifact(tmp_path, payload)

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 1
    assert out["status"] == "failed"
    assert out["failure_reason"] == "login_required"
    assert "g_working_set" not in out
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        assert repo.latest_terminal_run() is None
    finally:
        repo.close()
    assert not _manifest_path(tmp_path / "knowledge-base").exists()
    # 归档到 rejected/
    assert artifact_path.exists()
    assert list((_recovery_root(tmp_path) / "rejected").glob("*.json"))


def test_ingest_corrupt_artifact_rejected(tmp_path, capsys):
    """O3：artifact 损坏（内容 hash 不匹配）→ invalid_request，G 不动。"""
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    payload = build_artifact_payload(now)
    payload["window"]["oldest_seen_date"] = "2099-01-01 00:00"
    # 不重算 content_sha256 → hash 不匹配 + 覆盖断言双重失败
    artifact_path = write_artifact(tmp_path, payload)

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 64
    assert out["status"] == "invalid_request"
    assert out["error_code"].startswith("capture_artifact_invalid")
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        assert repo.latest_terminal_run() is None
    finally:
        repo.close()
    assert not _manifest_path(tmp_path / "knowledge-base").exists()
    assert artifact_path.exists()
    assert list((_recovery_root(tmp_path) / "rejected").glob("*.json"))




def test_ingest_duplicate_artifact_skipped(tmp_path, capsys):
    """O3：重复 run_id → duplicate 终态，不重跑 module、不刷新 G。"""
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    artifact_path = write_artifact(tmp_path, build_artifact_payload(now))

    first_exit, _ = run_ingest(artifact_path, tmp_path, capsys)
    assert first_exit == 4  # 如实 PARTIAL（fixture 无星大派内容）

    manifest_before = _manifest_path(tmp_path / "knowledge-base").read_bytes()

    # 重新发布同一 artifact（模拟误重放）
    artifact_path.write_text(
        json.dumps(build_artifact_payload(now), ensure_ascii=False), encoding="utf-8"
    )
    second_exit, out = run_ingest(artifact_path, tmp_path, capsys)

    assert second_exit == 64
    assert out["status"] == "duplicate"
    assert out["original_exit_code"] == 4
    assert out["original_status"] == "succeeded"
    assert out["original_completion_status"] == "partial"
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        latest = repo.latest_terminal_run()
        assert latest["changed_count"] == 2  # 仍是首次的 run
    finally:
        repo.close()
    assert _manifest_path(tmp_path / "knowledge-base").read_bytes() == manifest_before


def test_ingest_rejects_a_torn_receipt_without_overwriting_it(tmp_path, capsys, monkeypatch):
    """Atomic marker publication cannot create a torn file; preserve one as conflict evidence."""
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    payload = build_artifact_payload(now)
    artifact_path = write_artifact(tmp_path, payload)
    first_exit, _ = run_ingest(artifact_path, tmp_path, capsys)
    assert first_exit == 4

    consumed = _recovery_root(tmp_path) / "consumed"
    receipt_path = consumed / "123e4567-e89b-12d3-a456-426614174000.json"
    torn_raw = b'{"schema_version":'
    receipt_path.write_bytes(torn_raw)
    receipt_path.chmod(0o600)
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def forbid_business(*_args, **_kwargs):
        raise AssertionError("torn marker handling must not re-enter business")

    monkeypatch.setattr(capture_ingest, "run_capture_ingest_once", forbid_business)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 64, out
    assert out["error_code"] == "capture_completion_archive_conflict"
    assert receipt_path.read_bytes() == torn_raw
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        assert repo.latest_terminal_run()["changed_count"] == 2
    finally:
        repo.close()


def test_ingest_rejects_a_forged_receipt_without_overwriting_it(tmp_path, capsys, monkeypatch):
    """Valid JSON that conflicts with COMPLETE is evidence, never duplicate truth."""
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    now = datetime.now(TZ)
    payload = build_artifact_payload(now)
    artifact_path = write_artifact(tmp_path, payload)
    first_exit, _ = run_ingest(artifact_path, tmp_path, capsys)
    assert first_exit == 4

    receipt_path = _recovery_root(tmp_path) / "consumed" / "123e4567-e89b-12d3-a456-426614174000.json"
    forged = json.loads(receipt_path.read_text(encoding="utf-8"))
    forged["content_sha256"] = "f" * 64
    forged_raw = (json.dumps(forged, ensure_ascii=False, sort_keys=True) + "\n").encode()
    receipt_path.write_bytes(forged_raw)
    receipt_path.chmod(0o600)
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def forbid_business(*_args, **_kwargs):
        raise AssertionError("receipt conflict must not re-enter business")

    monkeypatch.setattr(capture_ingest, "run_capture_ingest_once", forbid_business)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 64, out
    assert out["status"] == "conflict"
    assert out["error_code"] == "capture_completion_archive_conflict"
    assert receipt_path.read_bytes() == forged_raw
    assert artifact_path.exists()






















def test_consumed_directory_drift_preserves_replay_inputs(tmp_path, capsys, monkeypatch):
    """Archive publication cannot report success through a replaced directory name."""
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    publish_marker = capture_ingest._publish_receipt_marker
    moved = _recovery_root(tmp_path) / "consumed.moved"
    drifted = False

    def drift_after_marker(directory_fd, name, receipt):
        nonlocal drifted
        publish_marker(directory_fd, name, receipt)
        if not drifted and name.endswith(".json"):
            drifted = True
            canonical = _recovery_root(tmp_path) / "consumed"
            canonical.rename(moved)
            canonical.mkdir(mode=0o700)

    monkeypatch.setattr(capture_ingest, "_publish_receipt_marker", drift_after_marker)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert out["archive_warning"].startswith("consumed_archive_failed:")
    assert artifact_path.exists()
    assert list((_recovery_root(tmp_path) / "staged").glob("*.artifact.json"))
    assert list((_recovery_root(tmp_path) / "consumed").iterdir()) == []
    assert list(moved.glob("*.artifact.json"))
    assert list(moved.glob("*.json"))

    (_recovery_root(tmp_path) / "consumed").rmdir()
    moved.rename(_recovery_root(tmp_path) / "consumed")
    monkeypatch.setattr(capture_ingest, "_publish_receipt_marker", publish_marker)

    def forbid_business(*_args, **_kwargs):
        raise AssertionError("COMPLETE recovery must not re-enter business")

    monkeypatch.setattr(capture_ingest, "run_capture_ingest_once", forbid_business)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)
    assert exit_code == 64, out
    assert out["status"] == "duplicate"
    assert artifact_path.exists()
    assert not list((_recovery_root(tmp_path) / "staged").glob("*.artifact.json"))


def test_rejected_directory_drift_preserves_replay_inputs(tmp_path, capsys, monkeypatch):
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest, cdp_runtime
    from fin_analyse.scraper.cdp_scraper import TZ

    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    monkeypatch.setattr(
        cdp_runtime,
        "_publish_g_working_set_after_terminal_run",
        lambda **_kwargs: None,
    )
    publish_marker = capture_ingest._publish_receipt_marker
    moved = _recovery_root(tmp_path) / "rejected.moved"
    drifted = False

    def drift_after_marker(directory_fd, name, receipt):
        nonlocal drifted
        publish_marker(directory_fd, name, receipt)
        if not drifted and name.endswith(".json"):
            drifted = True
            canonical = _recovery_root(tmp_path) / "rejected"
            canonical.rename(moved)
            canonical.mkdir(mode=0o700)

    monkeypatch.setattr(capture_ingest, "_publish_receipt_marker", drift_after_marker)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert out["archive_warning"].startswith("rejected_archive_failed:")
    assert artifact_path.exists()
    assert list((_recovery_root(tmp_path) / "staged").glob("*.artifact.json"))
    assert list((_recovery_root(tmp_path) / "rejected").iterdir()) == []
    assert list(moved.glob("*.artifact.json"))
    assert list(moved.glob("*.json"))

    (_recovery_root(tmp_path) / "rejected").rmdir()
    moved.rename(_recovery_root(tmp_path) / "rejected")
    monkeypatch.setattr(capture_ingest, "_publish_receipt_marker", publish_marker)

    def forbid_business(*_args, **_kwargs):
        raise AssertionError("COMPLETE rejected recovery must not re-enter business")

    monkeypatch.setattr(capture_ingest, "run_capture_ingest_once", forbid_business)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)
    assert exit_code == 1, out
    assert out["status"] == "succeeded"
    assert artifact_path.exists()
    assert not list((_recovery_root(tmp_path) / "staged").glob("*.artifact.json"))


def test_ingest_recovers_after_immutable_staging_before_claim(tmp_path, capsys, monkeypatch):
    """A crash after staging leaves payload evidence but creates no phantom run."""
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, payload)
    stage = capture_ingest._stage_validated_artifact

    def crash_after_stage(*args, **kwargs):
        stage(*args, **kwargs)
        raise SimulatedProcessCrash

    monkeypatch.setattr(capture_ingest, "_stage_validated_artifact", crash_after_stage)
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    [staged] = list((_recovery_root(tmp_path) / "staged").glob("*.artifact.json"))
    assert staged.read_bytes() == artifact_path.read_bytes()
    assert not (tmp_path / "runtime.sqlite3").exists()
    artifact_path.unlink()

    monkeypatch.setattr(capture_ingest, "_stage_validated_artifact", stage)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        assert repo.latest_terminal_run()["changed_count"] == 2
    finally:
        repo.close()
    assert not staged.exists()


def test_staged_capture_conflicts_with_same_run_different_hash_source(
    tmp_path, capsys, monkeypatch
):
    """A replacement cannot reuse the staged run id with different content."""
    from datetime import datetime, timedelta

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    now = datetime.now(TZ)
    first = build_artifact_payload(now)
    artifact_path = write_artifact(tmp_path, first)
    stage = capture_ingest._stage_validated_artifact

    def crash_after_stage(*args, **kwargs):
        stage(*args, **kwargs)
        raise SimulatedProcessCrash

    monkeypatch.setattr(capture_ingest, "_stage_validated_artifact", crash_after_stage)
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    replacement = build_artifact_payload(now + timedelta(seconds=1))
    assert replacement["run_id"] == first["run_id"]
    assert replacement["content_sha256"] != first["content_sha256"]
    replacement_raw = json.dumps(replacement, ensure_ascii=False).encode()
    artifact_path.write_bytes(replacement_raw)
    business = capture_ingest.run_capture_ingest_once
    business_calls = 0

    def count_business(*args, **kwargs):
        nonlocal business_calls
        business_calls += 1
        return business(*args, **kwargs)

    monkeypatch.setattr(capture_ingest, "_stage_validated_artifact", stage)
    monkeypatch.setattr(capture_ingest, "run_capture_ingest_once", count_business)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 64
    assert out["error_code"] == "capture_artifact_identity_conflict"
    assert business_calls == 0
    assert artifact_path.read_bytes() == replacement_raw
    assert list((_recovery_root(tmp_path) / "staged").glob("*.artifact.json"))
    assert not (tmp_path / "runtime.sqlite3").exists()


def test_staged_capture_survives_source_read_oserror_with_json_result(
    tmp_path, capsys, monkeypatch
):
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    stage = capture_ingest._stage_validated_artifact

    def crash_after_stage(*args, **kwargs):
        stage(*args, **kwargs)
        raise SimulatedProcessCrash

    monkeypatch.setattr(capture_ingest, "_stage_validated_artifact", crash_after_stage)
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    read_bound = capture_ingest._read_bound_file_at

    def source_read_error(directory_fd, name, *, max_bytes, required_mode):
        if name == artifact_path.name and required_mode is None:
            raise OSError("injected source read failure")
        return read_bound(
            directory_fd,
            name,
            max_bytes=max_bytes,
            required_mode=required_mode,
        )

    monkeypatch.setattr(capture_ingest, "_stage_validated_artifact", stage)
    monkeypatch.setattr(capture_ingest, "_read_bound_file_at", source_read_error)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out
    assert out["status"] == "succeeded"


def test_ingest_recovers_create_only_stage_after_atomic_publish_crash(
    tmp_path, capsys, monkeypatch
):
    """A crash after atomic publication leaves one discoverable stage."""
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    rename_noreplace = capture_ingest._rename_noreplace_at
    crashed = False

    def crash_after_publish(directory_fd, source_name, target_name):
        nonlocal crashed
        result = rename_noreplace(directory_fd, source_name, target_name)
        if not crashed and target_name.endswith(".artifact.json"):
            crashed = True
            raise SimulatedProcessCrash
        return result

    monkeypatch.setattr(capture_ingest, "_rename_noreplace_at", crash_after_publish)
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    staged_dir = _recovery_root(tmp_path) / "staged"
    [staged] = list(staged_dir.glob("*.artifact.json"))
    assert staged.stat().st_nlink == 1
    assert not list(staged_dir.glob(".*.tmp"))

    monkeypatch.setattr(capture_ingest, "_rename_noreplace_at", rename_noreplace)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out
    assert not staged.exists()


def test_ingest_resumes_completed_business_without_creating_a_second_run(
    tmp_path, capsys, monkeypatch
):
    """A crash after completion but before archive reuses the original terminal run."""
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    artifact_payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, artifact_payload)
    publish_archive = capture_ingest._publish_completion_archive

    def crash_before_archive(*_args, **_kwargs):
        raise SimulatedProcessCrash

    monkeypatch.setattr(capture_ingest, "_publish_completion_archive", crash_before_archive)
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        original_run_id = repo.latest_terminal_run()["run_id"]
    finally:
        repo.close()

    monkeypatch.setattr(capture_ingest, "_publish_completion_archive", publish_archive)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out
    receipt = json.loads(
        (_recovery_root(tmp_path) / "consumed" / "123e4567-e89b-12d3-a456-426614174000.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["ingest_run_id"] == original_run_id


def test_ingest_recovers_post_business_rejected_archive_and_cleans_stage(
    tmp_path, capsys, monkeypatch
):
    """A COMPLETE non-ready result is replayed from stage without a second run."""
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest, cdp_runtime
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    artifact_payload = build_artifact_payload(datetime.now(TZ))
    expected_content_sha256 = content_hash(artifact_payload)
    artifact_path = write_artifact(tmp_path, artifact_payload)
    publish_archive = capture_ingest._publish_completion_archive
    publish_g = cdp_runtime._publish_g_working_set_after_terminal_run

    monkeypatch.setattr(
        cdp_runtime,
        "_publish_g_working_set_after_terminal_run",
        lambda **_kwargs: None,
    )

    def crash_before_rejected_archive(*_args, **_kwargs):
        raise SimulatedProcessCrash

    monkeypatch.setattr(
        capture_ingest,
        "_publish_completion_archive",
        crash_before_rejected_archive,
    )
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        original_run_id = repo.latest_terminal_run()["run_id"]
        record = repo.read_capture_ingest(
            artifact_run_id="123e4567-e89b-12d3-a456-426614174000",
            content_sha256=expected_content_sha256,
        )
        assert record is not None
        assert record.phase == "COMPLETE"
    finally:
        repo.close()

    monkeypatch.setattr(capture_ingest, "_publish_completion_archive", publish_archive)
    monkeypatch.setattr(cdp_runtime, "_publish_g_working_set_after_terminal_run", publish_g)

    def forbid_business(*_args, **_kwargs):
        raise AssertionError("COMPLETE rejected recovery must not re-enter business")

    monkeypatch.setattr(capture_ingest, "run_capture_ingest_once", forbid_business)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 1, out
    rejected = _recovery_root(tmp_path) / "rejected"
    marker = json.loads(
        (rejected / "123e4567-e89b-12d3-a456-426614174000.json").read_text(encoding="utf-8")
    )
    assert marker["ingest_run_id"] == original_run_id
    assert artifact_path.exists()
    assert not list((_recovery_root(tmp_path) / "staged").glob("*.artifact.json"))


def test_ingest_resumes_business_terminal_before_g_without_creating_a_second_run(
    tmp_path, capsys, monkeypatch
):
    """A crash after ledger terminalization resumes publication from the same run."""
    from datetime import datetime

    from fin_analyse.scraper import cdp_runtime
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    artifact_payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, artifact_payload)
    publish_g = cdp_runtime._publish_g_working_set_after_terminal_run

    def crash_before_g(**_kwargs):
        raise SimulatedProcessCrash

    monkeypatch.setattr(cdp_runtime, "_publish_g_working_set_after_terminal_run", crash_before_g)
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        original_run_id = repo.latest_terminal_run()["run_id"]
        assert repo.get_active_lease() is None
    finally:
        repo.close()

    monkeypatch.setattr(cdp_runtime, "_publish_g_working_set_after_terminal_run", publish_g)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out
    receipt = json.loads(
        (_recovery_root(tmp_path) / "consumed" / "123e4567-e89b-12d3-a456-426614174000.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["ingest_run_id"] == original_run_id


def test_ingest_resumes_the_persisted_g_publication_plan(tmp_path, capsys, monkeypatch):
    """A crash after PUBLICATION_PREPARED retries the frozen plan, not a new one."""
    from datetime import datetime

    from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    artifact_payload = build_artifact_payload(datetime.now(TZ))
    expected_content_sha256 = content_hash(artifact_payload)
    artifact_path = write_artifact(tmp_path, artifact_payload)
    prepare = ScraperRuntimeRepository.prepare_capture_publication

    def crash_after_prepare(self, **kwargs):
        prepare(self, **kwargs)
        raise SimulatedProcessCrash

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "prepare_capture_publication",
        crash_after_prepare,
    )
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id="123e4567-e89b-12d3-a456-426614174000",
            content_sha256=expected_content_sha256,
        )
        assert record is not None
        assert record.phase == "PUBLICATION_PREPARED"
        assert record.publication_plan_json is not None
        persisted_plan = record.publication_plan_json
        original_run_id = record.ingest_run_id
    finally:
        repo.close()

    monkeypatch.setattr(ScraperRuntimeRepository, "prepare_capture_publication", prepare)

    def forbid_reprepare(self, **kwargs):
        del self, kwargs
        raise AssertionError("a persisted publication plan must not be replaced")

    monkeypatch.setattr(GWorkingSetService, "prepare_publication", forbid_reprepare)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id="123e4567-e89b-12d3-a456-426614174000",
            content_sha256=expected_content_sha256,
        )
        assert record is not None
        assert record.publication_plan_json == persisted_plan
        assert record.ingest_run_id == original_run_id
    finally:
        repo.close()


def test_ingest_recognizes_an_already_published_g_plan(tmp_path, capsys, monkeypatch):
    """A crash after G replace retries the plan without rewriting the manifest."""
    from datetime import datetime

    from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    artifact_payload = build_artifact_payload(datetime.now(TZ))
    expected_content_sha256 = content_hash(artifact_payload)
    artifact_path = write_artifact(tmp_path, artifact_payload)
    complete = ScraperRuntimeRepository.complete_capture_ingest

    def crash_before_complete(self, **_kwargs):
        raise SimulatedProcessCrash

    monkeypatch.setattr(
        ScraperRuntimeRepository,
        "complete_capture_ingest",
        crash_before_complete,
    )
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    manifest_path = _manifest_path(tmp_path / "knowledge-base")
    manifest_before = manifest_path.read_bytes()
    stat_before = manifest_path.stat()
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id="123e4567-e89b-12d3-a456-426614174000",
            content_sha256=expected_content_sha256,
        )
        assert record is not None
        assert record.phase == "PUBLICATION_PREPARED"
        original_run_id = record.ingest_run_id
    finally:
        repo.close()

    monkeypatch.setattr(ScraperRuntimeRepository, "complete_capture_ingest", complete)

    def forbid_manifest_write(self, directory_fd, raw):
        del self, directory_fd, raw
        raise AssertionError("an exact publication retry must be zero-write")

    monkeypatch.setattr(GWorkingSetService, "_publish_raw", forbid_manifest_write)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out
    stat_after = manifest_path.stat()
    assert manifest_path.read_bytes() == manifest_before
    assert (stat_after.st_ino, stat_after.st_mtime_ns) == (
        stat_before.st_ino,
        stat_before.st_mtime_ns,
    )
    receipt = json.loads(
        (_recovery_root(tmp_path) / "consumed" / "123e4567-e89b-12d3-a456-426614174000.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["ingest_run_id"] == original_run_id


def test_ingest_publishes_the_receipt_marker_last(tmp_path, capsys, monkeypatch):
    """Archive raw may exist after a crash; source remains until marker publication."""
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    artifact_payload = build_artifact_payload(datetime.now(TZ))
    expected_content_sha256 = content_hash(artifact_payload)
    artifact_path = write_artifact(tmp_path, artifact_payload)
    publish_marker = capture_ingest._publish_receipt_marker

    def crash_before_marker(*_args, **_kwargs):
        raise SimulatedProcessCrash

    monkeypatch.setattr(capture_ingest, "_publish_receipt_marker", crash_before_marker)
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    consumed = _recovery_root(tmp_path) / "consumed"
    assert (consumed / "123e4567-e89b-12d3-a456-426614174000.artifact.json").exists()
    assert not (consumed / "123e4567-e89b-12d3-a456-426614174000.json").exists()
    assert artifact_path.exists()
    repo = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = repo.read_capture_ingest(
            artifact_run_id="123e4567-e89b-12d3-a456-426614174000",
            content_sha256=expected_content_sha256,
        )
        assert record is not None
        assert record.phase == "COMPLETE"
        original_run_id = record.ingest_run_id
    finally:
        repo.close()

    monkeypatch.setattr(capture_ingest, "_publish_receipt_marker", publish_marker)

    def forbid_business(*_args, **_kwargs):
        raise AssertionError("COMPLETE recovery must not re-enter business")

    monkeypatch.setattr(capture_ingest, "run_capture_ingest_once", forbid_business)
    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out
    assert artifact_path.exists()
    receipt = json.loads(
        (consumed / "123e4567-e89b-12d3-a456-426614174000.json").read_text(encoding="utf-8")
    )
    assert receipt["ingest_run_id"] == original_run_id




def test_ingest_recovers_exact_orphan_temp_before_archive_link(tmp_path, capsys):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    consumed = _prepare_recovery_root(tmp_path) / "consumed"
    consumed.mkdir(mode=0o700)
    target_name = "123e4567-e89b-12d3-a456-426614174000.artifact.json"
    orphan = consumed / f".{target_name}.tmp"
    orphan.write_bytes(artifact_path.read_bytes())
    orphan.chmod(0o600)

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 4, out
    assert (consumed / target_name).exists()
    assert not list(consumed.glob(".*.tmp"))


def test_ingest_busy_handoff_lock_does_not_touch_artifact_or_business_state(tmp_path, capsys):
    """同一 handoff 已有 owner 时只 coalesce，不读取或移动 artifact。"""
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ
    from fin_analyse.scraper.scheduler_handoff_lock import (
        HandoffLockMode,
        hold_scheduler_handoff_lock,
        scheduler_handoff_lock_path,
    )

    tmp_path.chmod(0o700)
    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    runtime_db = tmp_path / "runtime.sqlite3"

    with hold_scheduler_handoff_lock(
        scheduler_handoff_lock_path(runtime_db),
        mode=HandoffLockMode.EXCLUSIVE,
    ):
        exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 75
    assert out == {
        "schema_version": "fin.zsxq-capture-ingest/v1",
        "status": "coalesced",
        "completion_status": "coalesced",
        "completion_data_gaps": [],
        "intent": "sync",
        "trigger": "manual",
        "coalesced": True,
        "error_code": "scheduler_handoff_locked",
    }
    assert artifact_path.exists()
    assert not runtime_db.exists()
    assert not (_recovery_root(tmp_path) / "consumed").exists()
    assert not (_recovery_root(tmp_path) / "rejected").exists()
    assert not _manifest_path(tmp_path / "knowledge-base").exists()


def test_ingest_active_module_owner_coalesces_without_moving_artifact(tmp_path, capsys):
    """旧 writer 只持有 ledger lease 时，手工 ingest 仍保留可重试 artifact。"""
    from datetime import UTC, datetime, timedelta

    from fin_analyse.scraper.cdp_scraper import TZ

    tmp_path.chmod(0o700)
    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    runtime_db = tmp_path / "runtime.sqlite3"
    now = datetime.now(UTC)
    repo = ScraperRuntimeRepository(runtime_db)
    try:
        owner = repo.acquire_or_coalesce(
            intent="sync",
            trigger="manual",
            now=now,
            deadline_at=now + timedelta(minutes=15),
            stale_before=now - timedelta(hours=1),
        )
        assert owner.acquired is True
    finally:
        repo.close()

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 75
    assert out["status"] == "coalesced"
    assert out["active_run_id"] == owner.run_id
    assert artifact_path.exists()
    assert not (_recovery_root(tmp_path) / "consumed").exists()
    assert not (_recovery_root(tmp_path) / "rejected").exists()
    assert not _manifest_path(tmp_path / "knowledge-base").exists()


def test_ingest_replaced_artifact_coalesces_across_runtime_databases(tmp_path, capsys):
    """稳定 handoff owner 不因 artifact 原子替换或 runtime DB 不同而失效。"""
    import fcntl
    import os
    from datetime import datetime

    from fin_analyse.scraper.capture_ingest import main
    from fin_analyse.scraper.cdp_scraper import TZ

    tmp_path.chmod(0o700)
    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    runtime_db = tmp_path / "runtime-b.sqlite3"
    kb_root = tmp_path / "knowledge-base"
    kb_root.mkdir(parents=True)
    (kb_root / "index.json").write_text(
        json.dumps({"articles": [], "total": 0, "updated": ""}), encoding="utf-8"
    )

    replacement_path = artifact_path.with_name("capture.next.json")
    replacement_path.write_text(
        json.dumps(build_artifact_payload(datetime.now(TZ)), ensure_ascii=False),
        encoding="utf-8",
    )
    handoff_descriptor = os.open(artifact_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(handoff_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        replacement_path.replace(artifact_path)
        replacement_bytes = artifact_path.read_bytes()
        exit_code = main(
            [
                "--artifact",
                str(artifact_path),
                "--runtime-db",
                str(runtime_db),
                "--knowledge-base-root",
                str(kb_root),
                "--trigger",
                "manual",
            ],
            _canonical_runtime_db=runtime_db,
        )
    finally:
        os.close(handoff_descriptor)

    out = json.loads(capsys.readouterr().out)
    assert exit_code == 75
    assert out == {
        "schema_version": "fin.zsxq-capture-ingest/v1",
        "status": "coalesced",
        "completion_status": "coalesced",
        "completion_data_gaps": [],
        "intent": "sync",
        "trigger": "manual",
        "coalesced": True,
        "error_code": "capture_handoff_locked",
    }
    assert artifact_path.exists()
    assert artifact_path.read_bytes() == replacement_bytes
    assert not runtime_db.exists()
    assert not (_recovery_root(tmp_path) / "consumed").exists()
    assert not (_recovery_root(tmp_path) / "rejected").exists()
    assert not _manifest_path(kb_root).exists()


def test_public_entry_rejects_another_runtime_db_before_business(
    tmp_path,
    capsys,
    monkeypatch,
):
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    first_exit, first_result = run_ingest(artifact_path, tmp_path, capsys)
    assert first_exit == 4, first_result
    manifest_path = _manifest_path(tmp_path / "knowledge-base")
    manifest_before = manifest_path.read_bytes()
    business_calls = 0
    business = capture_ingest.run_capture_ingest_once

    def count_business(*args, **kwargs):
        nonlocal business_calls
        business_calls += 1
        return business(*args, **kwargs)

    monkeypatch.setattr(capture_ingest, "run_capture_ingest_once", count_business)
    second_runtime = tmp_path / "other-runtime-owner" / "runtime.sqlite3"
    exit_code = capture_ingest.main(
        [
            "--artifact",
            str(artifact_path),
            "--runtime-db",
            str(second_runtime),
            "--knowledge-base-root",
            str(tmp_path / "knowledge-base"),
            "--trigger",
            "manual",
        ],
        _canonical_runtime_db=tmp_path / "runtime.sqlite3",
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 64
    assert result["status"] == "invalid_request"
    assert result["error_code"] == "runtime_db_not_canonical"
    assert business_calls == 0
    assert manifest_path.read_bytes() == manifest_before
    assert not second_runtime.exists()


def test_incomplete_archive_recovery_stays_bound_to_its_runtime_db(
    tmp_path,
    capsys,
    monkeypatch,
):
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    class SimulatedProcessCrash(BaseException):
        pass

    artifact_payload = build_artifact_payload(datetime.now(TZ))
    artifact_path = write_artifact(tmp_path, artifact_payload)
    publish_archive = capture_ingest._publish_completion_archive

    def crash_before_archive(*_args, **_kwargs):
        raise SimulatedProcessCrash

    monkeypatch.setattr(
        capture_ingest,
        "_publish_completion_archive",
        crash_before_archive,
    )
    with pytest.raises(SimulatedProcessCrash):
        run_ingest(artifact_path, tmp_path, capsys)

    first_repository = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    try:
        record = first_repository.read_capture_ingest(
            artifact_run_id=artifact_payload["run_id"],
            content_sha256=artifact_payload["content_sha256"],
        )
        assert record is not None
        assert record.phase == "COMPLETE"
    finally:
        first_repository.close()

    monkeypatch.setattr(
        capture_ingest,
        "_publish_completion_archive",
        publish_archive,
    )
    business_calls = 0
    business = capture_ingest.run_capture_ingest_once

    def count_business(*args, **kwargs):
        nonlocal business_calls
        business_calls += 1
        return business(*args, **kwargs)

    monkeypatch.setattr(capture_ingest, "run_capture_ingest_once", count_business)
    owner_path = _recovery_root(tmp_path) / "runtime-owner.json"
    owner_path.write_text(
        json.dumps(
            {
                "schema_version": "fin.zsxq-capture-recovery-owner/v1",
                "runtime_owner_id": capture_ingest.capture_runtime_owner_id(
                    tmp_path / "runtime-b.sqlite3"
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    owner_path.chmod(0o600)
    exit_code = capture_ingest.main(
        [
            "--artifact",
            str(artifact_path),
            "--knowledge-base-root",
            str(tmp_path / "knowledge-base"),
        ],
        _canonical_runtime_db=tmp_path / "runtime.sqlite3",
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 64
    assert result["error_code"] == "capture_recovery_owner_conflict"
    assert business_calls == 0
    assert list((_recovery_root(tmp_path) / "staged").glob("*.artifact.json"))


def test_ingest_symlink_artifact_cannot_bypass_busy_handoff_lock(tmp_path, capsys):
    """artifact symlink 仍由所在 handoff directory 的稳定 claim 串行化。"""
    import fcntl
    import os
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    tmp_path.chmod(0o700)
    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    target_path = tmp_path / "capture.target.json"
    artifact_path.replace(target_path)
    artifact_path.symlink_to(target_path)
    handoff_descriptor = os.open(artifact_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(handoff_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exit_code, out = run_ingest(artifact_path, tmp_path, capsys)
    finally:
        os.close(handoff_descriptor)

    assert exit_code == 75
    assert out["error_code"] == "capture_handoff_locked"
    assert artifact_path.is_symlink()
    assert target_path.exists()
    assert not (tmp_path / "runtime.sqlite3").exists()
    assert not (_recovery_root(tmp_path) / "consumed").exists()
    assert not (_recovery_root(tmp_path) / "rejected").exists()
    assert not _manifest_path(tmp_path / "knowledge-base").exists()


def test_ingest_symlink_artifact_is_not_read_when_handoff_is_idle(tmp_path, capsys):
    """The public entry never follows an artifact symlink outside the handoff."""
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    tmp_path.chmod(0o700)
    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    external = tmp_path / "external-capture.json"
    artifact_path.replace(external)
    artifact_path.symlink_to(external)

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 64
    assert out["error_code"] == "capture_artifact_unreadable:ValueError"
    assert artifact_path.is_symlink()
    assert external.exists()
    assert not (tmp_path / "runtime.sqlite3").exists()


def test_ingest_does_not_follow_a_staged_directory_symlink(tmp_path, capsys):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    external = tmp_path / "external-staged"
    external.mkdir()
    (_prepare_recovery_root(tmp_path) / "staged").symlink_to(
        external,
        target_is_directory=True,
    )

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 64
    assert out["error_code"] == "capture_staging_identity_conflict"
    assert list(external.iterdir()) == []
    assert artifact_path.exists()
    assert not (tmp_path / "runtime.sqlite3").exists()


def test_ingest_does_not_follow_a_consumed_directory_symlink(tmp_path, capsys):
    from datetime import datetime

    from fin_analyse.scraper.cdp_scraper import TZ

    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    external = tmp_path / "external-consumed"
    external.mkdir()
    (_prepare_recovery_root(tmp_path) / "consumed").symlink_to(
        external,
        target_is_directory=True,
    )

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert out["archive_warning"].startswith("consumed_archive_failed:")
    assert list(external.iterdir()) == []
    assert artifact_path.exists()
    assert list((_recovery_root(tmp_path) / "staged").glob("*.artifact.json"))


def test_ingest_handoff_identity_drift_emits_only_internal_error(tmp_path, capsys, monkeypatch):
    """All writes stay on the locked inode if its pathname is replaced."""
    from datetime import datetime

    from fin_analyse.scraper import capture_ingest
    from fin_analyse.scraper.cdp_scraper import TZ

    tmp_path.chmod(0o700)
    artifact_path = write_artifact(tmp_path, build_artifact_payload(datetime.now(TZ)))
    moved_handoff = tmp_path / "handoff.moved"
    stage = capture_ingest._stage_validated_artifact

    def drift_before_stage(handoff_fd, artifact, raw):
        artifact_path.parent.rename(moved_handoff)
        artifact_path.parent.mkdir(mode=0o700)
        return stage(handoff_fd, artifact, raw)

    monkeypatch.setattr(capture_ingest, "_stage_validated_artifact", drift_before_stage)

    exit_code, out = run_ingest(artifact_path, tmp_path, capsys)

    assert exit_code == 70
    assert out == {
        "schema_version": "fin.zsxq-capture-ingest/v1",
        "status": "internal_error",
        "error_code": "capture_handoff_lock_failed",
    }
    assert not list((_recovery_root(tmp_path) / "staged").glob("*.artifact.json"))
    assert (_recovery_root(tmp_path) / "consumed").exists()
    assert not (moved_handoff / "consumed").exists()
