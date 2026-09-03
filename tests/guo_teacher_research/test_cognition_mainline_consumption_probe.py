"""Consumption probe for the cognition mainline projection (设计门 部件5).

The probe answers 「接线之外有没有真被消费」: unit_ids + generation + PIT gap
codes ride out-of-band with the resolve result and are merged into the one
trace row at the server layer.  The projection attachments themselves never
change.
"""

from __future__ import annotations

import json
from pathlib import Path

from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
    CognitionMainlineReadModelReader,
    CognitionMainlinePublisher,
    generate_cognition_mainline_readmodel,
)
from fin_analyse.guo_teacher_research.production_capability_provider import (
    _g_layered_context_value,
)
from fin_analyse.guo_teacher_research.runtime_context import (
    AgentRuntimeContextResult,
    _build_cognition_mainline_projection,
)


def _write_annotation(doc_path: Path) -> None:
    doc_path.write_text(
        "as_of=2026-01-02\n"
        "\n"
        "## 来源与边界验证\n"
        "\n"
        "| 来源 ID | 发表时间 | 可回指原文 | 来源性质与本轮用途 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| S-0001 | 2026-01-01 10:00 | `knowledge-base/articles/x.md` | G 原文；测试。 |\n"
        "\n"
        "## 时间语义索引\n"
        "\n"
        "| 认知单元 | published_at | observed_at | effective_period | forecast_window |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| CU-0001-01 | 2026-01-01 10:00 | unknown/not stated | 测试 | none stated |\n"
        "\n"
        "## 认知单元\n"
        "\n"
        "### CU-0001-01：测试单元\n"
        "- 来源/时间：S-0001，2026-01-01。\n"
        "- 认知模式：主 `当前观察`。\n"
        "- G 原文：测试原文。\n"
        "- 深化表达：测试深化。\n"
        "- 验证：测试限制。\n"
        "\n"
        "### 主线变化证据\n"
        "\n"
        "| 节点 | 前序节点 | 变化类型 | 保持 | 新增 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 2026-01-01 测试 | 无 | baseline | 无前序可比较。 | CU-0001-01 |\n"
        "\n"
        "## 尾部 section\n",
        encoding="utf-8",
    )


def _build_readmodel(tmp_path: Path, identity: str) -> Path:
    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    root = tmp_path / "readmodel"
    payload = generate_cognition_mainline_readmodel(
        annotation,
        generation=1,
        working_set_identity=identity,
    )
    publication = CognitionMainlinePublisher(root).publish(
        payload,
        expected_prior_identity=None,
    )
    assert publication.disposition == "PUBLISHED"
    return root


def _annotation_for(tmp_path: Path) -> Path:
    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    return annotation


def test_projection_carries_consumption_audit(tmp_path: Path) -> None:
    identity = "a" * 64
    root = _build_readmodel(tmp_path, identity)

    projection = _build_cognition_mainline_projection(
        reader=CognitionMainlineReadModelReader(root),
        now="2026-01-02T00:00:00+08:00",
        working_set_identity=identity,
        question="测试",
    )

    assert projection["items"], "fixture unit should project"
    audit = projection["consumption_audit"]
    assert audit["unit_ids"] == ["CU-0001-01"]
    assert audit["generation"] == 1
    assert audit["gap_codes"] == []
    # 投影附件本身不变：items/data_gaps 之外无新增 agent 可见键。
    assert set(projection) == {"items", "data_gaps", "consumption_audit"}


def test_projection_audit_on_reader_absent(tmp_path: Path) -> None:
    projection = _build_cognition_mainline_projection(
        reader=None,
        now="2026-01-02T00:00:00+08:00",
        working_set_identity="a" * 64,
    )

    assert projection["items"] == []
    audit = projection["consumption_audit"]
    assert audit == {
        "unit_ids": [],
        "generation": None,
        "gap_codes": ["g_cognition_readmodel_unavailable"],
    }


def test_projection_audit_on_unreadable_readmodel(tmp_path: Path) -> None:
    projection = _build_cognition_mainline_projection(
        reader=CognitionMainlineReadModelReader(tmp_path / "empty"),
        now="2026-01-02T00:00:00+08:00",
        working_set_identity="a" * 64,
    )

    audit = projection["consumption_audit"]
    assert audit["unit_ids"] == []
    assert audit["generation"] is None
    assert audit["gap_codes"]
    assert all(code.startswith("g_cognition_readmodel_") for code in audit["gap_codes"])


def test_attestation_carries_consumption_audit() -> None:
    audit = {"unit_ids": ["CU-0001-01"], "generation": 7, "gap_codes": []}
    resolved = AgentRuntimeContextResult(
        llm_context={},
        audit_context={},
        quality_flags={"cognition_mainline_consumption": audit},
    )

    value = _g_layered_context_value(
        raw_items=[],
        audit_by_ref={},
        as_of=None,
        resolved=resolved,
        question="测试",
        gaps=[],
    )

    assert value["attestation"]["quality"]["cognition_mainline_consumption"] == audit


def test_attestation_omits_key_when_audit_absent() -> None:
    resolved = AgentRuntimeContextResult(llm_context={}, audit_context={})

    value = _g_layered_context_value(
        raw_items=[],
        audit_by_ref={},
        as_of=None,
        resolved=resolved,
        question="测试",
        gaps=[],
    )

    assert "cognition_mainline_consumption" not in value["attestation"]["quality"]


def test_trace_summary_merges_consumption_probe() -> None:
    from fin_analyse.read_capabilities.server import _trace_summary

    audit = {"unit_ids": ["CU-0001-01"], "generation": 7, "gap_codes": []}
    value = {
        "attestation": {
            "quality": {
                "pinned_injected": True,
                "cognition_mainline_consumption": audit,
            }
        }
    }

    summary = _trace_summary("read_g_context", value)

    assert summary is not None
    assert summary["g_pinned"] == {"pinned_injected": True}
    assert summary["cognition_mainline_consumption"] == audit
    # 审计 JSONL 行必须可序列化（typed 行契约）。
    json.dumps(summary, ensure_ascii=False)


def test_trace_summary_none_without_enrichment() -> None:
    from fin_analyse.read_capabilities.server import _trace_summary

    assert (
        _trace_summary("read_g_context", {"attestation": {"quality": {}}}) is None
    )
    assert _trace_summary("read_market_snapshot", {"attestation": {}}) is None


def test_wiring_injects_cognition_reader_from_environ_state(
    monkeypatch, tmp_path: Path
) -> None:
    """Composition 根按 environ 派生 state 根注入 reader（部件5 实弹发现的修复）。"""

    from fin_analyse.read_capabilities.wiring import build_reader_wiring

    identity = "a" * 64
    kb_root = tmp_path / "knowledge-base"
    kb_root.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "isolated-state"
    root = state / "fin-analyse" / "cognition-mainline-readmodel-v1"
    root.mkdir(parents=True, exist_ok=True)
    payload = generate_cognition_mainline_readmodel(
        _annotation_for(tmp_path),
        generation=1,
        working_set_identity=identity,
    )
    assert (
        CognitionMainlinePublisher(root).publish(
            payload, expected_prior_identity=None
        ).disposition
        == "PUBLISHED"
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    wiring = build_reader_wiring(
        kb_root,
        environ={"XDG_STATE_HOME": str(state)},
    )

    runner = wiring.runners.get("read_g_context")
    assert runner is not None, wiring.unavailable_tools
    from fin_analyse.read_capabilities.types import ProductionReadRequest

    result = runner(
        ProductionReadRequest(
            question="测试", as_of=__import__("datetime").datetime(2026, 1, 2, tzinfo=__import__("datetime").UTC)
        )
    )
    audit = result.value["attestation"]["quality"]["cognition_mainline_consumption"]
    # 裸 KB 无 G 工作集 → resolve 侧 working_set_identity 为空 → PIT 门按
    # 设计拒投影（identity mismatch）；本用例只断言 composition 注入了
    # reader：工件可读（generation）且不再是 unavailable。
    assert audit["generation"] == 1
    assert "g_cognition_readmodel_unavailable" not in audit["gap_codes"]
