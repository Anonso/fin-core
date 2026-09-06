"""回放线提名 → 裁决收件箱 producer 单测（D-051 追记 #2）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin_analyse.adjudication.producers import scan_replay_nominations
from scripts.consume_zsxq_capture_folder import _reconcile_replay_nomination_inbox


def _evidence(tmp_path: Path, batches: list[tuple[str, list[str]]]) -> Path:
    root = tmp_path / "cognition-replay-evidence"
    root.mkdir(parents=True, exist_ok=True)
    for batch, units in batches:
        (root / f"nominations-{batch}.json").write_text(
            json.dumps(
                {
                    "schema_version": "fin.cognition-replay-nominations/v1",
                    "batch": batch,
                    "nominations": [{"unit_id": u} for u in units],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return root


def _annotation(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "g-cognition-mainline.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_scan_skips_when_no_nominations(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    result = scan_replay_nominations(
        evidence_root=root, annotation_path=_annotation(tmp_path, "x")
    )
    assert result.disposition == "SKIPPED"
    assert result.reason == "no_nominations"


def test_scan_latest_batch_wins_and_landed_by_batch_marker(tmp_path: Path) -> None:
    root = _evidence(tmp_path, [("20260903", ["CU-0903-01"]), ("20260904", ["CU-0811-02", "CU-0828-02"])])
    doc = _annotation(tmp_path, "## 独立证据补充（批次 20260904 首批扫批）\n")
    result = scan_replay_nominations(evidence_root=root, annotation_path=doc)
    assert result.disposition == "SCANNED"
    assert result.batch == "20260904"
    assert result.proposals == 2
    assert result.landed is True
    assert result.nominations_path.endswith("nominations-20260904.json")


def test_scan_landed_by_all_unit_ids_without_batch_marker(tmp_path: Path) -> None:
    root = _evidence(tmp_path, [("20260904", ["CU-0811-02", "CU-0828-02"])])
    doc = _annotation(tmp_path, "逐提案：CU-0811-02 蹦床；CU-0828-02 月线。\n")
    result = scan_replay_nominations(evidence_root=root, annotation_path=doc)
    assert result.landed is True


def test_scan_unlanded_latest_batch(tmp_path: Path) -> None:
    root = _evidence(tmp_path, [("20260907", ["CU-0907-01", "CU-0907-02"])])
    doc = _annotation(tmp_path, "## 独立证据补充（批次 20260904 首批扫批）\n")
    result = scan_replay_nominations(evidence_root=root, annotation_path=doc)
    assert result.batch == "20260907"
    assert result.landed is False


def test_scan_unparseable_is_skipped_not_guessed(tmp_path: Path) -> None:
    root = tmp_path / "cognition-replay-evidence"
    root.mkdir(parents=True)
    (root / "nominations-20260904.json").write_text("{broken", encoding="utf-8")
    result = scan_replay_nominations(
        evidence_root=root, annotation_path=_annotation(tmp_path, "x")
    )
    assert result.disposition == "SKIPPED"
    assert result.reason == "nominations_unparseable"


def test_hook_opens_unlanded_and_resolves_after_landing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin_analyse.adjudication.inbox import open_default_inbox

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # 挂点按真实布局找 evidence：$STATE/fin-analyse/cognition-replay-evidence
    evidence_root = tmp_path / "state" / "fin-analyse" / "cognition-replay-evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "nominations-20260907.json").write_text(
        json.dumps(
            {"batch": "20260907", "nominations": [{"unit_id": "CU-0907-01"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    doc = _annotation(tmp_path, "旧批次内容")
    _reconcile_replay_nomination_inbox(annotation_path=doc)
    row = open_default_inbox().get("replay.nomination")
    assert row is not None
    assert row.status == "open"
    assert "批次 20260907" in row.title
    assert "1 条提案" in row.title
    assert row.payload_ref is not None and row.payload_ref.endswith("nominations-20260907.json")

    # owner 落账：标注文档出现批次标记 → auto-resolve
    doc.write_text("## 独立证据补充（批次 20260907 首批扫批）\n", encoding="utf-8")
    _reconcile_replay_nomination_inbox(annotation_path=doc)
    row = open_default_inbox().get("replay.nomination")
    assert row is not None
    assert row.status == "resolved"
    assert row.resolve_source == "producer"


def test_hook_landed_batch_never_opens_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin_analyse.adjudication.inbox import open_default_inbox

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _evidence(tmp_path, [("20260904", ["CU-0828-02"])])
    doc = _annotation(tmp_path, "（批次 20260904 首批扫批）\n")
    _reconcile_replay_nomination_inbox(annotation_path=doc)
    # pending=False 且行不存在 → no-op，不开项
    assert open_default_inbox().get("replay.nomination") is None
