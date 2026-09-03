"""Cognition mainline read-model: generator / validator / reader / publisher tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
    CognitionMainlineReadModelError,
    validate_cognition_mainline_readmodel,
)


def _minimal_payload() -> dict:
    """One minimally valid read-model payload (schema fin.cognition-mainline-readmodel/v1)."""
    return {
        "schema_version": "fin.cognition-mainline-readmodel/v1",
        "as_of": "2026-08-20T00:00:00+00:00",
        "generation": 1,
        "content_hash": "0" * 64,
        "annotation_ref": "manual-annotations/g-cognition-mainline.md",
        "available_at": "2026-08-19T12:47:00+00:00",
        "processed_at": "2026-08-20T00:00:00+00:00",
        "pit_working_set_identity": "a" * 64,
        "sources": [
            {
                "source_id": "S-0601",
                "article_ref": "knowledge-base/articles/20260601_c63be4709b7a.md",
                "published_at": "2026-06-01T14:17:00+00:00",
                "source_nature": "G_ORIGINAL",
            }
        ],
        "units": [
            {
                "unit_id": "CU-0601-01",
                "cognition_mode": "action_layer_not_cognition",
                "secondary_modes": [],
                "source_ref": "S-0601",
                "published_at": "2026-06-01T14:17:00+00:00",
                "observed_at": "2026-06-01T00:00:00+00:00",
                "effective_period": "当日异动/追涨语境",
                "forecast_window": "none_stated",
                "g_original_quote": "有利润点的才去抱龙，没有的可以去旅游。",
                "deepening_expression": "成本差异是主要条件之一。",
                "agent_reasoning": "无",
                "material_direction": "无",
                "material_action_guidance": "没有利润点则去旅游/不参与追涨，必要时模拟。",
                "agent_investment_choice": "无",
                "agent_trading_strategy": "无",
                "topics": ["风险", "利润垫"],
                "limitations": ["风险收益数字仅作为 G 的表达，未独立核验。"],
            }
        ],
        "evolution": [
            {
                "node": "2026-06 基线",
                "relative_prior": None,
                "change_type": "baseline",
                "kept": [],
                "added": ["CU-0601-01"],
                "authorship": "AGENT_REASONING_LABELED",
                "unit_refs": ["CU-0601-01"],
                "available_at": "2026-06-01T14:17:00+00:00",
                "accepted": True,
                "pit_working_set_identity": "a" * 64,
            }
        ],
    }


class TestValidatorClosedSets:
    """闭集枚举与必填字段的整份拒绝语义（任一失败整份拒绝，不部分发布）。"""

    def test_unknown_cognition_mode_rejected(self) -> None:
        payload = _minimal_payload()
        payload["units"][0]["cognition_mode"] = "not_a_cognition_mode"
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)

    def test_unknown_relation_rejected(self) -> None:
        payload = _minimal_payload()
        unit = payload["units"][0]
        unit["existing_evidence_summary"] = {
            "evidence_ref": "manifest.json#E-IDX",
            "available_at": "2026-08-20T04:23:00+00:00",
            "relation": "not_a_relation",
            "summary": "指数聚合",
            "limitations": [],
        }
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)

    def test_unknown_source_nature_rejected(self) -> None:
        payload = _minimal_payload()
        payload["sources"][0]["source_nature"] = "UNVERIFIED"
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)

    def test_unknown_change_type_rejected(self) -> None:
        payload = _minimal_payload()
        payload["evolution"][0]["change_type"] = "rewrite"
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)


class TestValidatorRequiredFields:
    def test_missing_top_level_required_rejected(self) -> None:
        payload = _minimal_payload()
        del payload["as_of"]
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)

    def test_missing_pit_field_rejected(self) -> None:
        payload = _minimal_payload()
        del payload["processed_at"]
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)

    def test_missing_unit_limitations_rejected(self) -> None:
        payload = _minimal_payload()
        del payload["units"][0]["limitations"]
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)


class TestValidatorTimeAndIdentity:
    def test_processed_at_before_available_at_rejected(self) -> None:
        payload = _minimal_payload()
        payload["processed_at"] = "2026-08-19T10:00:00+00:00"
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)

    def test_naive_datetime_rejected(self) -> None:
        payload = _minimal_payload()
        payload["sources"][0]["published_at"] = "2026-06-01T14:17:00"
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)

    def test_dangling_source_ref_rejected(self) -> None:
        payload = _minimal_payload()
        payload["units"][0]["source_ref"] = "S-9999"
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)


class TestValidatorPathSafety:
    """artifact validator 只接受 canonical knowledge-base refs，整份拒绝任何不安全形态。"""

    @staticmethod
    def _reject_article_ref(article_ref: str) -> None:
        payload = _minimal_payload()
        payload["sources"][0]["article_ref"] = article_ref
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)

    def test_absolute_article_ref_rejected(self) -> None:
        self._reject_article_ref(
            "/home/ypk/.local/share/fin-analyse/shared/knowledge-base/articles/20260730_x.md"
        )

    def test_wrapped_absolute_article_ref_rejected(self) -> None:
        """Markdown wrapper 包裹的绝对路径不得绕过 validator。"""
        self._reject_article_ref("`/home/private/outside.md`")

    def test_namespace_escape_ref_rejected(self) -> None:
        """`..` 越出 knowledge-base namespace 的相对路径整份拒绝。"""
        self._reject_article_ref("knowledge-base/../private.md")

    def test_dot_component_ref_rejected(self) -> None:
        """独立 `.` 组件不是 canonical 形态。"""
        self._reject_article_ref("knowledge-base/./x.md")

    def test_outside_namespace_ref_rejected(self) -> None:
        """namespace 外的相对路径整份拒绝。"""
        self._reject_article_ref("private/outside.md")

    def test_windows_drive_ref_rejected(self) -> None:
        """Windows drive/backslash 形态整份拒绝。"""
        self._reject_article_ref("C:\\private\\outside.md")


class TestValidatorAcceptance:
    def test_minimal_payload_accepted(self) -> None:
        result = validate_cognition_mainline_readmodel(_minimal_payload())
        assert result is not None

    def test_extra_top_level_field_rejected(self) -> None:
        payload = _minimal_payload()
        payload["invented_field"] = True
        with pytest.raises(CognitionMainlineReadModelError):
            validate_cognition_mainline_readmodel(payload)


_REPO_ROOT = Path(__file__).resolve().parents[2]
# canonical KB 根锚点（2026-09-03 归位迁移）：批注文档是 owner durable 数据，
# 不随仓走（knowledge-base/ 被 .gitignore，fresh checkout 会丢），读
# knowledge_root 缝解析的 canonical 根。缺文档时依赖它的用例整例跳过
# （生产 checkout 上恒存在）；_REPO_ROOT 不再是该文档的住址。
ANNOTATION_DOC = str(
    Path(
        os.environ.get("FIN_KNOWLEDGE_BASE_ROOT")
        or (Path.home() / ".local" / "share" / "fin-analyse" / "shared" / "knowledge-base")
    )
    / "manual-annotations"
    / "g-cognition-mainline.md"
)
needs_annotation_doc = pytest.mark.skipif(
    not Path(ANNOTATION_DOC).is_file(),
    reason="canonical annotation doc not present (knowledge root seam)",
)


def _write_valid_annotation(doc_path, article_ref_cell: str) -> None:
    """Write a fully valid annotation doc whose only variable part is the ref cell."""
    doc_path.write_text(
        "as_of=2026-01-02\n"
        "\n"
        "## 来源与边界验证\n"
        "\n"
        "| 来源 ID | 发表时间 | 可回指原文 | 来源性质与本轮用途 |\n"
        "| --- | --- | --- | --- |\n"
        f"| S-0001 | 2026-01-01 10:00 | {article_ref_cell} | G 原文；测试。 |\n"
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


class TestGenerator:
    """Generator: deterministic build from the manual-annotation markdown."""

    @needs_annotation_doc
    def test_builds_valid_readmodel_from_annotation_doc(self) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            generate_cognition_mainline_readmodel,
        )

        model = generate_cognition_mainline_readmodel(ANNOTATION_DOC)
        assert model["schema_version"] == "fin.cognition-mainline-readmodel/v1"
        assert model["generation"] == 1
        assert len(model["sources"]) == 19
        assert len(model["units"]) == 26
        assert len(model["evolution"]) == 4
        # 整份通过 validator（无损往返）
        validated = validate_cognition_mainline_readmodel(model)
        assert validated["content_hash"] == model["content_hash"]

    @needs_annotation_doc
    def test_generation_is_deterministic(self) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            generate_cognition_mainline_readmodel,
        )

        first = generate_cognition_mainline_readmodel(ANNOTATION_DOC)
        second = generate_cognition_mainline_readmodel(ANNOTATION_DOC)
        assert first == second

    @needs_annotation_doc
    def test_no_absolute_host_path_in_article_refs(self) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            generate_cognition_mainline_readmodel,
        )

        model = generate_cognition_mainline_readmodel(ANNOTATION_DOC)
        for source in model["sources"]:
            ref = source["article_ref"]
            assert not ref.startswith("/"), f"absolute article_ref leaked: {ref}"

    @needs_annotation_doc
    def test_unit_ids_are_unique_and_complete(self) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            generate_cognition_mainline_readmodel,
        )

        model = generate_cognition_mainline_readmodel(ANNOTATION_DOC)
        ids = [unit["unit_id"] for unit in model["units"]]
        assert len(ids) == len(set(ids))
        assert all(unit["limitations"] for unit in model["units"])

    def test_rejects_unmappable_absolute_path(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlineReadModelError,
            generate_cognition_mainline_readmodel,
        )

        doc = tmp_path / "annotation.md"
        doc.write_text(
            "## 来源与边界验证\n"
            "\n"
            "| 来源 ID | 发表时间 | 可回指原文 | 来源性质与本轮用途 |\n"
            "| --- | --- | --- | --- |\n"
            "| S-0001 | 2026-01-01 10:00 | "
            "/var/somewhere/unmappable/articles/x.md:1-2 | G 原文；测试。 |\n",
            encoding="utf-8",
        )
        with pytest.raises(CognitionMainlineReadModelError):
            generate_cognition_mainline_readmodel(str(doc))

    @needs_annotation_doc
    def test_article_refs_clean_no_wrappers_no_host_paths(self) -> None:
        """生产形状回归：19/19 曾带反引号、其中 6 个保留 /home/...，必须全部消除。"""
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            generate_cognition_mainline_readmodel,
        )

        model = generate_cognition_mainline_readmodel(ANNOTATION_DOC)
        assert len(model["sources"]) == 19
        for source in model["sources"]:
            ref = source["article_ref"]
            assert "`" not in ref and "*" not in ref, f"wrapped article_ref leaked: {ref!r}"
            assert not ref.startswith("/"), f"absolute article_ref leaked: {ref}"
            assert ref.startswith("knowledge-base/"), f"not repo-relative: {ref!r}"
            assert not any(part in {".", ".."} for part in ref.split("/")), ref

    @needs_annotation_doc
    def test_host_absolute_refs_mapped_to_repo_relative(self) -> None:
        """6 个来源原形为宿主绝对路径（含行号范围），必须映射为 canonical repo-relative ref。"""
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            generate_cognition_mainline_readmodel,
        )

        model = generate_cognition_mainline_readmodel(ANNOTATION_DOC)
        refs = {source["source_id"]: source["article_ref"] for source in model["sources"]}
        absolute_origin_ids = ("S-0730G", "S-0730M", "S-0810M", "S-0811A", "S-0811B", "S-0811C")
        assert {refs[sid] for sid in absolute_origin_ids} == {
            "knowledge-base/articles/20260730_zsxq-82255442558588260.md",
            "knowledge-base/articles/20260810_zsxq-45544211821188818.md",
            "knowledge-base/articles/20260811_zsxq-22255411148218111.md",
            "knowledge-base/articles/20260811_zsxq-22255411148128251.md",
            "knowledge-base/articles/20260811_zsxq-14422188814452552.md",
        }

    def test_wrapped_unmappable_absolute_path_fails_closed(self, tmp_path) -> None:
        """反引号包裹的绝对路径不得绕过绝对路径门；不可映射整份 fail closed。"""
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlineReadModelError,
            generate_cognition_mainline_readmodel,
        )

        # 其余 section 全部合法，唯一失败点必须是反引号包裹的不可映射绝对路径。
        doc = tmp_path / "annotation.md"
        _write_valid_annotation(doc, "`/var/somewhere/unmappable/articles/x.md:1-2`")
        with pytest.raises(CognitionMainlineReadModelError):
            generate_cognition_mainline_readmodel(str(doc))

    def test_windows_absolute_with_locator_fails_closed(self, tmp_path) -> None:
        """Windows drive 绝对路径不得被首个冒号切断成 `C`；必须整份 fail closed。"""
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlineReadModelError,
            generate_cognition_mainline_readmodel,
        )

        doc = tmp_path / "annotation.md"
        _write_valid_annotation(doc, "`C:\\private\\outside.md:15-33`")
        with pytest.raises(CognitionMainlineReadModelError):
            generate_cognition_mainline_readmodel(str(doc))

    def test_relative_traversal_ref_fails_closed(self, tmp_path) -> None:
        """相对 ref 含 `..` 越出 knowledge-base namespace → 整份 fail closed。"""
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlineReadModelError,
            generate_cognition_mainline_readmodel,
        )

        doc = tmp_path / "annotation.md"
        _write_valid_annotation(doc, "knowledge-base/../private.md:1-2")
        with pytest.raises(CognitionMainlineReadModelError):
            generate_cognition_mainline_readmodel(str(doc))

    def test_mapped_absolute_traversal_ref_fails_closed(self, tmp_path) -> None:
        """可映射绝对路径映射后越出 knowledge-base namespace → 整份 fail closed。"""
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlineReadModelError,
            generate_cognition_mainline_readmodel,
        )

        doc = tmp_path / "annotation.md"
        _write_valid_annotation(doc, "/home/ypk/shared/knowledge-base/../private.md:1-2")
        with pytest.raises(CognitionMainlineReadModelError):
            generate_cognition_mainline_readmodel(str(doc))


class TestPublisher:
    """Numeric-generation CAS publisher (disposition order is the contract)."""

    @staticmethod
    def _payload(generation: int) -> dict:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            generate_cognition_mainline_readmodel,
        )

        if not Path(ANNOTATION_DOC).is_file():
            pytest.skip("canonical annotation doc not present (knowledge root seam)")
        # 经公开 generator 构建：content_hash 与 canonical 内容一致（形状合法但内容
        # 不符的 payload 现在是发布门禁下的非法候选，见 CONTENT_HASH_MISMATCH）。
        return generate_cognition_mainline_readmodel(ANNOTATION_DOC, generation=generation)

    def test_first_publish_published(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlinePublisher,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        result = CognitionMainlinePublisher(root).publish(
            self._payload(1), expected_prior_identity="MISSING"
        )
        assert result.disposition == "PUBLISHED"

    def test_identical_retry_already_published(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlinePublisher,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        publisher = CognitionMainlinePublisher(root)
        candidate = self._payload(1)
        assert publisher.publish(candidate, expected_prior_identity="MISSING").disposition == (
            "PUBLISHED"
        )
        # 成功发布后的精确重试命中唯一 disposition：ALREADY_PUBLISHED（不判 generation）。
        retry = publisher.publish(candidate, expected_prior_identity=None)
        assert retry.disposition == "ALREADY_PUBLISHED"

    def test_generation_regression_rejected(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlinePublisher,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        publisher = CognitionMainlinePublisher(root)
        assert publisher.publish(
            self._payload(2), expected_prior_identity="MISSING"
        ).disposition == ("PUBLISHED")
        result = publisher.publish(self._payload(1), expected_prior_identity=None)
        assert result.disposition == "REJECTED"
        assert result.reason == "GENERATION_REGRESSION"

    def test_prior_drift_rejected(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlinePublisher,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        publisher = CognitionMainlinePublisher(root)
        assert publisher.publish(
            self._payload(1), expected_prior_identity="MISSING"
        ).disposition == ("PUBLISHED")
        result = publisher.publish(self._payload(2), expected_prior_identity="wrong-prior-sha")
        assert result.disposition == "REJECTED"
        assert result.reason == "PRIOR_DRIFT"

    @needs_annotation_doc
    def test_wrong_content_hash_candidate_rejected_healthy_kept(self, tmp_path) -> None:
        """错误 content_hash 的更高 generation 不得替换健康 artifact（fail closed）。"""
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlinePublisher,
            CognitionMainlineReadModelReader,
            generate_cognition_mainline_readmodel,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        publisher = CognitionMainlinePublisher(root)
        healthy = generate_cognition_mainline_readmodel(ANNOTATION_DOC, generation=1)
        assert publisher.publish(healthy, expected_prior_identity="MISSING").disposition == (
            "PUBLISHED"
        )

        # 更高 generation 但声明 hash 与 canonical 内容不符 → 整份拒绝，不得替换。
        corrupt = generate_cognition_mainline_readmodel(ANNOTATION_DOC, generation=2)
        corrupt["content_hash"] = "f" * 64  # 形状合法（64 hex）但与内容不符
        result = publisher.publish(corrupt, expected_prior_identity=None)
        assert result.disposition == "REJECTED"
        assert result.reason == "CONTENT_HASH_MISMATCH"

        # reader 仍读到原健康 generation；原子发布未落地，无临时文件残留。
        out = CognitionMainlineReadModelReader(root).read()
        assert out.failure_code is None
        assert out.generation == 1
        assert out.content_hash == healthy["content_hash"]
        assert sorted(p.name for p in root.iterdir()) == ["readmodel.v1.json"]

    @needs_annotation_doc
    def test_unsafe_article_ref_candidate_does_not_replace_healthy(self, tmp_path) -> None:
        """validator 整份拒绝不安全 ref 候选：不替换健康 artifact、不落盘。"""
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlinePublisher,
            CognitionMainlineReadModelError,
            CognitionMainlineReadModelReader,
            generate_cognition_mainline_readmodel,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        publisher = CognitionMainlinePublisher(root)
        healthy = generate_cognition_mainline_readmodel(ANNOTATION_DOC, generation=1)
        assert publisher.publish(healthy, expected_prior_identity="MISSING").disposition == (
            "PUBLISHED"
        )

        for ref in (
            "knowledge-base/../private.md",
            "`/home/private/outside.md`",
            "private/outside.md",
        ):
            corrupt = generate_cognition_mainline_readmodel(ANNOTATION_DOC, generation=2)
            corrupt["sources"][0]["article_ref"] = ref
            corrupt = _with_recomputed_hash(corrupt)  # hash 合法，ref 仍不安全
            with pytest.raises(CognitionMainlineReadModelError):
                publisher.publish(corrupt, expected_prior_identity=None)
            out = CognitionMainlineReadModelReader(root).read()
            assert out.failure_code is None, ref
            assert out.generation == 1, ref
        assert sorted(p.name for p in root.iterdir()) == ["readmodel.v1.json"]

    @needs_annotation_doc
    def test_unsafe_article_ref_candidate_creates_nothing(self, tmp_path) -> None:
        """空 store 上发布不安全 ref 候选：不创建新 artifact、不创建目录。"""
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlinePublisher,
            CognitionMainlineReadModelError,
            generate_cognition_mainline_readmodel,
        )

        root = tmp_path / "readmodel"  # 不存在
        corrupt = generate_cognition_mainline_readmodel(ANNOTATION_DOC, generation=1)
        corrupt["sources"][0]["article_ref"] = "private/outside.md"
        corrupt = _with_recomputed_hash(corrupt)
        with pytest.raises(CognitionMainlineReadModelError):
            CognitionMainlinePublisher(root).publish(corrupt, expected_prior_identity="MISSING")
        assert not root.exists()

    @needs_annotation_doc
    def test_raw_identical_retry_of_legacy_wrong_hash_artifact(self, tmp_path) -> None:
        """raw-identical 精确重试先于 hash 核对：遗留错误 hash artifact 重试仍 ALREADY_PUBLISHED。"""
        import json

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlinePublisher,
            generate_cognition_mainline_readmodel,
            validate_cognition_mainline_readmodel,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        corrupt = generate_cognition_mainline_readmodel(ANNOTATION_DOC, generation=1)
        corrupt["content_hash"] = "f" * 64  # 形状合法但与 canonical 内容不符
        # 模拟旧版本发布器直写的遗留 artifact（canonical JSON，hash 错误）。
        legacy_raw = json.dumps(
            validate_cognition_mainline_readmodel(corrupt),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest = root / "readmodel.v1.json"
        manifest.write_bytes(legacy_raw)
        manifest.chmod(0o600)

        result = CognitionMainlinePublisher(root).publish(corrupt, expected_prior_identity=None)
        assert result.disposition == "ALREADY_PUBLISHED"

    def test_reader_replaces_interleaving_keeps_single_artifact(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlinePublisher,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        publisher = CognitionMainlinePublisher(root)
        publisher.publish(self._payload(1), expected_prior_identity="MISSING")
        publisher.publish(self._payload(2), expected_prior_identity=None)
        manifest = list(root.iterdir())
        assert len(manifest) == 1  # 原子 replace，无 temp 残留


class TestReader:
    """Owner-only reader: canonical root, typed failure, no implicit creation."""

    @needs_annotation_doc
    def test_reads_published_payload(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlinePublisher,
            CognitionMainlineReadModelReader,
            generate_cognition_mainline_readmodel,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        payload = generate_cognition_mainline_readmodel(ANNOTATION_DOC)
        CognitionMainlinePublisher(root).publish(payload, expected_prior_identity="MISSING")
        out = CognitionMainlineReadModelReader(root).read()
        assert out.failure_code is None
        assert out.generation == payload["generation"]
        assert out.content_hash == payload["content_hash"]
        assert len(out.payload["units"]) == 26

    def test_missing_failure(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlineReadModelReader,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        out = CognitionMainlineReadModelReader(root).read()
        assert out.failure_code == "missing"

    def test_corrupt_failure(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlineReadModelReader,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        manifest = root / "readmodel.v1.json"
        manifest.write_text("{not json", encoding="utf-8")
        manifest.chmod(0o600)
        out = CognitionMainlineReadModelReader(root).read()
        assert out.failure_code == "corrupt"

    def test_hash_drift_failure(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlineReadModelReader,
        )

        root = tmp_path / "readmodel"
        root.mkdir(mode=0o700)
        payload = _minimal_payload()
        payload["content_hash"] = "f" * 64  # 与内容不符
        manifest = root / "readmodel.v1.json"
        manifest.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")
        manifest.chmod(0o600)
        out = CognitionMainlineReadModelReader(root).read()
        assert out.failure_code == "hash_drift"

    def test_does_not_implicitly_create_directory(self, tmp_path) -> None:
        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            CognitionMainlineReadModelReader,
        )

        root = tmp_path / "readmodel"  # 不存在
        out = CognitionMainlineReadModelReader(root).read()
        assert out.failure_code == "missing"
        assert not root.exists()


def _with_recomputed_hash(payload: dict) -> dict:
    """按模块 canonical 基准重算合法 content_hash（形状合法但内容不符 = 非法候选）。"""
    import hashlib
    import json

    projection = dict(payload)
    projection.pop("content_hash", None)
    raw = json.dumps(
        projection, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload["content_hash"] = hashlib.sha256(raw).hexdigest()
    return payload


def _generated() -> dict:
    from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
        generate_cognition_mainline_readmodel,
    )

    if not Path(ANNOTATION_DOC).is_file():
        pytest.skip("canonical annotation doc not present (knowledge root seam)")
    return generate_cognition_mainline_readmodel(ANNOTATION_DOC)


class TestProjector:
    """Pure G projection: PIT selector, G_ORIGINAL only, whole-unit eviction."""

    def test_accepts_current_as_of_with_g_original_only(self) -> None:
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _generated()
        out = project_cognition_mainline(
            payload,
            as_of=datetime(2026, 8, 20, tzinfo=timezone(timedelta(hours=8))),
            working_set_identity=payload["pit_working_set_identity"],
        )
        # 预算驱逐 gap 允许；PIT/identity gap 不允许。
        assert not any(gap.startswith("g_cognition_pit") for gap in out.data_gaps)
        assert len(out.items) >= 1
        # 只消费 G_ORIGINAL 来源单元（21 个 G 单元；5 个 mixed 不注入）。
        g_natures = {source["source_nature"] for source in payload["sources"]}
        assert "MIXED_PUBLISHED_REPORT" in g_natures
        assert "AI_ASSISTED_CONTENT_MIXED" in g_natures
        for item in out.items:
            assert item["source_bucket"] == "cognition_mainline_projection"
            assert item["source_refs"] == [item["source_ref"]]
            assert item["usage_boundary"] == "background_guidance_only_no_confidence_boost"

    def test_deterministic_same_revision_as_of(self) -> None:
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _generated()
        as_of = datetime(2026, 8, 20, tzinfo=timezone(timedelta(hours=8)))
        first = project_cognition_mainline(
            payload, as_of=as_of, working_set_identity=payload["pit_working_set_identity"]
        )
        second = project_cognition_mainline(
            payload, as_of=as_of, working_set_identity=payload["pit_working_set_identity"]
        )
        assert first == second

    def test_pit_revision_gate_early_as_of_gaps(self) -> None:
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _generated()
        # 早于 artifact.available_at（2026-08-19T12:47）→ 纯 typed gap，不倒灌。
        out = project_cognition_mainline(
            payload,
            as_of=datetime(2026, 8, 18, tzinfo=timezone(timedelta(hours=8))),
            working_set_identity=payload["pit_working_set_identity"],
        )
        assert out.items == ()
        assert "g_cognition_pit_artifact_not_available" in out.data_gaps

    def test_pit_identity_mismatch_gaps(self) -> None:
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _generated()
        out = project_cognition_mainline(
            payload,
            as_of=datetime(2026, 8, 20, tzinfo=timezone(timedelta(hours=8))),
            working_set_identity="b" * 64,
        )
        assert out.items == ()
        assert "g_cognition_pit_identity_mismatch" in out.data_gaps

    def test_pit_node_gate_filters_later_nodes(self) -> None:
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        # 受控 payload：revision 覆盖到 6/1，节点 N2 在 7/30 才可用；
        # as_of=7/1 时 revision gate 通过、N2 节点 gate 拒绝。
        payload = _minimal_payload()
        payload["available_at"] = "2026-06-01T14:17:00+08:00"
        payload["processed_at"] = "2026-06-01T14:17:00+08:00"
        payload["evolution"] = [
            {
                "node": "N1",
                "relative_prior": None,
                "change_type": "baseline",
                "kept": [],
                "added": [],
                "authorship": "AGENT_REASONING_LABELED",
                "unit_refs": ["CU-0601-01"],
                "available_at": "2026-06-01T14:17:00+08:00",
                "accepted": True,
                "pit_working_set_identity": None,
            },
            {
                "node": "N2",
                "relative_prior": "N1",
                "change_type": "increment",
                "kept": [],
                "added": [],
                "authorship": "AGENT_REASONING_LABELED",
                "unit_refs": ["CU-0601-01"],
                "available_at": "2026-07-30T14:17:00+08:00",
                "accepted": False,
                "pit_working_set_identity": None,
            },
        ]
        out = project_cognition_mainline(
            payload,
            as_of=datetime(2026, 7, 1, tzinfo=timezone(timedelta(hours=8))),
            working_set_identity=payload["pit_working_set_identity"],
        )
        assert out.items  # N1 可用 → 单元仍注入
        assert "g_cognition_pit_node_not_available" in out.data_gaps

    def test_whole_unit_eviction_never_cuts_quotes(self) -> None:
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _generated()
        out = project_cognition_mainline(
            payload,
            as_of=datetime(2026, 8, 20, tzinfo=timezone(timedelta(hours=8))),
            working_set_identity=payload["pit_working_set_identity"],
            budget_bytes=64,  # 极小预算 → 每个单元都超限 → 全部整单元驱逐
        )
        assert out.items == ()
        assert any(gap.startswith("g_cognition_unit_budget_evicted") for gap in out.data_gaps)
        # 不字符串切断：无 item 意味着没有半个单元文本。

    def test_latest_g_original_units_included_within_4096_budget(self) -> None:
        """生产症状回归：升序装填时 6 月单元耗尽预算、7/30/8/11/8/19 全被驱逐。"""
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _generated()
        out = project_cognition_mainline(
            payload,
            as_of=datetime(2026, 8, 20, tzinfo=timezone(timedelta(hours=8))),
            working_set_identity=payload["pit_working_set_identity"],
        )
        publishes = [item["published_at"] for item in out.items]
        # 默认 4096 预算必须包含最新适用节点（2026-08-19）的 G 单元。
        assert any(publish.startswith("2026-08-19") for publish in publishes), publishes
        # 不能再只得到 6 月旧单元。
        assert any(not publish.startswith("2026-06") for publish in publishes), publishes

    def test_items_ordered_latest_first_deterministic(self) -> None:
        """预算选择稳定确定：items 按 published_at 最新优先（无 topics 时的预算策略）。"""
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _generated()
        as_of = datetime(2026, 8, 20, tzinfo=timezone(timedelta(hours=8)))
        out = project_cognition_mainline(
            payload,
            as_of=as_of,
            working_set_identity=payload["pit_working_set_identity"],
        )
        publishes = [item["published_at"] for item in out.items]
        assert publishes == sorted(publishes, reverse=True)

    def test_bounded_budget_and_refs(self) -> None:
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _generated()
        out = project_cognition_mainline(
            payload,
            as_of=datetime(2026, 8, 20, tzinfo=timezone(timedelta(hours=8))),
            working_set_identity=payload["pit_working_set_identity"],
            budget_bytes=4096,
            max_refs=32,
        )
        total_bytes = sum(len(item["guidance_brief"].encode("utf-8")) for item in out.items)
        assert total_bytes <= 4096
        assert len(out.items) <= 32
        # G 原话与深化分列可区分
        for item in out.items:
            assert "G 原文" in item["guidance_brief"]
            assert "深化" in item["guidance_brief"]


def _topic_payload() -> dict:
    """Three G_ORIGINAL units: CU-A 算力(6/1), CU-B 其他(8/19), CU-C 其他(7/30)."""

    def unit(unit_id: str, source_id: str, published: str, quote: str) -> dict:
        return {
            "unit_id": unit_id,
            "cognition_mode": "current_observation",
            "secondary_modes": [],
            "source_ref": source_id,
            "published_at": published,
            "observed_at": "unknown/not stated",
            "effective_period": "测试",
            "forecast_window": "none_stated",
            "g_original_quote": quote,
            "deepening_expression": "测试深化",
            "agent_reasoning": "无",
            "material_direction": "无",
            "material_action_guidance": "无",
            "agent_investment_choice": "无",
            "agent_trading_strategy": "无",
            "topics": [],
            "limitations": [],
        }

    return {
        "schema_version": "fin.cognition-mainline-readmodel/v1",
        "as_of": "2026-08-20T00:00:00+00:00",
        "generation": 1,
        "content_hash": "0" * 64,
        "annotation_ref": "manual-annotations/test.md",
        "available_at": "2026-08-19T12:47:00+00:00",
        "processed_at": "2026-08-20T00:00:00+00:00",
        "pit_working_set_identity": "a" * 64,
        "sources": [
            {
                "source_id": "S-A",
                "article_ref": "knowledge-base/articles/a.md",
                "published_at": "2026-06-01T10:00:00+00:00",
                "source_nature": "G_ORIGINAL",
            },
            {
                "source_id": "S-B",
                "article_ref": "knowledge-base/articles/b.md",
                "published_at": "2026-08-19T12:47:00+00:00",
                "source_nature": "G_ORIGINAL",
            },
            {
                "source_id": "S-C",
                "article_ref": "knowledge-base/articles/c.md",
                "published_at": "2026-07-30T10:00:00+00:00",
                "source_nature": "G_ORIGINAL",
            },
        ],
        "units": [
            unit("CU-A", "S-A", "2026-06-01T10:00:00+00:00", "算力上游景气持续。"),
            unit("CU-B", "S-B", "2026-08-19T12:47:00+00:00", "市场参与者结构变化。"),
            unit("CU-C", "S-C", "2026-07-30T10:00:00+00:00", "无差别杀估值。"),
        ],
        "evolution": [
            {
                "node": "2026-06 基线",
                "relative_prior": None,
                "change_type": "baseline",
                "kept": [],
                "added": [],
                "authorship": "AGENT_REASONING_LABELED",
                "unit_refs": ["CU-A", "CU-B", "CU-C"],
                "available_at": "2026-06-01T00:00:00+00:00",
                "accepted": True,
                "pit_working_set_identity": "a" * 64,
            }
        ],
    }


class TestQuestionRelevance:
    """B 第一刀：cognition 按问题相关优先投影（无命中时保持 latest-first）。"""

    def test_question_relevance_orders_injection_ahead_of_latest(self) -> None:
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _topic_payload()
        as_of = datetime(2026, 8, 21, tzinfo=timezone(timedelta(hours=8)))
        relevant = project_cognition_mainline(
            payload,
            as_of=as_of,
            working_set_identity="a" * 64,
            budget_bytes=100_000,
            question="算力上游怎么看",
        )
        baseline = project_cognition_mainline(
            payload,
            as_of=as_of,
            working_set_identity="a" * 64,
            budget_bytes=100_000,
        )

        relevant_titles = [str(item["title"]) for item in relevant.items]
        baseline_titles = [str(item["title"]) for item in baseline.items]
        assert relevant_titles.index("G 认知单元 CU-A") < relevant_titles.index("G 认知单元 CU-B")
        assert baseline_titles.index("G 认知单元 CU-B") < baseline_titles.index("G 认知单元 CU-A")

    def test_empty_question_keeps_latest_first_regression(self) -> None:
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _topic_payload()
        as_of = datetime(2026, 8, 21, tzinfo=timezone(timedelta(hours=8)))
        explicit = project_cognition_mainline(
            payload,
            as_of=as_of,
            working_set_identity="a" * 64,
            budget_bytes=100_000,
            question="",
        )
        implicit = project_cognition_mainline(
            payload,
            as_of=as_of,
            working_set_identity="a" * 64,
            budget_bytes=100_000,
        )
        assert explicit == implicit

    def test_question_relevance_respects_budget_eviction(self) -> None:
        from datetime import datetime, timedelta, timezone

        from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
            project_cognition_mainline,
        )

        payload = _topic_payload()
        as_of = datetime(2026, 8, 21, tzinfo=timezone(timedelta(hours=8)))
        relevant = project_cognition_mainline(
            payload,
            as_of=as_of,
            working_set_identity="a" * 64,
            budget_bytes=400,
            question="算力上游怎么看",
        )
        assert [str(item["title"]) for item in relevant.items] == ["G 认知单元 CU-A"]
        assert "g_cognition_unit_budget_evicted" in relevant.data_gaps


def _projection_payload_with_quote(quote: str) -> dict:
    return {
        "available_at": "2026-08-19T12:47:00+00:00",
        "processed_at": "2026-08-19T12:47:00+00:00",
        "pit_working_set_identity": "w" * 64,
        "evolution": [
            {"available_at": "2026-08-19T12:47:00+00:00", "unit_refs": ["CU-1"]}
        ],
        "sources": [
            {
                "source_id": "S-1",
                "article_ref": "knowledge-base/articles/20260801_test.md",
                "source_nature": "G_ORIGINAL",
            }
        ],
        "units": [
            {
                "unit_id": "CU-1",
                "source_ref": "S-1",
                "published_at": "2026-08-01T10:00:00+00:00",
                "g_original_quote": quote,
                "deepening_expression": "",
                "observed_at": "2026-08-01",
                "effective_period": "当日",
                "forecast_window": "none_stated",
                "topics": [],
                "limitations": [],
            }
        ],
    }


def test_projection_attaches_jargon_notes_for_quote_terms() -> None:
    """NOW #14 下批：单元 G 逐字引句含黑话时，投影条目确定性附译注。"""
    from datetime import datetime, timedelta, timezone

    from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
        project_cognition_mainline,
    )

    payload = _projection_payload_with_quote("科技仓看科学家50和大光的量能。")
    out = project_cognition_mainline(
        payload,
        as_of=datetime(2026, 8, 20, tzinfo=timezone(timedelta(hours=8))),
        working_set_identity=payload["pit_working_set_identity"],
    )
    assert len(out.items) == 1
    notes = {n["term"]: n["meaning"] for n in out.items[0].get("jargon_notes", [])}
    assert notes["科学家50"] == "科创50"
    assert notes["大光"] == "光模块"


def test_projection_without_jargon_has_no_notes_key() -> None:
    from datetime import datetime, timedelta, timezone

    from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
        project_cognition_mainline,
    )

    payload = _projection_payload_with_quote("今天量能平淡，指数磨平。")
    out = project_cognition_mainline(
        payload,
        as_of=datetime(2026, 8, 20, tzinfo=timezone(timedelta(hours=8))),
        working_set_identity=payload["pit_working_set_identity"],
    )
    assert len(out.items) == 1
    assert "jargon_notes" not in out.items[0]
