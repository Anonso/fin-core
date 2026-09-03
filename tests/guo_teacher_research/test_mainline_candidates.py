"""Mainline candidate scanner: closed-set nomination with read-model dedup (部件1)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
    CognitionMainlinePublisher,
    generate_cognition_mainline_readmodel,
)
from fin_analyse.guo_teacher_research.mainline_candidates import (
    scan_mainline_candidates,
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


def _publish_readmodel(tmp_path: Path, annotation: Path) -> Path:
    root = tmp_path / "readmodel"
    payload = generate_cognition_mainline_readmodel(
        annotation,
        generation=1,
        working_set_identity="a" * 64,
    )
    publication = CognitionMainlinePublisher(root).publish(
        payload,
        expected_prior_identity=None,
    )
    assert publication.disposition == "PUBLISHED"
    return root


def _kb_tree_state(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_mode, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _make_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    kb_root = tmp_path / "knowledge-base"
    articles = kb_root / "articles"
    articles.mkdir(parents=True)
    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = _publish_readmodel(tmp_path, annotation)
    index = [
        {
            "column": "星大派锐评",
            "date": "2026-01-03 10:00",
            "title": "锐评 A",
            "path": str(articles / "2026-01-03_a.md"),
        },
        {
            "column": "星大派每日热点",
            "date": "2026-01-04 09:00",
            "title": "每日热点 0104",
            "path": str(articles / "2026-01-04_b.md"),
        },
        {
            "column": "星大派锐评",
            "date": "2026-01-01 10:00",
            "title": "锐评 as_of 前",
            "path": str(articles / "2026-01-01_c.md"),
        },
        {
            "column": "普通",
            "date": "2026-01-05 10:00",
            "title": "普通栏不进 G",
            "path": str(articles / "2026-01-05_d.md"),
        },
        {
            "column": "星大派好问题",
            "date": "2026-01-05 11:00",
            "title": "好问题缺 is_qa（fail-closed）",
            "path": str(articles / "2026-01-05_e.md"),
        },
        {
            "column": "星大派好问题",
            "date": "2026-01-05 12:00",
            "is_qa": True,
            "title": "好问题带 is_qa",
            "path": str(articles / "2026-01-05_e2.md"),
        },
        {
            "column": "星大派锐评",
            "date": "2026-01-06 10:00",
            "title": "同文已入档",
            "path": str(articles / "x.md"),
        },
        {
            "column": "星大派锐评",
            "date": "bad-date",
            "title": "日期坏行",
            "path": str(articles / "2026-01-07_g.md"),
        },
    ]
    index_path = kb_root / "index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    state_root = tmp_path / "state"
    return annotation, readmodel_root, index_path, state_root


def test_scan_nominates_dedups_and_writes_owner_only_draft(tmp_path: Path) -> None:
    annotation, readmodel_root, index_path, state_root = _make_fixture(tmp_path)
    before = _kb_tree_state(tmp_path / "knowledge-base")

    result = scan_mainline_candidates(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        index_path=index_path,
        state_root=state_root,
    )

    assert result.disposition == "SCANNED"
    assert result.scanned == 8
    assert result.after_as_of == 6  # C 早于 as_of 不计；G 日期坏行不计
    assert result.nominated == 2  # 锐评 A + 带 is_qa 的好问题
    assert result.same_article == 1  # x.md 已入档（CU-0001-01）
    assert result.excluded_usage == 1  # 每日热点 = ai_summary_reference
    assert result.not_eligible == 2  # 普通栏 + 缺 is_qa 的好问题
    assert result.malformed_dates == 1

    draft = Path(str(result.draft_path))
    assert draft.read_text(encoding="utf-8") == draft.read_text(encoding="utf-8")
    assert stat.S_IMODE(draft.stat().st_mode) == 0o600
    text = draft.read_text(encoding="utf-8")
    assert "锐评 A" in text and "好问题带 is_qa" in text
    assert "同文已入档" in text and "CU-0001-01" in text
    assert "每日热点 0104" not in text  # ai_summary_reference 不提名
    assert "普通栏不进 G" not in text
    assert "as_of 锚：2026-01-02" in text

    # 纯读：index/KB 零写入。
    assert _kb_tree_state(tmp_path / "knowledge-base") == before


def test_scan_rewrite_is_deterministic(tmp_path: Path) -> None:
    annotation, readmodel_root, index_path, state_root = _make_fixture(tmp_path)

    first = scan_mainline_candidates(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        index_path=index_path,
        state_root=state_root,
    )
    draft_bytes = Path(str(first.draft_path)).read_bytes()
    second = scan_mainline_candidates(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        index_path=index_path,
        state_root=state_root,
    )

    assert second.disposition == "SCANNED"
    assert Path(str(second.draft_path)).read_bytes() == draft_bytes
    assert not list(state_root.glob("fin-analyse/*.tmp"))


def test_scan_skips_when_annotation_has_no_as_of(tmp_path: Path) -> None:
    annotation = tmp_path / "annotation.md"
    annotation.write_text("没有锚点的文档\n", encoding="utf-8")

    result = scan_mainline_candidates(
        annotation_path=annotation,
        readmodel_root=tmp_path / "readmodel",
        index_path=tmp_path / "index.json",
        state_root=tmp_path / "state",
    )

    assert result.disposition == "SKIPPED"
    assert result.reason == "annotation_as_of_missing"


def test_scan_skips_when_readmodel_unavailable(tmp_path: Path) -> None:
    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)

    result = scan_mainline_candidates(
        annotation_path=annotation,
        readmodel_root=tmp_path / "empty-readmodel",
        index_path=tmp_path / "index.json",
        state_root=tmp_path / "state",
    )

    assert result.disposition == "SKIPPED"
    assert result.reason == "readmodel_unavailable"


def test_scan_skips_when_index_unreadable(tmp_path: Path) -> None:
    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = _publish_readmodel(tmp_path, annotation)

    result = scan_mainline_candidates(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        index_path=tmp_path / "missing-index.json",
        state_root=tmp_path / "state",
    )

    assert result.disposition == "SKIPPED"
    assert result.reason == "index_unreadable"


def test_draft_paths_without_traversal_escape_state_root(tmp_path: Path) -> None:
    """The draft lands under the state root and nowhere else."""

    annotation, readmodel_root, index_path, state_root = _make_fixture(tmp_path)
    result = scan_mainline_candidates(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        index_path=index_path,
        state_root=state_root,
    )

    draft = Path(str(result.draft_path))
    assert draft.parent == state_root / "fin-analyse"
    assert draft.name == "mainline-candidates.md"
    assert os.path.commonpath([str(draft), str(state_root)]) == str(state_root)
