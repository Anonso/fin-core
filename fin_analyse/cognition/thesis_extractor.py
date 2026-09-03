"""Thesis extraction for high-priority ZSXQ teacher-original G articles."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from fin_analyse.cognition.llm import CognitionCompletionControl, CognitionLLM
from fin_analyse.cognition.models import (
    EvidenceChain,
    InformationUnit,
    UsagePolicy,
    ZsxqCognitionSource,
)
from fin_analyse.common.zsxq_jargon import jargon_note_lines
from fin_analyse.utils.ids import stable_id

logger = logging.getLogger(__name__)

_T0_TEACHER_SOURCE_RANKS = frozenset({"t0_xingdapai", "t0_fengxian"})


def _jargon_prompt_part(title: str, content: str) -> str:
    """「本文命中黑话对照」输入段（设计稿落点 1，只增强不作必译验收）。

    无命中返回空串（prompt 与旧版逐字节一致）。措辞经 2026-09-03 学徒
    翻译 A/B 验证：标准义只进 interpretation，evidence 保持逐字零污染。
    """
    lines = jargon_note_lines(f"{title}\n{content}")
    if not lines:
        return ""
    return (
        "# 本文命中黑话对照\n"
        + "\n".join(lines)
        + "\n（命中词可在学徒翻译中使用标准义；不得写入 evidence）"
    )


@dataclass(frozen=True)
class ThesisExtraction:
    units: list[InformationUnit]
    evidence_chains: list[EvidenceChain]
    warnings: list[str]


# ---------------------------------------------------------------------------
# Shared helpers — module-level so rule and LLM extractors can reuse them
# ---------------------------------------------------------------------------


def _make_information_unit(
    source: ZsxqCognitionSource,
    now: str,
    *,
    unit_type: str,
    title: str,
    thesis: str,
    evidence: str,
    interpretation: str,
    confidence: float,
    topics: list[str],
    companies: list[str] | None = None,
    extractor: str = "rule",
) -> InformationUnit:
    unit_id = stable_id("zsxq-unit", source.source_id, unit_type, title, thesis)
    return InformationUnit(
        unit_id=unit_id,
        source_id=source.source_id,
        teacher_id="guo",
        unit_type=unit_type,
        title=title,
        thesis=thesis,
        original_evidence=[evidence],
        apprentice_interpretation=interpretation,
        confidence=confidence,
        related_companies=list(companies or []),
        related_topics=topics,
        theme_cluster_ids=[],
        usage_policy=UsagePolicy.default_research_policy(),
        created_at=now,
        metadata={"source_column": source.column, "extractor": extractor},
    )


def _make_evidence_chain(
    source: ZsxqCognitionSource,
    unit: InformationUnit,
) -> EvidenceChain:
    chain_id = stable_id("zsxq-chain", unit.unit_id)
    return EvidenceChain(
        chain_id=chain_id,
        unit_id=unit.unit_id,
        original_claims=list(unit.original_evidence),
        original_source_refs=[source.article_path],
        apprentice_inferences=[unit.apprentice_interpretation],
        inference_confidence=unit.confidence,
        external_validations=[],
        counter_evidence=[],
        source_boundary_notes=["apprentice_interpretation is not teacher original wording"],
    )


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------

_OPERATION_DISCIPLINE_PATTERNS = (
    re.compile(r"大跌大补.{0,16}小跌小补.{0,16}大涨大卖.{0,16}小涨小卖"),
    re.compile(r"大跌大补|小跌小补|大涨大卖|小涨小卖"),
    re.compile(r"逢(?:跌|低)(?:分批)?(?:补仓|加仓|买入)|逢(?:涨|高)(?:分批)?(?:减仓|卖出)"),
    re.compile(r"分批(?:补仓|减仓|买入|卖出)"),
)

# 句内触发的风险刹车/节奏表述。旧实现是跨句布尔（「已有」AND「拿住」）
# 触发后注入罐头单元——确定性规则不得注入原文未连缀表达的论点，收窄为
# 同句触发属有意变更（跨句拆写场景由 LLM 缝兜底）。
_RISK_BRAKE_PATTERN = re.compile(r"不要上头|没有的别急|已有[^。！？；;\n]{0,16}拿住")


def _sentence_containing(text: str, start: int, end: int) -> str:
    """Return the sentence-delimited span of ``text`` containing ``[start, end)``."""
    begin = start
    while begin > 0 and text[begin - 1] not in "。！？；;!?\n":
        begin -= 1
    finish = end
    while finish < len(text) and text[finish] not in "。！？；;!?\n":
        finish += 1
    if finish < len(text) and text[finish] != "\n":
        finish += 1
    return text[begin:finish].strip()


class RuleBasedZsxqThesisExtractor:
    def extract(self, source: ZsxqCognitionSource) -> ThesisExtraction:
        if source.source_rank not in _T0_TEACHER_SOURCE_RANKS:
            return ThesisExtraction([], [], ["skip non-T0 ZSXQ cognition source"])

        text = "\n".join(
            [source.title, source.content, *source.image_descriptions, *source.image_ocr]
        )
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        units: list[InformationUnit] = []

        discipline_units = self._operation_discipline_units(source, text, now)
        units.extend(discipline_units)
        # 双命中句纪律优先（刻意）：同句同时含操作纪律与风险刹车触发词时
        # 仅产 methodology_rule，market_timing 不重复产出。
        units.extend(
            self._risk_brake_units(
                source,
                text,
                now,
                taken={
                    unit.original_evidence[0] for unit in discipline_units if unit.original_evidence
                },
            )
        )

        chains = [_make_evidence_chain(source, unit) for unit in units]
        warnings: list[str] = []
        if source.completeness == "partial":
            warnings.append("source appears partial; extracted units require confirmation")
        return ThesisExtraction(units, chains, warnings)

    def _operation_discipline_units(
        self,
        source: ZsxqCognitionSource,
        text: str,
        now: str,
    ) -> list[InformationUnit]:
        """Deterministically capture the teacher's explicit operation
        discipline (e.g. 大跌大补/小涨小卖) as a verbatim methodology_rule.

        The LLM extraction prompt historically suppressed buy/position
        wording, so explicit discipline sentences could be skipped entirely.
        This rule guarantees such sentences survive regardless of LLM
        sampling; evidence is quoted verbatim and never paraphrased.
        """
        sentences: list[str] = []
        for pattern in _OPERATION_DISCIPLINE_PATTERNS:
            for match in pattern.finditer(text):
                sentence = _sentence_containing(text, match.start(), match.end())
                if sentence and sentence not in sentences:
                    sentences.append(sentence)
        return [
            _make_information_unit(
                source,
                now,
                unit_type="methodology_rule",
                title="操作纪律",
                thesis=f"老师给出明确操作纪律：{sentence.rstrip('。！？；;')}。",
                evidence=sentence,
                interpretation=(
                    "学徒翻译：这是老师原文明确表达的操作纪律，逐字引用，"
                    "属于可迁移规则；按用户策略与风险边界执行。"
                ),
                confidence=0.8,
                topics=["操作纪律", "仓位管理"],
            )
            for sentence in sentences
        ]

    def _risk_brake_units(
        self,
        source: ZsxqCognitionSource,
        text: str,
        now: str,
        *,
        taken: set[str],
    ) -> list[InformationUnit]:
        """Deterministically capture the teacher's explicit risk-brake / pacing
        wording (不要上头 / 没有的别急 / 已有…拿住) as a verbatim market_timing
        unit.

        Evidence is the containing sentence quoted verbatim — never a canned
        thesis — so the unit only exists when the article actually carries the
        wording in one place.  Sentences already captured as operation-
        discipline rules are skipped (discipline wins, see extract()).
        """
        sentences: list[str] = []
        for match in _RISK_BRAKE_PATTERN.finditer(text):
            sentence = _sentence_containing(text, match.start(), match.end())
            if sentence and sentence not in sentences and sentence not in taken:
                sentences.append(sentence)
        return [
            _make_information_unit(
                source,
                now,
                unit_type="market_timing",
                title="老师风险刹车",
                thesis=f"老师给出风险节奏表述：{sentence.rstrip('。！？；;')}。",
                evidence=sentence,
                interpretation="学徒翻译：这是市场节奏和风险刹车，不应被翻译成追高信号。",
                confidence=0.72,
                topics=["市场节奏", "风险刹车"],
            )
            for sentence in sentences
        ]


# ---------------------------------------------------------------------------
# LLM-enhanced extractor
# ---------------------------------------------------------------------------

_LLM_EXTRACTION_PROMPT = """你是一个金融投研认知助手，正在分析一篇老师原文 G 文章。

从以下文章内容和图片结构化事实中提取关键信息单元（InformationUnit）。
每篇文章通常有 1-5 个信息单元。多个互不相同的独立论点（不同标的、不同
机制、不同结论）应分别成单元，不得为省事并入单一单元。

## 提取方法（先摘录，后构造——两步缺一不可）
第一步：先从原文（正文/图片OCR/图片描述）逐字摘录承载实质观点、规则或
判断的句子（3-10 条）。老师的推测口吻（"我更相信""大概率"）不影响摘录。
第二步：只对这些摘录句构造信息单元，每个单元的 evidence 必须原样引用
其中一条或相邻几条摘录句，不得改写、拼接或超出摘录集。

## 字段说明
- unit_type: strategic_thesis | industry_map | company_mapping | market_timing | risk_signal | catalyst_observation | methodology_rule
  （risk_signal 仅用于风险刹车、回撤、节奏类信号；供需结构转移、产业链
  格局类论断用 strategic_thesis 或 industry_map，不要标成 risk_signal）
- title: 10字以内的标题
- thesis: 核心论点（1-2句）
- evidence: 支撑该论点的原文证据（1-2句，必须来自原文，原样引用摘录句）
- interpretation: 认知学徒的理解和推演（标注"学徒翻译：..."，区分于老师原话）；
  解释只能基于摘录句内的信息，不得引入原文没有的方法论、推断或结论
- confidence: 0-1之间的引用忠实度——衡量该表述在原文中的明确程度和你的
  引用是否逐字，而不是观点本身为真的概率；老师以推测口吻表达的判断
  照常提取，推测语气记入 interpretation（标注"老师时点判断"或"推测口吻"）；
  低于0.7的不提取
- topics: 相关主题关键词列表（须同时包含文章涉及的板块/行业词（如半导体、
  稀土）与概念词（如操作纪律、概率评估），便于命中不同提问方式）
- companies: 涉及的公司名称列表（无则为空数组）

## 重要规则
1. 只提取文章实际讨论的内容，不编造
2. evidence 必须是文章中出现的信息
3. interpretation 必须标注"学徒翻译："前缀，明确这不是老师原话；
   无摘录句支撑的内容不得写进 interpretation
4. 不主动给出"买入"建议、不主动给出仓位建议；但老师原文中明确表达的
   操作纪律/买卖纪律（如"大跌大补，小跌小补，大涨大卖，小涨小卖"、
   逢跌分批补仓/逢涨分批减仓）必须按原文提取为 methodology_rule，
   evidence 逐字引用，不得因涉及买卖表述而跳过
5. 如果文章内容不足以提取任何单元，返回 {"units": [], "empty_reason":
   "<一句话原因>"}——空返回是异常结果，必须先完成两步提取仍无实质
   内容才允许（纯闲聊/纯转发无观点是唯一合法空返回）
6. 关注表格中的数字、百分比和对比数据；关键量化锚点（金额、比例、价格、
   日期）必须随其论点进入 evidence 或 thesis，不得整条丢弃；概念定义句
   （"X 即/是指/等于 Y"）是高价值摘录对象，优先摘录
7. methodology_rule 用于老师明确表达的可迁移规则性认知（分析框架、
   认知规则、操作纪律、博弈规则），来源包括：
   - 叙事体/故事体文章（如凤仙郡小故事）中故事承载的隐喻规则；
   - 星大派特刊/锐评/老师原答中明确的分析框架句（如"先识别卡口环节，
     再看供需缺口是否传导到价格"这类系统性方法论表述）。
   - 老师原文中明确的操作纪律/买卖纪律（如"大跌大补，小跌小补，大涨大卖，
     小涨小卖"、逢跌分批补仓/逢涨分批减仓等逐字规则）。
   只提取老师明确表达的框架，不得把普通观点、建议或结论泛化为
   methodology_rule（普通论述仍用 strategic_thesis）；evidence 必须
   引用支撑该框架的原文片段。
   时点分离：thesis/rule 只写可迁移的框架本身（如"先观察市场能否出现
   统领核心主线"）；若原文同时包含老师对当时市场的判断（如"现在没有
   总龙""今早割了多头"这类具体时点状态），不得把时点判断混入规则——
   时点判断属于当时的观点而非长期方法，可放入 interpretation 并标注
   "老师时点判断"，或直接省略；含明确时点状态的规则若框架不完整则
   整体不提取。

## 输入格式
文章标题、正文内容、图片描述、图片结构化事实会依次给出。

始终返回 JSON 对象: {"units": [{"unit_type": "...", "title": "...", "thesis": "...", "evidence": "...", "interpretation": "...", "confidence": 0.8, "topics": ["..."], "companies": ["..."]}]}
（仅当完成两步提取仍无实质内容时: {"units": [], "empty_reason": "..."}）
"""


# 中心思想兜底（deep-read-unlock-20260819）：主提取空时提炼中心思想。
# 目的（用户拍板）：准确理解原文信息、减少噪音、提高文字信息密度和质量。
_CENTRAL_IDEA_UNIT_TYPES = frozenset(
    {"strategic_thesis", "market_timing", "risk_signal", "methodology_rule"}
)
#: 与 deep_read_artifacts 的 freshness 拒绝规则一致（pair fresh 判定）。
_CENTRAL_IDEA_RETRYABLE_WARNING_RE = re.compile(
    r"\bLLM extraction (?:failed|error)\b", re.IGNORECASE
)

_CENTRAL_IDEA_PROMPT = """你是一个金融投研认知助手。主提取未能从老师原文中提取出结构化信息单元
（原文口语化、情绪化或泛化）。请提炼这篇文章的中心思想，提高信息密度与质量。

## 要求
1. 准确理解原文信息：只提炼原文实际表达的内容，不编造、不泛化原文没有的内容；
   概念定义句与关键量化锚点优先保留
2. thesis（核心判断）：1 句话概括老师对当前问题的核心判断
3. interpretation（关键要点）：以"学徒翻译："开头，写 2-3 条要点说明判断依据
4. evidence（原文证据）：必须逐字引用原文中的原句（1-2 句），不得改写、不得拼接
5. unit_type：从 strategic_thesis | market_timing | risk_signal | methodology_rule 中选择；
   methodology_rule 仅当原文包含明确可迁移的分析框架/操作纪律时使用
6. confidence：0-1 之间的引用忠实度（该表述在原文中的明确程度与引用是否
   逐字，非观点为真概率；老师推测口吻照常提炼，语气记入 interpretation），
   低于 0.7 不要返回
7. topics：相关主题关键词（板块/行业词与概念词）
8. companies：涉及的公司名称（无则为空数组）

## 输入格式
文章标题、正文内容依次给出。

始终返回 JSON 对象: {"unit_type": "...", "title": "10字以内的标题", "thesis": "...",
"evidence": "...", "interpretation": "...", "confidence": 0.8, "topics": ["..."],
"companies": ["..."]}
"""


def replace_central_idea_warnings(warnings: list[str]) -> list[str]:
    """Replace retryable/empty-extraction warnings with a central-idea marker.

    The retryable classes are exactly the ones DeepReadArtifactService rejects
    when deciding pair freshness; a successful central idea must clear them so
    the artifact pair stays fresh. Other warnings (e.g. vision) are preserved.
    """
    kept = [
        warning
        for warning in warnings
        if not _CENTRAL_IDEA_RETRYABLE_WARNING_RE.search(warning)
        and "LLM found no extractable units" not in warning
        and "LLM backend unavailable" not in warning
    ]
    return kept + ["central_idea_extracted"]


def _strip_noise(text: str) -> str:
    """去空白与中文标点，用于 evidence 与原文的宽松子串比对。"""
    import unicodedata

    return "".join(
        ch
        for ch in unicodedata.normalize("NFKC", text)
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def _evidence_domain(source: ZsxqCognitionSource) -> str:
    """主链 evidence 校验域：LLM 实际所见的可引用文本全集
    （正文 + 图片OCR + 图片描述）。visual_facts 是模型生成的结构化
    转述而非可逐字引用的原文，不进校验域。"""
    parts = [source.content, *source.image_ocr, *source.image_descriptions]
    return "\n".join(part for part in parts if part)


#: 引句拼接时残留的纯连接词/序号片段（docstring 承诺「非相邻句各句忠实
#: 即可接受」，但 "引句A"以及"引句B" 的连接词会粘进相邻分段且必不在原文，
#: 把整条忠实 evidence 误杀——2026-08-30 新旧产物 diff 实证 2 条）。
_CONNECTOR_FRAGMENTS = frozenset(
    {"以及", "同时", "另外", "并且", "还有", "然后", "而且", "再", "及"}
)
_STITCHED_QUOTE_CONNECTOR_RE = re.compile(
    r"[”\"’']\s*(?:以及|同时|另外|并且|还有|然后|而且|及|再)\s*[“\"‘']"
)


def _is_connector_fragment(fragment: str) -> bool:
    clean = _strip_noise(fragment)
    return (not clean) or clean.isdigit() or clean in _CONNECTOR_FRAGMENTS


def _evidence_in_source(evidence: str, content: str) -> bool:
    """Deterministic check that the central-idea evidence verbatim appears in
    the source content (whitespace/punctuation-tolerant).

    Evidence may quote 1-2 original sentences.  Fragments are split on line
    breaks and sentence-ending punctuation; every fragment must appear as a
    contiguous verbatim substring of the source.  Non-adjacent sentences are
    therefore accepted only when each quoted sentence is itself faithful.
    Pure connector/numbering remnants between quoted sentences are ignored.
    """
    clean_content = _strip_noise(content)
    # 引号边界连接词先归一为句界，再走分段校验
    evidence = _STITCHED_QUOTE_CONNECTOR_RE.sub("。", evidence)
    fragments = [
        fragment.strip()
        for fragment in re.split(r"[\n。！？；;!?]+", evidence)
        if fragment.strip() and not _is_connector_fragment(fragment)
    ]
    if not fragments:
        return False
    return all(
        bool(clean_fragment) and clean_fragment in clean_content
        for clean_fragment in (_strip_noise(fragment) for fragment in fragments)
    )


def _central_idea_failure_code(result: object) -> str:
    """Map an LLM completion failure to a content-free reason code."""
    error = str(getattr(result, "error", "") or "")
    if error.startswith("LLM backend unavailable"):
        return "backend_unavailable"
    if "empty response" in error:
        return "llm_empty"
    if error.startswith("JSON parse failed"):
        return "json_parse_failed"
    return "llm_failed"


class LlmZsxqThesisExtractor:
    """LLM-based thesis extractor using structured output.

    Uses CognitionLLM to extract InformationUnits from ZSXQ articles,
    with visual facts as additional context. Falls back to empty
    extraction on any error — the caller should merge with rule-based
    results.
    """

    def extract_central_idea(
        self,
        source: ZsxqCognitionSource,
        *,
        control: CognitionCompletionControl | None = None,
    ) -> tuple[InformationUnit | None, str | None]:
        """Extract one central-idea unit when the main extraction was empty.

        Quality gates (deep-read-unlock-20260819 design v4):
        - confidence >= 0.7
        - evidence verifiably quoted from the source content
        - unit_type in the supported subset
        - interpretation carries the "学徒翻译：" prefix

        Returns ``(unit, None)`` on success and ``(None, failure_code)`` on
        rejection; ``failure_code`` is a content-free typed reason so callers
        can surface why the fallback failed.  Never raises.
        """
        if source.source_rank not in _T0_TEACHER_SOURCE_RANKS:
            return None, "source_rank_unsupported"
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        parts: list[str] = [
            f"# 文章标题\n{source.title}",
            f"# 文章内容\n{source.content[:4000]}",
        ]
        jargon_part = _jargon_prompt_part(source.title, source.content[:4000])
        if jargon_part:
            parts.insert(2, jargon_part)
        prompt = _CENTRAL_IDEA_PROMPT + "\n---\n" + "\n\n".join(parts)
        try:
            if control is not None:
                control.checkpoint_or_raise()
            llm = self._get_llm()
            result = llm.complete_json(
                prompt,
                expected_type="CentralIdea",
                control=control,
            )
            if control is not None:
                control.checkpoint_or_raise()
        except Exception as exc:
            logger.warning("Central-idea extraction failed: %s", exc)
            return None, "llm_exception"
        if not result.ok:
            return None, _central_idea_failure_code(result)
        if not isinstance(result.data, dict):
            return None, "invalid_shape"
        item = result.data
        try:
            unit_type = str(item.get("unit_type", ""))
            title = str(item.get("title", "")).strip()
            thesis = str(item.get("thesis", "")).strip()
            evidence = str(item.get("evidence", "")).strip()
            interpretation = str(item.get("interpretation", "")).strip()
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            return None, "field_type_invalid"
        if unit_type not in _CENTRAL_IDEA_UNIT_TYPES:
            return None, "unit_type_unsupported"
        if not (title and thesis and evidence and interpretation):
            return None, "missing_field"
        if confidence < 0.7:
            return None, "low_confidence"
        if not _evidence_in_source(evidence, source.content):
            return None, "evidence_not_verbatim"
        if not interpretation.startswith("学徒翻译："):
            interpretation = f"学徒翻译：{interpretation}"
        return (
            _make_information_unit(
                source,
                now,
                unit_type=unit_type,
                title=title[:50],
                thesis=thesis,
                evidence=evidence,
                interpretation=interpretation,
                confidence=confidence,
                topics=_as_str_list(item.get("topics", [])),
                companies=_as_str_list(item.get("companies", [])),
                extractor="central_idea",
            ),
            None,
        )

    def __init__(self, llm: object | None = None) -> None:
        self._llm = llm

    def _get_llm(self) -> CognitionLLM:
        from fin_analyse.cognition.llm import CognitionLLM

        if self._llm is not None:
            return CognitionLLM(backend=self._llm)
        return CognitionLLM.from_config(preferred=_cognition_preferred())

    def _get_llm_chain(self) -> list[CognitionLLM]:
        """主提取的升级链：按 priorities.cognition 排序的可用 backend。

        注入测试 backend 时链长 1（升级仍在同 backend 上以重试指令进行）。
        """
        from fin_analyse.cognition.llm import CognitionLLM

        if self._llm is not None:
            return [CognitionLLM(backend=self._llm)]
        from fin_analyse.claims.config_loader import create_backends_from_config

        backends = create_backends_from_config()
        chain = [
            CognitionLLM(backend=backends[name])
            for name in _cognition_preferred()
            if name in backends
        ]
        if not chain:
            for backend in backends.values():
                chain.append(CognitionLLM(backend=backend))
                break
        return chain

    def extract(
        self,
        source: ZsxqCognitionSource,
        *,
        visual_facts_text: str = "",
        control: CognitionCompletionControl | None = None,
    ) -> ThesisExtraction:
        """Extract information units using LLM.

        Returns empty ThesisExtraction on any error — never raises.
        """
        if source.source_rank not in _T0_TEACHER_SOURCE_RANKS:
            return ThesisExtraction([], [], ["skip non-T0 ZSXQ cognition source"])

        now = datetime.now(UTC).replace(microsecond=0).isoformat()

        # Build prompt
        parts: list[str] = [
            f"# 文章标题\n{source.title}",
            f"# 文章内容\n{source.content[:4000]}",
        ]
        jargon_part = _jargon_prompt_part(source.title, source.content[:4000])
        if jargon_part:
            parts.insert(2, jargon_part)
        if source.image_descriptions:
            parts.append("# 图片描述\n" + "\n".join(source.image_descriptions[:5]))
        if visual_facts_text:
            parts.append(visual_facts_text)
        elif source.image_ocr:
            parts.append("# 图片OCR文字\n" + "\n".join(source.image_ocr[:5]))

        prompt = _LLM_EXTRACTION_PROMPT + "\n---\n" + "\n\n".join(parts)

        # 链式升级（2026-08-30 实证）：各 provider 对该任务存在采样级
        # 「合法空数组」坍塌（同 prompt/内容/backend 空率 ~5/8，跨 GLM/
        # deepseek/qwen），且空返回不带 empty_reason 时与真坍塌不可区分。
        # 策略：bare-empty → 同 backend 追加重试指令一次 → 换链上下一
        # backend；带 empty_reason 的合法空即停（成本护栏）；硬失败/坏形
        # 保持既有单次语义。
        result = None
        exhausted_bare_empty = True
        sentinel_details: list[str] = []
        try:
            for llm in self._get_llm_chain():
                for attempt_prompt in (prompt, prompt + _EMPTY_ESCALATION_NUDGE):
                    if control is not None:
                        control.checkpoint_or_raise()
                    result = llm.complete_json(
                        attempt_prompt,
                        expected_type="ThesisUnits",
                        control=control,
                    )
                    if control is not None:
                        control.checkpoint_or_raise()
                    if not result.ok:
                        # 有界重试一次：LLM 偶发返回空/非 JSON（JSON parse
                        # failed），重试可救回可用抽取；仍失败才交失败语义。
                        result = llm.complete_json(
                            attempt_prompt,
                            expected_type="ThesisUnits",
                            control=control,
                        )
                        if control is not None:
                            control.checkpoint_or_raise()
                    if not result.ok:
                        exhausted_bare_empty = False
                        break
                    data = result.data
                    units_probe = data.get("units", []) if isinstance(data, dict) else data
                    if not isinstance(units_probe, list) or units_probe:
                        exhausted_bare_empty = False
                        break
                    if isinstance(data, dict) and str(data.get("empty_reason", "") or "").strip():
                        exhausted_bare_empty = False  # 合法空：带因即停
                        break
                    backend_failure = getattr(getattr(llm, "backend", None), "last_failure", None)
                    if backend_failure is not None:
                        # 后端失败哨兵：complete()/complete_bounded() 重试耗尽时
                        # 返回字面 "[]" 且 last_failure 非空——与模型真产空数组
                        # 不可分，last_failure 即硬失败证据。同 backend 的 nudge
                        # 不解决服务端故障，留证后直接换链下一 backend。
                        sentinel_details.append(_sentinel_summary(backend_failure))
                        break
                    # bare-empty：继续（同 backend 重试指令 → 下一 backend）
                if not exhausted_bare_empty:
                    break
        except Exception as exc:
            logger.warning("LLM thesis extraction failed: %s", exc)
            return ThesisExtraction([], [], [f"LLM extraction error: {exc}"])
        if result is None:
            # 空链守卫：全部 backend 熔断/不可用时链为空，循环零迭代。
            # 返回与旧 backend-unavailable 语义一致的 typed 失败（命中
            # retryable 契约，后续排空可重试），绝不带 None 走 .ok。
            return ThesisExtraction([], [], ["LLM extraction failed: LLM backend unavailable"])
        if exhausted_bare_empty and sentinel_details:
            # 全链以硬失败哨兵收尾：不能落「合法空」的不可重试语义；产出
            # typed retryable 失败（命中 LLM extraction failed 正则），排空/
            # 重生成后续可补做。
            return ThesisExtraction([], [], [f"LLM extraction failed: {sentinel_details[-1]}"])
        if exhausted_bare_empty:
            logger.warning(
                "LLM empty extraction persisted across escalation chain (%s)",
                source.title[:40],
            )

        if not result.ok:
            error = result.error or "unknown LLM completion error"
            return ThesisExtraction([], [], [f"LLM extraction failed: {error}"])

        if isinstance(result.data, dict):
            units_data = result.data.get("units", [])
        elif isinstance(result.data, list):
            units_data = result.data
        else:
            return ThesisExtraction(
                [],
                [],
                [
                    "LLM extraction failed: invalid JSON shape; "
                    "expected an object with units or a top-level array"
                ],
            )

        if not isinstance(units_data, list):
            return ThesisExtraction(
                [],
                [],
                ["LLM extraction failed: invalid units shape; expected an array"],
            )
        if not units_data:
            # P2-12：无 empty_reason 时保持裸字符串逐字节不变（既有精确等值
            # 断言 + replace_central_idea_warnings 的 substring 语义都依赖它）。
            empty_reason = ""
            if isinstance(result.data, dict):
                empty_reason = str(result.data.get("empty_reason", "") or "").strip()
            warning = (
                f"LLM found no extractable units: {empty_reason}"
                if empty_reason
                else "LLM found no extractable units"
            )
            return ThesisExtraction([], [], [warning])

        # P1-2：校验域 = LLM 实际所见全集（正文+图片OCR+图片描述），与
        # central-idea 的 content-only 判据不同——主链 prompt 允许引用图片文本。
        evidence_domain = _evidence_domain(source)
        units: list[InformationUnit] = []
        dropped_unverifiable = 0
        for item in units_data:
            if not isinstance(item, dict):
                continue
            try:
                evidence = str(item.get("evidence", "")).strip()
                if not evidence:
                    continue
                if not _evidence_in_source(evidence, evidence_domain):
                    dropped_unverifiable += 1
                    continue
                unit = _make_information_unit(
                    source,
                    now,
                    unit_type=str(item.get("unit_type", "industry_map")),
                    title=str(item.get("title", ""))[:50],
                    thesis=str(item.get("thesis", "")),
                    evidence=evidence,
                    interpretation=str(item.get("interpretation", "")),
                    confidence=float(item.get("confidence", 0.7)),
                    topics=_as_str_list(item.get("topics", [])),
                    companies=_as_str_list(item.get("companies", [])),
                    extractor="llm",
                )
                units.append(unit)
            except Exception:
                continue

        chains = [_make_evidence_chain(source, unit) for unit in units]
        warnings: list[str] = []
        if dropped_unverifiable:
            warnings.append(f"LLM evidence not verbatim: {dropped_unverifiable} dropped")
        if not units:
            warnings.append("LLM extraction produced no valid units")

        logger.info("LLM thesis extractor: %d units from %s", len(units), source.title[:40])
        return ThesisExtraction(units, chains, warnings)


#: bare-empty 升级重试指令：空返回且无 empty_reason 时追加，强迫模型
#: 先完成逐字摘录再构造单元（自由文本探针实证同内容可完整产出）。
_EMPTY_ESCALATION_NUDGE = (
    "\n---\n【重试指令】上一次返回了空 units 且未说明原因，这是异常结果。"
    "请严格按两步执行：先逐字摘录原文里承载实质观点的句子，再仅对这些"
    "摘录句构造 units 返回。确无实质内容才允许空返回并给出 empty_reason。"
)


def _sentinel_summary(failure: object) -> str:
    """后端失败哨兵的 content-free 摘要：仅 error_type + http_status。"""
    if not isinstance(failure, dict):
        return "backend failure"
    error_type = str(failure.get("error_type") or "LLMBackendError")
    status = failure.get("http_status")
    if isinstance(status, int):
        return f"backend failure ({error_type} http={status})"
    return f"backend failure ({error_type})"


def _as_str_list(val: object) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(v) for v in val if v]


#: 路由进配置（家规6）：cognition 提取的 backend 优先序读 llm.yaml
#: `priorities.cognition`，缺省回退历史元组（零行为变化）。故障实证
#: 2026-08-29 晚 glm53/deepseek 对该任务返回合法空数组（无 fallback 触发），
#: 提取路由需要可配置的降级序。
_COGNITION_PREFERRED_FALLBACK = ("glm53", "deepseek", "qwen", "claude")


def _cognition_preferred() -> tuple[str, ...]:
    from fin_analyse.claims.config_loader import configured_backend_order

    return configured_backend_order("cognition", _COGNITION_PREFERRED_FALLBACK)
