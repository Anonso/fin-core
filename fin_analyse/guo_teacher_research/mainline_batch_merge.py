"""Consume owner final-review verdicts and merge a G batch draft into the
canonical annotation document (MCP v1.4 local execution half).

契约 v1.4 工作流闭环的落地半边：起草会话发布
``g-batch-draft.v1.jsonl``（mainline_draft_manifest）→ owner 经飞书
``g_draft_verdict`` 逐单元 approve/reject → 本模块消费裁决执行
拼合 → 拼合结果上机验（fail-closed，不通过不留任何写入）→ 原子替换
canonical → as_of 滚至裁决时点 → 触发 readmodel 重建 → 清空 manifest。

合并是确定性拼接，无 LLM 参与——G 写入执行留在本地确定性链（Hermes LLM
不直写标注文档，D-053 v1.4 边界）。reject 语义 = 该单元不入档（连同其
时间语义索引行剔除；来源行保留作审阅痕迹）；语义修改请求走 note 留痕、
由起草会话改稿重稿，本模块不改写任何文字。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fin_analyse.runtime.state_roots import (
    adjudication_inbox_state_root,
    ensure_private_state_directory,
)

MANIFEST_NAME = "g-batch-draft.v1.jsonl"
VERSIDECAR_NAME = "g-batch-verdicts.v1.jsonl"
DRAFT_DOC_NAME = "annotation-draft.md"
MERGED_MARKER_NAME = "MERGED.json"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CST = ZoneInfo("Asia/Shanghai")
_AS_OF_RE = re.compile(r"as_of=(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)")
_UNIT_HEADER_RE = re.compile(r"^### (CU-\d{4}-[A-Z]?\d{2})：")


class MainlineBatchMergeError(ValueError):
    """Typed fail-closed error; canonical annotation is never touched."""


# -- verdict sidecar ---------------------------------------------------------


def load_verdicts(sidecar_path: Path) -> dict[str, dict]:
    """Latest verdict per unit_id; ``merged`` journal rows are history, not verdicts."""

    verdicts: dict[str, dict] = {}
    if not sidecar_path.exists():
        return verdicts
    for line in sidecar_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("action") == "merged":
            continue
        unit_id = row.get("unit_id")
        if isinstance(unit_id, str) and unit_id.strip() and "verdict" in row:
            verdicts[unit_id.strip()] = row
    return verdicts


def _verdict_as_of(verdicts: dict[str, dict], unit_ids: list[str]) -> str:
    """复核时点 = owner 完成裁决的时刻（最新一条 approve/reject 的时间戳）。"""

    latest: datetime | None = None
    for unit_id in unit_ids:
        raw = str(verdicts[unit_id].get("at", ""))
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise MainlineBatchMergeError(
                f"verdict timestamp unreadable for {unit_id}: {raw!r}"
            ) from exc
        if latest is None or stamp > latest:
            latest = stamp
    if latest is None:
        raise MainlineBatchMergeError("no verdict timestamps found")
    return latest.astimezone(_CST).strftime("%Y-%m-%d %H:%M")


# -- draft extraction --------------------------------------------------------


def _section_lines(text: str, start_heading: str, end_headings: tuple[str, ...]) -> list[str]:
    lines = text.splitlines()
    start = end = None
    for index, line in enumerate(lines):
        if start is None and line.strip() == start_heading:
            start = index + 1
        elif start is not None and any(
            line.startswith(prefix) for prefix in end_headings
        ):
            end = index
            break
    if start is None:
        raise MainlineBatchMergeError(f"draft missing section {start_heading!r}")
    return lines[start:end] if end is not None else lines[start:]


def extract_draft_parts(draft_text: str) -> dict[str, object]:
    """Pull the four splice payloads out of a machine-checked draft document."""

    source_rows = [
        line
        for line in _section_lines(
            draft_text, "## 来源与边界验证", ("## ",)
        )
        if line.startswith("| S-")
    ]
    time_rows = [
        line
        for line in _section_lines(draft_text, "## 时间语义索引", ("## ",))
        if line.startswith("| CU-")
    ]
    evolution_rows = [
        line
        for line in _section_lines(
            draft_text, "### 主线变化证据", ("## ", "### ")
        )
        if line.startswith("| 2026-")
    ]
    lines = draft_text.splitlines()
    try:
        units_start = next(
            i for i, line in enumerate(lines) if line.strip() == "## 认知单元"
        )
        units_end = next(
            i for i, line in enumerate(lines) if line.startswith("## 跨期演化线")
        )
    except StopIteration as exc:
        raise MainlineBatchMergeError("draft missing 认知单元/跨期演化线 sections") from exc
    units_block = "\n".join(lines[units_start + 1 : units_end]).strip("\n")
    return {
        "source_rows": source_rows,
        "time_rows": time_rows,
        "units_block": units_block,
        "evolution_rows": evolution_rows,
    }


def _drop_unit(units_block: str, unit_id: str) -> str:
    """Remove one unit section (header to next header/end) from the block."""

    lines = units_block.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        header = _UNIT_HEADER_RE.match(line)
        if header is not None:
            skipping = header.group(1) == unit_id
        if not skipping:
            out.append(line)
    return "\n".join(out).strip("\n")


def apply_cuts(parts: dict[str, object], cuts: list[str]) -> dict[str, object]:
    """reject 语义：单元与其时间语义索引行不入档；来源行保留作审阅痕迹。"""

    time_rows = [row for row in parts["time_rows"] if not any(row.startswith(f"| {u} |") for u in cuts)]  # type: ignore[union-attr]
    units_block = parts["units_block"]  # type: ignore[assignment]
    for unit_id in cuts:
        units_block = _drop_unit(units_block, unit_id)
    return {**parts, "time_rows": time_rows, "units_block": units_block}


# -- splice ------------------------------------------------------------------


def _find(lines: list[str], prefix: str, *, start: int = 0, end: int | None = None) -> int:
    end = len(lines) if end is None else end
    for index in range(start, end):
        if lines[index].startswith(prefix):
            return index
    raise MainlineBatchMergeError(f"canonical anchor not found: {prefix!r}")


def _find_last(lines: list[str], prefix: str, *, start: int, end: int) -> int:
    found = -1
    for index in range(start, end):
        if lines[index].startswith(prefix):
            found = index
    if found < 0:
        raise MainlineBatchMergeError(f"canonical anchor not found: {prefix!r}")
    return found


def splice(
    canonical_text: str,
    parts: dict[str, object],
    *,
    batch_note: str,
    as_of_text: str,
    new_unit_ids: list[str],
) -> str:
    """Deterministic four-point splice + header roll; raises before any ambiguity."""

    already = [unit_id for unit_id in new_unit_ids if unit_id in canonical_text]
    if already:
        raise MainlineBatchMergeError(f"canonical already contains units: {already}")

    lines = canonical_text.splitlines()

    # 1. 来源行：插到来源表最后一个 | S- 行之后（表后说明段之前）。
    h_time = _find(lines, "## 时间语义索引")
    last_source = _find_last(lines, "| S-", start=0, end=h_time)
    lines[last_source + 1 : last_source + 1] = list(parts["source_rows"])  # type: ignore[arg-type]

    # 2. 时间语义行：插到时间索引表最后一个 | CU- 行之后。
    h_time = _find(lines, "## 时间语义索引")
    next_h = _find(lines, "## ", start=h_time + 1)
    last_time = _find_last(lines, "| CU-", start=h_time, end=next_h)
    lines[last_time + 1 : last_time + 1] = list(parts["time_rows"])  # type: ignore[arg-type]

    # 3. 认知单元：整块插到「## 跨期演化线」之前。
    h_evo = _find(lines, "## 跨期演化线")
    units_block = str(parts["units_block"])
    if units_block:
        lines[h_evo:h_evo] = ["", *units_block.splitlines(), ""]

    # 4. 演化节点行：接在主线变化证据表最后一行之后。
    h_nodes = _find(lines, "### 主线变化证据")
    next_h = _find(lines, "## ", start=h_nodes + 1)
    last_node = _find_last(lines, "| 2026-", start=h_nodes, end=next_h)
    lines[last_node + 1 : last_node + 1] = list(parts["evolution_rows"])  # type: ignore[arg-type]

    merged = "\n".join(lines)
    if not _AS_OF_RE.search(merged):
        raise MainlineBatchMergeError("canonical as_of anchor missing")
    merged = _AS_OF_RE.sub(f"as_of={as_of_text}", merged, count=1)
    if "状态：" not in merged:
        raise MainlineBatchMergeError("canonical 状态 anchor missing")
    merged = merged.replace("状态：", f"状态：{batch_note}；", 1)
    return merged + "\n"


# -- orchestration -----------------------------------------------------------


def latest_draft_dir(state_root: Path) -> Path:
    candidates = sorted(
        (p for p in (state_root / "fin-analyse").glob("g-batch-draft-*") if p.is_dir()),
        key=lambda p: p.name,
    )
    if not candidates:
        raise MainlineBatchMergeError("no g-batch-draft-* directory under state root")
    return candidates[-1]


def _read_manifest_units(manifest_path: Path) -> list[dict]:
    if not manifest_path.exists():
        raise MainlineBatchMergeError(
            "manifest g-batch-draft.v1.jsonl missing — publish it first "
            "(python -m fin_analyse.guo_teacher_research.mainline_draft_manifest)"
        )
    units: list[dict] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("unit_id") != "_meta":
                units.append(row)
    if not units:
        raise MainlineBatchMergeError("manifest has no units")
    return units


def _run_verifier(merged_path: Path) -> None:
    script = _REPO_ROOT / "scripts" / "verify_mainline_annotation.py"
    result = subprocess.run(
        [sys.executable, str(script), "--annotation", str(merged_path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stdout.splitlines()[-10:])
        raise MainlineBatchMergeError(f"merged draft failed machine verification:\n{tail}")


def _append_journal(sidecar_path: Path, row: dict) -> None:
    sidecar_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(sidecar_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)


def run_merge(
    *,
    draft_dir: Path,
    canonical_path: Path,
    state_root: Path,
    batch_note: str | None = None,
    as_of_override: str | None = None,
    dry_run: bool = False,
    skip_rebuild: bool = False,
) -> dict[str, object]:
    """Full fail-closed sequence; returns a typed report, never partial writes."""

    draft_path = draft_dir / DRAFT_DOC_NAME
    if not draft_path.exists():
        raise MainlineBatchMergeError(f"draft document missing: {draft_path}")
    if (draft_dir / MERGED_MARKER_NAME).exists():
        raise MainlineBatchMergeError(f"batch already merged: {draft_dir.name}")
    inbox_root = ensure_private_state_directory(adjudication_inbox_state_root())
    manifest_path = inbox_root / MANIFEST_NAME
    sidecar_path = inbox_root / VERSIDECAR_NAME

    manifest_units = _read_manifest_units(manifest_path)
    unit_ids = [str(row["unit_id"]) for row in manifest_units]
    verdicts = load_verdicts(sidecar_path)
    undecided = [u for u in unit_ids if u not in verdicts]
    if undecided:
        raise MainlineBatchMergeError(f"units without verdict: {undecided}")
    cuts = sorted(u for u in unit_ids if verdicts[u].get("verdict") == "reject")
    as_of_text = as_of_override or _verdict_as_of(verdicts, unit_ids)
    if batch_note is None:
        batch_note = (
            f"批次入档（{as_of_text} 终审：{draft_dir.name} 共 "
            f"{len(unit_ids) - len(cuts)} 单元入档"
            + (f"、{len(cuts)} 单元 owner reject 剔除" if cuts else "")
            + "；owner 经飞书 g_draft_verdict 逐单元裁决，机验通过后由本地确定性链合并）"
        )

    canonical_text = canonical_path.read_text(encoding="utf-8")
    parts = apply_cuts(extract_draft_parts(draft_path.read_text(encoding="utf-8")), cuts)
    merged_text = splice(
        canonical_text,
        parts,
        batch_note=batch_note,
        as_of_text=as_of_text,
        new_unit_ids=unit_ids,
    )

    report: dict[str, object] = {
        "batch": draft_dir.name,
        "units_total": len(unit_ids),
        "units_merged": len(unit_ids) - len(cuts),
        "rejected": cuts,
        "as_of": as_of_text,
        "canonical": str(canonical_path),
        "dry_run": dry_run,
    }
    if dry_run:
        report["disposition"] = "DRY_RUN"
        return report

    # fail-closed 顺序：拼合 → 机验 → 原子替换；机验不过则 canonical 无任何写入。
    tmp_path = canonical_path.parent / (canonical_path.name + ".merge-tmp")
    descriptor = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, merged_text.encode("utf-8"))
    finally:
        os.close(descriptor)
    try:
        _run_verifier(tmp_path)
    except MainlineBatchMergeError:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, canonical_path)

    _append_journal(
        sidecar_path,
        {
            "at": datetime.now(_CST).isoformat(timespec="seconds"),
            "action": "merged",
            "batch": draft_dir.name,
            "as_of": as_of_text,
            "units": len(unit_ids) - len(cuts),
            "rejected": cuts,
            "via": "merge_script",
        },
    )
    (draft_dir / MERGED_MARKER_NAME).write_text(
        json.dumps({"as_of": as_of_text, "merged_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00")}, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest_path.unlink(missing_ok=True)  # g_draft_list 回空表，终审循环闭合
    report["disposition"] = "MERGED"

    if not skip_rebuild:
        from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import rebuild_if_stale

        data_root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        rebuild = rebuild_if_stale(
            annotation_path=canonical_path,
            readmodel_root=state_root / "fin-analyse" / "cognition-mainline-readmodel-v1",
            manifest_path=data_root
            / "fin-analyse"
            / "shared"
            / "knowledge-base"
            / "runtime"
            / "operations"
            / "g_working_set"
            / "manifest.v1.json",
        )
        report["rebuild"] = rebuild.to_dict()
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--draft-dir", type=Path, default=None, help="起草稿目录（默认 state 下最新 g-batch-draft-*）")
    parser.add_argument("--canonical", type=Path, default=None, help="canonical 标注文档（默认 knowledge_root 缝）")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--as-of", default=None, help="复核锚（默认=最新裁决时间戳）")
    parser.add_argument("--batch-note", default=None, help="写入状态行的批次记录句")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-rebuild", action="store_true")
    args = parser.parse_args(argv)

    state_root = args.state_root or Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    )
    draft_dir = args.draft_dir or latest_draft_dir(state_root)
    if args.canonical is None:
        from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

        canonical_path = (
            default_knowledge_base_root() / "manual-annotations" / "g-cognition-mainline.md"
        )
    else:
        canonical_path = args.canonical
    try:
        report = run_merge(
            draft_dir=draft_dir,
            canonical_path=canonical_path,
            state_root=state_root,
            batch_note=args.batch_note,
            as_of_override=args.as_of,
            dry_run=args.dry_run,
            skip_rebuild=args.skip_rebuild,
        )
    except MainlineBatchMergeError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
