from pathlib import Path

from fin_analyse.cognition.thesis_extractor import ThesisExtraction
from fin_analyse.cognition.zsxq_apprentice import ZsxqCognitionApprentice


class _NoOpLLMExtractor:
    def extract(self, source, visual_facts_text: str = "") -> ThesisExtraction:
        return ThesisExtraction([], [], [])


def test_deep_read_persists_golden_style_article(tmp_path: Path):
    """规则缝 verbatim 提取驱动持久化全链（units→clusters→clocks→suggestions）。

    原版本依赖罐头注入（关键词命中即得钼前驱体单元，evidence 非本文内容）；
    罐头删除后改用操作纪律句——规则缝仍确定性产出，evidence 逐字来自原文。
    """
    article = tmp_path / "2026-06-18_2000.md"
    frontmatter = "---\nid: 6421a58e9912\ntopic_id: 22255248888812480\ndate: 2026-06-18 20:00\ncolumn: 星大派特刊\n---"
    body = "本阶段可能最好的方案就是大跌大补，小跌小补，还有就是大涨大卖，小涨小卖。"
    article.write_text(
        f"{frontmatter}\n\n# 星大派特刊：节奏与纪律\n\n{body}",
        encoding="utf-8",
    )
    apprentice = ZsxqCognitionApprentice(runtime_root=tmp_path / "runtime")
    apprentice.llm_extractor = _NoOpLLMExtractor()

    result = apprentice.deep_read(article, now="2026-06-24T00:00:00")

    assert result.source.source_rank == "t0_xingdapai"
    assert any("操作纪律" in unit.title for unit in result.units)
    assert all(
        "大跌大补" in unit.original_evidence[0]
        for unit in result.units
        if unit.unit_type == "methodology_rule"
    )
    assert any(
        cluster.cluster_id == "cluster-general-zsxq-cognition"
        for cluster in result.theme_clusters
    )
    assert len(result.clocks) == len(result.units)
    assert len(result.suggestions) == len(result.units)
    assert all(
        "direct_buy_signal" in suggestion.forbidden_usage for suggestion in result.suggestions
    )

    assert (tmp_path / "runtime" / "zsxq_sources.jsonl").exists()
    assert (tmp_path / "runtime" / "information_units.jsonl").exists()
    assert (tmp_path / "runtime" / "research_suggestions.jsonl").exists()


def test_t0_keyword_article_yields_no_units_without_llm(tmp_path: Path):
    """罐头负例（pipeline 层）：T0 文章仅关键词命中、NoOp LLM 下必须零单元
    ——不再靠注入撑产出，也不因空结果崩溃；source 仍持久化。"""
    article = tmp_path / "2026-06-18_2100.md"
    frontmatter = "---\nid: aa11bb22\ntopic_id: 22255248888812499\ndate: 2026-06-18 21:00\ncolumn: 星大派特刊\n---"
    body = "核心机会：钼前驱体最具性价比；稀土氧化物（Y2O3/Dy2O3）地缘+需求双击；WF6 已暴涨。\n排名：钼前驱体 14.5，Y2O3/Dy2O3 14。"
    article.write_text(
        f"{frontmatter}\n\n# 星大派特刊：半导体AI卡脖子材料全面分析与评估报告\n\n{body}",
        encoding="utf-8",
    )
    apprentice = ZsxqCognitionApprentice(runtime_root=tmp_path / "runtime")
    apprentice.llm_extractor = _NoOpLLMExtractor()

    result = apprentice.deep_read(article, now="2026-06-24T00:00:00")

    assert result.source.source_rank == "t0_xingdapai"
    assert result.units == []
    assert result.evidence_chains == []
    assert result.theme_clusters == []
    assert len(apprentice.source_repo.list_all()) == 1


def test_deep_read_persists_fengxianjun_teacher_cognition(tmp_path: Path):
    article = tmp_path / "fengxian.md"
    frontmatter = "---\nid: fengxian1\ntopic_id: topic-fengxian\ndate: 2026-07-30 14:00\ncolumn: 凤仙郡小故事\n---"
    body = "老师提醒：已有的拿住，没有的别急，不要上头；长期框架仍要等事实验证。"
    article.write_text(
        f"{frontmatter}\n\n# 凤仙郡小故事：长期框架与风险边界\n\n{body}",
        encoding="utf-8",
    )
    apprentice = ZsxqCognitionApprentice(runtime_root=tmp_path / "runtime")
    apprentice.llm_extractor = _NoOpLLMExtractor()

    result = apprentice.deep_read(article, now="2026-08-01T00:00:00")

    assert result.source.source_rank == "t0_fengxian"
    assert result.units
    assert all("direct_buy_signal" in unit.usage_policy.forbidden_usage for unit in result.units)


def test_repeated_deep_read_is_stable(tmp_path: Path):
    article = tmp_path / "2026-06-22_1255.md"
    frontmatter = "---\nid: 8a33c4ccfd70\ntopic_id: 22255242885524140\ndate: 2026-06-22 12:55\ncolumn: 星大派特刊\n---"
    body = "本阶段可能最好的方案就是大跌大补，小跌小补，还有就是大涨大卖，小涨小卖。"
    article.write_text(
        f"{frontmatter}\n\n# 星大派特刊：节奏与纪律\n\n{body}",
        encoding="utf-8",
    )
    apprentice = ZsxqCognitionApprentice(runtime_root=tmp_path / "runtime")
    apprentice.llm_extractor = _NoOpLLMExtractor()

    first = apprentice.deep_read(article, now="2026-06-24T00:00:00")
    second = apprentice.deep_read(article, now="2026-06-24T00:00:00")

    assert [unit.unit_id for unit in first.units] == [unit.unit_id for unit in second.units]
    assert len(apprentice.unit_repo.list_all()) == len(first.units)


def test_external_context_article_persists_source_but_no_units(tmp_path: Path):
    article = tmp_path / "plain.md"
    frontmatter = "---\nid: plain\ndate: 2026-06-18 08:55\ncolumn: 普通\n---"
    article.write_text(
        f"{frontmatter}\n\n# 精读研报\n\n普通研报摘要。",
        encoding="utf-8",
    )
    apprentice = ZsxqCognitionApprentice(runtime_root=tmp_path / "runtime")

    result = apprentice.deep_read(article, now="2026-06-24T00:00:00")

    assert result.source.source_rank == "external_context"
    assert result.units == []
    assert result.warnings == ["skip non-T0 ZSXQ cognition source"]
