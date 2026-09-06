#!/usr/bin/env python3
"""cognition-replay nominate CLI：窗口到期判定 + 口径计算 → owner 扫批提案。

用法：
  cognition_replay_nominate.py --as-of 20260904 [--apply]

只产提案（nominations-<batch>.json + manifest 追加 artifact），不碰标注文档。
relation 闭集五值；窗口未到期 maturity=open、relation 为 null。
stdout = 数据契约 JSON（check_id/group_id，不含黑话词条）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fin_analyse.guo_teacher_research import cognition_replay_lib as lib  # noqa: E402


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _load_snapshot(root: Path, batch: str) -> list[dict]:
    path = root / f"snapshot-{batch}.jsonl"
    if not path.exists():
        raise SystemExit(f"snapshot-{batch}.jsonl missing — run snapshot CLI first")
    lines = [lib.validate_snapshot_line(json.loads(l)) for l in
             path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return lines


def _end_of(spec: dict, as_of: dt.date) -> dt.date:
    end = spec["window"].get("end")
    return dt.date.fromisoformat(end) if end else as_of


def nominate_one(spec: dict, lines: list[dict], as_of: dt.date) -> dict:
    """单规格 → 提案。缺数不编造：no_evidence / unknown 槽位。"""
    start = spec["window"]["start"]
    end = min(_end_of(spec, as_of), as_of).isoformat()
    matured = (
        spec["window"].get("end") is not None
        and dt.date.fromisoformat(spec["window"]["end"]) <= as_of
    )
    n = {
        "check_id": spec["check_id"],
        "unit_id": spec["unit_id"],
        "caliber": spec["caliber"],
        "maturity": "matured" if matured else "open",
        "proposed_relation": None,
        "computed": {"window": {"start": start, "end": end}},
        "members_snapshot": {},
        "evidence_refs": [],
        "note": None,
    }
    cal = spec["caliber"]

    if cal == "no_check" or cal in {"external_facts", "excluded"}:
        n["computed"]["machine_checkable"] = False
        n["proposed_relation"] = "unknown" if cal == "external_facts" else None
        if cal == "external_facts" and not matured:
            n["proposed_relation"] = None
        return n

    if cal == "relative_spread":
        main = spec["mainline_group"]
        bench = spec["benchmark_group"]
        main_g = next((l for l in lines if l["group_id"] == main), None)
        bench_g = next((l for l in lines if l["group_id"] == bench), None)
        # 成员快照内嵌（窗内映射变更=拒绝或双报的载体）。
        n["members_snapshot"] = {
            "mainline": {"group_id": main, "codes": main_g and list(main_g["by_code"])},
            "benchmark": {"group_id": bench, "codes": bench_g and list(bench_g["by_code"])},
        }
        if main_g is None or bench_g is None:
            n["computed"]["reason"] = "snapshot missing for group"
            if matured:
                n["proposed_relation"] = "no_evidence"
            return n
        mr, m_err = lib.basket_return(
            main_g["by_code"], list(main_g["by_code"]), _weights(main_g), start, end)
        br, b_err = lib.basket_return(
            bench_g["by_code"], list(bench_g["by_code"]), _weights(bench_g), start, end)
        computed = {"window": {"start": start, "end": end},
                    "mainline_ret": mr, "benchmark_ret": br}
        if m_err:
            computed["mainline_gap"] = m_err
        if b_err:
            computed["benchmark_gap"] = b_err
        if mr is not None and br is not None:
            spread = mr - br
            computed["spread"] = spread
            if matured:
                n["proposed_relation"] = (
                    "supports" if spread > 0.01
                    else "diverges" if spread < -0.01
                    else "partially_supports"
                )
        n["computed"] = computed
        return n

    if cal == "range_check":
        gid = spec["params"].get("group_id") or spec.get("mainline_group") or spec.get("benchmark_group")
        g = next((l for l in lines if l["group_id"] == gid), None)
        n["members_snapshot"] = {"group_id": gid, "codes": g and list(g["by_code"]) if g else None}
        if g is None:
            n["computed"]["reason"] = "snapshot missing for group"
            if matured:
                n["proposed_relation"] = "no_evidence"
            return n
        code = spec["params"]["code"]
        p = spec["params"]
        res = lib.range_check(
            g["by_code"], code, start, end,
            p.get("lower"), p.get("upper"), p.get("apply_to", "close"),
        )
        n["computed"].update(res)
        if matured and res.get("covered_days"):
            if p.get("floor") is not None:
                ok_floor = res["min_value"] >= p["floor"]
                n["proposed_relation"] = "supports" if ok_floor else "diverges"
            elif p.get("upper") is not None and p.get("lower") is not None:
                n["proposed_relation"] = (
                    "supports" if not res["outside_days"] else "partially_supports"
                )
        return n

    if cal == "window_event":
        gid = spec["params"].get("group_id")
        g = next((l for l in lines if l["group_id"] == gid), None)
        if g is None:
            n["computed"]["reason"] = "snapshot missing for group"
            if matured:
                n["proposed_relation"] = "no_evidence"
            return n
        code = spec["params"]["code"]
        rows = (g["by_code"].get(code) or {})
        metric = spec["params"].get("metric", "")
        if metric == "sse_monthly_close_red":
            anchor = lib.monthly_anchor(rows, spec["params"]["month"])
            n["computed"]["monthly_anchor"] = anchor
            if matured and anchor:
                red = anchor["close"] > anchor["open"]
                up_vs_prev = (anchor["monthly_ret"] or 0) > 0
                n["proposed_relation"] = (
                    "supports" if (red and up_vs_prev) else "diverges"
                )
                n["evidence_refs"].append(
                    f"monthly {spec['params']['month']}: close={anchor['close']:.2f} "
                    f"open={anchor['open']:.2f} ret={(anchor['monthly_ret'] or 0) * 100:+.2f}%"
                )
        else:
            n["computed"]["machine_checkable"] = False
            if matured:
                n["proposed_relation"] = "unknown"
        return n

    if cal == "same_day":
        gid = spec["params"].get("group_id")
        g = next((l for l in lines if l["group_id"] == gid), None)
        if g is None:
            n["computed"]["reason"] = "snapshot missing for group"
            return n
        code = spec["params"]["code"]
        rows = g["by_code"].get(code) or {}
        pub_day = start  # same_day 的 window.start = 发文日
        last_closed = lib.last_closed_trading_day(rows, pub_day)
        n["computed"]["last_closed_trading_day"] = last_closed
        n["computed"]["last_closed_close"] = rows.get(last_closed, {}).get("close")
        pub_at = spec["params"].get("published_at", "")
        if lib.same_day_close_available(pub_at, pub_day):
            n["computed"]["same_day_close"] = rows.get(pub_day, {}).get("close")
            n["computed"]["pit_cap"] = False
        else:
            n["computed"]["pit_cap"] = True
            n["note"] = "发文早于收盘：当日收盘只能事后对照，relation 上限 partially_supports"
        return n

    raise SystemExit(f"unhandled caliber: {cal}")


def _weights(group_line: dict) -> list[float]:
    n = len(group_line["by_code"])
    return [1.0 / n] * n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", required=True, help="YYYYMMDD")
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认 dry-run）")
    args = ap.parse_args()

    as_of = dt.datetime.strptime(args.as_of, "%Y%m%d").date()
    batch = as_of.strftime("%Y%m%d")
    root = lib.resolve_state_root()

    specs = lib.validate_specs(
        json.loads((root / "specs.v1.json").read_text(encoding="utf-8"))
    )
    lines = _load_snapshot(root, batch)

    nominations = [nominate_one(s, lines, as_of) for s in specs["specs"]]
    payload = {
        "schema_version": "fin.cognition-replay-nomination/v1",
        "batch": batch,
        "as_of": as_of.isoformat(),
        "nominations": nominations,
    }
    lib.validate_nomination(payload)

    if not args.apply:
        print(json.dumps({"mode": "dry-run", "batch": batch,
                          "nominations": [
                              {k: n[k] for k in ("check_id", "unit_id", "caliber",
                                                 "maturity", "proposed_relation")}
                              for n in nominations]},
                         ensure_ascii=False))
        return 0

    nom_path = root / f"nominations-{batch}.json"
    lib.atomic_write_json(nom_path, payload)

    manifest_path = root / f"manifest-{batch}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {
        "batch": batch, "as_of": as_of.isoformat(), "artifacts": [],
    }
    manifest["artifacts"] = [a for a in manifest.get("artifacts", [])
                             if a.get("file") != nom_path.name]
    manifest["artifacts"].append({
        "file": nom_path.name, "sha256": lib.sha256_file(nom_path),
        "bytes": nom_path.stat().st_size, "kind": "nominations",
        "writer": "cognition_replay_nominate",
    })
    lib.atomic_write_json(manifest_path, manifest)

    print(json.dumps({"mode": "apply", "batch": batch,
                      "nominations": nom_path.name,
                      "summary": [
                          {k: n[k] for k in ("check_id", "maturity", "proposed_relation")}
                          for n in nominations]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
