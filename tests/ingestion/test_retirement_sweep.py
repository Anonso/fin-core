"""退役墓碑清扫测试（设计门 zsxq-retirement-tombstone-20260908 探针面）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fin_analyse.ingestion.retirement import (
    load_registry,
    sweep_retired_articles,
)

ART_ID = "zsxq-45548825112521288"
SHA = hashlib.sha256(b"retired body").hexdigest()


def _kb(tmp_path: Path, *, with_md: bool = True, with_index: bool = True):
    kb = tmp_path / "kb"
    (kb / "articles").mkdir(parents=True)
    md = kb / "articles" / f"20260904_zsxq-{ART_ID.removeprefix('zsxq-')}.md"
    if with_md:
        md.write_bytes(b"retired body")
    if with_index:
        (kb / "index.json").write_text(json.dumps({
            "articles": [{"id": ART_ID, "path": str(md)},
                         {"id": "zsxq-other", "path": "x"}],
            "total": 2, "updated": "2026-09-01",
        }, ensure_ascii=False), encoding="utf-8")
    return kb, md


def _backup(tmp_path: Path, *, sha: str = SHA, with_item: bool = True):
    bdir = tmp_path / "article-prune-backup"
    bdir.mkdir(parents=True)
    (bdir / "20260904_zsxq-x.md").write_bytes(b"retired body")
    items = ([{"type": "article_file", "article_id": ART_ID,
               "backup": str(bdir / "f.md"), "sha256": sha}] if with_item else [])
    (bdir / "manifest.json").write_text(json.dumps(
        {"created_at": "2026-09-04", "reason": "test", "items": items}),
        encoding="utf-8")
    return bdir


def _registry(tmp_path: Path, backup_ref: str) -> Path:
    p = tmp_path / "zsxq_retired.json"
    p.write_text(json.dumps({"schema_version": "fin.zsxq-retired/v1",
                             "retired": [{"id": ART_ID, "retired_at": "2026-09-04",
                                          "reason": "test", "backup_ref": backup_ref}]}),
                 encoding="utf-8")
    return p


def test_sweep_removes_resurrected_article_and_index_entry(tmp_path):
    kb, md = _kb(tmp_path)
    _backup(tmp_path)
    payload = sweep_retired_articles(kb, _registry(tmp_path, "article-prune-backup"), data_home=tmp_path)
    assert not md.exists()
    assert payload["disposition"] == "SWEPT"
    idx = json.loads((kb / "index.json").read_text(encoding="utf-8"))
    assert [a["id"] for a in idx["articles"]] == ["zsxq-other"]
    assert idx["total"] == 1
    assert idx["updated"] != "2026-09-01"
    assert any(a["action"] == "removed_md" for a in payload["actions"])


def test_missing_manifest_entry_blocks_deletion(tmp_path):
    kb, md = _kb(tmp_path)
    _backup(tmp_path, with_item=False)
    payload = sweep_retired_articles(kb, _registry(tmp_path, "article-prune-backup"), data_home=tmp_path)
    assert md.exists()
    assert any("manifest_entry_missing" in w["warn"] for w in payload["warns"])


def test_sha_mismatch_blocks_deletion(tmp_path):
    kb, md = _kb(tmp_path)
    md.write_bytes(b"tampered")  # 与备份 sha 不符（typo id 防误删同源）
    _backup(tmp_path)
    payload = sweep_retired_articles(kb, _registry(tmp_path, "article-prune-backup"), data_home=tmp_path)
    assert md.exists()
    assert any("sha_mismatch" in w["warn"] for w in payload["warns"])


def test_missing_backup_dir_blocks_deletion(tmp_path):
    kb, md = _kb(tmp_path)
    payload = sweep_retired_articles(
        kb, _registry(tmp_path, "article-prune-backup-NOT-EXIST"))
    assert md.exists()
    assert any("manifest_entry_missing" in w["warn"] for w in payload["warns"])


def test_non_tombstoned_and_clean_run_no_index_write(tmp_path):
    kb = tmp_path / "kb"
    (kb / "articles").mkdir(parents=True)
    (kb / "articles" / "20260905_zsxq-999.md").write_bytes(b"keep me")
    (kb / "index.json").write_text(json.dumps({
        "articles": [{"id": "zsxq-999", "path": "x"}], "total": 1,
        "updated": "2026-09-01"}, ensure_ascii=False), encoding="utf-8")
    before = (kb / "index.json").read_text(encoding="utf-8")
    payload = sweep_retired_articles(kb, _registry(tmp_path, "article-prune-backup"),
                                     data_home=tmp_path)
    assert payload["disposition"] == "CLEAN"
    assert (kb / "articles" / "20260905_zsxq-999.md").exists()
    assert (kb / "index.json").read_text(encoding="utf-8") == before  # 无变更不落盘


def test_corrupt_registry_fails_open(tmp_path):
    kb, md = _kb(tmp_path)
    reg = tmp_path / "bad.json"
    reg.write_text("{broken", encoding="utf-8")
    assert load_registry(reg) == []
    assert sweep_retired_articles(kb, reg)["disposition"] == "CLEAN"
    assert md.exists()


def test_index_entry_removed_even_when_md_absent(tmp_path):
    kb, md = _kb(tmp_path, with_md=False)
    _backup(tmp_path)
    payload = sweep_retired_articles(kb, _registry(tmp_path, "article-prune-backup"), data_home=tmp_path)
    idx = json.loads((kb / "index.json").read_text(encoding="utf-8"))
    assert [a["id"] for a in idx["articles"]] == ["zsxq-other"]
    assert any(a["action"] == "index_entries_removed" for a in payload["actions"])
