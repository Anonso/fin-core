"""每日触达：push 待裁决摘要（adjudication-inbox 设计页 §触达）。

读侧 fail-closed：sender 抛错、超时、或返回 None（平台接受未回执）→ 一律
不写 ``sent_log``，当日后续重试机会仍可重发；发送成功（有 message_id）才落
``sent_log``。崩溃窗口语义 = at-least-once（send 已送达、落账前死亡 → 次日
重发一条重复摘要，接受）。

指纹去重 + 防抖：同日同指纹跳过；指纹变化仅当当日存在 submit/reopen 转换
事件才重发（纯标题抖动并入次日班）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import yaml

from fin_analyse.adjudication.inbox import (
    AdjudicationInbox,
    ItemRow,
    inbox_fingerprint,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_ESCALATE_AFTER_DAYS = 3
_TRANSITION_ACTIONS = ("submit", "reopen")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = _PROJECT_ROOT / "config" / "adjudication.yaml"

__all__ = ["PushDisposition", "PushOutcome", "load_digest_config", "render_digest", "push"]


class DigestSender(Protocol):
    def send(self, message: str) -> str | None: ...


class PushDisposition:
    NO_OPEN_ITEMS = "no_open_items"
    ALREADY_SENT = "already_sent"
    DEBOUNCED = "debounced"
    SENT = "sent"
    OUTCOME_UNKNOWN = "outcome_unknown"
    DRY_RUN = "dry_run"


class PushError(RuntimeError):
    """Typed failure: missing/invalid delivery target or config."""


@dataclass(frozen=True, slots=True)
class PushOutcome:
    disposition: str
    fingerprint: str | None = None
    rendered: str | None = None
    detail: str | None = None


def load_digest_config(path: Path | None = None) -> dict:
    """Load digest tuning values; missing file = built-in defaults (silent)."""

    config_path = path or CONFIG_PATH
    payload: dict = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise PushError(f"adjudication_config_invalid: {config_path}")
            payload = loaded
    digest = payload.get("digest") or {}
    if not isinstance(digest, dict):
        raise PushError("adjudication_config_digest_invalid")
    try:
        escalate_after_days = int(digest.get("escalate_after_days", _DEFAULT_ESCALATE_AFTER_DAYS))
    except (TypeError, ValueError) as error:
        raise PushError("adjudication_config_escalate_invalid") from error
    if escalate_after_days < 0:
        raise PushError("adjudication_config_escalate_invalid")
    return {"escalate_after_days": escalate_after_days}


def _shanghai_day(now: datetime) -> str:
    return now.astimezone(_SHANGHAI).date().isoformat()


def _backlog_days(row: ItemRow, now: datetime) -> int:
    opened = datetime.fromtimestamp(row.opened_at, tz=UTC)
    return max(0, int((now - opened).total_seconds() // 86400))


def render_digest(
    items: list[ItemRow],
    *,
    now: datetime,
    escalate_after_days: int,
) -> str:
    """Render the Feishu digest text (metadata only: counts/titles/pointers)."""

    lines = [f"[FIN 裁决收件箱] {len(items)} 项待裁决"]
    for index, row in enumerate(items, start=1):
        backlog = _backlog_days(row, now)
        marker = "⚠ " if backlog > escalate_after_days else ""
        lines.append(
            f"{index}. {marker}[{row.kind}] {row.title}（积压 {backlog} 天）"
        )
        if row.resolution_hint:
            lines.append(f"   裁决：{row.resolution_hint}")
        if row.payload_ref:
            lines.append(f"   详情：{row.payload_ref}")
    return "\n".join(lines)


def push(
    inbox: AdjudicationInbox,
    *,
    sender: DigestSender | None,
    target_environ: dict[str, str] | None = None,
    config_path: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> PushOutcome:
    """One push attempt. Never raises for disposition outcomes; target/config
    problems raise :class:`PushError` before any send."""

    current = now or datetime.now(tz=UTC)
    config = load_digest_config(config_path)
    items = inbox.list_open()
    if not items:
        return PushOutcome(disposition=PushDisposition.NO_OPEN_ITEMS)

    fingerprint = inbox_fingerprint(items)
    day = _shanghai_day(current)
    sent_fingerprints = inbox.sent_day_fingerprints(day)
    if fingerprint in sent_fingerprints:
        return PushOutcome(
            disposition=PushDisposition.ALREADY_SENT, fingerprint=fingerprint
        )
    if sent_fingerprints:
        # 防抖：指纹变化仅当「最近一次发送之后」存在 submit/reopen 转换事件
        # 才重发；首次发送前的转换已包含在上一条摘要里，不解锁重发。
        last_sent_at = inbox.last_sent_at(day) or 0.0
        if not inbox.has_transition_since(
            action_set=_TRANSITION_ACTIONS, since=last_sent_at
        ):
            return PushOutcome(
                disposition=PushDisposition.DEBOUNCED,
                fingerprint=fingerprint,
                detail="最近一次发送后无 submit/reopen 转换事件",
            )

    rendered = render_digest(
        items, now=current, escalate_after_days=config["escalate_after_days"]
    )
    if dry_run:
        return PushOutcome(
            disposition=PushDisposition.DRY_RUN,
            fingerprint=fingerprint,
            rendered=rendered,
        )

    if sender is None:
        raise PushError("adjudication_push_sender_required")
    environ = target_environ if target_environ is not None else {}
    target = environ.get("FIN_DAILY_WORKSPACE_DELIVERY_TARGET")
    if not target:
        raise PushError("adjudication_push_target_missing: FIN_DAILY_WORKSPACE_DELIVERY_TARGET")

    # send 失败/None 一律不写 sent_log（读侧 fail-closed）。
    message_id = sender.send(rendered)
    if message_id is None:
        return PushOutcome(
            disposition=PushDisposition.OUTCOME_UNKNOWN,
            fingerprint=fingerprint,
            detail="平台接受但未返回 message_id",
        )
    inbox.record_sent(day=day, fingerprint=fingerprint)
    return PushOutcome(disposition=PushDisposition.SENT, fingerprint=fingerprint, rendered=rendered)
