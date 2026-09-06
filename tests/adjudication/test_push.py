"""push 触达单测：指纹去重 / 防抖 / 读侧 fail-closed / 升级标记。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fin_analyse.adjudication.inbox import (
    AdjudicationInbox,
    AdjudicationItem,
)
from fin_analyse.adjudication.config import load_adjudication_config
from fin_analyse.adjudication.push import (
    PushDisposition,
    PushError,
    push,
)

_NOW = datetime(2026, 9, 6, 9, 0, 0, tzinfo=UTC)  # 上海 17:00，day=2026-09-06
_ENV = {"FIN_DAILY_WORKSPACE_DELIVERY_TARGET": "feishu:ctl"}
_ITEM = AdjudicationItem(
    item_id="mainline.nomination",
    kind="g-mainline-nomination",
    title="G 主线候选提名：2 条待勾选",
    payload_ref="/tmp/mainline-candidates.md",
    resolution_hint="勾选→起草协议→归档",
)


class _FakeSender:
    def __init__(self, *, message_id: str | None = "mid-1", error: Exception | None = None):
        self.message_id = message_id
        self.error = error
        self.messages: list[str] = []

    def send(self, message: str) -> str | None:
        if self.error is not None:
            raise self.error
        self.messages.append(message)
        return self.message_id


@pytest.fixture()
def inbox(tmp_path: Path) -> AdjudicationInbox:
    return AdjudicationInbox(state_root=tmp_path / "inbox")


def _sent_count(inbox: AdjudicationInbox) -> int:
    connection = inbox._connect()  # noqa: SLF001 - 测试直读 sent_log
    return int(connection.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0])


def test_push_no_open_items_is_noop(inbox: AdjudicationInbox) -> None:
    sender = _FakeSender()
    outcome = push(inbox, sender=sender, target_environ=_ENV, now=_NOW)
    assert outcome.disposition == PushDisposition.NO_OPEN_ITEMS
    assert sender.messages == []
    assert _sent_count(inbox) == 0


def test_push_sends_once_and_records_ledger(inbox: AdjudicationInbox) -> None:
    inbox.reconcile(_ITEM, pending=True)
    sender = _FakeSender()
    first = push(inbox, sender=sender, target_environ=_ENV, now=_NOW)
    assert first.disposition == PushDisposition.SENT
    assert len(sender.messages) == 1
    assert "裁决收件箱" in sender.messages[0]
    again = push(inbox, sender=sender, target_environ=_ENV, now=_NOW)
    assert again.disposition == PushDisposition.ALREADY_SENT
    assert len(sender.messages) == 1
    assert _sent_count(inbox) == 1


def test_push_digest_contains_hint_and_ref(inbox: AdjudicationInbox) -> None:
    inbox.reconcile(_ITEM, pending=True)
    sender = _FakeSender()
    push(inbox, sender=sender, target_environ=_ENV, now=_NOW)
    body = sender.messages[0]
    assert "1 项待裁决" in body
    assert "[g-mainline-nomination]" in body
    assert "勾选→起草协议→归档" in body
    assert "/tmp/mainline-candidates.md" in body


def test_push_escalation_marker_after_threshold(
    inbox: AdjudicationInbox, tmp_path: Path
) -> None:
    inbox.reconcile(_ITEM, pending=True)
    sender = _FakeSender()
    late = datetime(2026, 9, 12, 1, 0, 0, tzinfo=UTC)  # 积压 6 天 > 3
    push(inbox, sender=sender, target_environ=_ENV, now=late)
    assert "⚠ " in sender.messages[0]


def test_same_day_new_fingerprint_without_transition_is_debounced(
    inbox: AdjudicationInbox,
) -> None:
    inbox.reconcile(_ITEM, pending=True)
    sender = _FakeSender()
    assert push(inbox, sender=sender, target_environ=_ENV, now=_NOW).disposition == "sent"
    # 同日标题变化（指纹变）但无转换事件 → 防抖并入次日班
    inbox.reconcile(
        AdjudicationItem(
            item_id="mainline.nomination",
            kind="g-mainline-nomination",
            title="G 主线候选提名：3 条待勾选（标题抖动）",
        ),
        pending=True,
    )
    outcome = push(inbox, sender=sender, target_environ=_ENV, now=_NOW)
    assert outcome.disposition == PushDisposition.DEBOUNCED
    assert len(sender.messages) == 1


def test_same_day_transition_event_allows_resend(inbox: AdjudicationInbox) -> None:
    inbox.reconcile(_ITEM, pending=True)
    sender = _FakeSender()
    assert push(inbox, sender=sender, target_environ=_ENV, now=_NOW).disposition == "sent"
    # 新手动项 = 当日 submit 转换事件 → 允许重发
    inbox.add_manual(title="BUG-058 分母合同裁决", resolution_hint="拍板其一后施工")
    outcome = push(inbox, sender=sender, target_environ=_ENV, now=_NOW)
    assert outcome.disposition == PushDisposition.SENT
    assert len(sender.messages) == 2


def test_sender_exception_writes_no_ledger(inbox: AdjudicationInbox) -> None:
    inbox.reconcile(_ITEM, pending=True)
    sender = _FakeSender(error=RuntimeError("hermes send failed"))
    with pytest.raises(RuntimeError, match="hermes send failed"):
        push(inbox, sender=sender, target_environ=_ENV, now=_NOW)
    assert _sent_count(inbox) == 0
    # 当日重试机会仍在
    retry = push(inbox, sender=_FakeSender(), target_environ=_ENV, now=_NOW)
    assert retry.disposition == PushDisposition.SENT


def test_sender_none_outcome_writes_no_ledger(inbox: AdjudicationInbox) -> None:
    """平台接受未回执（None）→ 不落账（读侧 fail-closed，r2-P2-2）。"""

    inbox.reconcile(_ITEM, pending=True)
    sender = _FakeSender(message_id=None)
    outcome = push(inbox, sender=sender, target_environ=_ENV, now=_NOW)
    assert outcome.disposition == PushDisposition.OUTCOME_UNKNOWN
    assert _sent_count(inbox) == 0


def test_missing_target_raises_before_send(inbox: AdjudicationInbox) -> None:
    inbox.reconcile(_ITEM, pending=True)
    sender = _FakeSender()
    with pytest.raises(PushError, match="adjudication_push_target_missing"):
        push(inbox, sender=sender, target_environ={}, now=_NOW)
    assert sender.messages == []


def test_dry_run_renders_without_send_or_ledger(inbox: AdjudicationInbox) -> None:
    inbox.reconcile(_ITEM, pending=True)
    outcome = push(inbox, sender=None, target_environ=_ENV, now=_NOW, dry_run=True)
    assert outcome.disposition == PushDisposition.DRY_RUN
    assert outcome.rendered is not None
    assert "1 项待裁决" in outcome.rendered
    assert _sent_count(inbox) == 0


def test_load_adjudication_config_defaults_and_validation(tmp_path: Path) -> None:
    assert load_adjudication_config(tmp_path / "missing.yaml") == {
        "escalate_after_days": 3,
        "g_lag_days_threshold": 2,
    }
    config_file = tmp_path / "adjudication.yaml"
    config_file.write_text(
        "digest:\n  escalate_after_days: 5\ng_annotation_batch:\n"
        "  lag_days_threshold: 4\n",
        encoding="utf-8",
    )
    assert load_adjudication_config(config_file) == {
        "escalate_after_days": 5,
        "g_lag_days_threshold": 4,
    }
    bad = tmp_path / "bad.yaml"
    bad.write_text("digest:\n  escalate_after_days: -1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_adjudication_config(bad)
    # push() 把配置错误包成 typed PushError（fail-closed 于发送前）
    from fin_analyse.adjudication.inbox import AdjudicationInbox

    inbox = AdjudicationInbox(state_root=tmp_path / "inbox")
    inbox.reconcile(
        __import__(
            "fin_analyse.adjudication.inbox", fromlist=["AdjudicationItem"]
        ).AdjudicationItem(item_id="x", kind="k", title="t"),
        pending=True,
    )
    with pytest.raises(PushError):
        push(inbox, sender=None, target_environ={}, now=_NOW, config_path=bad)
