"""Agent Runtime Context Provider — unified G context resolution for Guo agent.

Orchestrates pinned source resolution (KB-first), fresh G context selection
(time windows, column scoring, relevance ranking, temporal assessment),
bounded semantic context budgets, source boundary guards, and outputs compact
`llm_context` plus full `audit_context`.

This is the SINGLE canonical G context provider — FreshGContextProvider was
merged in.  All filtering (pinned, time windows, Q&A exclusion, column scoring)
lives in one code path.

Rules:
- Pinned source is a candidate that must pass the relevance gate; not forced.
- Knowledge-base-first resolution; KB miss → data gap + refetch deferred.
- 锐评: 最近 N 个交易日窗口（config/g_context_windows.json，单源
  window_config），只取最新一条。  特刊/普通栏: 分级加长窗口，按相关性排序.
- Q&A articles (好问题) excluded from context injection.
- One bounded semantic budget; callers may only request a stricter cap.
- G-only source boundary; external/Z/research_reference are excluded.
- confidence_boost_allowed is always False; advisory_only is always True.
- Provider never instantiates ZsxqScraper directly; uses ingestion seam.
- Hermes does not choose context; selection is FIN-owned.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from collections.abc import Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Final

from fin_analyse.guo_teacher_research.g_working_set import (
    GWorkingSetReader,
    GWorkingSetService,
    decode_g_knowledge_index,
    select_active_g_working_set,
)
from fin_analyse.guo_teacher_research.source_contract import (
    GSourceDecision,
    classify_g_source,
)
from fin_analyse.guo_teacher_research.window_config import (
    calendar_artifact_available,
    load_g_window_config,
    load_position_topic_rules,
    trading_window_cutoff,
)
from fin_analyse.temporal import TemporalService
from fin_analyse.temporal.models import (
    TemporalAssessmentRequest,
    TemporalItem,
    TemporalTaskContext,
)

logger = logging.getLogger(__name__)

# ── Timezone ──────────────────────────────────────────────────────────────────

CST = timezone(timedelta(hours=8))

# ── Semantic context budget ─────────────────────────────────────────────────────────────

_DEFAULT_G_EVENT_BUDGET = 5

# Bounded overview budget for latest-focus ("最近关注什么变化") default-entry
# questions. This is query-specific and does not create a capability tier.
_LATEST_FOCUS_OVERVIEW_BUDGET = 5
# Max recent references pulled into the latest-focus overview pool before budget.
_LATEST_FOCUS_REFERENCE_CAP = 6
# B 第二刀（用户拍板 2026-08-21）：泛问题（无绑定目标）下 fresh 特刊上限。
_BROAD_OVERVIEW_SPECIAL_CAP = 2
# Same-day + near-day window (days) for latest-focus reference recency.
_LATEST_FOCUS_RECENCY_DAYS = 2

# Latest-focus intent trigger tokens.
_LATEST_FOCUS_TIME_TOKENS = ("最近", "近期", "这几天", "这两天", "今天", "近几日", "这段时间")
_LATEST_FOCUS_FOCUS_TOKENS = (
    "关注",
    "主线",
    "变化",
    "看什么",
    "重点",
    "边际",
    "动向",
    "新变化",
)
# ── Source classification guards ─────────────────────────────────────────────
# (FreshGContextProvider constants win where they differ)

_G_SOURCE_CLASSIFICATIONS = frozenset({"teacher_original"})
_COMMENTARY_COLUMNS = frozenset({"星大派锐评"})
_SPECIAL_REPORT_COLUMNS = frozenset({"星大派特刊", "凤仙郡小故事"})
# 星大派好问题 = 老师回答会员提问（teacher_original 原文观点）；
# 与特刊并列按相关性排序进入 G 上下文。
_GOOD_QUESTION_COLUMNS = frozenset({"星大派好问题"})
_EXCLUDED_SOURCE_CLASSIFICATIONS = frozenset({"research_reference", "unknown_reference"})
# Reference lane also rejects AI-assisted reference (never a G/Z/reference item).
_REFERENCE_EXCLUDED_SOURCE_CLASSIFICATIONS = frozenset(
    {"research_reference", "ai_assisted_reference", "unknown_reference"}
)
# BUG-006③：普通栏非 QA 教师原文已由 classify_g_source 放行（general G，
# 加长准入窗），选择层不再按「普通」整体排除；QA 文章仍排除。

# ── Time windows (FreshG rules) ───────────────────────────────────────────────
# BUG-006③：窗口值单源自 config/g_context_windows.json（window_config seam）；
# 准入层（g_working_set）与选择层共用同一份，杜绝双层漂移。

# 历史 G lane（2026-08-01 用户决策/审查 P0-A）："从锅老师历史看"类问题
# 检索超出 active 窗口的历史好问题/特刊（不扩大 active 窗口本身）。
_HISTORICAL_G_QUERY_MARKERS = ("历史", "历史看", "回顾", "过往观点", "之前说过")

# ── Constants ─────────────────────────────────────────────────────────────────

_PINNED_SOURCES_DEFAULT_PATH = Path("runtime/agent_context/pinned_sources.jsonl")
_PRIORITY_EVENTS_CACHE_PATH = Path("runtime/cognition/priority_events.jsonl")
_RUNTIME_CONTEXT_VERSION = "guo_runtime_context_v2"
_MAX_PRIORITY_CACHE_BYTES = 8 * 1024 * 1024
_MAX_REFERENCE_MARKDOWN_BYTES = 2 * 1024 * 1024
_MAX_INLINE_MATERIAL_BYTES = 256 * 1024
_MAX_DEEP_READ_PROJECTION_BYTES = _MAX_INLINE_MATERIAL_BYTES
_MAX_AGENT_VISIBLE_G_GUIDANCE_CHARS = 1_000
_MAX_G_MATERIAL_SOURCE_REFS = 64

# ── Topic keywords for relevance ─────────────────────────────────────────────

_TOPIC_KEYWORDS = frozenset(
    {
        "稀土",
        "消费电子",
        "新能源",
        "医药",
        "芯片",
        "半导体",
        "光伏",
        "锂电",
        "白酒",
        "煤炭",
        "有色",
        "钢铁",
        "化工",
        "汽车",
        "军工",
        "金融",
        "地产",
        # M6（用户拍板 2026-08-19）：08-17 特刊/主线意图词，防相关性门误伤。
        "PCB",
        "覆铜板",
        "CCL",
        "光互连",
        "光模块",
        "CPO",
        "InP",
        "磷化铟",
        "MLCC",
        "算力材料",
        "算力金属",
        "前驱体",
        "特气",
        "稀土氧化物",
        # 2026-08-28：当前持仓主题词补齐（BUG-006 相关，防概念题相关性门误伤）。
        "锗",
        "锗业",
        "钽",
        "稀有金属",
        "战略金属",
        "电解液",
        "服务器",
    }
)

# ── Position → topic inference rules (FreshG) ────────────────────────────────

def _position_topic_rules() -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Position→topic rules: configured table or built-in default.

    BUG-006③ 配置化（家规 6）：会变的选择进 config/position_topic_rules.json。
    Process-lifetime cache — one load, immutable snapshot, no IO in the
    matching loop; changes take effect with the next process restart (deployment
    rule 9), never mid-request.
    """
    global _CACHED_TOPIC_RULES
    if _CACHED_TOPIC_RULES is None:
        _CACHED_TOPIC_RULES = load_position_topic_rules() or _BUILTIN_POSITION_TOPIC_RULES
    return _CACHED_TOPIC_RULES


_CACHED_TOPIC_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] | None = None

_BUILTIN_POSITION_TOPIC_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("000657", "中钨", "钨"),
        ("钨", "六氟化钨", "硬质合金", "有色", "材料", "算力材料", "算力金属"),
    ),
    (
        ("600392", "盛和", "000831", "中国稀土", "稀土"),
        ("稀土", "磁材", "有色", "材料", "抗日材料", "算力材料"),
    ),
    (
        ("601899", "紫金"),
        ("铜", "黄金", "矿业", "有色", "资源", "金属", "算力金属"),
    ),
    (
        ("603993", "洛阳钼", "钼"),
        ("钼", "钴", "铜", "有色", "资源", "金属", "算力金属"),
    ),
    (("000960", "锡业", "锡"), ("锡", "有色", "金属", "算力金属")),
    (("159796", "电池", "锂电"), ("电池", "锂电", "新能源", "储能")),
    (
        ("002428", "云南锗业", "锗"),
        ("锗", "锗业", "稀有金属", "红外", "光纤", "有色", "材料", "算力金属"),
    ),
    (
        ("000962", "东方钽业", "钽"),
        ("钽", "钽电容", "军工", "半导体材料", "靶材", "有色", "材料"),
    ),
    (
        ("601138", "工业富联", "富联"),
        ("算力", "AI服务器", "服务器", "消费电子", "算力材料", "PCB"),
    ),
    (
        ("002709", "天赐材料", "天赐"),
        ("锂电", "电解液", "电池", "新能源", "材料"),
    ),
    (("600879", "航天电子", "航天电子"), ("军工", "航天", "导航")),
    (
        ("605376", "博迁新材", "博迁"),
        ("MLCC", "镍粉", "粉体", "消费电子", "材料"),
    ),
)

# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PinnedSource:
    """Runtime pinned source configuration for Guo teacher agent."""

    pinned_id: str
    agent_id: str = "guo_teacher"
    source_scope: str = "g_source"
    canonical_url: str = ""
    topic_id: str = ""
    published_at: str = ""
    pinned: bool = True
    # 用户拍板 2026-08-20 配置化：true = 常驻主线（无条件注入 + 压缩投影）。
    # 换常驻 = 编辑 pinned_sources.jsonl 把该标记移到目标条目，下次 resolve
    # 即生效（jsonl 每次读取），无需改代码/重建 release。
    resident_mainline: bool = False
    pinned_boost_factor: float = 2.0
    linked_articles: tuple[str, ...] = ()
    linked_articles_policy: str = "part_of_pinned_source_bundle"
    usage_policy: str = "background_guidance_only_no_confidence_boost"
    processing_status: str = ""  # ready | pending | failed | ""
    processed_at: str = ""
    processed_title: str = ""
    guidance_brief: str = ""
    source_refs: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()
    companies: tuple[str, ...] = ()
    theme_clusters: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentRuntimeContextRequest:
    """Request to resolve agent runtime context."""

    agent_id: str = "guo_teacher"
    question: str = ""
    ticker: str = ""
    company: str = ""
    tickers: tuple[str, ...] = ()
    positions: tuple[dict[str, Any], ...] = ()
    topic: str = ""
    max_g_events: int = 0  # 0 = semantic default; positive = stricter cap
    now: str = ""
    # Required current-G consultations retain the latest qualified commentary,
    # but may not fill the pack with an otherwise unrelated generic special
    # report merely because it has a higher static column score.
    require_relevant_special_reports: bool = False


@dataclass(frozen=True)
class AgentRuntimeContextResult:
    """Resolved runtime context with compact llm_context and full audit_context."""

    available: bool = False
    llm_context: dict[str, Any] = field(default_factory=dict)
    audit_context: dict[str, Any] = field(default_factory=dict)
    data_gaps: tuple[str, ...] = ()
    quality_flags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _SelectedReferenceMaterial:
    """The one local material actually used to build a reference projection."""

    kind: str
    ref: str
    available_at: str
    raw_sha256: str
    title: str
    published_at: str
    summary: str
    key_points: tuple[str, ...]
    tickers: tuple[str, ...]
    companies: tuple[str, ...]
    theme_clusters: tuple[str, ...]
    industry_chain_facts: tuple[str, ...]

    def provenance(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "available_at": self.available_at,
            "raw_sha256": self.raw_sha256,
        }


@dataclass(frozen=True)
class _ReferenceArticleSource:
    path: Path
    raw: bytes
    raw_sha256: str
    modified_at: str
    metadata: dict[str, Any]
    body: str
    title: str


@dataclass(frozen=True)
class _FreshDeepReadSnapshot:
    compact: dict[str, Any]
    available_at: str
    content_hash: str
    generation_id: str
    compact_raw_sha256: str


# ── Provider ──────────────────────────────────────────────────────────────────


class AgentRuntimeContextProvider:
    """Resolve Guo teacher agent runtime context — single canonical provider.

    Orchestrates:
    - Pinned source resolution (KB-first)
    - Fresh G context selection (time windows + column scoring + relevance + temporal)
    - Bounded semantic context budget
    - Source boundary guard (G-only)
    - Compact llm_context + full audit_context output

    Constructor accepts optional test fixtures; in production, loads from
    runtime config and knowledge base.
    """

    def __init__(
        self,
        *,
        kb_root: str | Path,
        pinned_sources: tuple[PinnedSource, ...] | None = None,
        knowledge_documents: list[dict[str, Any]] | None = None,
        fresh_g_candidates: tuple[dict[str, Any], ...] | None = None,
        g_working_set_reader: GWorkingSetReader | None = None,
        cognition_mainline_reader: Any | None = None,
    ) -> None:
        raw_kb_root = Path(kb_root)
        if not raw_kb_root.is_absolute():
            raise ValueError("knowledge-base root must be absolute")
        if ".." in raw_kb_root.parts:
            raise ValueError("knowledge-base root cannot contain parent traversal")
        self._kb_root = raw_kb_root
        self._pinned_sources = pinned_sources  # None = load from disk
        self._knowledge_documents = knowledge_documents  # None = use KnowledgeStore
        self._fresh_g_candidates = fresh_g_candidates  # None = read priority_events cache
        # Fixture-provided candidates are already one explicit test snapshot.
        # Production/cache-backed reads also carry the independent operational
        # freshness assessment, but that assessment never blocks older usable G.
        self._g_working_set_reader = (
            g_working_set_reader
            if g_working_set_reader is not None or fresh_g_candidates is not None
            else GWorkingSetService(kb_root=self._kb_root)
        )
        # None = 该增强投影不装配（typed gap，零阻断）；装配时由 production
        # composition 显式注入 canonical root。
        self._cognition_mainline_reader = cognition_mainline_reader

    # ── Public resolve ────────────────────────────────────────────────────

    def resolve(self, request: AgentRuntimeContextRequest) -> AgentRuntimeContextResult:
        """Resolve runtime context for the given agent request."""
        data_gaps: list[str] = []
        now = request.now or datetime.now(tz=CST).isoformat()

        # ── Normalize positions (quantity→shares, cost→avg_cost) ──
        normalized_positions = self._normalize_positions(request.positions)

        # ── Build intent tokens (FreshG-enhanced: includes inferred position topics) ──
        intent_tokens = _build_intent_tokens(request)

        # ── Latest-focus overview intent (no explicit target + "最近关注变化" style) ──
        latest_focus = _is_latest_focus_query(request, intent_tokens)

        # ── Resolve pinned sources ──
        pinned_resolution = self._resolve_pinned_sources(request, intent_tokens, now)
        data_gaps.extend(pinned_resolution["data_gaps"])

        # Collect pinned article_ids for pre-selection exclusion.
        # Include both the pinned_id itself AND its linked_articles so that
        # fresh_g selection doesn't re-select articles already covered by pinned.
        # M2/C1/C3（用户拍板 2026-08-19）：注入与跳过 pin 的 linked 统一按
        # canonical + raw 双身份进排除集；解析失败（exclusion_unresolved）的
        # raw identity 也进拒绝表——被门跳过的来源不得从 fresh lane 重现。
        pinned_ids: set[str] = set()
        pinned_raw_ids: set[str] = set()
        for decision in pinned_resolution.get("per_pin_decisions", []):
            for cid in decision.get("linked_canonical_ids", []):
                if cid:
                    pinned_ids.add(cid)
            for raw in decision.get("linked_raw_ids", []):
                if raw:
                    pinned_raw_ids.add(raw)
            for raw in decision.get("exclusion_unresolved", []):
                if raw:
                    pinned_raw_ids.add(raw)
        for c in pinned_resolution["candidates"]:
            aid = str(c.get("article_id", ""))
            if aid:
                pinned_ids.add(aid)
            # Also exclude linked article IDs (may be deep dicts with article_id
            # from deep_read, or raw ID strings)
            for la in c.get("linked_articles", []) or []:
                if isinstance(la, dict):
                    la_aid = str(la.get("article_id", ""))
                    if la_aid:
                        pinned_ids.add(la_aid)
                elif isinstance(la, str):
                    la_clean = la.rsplit("/", 1)[-1].replace(".html", "") if "/" in la else la
                    if la_clean:
                        pinned_ids.add(la_clean)

        # ── Resolve fresh G context (excludes pinned article_ids pre-selection) ──
        fresh_g_resolution = self._resolve_fresh_g(
            request,
            intent_tokens,
            now,
            exclude_article_ids=pinned_ids,
            exclude_raw_ids=pinned_raw_ids,
        )
        data_gaps.extend(fresh_g_resolution["data_gaps"])

        # ── Assemble candidate pool (safety-net dedup: pinned wins over fresh_g) ──
        deduped_fresh = [
            c
            for c in fresh_g_resolution["candidates"]
            if str(c.get("article_id", "")) not in pinned_ids
        ]

        all_candidates: list[dict[str, Any]] = []
        all_candidates.extend(pinned_resolution["candidates"])
        all_candidates.extend(deduped_fresh)

        # ── Recent reference lane (same-day, highly-relevant, not-G) ──
        # Same-day Q&A / market_observation items are not eligible for the
        # strict G lane, but when highly relevant to the question they carry
        # useful same-day background.  They enter as reference (not-G) context,
        # preserving the G/Z boundary (external/research_reference stays out).
        reference_exclude_ids = set(pinned_ids)
        for c in all_candidates:
            aid = str(c.get("article_id", ""))
            if aid:
                reference_exclude_ids.add(aid)
        reference_resolution = self._resolve_recent_reference(
            request,
            intent_tokens,
            now,
            exclude_article_ids=reference_exclude_ids,
            latest_focus=latest_focus,
        )
        data_gaps.extend(reference_resolution["data_gaps"])
        all_candidates.extend(reference_resolution["candidates"])

        # ── Collect source boundary exclusions ──
        source_dropped: list[str] = []
        source_dropped.extend(fresh_g_resolution.get("excluded_sources", []))
        source_dropped.extend(pinned_resolution.get("excluded_sources", []))

        # ── Apply source boundary guard ──
        eligible, boundary_dropped = _apply_source_boundary(all_candidates)
        source_dropped.extend(boundary_dropped)

        # ── Apply semantic budget (latest-focus overview uses a bounded cap) ──
        budget = self._resolve_budget(request, latest_focus=latest_focus)
        selected, budget_dropped = _apply_budget(eligible, budget, intent_tokens)

        # ── Latest-focus overview metadata (source trace for prompt/output) ──
        recent_ref_selected = [c for c in selected if c.get("source_bucket") == "recent_reference"]
        latest_focus_context = {
            "active": latest_focus,
            "status": (
                ("injected" if recent_ref_selected else "no_recent_reference")
                if latest_focus
                else "not_applicable"
            ),
            "item_count": len(recent_ref_selected),
            "selection_policy": reference_resolution.get(
                "selection_policy", "same_day_high_relevance_reference_v1"
            ),
            "reference_article_ids": [str(c.get("article_id", "")) for c in recent_ref_selected],
        }

        # ── READY manifest-bound set for fresh-G source_refs ──
        # Computed before size enforcement so the enforcement measures the
        # exact JSON the final build will produce (bound-filtered refs plus
        # the unbound-ref gap codes) — never a bound-set-free approximation.
        freshness = fresh_g_resolution.get("working_set_freshness") or {}
        fresh_g_bound_article_ids: set[str] | None = None
        if isinstance(freshness, dict) and freshness.get("status") == "READY":
            fresh_g_bound_article_ids = {
                str(article_id)
                for article_id in freshness.get("bound_article_ids", [])
                if isinstance(article_id, str) and article_id
            }

        # ── Agent-visible canonical size bound (D3, 256 KiB) ──
        # Per-article deep-read projections are individually bounded, but
        # several selected entries can still push the whole-context canonical
        # JSON above the bound; lower-priority entries are evicted
        # deterministically with auditable stable gaps.  The measurement
        # includes the exact gap codes the final llm_context will carry, so
        # the returned artifact itself never exceeds the bound.
        # ── G mainline projection (P1, stateless, read-only) ──
        # 只消费已选择候选(现有 G Working Set 的只读输出)并按意图主题投影
        # 主线演化摘要;缺失/空/不匹配 → typed gap,不改写任何状态。
        # P1 来源门(B1):仅 READY manifest + 真实 generation + bound-source
        # 成员才投影;否则只给 typed gap,绝不把未绑定来源送进 prompt。
        # generation 语义与 fin.read_g_context 能力一致:评估只输出
        # canonical_sha256,以其作为 generation(真实 READY 路径非空)。
        assessment_generation = str(
            freshness.get("generation") or freshness.get("canonical_sha256") or ""
        )
        mainline_projection = _build_mainline_projection(
            selected=selected,
            intent_tokens=intent_tokens,
            generation=assessment_generation,
            manifest_sha256=str(freshness.get("canonical_sha256") or ""),
            bound_article_ids=fresh_g_bound_article_ids,
            manifest_ready=freshness.get("status") == "READY",
        )

        # 概念主题补充:方法论的 related_topics 是概念词(操作纪律/概率评估等),
        # 不在 _TOPIC_KEYWORDS 板块词表内——仅靠板块词匹配,概念类问题在真实
        # 入口永不命中方法论投影。此处从已选候选的规则主题中提取问题文本命中
        # 项,只注入方法论投影(不扩散到共享 intent_tokens/mainline),纯确定性
        # 子串匹配,不引入新输入。
        concept_hits: set[str] = set()
        for candidate in selected:
            for rule in candidate.get("methodology_rules") or []:
                if not isinstance(rule, Mapping):
                    continue
                for topic in rule.get("related_topics") or []:
                    if not isinstance(topic, str) or not topic:
                        continue
                    if topic in request.question or (
                        request.question and request.question in topic
                    ):
                        concept_hits.add(topic)

        # ── G methodology projection (验收 2/3, stateless, read-only) ──
        # 消费已选择候选的 compact methodology_rules,按意图主题确定性分组;
        # B1 来源门与 mainline 同:仅 READY manifest + 真实 generation +
        # bound-source 成员才投影;365 天方法论时钟过滤超窗规则。
        methodology_projection = _build_methodology_projection(
            selected=selected,
            intent_tokens=intent_tokens,
            generation=assessment_generation,
            manifest_sha256=str(freshness.get("canonical_sha256") or ""),
            bound_article_ids=fresh_g_bound_article_ids,
            manifest_ready=freshness.get("status") == "READY",
            now=now,
            concept_topics=concept_hits,
        )

        # ── Cognition mainline projection (P-CM1, stateless, read-only) ──
        # 已人工审核 G 认知 read-model 的纯只读投影:reader 缺/损坏/哈希漂移/
        # PIT 不匹配 → typed gap,绝不阻塞 Agent;内部按 4KiB/≤32 refs 整单元
        # 驱逐(不字符串切断)。identity 与当前 G Working Set canonical sha 对齐。
        cognition_mainline_projection = _build_cognition_mainline_projection(
            reader=self._cognition_mainline_reader,
            now=now,
            working_set_identity=assessment_generation,
            question=str(getattr(request, "question", "") or ""),
        )

        def _projection_attachments() -> dict[str, Any]:
            attachments: dict[str, Any] = {}
            if mainline_projection["themes"]:
                attachments["mainline_projection"] = {
                    "themes": mainline_projection["themes"],
                    "generation": mainline_projection["generation"],
                    "manifest_sha256": mainline_projection["manifest_sha256"],
                    "data_gaps": mainline_projection["data_gaps"],
                }
            if methodology_projection["groups"]:
                attachments["methodology_projection"] = {
                    "groups": methodology_projection["groups"],
                    "generation": methodology_projection["generation"],
                    "manifest_sha256": methodology_projection["manifest_sha256"],
                    "data_gaps": methodology_projection["data_gaps"],
                    "low_confidence_skipped": methodology_projection["low_confidence_skipped"],
                }
            # 无条件携带（空 items 也带 typed gap）：assembler 消费该附件后
            # 把 gap 透传到 runner 的咨询 gap 展示——增强未装配/失败必可见，
            # 禁止"静默无投影且无 gap"（design §3 冻结）。
            attachments["cognition_mainline_projection"] = {
                "items": cognition_mainline_projection["items"],
                "data_gaps": cognition_mainline_projection["data_gaps"],
            }
            return attachments

        # ── D3 尺寸门(含投影):投影与 selected 一并计入 256 KiB 预算;发生
        # 逐出后投影必须基于逐出后的 selected 重建,并以新投影重新测量,
        # 循环至稳定——保证最终 canonical(含重建投影)确实在预算内。 ──
        size_evicted_total: list[str] = []
        while True:
            selected, size_evicted = _enforce_llm_context_size_bound(
                request=request,
                selected=selected,
                latest_focus_context=latest_focus_context,
                data_gaps=data_gaps,
                fresh_g_bound_article_ids=fresh_g_bound_article_ids,
                projections=_projection_attachments(),
            )
            size_evicted_total.extend(size_evicted)
            data_gaps.extend(size_evicted)
            if not size_evicted:
                break
            mainline_projection = _build_mainline_projection(
                selected=selected,
                intent_tokens=intent_tokens,
                generation=assessment_generation,
                manifest_sha256=str(freshness.get("canonical_sha256") or ""),
                bound_article_ids=fresh_g_bound_article_ids,
                manifest_ready=freshness.get("status") == "READY",
            )
            methodology_projection = _build_methodology_projection(
                selected=selected,
                intent_tokens=intent_tokens,
                generation=assessment_generation,
                manifest_sha256=str(freshness.get("canonical_sha256") or ""),
                bound_article_ids=fresh_g_bound_article_ids,
                manifest_ready=freshness.get("status") == "READY",
                now=now,
                concept_topics=concept_hits,
            )

        # ── Build llm_context ──
        # READY manifest-backed fresh-G material may only expose manifest-bound
        # source_refs (unbound refs are dropped in _build_llm_context).
        llm_context = _build_llm_context(
            request=request,
            selected=selected,
            data_gaps=data_gaps,
            latest_focus_context=latest_focus_context,
            fresh_g_bound_article_ids=fresh_g_bound_article_ids,
        )
        # 投影的 typed gap 只留在投影结构与审计内,不进顶层 data_gaps:
        # 咨询对外行为与现行为完全一致(增强缺失不改变咨询契约)。
        llm_context.update(_projection_attachments())

        # Combine all dropped for audit
        all_dropped: list[str] = (
            list(source_dropped) + list(budget_dropped) + list(size_evicted_total)
        )

        # ── Build audit_context ──
        audit_context = _build_audit_context(
            request=request,
            pinned=pinned_resolution,
            fresh_g=fresh_g_resolution,
            mainline_projection=mainline_projection,
            methodology_projection=methodology_projection,
            budget=budget,
            selected=selected,
            dropped=all_dropped,
            data_gaps=data_gaps,
            normalized_positions=normalized_positions,
            latest_focus_context=latest_focus_context,
        )

        # ── Quality flags ──
        quality_flags: dict[str, Any] = {
            "dynamic_budget": {
                "max_events": budget,
                "reason": "semantic_contract_budget",
            },
            "pinned_candidate_seen": pinned_resolution["candidate_seen"],
            "methodology_low_confidence_skipped": methodology_projection[
                "low_confidence_skipped"
            ],
            "pinned_injected": pinned_resolution["injected"],
            "latest_commentary_injected": fresh_g_resolution.get("commentary_injected", False),
            "advisory_only": True,
            "confidence_boost_allowed": False,
        }

        return AgentRuntimeContextResult(
            available=bool(selected) or pinned_resolution["injected"] is True,
            llm_context=llm_context,
            audit_context=audit_context,
            data_gaps=tuple(dict.fromkeys(data_gaps)),  # deduplicate preserving order
            quality_flags=quality_flags,
        )

    # ── Pinned source resolution ──────────────────────────────────────────

    def _resolve_pinned_sources(
        self,
        request: AgentRuntimeContextRequest,
        intent_tokens: dict[str, set[str]],
        now: str,
    ) -> dict[str, Any]:
        """Resolve pinned sources against knowledge documents.

        M2（用户拍板 2026-08-19）：非 42章经置顶改走相关性门——不通过不注入
        （gate=pinned_gate_skipped，skip_gap 记录）；42章经保留置顶注入但走
        压缩路径（M3）。逐条决定记入 per_pin_decisions；被跳过 pin 的 linked
        来源以 canonical 与 raw 双身份进排除集（防 fresh 重现，C1/C3）。
        """
        result: dict[str, Any] = {
            "candidate_seen": False,
            "knowledge_base_status": "none",
            "injected": False,
            "relevance_gate": "not_applicable",
            "candidates": [],
            "data_gaps": [],
            "linked_articles_policy": "part_of_pinned_source_bundle",
            "per_pin_decisions": [],
        }

        pinned_sources = self._get_pinned_sources(request)
        if not pinned_sources:
            return result

        kb_docs: list[dict[str, Any]] | None = None
        index_articles: list[dict[str, Any]] | None = None
        result["candidate_seen"] = True
        result["pinned_count"] = len(pinned_sources)

        for pinned in pinned_sources:
            decision: dict[str, Any] = {
                "pinned_id": getattr(pinned, "pinned_id", ""),
                "gate": "not_applicable",
                "injected": False,
                "skip_gap": None,
                "linked_raw_ids": [],
                "linked_canonical_ids": [],
                "exclusion_unresolved": [],
            }
            # C1/C3：linked 来源归一化（canonical + raw 双身份；解析失败 fail-closed）。
            for aid in getattr(pinned, "linked_articles", None) or ():
                raw = str(aid).strip()
                if not raw:
                    continue
                decision["linked_raw_ids"].append(raw)
                if index_articles is None:
                    index_articles = _load_index_articles_list(self._kb_root)
                cid, ok = _canonical_article_id(self._kb_root, raw, index_articles)
                if ok and cid:
                    decision["linked_canonical_ids"].append(cid)
                else:
                    decision["exclusion_unresolved"].append(raw)

            # M3/C1（配置化）：常驻主线 = jsonl 条目 resident_mainline 标记。
            is_resident, identity_conflict = _is_resident_mainline_pinned(pinned)
            if identity_conflict:
                decision["gate"] = "pinned_gate_skipped"
                decision["skip_gap"] = "pinned_source_identity_conflict"
                result["data_gaps"].append("pinned_source_identity_conflict")
                result["per_pin_decisions"].append(decision)
                continue

            candidate: dict[str, Any] | None = None
            relevant = False
            if _pinned_has_processed_artifact(pinned):
                result["knowledge_base_status"] = "processed_artifact"
                result["processing_status"] = pinned.processing_status or "ready"
                relevant = _is_processed_pinned_relevant(self._kb_root, pinned, intent_tokens)
                candidate = _processed_pinned_to_candidate(self._kb_root, pinned)
            elif pinned.processing_status:
                processing_status = pinned.processing_status.strip().lower()
                result["processing_status"] = processing_status
                if processing_status == "pending":
                    result["data_gaps"].append("pinned_source_processed_artifact_pending")
                elif processing_status == "failed":
                    result["data_gaps"].append("pinned_source_processed_artifact_failed")
                else:
                    result["data_gaps"].append("pinned_source_processed_artifact_unavailable")

            if candidate is None:
                # Try to find pinned source in KB
                if kb_docs is None:
                    kb_docs = self._get_knowledge_documents()
                matched_doc = _find_pinned_in_kb(pinned, kb_docs)
                if matched_doc is not None:
                    result["knowledge_base_status"] = "hit"
                    relevant = _is_relevant(matched_doc, intent_tokens, pinned)
                    candidate = _pinned_to_candidate(pinned, matched_doc)
                else:
                    result["knowledge_base_status"] = "miss"
                    result["data_gaps"].append("pinned_source_kb_miss")
                    result["data_gaps"].append("pinned_source_refetch_deferred")

            if candidate is not None and (is_resident or relevant):
                if is_resident and not relevant:
                    decision["gate"] = "bypassed_user_pinned"
                else:
                    decision["gate"] = "passed"
                decision["injected"] = True
                result["injected"] = True
                if is_resident:
                    # M3：常驻主线压缩为框架摘要（不复制完整 deep_read 富字段）。
                    _apply_resident_mainline_compact_projection(candidate)
                result["candidates"].append(candidate)
            else:
                decision["gate"] = "pinned_gate_skipped"
                decision["skip_gap"] = "pinned_source_relevance_gate_skipped"
                result["data_gaps"].append("pinned_source_relevance_gate_skipped")
            result["per_pin_decisions"].append(decision)

        # C2：顶层 relevance_gate 聚合闭集（兼容旧消费者；逐条事实只信 per_pin）。
        decisions = result["per_pin_decisions"]
        if decisions:
            injected_any = any(d.get("injected") for d in decisions)
            skipped_any = any(d.get("skip_gap") for d in decisions)
            result["relevance_gate"] = (
                "mixed" if injected_any and skipped_any else "all_passed" if injected_any else "all_skipped"
            )
        return result

    # ── Fresh G resolution (was FreshGContextProvider — now inline) ───────

    def _resolve_fresh_g(
        self,
        request: AgentRuntimeContextRequest,
        intent_tokens: dict[str, set[str]],
        now: str,
        *,
        exclude_article_ids: set[str] | None = None,
        exclude_raw_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Resolve fresh G context: time windows + column scoring + relevance + temporal.

        This replaces the old _resolve_latest_commentary + FreshGContextProvider
        delegation.  All filtering lives in one code path:
        1. Load candidates (from test fixture or priority_events cache)
        2. G-source filter
        3. Exclude Q&A articles（普通栏非 QA 由分类门放行，BUG-006③）
        4. Time window: 锐评=交易日, 特刊/普通=分级加长（单源配置）
        5. Deduplicate by title
        6. Exclude pinned article_ids (pre-selection, not post-hoc)
        7. Select: 1 latest 锐评 + N 特刊 ranked by relevance
        8. Temporal assessment per selected candidate
        9. Build candidate entries with source_bucket tags
        """
        exclude_ids = exclude_article_ids or set()
        result: dict[str, Any] = {
            "candidates": [],
            "data_gaps": [],
            "excluded_sources": [],
            "selection_counts": {
                "loaded": 0,
                "source_eligible": 0,
                "column_eligible": 0,
                "within_time_window": 0,
                "after_pinned_exclusion": 0,
                "after_deep_read": 0,
                "deep_read_no_extractable": 0,
                "special_relevance_excluded": 0,
                "selected": 0,
            },
            "count": 0,
            "commentary_injected": False,
            "commentary_reason": "no_commentary_within_window",
            "selection_policy": "time_window_column_score_relevance_v2",
            "working_set_freshness": {
                "status": "NOT_CHECKED",
                "bound_article_ids": [],
                "data_gaps": [],
            },
        }
        evaluation_time = _coerce_datetime(now) or datetime.now(tz=CST)
        working_set_assessment: Any | None = None

        if self._fresh_g_candidates is None and self._g_working_set_reader is not None:
            try:
                working_set_assessment = self._g_working_set_reader.evaluate(now=evaluation_time)
                result["working_set_freshness"] = working_set_assessment.to_runtime_context()
                result["data_gaps"].extend(working_set_assessment.data_gaps)
            except Exception:
                result["working_set_freshness"] = {
                    "status": "MISSING",
                    "canonical_sha256": "",
                    "evaluated_at": "",
                    "bound_article_ids": [],
                    "data_gaps": ["g_working_set_manifest_invalid"],
                }
                result["data_gaps"].append("g_working_set_manifest_invalid")

        # ── 1. Load candidates ──
        active_selection: object | None = None
        if self._fresh_g_candidates is not None:
            raw_candidates = [dict(candidate) for candidate in self._fresh_g_candidates]
            candidates_share_manifest_selection = False
        else:
            candidates_share_manifest_selection = True
            cached_events = _read_cache_candidates(self._kb_root)
            index_articles = _read_g_index_articles(self._kb_root, evaluation_time)
            if index_articles is None:
                result["data_gaps"].append("fresh_g_knowledge_index_unavailable")
                raw_candidates = []
            else:
                active = select_active_g_working_set(
                    index_articles=index_articles,
                    priority_events=cached_events,
                    now=evaluation_time,
                )
                active_selection = active
                if active is None:
                    result["data_gaps"].append("fresh_g_knowledge_index_invalid")
                    raw_candidates = []
                else:
                    result["data_gaps"].extend(active.data_gaps)
                    raw_candidates = [dict(candidate) for candidate in active.runtime_candidates()]

        freshness = result["working_set_freshness"]
        ready_manifest = (
            candidates_share_manifest_selection
            and isinstance(freshness, dict)
            and freshness.get("status") == "READY"
        )
        if ready_manifest and not _active_selection_matches_ready_manifest(
            working_set_assessment,
            active_selection,
            now=evaluation_time,
        ):
            result["data_gaps"] = list(getattr(working_set_assessment, "data_gaps", ()))
            result["data_gaps"].append("g_working_set_sources_changed")
            return result

        if not raw_candidates:
            result["data_gaps"].append("fresh_g_context_cache_empty")
            return result
        selection_counts = result["selection_counts"]
        selection_counts["loaded"] = len(raw_candidates)

        # ── 2. G-source filter ──
        eligible = _filter_g_source(raw_candidates)
        selection_counts["source_eligible"] = len(eligible)
        # Track excluded non-G for audit
        for c in raw_candidates:
            sc = str(c.get("source_classification", ""))
            source_decision = _g_source_decision(c)
            if not source_decision.eligible and source_decision.data_gap:
                result["data_gaps"].append(source_decision.data_gap)
            if not source_decision.eligible and (
                sc not in ("teacher_original",) or source_decision.data_gap
            ):
                result["excluded_sources"].append(
                    f"non_g_source_excluded:{c.get('article_id', 'unknown')}({sc})"
                )

        if not eligible:
            result["data_gaps"].append("no_g_source_candidates")
            return result

        # ── 3. Exclude 好问题/普通 columns + Q&A articles ──
        eligible = [
            c
            for c in eligible
            if not _qa_excluded(c)
        ]
        selection_counts["column_eligible"] = len(eligible)

        # ── 4. Time window ──
        # Use the request's evaluation time so replay, tests, and production
        # resolve the same semantic window. Wall-clock time is only a fallback
        # when a caller did not provide an explicit evaluation timestamp.
        evaluation_time = evaluation_time.astimezone(UTC)
        # BUG-006③：窗口单源配置；锐评=交易日语义（日历不可用回落自然日）。
        window_config = load_g_window_config()
        cutoff_commentary, _used = trading_window_cutoff(
            evaluation_time, window_config.commentary_trading_days
        )
        if not calendar_artifact_available():
            result["data_gaps"].append("g_window_calendar_unavailable")
        cutoff_special = evaluation_time - timedelta(days=window_config.special_report_days)
        recent: list[dict[str, Any]] = []
        historical_query = any(marker in request.question for marker in _HISTORICAL_G_QUERY_MARKERS)
        for c in eligible:
            col = _candidate_column(c)
            if historical_query and col in (_SPECIAL_REPORT_COLUMNS | _GOOD_QUESTION_COLUMNS):
                # 历史 G lane：好问题/特刊的历史文章可检索（超出 active 窗口），
                # 锐评保持交易日窗口（旧锐评无背景价值）。
                cutoff = evaluation_time - timedelta(days=window_config.historical_days)
            else:
                cutoff = cutoff_commentary if col in _COMMENTARY_COLUMNS else cutoff_special
            ts = _candidate_time(c)
            dt = _coerce_datetime(ts)
            if dt is None or dt.astimezone(UTC) > evaluation_time:
                result["data_gaps"].append("fresh_g_candidate_time_invalid")
                continue
            if dt.astimezone(UTC) >= cutoff:
                recent.append(c)
        selection_counts["within_time_window"] = len(recent)

        # ── 5. Deduplicate by title ──
        deduped = recent if candidates_share_manifest_selection else _deduplicate_by_title(recent)

        # ── 6. Exclude pinned article_ids before selection ──
        # C1/C3：canonical id 精确排除 + raw identity 拒绝表（排除失败/跳过 pin
        # 的 raw 标识不得从 fresh lane 重现）。
        if exclude_ids or exclude_raw_ids:
            kept: list[dict[str, Any]] = []
            excluded_count = 0
            for c in deduped:
                raw_aid = str(c.get("article_id", "")).strip()
                if raw_aid and raw_aid in (exclude_raw_ids or ()):
                    excluded_count += 1
                    continue
                if exclude_ids:
                    cid, ok = _canonical_article_id(self._kb_root, raw_aid)
                    if ok and cid and cid in exclude_ids:
                        excluded_count += 1
                        continue
                kept.append(c)
            deduped = kept
            if excluded_count:
                result["excluded_sources"].append(
                    f"pinned_pre_excluded:{excluded_count}_articles"
                )
        selection_counts["after_pinned_exclusion"] = len(deduped)

        # ── 6.5 Enrich with compact deep_read for information-driven selection ──
        deduped = _enrich_candidates_with_deep_read(self._kb_root, deduped)
        effective_max = request.max_g_events if request.max_g_events > 0 else 10
        broad_g_overview = _is_broad_g_overview_query(request, intent_tokens)
        require_semantic_deep_read = ready_manifest
        require_agent_visible_relevance = require_semantic_deep_read and not broad_g_overview
        if require_semantic_deep_read:
            raw_bound_ids = freshness.get("bound_article_ids", [])
            bound_ids = {
                str(article_id)
                for article_id in raw_bound_ids
                if isinstance(article_id, str) and article_id
            }
            preliminary_selected = _select_context_candidates(
                deduped,
                intent_tokens,
                effective_max,
                require_agent_visible_relevance=require_agent_visible_relevance,
                require_relevant_special_reports=request.require_relevant_special_reports,
            )
            if any(
                str(candidate.get("article_id", "")) not in bound_ids
                for candidate in preliminary_selected
            ):
                result["data_gaps"].append("g_working_set_runtime_selection_mismatch")
            deduped = [
                candidate
                for candidate in deduped
                if str(candidate.get("article_id", "")) in bound_ids
            ]
            manifest_bindings = _manifest_deep_read_bindings(working_set_assessment)
            if manifest_bindings is None or set(manifest_bindings) != bound_ids:
                result["data_gaps"].append("g_working_set_manifest_invalid")
                deduped = []
            semantically_usable: list[dict[str, Any]] = []
            no_extractable_units_count = 0
            # M1（用户拍板 2026-08-19）：锐评只取 2 天窗口内最新一条。在语义
            # 过滤前冻结该候选——最新一条空壳且 raw fallback 失败即整类不
            # 注入（no_commentary_within_window），禁止回退到较旧锐评。
            frozen_latest_commentary = _latest_commentary(deduped)
            frozen_commentary_key = (
                _candidate_key(frozen_latest_commentary) if frozen_latest_commentary else None
            )
            for candidate in deduped:
                article_id = str(candidate.get("article_id", "")).strip()
                if (
                    frozen_commentary_key is not None
                    and _candidate_column(candidate) in _COMMENTARY_COLUMNS
                    and _candidate_key(candidate) != frozen_commentary_key
                ):
                    # 非最新锐评不参与语义选择（最新已冻结，旧锐评不回退）。
                    continue
                snapshot = candidate.get("_fresh_deep_read_snapshot")
                binding = manifest_bindings.get(article_id) if manifest_bindings else None
                if not isinstance(snapshot, _FreshDeepReadSnapshot) or not (
                    binding is not None and _deep_read_snapshot_matches_manifest(snapshot, binding)
                ):
                    result["data_gaps"].append("g_working_set_deep_read_changed")
                    continue
                semantic_status = candidate.get("_deep_read_semantic_status")
                if semantic_status == "extractable":
                    semantically_usable.append(candidate)
                    continue
                if semantic_status == "no_extractable_units":
                    no_extractable_units_count += 1
                    # raw fallback 成功 → 以 bounded 正文纳入（空壳不得标为
                    # semantic-ready，但内容比只有标题更有用）。
                    if candidate.get("_fallback_raw_content"):
                        semantically_usable.append(candidate)
                    continue
                result["data_gaps"].append(f"deep_read_artifact_missing:{article_id}")
            selection_counts["deep_read_no_extractable"] = no_extractable_units_count
            if no_extractable_units_count and not semantically_usable:
                result["data_gaps"].append("g_context_no_extractable_units")
            deduped = semantically_usable
            if require_agent_visible_relevance and deduped:
                target_relevant = [
                    candidate
                    for candidate in deduped
                    if _agent_visible_relevance_score(candidate, intent_tokens) > 0
                ]
                deduped = [
                    candidate
                    for candidate in deduped
                    if _candidate_column(candidate) in _COMMENTARY_COLUMNS
                    or _agent_visible_relevance_score(candidate, intent_tokens) > 0
                ]
                if not target_relevant:
                    # The latest market-level commentary remains core G
                    # context, but it must not be mistaken for ticker/topic
                    # evidence when no target-relevant article is available.
                    result["data_gaps"].append("g_context_no_relevant_items")
                if not deduped:
                    return result
        selection_counts["after_deep_read"] = len(deduped)

        if request.require_relevant_special_reports:
            selection_counts["special_relevance_excluded"] = sum(
                1
                for candidate in deduped
                if _candidate_column(candidate)
                in (_SPECIAL_REPORT_COLUMNS | _GOOD_QUESTION_COLUMNS)
                and _candidate_half_life(candidate) != "short_signal"
                and _agent_visible_relevance_score(candidate, intent_tokens) <= 0
            )

        # ── 7. Select: 1 latest 锐评 + N 特刊 by relevance ──
        selected = _select_context_candidates(
            deduped,
            intent_tokens,
            effective_max,
            require_agent_visible_relevance=require_agent_visible_relevance,
            require_relevant_special_reports=request.require_relevant_special_reports,
        )

        # Track commentary injection
        has_commentary = any(_candidate_column(c) in _COMMENTARY_COLUMNS for c in selected)
        result["commentary_injected"] = has_commentary
        result["commentary_reason"] = (
            "selected_latest_within_window" if has_commentary else "no_commentary_within_window"
        )

        # ── 7. Temporal assessment + 8. Build candidates (deep_read integrated) ──
        for selection_rank, candidate in enumerate(selected, start=1):
            article_id = str(candidate.get("article_id", ""))
            source_decision = _g_source_decision(candidate)
            source = source_decision.classification
            if not source_decision.eligible or source is None:
                if source_decision.data_gap:
                    result["data_gaps"].append(source_decision.data_gap)
                continue
            temporal_assessment = _assess_single(candidate, now)

            # Reuse the exact pair snapshot used for semantic selection.  The
            # checked marker distinguishes a missing first read from a candidate
            # that bypassed enrichment, so an artifact appearing mid-resolution
            # cannot be consumed under a different generation.
            snapshot = candidate.get("_fresh_deep_read_snapshot")
            if (
                not isinstance(snapshot, _FreshDeepReadSnapshot)
                and candidate.get("_fresh_deep_read_checked") is not True
            ):
                snapshot = _project_fresh_deep_read_snapshot(
                    _coerce_fresh_deep_read_snapshot(
                        _load_fresh_deep_read(self._kb_root, article_id)
                    ),
                    expected_article_id=article_id,
                )
            dr = snapshot.compact if snapshot is not None else None
            artifact_available_at = snapshot.available_at if snapshot is not None else ""
            deep_read_material = (
                {
                    "generation_id": snapshot.generation_id,
                    "content_hash": snapshot.content_hash,
                    "compact_raw_sha256": snapshot.compact_raw_sha256,
                }
                if snapshot is not None
                else {}
            )

            bucket = str(candidate.get("_fresh_g_selection_bucket") or "")

            # Compute intent-matched targets/themes (always, for both paths)
            matched_targets, matched_themes = _compute_matches(candidate, intent_tokens)

            semantic_units_available = bool(
                dr
                and _deep_read_has_extractable_units(
                    dr,
                    expected_article_id=article_id,
                )
            )
            historical_payload_usable = bool(
                dr
                and _deep_read_payload_is_well_formed(
                    dr,
                    expected_article_id=article_id,
                )
            )
            if dr and (
                semantic_units_available
                or (not require_semantic_deep_read and historical_payload_usable)
            ):
                # Use deep_read injectable_summary as guidance (no truncation)
                guidance = dr.get("injectable_summary", "")
                dr_tickers = _extract_tickers_from_core_theses(self._kb_root, dr)
                dr_companies = _extract_companies_from_core_theses(dr)
                dr_themes = _extract_theme_names(dr)
                # Merge deep_read themes with intent-matched themes
                for mt in matched_themes:
                    if mt not in dr_themes:
                        dr_themes.append(mt)
                core_theses = dr.get("core_theses", [])
                suggestions = dr.get("suggestions", [])
            elif require_semantic_deep_read:
                if candidate.get("_fallback_raw_content"):
                    # M4（用户拍板 2026-08-19）：空壳 raw 兜底——情绪/regime
                    # 观察等无结构化要点内容以 bounded 原文正文注入（如 08-18
                    # 锐评的划市判断与资金流向数据），不无条件丢弃。
                    guidance = str(candidate.get("guidance_brief") or "")
                    dr_tickers = []
                    dr_companies = []
                    dr_themes = list(matched_themes)
                    core_theses = []
                    suggestions = []
                else:
                    result["data_gaps"].append(
                        "g_context_no_extractable_units"
                        if dr
                        else f"deep_read_artifact_missing:{article_id}"
                    )
                    continue
            else:
                # Fallback: template-based guidance_brief from title + matches
                enriched = _build_event_entry(candidate, temporal_assessment, intent_tokens)
                guidance = enriched.get("guidance_brief", "")
                dr_tickers = list(enriched.get("related_tickers", []))
                dr_companies = [str(co).strip() for co in (candidate.get("companies", []) or [])]
                dr_themes = list(enriched.get("matched_themes", []))
                core_theses = []
                suggestions = []
                semantic_status = candidate.get("_deep_read_semantic_status")
                result["data_gaps"].append(
                    "g_context_no_extractable_units"
                    if dr or semantic_status == "no_extractable_units"
                    else f"deep_read_artifact_missing:{article_id}"
                )

            raw_source_refs = candidate.get("source_refs")
            raw_source_refs = raw_source_refs if isinstance(raw_source_refs, (list, tuple)) else []

            candidate_entry: dict[str, Any] = {
                "source_bucket": bucket if bucket in ("latest_commentary",) else "fresh_g",
                "selection_bucket": bucket,
                "selection_rank": selection_rank,
                "article_id": article_id,
                "title": str(dr.get("title", "") if dr else candidate.get("title", "")),
                "source_classification": "teacher_original",
                "column": _candidate_column(candidate),
                "source_family": source.source_family,
                "content_type": source.content_type,
                "source_usage": source.usage,
                "priority_label": source.priority_label,
                "is_qa": candidate.get("is_qa") is True,
                "guidance_brief": guidance,
                "why_available": _build_why_available(candidate, bucket, bool(dr), intent_tokens),
                "published_at": _candidate_time(candidate),
                "available_at": _max_reference_available_at(
                    candidate,
                    artifact_available_at,
                ),
                "tickers": dr_tickers,
                "source_refs": list(
                    dict.fromkeys(
                        [
                            str(ref).strip()
                            for ref in (
                                *raw_source_refs,
                                article_id,
                            )
                            if str(ref).strip()
                        ]
                    )
                ),
                "companies": dr_companies[:20],
                "theme_clusters": dr_themes[:12],
                # G 方法论层输入:compact 保留的 methodology_rule 单元(验收 1 合同)。
                # 旧产物缺字段/非列表 → 空,适配器返回 typed gap 而非崩溃。
                "methodology_rules": _candidate_methodology_rules(dr),
                "core_theses": core_theses,
                "suggestions": suggestions,
                "half_life_class": _candidate_half_life(candidate),
                "attention_policy": temporal_assessment.get("attention_policy", {}),
                "publish_freshness": temporal_assessment.get("publish_freshness", ""),
                "deep_read_material": deep_read_material,
            }
            result["candidates"].append(candidate_entry)

        result["count"] = len(result["candidates"])
        selection_counts["selected"] = result["count"]
        return result

    # ── Recent reference resolution (same-day, high-relevance, not-G) ─────

    def _resolve_recent_reference(
        self,
        request: AgentRuntimeContextRequest,
        intent_tokens: dict[str, set[str]],
        now: str,
        *,
        exclude_article_ids: set[str] | None = None,
        latest_focus: bool = False,
    ) -> dict[str, Any]:
        """Resolve same-day, highly-relevant reference (not-G) candidates.

        Q&A (好问题) and market_observation items are excluded from the strict
        G lane, but a *same-day* item that is *highly relevant* to the question
        keywords carries useful background.  It is injected as a reference
        (not-G) candidate so the agent can see it while the G/Z boundary is
        preserved: external / research_reference / unknown_reference sources
        are never promoted here.

        When ``latest_focus`` is set (a "最近关注什么变化" overview question with
        no explicit target), the strong per-question relevance gate is relaxed:
        recent (same-day / near-day) high-signal reference-tier items are ranked
        by signal/recency and a bounded number is admitted, so FIN can surface
        the recent focus shift across multiple local articles.  They stay
        ``recent_reference`` (not-G); the source boundary is unchanged.
        """
        exclude_ids = exclude_article_ids or set()
        result: dict[str, Any] = {
            "candidates": [],
            "data_gaps": [],
            "selection_policy": (
                "latest_focus_recent_high_signal_reference_v1"
                if latest_focus
                else "same_day_high_relevance_reference_v1"
            ),
        }

        index_unavailable = False
        if self._fresh_g_candidates is not None:
            raw_candidates = list(self._fresh_g_candidates)
        else:
            raw_candidates, index_unavailable = _read_index_reference_candidates(
                self._kb_root
            )
        if not raw_candidates:
            if index_unavailable:
                result["data_gaps"].append("recent_reference_index_unavailable")
            return result

        if latest_focus:
            pool: list[dict[str, Any]] = []
            for c in raw_candidates:
                article_id = str(c.get("article_id", ""))
                if article_id and article_id in exclude_ids:
                    continue
                sc = str(c.get("source_classification", ""))
                if sc in _REFERENCE_EXCLUDED_SOURCE_CLASSIFICATIONS:
                    continue  # never promote external / research / AI-assisted
                if not _is_reference_eligible(c):
                    continue
                if not _is_recent_within(c, now, days=_LATEST_FOCUS_RECENCY_DAYS):
                    continue
                pool.append(c)
            ranked = sorted(pool, key=_latest_focus_rank_key, reverse=True)
            for c in ranked[:_LATEST_FOCUS_REFERENCE_CAP]:
                result["candidates"].append(_recent_reference_to_candidate(self._kb_root, c))
            if not result["candidates"]:
                result["data_gaps"].append("latest_focus_no_recent_reference")
            return result

        selected: list[dict[str, Any]] = []
        for c in raw_candidates:
            article_id = str(c.get("article_id", ""))
            if article_id and article_id in exclude_ids:
                continue
            sc = str(c.get("source_classification", ""))
            if sc in _EXCLUDED_SOURCE_CLASSIFICATIONS:
                continue  # never promote external / research_reference
            if not _is_reference_eligible(c):
                continue  # only reference-tier items (Q&A / 普通 / observation)
            if not _is_same_day(c, now):
                continue
            if not _reference_is_relevant(c, request, intent_tokens):
                continue
            selected.append(c)
        # 有公司/链事实的候选优先：空事实帖先占槽位、投影再被 mapping 门丢弃
        # 是 BUG-012 残余二的第二重损耗。排序只改变候选顺序，不改变准入。
        selected.sort(key=_reference_rank_key, reverse=True)
        for c in selected:
            result["candidates"].append(_recent_reference_to_candidate(self._kb_root, c))

        return result

    # ── Budget ────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_budget(
        request: AgentRuntimeContextRequest,
        *,
        latest_focus: bool = False,
    ) -> int:
        """Resolve a bounded semantic context budget.

        ``max_g_events`` may only tighten the FIN-owned default. Latest-focus
        requests use the same bounded overview cap; neither path encodes an
        internal capability level.
        """
        base_budget = _DEFAULT_G_EVENT_BUDGET
        if latest_focus:
            base_budget = max(base_budget, _LATEST_FOCUS_OVERVIEW_BUDGET)
        if request.max_g_events > 0:
            return min(base_budget, request.max_g_events)
        return base_budget

    # ── Data source accessors (override points for tests) ─────────────────

    def _get_pinned_sources(self, request: AgentRuntimeContextRequest) -> tuple[PinnedSource, ...]:
        """Get pinned sources for the agent."""
        if self._pinned_sources is not None:
            return tuple(p for p in self._pinned_sources if p.agent_id == request.agent_id)
        return self._load_pinned_sources(self._kb_root, request.agent_id)

    def _get_knowledge_documents(self) -> list[dict[str, Any]]:
        """Get knowledge documents for KB resolution."""
        if self._knowledge_documents is not None:
            return self._knowledge_documents
        return self._load_knowledge_documents(self._kb_root)

    # ── Production loaders (lazy, only called when test fixtures absent) ───

    @staticmethod
    def _load_pinned_sources(
        kb_root: Path,
        agent_id: str,
    ) -> tuple[PinnedSource, ...]:
        """Load pinned sources from runtime config JSONL.

        The release layout exposes ``knowledge-base/runtime`` as an owner-built
        symlink into the shared KB.  ``_read_bounded_owned_regular_file_at``
        refuses any parent symlink, so the pin file is resolved first and the
        resolved target re-validated (regular file, euid owner, owner-only
        mode, bounded size) before reading.
        """
        snapshot = _read_bounded_owned_regular_file_at(
            kb_root,
            _PINNED_SOURCES_DEFAULT_PATH,
            max_bytes=_MAX_PRIORITY_CACHE_BYTES,
        )
        if snapshot is None:
            snapshot = _read_bounded_owned_regular_file_resolved(
                kb_root / _PINNED_SOURCES_DEFAULT_PATH,
                max_bytes=_MAX_PRIORITY_CACHE_BYTES,
            )
        if snapshot is None:
            return ()
        sources: list[PinnedSource] = []
        try:
            for line in snapshot[0].decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and obj.get("agent_id") == agent_id:
                        sources.append(
                            PinnedSource(
                                pinned_id=str(obj.get("pinned_id", "")),
                                agent_id=str(obj.get("agent_id", "guo_teacher")),
                                source_scope=str(obj.get("source_scope", "g_source")),
                                canonical_url=str(obj.get("canonical_url", "")),
                                topic_id=str(obj.get("topic_id", "")),
                                published_at=str(obj.get("published_at", "")),
                                pinned=bool(obj.get("pinned", True)),
                                resident_mainline=bool(obj.get("resident_mainline", False)),
                                pinned_boost_factor=float(obj.get("pinned_boost_factor", 2.0)),
                                linked_articles=tuple(obj.get("linked_articles", []) or []),
                                linked_articles_policy=str(
                                    obj.get(
                                        "linked_articles_policy", "part_of_pinned_source_bundle"
                                    )
                                ),
                                usage_policy=str(
                                    obj.get(
                                        "usage_policy",
                                        "background_guidance_only_no_confidence_boost",
                                    )
                                ),
                                processing_status=str(obj.get("processing_status", "")),
                                processed_at=str(obj.get("processed_at", "")),
                                processed_title=str(obj.get("processed_title", "")),
                                guidance_brief=str(obj.get("guidance_brief", "")),
                                source_refs=tuple(obj.get("source_refs", []) or []),
                                tickers=tuple(obj.get("tickers", []) or []),
                                companies=tuple(obj.get("companies", []) or []),
                                theme_clusters=tuple(obj.get("theme_clusters", []) or []),
                                keywords=tuple(obj.get("keywords", []) or []),
                            )
                        )
                except json.JSONDecodeError:
                    continue
                except Exception as exc:
                    logger.warning("Malformed pinned source line: %s", exc)
                    continue
        except (UnicodeDecodeError, ValueError) as exc:
            logger.warning("Failed to read pinned_sources.jsonl: %s", exc)
        return tuple(sources)

    @staticmethod
    def _load_knowledge_documents(kb_root: Path) -> list[dict[str, Any]]:
        """Load documents from the local ZSXQ markdown knowledge base."""
        articles_dir = kb_root / "articles"
        if not articles_dir.is_dir():
            return []

        docs: list[dict[str, Any]] = []
        try:
            for path in sorted(articles_dir.glob("*.md")):
                relative_path = Path("articles") / path.name
                snapshot = _read_bounded_owned_regular_file_at(
                    kb_root,
                    relative_path,
                    max_bytes=_MAX_REFERENCE_MARKDOWN_BYTES,
                )
                if snapshot is None:
                    continue
                try:
                    text = snapshot[0].decode("utf-8")
                except UnicodeDecodeError:
                    continue
                parsed = _parse_markdown_knowledge_text(
                    text,
                    path=kb_root / relative_path,
                )
                if parsed is not None:
                    docs.append(parsed)
        except Exception as exc:
            logger.warning("Failed to load knowledge markdown documents: %s", exc)
            return []
        return docs

    # ── Position normalization ─────────────────────────────────────────────

    @staticmethod
    def _normalize_positions(
        positions: tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
        """Normalize quantity→shares, cost→avg_cost for all positions."""
        normalized: list[dict[str, Any]] = []
        for pos in positions:
            norm = dict(pos)
            if "quantity" in norm and "shares" not in norm:
                norm["shares"] = norm["quantity"]
            if "cost" in norm and "avg_cost" not in norm:
                norm["avg_cost"] = norm["cost"]
            normalized.append(norm)
        return normalized


# ── Intent token extraction ───────────────────────────────────────────────────


def _build_intent_tokens(request: AgentRuntimeContextRequest) -> dict[str, set[str]]:
    """Extract intent tokens from the request for relevance matching.

    FreshG-enhanced: includes inferred position topics via _position_topic_rules().
    """
    tokens: dict[str, set[str]] = {
        "tickers": set(),
        "companies": set(),
        "topics": set(),
    }

    for t in request.tickers:
        if t:
            tokens["tickers"].add(t.strip())
            # Bare tickers (read_ready_evidence lane) get the same
            # position-topic inference as structured positions — otherwise
            # holdings questions carry no topics and the relevance gate
            # finds nothing.
            tokens["topics"].update(_infer_position_topics(t.strip(), ""))
    if request.ticker:
        tokens["tickers"].add(request.ticker.strip())

    if request.company:
        tokens["companies"].add(request.company.strip())

    for pos in request.positions:
        tk = str(pos.get("ticker", "")).strip()
        co = str(pos.get("company", "")).strip()
        if tk:
            tokens["tickers"].add(tk)
        if co:
            tokens["companies"].add(co)
        tokens["topics"].update(_infer_position_topics(tk, co))

    if request.topic:
        tokens["topics"].add(request.topic.strip())

    question_lower = request.question.lower()
    for keyword in _TOPIC_KEYWORDS:
        if keyword in question_lower or keyword in request.question:
            tokens["topics"].add(keyword)

    return tokens


def _infer_position_topics(ticker: str, company: str) -> set[str]:
    """Infer topic keywords from position ticker/company name."""
    haystack = f"{ticker} {company}"
    topics: set[str] = set()
    for needles, inferred_topics in _position_topic_rules():
        if any(needle and needle in haystack for needle in needles):
            topics.update(inferred_topics)
    return topics


def _is_latest_focus_query(
    request: AgentRuntimeContextRequest,
    intent_tokens: dict[str, set[str]],
) -> bool:
    """Detect a "最近关注什么变化" overview question with no explicit target.

    Latest-focus applies only when the user does NOT pin a specific
    ticker/company/topic (so it is a broad "what's changed recently" ask) AND
    the question combines a recency token (最近/近期/这几天/今天…) with a focus
    token (关注/主线/变化/看什么/重点/边际…).
    """
    if request.ticker or request.company or request.topic:
        return False
    if (
        intent_tokens.get("tickers")
        or intent_tokens.get("companies")
        or intent_tokens.get("topics")
    ):
        return False
    question = request.question or ""
    if not question:
        return False
    has_time = any(tok in question for tok in _LATEST_FOCUS_TIME_TOKENS)
    has_focus = any(tok in question for tok in _LATEST_FOCUS_FOCUS_TOKENS)
    return has_time and has_focus


def _is_broad_g_overview_query(
    request: AgentRuntimeContextRequest,
    _intent_tokens: dict[str, set[str]],
) -> bool:
    """Use the bounded whole-G view whenever the Agent did not bind a target."""

    return not (
        request.topic or request.ticker or request.tickers or request.company or request.positions
    )


def _is_recent_within(candidate: dict[str, Any], now: str, *, days: int) -> bool:
    """Return True when the candidate is within ``days`` CST days of ``now``."""
    ts = _candidate_time(candidate)
    if not ts:
        return False
    cand_dt = _coerce_datetime(ts)
    now_dt = _coerce_datetime(now) if now else datetime.now(tz=CST)
    if cand_dt is None or now_dt is None:
        return False
    diff = (now_dt.astimezone(CST).date() - cand_dt.astimezone(CST).date()).days
    return 0 <= diff <= days


def _latest_focus_rank_key(candidate: dict[str, Any]) -> tuple[float, int, str]:
    """Rank latest-focus reference candidates by signal, local readiness, recency.

    High metadata.score wins first; then locally-ready deep-read / T0 /
    judge-positive items; then recency (published/created timestamp).
    """
    raw_meta = candidate.get("metadata")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    raw_score = candidate.get("score", meta.get("score", 0))
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        score = 0.0
    ready = 0
    if candidate.get("requires_deep_read") or meta.get("requires_deep_read"):
        ready += 1
    stage = str(candidate.get("stage") or meta.get("stage") or "").lower()
    judgement = str(candidate.get("judgement") or meta.get("judgement") or "").lower()
    if stage in ("t0", "t1") or "positive" in judgement:
        ready += 1
    return (score, ready, _candidate_time(candidate))


def _reference_rank_key(candidate: dict[str, Any]) -> tuple[int, str]:
    """Rank reference candidates so fact-bearing rows precede empty shells.

    The ready projection later rejects rows with no tickers/companies/chain
    facts; ranking them last prevents them from consuming the lane budget first
    (BUG-012 残余二).  A stable secondary key keeps the original index order
    for equal-fact candidates.
    """
    tickers = [str(t).strip() for t in (candidate.get("tickers") or []) if str(t).strip()]
    companies = [str(co).strip() for co in (candidate.get("companies") or []) if str(co).strip()]
    chain_facts = [
        str(f).strip() for f in (candidate.get("industry_chain_facts") or []) if str(f).strip()
    ]
    return (1 if (tickers or companies or chain_facts) else 0, str(candidate.get("article_id", "")))


def _read_cache_candidates(kb_root: Path) -> list[dict[str, Any]]:
    """Read candidates from knowledge-base/runtime/cognition/priority_events.jsonl."""
    snapshot = _read_bounded_owned_regular_file_at(
        kb_root,
        _PRIORITY_EVENTS_CACHE_PATH,
        max_bytes=_MAX_PRIORITY_CACHE_BYTES,
    )
    if snapshot is None:
        return []
    candidates: list[dict[str, Any]] = []
    try:
        for line in snapshot[0].decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    candidates.append(obj)
            except json.JSONDecodeError:
                continue
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning("Failed to read priority_events.jsonl: %s", exc)
        return []
    return candidates


def _read_index_reference_candidates(kb_root: Path) -> tuple[list[dict[str, Any]], bool]:
    """BUG-012②：read_ready_evidence 参考巷道的候选源——canonical index.json。

    priority_events.jsonl 按设计只记 T0/T1 推送事件（G 级为主），同日 reference
    级材料结构性缺料；普通栏文章由 owner 裁定（2026-08-28）整体归 reference
    tier，其目录事实在 index.json。只读、有界；投影即 allowlist——仅普通栏行
    入候选（设计门 P2-3：防未来新增 G 列静默放行）。返回 (candidates,
    index_unavailable)；索引缺失/损坏 → 空候选 + typed gap 诚实空。
    """
    snapshot = _read_bounded_owned_regular_file_at(
        kb_root,
        Path("index.json"),
        max_bytes=_MAX_PRIORITY_CACHE_BYTES,
    )
    if snapshot is None:
        return [], True
    try:
        payload = json.loads(snapshot[0].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("Failed to parse knowledge index.json for reference lane")
        return [], True
    rows = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return [], True
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        article_id = str(row.get("id", "")).strip()
        column = str(row.get("column", "")).strip()
        title = str(row.get("title", "")).strip()
        if not article_id or not title or column != "普通":
            continue
        keywords = [
            tag.strip()[:16]
            for tag in (row.get("tags") or [])[:8]
            if isinstance(tag, str) and tag.strip()
        ]
        companies = [
            str(company).strip()
            for company in (row.get("companies") or [])[:8]
            if str(company).strip()
        ]
        candidates.append(
            {
                "article_id": article_id,
                "title": title,
                # 普通栏=reference tier；"observation" 是 strict 校验认的既有
                # reference 级分类（_REFERENCE_CLASSIFICATIONS）。
                "source_classification": "observation",
                "column": column,
                "published_at": str(row.get("date", "")).strip(),
                "keywords": keywords,
                "companies": companies,
                "metadata": {"column": column, "path": str(row.get("path", ""))},
            }
        )
    return candidates, False


def _read_g_index_articles(
    kb_root: Path,
    now: datetime,
) -> list[dict[str, object]] | None:
    """Read the same local index used by the G working-set manifest."""
    snapshot = _read_bounded_owned_regular_file_at(
        kb_root,
        Path("index.json"),
        max_bytes=_MAX_PRIORITY_CACHE_BYTES,
    )
    if snapshot is None:
        return None
    decoded = decode_g_knowledge_index(snapshot[0], now=now)
    return decoded[0] if decoded is not None else None


def _load_deep_read_content(kb_root: Path, article_id: str) -> dict[str, Any] | None:
    """Load a source-fresh compact artifact through the public service seam."""
    loaded = _project_fresh_deep_read_snapshot(
        _coerce_fresh_deep_read_snapshot(_load_fresh_deep_read(kb_root, article_id)),
        expected_article_id=article_id,
    )
    return loaded.compact if loaded is not None and loaded.compact else None


def _load_fresh_deep_read(
    kb_root: Path,
    article_id: str,
) -> _FreshDeepReadSnapshot | None:
    """Return one immutable compact snapshot with its artifact identities."""
    if not article_id:
        return None
    article_source = _read_reference_article_source(kb_root, {"article_id": article_id})
    if article_source is None:
        return None
    try:
        from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

        service = DeepReadArtifactService(kb_root=kb_root)
        pair = service.load_fresh_pair(article_id, article_source.path)
        if pair is None or pair.content_hash != article_source.raw_sha256:
            return None
        available_at = _latest_material_time(pair.generated_at, pair.article_modified_at)
        if not available_at:
            return None
        return _FreshDeepReadSnapshot(
            compact=deepcopy(pair.compact),
            available_at=available_at,
            content_hash=str(pair.content_hash),
            generation_id=str(pair.generation_id),
            compact_raw_sha256=str(pair.compact_raw_sha256),
        )
    except Exception:
        return None


def _coerce_fresh_deep_read_snapshot(
    loaded: object,
) -> _FreshDeepReadSnapshot | None:
    if isinstance(loaded, _FreshDeepReadSnapshot):
        return loaded
    if not isinstance(loaded, (list, tuple)) or len(loaded) not in (3, 5):
        return None
    compact = loaded[0]
    available_at = loaded[1]
    content_hash = loaded[2]
    if not (
        isinstance(compact, dict)
        and isinstance(available_at, str)
        and isinstance(content_hash, str)
    ):
        return None
    generation_id = loaded[3] if len(loaded) == 5 else ""
    compact_raw_sha256 = loaded[4] if len(loaded) == 5 else ""
    if not isinstance(generation_id, str) or not isinstance(compact_raw_sha256, str):
        return None
    return _FreshDeepReadSnapshot(
        compact=deepcopy(compact),
        available_at=available_at,
        content_hash=content_hash,
        generation_id=generation_id,
        compact_raw_sha256=compact_raw_sha256,
    )


def _load_index_articles_list(kb_root: Path) -> list[dict[str, Any]] | None:
    """Bounded 只读加载 index.json 的 articles 列表（无则 None）。"""
    snapshot = _read_bounded_owned_regular_file_at(
        kb_root,
        Path("index.json"),
        max_bytes=_MAX_PRIORITY_CACHE_BYTES,
    )
    if snapshot is None:
        return None
    try:
        index_data = json.loads(snapshot[0])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(index_data, dict):
        return None
    articles = index_data.get("articles", [])
    return articles if isinstance(articles, list) else None


def _is_resident_mainline_pinned(pinned: Any) -> tuple[bool, bool]:
    """M3/C1（配置化）：常驻主线由 pinned_sources.jsonl 条目
    ``resident_mainline: true`` 声明（用户拍板 2026-08-20：每周可能换常驻，
    改配置即生效——jsonl 每次 resolve 读取，无需改代码/重建 release）。
    返回 (is_resident, conflict=False)；无三键硬编码，配置即权威。
    """
    return bool(getattr(pinned, "resident_mainline", False)), False


def _apply_resident_mainline_compact_projection(candidate: dict[str, Any]) -> None:
    """M3：常驻主线压缩投影——只保留 title + theme 名 + suggestion 摘要。

    Agent-visible 验收值：theme_clusters ≤10 项、suggestions ≤10 项每项 ≤128
    chars、guidance_brief ≤2500 chars 且 ≤6000 bytes；超限截断并标记。
    """
    themes = [
        str(t).strip()
        for t in (candidate.get("theme_clusters") or [])
        if isinstance(t, str) and t.strip()
    ][:10]
    suggestions = []
    for sug in (candidate.get("suggestions") or [])[:10]:
        if not isinstance(sug, dict):
            continue
        level = str(sug.get("level") or "")
        summary = str(sug.get("summary") or "")[:128]
        if summary:
            suggestions.append(f"{level}: {summary}".strip())
    lines = []
    if themes:
        lines.append("主线主题：" + "；".join(themes))
    lines.extend(f"- {s}" for s in suggestions)
    summary_text = "\n".join(lines)
    summary_bytes = summary_text.encode("utf-8")
    if len(summary_bytes) > 6_000:
        summary_text = summary_text[: 6_000 // 4]
        candidate["projection_truncated"] = True
    candidate["guidance_brief"] = summary_text
    candidate["core_theses"] = []
    candidate["suggestions"] = suggestions
    candidate["theme_clusters"] = themes


def _canonical_article_id(
    kb_root: Path,
    raw: str,
    index_articles: list[dict[str, Any]] | None = None,
) -> tuple[str | None, bool]:
    """归一化 raw 来源标识到 index article id（C1/C3，用户拍板 2026-08-19）。

    raw 可为 URL、``zsxq-<topic>``、纯 topic_id 或已有 index id。从 index 的
    ``topic_id``/``id`` 精确匹配：恰好 1 条 → (canonical_id, True)；0 或多条
    → (None, False)——调用方必须 fail-closed（exclusion_unresolved + 拒绝表），
    不得静默继续。
    """
    if not raw:
        return None, False
    zsxq_id = raw.rsplit("/", 1)[-1].replace(".html", "").strip()
    if zsxq_id.startswith("zsxq-"):
        zsxq_id = zsxq_id[len("zsxq-"):]
    if not zsxq_id:
        return None, False
    if index_articles is None:
        snapshot = _read_bounded_owned_regular_file_at(
            kb_root,
            Path("index.json"),
            max_bytes=_MAX_PRIORITY_CACHE_BYTES,
        )
        if snapshot is None:
            return None, False
        try:
            index_data = json.loads(snapshot[0])
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, False
        index_articles = index_data.get("articles", []) if isinstance(index_data, dict) else []
        if not isinstance(index_articles, list):
            return None, False
    canonical = [
        str(a.get("id"))
        for a in index_articles
        if isinstance(a, dict)
        and (
            str(a.get("topic_id", "")) == zsxq_id
            or str(a.get("id", "")) == zsxq_id
        )
    ]
    if len(canonical) == 1:
        return canonical[0], True
    return None, False


def _resolve_zsxq_url_to_kb_article_id(kb_root: Path, raw: str) -> str | None:
    """Resolve a ZSXQ short article ID or URL to a KB article ID.

    Resolution chain:
    1. Extract the ZSXQ short ID from the URL (e.g. jtdl8mv3ptqu)
    2. Look up by article_url in index.json (exact match)
    3. Fall back to title keyword matching when URL lookup fails

    Returns the KB article_id if found, or None.
    """
    if not raw:
        return None

    # Extract the ZSXQ short ID from the URL
    zsxq_id = raw.rsplit("/", 1)[-1].replace(".html", "") if "/" in raw else raw
    if not zsxq_id:
        return None

    snapshot = _read_bounded_owned_regular_file_at(
        kb_root,
        Path("index.json"),
        max_bytes=_MAX_PRIORITY_CACHE_BYTES,
    )
    if snapshot is None:
        return None

    try:
        index_data = json.loads(snapshot[0])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(index_data, dict):
        return None

    articles = index_data.get("articles", [])
    if not isinstance(articles, list) or not articles:
        return None

    # Step 1: Exact match by article_url
    for a in articles[:20_000]:
        if not isinstance(a, dict):
            continue
        article_url = str(a.get("article_url", ""))
        if article_url and zsxq_id in article_url:
            return str(a.get("id", ""))

    # Step 2: Check if zsxq_id matches any article's id or article_url suffix
    for a in articles[:20_000]:
        if not isinstance(a, dict):
            continue
        article_url = str(a.get("article_url", ""))
        aid = str(a.get("id", ""))
        if zsxq_id == aid:
            return aid
        if article_url and article_url.endswith(zsxq_id + ".html"):
            return aid

    return None


def _candidate_half_life(candidate: dict[str, Any]) -> str:
    """Extract half_life_class from a priority_events candidate."""
    meta = candidate.get("metadata") or {}
    return str(candidate.get("half_life_class") or meta.get("half_life_class") or "")


# ── Deep Read content extraction ───────────────────────────────────────────────


def _extract_tickers_from_core_theses(
    kb_root: Path,
    dr: dict[str, Any],
) -> list[str]:
    """Extract tickers from deep_read core_theses.related_companies."""
    company_names: set[str] = set()
    for t in dr.get("core_theses", []) or []:
        if not isinstance(t, dict):
            continue
        for co in t.get("related_companies") or []:
            if co and isinstance(co, str):
                company_names.add(co.strip())
    return _map_companies_to_tickers(kb_root, company_names)


def _extract_companies_from_core_theses(dr: dict[str, Any]) -> list[str]:
    """Extract unique company names from deep_read core_theses."""
    names: set[str] = set()
    for t in dr.get("core_theses", []) or []:
        if not isinstance(t, dict):
            continue
        for co in t.get("related_companies") or []:
            if co and isinstance(co, str):
                names.add(co.strip())
    return sorted(names)


def _extract_theme_names(dr: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for cluster in dr.get("theme_clusters", []) or []:
        if not isinstance(cluster, dict):
            continue
        name = cluster.get("name")
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return names


def _map_companies_to_tickers(kb_root: Path, company_names: set[str]) -> list[str]:
    """Map company names to A-share tickers via name map cache."""
    tickers: list[str] = []
    try:
        snapshot = _read_bounded_owned_regular_file_at(
            kb_root,
            Path("runtime/a_share_name_map.json"),
            max_bytes=_MAX_PRIORITY_CACHE_BYTES,
        )
        if snapshot is None:
            return tickers
        data = json.loads(snapshot[0])
        if not isinstance(data, dict):
            return tickers
        entries = data.get("entries", {})
        if not isinstance(entries, dict):
            return tickers
        for name in company_names:
            entry = entries.get(name)
            if isinstance(entry, dict) and entry.get("ticker"):
                tickers.append(str(entry["ticker"]))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        pass
    return tickers


def _enrich_candidates_with_deep_read(
    kb_root: Path,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Best-effort compact deep_read enrichment for information-driven selection.

    Loads compact deep_read artifacts and extracts information units
    (companies, tickers, topics, themes) into private _enriched_* fields
    so that _candidate_relevance_score can use them during selection.

    Every candidate receives a private semantic status. Production selection
    uses it to exclude compact artifacts that contain only article identity or
    title text before ranking; fixture-backed callers retain their explicit
    snapshot semantics.
    """
    for c in candidates:
        for private_key in (
            "_fresh_deep_read_snapshot",
            "_fresh_deep_read_checked",
            "_deep_read_semantic_status",
            "_enriched_companies",
            "_enriched_tickers",
            "_enriched_topics",
            "_enriched_themes",
        ):
            c.pop(private_key, None)
        article_id = str(c.get("article_id", ""))
        snapshot = _project_fresh_deep_read_snapshot(
            _coerce_fresh_deep_read_snapshot(_load_fresh_deep_read(kb_root, article_id)),
            expected_article_id=article_id,
        )
        c["_fresh_deep_read_checked"] = True
        if snapshot is not None:
            c["_fresh_deep_read_snapshot"] = snapshot
        dr = snapshot.compact if snapshot is not None else None
        if snapshot is None:
            c["_deep_read_semantic_status"] = "missing"
            continue
        if not dr or not _deep_read_has_extractable_units(
            dr,
            expected_article_id=article_id,
        ):
            c["_deep_read_semantic_status"] = "no_extractable_units"
            # 空壳回退：compact 抽取失败时用 bounded 原始正文，而不是只给标题。
            _fallback_raw_guidance(kb_root, c)
            continue
        c["_deep_read_semantic_status"] = "extractable"

        # Extract companies and topics from core_theses
        enriched_companies: list[str] = []
        enriched_topics: list[str] = []
        for t in dr.get("core_theses") or []:
            if not isinstance(t, dict):
                continue
            for co in t.get("related_companies") or []:
                if co and isinstance(co, str) and co.strip() not in enriched_companies:
                    enriched_companies.append(co.strip())
            for topic in t.get("related_topics") or []:
                if topic and isinstance(topic, str) and topic.strip() not in enriched_topics:
                    enriched_topics.append(topic.strip())

        # Extract themes from theme_clusters
        enriched_themes: list[str] = []
        for tc in dr.get("theme_clusters") or []:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name", "")
            if name and name not in enriched_themes:
                enriched_themes.append(name)

        # Map companies to tickers
        enriched_tickers = _map_companies_to_tickers(kb_root, set(enriched_companies))

        c["_enriched_companies"] = enriched_companies
        c["_enriched_tickers"] = enriched_tickers
        c["_enriched_topics"] = enriched_topics
        c["_enriched_themes"] = enriched_themes

    return candidates


def _deep_read_has_extractable_units(
    payload: dict[str, Any],
    *,
    expected_article_id: str,
) -> bool:
    projected = _project_deep_read_payload(
        payload,
        expected_article_id=expected_article_id,
    )
    if projected is None:
        return False
    core_theses = projected["core_theses"]
    thesis_texts = [
        thesis["thesis"].strip()
        for thesis in core_theses
        if isinstance(thesis.get("thesis"), str) and thesis["thesis"].strip()
    ]
    if not thesis_texts or len(thesis_texts) != len(core_theses):
        return False
    unit_count = projected.get("unit_count")
    if unit_count is not None and unit_count <= 0:
        return False
    guidance = projected["injectable_summary"][:_MAX_AGENT_VISIBLE_G_GUIDANCE_CHARS]
    return any(thesis in guidance for thesis in thesis_texts)


def _deep_read_payload_is_well_formed(
    payload: dict[str, Any],
    *,
    expected_article_id: str,
) -> bool:
    article_id = payload.get("article_id")
    title = payload.get("title")
    guidance = payload.get("injectable_summary")
    core_theses = payload.get("core_theses")
    theme_clusters = payload.get("theme_clusters")
    suggestions = payload.get("suggestions")
    unit_count = payload.get("unit_count")
    if (
        not _bounded_text(article_id, limit=160, allow_empty=False)
        or article_id != expected_article_id
        or not _bounded_text(title, limit=1_000)
        or not _bounded_text(guidance, limit=1_048_576)
        or not isinstance(core_theses, list)
        or len(core_theses) > 256
        or not isinstance(theme_clusters, list)
        or len(theme_clusters) > 128
        or not isinstance(suggestions, list)
        or len(suggestions) > 128
        or isinstance(unit_count, bool)
        or not isinstance(unit_count, int)
        or unit_count < 0
        or unit_count != len(core_theses)
    ):
        return False
    for thesis in core_theses:
        if (
            not isinstance(thesis, dict)
            or not _bounded_text(thesis.get("title"), limit=1_000, allow_empty=False)
            or not _bounded_text(thesis.get("thesis"), limit=32_768, allow_empty=False)
            or not _bounded_confidence(thesis.get("confidence"))
        ):
            return False
        for key in ("related_companies", "related_topics"):
            if not _bounded_text_list(
                thesis.get(key),
                max_items=256,
                text_limit=1_000,
            ):
                return False
    # G 方法论层:methodology_rules 是附加视图,不参与"compact 是否有效"判定——
    # 坏/超量规则只在投影时逐条丢弃(M2:不废整份 compact,不连累 core_theses)。
    for cluster in theme_clusters:
        cluster_unit_count = cluster.get("unit_count") if isinstance(cluster, dict) else None
        if (
            not isinstance(cluster, dict)
            or not _bounded_text(cluster.get("name"), limit=1_000, allow_empty=False)
            or not _bounded_text(
                cluster.get("active_status"),
                limit=200,
                allow_empty=False,
            )
            or isinstance(cluster_unit_count, bool)
            or not isinstance(cluster_unit_count, int)
            or cluster_unit_count < 0
            or not _bounded_text_list(
                cluster.get("core_theses"),
                max_items=256,
                text_limit=32_768,
            )
            or cluster_unit_count != len(cluster["core_theses"])
        ):
            return False
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            return False
        if (
            not _bounded_text(suggestion.get("level"), limit=200, allow_empty=False)
            or not _bounded_text(
                suggestion.get("summary"),
                limit=32_768,
                allow_empty=False,
            )
            or not _bounded_confidence(suggestion.get("confidence"))
        ):
            return False
        for key in (
            "tracking_indicators",
            "risk_boundaries",
            "allowed_usage",
            "forbidden_usage",
        ):
            if not _bounded_text_list(
                suggestion.get(key),
                max_items=256,
                text_limit=8_192,
            ):
                return False
    return True


def _project_methodology_rules(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """逐条严格过滤 methodology_rules 附加视图(M2:坏条目跳过、超 32 截断)。

    与闭合 reader 合同一致(每条字段有界、confidence finite [0,1]、topics 有界);
    单条不合格只丢弃该条,不废弃整份 compact,不连累 core_theses。
    """
    raw_rules = payload.get("methodology_rules")
    if not isinstance(raw_rules, list):
        return []
    projected: list[dict[str, Any]] = []
    for rule in raw_rules:
        if not isinstance(rule, dict) or len(projected) >= 32:
            continue
        title = rule.get("title")
        rule_text = rule.get("rule")
        teacher_quote = rule.get("teacher_quote")
        related_topics = rule.get("related_topics")
        confidence = rule.get("confidence")
        if (
            not _bounded_text(title, limit=100, allow_empty=False)
            or not _bounded_text(rule_text, limit=1_000, allow_empty=False)
            or not _bounded_text(teacher_quote, limit=500, allow_empty=False)
            or not _bounded_text(rule.get("apprentice_interpretation"), limit=2_000)
            or not _bounded_confidence(confidence)
            or not _bounded_text_list(related_topics, max_items=32, text_limit=1_000)
            or not _bounded_text(rule.get("source_id"), limit=300)
            or not _bounded_text(rule.get("article_id"), limit=160, allow_empty=False)
            or not _bounded_text(rule.get("published_at"), limit=64)
            or not _bounded_text(rule.get("generation_id"), limit=64)
        ):
            continue
        if not isinstance(related_topics, (list, tuple)):
            continue
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            continue
        projected.append(
            {
                "title": title,
                "rule": rule_text,
                "teacher_quote": teacher_quote,
                "apprentice_interpretation": str(rule.get("apprentice_interpretation") or ""),
                "related_topics": [
                    str(topic) for topic in related_topics if isinstance(topic, str) and topic
                ],
                "confidence": float(confidence),
                "source_id": str(rule.get("source_id") or ""),
                "article_id": str(rule.get("article_id") or ""),
                "published_at": str(rule.get("published_at") or ""),
                "generation_id": str(rule.get("generation_id") or ""),
            }
        )
    return projected


def _project_deep_read_payload(
    payload: dict[str, Any],
    *,
    expected_article_id: str,
) -> dict[str, Any] | None:
    """Return the closed, bounded deep-read shape that may enter Agent context."""
    if not _deep_read_payload_is_well_formed(
        payload,
        expected_article_id=expected_article_id,
    ):
        return None
    try:
        projected = {
            "article_id": payload["article_id"],
            "title": payload["title"],
            "unit_count": payload["unit_count"],
            "core_theses": [
                {
                    "title": thesis["title"],
                    "thesis": thesis["thesis"],
                    "related_companies": list(thesis["related_companies"]),
                    "related_topics": list(thesis["related_topics"]),
                    "confidence": float(thesis["confidence"]),
                }
                for thesis in payload["core_theses"]
            ],
            "theme_clusters": [
                {
                    "name": cluster["name"],
                    "active_status": cluster["active_status"],
                    "unit_count": cluster["unit_count"],
                    "core_theses": list(cluster["core_theses"]),
                }
                for cluster in payload["theme_clusters"]
            ],
            "suggestions": [
                {
                    "level": suggestion["level"],
                    "summary": suggestion["summary"],
                    "tracking_indicators": list(suggestion["tracking_indicators"]),
                    "risk_boundaries": list(suggestion["risk_boundaries"]),
                    "allowed_usage": list(suggestion["allowed_usage"]),
                    "forbidden_usage": list(suggestion["forbidden_usage"]),
                    "confidence": float(suggestion["confidence"]),
                }
                for suggestion in payload["suggestions"]
            ],
            "methodology_rules": _project_methodology_rules(payload),
            "injectable_summary": payload["injectable_summary"],
        }
        encoded = json.dumps(
            projected,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (KeyError, OverflowError, TypeError, ValueError):
        return None
    if len(encoded) > _MAX_DEEP_READ_PROJECTION_BYTES:
        return None
    return projected


def _project_fresh_deep_read_snapshot(
    snapshot: _FreshDeepReadSnapshot | None,
    *,
    expected_article_id: str,
) -> _FreshDeepReadSnapshot | None:
    """Preserve immutable identities while removing untrusted compact fields."""
    if snapshot is None:
        return None
    projected = _project_deep_read_payload(
        snapshot.compact,
        expected_article_id=expected_article_id,
    )
    return _FreshDeepReadSnapshot(
        compact=projected or {},
        available_at=snapshot.available_at,
        content_hash=snapshot.content_hash,
        generation_id=snapshot.generation_id,
        compact_raw_sha256=snapshot.compact_raw_sha256,
    )


def _bounded_text(
    value: object,
    *,
    limit: int,
    allow_empty: bool = True,
) -> bool:
    return isinstance(value, str) and len(value) <= limit and (allow_empty or bool(value.strip()))


def _bounded_text_list(
    value: object,
    *,
    max_items: int,
    text_limit: int,
) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= max_items
        and all(_bounded_text(item, limit=text_limit, allow_empty=False) for item in value)
    )


def _bounded_confidence(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        return False
    return isfinite(normalized) and 0.0 <= normalized <= 1.0


def _manifest_deep_read_bindings(
    assessment: object,
) -> dict[str, dict[str, str]] | None:
    manifest = getattr(assessment, "manifest", None)
    if not isinstance(manifest, dict):
        return None
    articles = manifest.get("articles")
    if not isinstance(articles, list):
        return None
    bindings: dict[str, dict[str, str]] = {}
    for article in articles:
        if not isinstance(article, dict):
            return None
        article_id = article.get("article_id")
        deep_read = article.get("deep_read")
        if (
            not isinstance(article_id, str)
            or not article_id
            or article_id in bindings
            or not isinstance(deep_read, dict)
            or deep_read.get("available") is not True
        ):
            return None
        identity = {
            key: deep_read.get(key)
            for key in ("generation_id", "content_hash", "compact_raw_sha256")
        }
        if any(not isinstance(value, str) or not value for value in identity.values()):
            return None
        bindings[article_id] = {
            key: value for key, value in identity.items() if isinstance(value, str)
        }
    return bindings


def _active_selection_matches_ready_manifest(
    assessment: object,
    active_selection: object,
    *,
    now: datetime,
) -> bool:
    """Bind one READY manifest to the exact index/event selection just read.

    Time passing is expected: a bound article that has since slid outside its
    active window may legitimately be absent from the live selection.  Every
    article still inside its window must match exactly, and the live selection
    must not introduce articles the manifest never bound (concurrent
    generation guard preserved).
    """
    manifest = getattr(assessment, "manifest", None)
    manifest_articles = manifest.get("articles") if isinstance(manifest, Mapping) else None
    active_candidates = getattr(active_selection, "candidates", None)
    if not isinstance(manifest_articles, list) or not isinstance(active_candidates, (list, tuple)):
        return False
    if len(active_candidates) > len(manifest_articles):
        return False

    active_by_id: dict[str, object] = {}
    for active_candidate in active_candidates:
        article_id = getattr(active_candidate, "article_id", None)
        if not isinstance(article_id, str) or not article_id or article_id in active_by_id:
            return False
        active_by_id[article_id] = active_candidate

    manifest_by_id: dict[str, Mapping] = {}
    for manifest_article in manifest_articles:
        if not isinstance(manifest_article, Mapping):
            return False
        article_id = manifest_article.get("article_id")
        if not isinstance(article_id, str) or not article_id or article_id in manifest_by_id:
            return False
        manifest_by_id[article_id] = manifest_article

    for article_id, active_candidate in active_by_id.items():
        manifest_article = manifest_by_id.get(article_id)
        if manifest_article is None:
            return False
        if (
            manifest_article.get("column") != getattr(active_candidate, "column", None)
            or manifest_article.get("title") != getattr(active_candidate, "title", None)
            or manifest_article.get("published_at")
            != getattr(active_candidate, "published_at", None)
        ):
            return False
        index_entry = getattr(active_candidate, "index_entry", None)
        entry_sha256 = _canonical_mapping_sha256(index_entry)
        if (
            not entry_sha256
            or getattr(active_candidate, "entry_sha256", None) != entry_sha256
            or manifest_article.get("index_entry_sha256") != entry_sha256
        ):
            return False
        priority_event = getattr(active_candidate, "priority_event", None)
        event_sha256 = _canonical_mapping_sha256(priority_event)
        if (
            not isinstance(priority_event, Mapping)
            or not event_sha256
            or manifest_article.get("priority_event_id") != priority_event.get("event_id")
            or manifest_article.get("priority_event_sha256") != event_sha256
        ):
            return False

    for article_id, manifest_article in manifest_by_id.items():
        if article_id in active_by_id:
            continue
        if not _manifest_article_slid_out(manifest_article, now):
            return False
    return True


def _manifest_article_slid_out(manifest_article: Mapping, now: datetime) -> bool:
    """True when a bound article's active window has already closed at ``now``."""
    published_raw = manifest_article.get("published_at")
    published = _parse_datetime(published_raw) if isinstance(published_raw, str) else None
    if published is None or published > now:
        return False
    column = manifest_article.get("column")
    if column in _COMMENTARY_COLUMNS:
        # BUG-006③：锐评滑出判定与选择层同用交易日语义，避免准入/滑出口径分裂。
        cutoff, _ = trading_window_cutoff(now, load_g_window_config().commentary_trading_days)
        return published < cutoff
    return now - published > timedelta(days=load_g_window_config().special_report_days)


def _canonical_mapping_sha256(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    projection = dict(value)
    projection.pop("canonical_sha256", None)
    try:
        raw = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(raw).hexdigest()


def _deep_read_snapshot_matches_manifest(
    snapshot: _FreshDeepReadSnapshot,
    binding: dict[str, str],
) -> bool:
    return (
        snapshot.generation_id == binding.get("generation_id")
        and snapshot.content_hash == binding.get("content_hash")
        and snapshot.compact_raw_sha256 == binding.get("compact_raw_sha256")
    )


def _build_why_available(
    candidate: dict[str, Any],
    bucket: str,
    has_deep_read: bool,
    intent_tokens: dict[str, set[str]],
) -> list[str]:
    """Build why_available list — includes selection reasons AND matching info."""
    reasons: list[str] = []

    # Source bucket
    if bucket == "latest_commentary":
        reasons.append("latest_commentary")
    elif bucket == "target_relevant_commentary":
        reasons.append("target_relevant_commentary")
    elif bucket == "theme_special_report":
        reasons.append("theme_special_report")

    # Deep read status
    if has_deep_read:
        reasons.append("deep_read_content")

    # Matching reasons — tell LLM WHY this article was selected
    title = str(candidate.get("title", ""))
    tickers = [str(t).strip() for t in (candidate.get("tickers", []) or [])]
    companies = [str(co).strip() for co in (candidate.get("companies", []) or [])]

    matched = False
    for tk in tickers:
        if tk in intent_tokens["tickers"]:
            reasons.append("position_overlap")
            matched = True
            break
    if not matched:
        for co in companies:
            if co in intent_tokens["companies"]:
                reasons.append("position_overlap")
                matched = True
                break
    if not matched:
        for co_name in intent_tokens["companies"]:
            if co_name and co_name in title:
                reasons.append("company_title_match")
                matched = True
                break

    for topic in intent_tokens["topics"]:
        if topic and topic in title:
            reasons.append("theme_overlap")
            break

    if bool(candidate.get("deep_read_complete", True)):
        reasons.append("deep_read_complete")

    reasons.append("g_source_background")
    return reasons


# ── G-source filtering ────────────────────────────────────────────────────────


def _filter_g_source(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only G-source eligible candidates."""
    result: list[dict[str, Any]] = []
    for c in candidates:
        if _g_source_decision(c).eligible:
            result.append(c)
    return result


def _is_g_source(candidate: dict[str, Any]) -> bool:
    """Check if a single candidate is a valid G source."""
    return _g_source_decision(candidate).eligible


def _g_source_decision(candidate: Mapping[str, object]) -> GSourceDecision:
    """Apply the exact source contract before a candidate can enter G.

    Persona eligibility is intentionally not an alternative path here: it
    cannot turn a generic label or a non-original source into a G source.
    """

    raw_metadata = candidate.get("metadata")
    metadata: Mapping[str, object] = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    source_classification = str(candidate.get("source_classification", ""))
    is_qa = candidate.get("is_qa") is True or metadata.get("is_qa") is True
    priority_label = candidate.get("priority_label", metadata.get("priority_label"))
    return classify_g_source(
        _candidate_column(dict(candidate)),
        teacher_original=source_classification in _G_SOURCE_CLASSIFICATIONS,
        is_qa=is_qa,
        priority_label=priority_label,
    )


_MAX_FALLBACK_RAW_CHARS: Final = 1200


def _fallback_raw_guidance(kb_root: Path, candidate: dict[str, Any]) -> None:
    """Fall back to bounded raw article body when the compact is an empty shell.

    deep_read extraction can fail (JSON parse) while the article itself is
    useful; giving codex only the title template hides the content.  The
    bounded body (frontmatter stripped) replaces ``guidance_brief`` and is
    marked ``_fallback_raw_content`` for audit.
    """

    article_id = str(candidate.get("article_id", "")).strip()
    if not article_id:
        return
    article_path = kb_root / "articles" / f"{article_id}.md"
    if not article_path.is_file() or article_path.is_symlink():
        # M4（用户拍板 2026-08-19）：articles/ 文件名带日期前缀
        # （20260818_zsxq-…md），精确名找不到——经 index.json 的
        # id/topic_id → path 定位 basename 后按 kb_root 查找；未命中维持
        # 原行为（不注入、不计成功）。符号链接/非 owner 一律拒绝。
        index_articles = _load_index_articles_list(kb_root)
        target_basename = ""
        for entry in index_articles or []:
            if not isinstance(entry, dict):
                continue
            if (
                str(entry.get("id", "")) == article_id
                or str(entry.get("topic_id", "")) == article_id
            ):
                path_value = str(entry.get("path", ""))
                if path_value:
                    target_basename = path_value.rsplit("/", 1)[-1]
                    break
        if not target_basename:
            return
        candidate_path = kb_root / "articles" / target_basename
        if (
            not candidate_path.is_file()
            or candidate_path.is_symlink()
            or candidate_path.stat().st_size > _MAX_FALLBACK_RAW_CHARS * 4
        ):
            return
        article_path = candidate_path
    try:
        text = article_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = text.splitlines()
    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    body = "\n".join(lines[closing_index + 1 :]) if closing_index is not None else text
    body = body.strip()
    if not body:
        return
    candidate["guidance_brief"] = body[:_MAX_FALLBACK_RAW_CHARS]
    candidate["_fallback_raw_content"] = True


def _is_qa_article(candidate: dict[str, Any]) -> bool:
    """Return True if the candidate is a Q&A (user question), not original content."""
    meta = candidate.get("metadata")
    if isinstance(meta, dict) and meta.get("is_qa"):
        return True
    title = str(candidate.get("title", ""))
    return "提问" in title or "?" in title or "？" in title


def _qa_excluded(candidate: dict[str, Any]) -> bool:
    """QA 排除规则：好问题列 + teacher_original 放行（老师回答会员提问
    含老师原文观点），其他 QA（普通/提问）保持排除。"""

    if not _is_qa_article(candidate):
        return False
    return not (
        _candidate_column(candidate) in _GOOD_QUESTION_COLUMNS
        and str(candidate.get("source_classification", "")) == "teacher_original"
    )


# ── Deduplication ─────────────────────────────────────────────────────────────


def _deduplicate_by_title(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove candidates with duplicate titles, keeping the first occurrence."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for c in candidates:
        title = str(c.get("title", "")).strip()
        normalized = title[:40]
        if normalized not in seen:
            seen.add(normalized)
            result.append(c)
    return result


# ── Selection ─────────────────────────────────────────────────────────────────


def _select_context_candidates(
    candidates: list[dict[str, Any]],
    intent_tokens: dict[str, set[str]],
    max_events: int,
    *,
    require_agent_visible_relevance: bool = False,
    require_relevant_special_reports: bool = False,
) -> list[dict[str, Any]]:
    """Select latest market G, then target-relevant G within the same budget."""
    if max_events <= 0:
        return []
    if require_agent_visible_relevance:
        candidates = [
            candidate
            for candidate in candidates
            if _candidate_column(candidate) in _COMMENTARY_COLUMNS
            or _agent_visible_relevance_score(candidate, intent_tokens) > 0
        ]

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()

    # 1 latest 锐评 (already time-filtered by the trading-day window)
    latest_commentary = _latest_commentary(candidates)
    if latest_commentary is not None:
        _append_selected(
            selected,
            selected_keys,
            latest_commentary,
            bucket="latest_commentary",
        )

    remaining = max_events - len(selected)
    if remaining <= 0:
        return selected

    # M1（用户拍板 2026-08-19）：锐评只消费最新一条（已在上方冻结）；旧锐评
    # 一律不再追加。剩余预算只给特刊/好问题。

    # N 特刊/好问题 ranked by relevance (exclude short_signal — too transient
    # for background; 好问题 = 老师原文观点，对历史观点类问题有直接价值，
    # 且严格要求 teacher_original——AI 助理解答不是老师观点)
    special_reports = [
        c
        for c in candidates
        if _candidate_column(c) in (_SPECIAL_REPORT_COLUMNS | _GOOD_QUESTION_COLUMNS)
        and _candidate_key(c) not in selected_keys
        and _candidate_half_life(c) != "short_signal"
        and (
            _candidate_column(c) not in _GOOD_QUESTION_COLUMNS
            or str(c.get("source_classification", "")) == "teacher_original"
        )
    ]
    if require_relevant_special_reports:
        special_reports = [
            candidate
            for candidate in special_reports
            if _agent_visible_relevance_score(candidate, intent_tokens) > 0
        ]
    for candidate in _score_and_rank(special_reports, intent_tokens)[:remaining]:
        _append_selected(
            selected,
            selected_keys,
            candidate,
            bucket="theme_special_report",
        )

    return selected


def _latest_commentary(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the latest 锐评 from candidates."""
    commentaries = [c for c in candidates if _candidate_column(c) in _COMMENTARY_COLUMNS]
    if not commentaries:
        return None
    return sorted(commentaries, key=_candidate_time, reverse=True)[0]


def _append_selected(
    selected: list[dict[str, Any]],
    selected_keys: set[str],
    candidate: dict[str, Any],
    *,
    bucket: str,
) -> None:
    key = _candidate_key(candidate)
    if key in selected_keys:
        return
    selected_keys.add(key)
    copied = dict(candidate)
    copied["_fresh_g_selection_bucket"] = bucket
    selected.append(copied)


def _candidate_key(candidate: dict[str, Any]) -> str:
    return (
        str(candidate.get("event_id") or "")
        or str(candidate.get("article_id") or "")
        or str(candidate.get("title") or "")
    )


# ── Scoring ───────────────────────────────────────────────────────────────────


def _score_and_rank(
    candidates: list[dict[str, Any]],
    intent_tokens: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Score candidates by relevance to intent, return ranked list."""
    ranked = sorted(
        candidates,
        key=lambda c: (_candidate_score(c, intent_tokens), _candidate_time(c)),
        reverse=True,
    )
    return ranked


def _candidate_score(
    candidate: dict[str, Any],
    intent_tokens: dict[str, set[str]],
) -> int:
    """Score a candidate: relevance + source authority + column quality."""
    score = _candidate_relevance_score(candidate, intent_tokens)

    # Source authority
    sc = str(candidate.get("source_classification", ""))
    if sc == "teacher_original":
        score += 2
    if bool(candidate.get("persona_eligible", False)):
        score += 1
    if bool(candidate.get("deep_read_complete", True)):
        score += 1

    # Column-based quality boost (FreshG rules: 特刊 > 锐评)
    col = _candidate_column(candidate)
    if col == "星大派特刊":
        score += 5
    elif col == "星大派锐评":
        score += 3

    # The label can only increase retrieval/recheck ordering. It never changes
    # source authority, freshness, content time sensitivity, or validity.
    if _candidate_priority_label(candidate) == "重中之重":
        score += 1

    # Half-life class: methodology articles have long-term value
    hl = _candidate_half_life(candidate)
    if hl == "long_methodology":
        score += 3

    # Pinned boost
    if candidate.get("source_bucket") == "pinned_source":
        boost = float(candidate.get("pinned_boost_factor", 2.0))
        score = int(score * boost)

    return score


def _candidate_relevance_score(
    candidate: dict[str, Any],
    intent_tokens: dict[str, set[str]],
) -> int:
    """Score candidate relevance to intent tokens.

    Merges shallow fields (title, tickers, companies, theme_clusters) and
    enriched information-unit fields (_enriched_tickers, _enriched_companies,
    _enriched_topics, _enriched_themes) from compact deep_read.

    Information-unit matches receive higher weight than shallow-field matches
    because they represent extracted investment theses, not just keyword hits.
    """
    score = 0
    title = str(candidate.get("title", ""))
    tickers = [str(t).strip() for t in (candidate.get("tickers", []) or [])]
    companies = [str(co).strip() for co in (candidate.get("companies", []) or [])]
    themes = [str(t).strip() for t in (candidate.get("theme_clusters", []) or [])]

    # ── Shallow field scoring ──
    for tk in tickers:
        if tk in intent_tokens["tickers"]:
            score += 10
    for co in companies:
        if co in intent_tokens["companies"]:
            score += 10
    for co_name in intent_tokens["companies"]:
        if co_name and co_name in title:
            score += 8
    for tk in intent_tokens["tickers"]:
        if tk and tk in title:
            score += 8
    for topic in intent_tokens["topics"]:
        if topic and topic in title:
            score += 5
    for theme in themes:
        if theme in intent_tokens["topics"]:
            score += 4

    # ── Information-unit scoring (from compact deep_read enrichment) ──
    # Higher weight: these are extracted investment theses, not keyword hits.
    enriched_tickers = [str(t).strip() for t in (candidate.get("_enriched_tickers") or [])]
    enriched_companies = [str(co).strip() for co in (candidate.get("_enriched_companies") or [])]
    enriched_topics = [str(t).strip() for t in (candidate.get("_enriched_topics") or [])]
    enriched_themes = [str(t).strip() for t in (candidate.get("_enriched_themes") or [])]

    for tk in enriched_tickers:
        if tk in intent_tokens["tickers"]:
            score += 12
    for co in enriched_companies:
        if co in intent_tokens["companies"]:
            score += 12
    for topic in enriched_topics:
        if topic in intent_tokens["topics"]:
            score += 6
    for theme in enriched_themes:
        if theme in intent_tokens["topics"]:
            score += 6

    return score


def _agent_visible_relevance_score(
    candidate: dict[str, Any],
    intent_tokens: dict[str, set[str]],
) -> int:
    """Score only typed compact fields that are projected to the Agent."""
    score = 0
    enriched_tickers = {
        str(value).strip()
        for value in (candidate.get("_enriched_tickers") or [])
        if str(value).strip()
    }
    enriched_companies = {
        str(value).strip()
        for value in (candidate.get("_enriched_companies") or [])
        if str(value).strip()
    }
    enriched_topics = {
        str(value).strip()
        for value in (
            *(candidate.get("_enriched_topics") or []),
            *(candidate.get("_enriched_themes") or []),
        )
        if str(value).strip()
    }
    score += 12 * len(enriched_tickers & intent_tokens["tickers"])
    score += 12 * len(enriched_companies & intent_tokens["companies"])
    for enriched_topic in enriched_topics:
        for intent_topic in intent_tokens["topics"]:
            if (
                enriched_topic == intent_topic
                or enriched_topic in intent_topic
                or intent_topic in enriched_topic
            ):
                score += 6
                break
    return score


# ── Temporal assessment ───────────────────────────────────────────────────────


def _assess_single(
    candidate: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    """Call TemporalService().assess() for a single candidate."""
    item = TemporalItem(
        item_id=str(candidate.get("article_id", "")),
        title=str(candidate.get("title", "")),
        source_scope="g_source",
        source_classification=str(candidate.get("source_classification", "")),
        column=_candidate_column(candidate),
        published_at=_candidate_time(candidate),
        semantic_payload={
            "theme_clusters": candidate.get("theme_clusters", []),
            "tickers": candidate.get("tickers", []),
            "companies": candidate.get("companies", []),
        },
        quality_flags={
            "persona_eligible": bool(candidate.get("persona_eligible", False)),
            "deep_read_complete": bool(candidate.get("deep_read_complete", True)),
            "deep_read_degraded": not bool(candidate.get("deep_read_complete", True)),
        },
    )
    try:
        assessment = TemporalService().assess(
            TemporalAssessmentRequest(
                item_type="g_source_article",
                context_mode="priority_article",
                now=now,
                items=(item,),
                task=TemporalTaskContext(),
            )
        )
        return {
            "content_time_sensitivity": assessment.content_time_sensitivity,
            "publish_freshness": assessment.publish_freshness,
            "attention_policy": assessment.attention_policy,
            "confidence_modifier": assessment.confidence_modifier,
            "confidence_boost_allowed": assessment.confidence_boost_allowed,
            "data_gaps": list(assessment.data_gaps),
        }
    except Exception as exc:
        logger.warning("TemporalService.assess() failed for %s: %s", item.item_id, exc)
        return {
            "content_time_sensitivity": {},
            "publish_freshness": "",
            "attention_policy": {},
            "confidence_modifier": 0.0,
            "confidence_boost_allowed": False,
            "data_gaps": ["temporal_assessment_error"],
        }


# ── Event entry building ─────────────────────────────────────────────────────


def _compute_matches(
    candidate: dict[str, Any],
    intent_tokens: dict[str, set[str]],
) -> tuple[list[str], list[str]]:
    """Compute which targets/themes from intent_tokens match this candidate.

    Returns (matched_targets, matched_themes).  Called for both deep_read
    and fallback paths so LLM always knows why this article was selected.
    """
    title = str(candidate.get("title", ""))
    tickers = [str(t).strip() for t in (candidate.get("tickers", []) or [])]
    companies = [str(co).strip() for co in (candidate.get("companies", []) or [])]

    matched_targets: list[str] = []
    matched_themes: list[str] = []

    for tk in tickers:
        if tk in intent_tokens["tickers"]:
            matched_targets.append(tk)
    for co in companies:
        if co in intent_tokens["companies"] and co not in matched_targets:
            matched_targets.append(co)
    for co_name in intent_tokens["companies"]:
        if co_name and co_name in title and co_name not in matched_targets:
            matched_targets.append(co_name)

    for topic in intent_tokens["topics"]:
        if topic and topic in title:
            matched_themes.append(topic)

    return matched_targets, matched_themes


def _build_event_entry(
    candidate: dict[str, Any],
    temporal: dict[str, Any],
    intent_tokens: dict[str, set[str]],
) -> dict[str, Any]:
    """Build a single event entry for the result."""
    title = str(candidate.get("title", ""))
    tickers = [str(t).strip() for t in (candidate.get("tickers", []) or [])]
    companies = [str(co).strip() for co in (candidate.get("companies", []) or [])]
    themes = [str(t).strip() for t in (candidate.get("theme_clusters", []) or [])]

    matched_targets: list[str] = []
    matched_themes: list[str] = []
    why_selected: list[str] = []

    for tk in tickers:
        if tk in intent_tokens["tickers"]:
            matched_targets.append(tk)
            why_selected.append("position_overlap")
    for co in companies:
        if co in intent_tokens["companies"]:
            if co not in matched_targets:
                matched_targets.append(co)
            if "position_overlap" not in why_selected:
                why_selected.append("position_overlap")
    for co_name in intent_tokens["companies"]:
        if co_name and co_name in title and co_name not in matched_targets:
            matched_targets.append(co_name)
            why_selected.append("company_title_match")

    for topic in intent_tokens["topics"]:
        if topic and topic in title:
            matched_themes.append(topic)
            if "theme_overlap" not in why_selected:
                why_selected.append("theme_overlap")
    for theme in themes:
        if theme in intent_tokens["topics"]:
            if theme not in matched_themes:
                matched_themes.append(theme)
            if "theme_overlap" not in why_selected:
                why_selected.append("theme_overlap")

    if bool(candidate.get("deep_read_complete", True)):
        why_selected.append("deep_read_complete")
    bucket = str(candidate.get("_fresh_g_selection_bucket") or "")
    if bucket and bucket not in why_selected:
        why_selected.append(bucket)

    guidance_brief = _build_guidance_brief(title, matched_targets, matched_themes)

    return {
        "event_id": str(candidate.get("event_id", "")),
        "article_id": str(candidate.get("article_id", "")),
        "title": title,
        "source_level": "g_direct",
        "related_tickers": tickers or [tk for tk in intent_tokens["tickers"] if tk and tk in title],
        "matched_targets": matched_targets,
        "matched_themes": matched_themes,
        "time_sensitivity": temporal.get("content_time_sensitivity", {}),
        "publish_freshness": temporal.get("publish_freshness", ""),
        "why_selected": why_selected or ["g_source_background"],
        "guidance_brief": guidance_brief,
        "forbidden_use": [
            "do_not_treat_as_trade_signal",
            "do_not_boost_confidence",
            "do_not_replace_agent_reasoning",
        ],
    }


def _build_guidance_brief(
    title: str,
    matched_targets: list[str],
    matched_themes: list[str],
) -> str:
    """Build a short guidance brief (≤120 chars)."""
    parts: list[str] = []
    if matched_targets:
        parts.append(f"涉及{'/'.join(matched_targets[:2])}")
    if matched_themes:
        parts.append(f"主题{'/'.join(matched_themes[:2])}")
    base = "；".join(parts) if parts else title[:80]
    brief = f"背景参考：{base}。不作为交易依据。"
    if len(brief) > 120:
        brief = brief[:117] + "..."
    return brief


# ── Pinned source KB resolution ───────────────────────────────────────────────


def _find_pinned_in_kb(
    pinned: PinnedSource,
    kb_docs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find pinned source in knowledge base documents.

    Resolution keys (in order):
    1. Exact topic_id match
    2. Canonical URL match
    3. Document url match
    4. metadata article_url match
    5. metadata source_url match
    6. Linked article URL match
    """
    if not kb_docs:
        return None

    res_urls: set[str] = {pinned.canonical_url}
    res_urls.update(pinned.linked_articles)
    res_urls.discard("")

    res_topic = pinned.topic_id.strip() if pinned.topic_id else ""

    for doc in kb_docs:
        doc_url = str(doc.get("url", ""))
        metadata = doc.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        # 1. Topic ID match
        meta_topic = str(metadata.get("topic_id", ""))
        if res_topic and meta_topic and meta_topic == res_topic:
            return doc

        # 2-3. URL match
        for url in res_urls:
            if url and url == doc_url:
                return doc

        # 4-5. Metadata URL matches
        article_url = str(metadata.get("article_url", ""))
        source_url = str(metadata.get("source_url", ""))
        for url in res_urls:
            if url and url in (article_url, source_url):
                return doc

    return None


def _parse_markdown_knowledge_text(text: str, *, path: Path) -> dict[str, Any] | None:
    """Parse already-bounded markdown bytes without reopening the path."""

    metadata: dict[str, Any] = {"filepath": str(path)}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            front_matter = parts[1]
            body = parts[2]
            parsed_front_matter = _parse_simple_front_matter(front_matter)
            if parsed_front_matter is None:
                return None
            metadata.update(parsed_front_matter)

    title = str(metadata.get("title") or "").strip()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            break
    if not title:
        title = path.stem

    external_id = str(metadata.get("id") or metadata.get("topic_id") or path.stem).strip()
    document_id = f"zsxq:{external_id or path.stem}"
    url = str(
        metadata.get("source_url") or metadata.get("article_url") or metadata.get("url") or ""
    ).strip()

    return {
        "document_id": document_id,
        "title": title,
        "content": body.strip(),
        "url": url,
        "metadata": metadata,
    }


def _parse_simple_front_matter(front_matter: str) -> dict[str, Any] | None:
    """Parse the simple key/value front matter used by knowledge-base articles."""
    metadata: dict[str, Any] = {}
    for raw_line in front_matter.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            continue
        if key in metadata:
            return None
        metadata[key] = _parse_front_matter_scalar(value.strip())
    return metadata


def _parse_front_matter_scalar(value: str) -> Any:
    if value in {"", "None", "null"}:
        return ""
    if value in {"[]", "{}"}:
        return [] if value == "[]" else {}
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _pinned_to_candidate(
    pinned: PinnedSource,
    kb_doc: dict[str, Any],
) -> dict[str, Any]:
    """Convert a matched pinned source + KB doc into a runtime context candidate."""
    return _with_bucket(
        {
            "source_bucket": "pinned_source",
            "pinned_id": pinned.pinned_id,
            "title": str(kb_doc.get("title", "")),
            "guidance_brief": _truncate(
                f"置顶背景参考：{kb_doc.get('title', '')}。不作为交易依据。", 120
            ),
            "why_available": ["pinned_source", "knowledge_base_hit"],
            "usage_boundary": pinned.usage_policy,
            "source_scope": "g_source",
            "source_classification": str(
                (kb_doc.get("metadata") or {}).get("source_classification", "teacher_original")
            ),
            "canonical_url": pinned.canonical_url,
            "published_at": pinned.published_at,
        },
        "pinned_source",
    )


def _pinned_has_processed_artifact(pinned: PinnedSource) -> bool:
    """Return True when a pinned bundle has a ready setting-time artifact.

    A processed artifact is valid when:
    - processing_status is "ready", AND
    - either guidance_brief is hand-written, OR linked_articles provide
      deep_read content at runtime.
    """
    if pinned.processing_status.strip().lower() != "ready":
        return False
    if pinned.guidance_brief.strip():
        return True
    # Allow empty guidance_brief when linked_articles carry deep_read content
    return bool(pinned.linked_articles)


def _is_processed_pinned_relevant(
    kb_root: Path,
    pinned: PinnedSource,
    intent_tokens: dict[str, set[str]],
) -> bool:
    """Gate preprocessed pinned bundles by ticker/company/topic overlap.

    Checks shallow fields first, then falls back to linked article compact
    deep_read information units (core_theses.related_companies,
    core_theses.related_topics, theme_clusters[].name).
    """
    if _overlaps(pinned.tickers, intent_tokens["tickers"]):
        return True
    if _overlaps(pinned.companies, intent_tokens["companies"]):
        return True
    if _overlaps(pinned.theme_clusters, intent_tokens["topics"]):
        return True
    if _overlaps(pinned.keywords, intent_tokens["topics"]):
        return True

    # ── Enrich via linked article compact deep_read info units ──
    for aid in pinned.linked_articles:
        aid_clean = aid.rsplit("/", 1)[-1].replace(".html", "") if "/" in aid else aid
        dr = _load_deep_read_content(kb_root, aid_clean)
        if dr is None and "/" in aid:
            resolved = _resolve_zsxq_url_to_kb_article_id(kb_root, aid)
            if resolved:
                dr = _load_deep_read_content(kb_root, resolved)
        if dr is None:
            continue

        # Extract companies/topics from core_theses
        dr_companies: set[str] = set()
        dr_topics: set[str] = set()
        for t in dr.get("core_theses") or []:
            if not isinstance(t, dict):
                continue
            for co in t.get("related_companies") or []:
                if co and isinstance(co, str):
                    dr_companies.add(co.strip())
            for topic in t.get("related_topics") or []:
                if topic and isinstance(topic, str):
                    dr_topics.add(topic.strip())

        if dr_companies & intent_tokens["companies"]:
            return True

        dr_tickers = set(_map_companies_to_tickers(kb_root, dr_companies))
        if dr_tickers & intent_tokens["tickers"]:
            return True

        dr_themes: set[str] = set()
        for tc in dr.get("theme_clusters") or []:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name", "")
            if name:
                dr_themes.add(name)

        if dr_topics & intent_tokens["topics"]:
            return True
        if dr_themes & intent_tokens["topics"]:
            return True

    # ── Text fallback: search across all processed fields ──
    searchable = " ".join(
        (
            pinned.processed_title,
            pinned.guidance_brief,
            " ".join(pinned.tickers),
            " ".join(pinned.companies),
            " ".join(pinned.theme_clusters),
            " ".join(pinned.keywords),
        )
    )
    return any(
        token and token in searchable for token_set in intent_tokens.values() for token in token_set
    )


def _processed_pinned_to_candidate(
    kb_root: Path,
    pinned: PinnedSource,
) -> dict[str, Any]:
    """Convert a setting-time processed pinned bundle into one candidate.

    Loads deep_read compact artifacts for each linked_article and aggregates
    them into a single rich candidate.  Falls back to hand-written fields
    when deep_read cache is empty.
    """
    linked_articles_deep: list[dict[str, Any]] = []
    all_tickers: list[str] = list(pinned.tickers)
    all_companies: list[str] = list(pinned.companies)
    all_themes: list[str] = list(pinned.theme_clusters)
    all_keywords: list[str] = list(pinned.keywords)
    artifact_available_times: list[str] = []
    linked_material_ids: list[str] = []

    # Try loading deep_read artifacts for linked articles
    has_dr = False
    for aid in pinned.linked_articles:
        # linked_articles may be full URLs; extract the article ID
        aid_clean = aid.rsplit("/", 1)[-1].replace(".html", "") if "/" in aid else aid
        resolved_id = aid_clean
        loaded = _project_fresh_deep_read_snapshot(
            _coerce_fresh_deep_read_snapshot(_load_fresh_deep_read(kb_root, resolved_id)),
            expected_article_id=resolved_id,
        )
        # If direct lookup fails, try resolving via index.json article_url mapping
        if (loaded is None or not loaded.compact) and "/" in aid:
            resolved = _resolve_zsxq_url_to_kb_article_id(kb_root, aid)
            if resolved:
                resolved_id = resolved
                loaded = _project_fresh_deep_read_snapshot(
                    _coerce_fresh_deep_read_snapshot(_load_fresh_deep_read(kb_root, resolved_id)),
                    expected_article_id=resolved_id,
                )
        if (
            loaded is not None
            and loaded.compact
            and _deep_read_has_extractable_units(
                loaded.compact,
                expected_article_id=resolved_id,
            )
        ):
            dr = loaded.compact
            artifact_available_at = loaded.available_at
            has_dr = True
            artifact_available_times.append(artifact_available_at)
            linked_material_ids.append(resolved_id)
            linked_articles_deep.append(
                {
                    "article_id": dr.get("article_id", aid_clean),
                    "title": dr.get("title", ""),
                    "column": dr.get("column", ""),
                    "injectable_summary": dr.get("injectable_summary", ""),
                    "core_theses": dr.get("core_theses", []),
                    "theme_clusters": dr.get("theme_clusters", []),
                    "suggestions": dr.get("suggestions", []),
                }
            )
            dr_tickers = _extract_tickers_from_core_theses(kb_root, dr)
            dr_companies = _extract_companies_from_core_theses(dr)
            all_tickers.extend(dr_tickers)
            all_companies.extend(dr_companies)
            for c in dr.get("theme_clusters", []) or []:
                if not isinstance(c, dict):
                    continue
                name = c.get("name", "")
                if name and name not in all_themes:
                    all_themes.append(name)

    # Guidance: prefer aggregated deep_read summary, fall back to hand-written
    if has_dr:
        guidance_parts: list[str] = []
        for la in linked_articles_deep:
            guidance_parts.append(f"【{la['title']}】\n{la['injectable_summary']}")
        guidance = "\n\n".join(guidance_parts)
        why = ["pinned_source", "deep_read_aggregation", "current_mainline_focus"]
    else:
        guidance = pinned.guidance_brief
        why = ["pinned_source", "preprocessed_artifact", "current_mainline_focus"]

    source_refs = list(linked_material_ids)
    source_refs.extend(
        ref
        for ref in (
            *(pinned.source_refs or ()),
            pinned.canonical_url,
            *pinned.linked_articles,
        )
        if ref
    )
    available_at = _latest_material_time(pinned.processed_at, *artifact_available_times)

    return _with_bucket(
        {
            "source_bucket": "pinned_source",
            "pinned_id": pinned.pinned_id,
            "article_id": pinned.pinned_id,  # for dedup key
            "title": pinned.processed_title or "置顶主线关注",
            "guidance_brief": guidance,
            "why_available": why,
            "usage_boundary": pinned.usage_policy,
            "source_scope": "g_source",
            "source_classification": "teacher_original",
            "canonical_url": pinned.canonical_url,
            "published_at": pinned.published_at,
            "processed_at": pinned.processed_at,
            "available_at": available_at,
            "source_refs": list(dict.fromkeys(source_refs)),
            "tickers": list(dict.fromkeys(all_tickers)),
            "companies": list(dict.fromkeys(all_companies)),
            "theme_clusters": list(dict.fromkeys(all_themes)),
            "keywords": all_keywords,
            "core_theses": [t for la in linked_articles_deep for t in la.get("core_theses", [])],
            "suggestions": [s for la in linked_articles_deep for s in la.get("suggestions", [])],
            "linked_articles": linked_articles_deep,
            "pinned_boost_factor": pinned.pinned_boost_factor,
            "linked_articles_policy": pinned.linked_articles_policy,
            "half_life_class": "long_methodology",
            "attention_policy": {},
            "publish_freshness": "",
        },
        "pinned_source",
    )


def _overlaps(left: tuple[str, ...], right: set[str]) -> bool:
    """Return whether two token collections overlap after stripping blanks."""
    left_tokens = {str(item).strip() for item in left if str(item).strip()}
    right_tokens = {str(item).strip() for item in right if str(item).strip()}
    return bool(left_tokens & right_tokens)


def _theme_name_hits(name: str, topics: set[str]) -> bool:
    """Whether a theme name (often a long phrase) hits any intent keyword.

    Intent topics are short keywords (e.g. "半导体") while real theme names
    are long phrases (e.g. "半导体底层卡口 / AI硬科技材料 / 去日化"); exact
    token equality never matches, so use containment either direction.
    """
    for topic in topics:
        if not topic:
            continue
        if topic in name or name in topic:
            return True
    return False


# ── Recent reference lane (same-day, high-relevance, not-G) ──────────────────


def _is_reference_eligible(candidate: dict[str, Any]) -> bool:
    """Return True for reference-tier items (Q&A / non-teacher classification).

    These are the items excluded from the strict G lane.  They only become
    reference context when also same-day and highly relevant.  BUG-006③:
    tier routing follows the SOURCE CLASSIFICATION, not the column — 普通
    non-QA teacher_original is a G source, while any non-teacher_original
    classification (market_observation / research_reference / ai_assisted…)
    stays reference-tier regardless of column.
    """
    classification = str(candidate.get("source_classification", "")).strip()
    if classification and classification != "teacher_original":
        return True
    if _candidate_column(candidate) == "普通":
        # 普通栏 owner 撤项后回到 reference tier（老师原创内容也不进 G）。
        return True
    if _is_qa_article(candidate):
        return True
    meta = candidate.get("metadata")
    obs_type = ""
    if isinstance(meta, dict):
        obs_type = str(meta.get("type") or meta.get("record_type") or "")
    obs_type = obs_type or str(candidate.get("type") or candidate.get("record_type") or "")
    return obs_type in ("market_observation", "observation")


def _is_same_day(candidate: dict[str, Any], now: str) -> bool:
    """Return True when the candidate was published on the same CST day as now."""
    ts = _candidate_time(candidate)
    if not ts:
        return False
    cand_dt = _coerce_datetime(ts)
    now_dt = _coerce_datetime(now) if now else datetime.now(tz=CST)
    if cand_dt is None or now_dt is None:
        return False
    return cand_dt.astimezone(CST).date() == now_dt.astimezone(CST).date()


def _coerce_datetime(ts: str) -> datetime | None:
    """Parse an ISO-ish timestamp robustly (handles microseconds + tz)."""
    if not ts:
        return None
    cleaned = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        dt = _parse_datetime(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def _reference_is_relevant(
    candidate: dict[str, Any],
    request: AgentRuntimeContextRequest,
    intent_tokens: dict[str, set[str]],
) -> bool:
    """High-relevance gate for reference candidates.

    A reference (not-G) item is admitted only on a *strong* signal, so a bare
    topic keyword in a Q&A title is not enough:
    - direct ticker/company overlap with intent, OR
    - a candidate keyword/theme term (>=2 chars) that appears verbatim in the
      user's question, OR
    - the candidate's own title (a Q&A's source question / headline) shares a
      substantive term (>=4 chars) with the user's question — a keyword-less
      Q&A about the same topic is still relevant.

    BUG-012 残余二：标题子串只认 4 字以上领域词。自由文本标题里的 2 字泛词
    （“主线”“公司”“什么”“信息”等）不足以证明同主题，否则同日无关帖会
    大量挤占 reference lane。
    """
    tickers = {str(t).strip() for t in (candidate.get("tickers", []) or [])}
    companies = {str(co).strip() for co in (candidate.get("companies", []) or [])}
    if tickers & intent_tokens["tickers"]:
        return True
    if companies & intent_tokens["companies"]:
        return True

    question = request.question or ""
    for term in (
        *(candidate.get("keywords", []) or []),
        *(candidate.get("theme_clusters", []) or []),
    ):
        term = str(term).strip()
        if len(term) >= 2 and term in question:
            return True

    return _has_common_substring(str(candidate.get("title", "")), question, min_len=4)


_REF_OVERLAP_STRIP = "，。！？：；、（）()【】《》〈〉…—-·•*#\"'“”‘’ \t\n\r　"


def _has_common_substring(a: str, b: str, *, min_len: int = 2) -> bool:
    """Return True if *a* and *b* share a contiguous substring of >=min_len.

    Whitespace/punctuation is stripped first so overlap reflects content, not
    shared separators.  Used to detect a Q&A whose title is about the same
    topic as the user's question (e.g. both contain "浪潮").
    """
    aa = "".join(ch for ch in a if ch not in _REF_OVERLAP_STRIP)
    bb = "".join(ch for ch in b if ch not in _REF_OVERLAP_STRIP)
    if len(aa) < min_len or len(bb) < min_len:
        return False
    grams_b = {bb[i : i + min_len] for i in range(len(bb) - min_len + 1)}
    return any(aa[i : i + min_len] in grams_b for i in range(len(aa) - min_len + 1))


def _extract_reference_key_points(candidate: dict[str, Any]) -> list[str]:
    """Extract ordered, de-duplicated key points from a reference candidate.

    Prefers an explicit ``key_points`` list; falls back to core_theses thesis
    strings. These are the information units that must reach the runtime prompt.
    """
    points: list[str] = []
    for p in candidate.get("key_points") or []:
        s = str(p).strip()
        if s and s not in points:
            points.append(s)
    if points:
        return points
    for t in candidate.get("core_theses") or []:
        if isinstance(t, dict):
            th = str(t.get("thesis", "")).strip()
            if th and th not in points:
                points.append(th)
    return points


def _resolve_reference_material(
    kb_root: Path,
    candidate: dict[str, Any],
) -> _SelectedReferenceMaterial | None:
    """Resolve exactly one local material, without mixing facts across artifacts.

    Inline cache fields win when present, followed by an exact compact deep-read
    envelope and then an exact markdown article.  Every path is local-only and
    binds the bytes actually used to a SHA-256 digest.
    """
    article_id = str(candidate.get("article_id", "")).strip()
    if not article_id:
        return None

    inline_summary = str(
        candidate.get("summary")
        or candidate.get("injectable_summary")
        or candidate.get("content")
        or candidate.get("excerpt")
        or ""
    ).strip()
    inline_points = _extract_reference_key_points(candidate)
    if inline_summary or inline_points:
        mapping_facts = candidate.get("mapping_facts")
        mapping_facts = mapping_facts if isinstance(mapping_facts, dict) else {}
        try:
            raw = json.dumps(
                {key: value for key, value in candidate.items() if not key.startswith("_")},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None
        if len(raw) > _MAX_INLINE_MATERIAL_BYTES:
            return None
        return _SelectedReferenceMaterial(
            kind="inline_candidate",
            ref=article_id,
            available_at=_explicit_reference_available_at(candidate),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            title=str(candidate.get("title", "")).strip(),
            published_at=_candidate_time(candidate),
            summary=inline_summary,
            key_points=tuple(inline_points),
            tickers=_material_string_list(mapping_facts.get("tickers"), limit=8),
            companies=_material_string_list(mapping_facts.get("companies"), limit=8),
            theme_clusters=_material_string_list(mapping_facts.get("theme_clusters"), limit=8),
            industry_chain_facts=_material_string_list(
                mapping_facts.get("industry_chain_facts"), limit=8
            ),
        )

    article_source = _read_reference_article_source(kb_root, candidate)
    deep_read = _load_deep_read_material(
        kb_root,
        article_id,
        candidate=candidate,
        article_source=article_source,
    )
    if deep_read is not None:
        return deep_read
    if article_source is None:
        return None
    metadata = article_source.metadata
    return _SelectedReferenceMaterial(
        kind="knowledge_markdown",
        ref=article_id,
        available_at=_max_reference_available_at(candidate, article_source.modified_at),
        raw_sha256=article_source.raw_sha256,
        title=article_source.title,
        published_at=str(metadata.get("published_at") or metadata.get("date") or "").strip(),
        summary=_reference_body_summary(article_source.body),
        key_points=tuple(_extract_body_key_points(article_source.body)),
        tickers=_material_string_list(
            metadata.get("mapping_tickers") or metadata.get("tickers"), limit=8
        ),
        companies=_material_string_list(
            metadata.get("mapping_companies") or metadata.get("companies"), limit=8
        ),
        theme_clusters=_material_string_list(
            metadata.get("mapping_theme_clusters") or metadata.get("theme_clusters"), limit=8
        ),
        industry_chain_facts=_material_string_list(metadata.get("industry_chain_facts"), limit=8),
    )


def _load_deep_read_material(
    kb_root: Path,
    article_id: str,
    *,
    candidate: dict[str, Any],
    article_source: _ReferenceArticleSource | None,
) -> _SelectedReferenceMaterial | None:
    """Load one public, source-fresh full/compact artifact generation."""
    if article_source is None:
        return None
    try:
        from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService

        service = DeepReadArtifactService(kb_root=kb_root)
        pair = service.load_fresh_pair(article_id, article_source.path)
    except (OSError, ValueError, TypeError):
        return None
    if pair is None or pair.content_hash != article_source.raw_sha256:
        return None
    payload = pair.compact
    if payload.get("article_id") != article_id:
        return None
    summary = str(payload.get("injectable_summary", "")).strip()
    points: list[str] = []
    for thesis in payload.get("core_theses") or []:
        if isinstance(thesis, dict):
            text = str(thesis.get("thesis", "")).strip()
            if text and text not in points:
                points.append(text)
    mapping_facts = payload.get("mapping_facts")
    mapping_facts = mapping_facts if isinstance(mapping_facts, dict) else {}
    material_available_at = _latest_material_time(
        pair.generated_at,
        pair.article_modified_at,
    )
    if not material_available_at:
        return None
    return _SelectedReferenceMaterial(
        kind="deep_read_compact",
        ref=article_id,
        available_at=_max_reference_available_at(candidate, material_available_at),
        raw_sha256=pair.compact_raw_sha256,
        title=str(payload.get("title", "")).strip(),
        published_at=str(payload.get("published_at", "")).strip(),
        summary=summary,
        key_points=tuple(points),
        tickers=_material_string_list(mapping_facts.get("tickers"), limit=8),
        companies=_material_string_list(mapping_facts.get("companies"), limit=8),
        theme_clusters=_material_string_list(mapping_facts.get("theme_clusters"), limit=8),
        industry_chain_facts=_material_string_list(
            mapping_facts.get("industry_chain_facts"), limit=8
        ),
    )


def _reference_article_path(
    kb_root: Path,
    candidate: dict[str, Any],
) -> Path | None:
    """Compatibility helper returning an exact, safely-read markdown path."""
    source = _read_reference_article_source(kb_root, candidate)
    return source.path if source is not None else None


def _read_reference_article_source(
    kb_root: Path,
    candidate: dict[str, Any],
) -> _ReferenceArticleSource | None:
    """Read and validate markdown bytes through one stable no-follow descriptor.

    Hashing, parsing, identity, and mtime all derive from the same descriptor
    snapshot. Fuzzy filename lookup and conflicting material identities fail
    closed.
    """
    article_id = str(candidate.get("article_id", "")).strip()
    if not article_id:
        return None
    path = _resolve_reference_article_path(kb_root, candidate, article_id=article_id)
    if path is None:
        return None
    try:
        relative_path = path.relative_to(kb_root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    snapshot = _read_bounded_owned_regular_file_at(
        kb_root,
        relative_path,
        max_bytes=_MAX_REFERENCE_MARKDOWN_BYTES,
    )
    if snapshot is None:
        return None
    raw, raw_sha256, modified_at = snapshot
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    parsed = _parse_markdown_knowledge_text(text, path=path)
    if parsed is None:
        return None
    parsed_metadata = parsed.get("metadata")
    if not isinstance(parsed_metadata, dict):
        return None
    canonical_identity_values = [
        str(parsed_metadata.get(key, "")).strip()
        for key in ("id", "article_id", "material_id")
        if str(parsed_metadata.get(key, "")).strip()
    ]
    topic_id = str(parsed_metadata.get("topic_id", "")).strip()
    if canonical_identity_values and set(canonical_identity_values) != {article_id}:
        return None
    if article_id.startswith("zsxq-"):
        raw_topic_id = article_id.removeprefix("zsxq-")
        if (
            not canonical_identity_values
            or not raw_topic_id.isascii()
            or not raw_topic_id.isdecimal()
            or (topic_id and topic_id != raw_topic_id)
        ):
            return None
    elif canonical_identity_values:
        if topic_id and topic_id != article_id:
            return None
    elif topic_id != article_id:
        return None
    return _ReferenceArticleSource(
        path=path,
        raw=raw,
        raw_sha256=raw_sha256,
        modified_at=modified_at,
        metadata=parsed_metadata,
        body=str(parsed.get("content", "")),
        title=str(parsed.get("title", "")).strip(),
    )


def _resolve_reference_article_path(
    kb_root: Path,
    candidate: dict[str, Any],
    *,
    article_id: str,
) -> Path | None:
    metadata = candidate.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    filepath = str(metadata.get("filepath") or candidate.get("filepath") or "").strip()
    if filepath:
        raw_path = Path(filepath)
        if raw_path.is_absolute():
            path = raw_path
        elif raw_path.parts and raw_path.parts[0] == "knowledge-base":
            path = kb_root.joinpath(*raw_path.parts[1:])
        else:
            path = kb_root / raw_path
    else:
        path = kb_root / "articles" / f"{article_id}.md"
        if not path.exists():
            indexed = _indexed_article_path(kb_root, article_id)
            if indexed is not None:
                path = indexed
    return _secure_path_within_knowledge_base(kb_root, path, suffix=".md")


def _indexed_article_path(kb_root: Path, article_id: str) -> Path | None:
    snapshot = _read_bounded_owned_regular_file_at(
        kb_root,
        Path("index.json"),
        max_bytes=_MAX_PRIORITY_CACHE_BYTES,
    )
    if snapshot is None:
        return None
    try:
        index = json.loads(snapshot[0])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(index, dict) or not isinstance(index.get("articles"), list):
        return None
    matches = [
        entry
        for entry in index["articles"][:20_000]
        if isinstance(entry, dict) and str(entry.get("id", "")).strip() == article_id
    ]
    if len(matches) != 1:
        return None
    basename = Path(str(matches[0].get("path", ""))).name
    if not basename.endswith(".md"):
        return None
    return kb_root / "articles" / basename


def _secure_path_within_knowledge_base(
    kb_root: Path,
    path: Path,
    *,
    suffix: str,
) -> Path | None:
    """Return one canonical non-symlink file within the approved KB root."""
    root = kb_root.absolute()
    candidate = path.absolute()
    if ".." in candidate.parts:
        return None
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.suffix != suffix or root.is_symlink():
        return None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        return None
    return resolved


def _read_bounded_owned_regular_file_resolved(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[bytes, str, str] | None:
    """Follow-resolve one pin-file path and validate the resolved target.

    Only for the pinned-sources file: the release ``knowledge-base/runtime``
    component is a controlled owner-built symlink into the shared KB.  The
    resolved target must be a regular file owned by the current user with
    owner-only mode and bounded size; anything else fails closed.
    """
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    try:
        metadata = resolved.stat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_size > max_bytes
        or metadata.st_size < 0
    ):
        return None
    try:
        payload = resolved.read_bytes()
    except OSError:
        return None
    if len(payload) > max_bytes:
        return None
    return payload, str(resolved), hashlib.sha256(payload).hexdigest()


def _read_bounded_owned_regular_file_at(
    kb_root: Path,
    relative_path: Path,
    *,
    max_bytes: int,
) -> tuple[bytes, str, str] | None:
    """Read a KB-relative file without following any root or parent symlink."""
    root = kb_root.absolute()
    if (
        not root.is_absolute()
        or ".." in root.parts
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or not relative_path.name
    ):
        return None
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    opened_directories: list[int] = []
    try:
        current_fd = os.open("/", directory_flags)
        opened_directories.append(current_fd)
        for part in root.parts[1:]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            current = os.fstat(next_fd)
            if not stat.S_ISDIR(current.st_mode):
                os.close(next_fd)
                return None
            opened_directories.append(next_fd)
            current_fd = next_fd
        root_stat = os.fstat(current_fd)
        if root_stat.st_uid != os.getuid():
            return None
        for part in relative_path.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            current = os.fstat(next_fd)
            if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.getuid():
                os.close(next_fd)
                return None
            opened_directories.append(next_fd)
            current_fd = next_fd
        fd = os.open(relative_path.name, file_flags, dir_fd=current_fd)
        try:
            opened = os.fstat(fd)
            snapshot = _read_bounded_owned_regular_fd(fd, max_bytes=max_bytes)
            if snapshot is None:
                return None
            linked = os.stat(relative_path.name, dir_fd=current_fd, follow_symlinks=False)
            if not stat.S_ISREG(linked.st_mode) or (opened.st_dev, opened.st_ino) != (
                linked.st_dev,
                linked.st_ino,
            ):
                return None
            return snapshot
        finally:
            os.close(fd)
    except OSError:
        return None
    finally:
        for directory_fd in reversed(opened_directories):
            with suppress(OSError):
                os.close(directory_fd)


def _read_bounded_owned_regular_fd(
    fd: int,
    *,
    max_bytes: int,
) -> tuple[bytes, str, str] | None:
    before = os.fstat(fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or before.st_size < 0
        or before.st_size > max_bytes
    ):
        return None
    remaining = before.st_size
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(fd, min(remaining, 64 * 1024))
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        return None
    after = os.fstat(fd)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_uid,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
    )
    if identity != (
        after.st_dev,
        after.st_ino,
        after.st_uid,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
    ):
        return None
    raw = b"".join(chunks)
    return (
        raw,
        hashlib.sha256(raw).hexdigest(),
        datetime.fromtimestamp(before.st_mtime, tz=UTC).isoformat(),
    )


def _material_string_list(value: object, *, limit: int) -> tuple[str, ...]:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            value = [part.strip(" \t\"'") for part in text[1:-1].split(",")]
        elif text:
            value = [text]
        else:
            value = []
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value[:limit]:
        text = str(item).strip()
        if text and len(text) <= 400 and text not in result:
            result.append(text)
    return tuple(result)


def _latest_material_time(*values: str) -> str:
    parsed = [item for value in values if (item := _strict_material_datetime(value)) is not None]
    if len(parsed) != len([value for value in values if value]):
        return ""
    return max(parsed, key=lambda item: item.astimezone(UTC)).isoformat() if parsed else ""


def _explicit_reference_available_at(candidate: dict[str, Any]) -> str:
    return _max_reference_available_at(candidate, "")


def _max_reference_available_at(candidate: dict[str, Any], material_time: str) -> str:
    """Return the conservative latest availability timestamp for one material."""
    metadata = candidate.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    available_times: list[datetime] = []
    if material_time:
        parsed_material = _strict_material_datetime(material_time)
        if parsed_material is None:
            return ""
        available_times.append(parsed_material)
    for source in (candidate, metadata):
        for key in ("available_at", "processed_at", "ingested_at", "saved_at"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                parsed = _strict_material_datetime(value.strip())
                if parsed is None:
                    return ""
                available_times.append(parsed)
    if not available_times:
        return ""
    return max(available_times, key=lambda item: item.astimezone(UTC)).isoformat()


def _strict_material_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _reference_body_summary(body: str, *, max_len: int = 320) -> str:
    """Return the first substantive (non-heading) paragraph of an article body."""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line[:max_len]
    return ""


_REF_BODY_BULLET_PREFIXES = ("•", "·", "・")


def _extract_body_key_points(
    body: str,
    *,
    max_points: int = 6,
    max_len: int = 400,
) -> list[str]:
    """Extract bullet-style key points from an article body.

    Targets the per-item bullet lines (e.g. the per-wave 画像 bullets) — the
    information units that carry the reference's substance.
    """
    points: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line[0] not in _REF_BODY_BULLET_PREFIXES:
            continue
        text = line.lstrip("•·・ ").strip()
        if len(text) >= 6 and text not in points:
            points.append(text[:max_len])
        if len(points) >= max_points:
            break
    return points


def _normalize_reference_time(value: str) -> str:
    """Normalize one reference timestamp to tz-aware ISO (naive assumed CST).

    知识库前言 date 是朴素 CST（如 "2026-08-30 09:30"）；read_ready_evidence
    的 strict 校验只认带时区时间戳（BUG-012② 供料换源后普通栏走 markdown
    材料分支，必须在投影边界归一，不改校验器）。不可解析则原样返回，交由
    下游诚实拒绝。
    """
    text = str(value or "").strip()
    if not text:
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.isoformat()


def _recent_reference_to_candidate(
    kb_root: Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Build a reference (not-G) runtime candidate entry.

    The guidance brief is information-dense: it carries the reference's own
    summary and key points so the downstream runtime prompt/output can surface the
    same-day detail, while the entry stays clearly tagged as reference (not-G).
    """
    material = _resolve_reference_material(kb_root, candidate)
    title = material.title if material is not None else str(candidate.get("title", ""))
    summary = material.summary[:1_000] if material is not None else ""
    key_points = [point[:400] for point in material.key_points[:8]] if material is not None else []
    detail_bits: list[str] = []
    if summary:
        detail_bits.append(summary)
    if key_points:
        detail_bits.append("同日参考要点：" + "；".join(key_points))
    detail = " ".join(detail_bits)
    guidance = (
        f"同日高相关背景参考（reference，非 G 源结论）：{title}。"
        + (f"{detail}。" if detail else "")
        + "仅作背景，不作为交易依据，不提高置信度。"
    )
    tickers = list(material.tickers) if material is not None else []
    companies = list(material.companies) if material is not None else []
    themes = list(material.theme_clusters) if material is not None else []
    keywords = [str(k).strip() for k in (candidate.get("keywords", []) or []) if k]
    industry_chain_facts = list(material.industry_chain_facts) if material is not None else []
    published_at = material.published_at if material is not None else _candidate_time(candidate)
    selected_material = material.provenance() if material is not None else {}
    available_at = material.available_at if material is not None else ""
    published_at = _normalize_reference_time(published_at)
    available_at = _normalize_reference_time(available_at)
    return {
        "source_bucket": "recent_reference",
        "article_id": str(candidate.get("article_id", "")),
        "title": title,
        "guidance_brief": guidance,
        "reference_key_points": key_points,
        "reference_summary": summary,
        "why_available": [
            "same_day_reference",
            "high_relevance_question_match",
            "reference_not_g_source",
        ],
        "usage_boundary": "reference_not_g_source_advisory_only",
        "source_scope": "reference",
        "source_classification": str(candidate.get("source_classification", "")),
        "column": _candidate_column(candidate),
        "published_at": published_at,
        "available_at": available_at,
        "tickers": tickers,
        "companies": companies,
        "theme_clusters": themes,
        "keywords": keywords,
        "industry_chain_facts": industry_chain_facts,
        "selected_material": selected_material,
        "local_ready_evidence": {
            "ready": bool(summary or key_points),
            "summary_available": bool(summary),
            "key_points_available": bool(key_points),
            "deep_read_complete": candidate.get("deep_read_complete") is True,
            "local_only": True,
        },
        "core_theses": list(candidate.get("core_theses") or []),
        "suggestions": list(candidate.get("suggestions") or []),
        "half_life_class": _candidate_half_life(candidate),
        "attention_policy": {},
        "publish_freshness": "",
    }


# ── Relevance ─────────────────────────────────────────────────────────────────


def _is_relevant(
    doc: dict[str, Any],
    intent_tokens: dict[str, set[str]],
    pinned: PinnedSource,
) -> bool:
    """Check if a KB document is relevant to the request intent."""
    title = str(doc.get("title", ""))
    content = str(doc.get("content", ""))

    for tk in intent_tokens["tickers"]:
        if tk and (tk in title or tk in content):
            return True

    for co in intent_tokens["companies"]:
        if co and (co in title or co in content):
            return True

    return any(topic and (topic in title or topic in content) for topic in intent_tokens["topics"])


# ── Source boundary ───────────────────────────────────────────────────────────


def _apply_source_boundary(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Filter candidates to G-only; track what was excluded."""
    eligible: list[dict[str, Any]] = []
    dropped: list[str] = []
    for c in candidates:
        bucket = str(c.get("source_bucket") or c.get("_bucket", ""))
        if bucket in ("pinned_source", "recent_reference") or _is_g_source(c):
            eligible.append(c)
        else:
            dropped.append(
                f"non_g_source_excluded:{c.get('article_id', c.get('title', 'unknown'))}"
            )
    return eligible, dropped


# ── Budget ────────────────────────────────────────────────────────────────────


def _apply_budget(
    candidates: list[dict[str, Any]],
    max_events: int,
    intent_tokens: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply semantic budget: keep top N by priority and relevance.

    Priority order: pinned_source > latest_commentary > recent_reference > fresh_g.
    A same-day recent_reference is question-matched, so it ranks above generic
    fresh_g background but never displaces the single latest commentary.
    """
    if max_events <= 0:
        return [], []

    pinned = [c for c in candidates if c.get("source_bucket") == "pinned_source"]
    commentary = [c for c in candidates if c.get("source_bucket") == "latest_commentary"]
    reference = [c for c in candidates if c.get("source_bucket") == "recent_reference"]
    fresh = [
        c
        for c in candidates
        if c.get("source_bucket") not in ("pinned_source", "latest_commentary", "recent_reference")
    ]

    selected: list[dict[str, Any]] = []
    dropped: list[str] = []

    # Pinned first (up to all)
    for c in pinned:
        if len(selected) < max_events:
            selected.append(c)
        else:
            dropped.append(f"budget_exceeded:{c.get('pinned_id', c.get('title', ''))}")

    # Commentary next
    for c in commentary:
        if len(selected) < max_events:
            selected.append(c)
        else:
            dropped.append(f"budget_exceeded:{c.get('article_id', c.get('title', ''))}")

    # Same-day question-matched reference next (advisory, not-G)
    for c in reference:
        if len(selected) < max_events:
            selected.append(c)
        else:
            dropped.append(f"budget_exceeded:{c.get('article_id', c.get('title', ''))}")

    # Fresh G by relevance score
    remaining = max_events - len(selected)
    if remaining > 0:
        scored_fresh = sorted(
            fresh,
            key=lambda c: (
                _candidate_relevance_score(c, intent_tokens),
                _candidate_time(c),
            ),
            reverse=True,
        )
        # B 第二刀（用户拍板 2026-08-21）：泛问题（无绑定目标）下 fresh 特刊
        # 最多保留 score+time 前 2 条，主次分明；有绑定目标的问题不受此上限
        # 影响（相关性过滤已在选择层完成）。
        if not any(intent_tokens.get(key) for key in ("tickers", "companies", "topics")):
            scored_fresh = scored_fresh[:_BROAD_OVERVIEW_SPECIAL_CAP]
        for c in scored_fresh[:remaining]:
            selected.append(c)
        for c in scored_fresh[remaining:]:
            dropped.append(f"budget_exceeded:{c.get('article_id', c.get('title', 'unknown'))}")

    return selected, dropped


# ── Output builders ───────────────────────────────────────────────────────────


def _build_llm_context(
    *,
    request: AgentRuntimeContextRequest,
    selected: list[dict[str, Any]],
    data_gaps: list[str],
    latest_focus_context: dict[str, Any] | None = None,
    fresh_g_bound_article_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Build compact llm_context for LLM consumption.

    ``fresh_g_bound_article_ids`` is the READY working-set manifest's bound
    article-id set.  When present, every Agent-visible source_ref of fresh-G
    material (fresh_g / latest_commentary buckets) must be manifest-bound:
    unbound refs are dropped and surface as a stable typed gap.  Non-READY
    paths and pinned/reference lanes are unaffected.
    """
    g_context: list[dict[str, Any]] = []
    for c in selected:
        guidance_brief = _candidate_guidance_brief(c)
        entry: dict[str, Any] = {
            "source_bucket": c.get("source_bucket", "fresh_g"),
            "title": c.get("title", ""),
            "guidance_brief": guidance_brief,
            "why_available": c.get("why_available", ["g_source_background"]),
            "usage_boundary": c.get(
                "usage_boundary",
                "background_guidance_only_no_confidence_boost",
            ),
        }
        source_ref = c.get("article_id") or c.get("pinned_id") or ""
        if source_ref:
            entry["source_ref"] = source_ref
        source_family = c.get("source_family")
        if source_family:
            entry.update(
                {
                    "source_classification": c.get("source_classification", ""),
                    "column": _candidate_column(c),
                    "source_family": source_family,
                    "content_type": c.get("content_type", ""),
                    "source_usage": c.get("source_usage", ""),
                    "priority_label": c.get("priority_label"),
                }
            )
        raw_source_refs = c.get("source_refs")
        raw_source_refs = raw_source_refs if isinstance(raw_source_refs, (list, tuple)) else []
        source_refs = [
            ref.strip() for ref in raw_source_refs if isinstance(ref, str) and ref.strip()
        ]
        source_refs_valid = (
            len(source_refs) == len(raw_source_refs)
            and len(source_refs) <= _MAX_G_MATERIAL_SOURCE_REFS
            and len(source_refs) == len(set(source_refs))
            and all(len(ref) <= 300 for ref in source_refs)
        )
        if not source_refs_valid:
            if raw_source_refs:
                data_gaps.append(f"g_source_material_refs_invalid:{source_ref or 'unknown'}")
        else:
            if fresh_g_bound_article_ids is not None and c.get("source_bucket") in (
                "fresh_g",
                "latest_commentary",
            ):
                # READY manifest-backed fresh-G material: every Agent-visible
                # source_ref must be manifest-bound (the article_id itself is
                # bound); unbound refs never enter llm_context and surface as
                # a stable typed gap.
                unbound_refs = [ref for ref in source_refs if ref not in fresh_g_bound_article_ids]
                source_refs = [ref for ref in source_refs if ref in fresh_g_bound_article_ids]
                if unbound_refs:
                    data_gaps.append(f"g_source_material_refs_unbound:{source_ref or 'unknown'}")
            if source_refs:
                entry["source_refs"] = source_refs
        # Rich metadata
        tickers = [str(t).strip() for t in (c.get("tickers") or []) if t]
        companies = [str(co).strip() for co in (c.get("companies") or []) if co]
        themes = [str(t).strip() for t in (c.get("theme_clusters") or []) if t]
        keywords = [str(k).strip() for k in (c.get("keywords") or []) if k]
        if tickers:
            entry["tickers"] = tickers[:20]
        if companies:
            entry["companies"] = companies[:20]
        if themes:
            entry["theme_clusters"] = themes[:12]
        if keywords:
            entry["keywords"] = keywords[:20]
        # Timeliness
        hl = c.get("half_life_class", "")
        if hl:
            entry["half_life_class"] = hl
        ap = c.get("attention_policy", {})
        if ap:
            entry["attention_policy"] = ap
        pf = c.get("publish_freshness", "")
        if pf:
            entry["publish_freshness"] = pf
        published_at = _candidate_time(c)
        available_at = c.get("available_at", "")
        if published_at:
            entry["published_at"] = published_at
        if available_at:
            entry["available_at"] = available_at
        # Deep read content
        ct = c.get("core_theses", [])
        if ct:
            entry["core_theses"] = ct
        sug = c.get("suggestions", [])
        if sug:
            entry["suggestions"] = sug
        # Reference (not-G) key points, if this is a recent_reference item
        rkp = c.get("reference_key_points", [])
        if rkp:
            entry["reference_key_points"] = rkp
        if c.get("source_bucket") == "recent_reference":
            entry["source_scope"] = c.get("source_scope", "")
            entry["source_classification"] = c.get("source_classification", "")
            entry["column"] = _candidate_column(c)
            entry["tickers"] = tickers[:20]
            entry["companies"] = companies[:20]
            entry["theme_clusters"] = themes[:12]
            entry["reference_key_points"] = list(c.get("reference_key_points", []))
            entry["reference_summary"] = c.get("reference_summary", "")
            entry["industry_chain_facts"] = c.get("industry_chain_facts", [])
            entry["selected_material"] = c.get("selected_material", {})
            entry["local_ready_evidence"] = c.get("local_ready_evidence", {})
            entry["instruction_authority"] = "none"
        g_context.append(entry)

    return {
        "runtime_context_version": _RUNTIME_CONTEXT_VERSION,
        "agent_id": request.agent_id,
        "g_context": g_context,
        "latest_focus_context": latest_focus_context or {"active": False},
        "data_gaps": list(dict.fromkeys(data_gaps)),
    }


def _canonical_llm_context_json(
    request: AgentRuntimeContextRequest,
    selected: list[dict[str, Any]],
    latest_focus_context: dict[str, Any],
    *,
    data_gaps: list[str],
    fresh_g_bound_article_ids: set[str] | None = None,
    projections: dict[str, Any] | None = None,
) -> bytes:
    """Canonical UTF-8 JSON of the Agent-visible llm_context (D3 bound basis).

    ``data_gaps`` is measured with the exact content the final llm_context
    will carry (a copy is used; ``_build_llm_context`` only appends the same
    stable codes the caller's build will append).  ``fresh_g_bound_article_ids``
    is the same READY bound set the final build applies, so the measured JSON
    is byte-identical to the returned artifact.  ``projections`` carries the
    exact mainline/methodology attachment dicts the final llm_context will
    receive, so projection bytes are inside the bound.
    """
    ctx = _build_llm_context(
        request=request,
        selected=selected,
        data_gaps=data_gaps,
        latest_focus_context=latest_focus_context,
        fresh_g_bound_article_ids=fresh_g_bound_article_ids,
    )
    if projections:
        ctx.update(projections)
    return json.dumps(
        ctx,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _enforce_llm_context_size_bound(
    *,
    request: AgentRuntimeContextRequest,
    selected: list[dict[str, Any]],
    latest_focus_context: dict[str, Any],
    data_gaps: list[str],
    fresh_g_bound_article_ids: set[str] | None = None,
    projections: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deterministically keep the Agent-visible canonical llm_context ≤ 256 KiB.

    Per-article deep-read projections are individually bounded, but several
    selected entries can still push the whole-context canonical JSON above the
    D3 ``_MAX_INLINE_MATERIAL_BYTES`` bound.  Entries are evicted in reverse
    semantic-budget priority (fresh_g lowest relevance first — the selected
    list arrives relevance-descending — then recent_reference, latest_commentary,
    pinned); every eviction is an auditable stable gap.  The measurement uses
    the real gap codes, the eviction gaps already produced, and the READY
    bound set — exactly what the final build applies — so the final build
    (which carries precisely ``data_gaps + evicted`` and the same bound set)
    matches the last measured size.  If even an empty g_context cannot fit,
    the context fails closed with a stable gap and no G material is exposed.
    """
    if not selected:
        return selected, []
    encoded = _canonical_llm_context_json(
        request,
        selected,
        latest_focus_context,
        data_gaps=list(data_gaps),
        fresh_g_bound_article_ids=fresh_g_bound_article_ids,
        projections=projections,
    )
    if len(encoded) <= _MAX_INLINE_MATERIAL_BYTES:
        return selected, []
    remaining = list(selected)
    # Mirrors _apply_budget selection order exactly, inverted for eviction:
    # fresh_g is the lowest priority (evicted first), then recent_reference,
    # latest_commentary, and pinned_source is the highest priority (kept
    # last).  Any unrecognized bucket is treated as the lowest priority, like
    # the budget's catch-all fresh lane.
    priority = {
        "pinned_source": 3,
        "latest_commentary": 2,
        "recent_reference": 1,
        "fresh_g": 0,
    }
    evicted: list[str] = []
    while remaining:
        # Evict the rightmost entry of the lowest-priority bucket still present.
        lowest = min(priority.get(c.get("source_bucket", "fresh_g"), 0) for c in remaining)
        victim_index = max(
            index
            for index, c in enumerate(remaining)
            if priority.get(c.get("source_bucket", "fresh_g"), 0) == lowest
        )
        victim = remaining.pop(victim_index)
        label = str(
            victim.get("article_id") or victim.get("pinned_id") or victim.get("title") or "unknown"
        )
        evicted.append(f"g_context_canonical_evicted:{label}")
        encoded = _canonical_llm_context_json(
            request,
            remaining,
            latest_focus_context,
            data_gaps=list(data_gaps) + list(evicted),
            fresh_g_bound_article_ids=fresh_g_bound_article_ids,
            projections=projections,
        )
        if len(encoded) <= _MAX_INLINE_MATERIAL_BYTES:
            return remaining, evicted
    evicted.append("g_context_canonical_budget_exceeded")
    return [], evicted


def _build_audit_context(
    *,
    request: AgentRuntimeContextRequest,
    pinned: dict[str, Any],
    fresh_g: dict[str, Any],
    budget: int,
    selected: list[dict[str, Any]],
    dropped: list[str],
    data_gaps: list[str],
    normalized_positions: list[dict[str, Any]],
    latest_focus_context: dict[str, Any] | None = None,
    mainline_projection: dict[str, Any] | None = None,
    methodology_projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build full audit_context for work_trace, debug, and Hermes display."""
    dropped_entries: list[dict[str, Any]] = []
    for d in dropped:
        if ":" in d:
            reason, source_id = d.split(":", 1)
        else:
            reason, source_id = d, ""
        dropped_entries.append({"source_id": source_id, "reason": reason})

    selected_audit: list[dict[str, Any]] = []
    for c in selected:
        selected_audit.append(
            {
                "source_bucket": c.get("source_bucket", "fresh_g"),
                "selection_bucket": c.get("selection_bucket", ""),
                "selection_rank": c.get("selection_rank", 0),
                "title": c.get("title", ""),
                "article_id": c.get("article_id", ""),
                "pinned_id": c.get("pinned_id", ""),
                "source_classification": c.get("source_classification", ""),
                "column": _candidate_column(c),
                "published_at": _candidate_time(c),
                "available_at": c.get("available_at", ""),
                "guidance_brief": _candidate_guidance_brief(c),
                "why_available": c.get("why_available", []),
                "usage_boundary": c.get("usage_boundary", ""),
                "source_refs": c.get("source_refs", []),
                "tickers": c.get("tickers", []),
                "companies": c.get("companies", []),
                "theme_clusters": c.get("theme_clusters", []),
                "reference_key_points": c.get("reference_key_points", []),
                "reference_summary": c.get("reference_summary", ""),
                "industry_chain_facts": c.get("industry_chain_facts", []),
                "selected_material": c.get("selected_material", {}),
                "deep_read_material": c.get("deep_read_material", {}),
                "local_ready_evidence": c.get("local_ready_evidence", {}),
                "source_scope": c.get("source_scope", ""),
                "instruction_authority": "none",
            }
        )

    return {
        "selection_policy": fresh_g.get(
            "selection_policy", "time_window_column_score_relevance_v2"
        ),
        "selection_policy_flags": {
            "require_relevant_special_reports": request.require_relevant_special_reports,
        },
        "request": {
            "agent_id": request.agent_id,
            "question": request.question,
            "ticker": request.ticker,
            "company": request.company,
            "topic": request.topic,
            "max_g_events": request.max_g_events,
        },
        "pinned": {
            "candidate_seen": pinned.get("candidate_seen", False),
            "knowledge_base_status": pinned.get("knowledge_base_status", "none"),
            "processing_status": pinned.get("processing_status", ""),
            "injected": pinned.get("injected", False),
            "relevance_gate": pinned.get("relevance_gate", "not_applicable"),
            "linked_articles_policy": pinned.get(
                "linked_articles_policy", "part_of_pinned_source_bundle"
            ),
            # M5（用户拍板 2026-08-19）：逐条 pin 决定（gate/injected/skip），
            # 与顶层聚合值并存；审计/展示/回放均以 per-pin 为唯一事实源。
            "per_pin_decisions": pinned.get("per_pin_decisions", []),
        },
        "latest_commentary": {
            "window_days": load_g_window_config().commentary_trading_days,
            "injected": fresh_g.get("commentary_injected", False),
            "reason": fresh_g.get("commentary_reason", "no_commentary_within_window"),
        },
        "fresh_g": {
            "candidates_available": fresh_g.get("count", 0) > 0,
            "candidates_count": fresh_g.get("count", 0),
            "selection_counts": fresh_g.get("selection_counts", {}),
            "excluded_sources": fresh_g.get("excluded_sources", []),
            "working_set_freshness": fresh_g.get(
                "working_set_freshness",
                {"status": "NOT_CHECKED", "bound_article_ids": [], "data_gaps": []},
            ),
        },
        "budget": {
            "policy": "semantic_contract_budget",
            "max_events": budget,
            "selected_count": len(selected),
            "token_estimate": _estimate_tokens(selected),
        },
        "selected": selected_audit,
        "dropped": dropped_entries,
        "source_boundary": {
            "g_only": True,
            "external_excluded": True,
            "advisory_only": True,
        },
        "normalized_positions": normalized_positions,
        "latest_focus_context": latest_focus_context or {"active": False},
        "mainline_projection": mainline_projection
        or {
            "themes": [],
            "generation": "",
            "manifest_sha256": "",
            "data_gaps": [],
        },
        "methodology_projection": methodology_projection
        or {
            "groups": [],
            "generation": "",
            "manifest_sha256": "",
            "data_gaps": [],
        },
        "data_gaps": list(dict.fromkeys(data_gaps)),
    }


def _estimate_tokens(selected: list[dict[str, Any]]) -> int:
    """Rough token estimate for selected candidates."""
    total = 0
    for c in selected:
        text = str(c.get("title", "")) + str(c.get("guidance_brief", ""))
        total += len(text) // 2
    return max(1, total)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _candidate_column(candidate: dict[str, Any]) -> str:
    metadata = candidate.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return str(candidate.get("column") or metadata.get("column") or "")


def _candidate_priority_label(candidate: Mapping[str, object]) -> str | None:
    metadata = candidate.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    raw = candidate.get("priority_label", metadata.get("priority_label"))
    return raw if raw == "重中之重" else None


def _candidate_time(candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("published_at")
        or candidate.get("created_at")
        or candidate.get("observed_at")
        or ""
    )


def _candidate_guidance_brief(candidate: dict[str, Any]) -> str:
    """Return guidance_brief. Selection is bounded by the semantic budget."""
    brief = str(candidate.get("guidance_brief") or "").strip()
    if brief:
        return brief
    title = str(candidate.get("title") or "").strip()
    if title:
        return f"背景参考：{title}。不作为交易依据。"
    return "G 源背景参考；不作为交易依据。"


def _with_bucket(candidate: dict[str, Any], bucket: str) -> dict[str, Any]:
    """Tag a candidate with its source bucket."""
    c = dict(candidate)
    c["source_bucket"] = bucket
    return c


def _parse_datetime(ts: str) -> datetime:
    """Parse a timestamp string, falling back to epoch."""
    if not ts:
        return datetime(1970, 1, 1, tzinfo=CST)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            return dt
        except ValueError:
            continue
    return datetime(1970, 1, 1, tzinfo=CST)


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len chars."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _candidate_methodology_rules(dr: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """归一化 compact 的 methodology_rules(验收 1 合同);缺失/畸形 → 空。"""
    if dr is None or not isinstance(dr, Mapping):
        return []
    raw = dr.get("methodology_rules")
    if not isinstance(raw, (list, tuple)):
        return []
    rules: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        if not isinstance(entry.get("related_topics"), (list, tuple)):
            continue
        title = str(entry.get("title") or "").strip()
        rule = str(entry.get("rule") or "").strip()
        if not title or not rule:
            continue
        rules.append(
            {
                "title": title[:50],
                "rule": rule,
                "teacher_quote": str(entry.get("teacher_quote") or ""),
                "apprentice_interpretation": str(entry.get("apprentice_interpretation") or ""),
                "related_topics": [
                    str(t) for t in entry["related_topics"] if isinstance(t, str) and t
                ],
                "confidence": entry.get("confidence"),
                "source_id": str(entry.get("source_id") or ""),
                "article_id": str(entry.get("article_id") or ""),
                "published_at": str(entry.get("published_at") or ""),
                "generation_id": str(entry.get("generation_id") or ""),
            }
        )
    return rules


def _build_mainline_projection(
    *,
    selected: Sequence[Mapping[str, Any]],
    intent_tokens: Mapping[str, set[str]],
    generation: str,
    manifest_sha256: str,
    bound_article_ids: set[str] | None = None,
    manifest_ready: bool = False,
) -> dict[str, Any]:
    """Project the selected G candidates by request-intent themes (P1).

    Pure read-only adapter over :func:`project_mainline`: articles are the
    already-selected candidates (the only read output of the G working set),
    and only themes overlapping the request's intent topics are kept.  A
    READY manifest with a real generation and bound-source membership is
    required (B1); anything else returns only a typed gap.  Nothing is
    written or cached.
    """
    from fin_analyse.guo_teacher_research.g_mainline_projection import project_mainline

    if not manifest_ready or not generation:
        return {
            "themes": [],
            "generation": generation,
            "manifest_sha256": manifest_sha256,
            "data_gaps": ["g_mainline_manifest_not_ready"],
        }
    topics = intent_tokens.get("topics") or set()
    gaps: list[str] = []
    articles: list[dict[str, Any]] = []
    for candidate in selected:
        if not isinstance(candidate, Mapping):
            continue
        clusters = candidate.get("theme_clusters")
        source_ref = str(candidate.get("source_ref") or candidate.get("article_id") or "")
        title = str(candidate.get("title") or "")
        published_at = str(candidate.get("published_at") or "")
        if not source_ref or not title or not published_at:
            continue
        if not isinstance(clusters, (list, tuple)):
            continue
        # 置顶源是独立用户 lane,不是 manifest bound 成员:不参与工作集投影。
        if candidate.get("source_bucket") == "pinned_source":
            continue
        # B1:只投影 manifest-bound 来源;未绑定/漂移 → 单条 gap 跳过。
        bound_ids = bound_article_ids or set()
        if bound_ids and source_ref not in bound_ids:
            gaps.append("g_mainline_source_unbound")
            continue
        # H2:只注入命中意图主题的 clusters,不把同文的无关主题一并带入。
        # 真实主题名是长短语(如 "半导体底层卡口 / AI硬科技材料 / 去日化"),
        # 意图关键词是短词(如 "半导体"):按包含匹配,关键词是主题名的子串
        # 或主题名是关键词的子串都算命中。
        matched = [str(c) for c in clusters if _theme_name_hits(str(c), topics)]
        if not matched:
            continue
        # H2:thesis heads 只保留 related_topics 与意图主题重叠者,不泄漏无关论点。
        core_theses = candidate.get("core_theses") or ()
        thesis_heads = tuple(
            str(t.get("title") or "")
            for t in core_theses
            if isinstance(t, Mapping)
            and t.get("title")
            and any(
                _theme_name_hits(str(topic), topics)
                for topic in t.get("related_topics", [])
                if isinstance(topic, str)
            )
        )
        articles.append(
            {
                "source_ref": source_ref,
                "title": title,
                "published_at": published_at,
                "theme_clusters": matched,
                "thesis_heads": thesis_heads,
            }
        )
    result = project_mainline(
        tuple(articles),
        generation=generation,
        manifest_sha256=manifest_sha256,
    )
    gaps = list(dict.fromkeys((*gaps, *result.data_gaps)))
    if result.themes:
        return {
            "themes": [
                {
                    "theme": theme.theme,
                    "theses": [
                        {
                            "source_ref": thesis.source_ref,
                            "title": thesis.title,
                            "published_at": thesis.published_at,
                            "generation": thesis.generation,
                            "thesis_heads": list(thesis.thesis_heads),
                        }
                        for thesis in theme.theses
                    ],
                }
                for theme in result.themes
            ],
            "generation": result.generation,
            "manifest_sha256": result.manifest_sha256,
            "data_gaps": gaps,
        }
    if not articles:
        gaps.append("g_mainline_no_matching_theme")
    return {
        "themes": [],
        "generation": result.generation,
        "manifest_sha256": result.manifest_sha256,
        "data_gaps": gaps,
    }


def _build_methodology_projection(
    *,
    selected: Sequence[Mapping[str, Any]],
    intent_tokens: Mapping[str, set[str]],
    generation: str,
    manifest_sha256: str,
    bound_article_ids: set[str] | None = None,
    manifest_ready: bool = False,
    now: str = "",
    concept_topics: set[str] | None = None,
) -> dict[str, Any]:
    """Project the selected candidates' methodology rules by request-intent topics.

    Pure read-only adapter over :func:`project_methodology`: rules come from
    the candidates' compact ``methodology_rules`` (teacher-original
    methodology_rule units), each rule keeps its real source_ref and time,
    and only rules overlapping the request's intent topics are kept.  A READY
    manifest with a real generation and bound-source membership is required
    (B1, same gate as mainline); rules older than the 365-day methodology
    clock are not injected.  Nothing is written or cached.
    """
    from fin_analyse.guo_teacher_research.g_methodology_projection import project_methodology

    if not manifest_ready or not generation:
        return {
            "groups": [],
            "generation": generation,
            "manifest_sha256": manifest_sha256,
            "data_gaps": ["g_methodology_manifest_not_ready"],
            "low_confidence_skipped": 0,
        }
    topics = set(intent_tokens.get("topics") or set())
    # 概念词补充只作用于此投影(不扩散到共享 intent_tokens/mainline)。
    if concept_topics:
        topics |= concept_topics
    gaps: list[str] = []
    rules: list[dict[str, Any]] = []
    # 无 now 时不启用 365 天窗口(与 mainline point-in-time 同语义:无时点不过滤)。
    now_dt = _parse_datetime(now) if now else None
    for candidate in selected:
        if not isinstance(candidate, Mapping):
            continue
        source_ref = str(candidate.get("source_ref") or candidate.get("article_id") or "")
        if not source_ref:
            continue
        # 置顶源是独立用户 lane,不是 manifest bound 成员:不参与方法论投影。
        if candidate.get("source_bucket") == "pinned_source":
            continue
        # B1:只投影 manifest-bound 来源;未绑定/漂移 → 单条 gap 跳过。
        bound_ids = bound_article_ids or set()
        if bound_ids and source_ref not in bound_ids:
            gaps.append("g_methodology_source_unbound")
            continue
        raw_rules = candidate.get("methodology_rules")
        if not isinstance(raw_rules, (list, tuple)):
            continue
        # M1:规则身份强校验——article_id 必须等于候选权威身份;generation_id
        # 非空且等于 fresh-pair generation(存在 deep_read_material 时)。
        material = candidate.get("deep_read_material")
        artifact_generation = (
            material.get("generation_id") if isinstance(material, Mapping) else None
        )
        candidate_article_id = str(candidate.get("article_id") or "")
        # B3:deep-read 保守 available_at(artifact 生成时点)——as-of 门防历史回放。
        candidate_available_at = str(candidate.get("available_at") or "")
        for raw in raw_rules:
            if not isinstance(raw, Mapping):
                gaps.append("g_methodology_entry_invalid")
                continue
            rule_topics = raw.get("related_topics")
            if not isinstance(rule_topics, (list, tuple)):
                gaps.append("g_methodology_entry_invalid")
                continue
            rule_topic_strs = [str(t) for t in rule_topics if isinstance(t, str) and t]
            # H2:只投影命中意图主题的规则(双向包含匹配,与 mainline 同规则)。
            if not any(_theme_name_hits(topic, topics) for topic in rule_topic_strs):
                continue
            rule_article = str(raw.get("article_id") or "")
            if rule_article and rule_article != candidate_article_id:
                gaps.append("g_methodology_identity_mismatch")
                continue
            rule_generation = str(raw.get("generation_id") or "")
            if not rule_generation or (
                artifact_generation and rule_generation != str(artifact_generation)
            ):
                gaps.append("g_methodology_generation_mismatch")
                continue
            # B3:as-of 门——候选的 deep-read available_at 不晚于有效 now,
            # 否则历史回放可注入当时尚未生成的 deep-read 方法论。
            if (
                candidate_available_at
                and now_dt is not None
                and _parse_datetime(candidate_available_at) > now_dt
            ):
                gaps.append("g_methodology_not_available_yet")
                continue
            # 365 天方法论时钟:超窗规则不注入(与 dynamic_clock 语义一致)。
            published_at = str(raw.get("published_at") or "")
            if (
                published_at
                and now_dt is not None
                and _parse_datetime(published_at) < now_dt - timedelta(days=365)
            ):
                gaps.append("g_methodology_rule_stale")
                continue
            rules.append(
                {
                    "source_ref": source_ref,
                    "title": str(raw.get("title") or "")[:50],
                    "rule": str(raw.get("rule") or ""),
                    "teacher_quote": str(raw.get("teacher_quote") or ""),
                    "apprentice_interpretation": str(raw.get("apprentice_interpretation") or ""),
                    "related_topics": rule_topic_strs,
                    "confidence": raw.get("confidence"),
                    "published_at": published_at,
                    "available_at": candidate_available_at,
                    "generation": generation,
                }
            )
    result = project_methodology(
        tuple(rules),
        generation=generation,
        manifest_sha256=manifest_sha256,
    )
    gaps = list(dict.fromkeys((*gaps, *result.data_gaps)))
    if result.groups:
        return {
            "groups": [
                {
                    "topic": group.topic,
                    "rules": [
                        {
                            "source_ref": rule.source_ref,
                            "title": rule.title,
                            "rule": rule.rule,
                            "teacher_quote": rule.teacher_quote,
                            "apprentice_interpretation": rule.apprentice_interpretation,
                            "related_topics": list(rule.related_topics),
                            "confidence": rule.confidence,
                            "published_at": rule.published_at,
                            "available_at": rule.available_at,
                            "generation": rule.generation,
                        }
                        for rule in group.rules
                    ],
                }
                for group in result.groups
            ],
            "generation": result.generation,
            "manifest_sha256": result.manifest_sha256,
            "data_gaps": gaps,
            "low_confidence_skipped": result.low_confidence_skipped,
        }
    if not rules:
        gaps.append("g_methodology_no_matching_theme")
    return {
        "groups": [],
        "generation": result.generation,
        "manifest_sha256": result.manifest_sha256,
        "data_gaps": gaps,
        "low_confidence_skipped": result.low_confidence_skipped,
    }


def _build_cognition_mainline_projection(
    *,
    reader: Any | None,
    now: str,
    working_set_identity: str,
    question: str = "",
) -> dict[str, Any]:
    """Project the cognition mainline read-model (P-CM1, stateless, read-only).

    Reader absent/unavailable/corrupt/drifted or PIT mismatch -> typed gap
    only, never blocks the agent and never backfills.  Whole-unit budget
    eviction happens inside :func:`project_cognition_mainline`; nothing is
    written or cached.
    """
    from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
        project_cognition_mainline,
    )

    if reader is None:
        return {"items": [], "data_gaps": ["g_cognition_readmodel_unavailable"]}
    readout = reader.read()
    if readout.failure_code is not None:
        return {
            "items": [],
            "data_gaps": [f"g_cognition_readmodel_{readout.failure_code}"],
        }
    try:
        as_of = datetime.fromisoformat(now)
    except ValueError:
        return {"items": [], "data_gaps": ["g_cognition_pit_as_of_invalid"]}
    projection = project_cognition_mainline(
        readout.payload,
        as_of=as_of,
        working_set_identity=working_set_identity,
        question=question,
    )
    return {
        "items": list(projection.items),
        "data_gaps": list(projection.data_gaps),
    }
