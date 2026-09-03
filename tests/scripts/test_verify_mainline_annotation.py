"""Machine verification for mainline annotation drafts (部件2, fail-closed)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(
    monkeypatch, tmp_path: Path, annotation: Path, kb_root: Path
) -> tuple[int, str]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.verify_mainline_annotation",
            "--annotation",
            str(annotation),
            "--kb-root",
            str(kb_root),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    return proc.returncode, proc.stdout


def _write_kb(tmp_path: Path, articles: dict[str, str]) -> Path:
    kb = tmp_path / "knowledge-base"
    articles_dir = kb / "articles"
    articles_dir.mkdir(parents=True)
    for name, text in articles.items():
        (articles_dir / name).write_text(text, encoding="utf-8")
    return kb


def _write_annotation(
    doc_path: Path,
    *,
    as_of: str,
    sources: list[tuple[str, str, str]],  # (source_id, published, usage)
    time_rows: list[tuple[str, str]],
    units: list[str],  # rendered unit sections
    evolution_rows: list[tuple[str, str, str]],  # (node, prior, change)
) -> None:
    lines = [f"as_of={as_of}", "", "## 来源与边界验证", "", ""]
    header = "| 来源 ID | 发表时间 | 可回指原文 | 来源性质与本轮用途 |"
    sep = "| --- | --- | --- | --- |"
    rows = [
        f"| {sid} | {published} | `knowledge-base/articles/{sid.lower()}.md` | {usage} |"
        for sid, published, usage in sources
    ]
    lines += [header, sep, *rows, "", "## 时间语义索引", "", ""]
    time_header = "| 认知单元 | published_at | observed_at | effective_period | forecast_window |"
    time_sep = "| --- | --- | --- | --- | --- |"
    time_table = [f"| {uid} | {published} | unknown/not stated | 测试 | none stated |" for uid, published in time_rows]
    lines += [time_header, time_sep, *time_table, "", "## 认知单元", "", *units, "", "### 主线变化证据", "", ""]
    evo_header = "| 节点 | 前序节点 | 变化类型 | 保持 | 新增 |"
    evo_sep = "| --- | --- | --- | --- | --- |"
    evo_rows = [f"| {node} | {prior} | {change} | 无前序可比较。 | {added} |" for node, prior, change, added in evolution_rows]
    lines += [evo_header, evo_sep, *evo_rows, "", "## 尾部 section", ""]
    doc_path.write_text("\n".join(lines), encoding="utf-8")


def _unit(unit_id: str, source_id: str, published_day: str, quote: str) -> str:
    return (
        f"### {unit_id}：测试单元\n"
        f"- 来源/时间：{source_id}，{published_day}。\n"
        "- 认知模式：主 `当前观察`。\n"
        f"- G 原文：{quote}\n"
        "- 深化表达：测试深化。\n"
        "- 验证：测试限制。"
    )


def test_pass_exact_and_whitespace_normalized_quotes(monkeypatch, tmp_path: Path) -> None:
    article_exact = "老师原话：这是逐字摘录的一句话。后续还有别的段落。"
    article_spaced = "观点一。结论：先 守 后 攻。"
    kb = _write_kb(
        tmp_path,
        {
            "s-0001.md": article_exact,
            "s-0002.md": article_spaced,
        },
    )
    annotation = tmp_path / "annotation.md"
    _write_annotation(
        annotation,
        as_of="2026-01-02",
        sources=[
            ("S-0001", "2026-01-01 10:00", "G 原文；测试。"),
            ("S-0002", "2026-01-01 11:00", "G 原文；测试。"),
        ],
        time_rows=[("CU-0001-01", "2026-01-01 10:00"), ("CU-0002-01", "2026-01-01 11:00")],
        units=[
            _unit("CU-0001-01", "S-0001", "2026-01-01", "这是逐字摘录的一句话。"),
            _unit("CU-0002-01", "S-0002", "2026-01-01", "结论：先守后攻。"),
        ],
        evolution_rows=[("2026-01-01 测试", "无", "`baseline`", "CU-0001-01, CU-0002-01")],
    )

    code, out = _run(monkeypatch, tmp_path, annotation, kb)

    assert code == 0, out
    assert "CU-0001-01 g_original_quote: exact" in out
    assert "CU-0002-01 g_original_quote: whitespace-normalized" in out
    assert "RESULT: PASS" in out


def test_fail_excerpt_reports_unit_without_quote_content(monkeypatch, tmp_path: Path) -> None:
    kb = _write_kb(tmp_path, {"s-0001.md": "文章真实内容。"})
    annotation = tmp_path / "annotation.md"
    forged = "这句话不在文章里。"
    _write_annotation(
        annotation,
        as_of="2026-01-02",
        sources=[("S-0001", "2026-01-01 10:00", "G 原文；测试。")],
        time_rows=[("CU-0001-01", "2026-01-01 10:00")],
        units=[_unit("CU-0001-01", "S-0001", "2026-01-01", forged)],
        evolution_rows=[("2026-01-01 测试", "无", "`baseline`", "CU-0001-01")],
    )

    code, out = _run(monkeypatch, tmp_path, annotation, kb)

    assert code == 1
    assert "FAIL unit CU-0001-01 g_original_quote" in out
    assert forged not in out  # 引句内容绝不入日志
    assert "RESULT: FAIL" in out


def test_fail_g_original_unit_without_node_coverage(monkeypatch, tmp_path: Path) -> None:
    kb = _write_kb(tmp_path, {"s-0001.md": "可回指内容。"})
    annotation = tmp_path / "annotation.md"
    _write_annotation(
        annotation,
        as_of="2026-01-06",  # ≥ 孤儿单元 published_at，先过整份校验
        sources=[("S-0001", "2026-01-05 10:00", "G 原文；测试。")],
        time_rows=[("CU-0105-01", "2026-01-05 10:00")],
        units=[_unit("CU-0105-01", "S-0001", "2026-01-05", "可回指内容。")],
        evolution_rows=[("2026-01-01 基线", "无", "`baseline`", "（无新增单元）")],
    )

    code, out = _run(monkeypatch, tmp_path, annotation, kb)

    assert code == 1
    assert "CU-0105-01: G_ORIGINAL unit not covered" in out


def test_mixed_unit_without_node_row_is_info_only(monkeypatch, tmp_path: Path) -> None:
    kb = _write_kb(tmp_path, {"s-0001.md": "混合材料原文片段。"})
    annotation = tmp_path / "annotation.md"
    _write_annotation(
        annotation,
        as_of="2026-01-06",
        sources=[("S-0001", "2026-01-05 10:00", "AI-assisted/content-mixed；逐段归属。")],
        time_rows=[("CU-0105-M01", "2026-01-05 10:00")],
        units=[_unit("CU-0105-M01", "S-0001", "2026-01-05", "混合材料原文片段。")],
        evolution_rows=[("2026-01-01 基线", "无", "`baseline`", "（无新增单元）")],
    )

    code, out = _run(monkeypatch, tmp_path, annotation, kb)

    assert code == 0, out
    assert "info unit CU-0105-M01" in out
    assert "RESULT: PASS" in out


def test_fail_whole_payload_validation(monkeypatch, tmp_path: Path) -> None:
    kb = _write_kb(tmp_path, {"s-0001.md": "内容。"})
    annotation = tmp_path / "annotation.md"
    annotation.write_text("不是合法标注文档", encoding="utf-8")

    code, out = _run(monkeypatch, tmp_path, annotation, kb)

    assert code == 1
    assert "FAIL whole-payload-validation" in out
