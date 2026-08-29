#!/usr/bin/env python3
"""End-of-day read-only reconcile for the FIN Daily Workspace chain.

For one trading day, assert every checkpoint (premarket/morning/close/postmarket)
is accounted for end-to-end and no round was silently dropped or delivered as a
fake. Reads only durable local evidence (never writes, never opens WAL).

Account sources:
  prepare   - daily_workspace_run_ledger rows (new code) and/or outbox/product
              versions (legacy code that did not write the ledger); systemd's
              structured zero-side-effect gate-rejection event
  delivery  - outbox rows (state/message_id/settlement), obligation terminal
              states, runtime-truth dispatch events, WINDOW_MISSED stage records

Exit code:
  0  all four checkpoints accounted, no quality anomalies
  1  at least one anomaly (silent gap / degraded-delivered / unaccounted pending /
     terminal mismatch)
  2  state data unavailable or unusable (report honestly, do not guess)

Anomaly definitions (contract-aligned):
  SILENT_GAP            checkpoint has neither prepare nor delivery evidence
  DEGRADED_DELIVERED    product delivered but agent output unused / zero agent
                        attempt time (the "fake push" the e7521508 fix removes)
  UNACCOUNTED_PENDING   obligation left PENDING without outbox row or window-missed
  TERMINAL_MISMATCH     outbox says DELIVERED but obligation not SETTLED (or reverse)
  SCHEDULED_GATE_REJECTED systemd rejected the scheduled invocation before any
                         business side effect; this is not a SILENT_GAP

Example:
  python scripts/reconcile_daily_workspace_day.py --trading-day 2026-08-11
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

CHECKPOINTS = ("premarket", "morning", "close", "postmarket")
_CALENDAR_PATH = Path(__file__).resolve().parents[1] / "config" / "market" / "a_share_calendar_2026.json"


def _is_trading_day(day: str) -> bool:
    """Confirmed A-share trading day from the frozen calendar artifact.

    Only a confirmed non-trading day suppresses the SILENT_GAP anomaly.  A
    missing artifact, malformed day or a date outside calendar coverage fails
    closed and keeps the legacy behavior (report the gap).
    """

    from fin_analyse.consultation.daily_workspace_schedule import SHANGHAI_TZ
    from fin_analyse.market.trading_calendar import (
        AShareTradingCalendar,
        CalendarArtifactError,
        TradingSessionPhase,
    )

    try:
        calendar = AShareTradingCalendar.from_file(_CALENDAR_PATH)
        value = date.fromisoformat(day)
    except (CalendarArtifactError, ValueError):
        return True
    decision = calendar.session_at(
        datetime.combine(value, time(10, 0), tzinfo=SHANGHAI_TZ)
    )
    return bool(
        decision.data_gaps or decision.phase is not TradingSessionPhase.CLOSED_DAY
    )


def _scheduled_gate_failures(day: str) -> dict[str, list[dict[str, str]]]:
    """Read systemd's structured, zero-side-effect gate-rejection evidence."""
    try:
        start = date.fromisoformat(day)
    except ValueError:
        return {}
    command = [
        "journalctl",
        "--user",
        "--no-pager",
        "--output=json",
        "--since",
        f"{start.isoformat()} 00:00:00",
        "--until",
        f"{(start + timedelta(days=1)).isoformat()} 00:00:00",
    ]
    for checkpoint in CHECKPOINTS:
        command.extend(("--unit", f"fin-daily-workspace-prepare@{checkpoint}.service"))
        command.extend(("--unit", f"fin-daily-workspace-delivery@{checkpoint}.service"))
    try:
        result = subprocess.run(command, capture_output=True, check=False, text=True)
    except OSError:
        return {}
    if result.returncode != 0:
        return {}

    failures: dict[str, list[dict[str, str]]] = {}
    for line in result.stdout.splitlines():
        try:
            record = json.loads(line)
            payload = json.loads(record.get("MESSAGE", ""))
        except (TypeError, json.JSONDecodeError):
            continue
        checkpoint = payload.get("checkpoint")
        phase = payload.get("phase")
        error_code = payload.get("error_code")
        if (
            payload.get("schema_version") != "fin.daily-workspace-scheduled-checkpoint/v1"
            or payload.get("status") != "ERROR"
            or payload.get("trading_day_id") != day
            or payload.get("production_scheduler") is not True
            or payload.get("side_effects_unknown") is not False
            or checkpoint not in CHECKPOINTS
            or phase not in {"prepare", "deliver"}
            or not isinstance(error_code, str)
            or not error_code.startswith("DAILY_WORKSPACE_SCHEDULED_")
        ):
            continue
        failure = {"phase": phase, "error_code": error_code}
        entries = failures.setdefault(checkpoint, [])
        if failure not in entries:
            entries.append(failure)
    return failures


def _canonical_state_root() -> Path:
    import pwd

    canonical_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    return canonical_home / ".local" / "state" / "fin-analyse"


def _readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _run_ledger_evidence(cur: sqlite3.Cursor, day: str, checkpoint: str) -> list[dict[str, Any]]:
    rows = cur.execute(
        "SELECT run_id, trigger, started_at, completed_at, stage_statuses "
        "FROM daily_workspace_run_ledger WHERE trading_day_id=? AND checkpoint=? "
        "ORDER BY started_at",
        (day, checkpoint),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for run_id, trigger, started_at, completed_at, stage_statuses in rows:
        stages: list[dict[str, Any]] = []
        with contextlib.suppress(TypeError, json.JSONDecodeError):
            stages = json.loads(stage_statuses) if stage_statuses else []
        failed = [s.get("stage") for s in stages if str(s.get("status", "")).endswith("FAILED")]
        window_missed = any("WINDOW_MISSED" in json.dumps(s, ensure_ascii=False) for s in stages)
        out.append(
            {
                "run_id": run_id,
                "trigger": trigger,
                "started_at": started_at,
                "completed_at": completed_at,
                "failed_stages": failed,
                "window_missed": window_missed,
            }
        )
    return out


def _product_quality(cur: sqlite3.Cursor, artifact_hash: str) -> dict[str, Any] | None:
    row = cur.execute(
        "SELECT json_extract(product_json,'$.delivery_timing'), "
        "json_extract(product_json,'$.agent_provenance'), "
        "json_extract(product_json,'$.degraded'), "
        "json_extract(product_json,'$.generated_via') FROM products WHERE artifact_hash=? "
        "ORDER BY created_at DESC LIMIT 1",
        (artifact_hash,),
    ).fetchone()
    if not row:
        return None
    timing_s, prov_s, degraded, generated_via = row
    timing = json.loads(timing_s) if timing_s else {}
    prov = json.loads(prov_s) if prov_s else {}
    attempt_seconds = None
    if timing.get("generated_at") and timing.get("prepared_at"):
        try:
            fmt = "%Y-%m-%dT%H:%M:%S.%f%z"
            g = datetime.strptime(timing["generated_at"], fmt)
            p = datetime.strptime(timing["prepared_at"], fmt)
            attempt_seconds = max(0.0, (g - p).total_seconds())
        except ValueError:
            attempt_seconds = None
    return {
        "target_at": timing.get("target_at"),
        "evidence_cutoff_at": timing.get("evidence_cutoff_at"),
        "agent_attempt_seconds": attempt_seconds,
        "runtime_invoked_at_generation": prov.get("runtime_invoked_at_generation"),
        "output_used": prov.get("output_used"),
        "failure_notice": degraded == 1,
        # L1 direct lane runs no agent at all: output_used/attempt_seconds are
        # structurally unused there and must not count as degraded delivery.
        "l1_direct": generated_via == "l1-direct-v1",
    }


def _obligations_for_workspace(cur: sqlite3.Cursor, workspace_ref: str) -> list[dict[str, Any]]:
    rows = cur.execute(
        "SELECT product_version, state, settlement, claimed_at, settled_at "
        "FROM daily_workspace_obligations WHERE workspace_ref=? ORDER BY product_version",
        (workspace_ref,),
    ).fetchall()
    return [
        {
            "product_version": v,
            "state": s,
            "settlement": st,
            "claimed_at": c,
            "settled_at": t,
        }
        for v, s, st, c, t in rows
    ]


def _reconcile_day(
    semantic_db: Path,
    outbox_db: Path,
    ledger_db: Path,
    day: str,
    *,
    scheduled_gate_reader: Callable[[str], dict[str, list[dict[str, str]]]] | None = None,
    is_trading_day: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    if not (semantic_db.exists() and outbox_db.exists() and ledger_db.exists()):
        missing = [str(p) for p in (semantic_db, outbox_db, ledger_db) if not p.exists()]
        return {"ok": False, "unavailable": missing, "checkpoints": {}}

    sem = _readonly(semantic_db)
    obx = _readonly(outbox_db)
    led = _readonly(ledger_db)
    try:
        sem_c = sem.cursor()
        obx_c = obx.cursor()
        led_c = led.cursor()

        outbox_rows = obx_c.execute(
            "SELECT checkpoint, workspace_ref, product_version, artifact_hash, state, "
            "message_id, delivered_at, acceptance_outcome, settlement "
            "FROM daily_workspace_delivery_outbox WHERE trading_day_id=?",
            (day,),
        ).fetchall()
        dispatch_events = led_c.execute(
            "SELECT message_id, observed_at, stage, status FROM public_entry_delivery_events "
            "WHERE observed_at LIKE ? AND stage='dispatched' ORDER BY observed_at",
            (f"{day}T%",),
        ).fetchall()
        scheduled_gate_failures = (
            scheduled_gate_reader(day) if scheduled_gate_reader is not None else {}
        )
        trading_day = is_trading_day(day) if is_trading_day is not None else True

        checkpoints: dict[str, Any] = {}
        anomalies: list[dict[str, Any]] = []
        obligations_all: list[dict[str, Any]] = []
        runs_all: dict[str, list[dict[str, Any]]] = {}
        for checkpoint in CHECKPOINTS:
            day_outbox = [
                {
                    "workspace_ref": r[1],
                    "product_version": r[2],
                    "artifact_hash": r[3],
                    "state": r[4],
                    "message_id": r[5],
                    "delivered_at": r[6],
                    "acceptance_outcome": r[7],
                    "settlement": r[8],
                }
                for r in outbox_rows
                if r[0] == checkpoint
            ]
            runs = _run_ledger_evidence(sem_c, day, checkpoint)
            products = []
            for o in day_outbox:
                quality = _product_quality(sem_c, o["artifact_hash"]) or {}
                products.append({"version": o["product_version"], **quality})
            workspace_refs = {o["workspace_ref"] for o in day_outbox if o["workspace_ref"]}
            obligations: list[dict[str, Any]] = []
            for wr in workspace_refs:
                obligations.extend(_obligations_for_workspace(sem_c, wr))
            obligations_all.extend(obligations)
            runs_all[checkpoint] = runs
            gate_failures = scheduled_gate_failures.get(checkpoint, [])
            # delivery events carry no checkpoint identity; they are day-level
            # corroboration only, never per-checkpoint evidence
            prepare_accounted = (
                bool(runs) or bool(products) or bool(workspace_refs) or bool(gate_failures)
            )
            delivery_accounted = bool(day_outbox) or bool(obligations)
            window_missed = any(r["window_missed"] for r in runs)
            failure_notice_delivered = any(p.get("failure_notice") is True for p in products)
            degraded_delivered = any(
                p.get("failure_notice") is not True
                and not p.get("l1_direct")
                and (p.get("output_used") is False or (p.get("agent_attempt_seconds") == 0.0))
                for p in products
            )

            cp_anomalies: list[str] = []
            if not prepare_accounted and not delivery_accounted and trading_day:
                cp_anomalies.append("SILENT_GAP")
            if gate_failures:
                cp_anomalies.append("SCHEDULED_GATE_REJECTED")
            if degraded_delivered:
                cp_anomalies.append("DEGRADED_DELIVERED")

            anomalies.extend(
                {"checkpoint": checkpoint, "anomalies": cp_anomalies}
                for _ in [None]
                if cp_anomalies
            )

            checkpoints[checkpoint] = {
                "prepare_accounted": prepare_accounted,
                "delivery_accounted": delivery_accounted,
                "silent_gap_suppressed": (
                    not trading_day and not prepare_accounted and not delivery_accounted
                ),
                "run_ledger": runs,
                "products": products,
                "outbox": day_outbox,
                "window_missed": window_missed,
                "failure_notice_delivered": failure_notice_delivered,
                "scheduled_gate_failures": gate_failures,
                "anomalies": cp_anomalies,
            }

        # day-level anomalies: obligations/outbox are workspace-scoped (one workspace
        # spans the whole day), so attribution must not be per-checkpoint
        all_outbox = [
            {"workspace_ref": r[1], "product_version": r[2], "state": r[4], "settlement": r[8]}
            for r in outbox_rows
        ]
        day_anomalies: list[str] = []
        for o in obligations_all:
            version = o["product_version"]
            outbox_match = [x for x in all_outbox if x["product_version"] == version]
            if (
                o["state"] == "PENDING"
                and not outbox_match
                and not any(r["window_missed"] for cp in runs_all.values() for r in cp)
            ):
                day_anomalies.append("UNACCOUNTED_PENDING")
            if outbox_match and o["state"] != "SETTLED":
                day_anomalies.append("TERMINAL_MISMATCH")
        settled_versions = {
            o["product_version"] for o in obligations_all if o["state"] == "SETTLED"
        }
        for x in all_outbox:
            if x["state"] == "DELIVERED" and x["product_version"] not in settled_versions:
                day_anomalies.append("TERMINAL_MISMATCH")
        outbox_message_ids = {
            x.get("message_id") for x in checkpoints.values() for x in x.get("outbox", [])
        }
        event_message_ids = {e[0] for e in dispatch_events}
        if event_message_ids - outbox_message_ids:
            day_anomalies.append("DISPATCH_EVENT_NOT_IN_OUTBOX")
        if outbox_message_ids - event_message_ids:
            day_anomalies.append("OUTBOX_NOT_IN_DISPATCH_EVENTS")
        if day_anomalies:
            anomalies.append({"checkpoint": "*day*", "anomalies": sorted(set(day_anomalies))})
        return {
            "ok": not anomalies,
            "trading_day": day,
            "is_trading_day": trading_day,
            "checkpoints": checkpoints,
            "anomalies": anomalies,
        }
    finally:
        sem.close()
        obx.close()
        led.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trading-day",
        default=datetime.now().astimezone().strftime("%Y-%m-%d"),
        help="trading day to reconcile (default: today, local timezone)",
    )
    root = _canonical_state_root()
    parser.add_argument("--semantic-db", default=root / "semantic-research-v1" / "state.sqlite3")
    parser.add_argument(
        "--outbox-db", default=root / "daily-workspace-delivery-v1" / "outbox.sqlite3"
    )
    parser.add_argument("--ledger-db", default=root / "runtime-truth-v1" / "public-entry.sqlite3")
    args = parser.parse_args(argv)

    report = _reconcile_day(
        Path(args.semantic_db),
        Path(args.outbox_db),
        Path(args.ledger_db),
        args.trading_day,
        scheduled_gate_reader=_scheduled_gate_failures,
        is_trading_day=_is_trading_day,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("unavailable"):
        return 2
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
