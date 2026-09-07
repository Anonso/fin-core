"""G 批次终审闭环的本地执行半边：manifest 发布 + 裁决消费合并（MCP v1.4）。

纯函数（extract/apply_cuts/splice/publish）用 tmp fixture 验证；真实链路的
机验/重建由合并当日的实际批次跑（scripts/verify_mainline_annotation.py 对
canonical 全量校验是 run_merge 内置闸，不在单测重复环境）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.mainline_batch_merge import (
    MainlineBatchMergeError,
    apply_cuts,
    extract_draft_parts,
    load_verdicts,
    run_merge,
    splice,
)
from fin_analyse.guo_teacher_research.mainline_draft_manifest import publish_manifest

_CANONICAL = """# G 历史认知主线人工标注（窗口 2026-06-01 起 · as_of 滚动只进不退）

状态：既有批次记录；`as_of=2026-09-04 22:25`（带时点：真实复核时点）。

## 来源与边界验证

| 来源 ID | 发表时间 | 可回指原文 | 来源性质与本轮用途 |
| --- | --- | --- | --- |
| S-0901A | 2026-09-01 10:00 | `knowledge-base/articles/20260901_zsxq-1.md` | G 原文；既有行。 |

既有批次的表后说明段。

## 时间语义索引

| 认知单元 | published_at | observed_at | effective_period | forecast_window |
| --- | --- | --- | --- | --- |
| CU-0901-01 | 2026-09-01 10:00 | unknown/not stated | 当期 | none stated |

## 认知单元

### CU-0901-01：既有单元

- 来源/时间：S-0901A，2026-09-01。
- 认知模式：主 `当前观察`。
- G 原文：“既有观察。”。
- 深化表达：既有。
- Agent 推理：无。
- 来源材料内方向/选择：无。
- Agent 投资选择：无。
- 来源材料内行动指导：无。
- Agent 交易策略：无。
- 验证：原文已回指。

## 跨期演化线（明确属于 Agent 推理）

### 主线变化证据

| 节点 | 相对前序 | 变化类型 | 保留内容 | 新增内容与判定依据 |
| --- | --- | --- | --- | --- |
| 2026-09-01 | 无 | baseline（非变化类型） | 无前序可比较。 | 建立基线。 |

## 市场实践回放线

尾节，保证演化节有终结边界。
"""

_DRAFT = """# G 认知主线批次起草稿（机验稿）

状态：起草稿；本稿 as_of=2026-09-08 09:00 为起草锚。

## 来源与边界验证

| 来源 ID | 发表时间 | 可回指原文 | 来源性质与本轮用途 |
| --- | --- | --- | --- |
| S-0905A | 2026-09-05 11:28 | `knowledge-base/articles/20260905_zsxq-100.md` | G 原文；测试行。 |
| S-0905B | 2026-09-05 12:00 | `knowledge-base/articles/20260905_zsxq-200.md` | G 原文；测试行。 |

## 时间语义索引

| 认知单元 | published_at | observed_at | effective_period | forecast_window |
| --- | --- | --- | --- | --- |
| CU-0905-01 | 2026-09-05 11:28 | 2026-09-05（“今天”） | 当期 | none stated |
| CU-0905-02 | 2026-09-05 12:00 | unknown/not stated | 当期 | none stated |

## 认知单元

### CU-0905-01：测试单元一

- 来源/时间：S-0905A，2026-09-05。
- 认知模式：主 `当前观察`。
- G 原文：“第一个测试判断。”。
- 深化表达：测试一。
- Agent 推理：无。
- 来源材料内方向/选择：无。
- Agent 投资选择：无。
- 来源材料内行动指导：无。
- Agent 交易策略：无。
- 验证：原文已回指。

### CU-0905-02：测试单元二

- 来源/时间：S-0905B，2026-09-05。
- 认知模式：主 `结构分析`。
- G 原文：“第二个测试判断。”。
- 深化表达：测试二。
- Agent 推理：无。
- 来源材料内方向/选择：无。
- Agent 投资选择：无。
- 来源材料内行动指导：无。
- Agent 交易策略：无。
- 验证：原文已回指。

## 跨期演化线（明确属于 Agent 推理）

### 主线变化证据

| 节点 | 相对前序 | 变化类型 | 保留内容 | 新增内容与判定依据 |
| --- | --- | --- | --- | --- |
| 2026-09-05 | 9/1 | `increment` | 前序保留。 | 新增两个测试单元。 |

## 终审与合并记录（占位）

owner 终审后合并。
"""


def _verdict_row(unit_id: str, verdict: str, at: str) -> str:
    return json.dumps(
        {"at": at, "unit_id": unit_id, "verdict": verdict, "via": "mcp"},
        ensure_ascii=False,
    )


def test_splice_inserts_four_sections_and_rolls_header() -> None:
    parts = extract_draft_parts(_DRAFT)
    merged = splice(
        _CANONICAL,
        parts,
        batch_note="批次入档（测试）",
        as_of_text="2026-09-08 10:30",
        new_unit_ids=["CU-0905-01", "CU-0905-02"],
    )
    lines = merged.splitlines()
    # 来源行接在既有 | S- 行之后、表后说明段之前
    s_new = next(i for i, l in enumerate(lines) if l.startswith("| S-0905A"))
    s_old = next(i for i, l in enumerate(lines) if l.startswith("| S-0901A"))
    note = next(i for i, l in enumerate(lines) if l.startswith("既有批次的表后说明段"))
    assert s_old < s_new < note
    # 时间行接在既有 | CU- 行之后
    t_new = next(i for i, l in enumerate(lines) if l.startswith("| CU-0905-01 |"))
    t_old = next(i for i, l in enumerate(lines) if l.startswith("| CU-0901-01 |"))
    assert t_old < t_new
    # 单元块在跨期演化线之前；演化行接在最后一行既有节点之后、仍在演化节内
    h_evo = next(i for i, l in enumerate(lines) if l.startswith("## 跨期演化线"))
    assert any("### CU-0905-02：测试单元二" in l for l in lines[:h_evo])
    n_new = next(i for i, l in enumerate(lines) if l.startswith("| 2026-09-05 |"))
    n_old = next(i for i, l in enumerate(lines) if l.startswith("| 2026-09-01 |"))
    h_replay = next(i for i, l in enumerate(lines) if l.startswith("## 市场实践回放线"))
    assert h_evo < n_old < n_new < h_replay
    # 头部滚锚与批次记录句
    assert "as_of=2026-09-08 10:30" in merged
    assert "状态：批次入档（测试）；既有批次记录" in merged


def test_apply_cuts_removes_unit_and_time_row_keeps_source() -> None:
    parts = apply_cuts(extract_draft_parts(_DRAFT), ["CU-0905-02"])
    assert "CU-0905-02" not in parts["units_block"]
    assert "CU-0905-01" in parts["units_block"]
    assert all(not row.startswith("| CU-0905-02 |") for row in parts["time_rows"])
    assert any(row.startswith("| S-0905B") for row in parts["source_rows"])


def test_undecided_verdicts_block_merge(tmp_path: Path, monkeypatch) -> None:
    state_root = tmp_path / "state"
    draft_dir = tmp_path / "g-batch-draft-test"
    draft_dir.mkdir()
    (draft_dir / "annotation-draft.md").write_text(_DRAFT, encoding="utf-8")
    inbox = state_root / "fin-analyse" / "adjudication-inbox-v1"
    inbox.mkdir(parents=True)
    monkeypatch.setattr(
        "fin_analyse.guo_teacher_research.mainline_batch_merge.adjudication_inbox_state_root",
        lambda: inbox,
    )
    manifest = inbox / "g-batch-draft.v1.jsonl"
    manifest.write_text(
        json.dumps({"unit_id": "_meta", "batch": draft_dir.name}, ensure_ascii=False)
        + "\n"
        + json.dumps({"unit_id": "CU-0905-01"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"unit_id": "CU-0905-02"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    (inbox / "g-batch-verdicts.v1.jsonl").write_text(
        _verdict_row("CU-0905-01", "approve", "2026-09-08T10:00:00+08:00") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "fin_analyse.guo_teacher_research.mainline_batch_merge.adjudication_inbox_state_root",
        lambda: inbox,
    )
    with pytest.raises(MainlineBatchMergeError, match="units without verdict"):
        run_merge(
            draft_dir=draft_dir,
            canonical_path=tmp_path / "canonical.md",
            state_root=state_root,
            skip_rebuild=True,
        )


def test_reject_cut_and_merged_journal_with_dry_run_off(tmp_path: Path, monkeypatch) -> None:
    """全链（无机验/无重建的 dry_run=False 变体）：reject 剔除 + journal 落账。"""

    state_root = tmp_path / "state"
    draft_dir = tmp_path / "g-batch-draft-test"
    draft_dir.mkdir()
    (draft_dir / "annotation-draft.md").write_text(_DRAFT, encoding="utf-8")
    canonical = tmp_path / "canonical.md"
    canonical.write_text(_CANONICAL, encoding="utf-8")
    inbox = state_root / "fin-analyse" / "adjudication-inbox-v1"
    inbox.mkdir(parents=True)
    (inbox / "g-batch-draft.v1.jsonl").write_text(
        json.dumps({"unit_id": "_meta", "batch": draft_dir.name}, ensure_ascii=False)
        + "\n"
        + json.dumps({"unit_id": "CU-0905-01"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"unit_id": "CU-0905-02"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    (inbox / "g-batch-verdicts.v1.jsonl").write_text(
        _verdict_row("CU-0905-01", "approve", "2026-09-08T10:00:00+08:00")
        + "\n"
        + _verdict_row("CU-0905-02", "reject", "2026-09-08T10:01:00+08:00")
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "fin_analyse.guo_teacher_research.mainline_batch_merge.adjudication_inbox_state_root",
        lambda: inbox,
    )
    monkeypatch.setattr(
        "fin_analyse.guo_teacher_research.mainline_batch_merge._run_verifier",
        lambda _path: None,
    )
    report = run_merge(
        draft_dir=draft_dir,
        canonical_path=canonical,
        state_root=state_root,
        skip_rebuild=True,
    )
    assert report["disposition"] == "MERGED"
    assert report["units_merged"] == 1 and report["rejected"] == ["CU-0905-02"]
    merged = canonical.read_text(encoding="utf-8")
    assert "CU-0905-01" in merged and "CU-0905-02" not in merged
    assert "as_of=2026-09-08 10:01" in merged  # 锚=最新裁决时间戳
    journal = [
        json.loads(l)
        for l in (inbox / "g-batch-verdicts.v1.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert journal[-1]["action"] == "merged" and journal[-1]["rejected"] == ["CU-0905-02"]
    assert not (inbox / "g-batch-draft.v1.jsonl").exists()  # manifest 清空
    assert (draft_dir / "MERGED.json").exists()


def test_already_merged_guard(tmp_path: Path) -> None:
    draft_dir = tmp_path / "g-batch-draft-test"
    draft_dir.mkdir()
    (draft_dir / "annotation-draft.md").write_text(_DRAFT, encoding="utf-8")
    (draft_dir / "MERGED.json").write_text("{}", encoding="utf-8")
    with pytest.raises(MainlineBatchMergeError, match="already merged"):
        run_merge(
            draft_dir=draft_dir,
            canonical_path=tmp_path / "canonical.md",
            state_root=tmp_path / "state",
            skip_rebuild=True,
        )


def test_publish_manifest_writes_meta_and_units(tmp_path: Path, monkeypatch) -> None:
    """发布面：读起草稿（readmodel 生成器解析）写 manifest；同批幂等、跨批拒绝。"""

    state_root = tmp_path / "state"
    inbox = state_root / "fin-analyse" / "adjudication-inbox-v1"
    draft_dir = tmp_path / "g-batch-draft-test"
    draft_dir.mkdir()
    (draft_dir / "annotation-draft.md").write_text(_DRAFT, encoding="utf-8")
    monkeypatch.setattr(
        "fin_analyse.guo_teacher_research.mainline_draft_manifest.adjudication_inbox_state_root",
        lambda: inbox,
    )
    report = publish_manifest(draft_dir=draft_dir, state_root=state_root)
    assert report["disposition"] == "PUBLISHED" and report["units"] == 2
    manifest = inbox / "g-batch-draft.v1.jsonl"
    rows = [json.loads(l) for l in manifest.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows[0]["unit_id"] == "_meta" and rows[0]["batch"] == draft_dir.name
    first = next(r for r in rows if r["unit_id"] == "CU-0905-01")
    assert first["excerpt"].startswith("“第一个测试判断。”")
    assert first["topic_id"] == "100" and first["cognition_mode"] == "当前观察"
    # 同批重发幂等
    assert publish_manifest(draft_dir=draft_dir, state_root=state_root)["units"] == 2
    # 跨批拒绝（owner 可能正在审）
    other = tmp_path / "g-batch-draft-other"
    other.mkdir()
    (other / "annotation-draft.md").write_text(_DRAFT, encoding="utf-8")
    with pytest.raises(MainlineBatchMergeError, match="another batch"):
        publish_manifest(draft_dir=other, state_root=state_root)


def test_load_verdicts_ignores_merged_journal(tmp_path: Path) -> None:
    sidecar = tmp_path / "verdicts.jsonl"
    sidecar.write_text(
        _verdict_row("CU-0905-01", "approve", "2026-09-08T10:00:00+08:00")
        + "\n"
        + json.dumps(
            {"at": "2026-09-08T10:05:00+08:00", "action": "merged", "batch": "x", "via": "merge_script"}
        )
        + "\n",
        encoding="utf-8",
    )
    verdicts = load_verdicts(sidecar)
    assert set(verdicts) == {"CU-0905-01"} and verdicts["CU-0905-01"]["verdict"] == "approve"
