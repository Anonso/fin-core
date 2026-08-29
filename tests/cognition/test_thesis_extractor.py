import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fin_analyse.cognition.llm import CognitionCompletionControl
from fin_analyse.cognition.thesis_extractor import (
    _LLM_EXTRACTION_PROMPT,
    LlmZsxqThesisExtractor,
    RuleBasedZsxqThesisExtractor,
)
from fin_analyse.cognition.zsxq_apprentice import load_zsxq_cognition_source
from fin_analyse.common.execution_control import ExecutionFence


class _JsonBackend:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def complete(self, _prompt: str) -> str:
        return json.dumps(self._payload)


def _source(tmp_path: Path, title: str, body: str, *, column: str = "星大派特刊"):
    article = tmp_path / "article.md"
    frontmatter = (
        f"---\nid: demo\ntopic_id: topic-demo\ndate: 2026-06-18 20:00\ncolumn: {column}\n---"
    )
    article.write_text(
        f"{frontmatter}\n\n# {title}\n\n{body}",
        encoding="utf-8",
    )
    return load_zsxq_cognition_source(article)


def _unit_payload() -> dict[str, object]:
    return {
        "unit_type": "strategic_thesis",
        "title": "存储盈利兑现",
        "thesis": "存储价格上行正在进入利润兑现阶段。",
        "evidence": "原文称存储价格上行并改善利润。",
        "interpretation": "学徒翻译：后续需要跟踪价格和利润兑现。",
        "confidence": 0.8,
        "topics": ["存储", "利润兑现"],
        "companies": [],
    }


def test_llm_extractor_threads_optional_bounded_control(tmp_path: Path):
    calls: list[dict[str, object]] = []

    class BoundedBackend:
        def complete(self, _prompt: str) -> str:
            raise AssertionError("controlled extraction used unbounded complete")

        def complete_bounded(self, _prompt: str, **kwargs: object) -> str:
            calls.append(kwargs)
            return json.dumps({"units": [_unit_payload()]})

    control = CognitionCompletionControl(
        fence=ExecutionFence(datetime.now(UTC) + timedelta(minutes=1)),
        checkpoint=lambda: None,
    )

    extraction = LlmZsxqThesisExtractor(llm=BoundedBackend()).extract(
        _source(tmp_path, "存储", "原文称存储价格上行并改善利润。"),
        control=control,
    )

    assert len(extraction.units) == 1
    assert len(calls) == 1


def test_keyword_only_article_yields_no_canned_units(tmp_path: Path):
    """罐头注入负例回归：仅关键词命中（卡脖子材料/去日化/钼前驱体等）且无
    verbatim 可提取内容时，规则抽取必须零产出——旧实现会注入 7 条硬编码
    evidence 非本文内容的单元（B2 盲评 08 样本实证）。"""
    source = _source(
        tmp_path,
        "星大派特刊：半导体AI卡脖子材料全面分析与评估报告",
        "核心机会：钼前驱体最具性价比；稀土氧化物（Y2O3/Dy2O3）地缘+需求双击；niche前驱体 α最大；WF6 已暴涨。\n排名：钼前驱体 14.5，Y2O3/Dy2O3 14，Niche前驱体 13.5，WF6 13。",
    )

    extraction = RuleBasedZsxqThesisExtractor().extract(source)

    assert extraction.units == []
    assert extraction.evidence_chains == []
    # 短正文会如实附 partial 警告；不得出现任何罐头单元
    assert extraction.warnings in (
        [],
        ["source appears partial; extracted units require confirmation"],
    )


def test_risk_brake_captures_verbatim_sentence(tmp_path: Path):
    """「不要上头」类风险刹车改为逐字句提取：evidence 必须是原文整句，
    thesis 由该句生成，不再使用罐头拼接（"不要上头 / 没有的别急 / 已有拿住"）。"""
    source = _source(
        tmp_path,
        "凤仙郡小故事：长期框架与风险边界",
        "老师提醒：已有的拿住，没有的别急，不要上头；长期框架仍要等事实验证。",
        column="凤仙郡小故事",
    )

    extraction = RuleBasedZsxqThesisExtractor().extract(source)

    brake_units = [unit for unit in extraction.units if unit.unit_type == "market_timing"]
    assert len(brake_units) == 1
    unit = brake_units[0]
    assert unit.original_evidence == ["老师提醒：已有的拿住，没有的别急，不要上头；"]
    assert "老师给出风险节奏表述" in unit.thesis
    assert "不要上头" in unit.thesis
    # 不得再出现罐头拼接 evidence
    assert unit.original_evidence != ["不要上头 / 没有的别急 / 已有拿住"]
    assert unit.related_topics == ["市场节奏", "风险刹车"]


def test_discipline_takes_priority_over_risk_brake(tmp_path: Path):
    """双命中句纪律优先（刻意）：同句同时含操作纪律与风险刹车触发词时
    仅产 methodology_rule，market_timing 不重复产出。"""
    source = _source(
        tmp_path,
        "星大派锐评：节奏与纪律",
        "现阶段大跌大补小跌小补，已有持仓的拿住不要上头。",
    )

    extraction = RuleBasedZsxqThesisExtractor().extract(source)

    types = sorted(unit.unit_type for unit in extraction.units)
    assert types == ["methodology_rule"]
    assert any("大跌大补" in unit.original_evidence[0] for unit in extraction.units)


def test_rule_extractor_captures_operation_discipline(tmp_path: Path) -> None:
    """The teacher's explicit operation discipline (大跌大补/小涨小卖) must be
    captured deterministically as a methodology_rule unit, regardless of LLM
    sampling."""
    source = _source(
        tmp_path,
        "星大派锐评：知道你们觉得大盘很痰",
        "本阶段可能最好的方案就是大跌大补，小跌小补，还有就是大涨大卖，小涨小卖。"
        "这个版本下，可能下周就4000，也可能9月第一周就4100。",
    )

    extraction = RuleBasedZsxqThesisExtractor().extract(source)

    discipline_units = [
        unit for unit in extraction.units if unit.unit_type == "methodology_rule"
    ]
    assert len(discipline_units) >= 1
    assert any("大跌大补" in unit.original_evidence[0] for unit in discipline_units)
    for unit in discipline_units:
        assert "不构成个股或时点买入建议" not in unit.apprentice_interpretation


def test_rule_extractor_does_not_invent_operation_discipline(tmp_path: Path) -> None:
    """Casual buy/sell wording without an explicit discipline phrase must not
    produce a fabricated methodology_rule unit."""
    source = _source(
        tmp_path,
        "星大派锐评",
        "市场今天高开低走，有同学问要不要买入，我只能说先观察，别急着上车。",
    )

    extraction = RuleBasedZsxqThesisExtractor().extract(source)

    assert all(unit.unit_type != "methodology_rule" for unit in extraction.units)


def test_extraction_prompt_permits_teacher_operation_discipline() -> None:
    """Prompt rule 4 must not suppress the teacher's own explicit operation
    discipline: it is a methodology_rule candidate, not system buy advice."""
    assert "大跌大补" in _LLM_EXTRACTION_PROMPT
    assert "不得因涉及买卖表述而跳过" in _LLM_EXTRACTION_PROMPT
    assert '不主动给出"买入"建议' in _LLM_EXTRACTION_PROMPT


def test_non_t0_source_is_not_extracted(tmp_path: Path):
    article = tmp_path / "plain.md"
    frontmatter = "---\nid: plain\ndate: 2026-06-18 08:55\ncolumn: 普通\n---"
    article.write_text(
        f"{frontmatter}\n\n# 精读研报\n\n半导体普通研报摘要。",
        encoding="utf-8",
    )
    source = load_zsxq_cognition_source(article)

    extraction = RuleBasedZsxqThesisExtractor().extract(source)

    assert extraction.units == []
    assert extraction.evidence_chains == []
    assert extraction.warnings == ["skip non-T0 ZSXQ cognition source"]


def test_llm_empty_top_level_list_is_a_valid_empty_extraction(tmp_path: Path):
    source = _source(tmp_path, "无可提取论点", "只有一条简短通知。")

    extraction = LlmZsxqThesisExtractor(llm=_JsonBackend([])).extract(source)

    assert extraction.units == []
    assert extraction.evidence_chains == []
    assert extraction.warnings == ["LLM found no extractable units"]


def test_llm_accepts_wrapped_and_top_level_unit_lists(tmp_path: Path):
    source = _source(tmp_path, "存储产业跟踪", "原文称存储价格上行并改善利润。")

    for payload in ({"units": [_unit_payload()]}, [_unit_payload()]):
        extraction = LlmZsxqThesisExtractor(llm=_JsonBackend(payload)).extract(source)

        assert [unit.title for unit in extraction.units] == ["存储盈利兑现"]
        assert extraction.units[0].original_evidence == ["原文称存储价格上行并改善利润。"]
        assert len(extraction.evidence_chains) == 1
        assert extraction.warnings == []


def test_llm_extracts_fengxianjun_teacher_source_with_source_neutral_prompt(tmp_path: Path):
    source = _source(
        tmp_path,
        "凤仙郡小故事：产业迁移的长期框架",
        "老师指出产业迁移要先验证竞争位置和兑现路径，不能把长期框架直接当作短线信号。",
        column="凤仙郡小故事",
    )

    class _RecordingBackend:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return json.dumps({"units": [_unit_payload()]})

    backend = _RecordingBackend()
    extraction = LlmZsxqThesisExtractor(llm=backend).extract(source)

    assert source.source_rank == "t0_fengxian"
    assert [unit.title for unit in extraction.units] == ["存储盈利兑现"]
    assert "老师原文 G 文章" in backend.prompts[0]
    assert "星大派老师" not in backend.prompts[0]


def test_llm_rejects_scalar_json_with_an_explicit_shape_error(tmp_path: Path):
    source = _source(tmp_path, "错误响应", "正文。")

    extraction = LlmZsxqThesisExtractor(llm=_JsonBackend(42)).extract(source)

    assert extraction.units == []
    assert extraction.evidence_chains == []
    assert extraction.warnings == [
        "LLM extraction failed: invalid JSON shape; "
        "expected an object with units or a top-level array"
    ]
    assert "None" not in extraction.warnings[0]


def test_llm_list_shape_does_not_bypass_item_or_evidence_validation(tmp_path: Path):
    source = _source(tmp_path, "无有效单元", "正文。")

    for payload in ([42], [{"title": "缺少原文证据"}]):
        extraction = LlmZsxqThesisExtractor(llm=_JsonBackend(payload)).extract(source)

        assert extraction.units == []
        assert extraction.evidence_chains == []
        assert extraction.warnings == ["LLM extraction produced no valid units"]


class _RetryJsonBackend:
    """First call returns non-JSON (parse failure), second returns the payload."""

    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.calls = 0

    def complete(self, _prompt: str) -> str:
        self.calls += 1
        if self.calls == 1:
            return "not valid json at all"
        return json.dumps(self._payload)


def test_llm_retries_once_on_json_parse_failure(tmp_path: Path):
    """JSON parse failed 时重试一次，可救回可用抽取（生产 5 篇空壳的修复）。"""
    source = _source(tmp_path, "存储产业跟踪", "原文称存储价格上行并改善利润。")

    backend = _RetryJsonBackend({"units": [_unit_payload()]})
    extraction = LlmZsxqThesisExtractor(llm=backend).extract(source)

    assert backend.calls == 2
    assert [unit.title for unit in extraction.units] == ["存储盈利兑现"]
    assert extraction.warnings == []


def test_llm_returns_empty_shell_after_retry_fails(tmp_path: Path):
    """重试仍失败 → 空壳 + warning（不做无界重试）。"""
    source = _source(tmp_path, "存储产业跟踪", "原文称存储价格上行。")

    backend = _RetryJsonBackend("still not json")
    extraction = LlmZsxqThesisExtractor(llm=backend).extract(source)

    assert backend.calls == 2
    assert extraction.units == []
    assert any("LLM extraction failed" in warning for warning in extraction.warnings)


# ── P0: 凤仙郡方法论规则单元(方案 A,提示词级)──────────────────────────


def test_prompt_declares_methodology_rule_unit_type_for_narrative_articles(
    tmp_path: Path,
) -> None:
    """验收1:提示词声明 methodology_rule 类型与叙事体提取规则。

    凤仙郡是叙事体小故事,既有 6 种 unit_type 全是论点/映射/时点型,LLM
    判"无内容可提"→ 空壳 → 语义门滤除。提示词必须给叙事体一个
    methodology_rule(规则性认知)出口。
    """
    source = _source(tmp_path, "凤仙郡小故事", "老师讲了一个故事。")

    class _CaptureBackend:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return json.dumps({"units": []})

    backend = _CaptureBackend()
    LlmZsxqThesisExtractor(llm=backend).extract(source)

    prompt = backend.prompts[0]
    assert "methodology_rule" in prompt
    assert "凤仙郡" in prompt or "叙事" in prompt or "小故事" in prompt


def test_methodology_rule_unit_passes_through_extraction(tmp_path: Path) -> None:
    """验收2:LLM 返回 methodology_rule 单元时透传为 InformationUnit。

    unit_type 是自由字符串(无枚举校验),新类型必须完整落位,不因类型
    陌生被丢弃;title/thesis/evidence 与既有单元同规则。
    """
    source = _source(
        tmp_path,
        "凤仙郡小故事:交易纪律",
        "老师借故事讲:频繁切换标的是大忌。",
        column="凤仙郡小故事",
    )
    rule_unit = {
        "unit_type": "methodology_rule",
        "title": "交易纪律",
        "thesis": "频繁切换标的是大忌,要守住纪律。",
        "evidence": "老师借故事讲频繁切换是大忌。",
        "interpretation": "学徒翻译:规则性认知,适用于交易纪律。",
        "confidence": 0.8,
        "topics": ["交易纪律", "凤仙郡"],
        "companies": [],
    }

    extraction = LlmZsxqThesisExtractor(llm=_JsonBackend({"units": [rule_unit]})).extract(source)

    assert len(extraction.units) == 1
    unit = extraction.units[0]
    assert unit.unit_type == "methodology_rule"
    assert unit.title == "交易纪律"
    assert unit.thesis == "频繁切换标的是大忌,要守住纪律。"
    assert "频繁切换" in unit.original_evidence[0]
    assert unit.related_topics == ["交易纪律", "凤仙郡"]
    assert extraction.warnings == []


def test_narrative_rule_only_applies_to_explicit_rule_stories(tmp_path: Path) -> None:
    """Minor6 回归:非叙事论述文章不因提示词被诱导标成 methodology_rule。

    验收4 的因果补充:提示词第 7 条限定"仅故事明确承载可迁移规则时",
    普通论述文章(战略/行业分析)不得产出 methodology_rule 单元。
    """
    source = _source(
        tmp_path,
        "星大派特刊:算力主线",
        "算力需求扩张带动光模块与铜连接放量,相关公司盈利有望兑现。",
        column="星大派特刊",
    )
    thesis_unit = _unit_payload()  # strategic_thesis

    extraction = LlmZsxqThesisExtractor(llm=_JsonBackend({"units": [thesis_unit]})).extract(source)

    assert len(extraction.units) == 1
    assert extraction.units[0].unit_type == "strategic_thesis"
    assert extraction.units[0].unit_type != "methodology_rule"


def test_prompt_rule_governs_extraction_behavior(tmp_path: Path) -> None:
    """Major3 因果:提示词规则文本存在与否改变提取行为。

    模拟"规则缺失"的 prompt(fake backend 检查提示词):无 methodology_rule
    规则 → LLM 判叙事体无内容可提 → 空壳;有规则 → 产出规则单元。
    证明提示词文本不是装饰,而是提取行为的因果输入。
    """
    source = _source(
        tmp_path,
        "凤仙郡小故事:交易纪律",
        "老师借故事讲频繁切换标的是大忌。",
        column="凤仙郡小故事",
    )
    rule_unit = {
        "unit_type": "methodology_rule",
        "title": "交易纪律",
        "thesis": "频繁切换标的是大忌。",
        "evidence": "频繁切换是大忌",
        "interpretation": "学徒翻译:规则性认知。",
        "confidence": 0.8,
        "topics": ["交易纪律"],
        "companies": [],
    }

    class _PromptGatedBackend:
        def complete(self, prompt: str) -> str:
            if "methodology_rule" in prompt and "明确表达的可迁移规则" in prompt:
                return json.dumps({"units": [rule_unit]})
            # 模拟规则缺失:叙事体无内容可提
            return json.dumps({"units": []})

    extraction = LlmZsxqThesisExtractor(llm=_PromptGatedBackend()).extract(source)

    assert len(extraction.units) == 1
    assert extraction.units[0].unit_type == "methodology_rule"


# ── P0 扩展: methodology_rule 覆盖特刊/锐评/老师原答的明确框架句 ─────────────


def test_prompt_declares_methodology_rule_for_special_report_frameworks(
    tmp_path: Path,
) -> None:
    """验收1(扩展):提示词覆盖特刊/锐评/老师原答的明确分析框架句,并保留泛化边界。

    用户需求是"G 的方法论"——特刊系统 thesis(如卡口分析、供需缺口)同样是
    老师验证过的分析框架,不能只给叙事体。直接断言提示词常量(不依赖文章标题)。
    """
    from fin_analyse.cognition.thesis_extractor import _LLM_EXTRACTION_PROMPT

    assert "methodology_rule" in _LLM_EXTRACTION_PROMPT
    # 来源覆盖:叙事体 + 特刊/锐评/老师原答的明确框架句
    assert "特刊" in _LLM_EXTRACTION_PROMPT or "锐评" in _LLM_EXTRACTION_PROMPT
    # 泛化边界保留:普通观点/建议不泛化
    assert "泛化" in _LLM_EXTRACTION_PROMPT


def test_methodology_rule_from_special_report_passes_through(tmp_path: Path) -> None:
    """验收2(扩展):特刊文章的明确框架句经 mock LLM 返回 → 透传 InformationUnit。"""
    source = _source(
        tmp_path,
        "星大派特刊:半导体卡口",
        "先识别卡口环节,再看供需缺口是否传导到价格。",
        column="星大派特刊",
    )
    rule_unit = {
        "unit_type": "methodology_rule",
        "title": "卡口分析",
        "thesis": "先识别卡口环节,再看供需缺口是否传导到价格。",
        "evidence": "先识别卡口环节,再看供需缺口是否传导到价格。",
        "interpretation": "学徒翻译:该框架可迁移到其他供需失衡板块。",
        "confidence": 0.8,
        "topics": ["半导体", "卡口"],
        "companies": [],
    }

    extraction = LlmZsxqThesisExtractor(llm=_JsonBackend({"units": [rule_unit]})).extract(source)

    assert len(extraction.units) == 1
    unit = extraction.units[0]
    assert unit.unit_type == "methodology_rule"
    assert unit.title == "卡口分析"
    assert "卡口环节" in unit.original_evidence[0]
    assert unit.related_topics == ["半导体", "卡口"]


# ── 时点分离: methodology_rule 不混入老师当时市场判断 ────────────────────────


def test_prompt_separates_time_sensitive_state_from_methodology(tmp_path: Path) -> None:
    """验收:提示词要求 rule 只写可迁移框架,时点判断(现在/今早等)不混入。"""
    from fin_analyse.cognition.thesis_extractor import _LLM_EXTRACTION_PROMPT

    assert "时点分离" in _LLM_EXTRACTION_PROMPT
    assert "老师时点判断" in _LLM_EXTRACTION_PROMPT


def test_extraction_prompt_requires_min_confidence_0_7() -> None:
    """宁缺毋滥：提取提示词置信门槛为 0.7。"""
    from fin_analyse.cognition.thesis_extractor import _LLM_EXTRACTION_PROMPT

    assert "低于0.7的不提取" in _LLM_EXTRACTION_PROMPT
    assert "低于0.6的不提取" not in _LLM_EXTRACTION_PROMPT
