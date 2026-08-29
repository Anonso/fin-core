#!/usr/bin/env python3
"""B4: operator alert for STARTED daily-workspace runs (B2 ledger as fact source).

告警只读 B2 run ledger 投影：当日有 run 行时，按设计 v2 规则判定并发送至多
一条告警；空行 = no_alert（未启动的 run 不伪称可发现）。发送事实经
public-entry ledger 以 `daily_workspace_alert` attempt 落账（全部终态，
OUTCOME_UNKNOWN 不自动重发）。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from fin_analyse.operations.daily_workspace_delivery import (
    DispatchAcceptanceOutcome,
    HermesCliMessageSender,
)
from fin_analyse.runtime.state_roots import semantic_research_state_root

_SCHEMA = "fin.daily-workspace-alert/v1"


def _canonical_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("trade date must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("trade date must be canonical YYYY-MM-DD")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=_canonical_date)
    parser.add_argument(
        "--delivery-target",
        required=True,
        help="feishu:... 告警发送目标（B4 必需）",
    )
    return parser


def _read_owner_secret(path: Path) -> bytes:
    metadata = path.lstat()
    if not metadata.st_uid == __import__("os").geteuid() or metadata.st_mode & 0o077:
        raise RuntimeError("daily_workspace_state_owner_secret_insecure")
    return path.read_bytes()


def _require_secure_state_root(state_root: Path) -> None:
    metadata = state_root.lstat()
    if (
        not metadata.st_uid == __import__("os").geteuid()
        or metadata.st_mode & 0o077
    ):
        raise RuntimeError("daily_workspace_state_root_insecure")


def _build_alert_message(
    *,
    trading_day_id: str,
    run_id: str,
    alert_kind: str,
    reason: str,
    freshness: str | None,
) -> str:
    """告警文本：结论优先、事实逐字、不夸大（FIN 展示风格）。"""
    lines = [
        "【FIN 每日链告警】",
        f"交易日：{trading_day_id}",
        f"run：{run_id}",
        f"类型：{alert_kind}",
    ]
    if reason:
        lines.append(f"原因：{reason}")
    if freshness is not None:
        lines.append(f"G freshness：{freshness}")
    return "\n".join(lines)


def evaluate_alert(
    *,
    trading_day_id: str,
    latest_row: Any | None,
    freshness: str | None,
) -> dict[str, str] | None:
    """B4 判定规则（design v2 + round-1 处置）：空行 = no_alert；有行按
    失败 stage（含逐字 detail）/WINDOW_MISSED/stale/unknown/不可信 freshness
    优先级返回至多一条告警事实。"""

    if latest_row is None:
        return None
    run_id = str(latest_row["run_id"])
    try:
        stages = json.loads(str(latest_row["stage_statuses"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        stages = []
    reason_parts: list[str] = []
    for stage in stages if isinstance(stages, list) else []:
        if not isinstance(stage, dict):
            continue
        status = str(stage.get("status", ""))
        if status in {"COLLECT_FAILED", "PREPARE_FAILED", "DELIVER_FAILED"}:
            detail = str(stage.get("detail", "") or "")
            part = f"{stage.get('stage', '?')}={status}"
            if detail:
                part += f"({detail})"
            reason_parts.append(part)
        elif status == "WINDOW_MISSED":
            reason_parts.append(f"{stage.get('stage', '?')}=WINDOW_MISSED")
    if reason_parts:
        return {
            "run_id": run_id,
            "alert_kind": "run_stage_failure",
            "reason": ";".join(reason_parts),
        }
    if freshness == "STALE":
        return {
            "run_id": run_id,
            "alert_kind": "g_freshness_stale",
            "reason": "G 已过期",
        }
    if freshness == "UNKNOWN":
        # B2 合法 freshness 值：已有真实采集但 freshness unknown——不得静默漏报。
        return {
            "run_id": run_id,
            "alert_kind": "g_freshness_unknown",
            "reason": "G freshness 未知",
        }
    if freshness is None:
        # 有行但无可信新采集（含失败 run）——区别于空行 no_alert。
        return {
            "run_id": run_id,
            "alert_kind": "g_freshness_untrusted",
            "reason": "G 无可信新采集",
        }
    return None


def build_production_alert_composition(
    *,
    delivery_target: str,
) -> Callable[[str], tuple[Any | None, str | None]]:
    """Compose through FIN-owned state/interfaces（ledger 读 + sender +
    acceptance）。"""

    from fin_analyse.guo_teacher_research.semantic_state import ResearchStateRepository

    state_root = semantic_research_state_root()
    _require_secure_state_root(state_root)
    repository = ResearchStateRepository(
        state_root / "state.sqlite3",
        token_secret=_read_owner_secret(state_root / "continuation-token-secret.bin"),
    )
    # 缩小后（review_exhausted）：组合只提供 ledger 读；发送由 main 的
    # HermesCliMessageSender 直接承担；发送事实的 durable 落账为后续 slice。
    def read_snapshot(trading_day_id: str) -> tuple[Any | None, str | None]:
        # B4-R2: stage 与 freshness 来自同一只读快照，避免跨 run 混配。
        snapshot = repository.latest_run_ledger_snapshot(trading_day_id)
        if snapshot is None:
            return None, None
        return snapshot[0], snapshot[1]

    return read_snapshot


def main(
    argv: list[str] | None = None,
    *,
    snapshot_reader: Callable[[str], tuple[Any | None, str | None]] | None = None,
    sender: Callable[[str], str | None] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    trading_day_id = args.trade_date.isoformat()
    if snapshot_reader is None or sender is None:
        read_snapshot = build_production_alert_composition(
            delivery_target=args.delivery_target
        )
        snapshot_reader = read_snapshot
        sender = HermesCliMessageSender(target=args.delivery_target).send

    try:
        latest_row, freshness = snapshot_reader(trading_day_id)
    except Exception as error:
        print(
            json.dumps(
                {
                    "schema_version": _SCHEMA,
                    "trading_day_id": trading_day_id,
                    "alert": "failed",
                    "reason": _narrow_reason(error),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 3

    alert = evaluate_alert(
        trading_day_id=trading_day_id,
        latest_row=latest_row,
        freshness=freshness,
    )
    if alert is None:
        print(
            json.dumps(
                {
                    "schema_version": _SCHEMA,
                    "trading_day_id": trading_day_id,
                    "alert": "no_alert",
                    "freshness": freshness,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    message = _build_alert_message(
        trading_day_id=trading_day_id,
        run_id=alert["run_id"],
        alert_kind=alert["alert_kind"],
        reason=alert["reason"],
        freshness=freshness,
    )
    outcome = DispatchAcceptanceOutcome.SUCCEEDED
    message_id: str | None = None
    try:
        message_id = sender(message)
    except Exception:
        outcome = DispatchAcceptanceOutcome.OUTCOME_UNKNOWN
    if message_id is None:
        outcome = DispatchAcceptanceOutcome.OUTCOME_UNKNOWN
    # B4-R3: OUTCOME_UNKNOWN 不得表述为 sent——如实 "attempted"。
    alert_state = "sent" if outcome is DispatchAcceptanceOutcome.SUCCEEDED else "attempted"
    print(
        json.dumps(
            {
                "schema_version": _SCHEMA,
                "trading_day_id": trading_day_id,
                "alert": alert_state,
                "alert_kind": alert["alert_kind"],
                "run_id": alert["run_id"],
                "reason": alert["reason"],
                "send_outcome": outcome.value,
                "message_id": message_id,
                "freshness": freshness,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if outcome is DispatchAcceptanceOutcome.SUCCEEDED else 2


def _narrow_reason(error: Exception) -> str:
    code = getattr(error, "code", None)
    return str(code) if isinstance(code, str) and code else "alert_failed"


if __name__ == "__main__":
    raise SystemExit(main())
