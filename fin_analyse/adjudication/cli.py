"""fin-adjudication CLI：list / show / done / add / push（owner 侧唯一入口）。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from fin_analyse.adjudication.inbox import (
    AdjudicationInboxError,
    open_default_inbox,
)
from fin_analyse.adjudication.push import (
    PushError,
    push,
)

_EXIT_OK = 0
_EXIT_ERROR = 1


def _fmt_backlog(opened_at: float, now_ts: float) -> int:
    return max(0, int((now_ts - opened_at) // 86400))


def _cmd_list(args: argparse.Namespace) -> int:
    inbox = open_default_inbox()
    items = inbox.list_open()
    if not items:
        print("（无待裁决项）")
        return _EXIT_OK
    now_ts = max(item.last_seen_at for item in items)
    for row in items:
        backlog = _fmt_backlog(row.opened_at, now_ts)
        print(
            f"{row.item_id}\t{row.kind}\t积压 {backlog} 天\t{row.title}"
        )
        if row.resolution_hint:
            print(f"    裁决：{row.resolution_hint}")
        if row.payload_ref:
            print(f"    详情：{row.payload_ref}")
    return _EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    inbox = open_default_inbox()
    row = inbox.get(args.item_id)
    if row is None:
        print(f"未找到：{args.item_id}", file=sys.stderr)
        return _EXIT_ERROR
    for key in (
        "item_id",
        "kind",
        "title",
        "payload_ref",
        "resolution_hint",
        "status",
        "opened_at",
        "last_seen_at",
        "resolved_at",
        "resolve_source",
        "note",
        "reopen_count",
    ):
        print(f"{key}: {getattr(row, key)}")
    return _EXIT_OK


def _cmd_done(args: argparse.Namespace) -> int:
    inbox = open_default_inbox()
    inbox.resolve(args.item_id, source="owner", note=args.note)
    print(f"已裁决：{args.item_id}")
    return _EXIT_OK


def _cmd_add(args: argparse.Namespace) -> int:
    inbox = open_default_inbox()
    item_id = inbox.add_manual(
        title=args.title,
        resolution_hint=args.hint,
        payload_ref=args.ref,
        item_id=args.id,
    )
    print(f"已登记：{item_id}")
    return _EXIT_OK


def _cmd_push(args: argparse.Namespace) -> int:
    if args.dry_run:
        inbox = open_default_inbox()
        outcome = push(inbox, sender=None, dry_run=True)
        if outcome.rendered is not None:
            print(outcome.rendered)
        print(f"disposition: {outcome.disposition}", file=sys.stderr)
        return _EXIT_OK
    from fin_analyse.operations.daily_workspace_delivery import HermesCliMessageSender

    inbox = open_default_inbox()
    outcome = push(
        inbox,
        sender=HermesCliMessageSender(
            target=os.environ["FIN_DAILY_WORKSPACE_DELIVERY_TARGET"]
        ),
        target_environ=dict(os.environ),
    )
    print(f"disposition: {outcome.disposition}")
    if outcome.detail:
        print(f"detail: {outcome.detail}")
    return _EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fin-adjudication",
        description="裁决收件箱：列出/裁决/登记/推送待 owner 裁决项",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="列出全部 open 项（按积压时间升序）")

    show = subparsers.add_parser("show", help="查看单项完整字段")
    show.add_argument("item_id")

    done = subparsers.add_parser("done", help="记录裁决（关闭一项）")
    done.add_argument("item_id")
    done.add_argument("--note", default=None, help="裁决备注（一句话）")

    add = subparsers.add_parser("add", help="手动登记一项（NOW/BUGS 长生命周期项等）")
    add.add_argument("--title", required=True)
    add.add_argument("--hint", default=None, help="怎么裁决（命令/入口一句话）")
    add.add_argument("--ref", default=None, help="详情指针（路径/锚）")
    add.add_argument("--id", default=None, help="显式 item_id（默认按标题生成 manual:<slug>）")

    push_parser = subparsers.add_parser("push", help="推送当日摘要（timer 调用；--dry-run 只渲染）")
    push_parser.add_argument("--dry-run", action="store_true", help="只渲染不发送不落账")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers = {
        "list": _cmd_list,
        "show": _cmd_show,
        "done": _cmd_done,
        "add": _cmd_add,
        "push": _cmd_push,
    }
    try:
        return handlers[args.command](args)
    except AdjudicationInboxError as error:
        print(f"fin-adjudication: {error}", file=sys.stderr)
        return _EXIT_ERROR
    except PushError as error:
        print(f"fin-adjudication: {error}", file=sys.stderr)
        return _EXIT_ERROR
    except KeyError as error:
        print(
            f"fin-adjudication: 缺少环境变量 {error}（unit 侧 EnvironmentFile 提供）",
            file=sys.stderr,
        )
        return _EXIT_ERROR
    except Exception as error:  # noqa: BLE001 - CLI 边界：journal 里一行带类型，不带 traceback
        print(f"fin-adjudication: {type(error).__name__}: {error}", file=sys.stderr)
        return _EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
