"""cognition-replay-facts 共享库：schema 闭集校验、原子写、口径计算。

设计稿 docs/design/cognition-replay-facts.md（设计门 18 发现全采纳后的 v2.1）。
数据根 $STATE/fin-analyse/cognition-replay-evidence/（0700/0600）。

闭集（fail-closed，任一未知值拒绝整份文件）：
- relation 五值 = readmodel:57 机器闭集 {supports, partially_supports, diverges,
  unknown, no_evidence}；窗口未到期不是 relation——nomination 用 maturity 字段
  表达（matured|open），relation 只在 matured 时给出。
- claim_type/caliber/status/role/confidence 各自闭集，见下方 frozenset。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fin_analyse.runtime.state_roots import (
    ensure_private_state_directory,
    write_private_state_text,
)

RELATION_CLOSED = frozenset(
    {"supports", "partially_supports", "diverges", "unknown", "no_evidence"}
)
CLAIM_TYPE_CLOSED = frozenset(
    {
        "current_observation",
        "forecast",
        "scenario",
        "structural_analysis",
        "historical_analysis",
        "action_layer",
        "mixed_published_report",
    }
)
CALIBER_CLOSED = frozenset(
    {
        "same_day",
        "window_event",
        "relative_spread",
        "range_check",
        "no_check",
        "external_facts",
        "excluded",
    }
)
SPEC_STATUS_CLOSED = frozenset({"active", "unscoreable", "retired"})
ROLE_CLOSED = frozenset({"mainline", "benchmark", "reference"})
CONFIDENCE_CLOSED = frozenset(
    {"owner_confirmed", "corpus_inferred", "speculative"}
)
MATURITY_CLOSED = frozenset({"matured", "open"})

#: claim_type → caliber 派生表（设计 §2.2）；specs 里的 caliber 必须与之一致
#: 或属该 claim_type 的合法集，防规格漂移。
_CLAIM_CALIBER: dict[str, frozenset[str]] = {
    "current_observation": frozenset({"same_day"}),
    "forecast": frozenset({"window_event", "relative_spread", "range_check"}),
    "scenario": frozenset({"range_check", "window_event"}),
    "structural_analysis": frozenset(
        {"relative_spread", "external_facts", "range_check"}
    ),
    "historical_analysis": frozenset({"external_facts", "no_check"}),
    "action_layer": frozenset({"no_check"}),
    "mixed_published_report": frozenset({"excluded"}),
}

SNAPSHOT_LINE_KEYS = frozenset(
    {
        "schema_version",
        "batch",
        "as_of",
        "group_id",
        "role",
        "adjustment_basis",
        "by_code",
        "source",
        "retrieved_at",
        "note",
    }
)
SERIES_KEYS = frozenset({"open", "high", "low", "close", "volume"})


class ReplaySchemaError(ValueError):
    """schema/闭集违规；消息不含黑话正文（只含字段与值类型）。"""


def _require(d: dict, key: str, types: tuple[type, ...]) -> Any:
    if key not in d:
        raise ReplaySchemaError(f"missing key: {key}")
    if not isinstance(d[key], types):
        raise ReplaySchemaError(f"bad type for {key}")
    return d[key]


def _closed(value: Any, closed: frozenset, what: str) -> str:
    if value not in closed:
        raise ReplaySchemaError(f"{what} not in closed set: {value!r}")
    return str(value)


def validate_mapping(d: dict, jargon_sha: str) -> dict:
    """mapping.v1.json 校验；词条语义必须与 zsxq_jargon.json 一致（防漂移）。"""
    _closed(_require(d, "schema_version", (str,)), {"fin.cognition-replay-mapping/v1"}, "schema_version")
    _require(d, "updated_at", (str,))
    _require(d, "jargon_source_sha256", (str,))
    if d["jargon_source_sha256"] != jargon_sha:
        raise ReplaySchemaError("jargon_source_sha256 mismatch (zsxq_jargon.json drifted; re-import)")
    entries = _require(d, "entries", (dict,))
    groups = _require(d, "groups", (dict,))
    if not entries or not groups:
        raise ReplaySchemaError("empty entries/groups")
    for term, e in entries.items():
        _closed(_require(e, "group_id", (str,)), set(groups), "entry.group_id")
        _closed(_require(e, "confidence", (str,)), CONFIDENCE_CLOSED, "entry.confidence")
        _require(e, "meaning", (str,))
    for gid, g in groups.items():
        codes = _require(g, "codes", (list,))
        weights = _require(g, "weights", (list,))
        if not codes or len(codes) != len(weights):
            raise ReplaySchemaError(f"group {gid}: codes/weights mismatch")
        if any(not isinstance(c, str) or not c for c in codes):
            raise ReplaySchemaError(f"group {gid}: bad codes")
        if abs(sum(float(w) for w in weights) - 1.0) > 1e-6:
            raise ReplaySchemaError(f"group {gid}: weights must sum to 1")
        _closed(_require(g, "role", (str,)), ROLE_CLOSED, f"group {gid}.role")
    return d


def validate_specs(d: dict) -> dict:
    """specs.v1.json 校验：闭集 + claim_type↔caliber 派生一致 + 方向类钉窗钉基准。"""
    _closed(_require(d, "schema_version", (str,)), {"fin.cognition-replay-specs/v1"}, "schema_version")
    _require(d, "specs", (list,))
    check_ids: set[str] = set()
    for s in d["specs"]:
        cid = str(_require(s, "check_id", (str,)) or "")
        if not cid.strip():
            raise ReplaySchemaError("empty check_id")
        if cid in check_ids:
            raise ReplaySchemaError(f"duplicate check_id: {cid}")
        check_ids.add(cid)
        _require(s, "unit_id", (str,))
        claim = _closed(_require(s, "claim_type", (str,)), CLAIM_TYPE_CLOSED, "claim_type")
        cal = _closed(_require(s, "caliber", (str,)), CALIBER_CLOSED, "caliber")
        if cal not in _CLAIM_CALIBER[claim]:
            raise ReplaySchemaError(f"{cid}: caliber {cal} not derivable from claim_type {claim}")
        _closed(_require(s, "status", (str,)), SPEC_STATUS_CLOSED, "status")
        w = _require(s, "window", (dict,))
        _require(w, "start", (str,))
        date.fromisoformat(w["start"])
        if w.get("end") is not None:
            date.fromisoformat(w["end"])
            if w["end"] < w["start"]:
                raise ReplaySchemaError(f"{cid}: window.end < start")
        if not str(w.get("maturity_rule", "")):
            raise ReplaySchemaError(f"{cid}: missing window.maturity_rule")
        _require(s, "start_anchor", (str,))
        if not str(s["start_anchor"]).strip():
            raise ReplaySchemaError(f"{cid}: start_anchor empty (防起点 cherry-pick)")
        if cal == "relative_spread":
            if s["status"] != "unscoreable" and (
                not s.get("mainline_group") or not s.get("benchmark_group")
            ):
                raise ReplaySchemaError(f"{cid}: relative_spread needs mainline+benchmark groups")
            if w.get("end") is None and "owner_pinned_pending" not in str(w.get("maturity_rule")):
                raise ReplaySchemaError(f"{cid}: open window requires owner_pinned_pending rule")
        if cal in {"window_event", "relative_spread", "range_check", "same_day"}:
            _require(s, "params", (dict,))
    return d


def validate_snapshot_line(d: dict) -> dict:
    _closed(_require(d, "schema_version", (str,)), {"fin.cognition-replay-snapshot/v1"}, "schema_version")
    for key in ("batch", "as_of", "retrieved_at", "source", "adjustment_basis", "group_id", "role"):
        _require(d, key, (str,))
    _closed(d["role"], ROLE_CLOSED, "role")
    by_code = _require(d, "by_code", (dict,))
    if not by_code:
        raise ReplaySchemaError("empty by_code")
    for code, rows in by_code.items():
        if not isinstance(code, str) or not code or not isinstance(rows, dict) or not rows:
            raise ReplaySchemaError(f"by_code[{code!r}] malformed")
        for day, row in rows.items():
            date.fromisoformat(day)
            if set(row) - SERIES_KEYS:
                raise ReplaySchemaError(f"series row has unknown keys: {sorted(set(row) - SERIES_KEYS)}")
            for k in ("open", "high", "low", "close"):
                _require(row, k, (int, float))
    extra = set(d) - SNAPSHOT_LINE_KEYS
    if extra:
        raise ReplaySchemaError(f"snapshot line has unknown keys: {sorted(extra)}")
    return d


def validate_nomination(d: dict) -> dict:
    _closed(_require(d, "schema_version", (str,)), {"fin.cognition-replay-nomination/v1"}, "schema_version")
    _require(d, "batch", (str,))
    _require(d, "as_of", (str,))
    _require(d, "nominations", (list,))
    for n in d["nominations"]:
        _require(n, "check_id", (str,))
        _require(n, "unit_id", (str,))
        _closed(_require(n, "maturity", (str,)), MATURITY_CLOSED, "maturity")
        rel = n.get("proposed_relation")
        if n["maturity"] == "matured":
            _closed(rel, RELATION_CLOSED, "proposed_relation")
        elif rel is not None:
            raise ReplaySchemaError("open nomination must not carry proposed_relation")
        _require(n, "computed", (dict,))
        _require(n, "members_snapshot", (dict,))
    return d


# ── 存储原语 ──────────────────────────────────────────────


def resolve_state_root(environ: dict[str, str] | None = None) -> Path:
    from fin_analyse.runtime.state_roots import _state_home

    root = _state_home(home=None, environ=environ) / "fin-analyse" / "cognition-replay-evidence"
    return ensure_private_state_directory(root)


def atomic_write_json(path: Path, payload: dict) -> None:
    """整文件原子重写（temp + flock + os.replace），0600；批=文件一一对应。"""
    write_private_state_text(path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ── 口径计算 ──────────────────────────────────────────────


def _group_series(snapshot_line: dict, group_id: str) -> dict[str, dict[str, dict]]:
    """group_id → code → date → ohlc row（来自同批快照行）。"""
    out: dict[str, dict[str, dict]] = {}
    for line in snapshot_line if isinstance(snapshot_line, list) else [snapshot_line]:
        if line.get("group_id") != group_id:
            continue
        for code, rows in line.get("by_code", {}).items():
            out.setdefault(code, {}).update(rows)
    return out


def basket_return(
    by_code: dict[str, dict[str, dict]], codes: list[str], weights: list[float],
    start: str, end: str,
) -> tuple[float | None, str | None]:
    """等权（权重和为 1）收盘价累计收益：Σw·(close_end/close_start − 1)。

    起点取 ≥start 首个共同交易日，终点取 ≤end 最后共同交易日；
    任一 code 缺起点/终点价 → (None, reason)（缺数不编造）。
    """
    starts: dict[str, str] = {}
    ends: dict[str, str] = {}
    for code in codes:
        rows = by_code.get(code) or {}
        if not rows:
            return None, f"no series for {code}"
        dates = sorted(rows)
        s = next((d for d in dates if d >= start), None)
        e = next((d for d in reversed(dates) if d <= end), None)
        if s is None or e is None or s > e:
            return None, f"{code}: no trading day in window"
        starts[code], ends[code] = s, e
    anchor_start = max(starts.values())
    anchor_end = min(ends.values())
    total = 0.0
    for code, w in zip(codes, weights):
        rows = by_code[code]
        s = next((d for d in sorted(rows) if d >= anchor_start), None)
        e = next((d for d in reversed(sorted(rows)) if d <= anchor_end), None)
        if s is None or e is None or s > e:
            return None, f"{code}: no common anchor day"
        total += float(w) * (float(rows[e]["close"]) / float(rows[s]["close"]) - 1.0)
    return total, None


def range_check(
    by_code: dict[str, dict[str, dict]], code: str, start: str, end: str,
    lower: float | None, upper: float | None, apply_to: str,
) -> dict:
    rows = {
        d: r for d, r in (by_code.get(code) or {}).items()
        if start <= d <= end
    }
    if not rows:
        return {"covered_days": 0, "reason": "no series in window"}
    vals = {d: float(r[apply_to]) for d, r in rows.items()}
    inside = [d for d, v in vals.items()
              if (lower is None or v >= lower) and (upper is None or v <= upper)]
    outside = sorted(d for d in vals if d not in set(inside))
    return {
        "covered_days": len(vals),
        "inside_days": len(inside),
        "outside_days": outside,
        "min_value": min(vals.values()),
        "min_value_date": min(vals, key=lambda d: vals[d]),
        "max_value": max(vals.values()),
        "max_value_date": max(vals, key=lambda d: vals[d]),
        "apply_to": apply_to,
    }


def monthly_anchor(rows: dict[str, dict], month: str) -> dict | None:
    """月度锚：{open, close, prev_month_close, monthly_ret}（raw 指数值）。"""
    days = sorted(d for d in rows if d.startswith(month))
    if not days:
        return None
    prev_days = sorted(d for d in rows if d < f"{month}-01")
    prev_close = float(rows[prev_days[-1]]["close"]) if prev_days else None
    return {
        "open": float(rows[days[0]]["open"]),
        "close": float(rows[days[-1]]["close"]),
        "close_date": days[-1],
        "prev_month_close": prev_close,
        "monthly_ret": (
            float(rows[days[-1]]["close"]) / prev_close - 1.0
            if prev_close else None
        ),
    }


def last_closed_trading_day(rows: dict[str, dict], published_on: str) -> str | None:
    """same_day PIT：发文时点前最近已收盘交易日（发文当日收盘不可用）。"""
    prior = sorted(d for d in rows if d < published_on)
    return prior[-1] if prior else None


def same_day_close_available(published_at: str, trade_date: str) -> bool:
    """发文时刻 ≥ 当日 15:00 收盘才可能见到当日收盘（粗粒度 PIT 门）。"""
    try:
        ts = datetime.fromisoformat(published_at)
    except ValueError:
        return False
    if ts.strftime("%Y-%m-%d") != trade_date:
        return ts.strftime("%Y-%m-%d") < trade_date
    return ts.hour >= 15
