"""主线提名 → 裁决收件箱 producer 挂点单测（adjudication-inbox 设计页）。"""

from __future__ import annotations

from pathlib import Path

from scripts.consume_zsxq_capture_folder import _reconcile_mainline_nomination_inbox


def _inbox(tmp_path: Path):
    from fin_analyse.adjudication.inbox import open_default_inbox

    return open_default_inbox()


def test_scanned_with_nominations_opens_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _reconcile_mainline_nomination_inbox(
        {
            "schema_version": "fin.mainline-candidates/v1",
            "disposition": "SCANNED",
            "nominated": 2,
            "same_article": 1,
            "draft_path": "/tmp/mainline-candidates.md",
        }
    )
    row = _inbox(tmp_path).get("mainline.nomination")
    assert row is not None
    assert row.status == "open"
    assert row.title == "G 主线候选提名：2 条待勾选，另有 1 条同文待核"
    assert row.payload_ref == "/tmp/mainline-candidates.md"
    assert "fin-adjudication done mainline.nomination" in (row.resolution_hint or "")


def test_scanned_zero_nominated_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    inbox = _inbox(tmp_path)
    inbox.reconcile(
        _nomination_item(2), pending=True
    )
    _reconcile_mainline_nomination_inbox(
        {"disposition": "SCANNED", "nominated": 0, "same_article": 0}
    )
    row = inbox.get("mainline.nomination")
    assert row is not None
    assert row.status == "resolved"
    assert row.resolve_source == "producer"


def test_skipped_and_failed_leave_inbox_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    inbox = _inbox(tmp_path)
    inbox.reconcile(_nomination_item(1), pending=True)
    for disposition in ("SKIPPED", "FAILED"):
        _reconcile_mainline_nomination_inbox({"disposition": disposition})
    row = inbox.get("mainline.nomination")
    assert row is not None
    assert row.status == "open"


def _nomination_item(count: int):
    from fin_analyse.adjudication import AdjudicationItem

    return AdjudicationItem(
        item_id="mainline.nomination",
        kind="g-mainline-nomination",
        title=f"G 主线候选提名：{count} 条待勾选",
    )
