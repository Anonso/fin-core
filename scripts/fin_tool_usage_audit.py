#!/usr/bin/env python3
"""Read-only FIN/Hermes tool-usage audit.

Answers "which tools did the Agents actually use, and which were granted but
never used", from the three existing traces:

1. FIN semantic state ``capability_trace`` (authoritative consumed FIN reads);
2. codex rollout files (raw tool calls, including non-FIN tools);
3. Hermes profile ``messages`` (outer Agent tool rows and calls).

Never writes state.  ``--json`` prints one machine-readable document.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

STATE_DB = Path("/home/ypk/.local/state/fin-analyse/semantic-research-v1/state.sqlite3")
HERMES_STATE_DB = Path("/home/ypk/.hermes/profiles/fin/state.db")
CODEX_SESSIONS_ROOT = Path("/home/ypk/fin-data/codex-routes/codex-proxy-a/sessions")

GRANTED_CONSULTATION_CAPABILITIES = frozenset(
    {
        "fin.read_actual_portfolio",
        "fin.read_g_context",
        "fin.read_teacher_cognition",
        "fin.read_market_overview",
        "fin.read_market_snapshot",
        "fin.read_margin_evidence",
        "fin.read_external_evidence",
    }
)


def _since_epoch(days: int) -> float:
    return (datetime.now(UTC) - timedelta(days=days)).timestamp()


def audit_fin_trace(since: float) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT cv.created_at, cv.input_json, cv.payload_json FROM chain_versions cv "
        "WHERE cv.kind='answer' AND cv.created_at >= ? ORDER BY cv.created_at",
        (since,),
    ).fetchall()
    con.close()
    by_capability: Counter[str] = Counter()
    empty_trace: list[dict[str, Any]] = []
    consumed_sessions = 0
    for created, input_json, payload_json in rows:
        try:
            prov = json.loads(payload_json).get("response_projection", {}).get("provenance", {})
            trace: list[str] = []
            for item in prov.get("capability_trace", []):
                capability = item.get("capability") if isinstance(item, dict) else None
                if isinstance(capability, str):
                    trace.append(capability)
        except Exception:
            trace = []
        if trace:
            by_capability.update(trace)
            consumed_sessions += 1
        else:
            question = ""
            with suppress(Exception):
                question = json.loads(input_json).get("question", "")[:60]
            empty_trace.append({"created_at": created, "question": question})
    return {
        "products": len(rows),
        "consumed_sessions": consumed_sessions,
        "empty_trace_count": len(empty_trace),
        "by_capability": dict(by_capability.most_common()),
        "empty_trace": empty_trace,
    }


def audit_rollouts(since: float) -> dict[str, Any]:
    mcp_tools: Counter[str] = Counter()
    other_tools: Counter[str] = Counter()
    sessions_total = 0
    sessions_with_tools = 0
    for f in sorted(CODEX_SESSIONS_ROOT.glob("*/*/*/rollout-*.jsonl")):
        if f.stat().st_mtime < since:
            continue
        with suppress(Exception):
            lines = f.read_text(errors="replace").splitlines()
        if not any("USER QUESTION" in line for line in lines):
            continue
        sessions_total += 1
        has_tools = False
        for line in lines:
            if '"McpToolCall"' in line:
                match = re.search(r'"tool":"(fin\.[^"]+)"', line)
                if match:
                    mcp_tools[match.group(1)] += 1
                    has_tools = True
            elif '"custom_tool_call"' in line:
                match = re.search(r'"name":"([^"]+)"', line)
                if match and match.group(1) != "mcpToolCall":
                    other_tools[match.group(1)] += 1
                    has_tools = True
        if has_tools:
            sessions_with_tools += 1
    return {
        "sessions": sessions_total,
        "sessions_with_tools": sessions_with_tools,
        "sessions_zero_tool": sessions_total - sessions_with_tools,
        "mcp_tools": dict(mcp_tools.most_common()),
        "other_tools": dict(other_tools.most_common()),
    }


def audit_hermes(since: float) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{HERMES_STATE_DB}?mode=ro", uri=True)
    tool_rows: Counter[str] = Counter()
    tool_calls: Counter[str] = Counter()
    for tool_name, tool_calls_json in con.execute(
        "SELECT tool_name, tool_calls FROM messages WHERE timestamp >= ?",
        (since,),
    ):
        if isinstance(tool_name, str) and tool_name:
            tool_rows[tool_name] += 1
        if isinstance(tool_calls_json, str) and tool_calls_json:
            try:
                calls = json.loads(tool_calls_json)
            except Exception:
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                name = function.get("name") if isinstance(function, dict) else None
                if isinstance(name, str):
                    tool_calls[name] += 1
    con.close()
    return {
        "tool_rows": dict(tool_rows.most_common()),
        "tool_calls": dict(tool_calls.most_common()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3, help="audit window in days")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    arguments = parser.parse_args()

    since = _since_epoch(max(1, arguments.days))
    fin = audit_fin_trace(since)
    rollouts = audit_rollouts(since)
    hermes = audit_hermes(since)
    used = set(fin["by_capability"])
    never_used = sorted(GRANTED_CONSULTATION_CAPABILITIES - used)
    report = {
        "schema": "fin.tool-usage-audit/v1",
        "window_days": arguments.days,
        "granted_consultation_capabilities": sorted(GRANTED_CONSULTATION_CAPABILITIES),
        "never_used_granted": never_used,
        "fin": fin,
        "codex_rollouts": rollouts,
        "hermes": hermes,
    }

    if arguments.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print(f"FIN/Hermes 工具使用审计（近 {arguments.days} 天）")
    print("-" * 64)
    print(
        f"[FIN] answer 产品 {fin['products']}，消费能力会话 {fin['consumed_sessions']}，"
        f"空 capability_trace {fin['empty_trace_count']}"
    )
    for name, count in fin["by_capability"].items():
        print(f"  {name}: {count}")
    print(f"[codex rollout] 咨询会话 {rollouts['sessions']}，有工具 {rollouts['sessions_with_tools']}，"
          f"零工具 {rollouts['sessions_zero_tool']}")
    for name, count in rollouts["mcp_tools"].items():
        print(f"  MCP {name}: {count}")
    for name, count in rollouts["other_tools"].items():
        print(f"  其它 {name}: {count}")
    print("[Hermes 外层 Agent] 高频工具调用：")
    for name, count in list(hermes["tool_calls"].items())[:10]:
        print(f"  {name}: {count}")
    print(f"[缺口] 授权但从未消费: {never_used or '无'}")


if __name__ == "__main__":
    main()
