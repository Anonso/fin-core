"""裁决收件箱 store 单测（adjudication-inbox 设计页 §验证方式）。"""

from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fin_analyse.adjudication.inbox import (
    AdjudicationInbox,
    AdjudicationInboxError,
    AdjudicationItem,
    inbox_fingerprint,
)

_MAINLINE = AdjudicationItem(
    item_id="mainline.nomination",
    kind="g-mainline-nomination",
    title="G 主线候选提名：3 条待勾选",
    payload_ref="/tmp/mainline-candidates.md",
    resolution_hint="勾选→起草协议→归档；完成后 fin-adjudication done mainline.nomination",
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


@pytest.fixture()
def inbox(tmp_path: Path, clock: _Clock) -> AdjudicationInbox:
    return AdjudicationInbox(state_root=tmp_path / "adjudication-inbox-v1", clock=clock)


def test_reconcile_insert_adds_submit_event(inbox: AdjudicationInbox) -> None:
    inbox.reconcile(_MAINLINE, pending=True)
    row = inbox.get("mainline.nomination")
    assert row is not None
    assert row.status == "open"
    assert row.opened_at == 1_000_000.0
    assert row.reopen_count == 0


def test_replay_same_content_adds_no_event_and_keeps_opened_at(
    inbox: AdjudicationInbox, clock: _Clock
) -> None:
    inbox.reconcile(_MAINLINE, pending=True)
    clock.advance(3600)
    inbox.reconcile(_MAINLINE, pending=True)  # 高频重放
    row = inbox.get("mainline.nomination")
    assert row is not None
    assert row.opened_at == 1_000_000.0
    assert row.last_seen_at == 1_003_600.0
    assert _transition_count(inbox) == 1  # 仅 submit


def test_replay_updated_content_updates_fields_without_event(
    inbox: AdjudicationInbox, clock: _Clock
) -> None:
    inbox.reconcile(_MAINLINE, pending=True)
    clock.advance(60)
    inbox.reconcile(
        AdjudicationItem(
            item_id="mainline.nomination",
            kind="g-mainline-nomination",
            title="G 主线候选提名：5 条待勾选",
            payload_ref=_MAINLINE.payload_ref,
            resolution_hint=_MAINLINE.resolution_hint,
        ),
        pending=True,
    )
    row = inbox.get("mainline.nomination")
    assert row is not None
    assert row.title == "G 主线候选提名：5 条待勾选"
    assert row.opened_at == 1_000_000.0
    assert _transition_count(inbox) == 1


def test_zero_nominated_resolves_open_item(inbox: AdjudicationInbox) -> None:
    inbox.reconcile(_MAINLINE, pending=True)
    inbox.reconcile(
        AdjudicationItem(
            item_id="mainline.nomination",
            kind="g-mainline-nomination",
            title="G 主线候选提名：0 条待勾选",
        ),
        pending=False,
    )
    row = inbox.get("mainline.nomination")
    assert row is not None
    assert row.status == "resolved"
    assert row.resolve_source == "producer"


def test_resolved_then_pending_again_reopens_and_resets_opened_at(
    inbox: AdjudicationInbox, clock: _Clock
) -> None:
    inbox.reconcile(_MAINLINE, pending=True)
    inbox.reconcile(_MAINLINE, pending=False)
    clock.advance(86400)
    inbox.reconcile(_MAINLINE, pending=True)
    row = inbox.get("mainline.nomination")
    assert row is not None
    assert row.status == "open"
    assert row.opened_at == 1_086_400.0  # reopen 必重置
    assert row.reopen_count == 1
    assert _transition_count(inbox) == 3  # submit + resolve + reopen


def test_reconcile_pending_false_without_row_is_noop(inbox: AdjudicationInbox) -> None:
    inbox.reconcile(_MAINLINE, pending=False)
    assert inbox.get("mainline.nomination") is None
    assert _transition_count(inbox) == 0


def test_owner_resolve_and_done_guard(inbox: AdjudicationInbox) -> None:
    inbox.reconcile(_MAINLINE, pending=True)
    inbox.resolve("mainline.nomination", source="owner", note="已入档")
    row = inbox.get("mainline.nomination")
    assert row is not None
    assert row.note == "已入档"
    with pytest.raises(AdjudicationInboxError, match="adjudication_item_not_open"):
        inbox.resolve("mainline.nomination", source="owner")
    with pytest.raises(AdjudicationInboxError, match="adjudication_item_not_open"):
        inbox.resolve("missing.item", source="owner")


def test_add_manual_uses_submit_event_and_slug_id(inbox: AdjudicationInbox) -> None:
    item_id = inbox.add_manual(title="BUG-058 分母合同裁决", resolution_hint="拍板其一后施工")
    assert item_id.startswith("manual:")
    row = inbox.get(item_id)
    assert row is not None
    assert row.kind == "manual"
    assert _transition_count(inbox) == 1


def test_add_manual_duplicate_title_is_idempotent(inbox: AdjudicationInbox) -> None:
    first = inbox.add_manual(title="待拍板项", resolution_hint="a")
    second = inbox.add_manual(title="待拍板项", resolution_hint="a")
    assert first == second
    assert _transition_count(inbox) == 1


def test_invalid_items_rejected(inbox: AdjudicationInbox) -> None:
    with pytest.raises(AdjudicationInboxError):
        inbox.reconcile(
            AdjudicationItem(item_id="", kind="k", title="t"), pending=True
        )
    with pytest.raises(AdjudicationInboxError):
        inbox.reconcile(
            AdjudicationItem(item_id="x", kind="k", title=" "), pending=True
        )


def test_db_and_sidecar_modes_owner_only(tmp_path: Path) -> None:
    root = tmp_path / "adjudication-inbox-v1"
    inbox = AdjudicationInbox(state_root=root)
    inbox.reconcile(_MAINLINE, pending=True)
    inbox.record_sent(day="2026-09-06", fingerprint="abc")
    assert stat.S_IMODE(os.stat(root / "inbox.sqlite").st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        candidate = root / f"inbox.sqlite{suffix}"
        if candidate.exists():
            assert stat.S_IMODE(os.stat(candidate).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700


def test_concurrent_reconciles_serialize(inbox: AdjudicationInbox) -> None:
    def _write(index: int) -> None:
        item = AdjudicationItem(
            item_id=f"manual:concurrent-{index}",
            kind="manual",
            title=f"并发写入 {index}",
        )
        inbox.reconcile(item, pending=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_write, range(16)))
    assert len(inbox.list_open()) == 16


def test_fingerprint_stable_and_content_sensitive() -> None:
    rows = [
        _row("a", "标题 A"),
        _row("b", "标题 B"),
    ]
    assert inbox_fingerprint(rows) == inbox_fingerprint(list(reversed(rows)))
    assert inbox_fingerprint(rows) != inbox_fingerprint([rows[0], _row("b", "标题 B改")])


def _row(item_id: str, title: str):
    from fin_analyse.adjudication.inbox import ItemRow

    return ItemRow(
        item_id=item_id,
        kind="k",
        title=title,
        payload_ref=None,
        resolution_hint=None,
        status="open",
        opened_at=1.0,
        last_seen_at=1.0,
        resolved_at=None,
        resolve_source=None,
        note=None,
        reopen_count=0,
    )


def _transition_count(inbox: AdjudicationInbox) -> int:
    connection = inbox._connect()  # noqa: SLF001 - 测试直读事件台账
    return int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
