"""SPOKEN_FAN_TRANSCRIBED 第四档单元测试（设计 g-spoken-transcribed-grade v2）。

覆盖：来源表 canonical 标记解析、无标记回落 G_ORIGINAL 回归、validator 放行、
projector archive-only 锁死（新档永不投影）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
    generate_cognition_mainline_readmodel,
    project_cognition_mainline,
)


def _write_annotation(doc_path: Path, *, usage: str) -> None:
    doc_path.write_text(
        "as_of=2026-01-06\n"
        "\n"
        "## 来源与边界验证\n"
        "\n"
        "| 来源 ID | 发表时间 | 可回指原文 | 来源性质与本轮用途 |\n"
        "| --- | --- | --- | --- |\n"
        f"| S-0001 | 2026-01-05 10:00 | `knowledge-base/articles/s-0001.md` | {usage} |\n"
        "\n"
        "## 时间语义索引\n"
        "\n"
        "| 认知单元 | published_at | observed_at | effective_period | forecast_window |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| CU-0105-01 | 2026-01-05 10:00 | unknown/not stated | 测试 | none stated |\n"
        "\n"
        "## 认知单元\n"
        "\n"
        "### CU-0105-01：测试单元\n"
        "- 来源/时间：S-0001，2026-01-05。\n"
        "- 认知模式：主 `当前观察`。\n"
        "- G 原文：口播转述片段。\n"
        "- 深化表达：测试深化。\n"
        "- 验证：测试限制。\n"
        "\n"
        "### 主线变化证据\n"
        "\n"
        "| 节点 | 前序节点 | 变化类型 | 保持 | 新增 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 2026-01-05 测试 | 无 | baseline | 无前序可比较。 | CU-0105-01 |\n"
        "\n"
        "## 尾部 section\n",
        encoding="utf-8",
    )


def test_spoken_marker_parses_to_new_grade(tmp_path: Path) -> None:
    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation, usage="spoken_fan_transcribed；直播口播·粉丝AI转述。")

    payload = generate_cognition_mainline_readmodel(annotation)

    natures = {s["source_id"]: s["source_nature"] for s in payload["sources"]}
    assert natures["S-0001"] == "SPOKEN_FAN_TRANSCRIBED"


def test_unmarked_source_still_defaults_to_g_original(tmp_path: Path) -> None:
    """防静默升格回归：无 canonical 标记的来源一律回落 G_ORIGINAL。"""

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation, usage="G 原文；测试。")

    payload = generate_cognition_mainline_readmodel(annotation)

    natures = {s["source_id"]: s["source_nature"] for s in payload["sources"]}
    assert natures["S-0001"] == "G_ORIGINAL"


def test_noncanonical_spelling_does_not_upgrade(tmp_path: Path) -> None:
    """v1 草稿的 `spoken/fan-transcribed` 写法已作废：不得映射进新档。"""

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation, usage="spoken/fan-transcribed；旧写法。")

    payload = generate_cognition_mainline_readmodel(annotation)

    natures = {s["source_id"]: s["source_nature"] for s in payload["sources"]}
    assert natures["S-0001"] == "G_ORIGINAL"


def test_projector_excludes_spoken_grade(tmp_path: Path) -> None:
    """archive-only 锁死：spoken 单元即便被 evolution 节点覆盖也不投影。"""

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation, usage="spoken_fan_transcribed；直播口播·粉丝AI转述。")
    payload = generate_cognition_mainline_readmodel(annotation)

    projection = project_cognition_mainline(
        payload,
        as_of=datetime(2026, 1, 7, tzinfo=UTC),
        working_set_identity=payload["pit_working_set_identity"],
    )

    projected_text = " ".join(str(item) for item in projection.items)
    assert "CU-0105-01" not in projected_text
    assert "口播转述片段" not in projected_text
