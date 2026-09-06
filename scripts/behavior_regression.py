#!/usr/bin/env python3
"""行为回归探针 runner（设计：docs/design/behavior-probe-set-v1.md）。

题集=config/behavior_probes.yaml（判据锚预注册）；腿=finqa_chain --node（节点表
语义，launcher 唯一起法权威）。判定是网不是闸：report 落 $STATE（0700，不入
git、不落问题外用户数据），FAIL/WARN 人工裁决后方可收口。exit 恒 0。

用法：
  behavior_regression.py --plan                     # 干跑：只打印将跑的探针与锚
  behavior_regression.py --profile smoke            # smoke（人格修订收口）
  behavior_regression.py --profile full             # smoke+full（换模型/版本钉）
  behavior_regression.py --profile ondemand         # 全量含追问稳定轴（按需）
  behavior_regression.py --probe <id> [...]         # 单题复跑
  behavior_regression.py --leg commandcode          # 覆盖腿（出闸对照跑生产腿）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_FILE = _PROJECT_ROOT / "config" / "behavior_probes.yaml"
_FINQA_PY = _PROJECT_ROOT / ".venv" / "bin" / "python"
_FINQA_CHAIN = _PROJECT_ROOT / "scripts" / "finqa_chain.py"
_CONSULT_WORKSPACE = Path.home() / "fin-data" / "consult-agent"
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_PROFILE_ORDER = {"smoke": 0, "full": 1, "ondemand": 2}


def _state_dir() -> Path:
    base = (
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        / "fin-analyse"
        / "behavior-regression"
    )
    path = base / datetime.now().strftime("%Y%m%d-%H%M%S")
    path.mkdir(parents=True)
    os.chmod(path, 0o700)
    return path


def _load_probes() -> dict[str, Any]:
    payload = yaml.safe_load(_CONFIG_FILE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "fin.behavior-probes/v1":
        raise SystemExit(f"behavior-probes: malformed config: {_CONFIG_FILE}")
    probes = payload.get("probes")
    if not isinstance(probes, list) or not probes:
        raise SystemExit("behavior-probes: no probes in config")
    return payload


def _select(probes: list[dict], profile: str, only: list[str] | None) -> list[dict]:
    ceiling = _PROFILE_ORDER[profile]
    chosen = [p for p in probes if _PROFILE_ORDER.get(p.get("profile", "full"), 1) <= ceiling]
    if only:
        known = {p["id"] for p in probes}
        unknown = set(only) - known
        if unknown:
            raise SystemExit(f"behavior-probes: unknown probe id(s): {sorted(unknown)}")
        chosen = [p for p in probes if p["id"] in set(only)]
    return chosen


def _gate_skip(probe: dict, today: datetime) -> str | None:
    gate = probe.get("gate") or {}
    weekday = gate.get("weekday")
    if weekday and today.weekday() != _WEEKDAYS[weekday]:
        return f"gate: 仅 {weekday} 出题有效（{gate.get('note', '')}）"
    return None


def _run_question(state: Path, name: str, question: str, *, leg: str, timeout: int, retries: int) -> dict:
    """One headless finqa call; answer → <name>.md, diagnostics → meta return."""

    env = dict(os.environ)
    env.setdefault("FINQA_NODE_TIMEOUT", "900")
    runner = _FINQA_PY if _FINQA_PY.is_file() else Path(sys.executable)
    argv = [str(runner), str(_FINQA_CHAIN), "--node", leg, question]
    started = time.time()
    rc: int | str = "unrun"
    for attempt in range(retries + 1):
        completed = subprocess.run(  # noqa: PLW1510 - rc 显式分流
            argv,
            cwd=str(_CONSULT_WORKSPACE),
            env=env,
            capture_output=True,
            timeout=timeout + 40,
        )
        rc = completed.returncode
        if rc == 0 and completed.stdout.strip():
            (state / f"{name}.md").write_bytes(completed.stdout)
            break
        (state / f"{name}.err").write_bytes(completed.stderr)
        if attempt < retries:
            time.sleep(2)
    else:
        (state / f"{name}.md").write_bytes(b"")
    served = ""
    if completed.stderr:
        tail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        for line in reversed(tail):
            matched = re.search(r"served-by (\S+)", line)
            if matched:
                served = matched.group(1)
                break
    return {"rc": rc, "seconds": round(time.time() - started, 1), "served_by": served or "-"}


def _hit_count(pattern: str, text: str, *, first_line_only: bool) -> int:
    if first_line_only:
        lines = text.splitlines()
        target = lines[0] if lines else ""
    else:
        target = text
    try:
        return len(re.findall(pattern, target))
    except re.error as error:
        raise SystemExit(f"behavior-probes: bad regex {pattern!r}: {error}")


def _evaluate(checks: list[dict], text: str) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for check in checks:
        cid = check["id"]
        kind = check.get("kind", "expect")
        note = (check.get("note") or "").replace("\n", " ")
        if kind == "manual":
            rows.append((cid, "manual", "MANUAL", note))
            continue
        patterns = check.get("regex") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        counts = [
            _hit_count(pattern, text, first_line_only=bool(check.get("first_line_only")))
            for pattern in patterns
        ]
        total = sum(counts)
        if kind == "expect":
            ok = any(count > 0 for count in counts)
        elif kind == "expect_all":
            ok = all(count > 0 for count in counts)
        elif kind == "forbid":
            ok = total == 0
        else:
            raise SystemExit(f"behavior-probes: unknown check kind {kind!r} ({cid})")
        verdict = "PASS" if ok else ("WARN" if check.get("warn_only") else "FAIL")
        detail = f"hits={total}"
        if total:
            shown: list[str] = []
            for pattern in patterns:
                for matched in re.findall(pattern, text):
                    token = matched if isinstance(matched, str) else str(matched)
                    if token not in shown:
                        shown.append(token)
            detail += " sample=" + "|".join(shown[:5])
        if note:
            detail += f"  # {note}"
        rows.append((cid, kind, verdict, detail))
    return rows


def _plan(probes: list[dict], leg: str, today: datetime) -> str:
    lines = [f"plan: {len(probes)} probe(s), leg={leg}, today={today:%Y-%m-%d %a}", ""]
    for probe in probes:
        skip = _gate_skip(probe, today)
        head = f"[{probe['profile']:8}] {probe['id']}  ({probe.get('bug', '')} {probe.get('axis', '')})"
        lines.append(head + ("  → SKIP " + skip if skip else ""))
        lines.append(f"  Q: {probe['question']}")
        if probe.get("followup"):
            lines.append(f"  追问: {probe['followup']['question']}")
        for check in probe.get("checks", []):
            anchor = check.get("regex")
            if isinstance(anchor, list):
                anchor = " / ".join(anchor)
            suffix = "" if check.get("kind") != "manual" else " (人工裁决)"
            lines.append(f"    - {check['id']} [{check.get('kind')}]{suffix} {anchor or ''}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="行为回归探针 runner（题集见 config/behavior_probes.yaml）")
    parser.add_argument("--profile", choices=list(_PROFILE_ORDER), default="full")
    parser.add_argument("--leg", default=None, help="覆盖题集默认腿（如 commandcode 出闸对照）")
    parser.add_argument("--probe", action="append", default=None, help="只跑指定探针 id（可重复）")
    parser.add_argument("--plan", action="store_true", help="干跑：打印题集与锚，不实际出题")
    args = parser.parse_args()

    payload = _load_probes()
    defaults = payload.get("defaults") or {}
    leg = args.leg or defaults.get("leg", "commandcode-flash")
    timeout = int(defaults.get("timeout_seconds", 940))
    retries = int(defaults.get("retries", 1))
    probes = _select(payload["probes"], args.profile, args.probe)
    today = datetime.now()

    if args.plan:
        print(_plan(probes, leg, today))
        return 0

    if not _CONSULT_WORKSPACE.is_dir() or not (_CONSULT_WORKSPACE / "CLAUDE.md").is_file():
        print(f"FATAL: consult-agent 工作区缺失: {_CONSULT_WORKSPACE}", file=sys.stderr)
        return 2

    state = _state_dir()
    (state / "checks.md").write_text(
        "# 判据锚快照（预注册；与 config/behavior_probes.yaml 本轮逐字同源）\n\n"
        + _plan(probes, leg, today),
        encoding="utf-8",
    )
    report: list[str] = [f"behavior-regression {today:%Y-%m-%d %H:%M} leg={leg} profile={args.profile}", ""]
    banner = f"{'probe':28} {'check':24} {'verdict':8} detail"
    report.append(banner)
    report.append("-" * len(banner))
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "MANUAL": 0, "SKIP": 0}

    for probe in probes:
        pid = probe["id"]
        skip = _gate_skip(probe, today)
        if skip:
            counts["SKIP"] += 1
            report.append(f"{pid:28} {'(gate)':24} {'SKIP':8} {skip}")
            (state / f"{pid}.rc").write_text("skip")
            continue
        result = _run_question(state, pid, probe["question"], leg=leg, timeout=timeout, retries=retries)
        (state / f"{pid}.rc").write_text(str(result["rc"]))
        (state / "meta.jsonl").open("a", encoding="utf-8").write(
            json.dumps({"probe": pid, **result, "followup": False}, ensure_ascii=False) + "\n"
        )
        print(f"done {pid} rc={result['rc']} {result['seconds']}s", file=sys.stderr)
        if result["rc"] != 0:
            counts["FAIL"] += 1
            report.append(f"{pid:28} {'(run)':24} {'FAIL':8} rc={result['rc']}（腿失败，非判定 FAIL；见 {pid}.err）")
            continue
        answer = (state / f"{pid}.md").read_text(encoding="utf-8", errors="replace")
        for cid, kind, verdict, detail in _evaluate(probe.get("checks", []), answer):
            counts[verdict] = counts.get(verdict, 0) + 1
            report.append(f"{pid:28} {cid:24} {verdict:8} {detail}")
        followup = probe.get("followup")
        if followup:
            composed = (
                "【上一轮顾问回答】\n" + answer.strip() + "\n\n【我的追问】\n" + followup["question"]
            )
            fname = f"{pid}-followup"
            fresult = _run_question(state, fname, composed, leg=leg, timeout=timeout, retries=retries)
            (state / f"{fname}.rc").write_text(str(fresult["rc"]))
            (state / "meta.jsonl").open("a", encoding="utf-8").write(
                json.dumps({"probe": fname, **fresult, "followup": True}, ensure_ascii=False) + "\n"
            )
            print(f"done {fname} rc={fresult['rc']} {fresult['seconds']}s", file=sys.stderr)
            if fresult["rc"] != 0:
                counts["FAIL"] += 1
                report.append(f"{fname:24} {'(run)':24} {'FAIL':8} rc={fresult['rc']}（腿失败；见 {fname}.err）")
                continue
            fanswer = (state / f"{fname}.md").read_text(encoding="utf-8", errors="replace")
            for cid, kind, verdict, detail in _evaluate(followup.get("checks", []), fanswer):
                counts[verdict] = counts.get(verdict, 0) + 1
                report.append(f"{fname:24} {cid:24} {verdict:8} {detail}")

    report.append("")
    report.append(
        f"RESULT: FAIL={counts['FAIL']} WARN={counts['WARN']} MANUAL={counts['MANUAL']}"
        f" SKIP={counts['SKIP']}（FAIL/WARN/MANUAL 均须人工裁决后方可收口；是网不是闸）"
    )
    report.append(f"台账: {state}")
    text = "\n".join(report)
    (state / "report.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
