"""Cognition mainline rebuild: follow the G Working Set identity without drift."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
    _MANIFEST_NAME,
    CognitionMainlinePublisher,
    generate_cognition_mainline_readmodel,
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


def _write_manifest(path: Path, identity: str, *, status: str = "READY") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "g-working-set-manifest.v2",
                "status": status,
                "canonical_sha256": identity,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _publish_initial(readmodel_root: Path, annotation: Path, identity: str) -> None:
    payload = generate_cognition_mainline_readmodel(
        annotation,
        generation=1,
        working_set_identity=identity,
    )
    publication = CognitionMainlinePublisher(readmodel_root).publish(
        payload,
        expected_prior_identity=None,
    )
    assert publication.disposition == "PUBLISHED"
    # Establish the published annotation baseline (sidecar contract:
    # sha256(document name + b"\n" + document bytes) — restated here on
    # purpose so a fingerprint change breaks this helper loudly).
    digest = hashlib.sha256()
    digest.update(annotation.name.encode("utf-8"))
    digest.update(b"\n")
    digest.update(annotation.read_bytes())
    (readmodel_root / "annotation.sha256").write_text(
        digest.hexdigest() + "\n", encoding="utf-8"
    )


def _artifact_identity(readmodel_root: Path) -> str:
    return json.loads(
        (readmodel_root / _MANIFEST_NAME).read_text(encoding="utf-8")
    )["pit_working_set_identity"]


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_rebuild_skips_when_identity_is_current(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    identity = "a" * 64
    _write_manifest(manifest, identity)
    _publish_initial(readmodel_root, annotation, identity)
    before = _file_sha256(readmodel_root / _MANIFEST_NAME)

    result = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    assert result.disposition == "ALREADY_CURRENT"
    assert result.candidate_identity == identity
    assert result.trigger is None
    assert _file_sha256(readmodel_root / _MANIFEST_NAME) == before


def test_rebuild_skips_when_working_set_not_ready(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, "a" * 64, status="PARTIAL")
    _publish_initial(readmodel_root, annotation, "b" * 64)
    before = _file_sha256(readmodel_root / _MANIFEST_NAME)

    result = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    assert result.disposition == "SKIPPED"
    assert result.reason == "working_set_not_ready"
    assert _file_sha256(readmodel_root / _MANIFEST_NAME) == before


def test_rebuild_publishes_generation_plus_one_with_new_identity(
    tmp_path: Path,
) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    old_identity = "a" * 64
    new_identity = "b" * 64
    _write_manifest(manifest, new_identity)
    _publish_initial(readmodel_root, annotation, old_identity)

    result = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    assert result.disposition == "PUBLISHED"
    assert result.candidate_identity == new_identity
    assert result.generation == 2
    assert result.trigger == "identity_changed"
    artifact = json.loads(
        (readmodel_root / _MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert artifact["pit_working_set_identity"] == new_identity
    assert artifact["generation"] == 2
    assert (readmodel_root / "annotation.sha256").exists()


def test_rebuild_self_heals_when_sidecar_baseline_missing(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    identity = "a" * 64
    _write_manifest(manifest, identity)
    _publish_initial(readmodel_root, annotation, identity)
    (readmodel_root / "annotation.sha256").unlink()

    result = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    # Unknown baseline -> exactly one self-healing rebuild, then stable.
    assert result.disposition == "PUBLISHED"
    assert result.generation == 2
    assert result.trigger == "annotation_changed"
    assert (readmodel_root / "annotation.sha256").exists()
    again = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )
    assert again.disposition == "ALREADY_CURRENT"


def test_rebuild_fires_on_annotation_content_change(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    identity = "a" * 64
    _write_manifest(manifest, identity)
    _publish_initial(readmodel_root, annotation, identity)
    before = _file_sha256(readmodel_root / _MANIFEST_NAME)
    annotation.write_text(
        annotation.read_text(encoding="utf-8").replace("测试深化。", "测试深化（修订）。"),
        encoding="utf-8",
    )

    result = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    assert result.disposition == "PUBLISHED"
    assert result.generation == 2
    assert result.trigger == "annotation_changed"
    assert _file_sha256(readmodel_root / _MANIFEST_NAME) != before
    again = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )
    assert again.disposition == "ALREADY_CURRENT"


def test_rebuild_fires_on_rename_even_with_unchanged_content(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation-dated.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    identity = "a" * 64
    _write_manifest(manifest, identity)
    _publish_initial(readmodel_root, annotation, identity)

    renamed = tmp_path / "annotation.md"
    annotation.rename(renamed)

    result = rebuild_if_stale(
        annotation_path=renamed,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    # 设计门 P2（annotation_ref 悬挂）：改名即使内容不变也必须触发一次重建，
    # 让工件 annotation_ref 跟上真实文件名。
    assert result.disposition == "PUBLISHED"
    assert result.trigger == "annotation_changed"
    artifact = json.loads(
        (readmodel_root / _MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert artifact["annotation_ref"] == "manual-annotations/annotation.md"


def test_rebuild_reports_both_triggers(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, "b" * 64)
    _publish_initial(readmodel_root, annotation, "a" * 64)
    annotation.write_text(
        annotation.read_text(encoding="utf-8").replace("测试深化。", "测试深化（修订）。"),
        encoding="utf-8",
    )

    result = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    assert result.disposition == "PUBLISHED"
    assert result.trigger == "both"


def test_rebuild_skips_when_annotation_unreadable(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    identity = "a" * 64
    _write_manifest(manifest, identity)
    _publish_initial(readmodel_root, annotation, identity)
    before = _file_sha256(readmodel_root / _MANIFEST_NAME)
    annotation.unlink()

    result = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    assert result.disposition == "SKIPPED"
    assert result.reason == "annotation_unreadable"
    assert _file_sha256(readmodel_root / _MANIFEST_NAME) == before
    baseline = (readmodel_root / "annotation.sha256").read_text(encoding="utf-8")
    assert len(baseline.strip()) == 64  # baseline untouched by the skip


def test_failed_rebuild_does_not_write_baseline(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, "b" * 64)
    _publish_initial(readmodel_root, annotation, "a" * 64)
    (readmodel_root / "annotation.sha256").unlink()
    annotation.write_text("不是合法标注文档", encoding="utf-8")

    result = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    assert result.disposition == "FAILED"
    assert result.trigger == "both"  # identity moved + baseline sidecar missing
    assert not (readmodel_root / "annotation.sha256").exists()


def test_rebuild_fails_closed_on_invalid_annotation(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, "b" * 64)
    _publish_initial(readmodel_root, annotation, "a" * 64)
    before = _file_sha256(readmodel_root / _MANIFEST_NAME)
    annotation.write_text("不是合法标注文档", encoding="utf-8")

    result = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    assert result.disposition == "FAILED"
    assert result.reason is not None
    assert result.reason.startswith("annotation_invalid:")
    assert _file_sha256(readmodel_root / _MANIFEST_NAME) == before
    assert _artifact_identity(readmodel_root) == "a" * 64


def test_rebuild_skips_when_manifest_unreadable(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.cognition_mainline_rebuild import (
        rebuild_if_stale,
    )

    annotation = tmp_path / "annotation.md"
    _write_annotation(annotation)
    readmodel_root = tmp_path / "readmodel"
    manifest = tmp_path / "manifest.json"
    _publish_initial(readmodel_root, annotation, "a" * 64)
    manifest.write_text("{bad json", encoding="utf-8")

    result = rebuild_if_stale(
        annotation_path=annotation,
        readmodel_root=readmodel_root,
        manifest_path=manifest,
    )

    assert result.disposition == "SKIPPED"
    assert result.reason == "working_set_manifest_unreadable"
