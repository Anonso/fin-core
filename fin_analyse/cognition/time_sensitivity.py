"""Content-driven time sensitivity assessment for priority article analysis.

Architecture (per §13 Agent P0 Gate):
- Fast path: rule-based, timeout-safe, no LLM/MoA — returns immediately for cron/consumer.
- Async enrichment: optional LLM/MoA review for T0 articles, gated behind env var,
  never blocks first push.  Conflict between fast_path and enriched is recorded,
  not silently overwritten.

Source boundary:
- agent_inference: rule-based keyword matching (default fast_path).
- g_logic_transfer: LLM/MoA reasoning over G-source article themes/cues.
- g_direct: ONLY when evidence_spans directly quote the star teacher article.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

TZ_SH = timezone(timedelta(hours=8))

# ═══════════════════════════════════════════════════════════════════════════════
# Output model
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class TimeSensitivityAssessment:
    """Structured time sensitivity output — never driven by publish date alone.

    Fast path (cron/consumer) always returns rule_fast_path + agent_inference.
    """

    category: str = "unknown"  # intraday_event | short_term_tracking | active_theme | durable_framework | unknown
    label: str = ""  # Chinese display label for Feishu
    horizon: str = "unknown"  # intraday | 1-3d | 1-2w | durable | unknown
    publish_freshness: str = ""  # objective recency, NEVER the primary driver
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    data_gaps: list[str] = field(default_factory=list)
    # ── quality metadata (per §13) ──
    source_level: str = "agent_inference"  # agent_inference | g_logic_transfer | g_direct
    quality_mode: str = "rule_fast_path"  # rule_fast_path | llm_enriched | moa_enriched | fallback

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "label": self.label,
            "horizon": self.horizon,
            "publish_freshness": self.publish_freshness,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "data_gaps": list(self.data_gaps),
            "source_level": self.source_level,
            "quality_mode": self.quality_mode,
        }

    def to_display_string(self) -> str:
        """Single-line display string for use in report/feishu."""
        evidence_str = "；".join(self.evidence) if self.evidence else "无额外证据"
        return f"[{self.category}] {self.label}（原因：{self.reason}。证据：{evidence_str}）"


# ═══════════════════════════════════════════════════════════════════════════════
# Enrichment record — async quality layer for T0 star articles
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class EnrichmentRecord:
    """Async enrichment audit record for T0 star articles.

    Written after fast_path completes; consumed by background LLM/MoA workers.
    Conflict detection ensures enriched results don't silently overwrite fast_path.
    """

    article_id: str = ""
    job_id: str = ""
    enrichment_status: str = "pending"  # pending | in_progress | completed | failed
    # ── fast path snapshot ──
    fast_path_result: dict[str, Any] | None = None
    # ── enriched result (populated by LLM/MoA worker) ──
    enriched_result: dict[str, Any] | None = None
    # ── conflict detection ──
    conflict: bool = False
    conflict_reason: str = ""
    # ── metadata ──
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "job_id": self.job_id,
            "enrichment_status": self.enrichment_status,
            "fast_path_result": dict(self.fast_path_result) if self.fast_path_result else None,
            "enriched_result": dict(self.enriched_result) if self.enriched_result else None,
            "conflict": self.conflict,
            "conflict_reason": self.conflict_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def create_enrichment_pending(
    article_id: str,
    job_id: str,
    fast_path: TimeSensitivityAssessment,
) -> EnrichmentRecord:
    """Create a pending enrichment record from a fast_path result.

    Called by the runner/consumer after fast_path completes for T0 articles.
    Does NOT call LLM/MoA — just records that enrichment is pending.
    """
    now = datetime.now(TZ_SH).isoformat()
    return EnrichmentRecord(
        article_id=article_id,
        job_id=job_id,
        enrichment_status="pending",
        fast_path_result=fast_path.to_dict(),
        created_at=now,
        updated_at=now,
    )


def resolve_enrichment_conflict(
    fast_path: TimeSensitivityAssessment,
    enriched: TimeSensitivityAssessment,
) -> EnrichmentRecord:
    """Compare fast_path and enriched results; record conflict if they differ.

    Conflict is detected when categories differ. The enriched result is
    preserved alongside the fast_path snapshot — the display layer decides
    which to show.  Never silently overwrites.
    """
    now = datetime.now(TZ_SH).isoformat()
    conflict = fast_path.category != enriched.category
    reason = ""
    if conflict:
        reason = (
            f"fast_path={fast_path.category} vs enriched={enriched.category}; "
            f"fast_path confidence={fast_path.confidence:.2f}, "
            f"enriched confidence={enriched.confidence:.2f}"
        )

    return EnrichmentRecord(
        article_id="",
        job_id="",
        enrichment_status="completed",
        fast_path_result=fast_path.to_dict(),
        enriched_result=enriched.to_dict(),
        conflict=conflict,
        conflict_reason=reason,
        created_at=now,
        updated_at=now,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Keyword rules (strict, per the design specification)
# ═══════════════════════════════════════════════════════════════════════════════

# ── intraday_event: 盘中/当日情绪/盘口 ──
_INTRADAY_CLUES: list[str] = [
    "早盘",
    "午盘",
    "尾盘",
    "收盘",
    "开盘",
    "跳水",
    "拉升",
    "涨停",
    "跌停",
    "炸板",
    "盘面",
    "盘口",
    "情绪杀",
    "今天",
    "今日",
    "昨天",
    "上午",
    "下午",
    "吃面",
    "亏钱",
    "爆仓",
    "割肉",
    "追高",
    "杀跌",
]

# ── short_term_tracking: 报价/订单/供需/产能 ──
_TRACKING_CLUES: list[str] = [
    "报价",
    "订单",
    "库存",
    "供需",
    "产能",
    "涨价",
    "降价",
    "价格",
    "数量",
    "招标",
    "中标",
    "合同",
    "扩产",
    "投产",
    "出货",
    "成本",
    "毛利率",
    "净利率",
]

# ── active_theme: 产业链/ROI/国产替代/政策窗口/主线变化 ──
_THEME_CLUES: list[str] = [
    "产业链",
    "ROI",
    "国产替代",
    "政策窗口",
    "主线变化",
    "行业格局",
    "景气度",
    "渗透率",
    "市占率",
    "集中度",
    "技术路线",
    "路线图",
    "产能周期",
    "资本开支",
]

# ── durable_framework: 方法论/信息差/金融分配/认知框架 ──
_DURABLE_CLUES: list[str] = [
    "方法论",
    "信息差",
    "金融分配",
    "认知框架",
    "框架",
    "体系",
    "长期",
    "估值模型",
    "商业模式",
    "护城河",
    "壁垒",
    "定价权",
    "议价权",
    "投资哲学",
    "交易体系",
    "风控框架",
]

_FENGXIANJUN_COLUMN = "凤仙郡小故事"
# A 凤仙郡 column is a long-term business/political-economy story by default.
# A current-view classification therefore needs an explicit time locator in the
# original claim, rather than a topic cluster or a downstream inferred clock.
_FENGXIANJUN_CURRENT_CLAIM_CLUES: list[str] = [
    *_INTRADAY_CLUES,
    "本周",
    "下周",
    "这周",
    "本月",
    "下月",
    "近期",
    "最近",
    "当前",
    "目前",
    "当下",
    "现阶段",
    "短期",
    "即将",
    "刚刚",
    "最新",
]


def _contains_any(text: str, clues: list[str]) -> bool:
    """Case-insensitive check if text contains any clue word."""
    lower = text.lower()
    return any(clue.lower() in lower for clue in clues)


def _find_evidence(text: str, clues: list[str]) -> list[str]:
    """Return the matching clue words found in text."""
    lower = text.lower()
    return [clue for clue in clues if clue.lower() in lower]


def _fengxianjun_defaults_to_durable(column: str, source_claim_text: str) -> bool:
    """Whether a 凤仙郡 item has no explicit time-scoped original claim.

    Only the article title and extracted claim units belong here.  Theme
    clusters, reasoning chains, suggestions, and deep-read clocks are derived
    material and must not turn the source-family default into a current view.
    """
    return column == _FENGXIANJUN_COLUMN and not _contains_any(
        source_claim_text,
        _FENGXIANJUN_CURRENT_CLAIM_CLUES,
    )


def _fengxianjun_durable_assessment() -> TimeSensitivityAssessment:
    """Return the source-family default without inventing a validity window."""
    return TimeSensitivityAssessment(
        category="durable_framework",
        label="中长期框架 — 凤仙郡小故事默认不作为当前观点",
        horizon="durable",
        reason="栏目默认提供长期商业、产业或政治经济框架；原文未见明确时点的当前主张",
        evidence=["凤仙郡小故事栏目默认"],
        confidence=0.55,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Publish freshness (objective only)
# ═══════════════════════════════════════════════════════════════════════════════


def _compute_publish_freshness(
    freshness_score: float | None,
    published_at: str,
) -> str:
    """Compute objective publish recency.

    This is a purely temporal measure. It must NEVER be used as the
    primary driver of time_sensitivity; content semantics take precedence.
    """
    if published_at:
        return published_at
    if freshness_score is not None:
        if freshness_score >= 1.0:
            return "高新鲜度（0-6h内发布）"
        elif freshness_score >= 0.5:
            return "中新鲜度（24h内发布）"
        else:
            return "低新鲜度（>24h前发布）"
    return "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Deep-read clock mapping
# ═══════════════════════════════════════════════════════════════════════════════


def _classify_from_clocks(clocks: list[dict]) -> TimeSensitivityAssessment | None:
    """Map deep_read clock labels to time sensitivity categories.

    Returns None if clocks don't provide a clear signal.
    """
    if not clocks:
        return None

    clock_labels = [
        c.get("current_label", c.get("label", ""))
        for c in clocks[:3]
        if c.get("current_label") or c.get("label")
    ]
    if not clock_labels:
        return None

    labels_str = "、".join(clock_labels)
    evidence = [f"deep_read时钟={labels_str}"]

    for label in clock_labels:
        lower_label = label.lower()
        if any(w in lower_label for w in ("urgent", "high", "紧迫", "紧急", "即时")):
            return TimeSensitivityAssessment(
                category="intraday_event",
                label="盘中/当日事件 — deep_read 时钟判定高紧迫，快速衰减",
                horizon="intraday",
                reason=f"deep_read 动态时钟标记为高紧迫（{labels_str}）",
                evidence=evidence,
                confidence=0.8,
            )
        if any(w in lower_label for w in ("tracking", "follow", "跟踪", "关注", "short_term")):
            return TimeSensitivityAssessment(
                category="short_term_tracking",
                label="短期跟踪 — deep_read 时钟判定需持续关注",
                horizon="1-3d",
                reason=f"deep_read 动态时钟标记为短期跟踪（{labels_str}）",
                evidence=evidence,
                confidence=0.75,
            )
        if any(w in lower_label for w in ("durable", "stable", "持久", "长期", "long_term")):
            return TimeSensitivityAssessment(
                category="durable_framework",
                label="中长期有效 — deep_read 时钟判定低时间衰减",
                horizon="durable",
                reason=f"deep_read 动态时钟标记为长期有效（{labels_str}）",
                evidence=evidence,
                confidence=0.8,
            )

    # Generic clock → short_term_tracking as safe default
    return TimeSensitivityAssessment(
        category="short_term_tracking",
        label=f"短期跟踪 — deep_read 时钟信号（{labels_str}），未来数日仍有跟踪价值",
        horizon="1-3d",
        reason=f"deep_read 动态时钟信号（{labels_str}），内容有时效关注价值",
        evidence=evidence,
        confidence=0.6,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Rule-based classification
# ═══════════════════════════════════════════════════════════════════════════════


def _classify_by_rules(
    title: str,
    column: str,
    units: list[dict],
    clusters: list[dict],
    chains: list[dict],
    suggestions: list[dict],
) -> TimeSensitivityAssessment | None:
    """Rule-based time sensitivity classification.

    Returns None if rules are insufficient (→ fall through to LLM or unknown).
    """
    # Collect all semantic text
    unit_texts = " ".join(u.get("title", "") + " " + u.get("thesis", "") for u in (units or [])[:5])
    cluster_texts = " ".join(c.get("theme", c.get("name", "")) for c in (clusters or [])[:5])
    chain_texts = " ".join(c.get("reasoning", c.get("description", "")) for c in (chains or [])[:3])
    suggestion_texts = " ".join(s.get("suggestion_text", "") for s in (suggestions or [])[:3])
    all_semantic = f"{title} {unit_texts} {cluster_texts} {chain_texts} {suggestion_texts} {column}"

    is_g_source = column in ("星大派特刊", "星大派锐评")

    if _fengxianjun_defaults_to_durable(column, f"{title} {unit_texts}"):
        return _fengxianjun_durable_assessment()

    # ── Rule 1: intraday clues → check for override ──
    if _contains_any(all_semantic, _INTRADAY_CLUES):
        evidence = ["内容含盘中/短期情绪线索"]
        evidence.extend(_find_evidence(all_semantic, _INTRADAY_CLUES)[:5])

        # Tracking/theme clues override intraday → promote to short_term_tracking
        if _contains_any(all_semantic, _TRACKING_CLUES):
            evidence.append("同时含报价/供需/订单等跟踪线索")
            evidence.extend(_find_evidence(all_semantic, _TRACKING_CLUES)[:3])
            return TimeSensitivityAssessment(
                category="short_term_tracking",
                label="短期跟踪 — 内容既含短期盘面线索，也涉及报价/供需/订单变化，未来数日仍有跟踪价值",
                horizon="1-3d",
                reason="内容包含短期盘面情绪线索，但同时包含报价/供需/订单等需要持续跟踪的信息",
                evidence=evidence,
                confidence=0.65,
            )
        return TimeSensitivityAssessment(
            category="intraday_event",
            label="短时效：盘面情绪点评/当日事件，需当日或次日复核",
            horizon="intraday",
            reason="内容以盘中/当日情绪和盘口现象为主，信息快速衰减",
            evidence=evidence,
            confidence=0.7,
        )

    # ── Rule 2: tracking clues (STRONG signal — before durable) ──
    has_tracking = _contains_any(all_semantic, _TRACKING_CLUES)
    has_theme_signal = _contains_any(all_semantic, _THEME_CLUES)
    has_durable_signal = _contains_any(all_semantic, _DURABLE_CLUES)

    if has_tracking:
        evidence = ["内容含报价/供需/订单/产业链变化线索"]
        evidence.extend(_find_evidence(all_semantic, _TRACKING_CLUES)[:5])
        # If also has theme clues, promote to active_theme
        if has_theme_signal:
            evidence.append("同时含产业链/ROI/政策窗口等主线信号")
            evidence.extend(_find_evidence(all_semantic, _THEME_CLUES)[:3])
            return TimeSensitivityAssessment(
                category="active_theme",
                label="当前主线 — 涉及报价/供需跟踪+产业链主线方向，需持续关注",
                horizon="1-2w",
                reason="内容同时涉及报价/订单/供需等强跟踪信号和产业链主线方向，属于当前主线跟踪范畴",
                evidence=evidence,
                confidence=0.7,
            )
        # If `信息差` co-occurs with tracking signals, it's evidence FOR tracking,
        # not durable — `信息差` here means information asymmetry about quotes/orders,
        # not methodological framework.
        if any("信息差" in e for e in _find_evidence(all_semantic, _DURABLE_CLUES)):
            evidence.append("含信息差线索（与报价/供需跟踪信号相关）")
        return TimeSensitivityAssessment(
            category="short_term_tracking",
            label="持续关注：涉及报价/供需/订单/产业链变化，未来数日仍有跟踪价值",
            horizon="1-3d",
            reason="内容涉及报价、订单、供需或产能等信息，未来数日仍有持续跟踪价值",
            evidence=evidence,
            confidence=0.7,
        )

    # ── Rule 3: theme clues (before durable) ──
    if has_theme_signal:
        evidence = ["内容含产业链/主线相关线索"]
        evidence.extend(_find_evidence(all_semantic, _THEME_CLUES)[:5])
        # If 信息差 co-occurs with theme, it's theme-level tracking
        if any("信息差" in e for e in _find_evidence(all_semantic, _DURABLE_CLUES)):
            evidence.append("含信息差信号（与产业链主线判断相关）")
        return TimeSensitivityAssessment(
            category="active_theme",
            label="当前主线 — 涉及产业链/ROI/政策窗口方向，需持续关注",
            horizon="1-2w",
            reason="内容涉及产业链分析、ROI判断或政策窗口，属于主线跟踪范畴",
            evidence=evidence,
            confidence=0.65,
        )

    # ── Rule 4: durable framework (ONLY when NO tracking/theme/intraday signals) ──
    # `信息差` alone (without 报价/ROI/供需/情绪杀) can be durable.
    # But `信息差` + any tracking/theme signal → was already captured above.
    if has_durable_signal:
        evidence = ["内容含方法论/长期框架线索（无报价/供需/产业链等短期跟踪信号）"]
        evidence.extend(_find_evidence(all_semantic, _DURABLE_CLUES)[:5])
        return TimeSensitivityAssessment(
            category="durable_framework",
            label="中长期有效：产业逻辑/方法论框架，不因发布时间自动失效",
            horizon="durable",
            reason="内容涉及投资方法论、认知框架或长期产业逻辑，且无明显短期跟踪信号，时间衰减低",
            evidence=evidence,
            confidence=0.7,
        )

    # ── Rule 5: G source + theme clusters ──
    has_theme = bool(clusters) and any(
        c.get("theme") or c.get("name") for c in (clusters or [])[:3]
    )
    if is_g_source and has_theme:
        theme_names = [
            c.get("theme", c.get("name", ""))
            for c in (clusters or [])[:3]
            if c.get("theme") or c.get("name")
        ]
        evidence = [f"G源（{column}）+ 主题聚类：{'、'.join(theme_names)}"]
        return TimeSensitivityAssessment(
            category="active_theme",
            label=f"当前主线 — G源（{column}）文章，涉及主题聚类方向，需持续关注产业链动态",
            horizon="1-2w",
            reason=f"G源文章涉及主题聚类（{'、'.join(theme_names)}），属于当前主线方向",
            evidence=evidence,
            confidence=0.6,
        )

    # Note: G source alone (without content clues or theme clusters) is NOT
    # returned here — it falls through to LLM classification. Rule-based
    # classification requires at least some content signal.
    if has_theme:
        theme_names = [
            c.get("theme", c.get("name", ""))
            for c in (clusters or [])[:3]
            if c.get("theme") or c.get("name")
        ]
        evidence = [f"主题聚类：{'、'.join(theme_names)}"]
        if is_g_source:
            evidence.insert(0, f"G源（{column}）")
        return TimeSensitivityAssessment(
            category="active_theme",
            label="当前主线 — 涉及产业链主题聚类方向，需持续关注",
            horizon="1-2w",
            reason=f"文章涉及主题聚类（{'、'.join(theme_names)}），属于当前关注方向",
            evidence=evidence,
            confidence=0.6 if is_g_source else 0.55,
        )

    # Insufficient for rule-based → return None (fall through)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# LLM semantic classification (T0 star articles only)
# ═══════════════════════════════════════════════════════════════════════════════

_LLM_TIME_SENSITIVITY_PROMPT = """你是一个投研内容时效性分析器。根据文章信息判断其"内容语义时效性"。

## 分类定义
- intraday_event: 盘中/当日情绪、盘口、短期事件，信息快速衰减
- short_term_tracking: 未来几天仍需跟踪，如报价、供需、政策窗口、订单变化
- active_theme: 当前主线/产业链逻辑，需持续关注
- durable_framework: 方法论/长期框架，低时间衰减
- unknown: 缺少足够内容判断

## 规则
1. 不要用发布时间直接决定时效性。发布时间只说明"新鲜度"，不代表内容时效性。
2. 两天前的文章如果讨论报价/供需/产业链，仍然可以是 short_term_tracking 或 active_theme。
3. 今天的文章如果只是盘面吐槽/情绪宣泄，应判为 intraday_event。
4. 必须在 evidence_spans 中给出原文片段作为证据。

## 输入
{input_text}

## 输出
严格输出一个 JSON 对象：
{{
  "category": "intraday_event | short_term_tracking | active_theme | durable_framework | unknown",
  "label": "面向用户的中文标签（15-30字）",
  "horizon": "intraday | 1-3d | 1-2w | durable | unknown",
  "reason": "判断理由（2-3句话）",
  "evidence_spans": ["原文片段1", "原文片段2"],
  "confidence": 0.0-1.0
}}

只输出 JSON，不要输出其他内容。"""


def _llm_classify(
    title: str,
    column: str,
    units: list[dict],
    clusters: list[dict],
    chains: list[dict],
    suggestions: list[dict],
) -> TimeSensitivityAssessment | None:
    """LLM-based time sensitivity classification for T0 star articles.

    **CURRENTLY DISABLED for cron/priority path.**  ThreadPoolExecutor does not
    provide hard timeout — the thread keeps running after future.result(timeout=N)
    and context manager exit.  For cron safety, the default code path skips LLM
    entirely and uses rule fallback + ``llm_time_sensitivity_unavailable`` data_gap.

    To re-enable LLM (e.g. interactive one-shot analysis), set
    ``FIN_TIME_SENSITIVITY_LLM=1`` in the environment.
    """
    if not os.environ.get("FIN_TIME_SENSITIVITY_LLM"):
        return None

    try:
        from fin_analyse.cognition.llm import CognitionLLM
    except ImportError:
        logger.debug("CognitionLLM not available for time sensitivity")
        return None

    # Build input text from available artifacts
    parts = [f"标题：{title}"]
    if column:
        parts.append(f"栏目：{column}")
    for u in (units or [])[:5]:
        parts.append(f"信息单元：{u.get('title', '')} — {u.get('thesis', '')}")
    for c in (clusters or [])[:3]:
        parts.append(f"主题聚类：{c.get('theme', c.get('name', ''))}")
    for c in (chains or [])[:2]:
        parts.append(f"证据链：{c.get('reasoning', c.get('description', ''))}")
    for s in (suggestions or [])[:2]:
        parts.append(f"研究方向：{s.get('suggestion_text', '')}")

    input_text = "\n".join(p for p in parts if p and not p.endswith("："))
    prompt = _LLM_TIME_SENSITIVITY_PROMPT.format(input_text=input_text[:3000])

    try:
        llm = CognitionLLM.from_config()
        result = llm.complete_json(prompt, expected_type="time_sensitivity")
    except Exception as exc:
        logger.debug("LLM time sensitivity call failed: %s", exc)
        return None

    if not result.ok or not isinstance(result.data, dict):
        logger.debug("LLM time sensitivity returned invalid: %s", result.error)
        return None

    data = result.data
    category = str(data.get("category", "unknown"))
    valid_categories = {
        "intraday_event",
        "short_term_tracking",
        "active_theme",
        "durable_framework",
        "unknown",
    }
    if category not in valid_categories:
        category = "unknown"

    valid_horizons = {"intraday", "1-3d", "1-2w", "durable", "unknown"}
    horizon = str(data.get("horizon", "unknown"))
    if horizon not in valid_horizons:
        horizon = "unknown"

    evidence_spans = data.get("evidence_spans", [])
    evidence = [str(s) for s in evidence_spans[:5]] if isinstance(evidence_spans, list) else []

    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    return TimeSensitivityAssessment(
        category=category,
        label=str(data.get("label", ""))[:60],
        horizon=horizon,
        reason=str(data.get("reason", ""))[:300],
        evidence=evidence,
        confidence=confidence,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════


def assess_time_sensitivity(
    article: dict[str, Any],
    deep_read_result: dict[str, Any],
    temporal_context: dict[str, Any] | None,
) -> TimeSensitivityAssessment:
    """Assess time sensitivity from content semantics (NOT publish recency).

    Priority:
    1. deep_read clocks (dynamic clock labels)
    2. Rule-based keyword classification
    3. LLM semantic classification (T0 star articles only)
    4. Fallback: unknown

    Publish freshness is computed separately and NEVER drives category.
    """
    title = str(article.get("title", "")).strip()
    column = str(article.get("column", ""))
    published_at = str(article.get("published_at", ""))
    units: list[dict] = deep_read_result.get("units", []) or []
    clocks: list[dict] = deep_read_result.get("clocks", []) or []
    clusters: list[dict] = deep_read_result.get("theme_clusters", []) or []
    chains: list[dict] = deep_read_result.get("evidence_chains", []) or []
    suggestions: list[dict] = deep_read_result.get("suggestions", []) or []
    has_deep_read = bool(units)

    fengxianjun_default_long_term = _fengxianjun_defaults_to_durable(
        column,
        " ".join(
            [
                title,
                *(
                    f"{unit.get('title', '')} {unit.get('thesis', '')}"
                    for unit in units[:5]
                ),
            ]
        ),
    )

    top_event = (temporal_context or {}).get("top_event", {}) or {}
    freshness_score = top_event.get("freshness_score")
    is_g_source = column in ("星大派特刊", "星大派锐评")

    # ── publish_freshness (objective, separate) ──
    publish_freshness = _compute_publish_freshness(freshness_score, published_at)

    # ── empty title → unknown ──
    if not title:
        return TimeSensitivityAssessment(
            category="unknown",
            label="时效未知",
            horizon="unknown",
            publish_freshness=publish_freshness,
            reason="文章标题为空，无法判断时效性",
            data_gaps=["empty_title"],
        )

    # ── 1) deep_read clocks ──
    if has_deep_read and not fengxianjun_default_long_term:
        clock_result = _classify_from_clocks(clocks)
        if clock_result is not None:
            clock_result.publish_freshness = publish_freshness
            return clock_result

    # ── 2) rule-based classification ──
    rule_result = _classify_by_rules(
        title=title,
        column=column,
        units=units,
        clusters=clusters,
        chains=chains,
        suggestions=suggestions,
    )
    if rule_result is not None:
        rule_result.publish_freshness = publish_freshness
        return rule_result

    # ── 3) LLM enrichment NOT called synchronously (per §13 Agent P0 Gate) ──
    # Fast path MUST return immediately for cron/consumer.  LLM/MoA enrichment
    # is async-only via EnrichmentRecord written by the runner for T0 articles.
    # The _llm_classify() helper is retained for manual/interactive use only
    # (gated behind FIN_TIME_SENSITIVITY_LLM=1) but is NEVER called from
    # assess_time_sensitivity() or any cron/consumer code path.
    if is_g_source:
        gap_assessment = TimeSensitivityAssessment(
            category="short_term_tracking",
            label=f"持续关注 — G源（{column}）文章（规则兜底，LLM enrichment pending）",
            horizon="1-3d",
            publish_freshness=publish_freshness,
            reason="G源文章，规则无法完全匹配时使用保守判定；异步 enrichment 可后续提升置信度",
            data_gaps=["llm_enrichment_pending"],
            confidence=0.4,
        )
        return gap_assessment

    # ── 4) fallback: unknown ──
    gaps: list[str] = []
    if not published_at:
        gaps.append("missing_published_at")
    if not has_deep_read:
        gaps.append("no_deep_read_data")

    return TimeSensitivityAssessment(
        category="unknown",
        label="时效未知：缺少足够的发布时间或内容语义线索",
        horizon="unknown",
        publish_freshness=publish_freshness,
        reason="缺少发布时间且内容线索不足以判断时效性"
        if not published_at
        else "内容线索不足以判断时效性",
        evidence=[],
        confidence=0.0,
        data_gaps=gaps,
    )


def assess_text_time_sensitivity(
    title: str = "",
    content: str = "",
    column: str = "",
) -> TimeSensitivityAssessment:
    """Deterministic rule-based time sensitivity from raw text (no deep_read/LLM).

    Reuses the same keyword-marker lists as _classify_by_rules but accepts
    raw title + content + column instead of requiring article + deep_read_result + temporal_context.

    This is the fast-path entry point for RuleBasedClaimExtractor and other
    ingestion-time consumers that need horizon enrichment without LLM.

    Returns:
        TimeSensitivityAssessment with category, horizon, and evidence.
        Falls back to category="unknown", horizon="unknown" when no clues match.
    """
    # Build semantic text from title + content + column
    semantic = f"{title} {content[:3000]} {column}"

    if _fengxianjun_defaults_to_durable(column, f"{title} {content[:3000]}"):
        return _fengxianjun_durable_assessment()

    # ── Rule 1: intraday clues ──
    if _contains_any(semantic, _INTRADAY_CLUES):
        evidence = ["内容含盘中/短期情绪线索"]
        evidence.extend(_find_evidence(semantic, _INTRADAY_CLUES)[:5])

        # Tracking/theme clues override intraday → promote to short_term_tracking
        if _contains_any(semantic, _TRACKING_CLUES):
            evidence.append("同时含报价/供需/订单等跟踪线索")
            evidence.extend(_find_evidence(semantic, _TRACKING_CLUES)[:3])
            return TimeSensitivityAssessment(
                category="short_term_tracking",
                label="短期跟踪 — 内容既含短期盘面线索，也涉及报价/供需/订单变化",
                horizon="1-3d",
                reason="内容包含短期盘面情绪线索，但同时包含报价/供需/订单等需要持续跟踪的信息",
                evidence=evidence,
                confidence=0.65,
                source_level="agent_inference",
                quality_mode="rule_fast_path",
            )
        return TimeSensitivityAssessment(
            category="intraday_event",
            label="短时效：盘面情绪点评/当日事件",
            horizon="intraday",
            reason="内容以盘中/当日情绪和盘口现象为主，信息快速衰减",
            evidence=evidence,
            confidence=0.7,
            source_level="agent_inference",
            quality_mode="rule_fast_path",
        )

    # ── Rule 2: tracking clues ──
    has_tracking = _contains_any(semantic, _TRACKING_CLUES)
    has_theme_signal = _contains_any(semantic, _THEME_CLUES)
    has_durable_signal = _contains_any(semantic, _DURABLE_CLUES)

    if has_tracking:
        evidence = ["内容含报价/供需/订单/产业链变化线索"]
        evidence.extend(_find_evidence(semantic, _TRACKING_CLUES)[:5])
        if has_theme_signal:
            evidence.append("同时含产业链/ROI/政策窗口等主线信号")
            evidence.extend(_find_evidence(semantic, _THEME_CLUES)[:3])
            return TimeSensitivityAssessment(
                category="active_theme",
                label="当前主线 — 涉及报价/供需跟踪+产业链主线方向",
                horizon="1-2w",
                reason="内容同时涉及报价/订单/供需等强跟踪信号和产业链主线方向",
                evidence=evidence,
                confidence=0.7,
                source_level="agent_inference",
                quality_mode="rule_fast_path",
            )
        return TimeSensitivityAssessment(
            category="short_term_tracking",
            label="持续关注：涉及报价/供需/订单/产业链变化",
            horizon="1-3d",
            reason="内容涉及报价、订单、供需或产能等信息，未来数日仍有持续跟踪价值",
            evidence=evidence,
            confidence=0.7,
            source_level="agent_inference",
            quality_mode="rule_fast_path",
        )

    # ── Rule 3: theme clues ──
    if has_theme_signal:
        evidence = ["内容含产业链/主线相关线索"]
        evidence.extend(_find_evidence(semantic, _THEME_CLUES)[:5])
        return TimeSensitivityAssessment(
            category="active_theme",
            label="当前主线 — 涉及产业链/ROI/政策窗口方向",
            horizon="1-2w",
            reason="内容涉及产业链分析、ROI判断或政策窗口，属于主线跟踪范畴",
            evidence=evidence,
            confidence=0.65,
            source_level="agent_inference",
            quality_mode="rule_fast_path",
        )

    # ── Rule 4: durable framework ──
    if has_durable_signal:
        evidence = ["内容含方法论/长期框架线索"]
        evidence.extend(_find_evidence(semantic, _DURABLE_CLUES)[:5])
        return TimeSensitivityAssessment(
            category="durable_framework",
            label="中长期有效：产业逻辑/方法论框架",
            horizon="durable",
            reason="内容涉及投资方法论、认知框架或长期产业逻辑，时间衰减低",
            evidence=evidence,
            confidence=0.7,
            source_level="agent_inference",
            quality_mode="rule_fast_path",
        )

    # ── Fallback: unknown ──
    return TimeSensitivityAssessment(
        category="unknown",
        label="时效未知：缺少足够的内容语义线索",
        horizon="unknown",
        reason="内容线索不足以判断时效性",
        evidence=[],
        confidence=0.0,
        data_gaps=["no_temporal_clues"],
        source_level="agent_inference",
        quality_mode="rule_fast_path",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Enrichment persistence — async quality layer audit trail
# ═══════════════════════════════════════════════════════════════════════════════

_ENRICHMENT_SINK_NAME = "time_sensitivity_enrichment.jsonl"


def write_enrichment_record(
    record: EnrichmentRecord,
    kb_root: str | Any | None = None,
) -> None:
    """Append an enrichment record to the runtime audit sink.

    Safe to call from runner/consumer after fast_path completes.
    Does NOT call LLM/MoA — only writes the pending record.
    Failure to write is logged but never raised.
    """
    try:
        from pathlib import Path

        from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

        root = kb_root if kb_root is not None else default_knowledge_base_root()
        runtime = Path(str(root)) / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        sink = runtime / _ENRICHMENT_SINK_NAME
        import json

        with open(sink, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("Failed to write enrichment record for %s", record.article_id, exc_info=True)
