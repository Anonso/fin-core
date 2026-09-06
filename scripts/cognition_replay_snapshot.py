#!/usr/bin/env python3
"""cognition-replay snapshot CLI：行情事实快照（批=文件，整文件原子重写）。

用法：
  cognition_replay_snapshot.py --as-of 20260904 [--apply] [--no-proxy]

默认 dry-run（打印将写入的摘要，不落盘）；--apply 才写。
stdout = 数据契约 JSON（group_id，不含黑话词条）；诊断/错误 → stderr。
幂等：同批重跑内容等价（retrieved_at 除外）；写前读旧做内容比较，等价即跳过。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fin_analyse.guo_teacher_research import cognition_replay_lib as lib  # noqa: E402
from fin_analyse.market.providers.akshare import AKShareProvider  # noqa: E402

JARGON_PATH = REPO_ROOT / "config" / "zsxq_jargon.json"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", required=True, help="YYYYMMDD")
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认 dry-run）")
    ap.add_argument("--no-proxy", action="store_true",
                    help="显式脱代理（运行旋钮；默认透传环境 proxy）")
    ap.add_argument("--lookback-days", type=int, default=40,
                    help="窗口起点向前取数缓冲（自然日，默认 40）")
    ap.add_argument("--require-today", action="store_true",
                    help="as_of 当日在任一组序列中无收盘行（节假日/数据未就绪）则跳过不写——供每日定时调用")
    args = ap.parse_args()

    as_of = dt.datetime.strptime(args.as_of, "%Y%m%d").date()
    batch = as_of.strftime("%Y%m%d")
    root = lib.resolve_state_root()

    jargon = json.loads(JARGON_PATH.read_text(encoding="utf-8"))
    jargon_sha = lib.canonical_sha256(jargon)
    mapping = lib.validate_mapping(
        json.loads((root / "mapping.v1.json").read_text(encoding="utf-8")), jargon_sha
    )
    specs = lib.validate_specs(
        json.loads((root / "specs.v1.json").read_text(encoding="utf-8"))
    )

    # 取数窗 = 所有 active 规格的最早 start − lookback 缓冲，到 as_of。
    starts = [
        dt.date.fromisoformat(s["window"]["start"])
        for s in specs["specs"] if s["status"] == "active"
    ]
    fetch_start = (min(starts) - dt.timedelta(days=args.lookback_days)).isoformat() if starts \
        else (as_of - dt.timedelta(days=args.lookback_days)).isoformat()
    fetch_end = as_of.isoformat()

    if args.no_proxy:
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                    "all_proxy", "ALL_PROXY"):
            os.environ.pop(var, None)

    provider = AKShareProvider()
    lines: list[dict] = []
    gaps: list[dict] = []
    fetched_at = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")

    # group_id → 合并其下所有词条所属组（一组取一次数）。
    for gid, group in mapping["groups"].items():
        by_code: dict[str, dict[str, dict]] = {}
        for code in group["codes"]:
            rows = provider.get_index_history(code, fetch_start, fetch_end)
            if not rows:
                gaps.append({"group_id": gid, "code": code,
                             "gap": "index_history_empty"})
                continue
            by_code[code] = {
                r.date: {"open": r.open, "high": r.high, "low": r.low,
                         "close": r.close, "volume": r.volume}
                for r in rows
            }
        if not by_code:
            continue
        line = {
            "schema_version": "fin.cognition-replay-snapshot/v1",
            "batch": batch,
            "as_of": fetch_end,
            "group_id": gid,
            "role": group["role"],
            "adjustment_basis": "index_raw",
            "by_code": by_code,
            "source": "akshare:stock_zh_index_daily via AKShareProvider.get_index_history",
            "retrieved_at": fetched_at,
            "note": None,
        }
        lib.validate_snapshot_line(line)
        lines.append(line)

    manifest = {
        "batch": batch,
        "as_of": fetch_end,
        "artifacts": [],
        "specs_sha256": lib.canonical_sha256(specs),
        "mapping_sha256": lib.canonical_sha256(mapping),
        "jargon_source_sha256": jargon_sha,
        "gaps": gaps,
        "written_at": fetched_at,
    }

    # 取数全败（网络/代理/源故障）≠ 节假日：硬错误退出，防真实交易日被静默跳过。
    if not lines:
        print(json.dumps({
            "mode": "error", "batch": batch, "gaps": gaps,
            "reason": "all groups failed to fetch (network/proxy/source) — not a holiday skip",
        }, ensure_ascii=False))
        _log("all groups failed to fetch; rc=1")
        return 1

    if args.require_today and not any(fetch_end in l["by_code"] for l in lines):
        print(json.dumps({
            "mode": "skip", "batch": batch,
            "reason": f"no close row dated {fetch_end} in any group (节假日/数据未就绪)",
        }, ensure_ascii=False))
        _log(f"require-today: skip batch {batch}")
        return 0

    if not args.apply:
        print(json.dumps({
            "mode": "dry-run", "batch": batch, "groups": [l["group_id"] for l in lines],
            "gaps": gaps,
            "per_group_days": {
                l["group_id"]: {c: len(v) for c, v in l["by_code"].items()}
                for l in lines
            },
        }, ensure_ascii=False))
        return 0

    snapshot_path = root / f"snapshot-{batch}.jsonl"
    manifest_path = root / f"manifest-{batch}.json"

    payload = "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines)

    def _content_key(payload_text: str) -> str:
        """内容等价键：逐行剔 retrieved_at 后的规范化 JSON（幂等口径）。"""
        stripped = []
        for line in payload_text.splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            obj.pop("retrieved_at", None)
            stripped.append(json.dumps(obj, ensure_ascii=False, sort_keys=True))
        return "\n".join(stripped)

    new_key = _content_key(payload)
    if snapshot_path.exists() and _content_key(snapshot_path.read_text(encoding="utf-8")) == new_key:
        _log(f"snapshot-{batch} content-equivalent, skip rewrite")
    else:
        lib.atomic_write_text(snapshot_path, payload)

    manifest["artifacts"].append({
        "file": snapshot_path.name, "sha256": lib.sha256_file(snapshot_path),
        "bytes": snapshot_path.stat().st_size, "kind": "snapshot",
    })
    # 幂等：manifest 自身内容等价（剔 written_at）也跳过重写。
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        old.pop("written_at", None)
        new = json.loads(json.dumps(manifest))
        new.pop("written_at", None)
        if old == new:
            _log(f"manifest-{batch} content-equivalent, skip rewrite")
            manifest = old
        else:
            lib.atomic_write_json(manifest_path, manifest)
    else:
        lib.atomic_write_json(manifest_path, manifest)

    print(json.dumps({
        "mode": "apply", "batch": batch,
        "snapshot": snapshot_path.name, "manifest": manifest_path.name,
        "groups": [l["group_id"] for l in lines], "gaps": gaps,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
