"""Phase 3: MoA cross-cluster synthesis — the core of MoA deepening.

Takes multiple ClusterAnalysis outputs from Phase 2 and synthesizes them
into a unified SynthesisReport via MoA with 4 capability slots across T0/T1.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fin_analyse.cognition.cross_article.models import (
    ClusterAnalysis,
    QualityFlags,
    SynthesisReport,
    build_suggested_signal_queries,
    validate_no_trade_fields,
)
from fin_analyse.cognition.cross_article.synthesis_store import SynthesisStore
from fin_analyse.moa._utils import clamp_float
from fin_analyse.moa.models import (
    MoAReferenceRole,
    MoARequest,
    MoAResult,
)
from fin_analyse.utils.ids import stable_id

logger = logging.getLogger(__name__)

# ── Reference role prompts ───────────────────────────────────────────────────

_ROLE_SECTOR_DIRECTION = """你是一个板块方向分析器。基于以下 cluster 分析摘要，识别星大派当前最看好的板块方向。

对每个板块方向，标注：
- 板块名称和方向判断
- 确信度 (0-1)
- 证据来自哪些 cluster
- 是在强化还是弱化
- 支持观点的关键证据

只输出简洁中文分析。"""

_ROLE_STOCK_CANDIDATE = """你是一个标的提取器。基于以下 cluster 分析摘要，提取星大派关注的公司。

对每个标的，严格区分：
- direct_mention: 老师原文直接点名
- inferred_from_logic: 从行业逻辑推导

标注推导链路、来源 cluster、置信度。推导标的 confidence 不超过 0.7。"""

_ROLE_CONFLICT_DETECTOR = """你是一个矛盾检测器。基于以下 cluster 分析摘要，找出跨 cluster 的矛盾。

检查：
- 不同板块是否争抢资金/关注度
- 同一标的是否在不同 cluster 中有矛盾判断
- 行业逻辑是否存在冲突

每个矛盾标注涉及的 cluster 和具体冲突点。"""

_ROLE_BLIND_SPOT = """你是一个盲区检查器。基于以下 cluster 分析摘要，找出未被覆盖的重要领域。

注意：只能基于 cluster 覆盖范围指出"缺什么信息"，不能引入实时市场事实、新闻或研报结论。
输出：缺失的板块、未被讨论的风险、需要验证的问题。"""

_ROLE_TEMPORAL_COHERENCE = """你是一个时间线一致性判断器。基于以下 cluster 分析摘要，判断观点的时效趋势。

对每个主题判断：
- 持续强化（多篇文章不断加强 → 高置信）
- 新出现（首次提及，待验证 → 低置信）
- 在弱化/修正（后期文章修正前期观点）
- 一致（多篇文章观点无变化）

按时间线排序，标注趋势和证据。"""

# ── Aggregator prompt (single-backend fallback) ───────────────────────────────

_AGGREGATOR_PROMPT = """你是星大派跨文章认知综合器。输入是多个主题 cluster 的分析摘要和多个参考分析员的输出，你需要综合输出：

1. 板块方向排序 — 星大派当前最关注的板块，按确信度排序
2. 关注标的汇总 — 区分直接点名和间接推导，标注推导链路
3. 风险与盲区 — 被忽略的风险、未被讨论的重要板块
4. 观点变化趋势 — 对比上一期（如有），哪些观点在强化/弱化/转向

输出一个 JSON object：
{{
  "sector_directions": [
    {{
      "sector": "板块名",
      "direction": "方向描述",
      "source_clusters": ["cluster_id"],
      "source_article_ids": ["article_id"],
      "strength": 0.0-1.0,
      "key_evidence": "证据简述",
      "evolution_trend": "强化 / 弱化 / 新观点 / 一致"
    }}
  ],
  "focused_stocks": [
    {{
      "company": "公司名",
      "ticker": "股票代码或空",
      "reference_type": "direct_mention / inferred_from_logic",
      "derivation_chain": "推导链路简述",
      "source_clusters": ["cluster_id"],
      "source_article_ids": ["article_id"],
      "confidence": 0.0-1.0
    }}
  ],
  "risks_and_blind_spots": [
    {{"type": "板块遗漏 / 风险忽略 / 证据不足", "description": "...", "severity": "low / medium / high"}}
  ],
  "viewpoint_changes": [
    {{
      "topic": "主题",
      "previous_stance": "上一个立场",
      "current_stance": "当前立场",
      "trend": "强化 / 弱化 / 修正 / 转向",
      "evidence": "变化证据",
      "source_clusters": ["cluster_id"],
      "source_article_ids": ["article_id"]
    }}
  ],
  "cross_cluster_contradictions": [],
  "consensus": ["共识1"],
  "disagreements": ["分歧1"],
  "blind_spots": ["盲区1"],
  "confidence": 0.0-1.0
}}

硬约束：
- 每个结论必须标注来源 cluster_id，无来源的不得输出
- 区分「老师直接表达」和「从老师逻辑推导」，不得混淆
- 不确定时标注 confidence < 0.5，不强行给出确定结论
- 不输出 action、position_pct、target_price、buy、sell、买入、卖出、仓位、加仓、减仓
- advisory_only 始终为 true

只输出 JSON，不要加额外文字。"""

# ── Expected schema for MoA ──────────────────────────────────────────────────

_SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "sector_directions",
        "focused_stocks",
        "viewpoint_changes",
        "risks_and_blind_spots",
        "cross_cluster_contradictions",
        "consensus",
        "disagreements",
        "blind_spots",
        "confidence",
    ],
    "properties": {
        "sector_directions": {"type": "array"},
        "focused_stocks": {"type": "array"},
        "viewpoint_changes": {"type": "array"},
        "risks_and_blind_spots": {"type": "array"},
        "cross_cluster_contradictions": {"type": "array"},
        "consensus": {"type": "array", "items": {"type": "string"}},
        "disagreements": {"type": "array", "items": {"type": "string"}},
        "blind_spots": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
}


def _role_prompt(question: str, analyses_text: str, instruction: str) -> str:
    return f"{question}\n\nCluster 分析摘要：\n{analyses_text}\n\n你的任务：{instruction}\n\n请输出简洁中文分析。"


class CrossClusterSynthesizer:
    """Phase 3 synthesis engine with MoA capability slots.

    Primary path: MoA with 4 capability slots (T0/T1) → aggregator synthesis.
    Fallback path: single aggregator backend direct call.
    Last resort: stale previous synthesis or raw analyses summary.
    """

    def __init__(
        self,
        store: SynthesisStore,
        aggregator_backend: Any | None = None,
        moa_engine: Any | None = None,
        reference_backends: dict[str, Any] | None = None,
    ) -> None:
        self._store = store
        self._aggregator = aggregator_backend
        self._moa = moa_engine
        self._reference_backends = reference_backends or {}

    # ── public ───────────────────────────────────────────────────────────

    def synthesize(
        self,
        analyses: list[ClusterAnalysis],
        *,
        time_range: dict[str, str] | None = None,
    ) -> SynthesisReport:
        """Synthesize cluster analyses via MoA (or fallbacks)."""
        if not analyses:
            return self._empty_synthesis([], [])

        analyses_text = self._format_analyses(analyses)

        # ── Primary: MoA with reference roles ──
        if self._moa is not None and len(self._reference_backends) >= 2:
            try:
                return self._synthesize_with_moa(analyses, analyses_text, time_range)
            except Exception as exc:
                logger.warning("MoA synthesis failed, falling back: %s", exc)

        # ── Fallback: single aggregator ──
        if self._aggregator is not None:
            try:
                return self._synthesize_with_single(analyses, analyses_text, time_range)
            except Exception as exc:
                logger.warning("Single aggregator failed: %s", exc)

        # ── Last resort ──
        return self._fallback_stale_or_raw(analyses, "all backends failed", time_range)

    # ── MoA path ─────────────────────────────────────────────────────────

    def _synthesize_with_moa(
        self,
        analyses: list[ClusterAnalysis],
        analyses_text: str,
        time_range: dict[str, str] | None,
    ) -> SynthesisReport:
        """Run full MoA with 4 capability slots."""
        assert self._moa is not None
        article_ids, cluster_ids = self._collect_ids(analyses)

        roles = [
            MoAReferenceRole(
                name="core_reasoning",
                backend_name="t0",
                prompt=_role_prompt(
                    "识别星大派当前最看好的板块方向（按确信度排序）并提取关注标的（严格区分直接点名和逻辑推导）。",
                    analyses_text,
                    _ROLE_SECTOR_DIRECTION
                    + "\n\n此外，也请完成标的提取：\n"
                    + _ROLE_STOCK_CANDIDATE,
                ),
            ),
            MoAReferenceRole(
                name="cross_view_risk",
                backend_name="t1",
                prompt=_role_prompt(
                    "找出跨 cluster 的矛盾和冲突，以及未被覆盖的重要板块和风险盲区。",
                    analyses_text,
                    _ROLE_CONFLICT_DETECTOR
                    + "\n\n此外，也请检查盲区：\n"
                    + _ROLE_BLIND_SPOT,
                ),
                weight=1.2,
            ),
            MoAReferenceRole(
                name="boundary_schema_guard",
                backend_name="t1",
                prompt=_role_prompt(
                    "按时间线判断观点的强化/弱化/转向趋势，检查 source 边界和 schema 合规。",
                    analyses_text,
                    _ROLE_TEMPORAL_COHERENCE,
                ),
            ),
            MoAReferenceRole(
                name="independent_strong_reasoning",
                backend_name="t1",
                prompt=_role_prompt(
                    "从独立角度重新审视所有 cluster 分析，发现主流视角可能遗漏的信号、关联和盲区。",
                    analyses_text,
                    "请给出独立于其他能力槽的完整判断，包括：独立识别的板块方向、被其他分析忽略的标的线索、跨时段的隐含趋势、以及你认为最重要的 3 个盲区。",
                ),
            ),
        ]

        now = datetime.now(UTC).isoformat()
        request = MoARequest(
            task_id=stable_id("cross-synthesis", *cluster_ids, prefix="xsyn-"),
            task_type="cross_article_synthesis",
            context={
                "cluster_ids": cluster_ids,
                "article_ids": article_ids,
                "num_analyses": len(analyses),
                "generated_at": now,
            },
            aggregator_prompt=_AGGREGATOR_PROMPT.format(
                analyses_text=analyses_text[:6000],
            ),
            reference_roles=roles,
            expected_schema=_SYNTHESIS_SCHEMA,
            min_reference_success=2,
            fallback_policy="fallback",
            metadata={
                "adapter": "cross_article_synthesis",
                "moa_topology": "capability_slots_v1",
                "capability_slots": [
                    {
                        "slot": "core_reasoning",
                        "capability": "板块方向识别+标的提取",
                        "backend_name": "t0",
                        "output_focus": "sector_direction_stock_candidates",
                    },
                    {
                        "slot": "cross_view_risk",
                        "capability": "跨cluster矛盾检测+盲区检查",
                        "backend_name": "t1",
                        "output_focus": "conflicts_blind_spots",
                    },
                    {
                        "slot": "boundary_schema_guard",
                        "capability": "时间线一致性+source/schema边界",
                        "backend_name": "t1",
                        "output_focus": "temporal_coherence_boundary",
                    },
                    {
                        "slot": "independent_strong_reasoning",
                        "capability": "独立视角+深度推理",
                        "backend_name": "t1",
                        "output_focus": "independent_deep_analysis",
                    },
                ],
            },
        )

        result = self._moa.deliberate(request)

        if result.status == "ok" and result.final:
            return self._moa_result_to_report(
                result, analyses, article_ids, cluster_ids, time_range
            )

        # MoA returned fallback
        logger.warning(
            "MoA returned status=%s, fallback_reason=%s", result.status, result.fallback_reason
        )
        return self._fallback_stale_or_raw(
            analyses,
            result.fallback_reason or f"moa_status={result.status}",
            time_range,
        )

    def _moa_result_to_report(
        self,
        result: MoAResult,
        analyses: list[ClusterAnalysis],
        article_ids: list[str],
        cluster_ids: list[str],
        time_range: dict[str, str] | None,
    ) -> SynthesisReport:
        """Convert MoAResult to SynthesisReport."""
        now = datetime.now(UTC).isoformat()
        final = result.final

        prev = self._store.load_latest_synthesis()
        prev_id = prev.synthesis_id if prev else ""

        sectors = list(final.get("sector_directions", []))
        for s in sectors:
            with contextlib.suppress(ValueError):
                validate_no_trade_fields(s)  # strip silently

        stocks = list(final.get("focused_stocks", []))
        for s in stocks:
            with contextlib.suppress(ValueError):
                validate_no_trade_fields(s)

        changes = list(final.get("viewpoint_changes", []))
        for c in changes:
            if not c.get("source_clusters"):
                c["source_clusters"] = cluster_ids
            if not c.get("source_article_ids"):
                c["source_article_ids"] = article_ids

        syn_id = f"syn-{now[:19].replace(':', '')}"
        queries = build_suggested_signal_queries(focused_stocks=stocks)

        report = SynthesisReport(
            synthesis_id=syn_id,
            generated_at=now,
            source_article_ids=article_ids,
            source_cluster_ids=cluster_ids,
            sector_directions=sectors,
            focused_stocks=stocks,
            viewpoint_changes=changes,
            quality_flags=QualityFlags(),
            confidence=clamp_float(result.confidence, 0.0, 1.0),
            time_range=time_range or {},
            risks_and_blind_spots=list(final.get("risks_and_blind_spots", [])),
            cross_cluster_contradictions=list(final.get("cross_cluster_contradictions", [])),
            previous_synthesis_id=prev_id,
            suggested_signal_queries=queries,
        )
        self._store.save_synthesis(report)
        return report

    # ── Single aggregator fallback ────────────────────────────────────────

    def _synthesize_with_single(
        self,
        analyses: list[ClusterAnalysis],
        analyses_text: str,
        time_range: dict[str, str] | None,
    ) -> SynthesisReport:
        """Single aggregator path when MoA engine is unavailable."""
        assert self._aggregator is not None
        prompt = _AGGREGATOR_PROMPT.format(
            analyses_text=analyses_text[:8000],
        )
        raw = self._aggregator.complete(prompt)
        result = self._parse_result(raw)
        if result is None:
            raise ValueError("aggregator returned invalid JSON")

        article_ids, cluster_ids = self._collect_ids(analyses)
        report = self._build_report_from_dict(
            result, analyses, article_ids, cluster_ids, time_range
        )
        self._store.save_synthesis(report)
        return report

    # ── Fallback ──────────────────────────────────────────────────────────

    def _fallback_stale_or_raw(
        self,
        analyses: list[ClusterAnalysis],
        error: str,
        time_range: dict[str, str] | None,
    ) -> SynthesisReport:
        """Try previous synthesis, then raw analyses summary."""
        prev = self._store.load_latest_synthesis()
        if prev is not None:
            return SynthesisReport(
                synthesis_id=prev.synthesis_id,
                generated_at=datetime.now(UTC).isoformat(),
                source_article_ids=prev.source_article_ids,
                source_cluster_ids=prev.source_cluster_ids,
                sector_directions=prev.sector_directions,
                focused_stocks=prev.focused_stocks,
                viewpoint_changes=prev.viewpoint_changes,
                quality_flags=QualityFlags(stale=True, fallback=True),
                confidence=max(0.0, prev.confidence - 0.1),
                time_range=time_range or {},
                risks_and_blind_spots=prev.risks_and_blind_spots,
                previous_synthesis_id=prev.synthesis_id,
            )
        return self._fallback_from_analyses(analyses, error, time_range)

    def _fallback_from_analyses(
        self,
        analyses: list[ClusterAnalysis],
        error: str | None,
        time_range: dict[str, str] | None,
    ) -> SynthesisReport:
        now = datetime.now(UTC).isoformat()
        article_ids, cluster_ids = self._collect_ids(analyses)
        all_stocks: list[dict[str, Any]] = []
        for a in analyses:
            for s in a.mentioned_stocks:
                all_stocks.append(s)

        return SynthesisReport(
            synthesis_id=f"syn-fallback-{now[:19].replace(':', '')}",
            generated_at=now,
            source_article_ids=article_ids,
            source_cluster_ids=cluster_ids,
            sector_directions=[],
            focused_stocks=[
                {
                    "company": s.get("company", "unknown"),
                    "ticker": "",
                    "reference_type": s.get("reference_type", "inferred_from_logic"),
                    "derivation_chain": "Phase 2 fallback",
                    "source_clusters": cluster_ids,
                    "source_article_ids": s.get("source_articles", article_ids),
                    "confidence": min(s.get("confidence", 0.4), 0.5),
                }
                for s in all_stocks[:10]
            ],
            viewpoint_changes=[],
            quality_flags=QualityFlags(fallback=True, partial=True),
            confidence=0.2,
            time_range=time_range or {},
            risks_and_blind_spots=[
                {"type": "degradation", "description": f"MoA failed: {error}", "severity": "high"}
            ],
        )

    def _empty_synthesis(self, article_ids: list[str], cluster_ids: list[str]) -> SynthesisReport:
        now = datetime.now(UTC).isoformat()
        return SynthesisReport(
            synthesis_id=f"syn-empty-{now[:19].replace(':', '')}",
            generated_at=now,
            source_article_ids=article_ids,
            source_cluster_ids=cluster_ids,
            sector_directions=[],
            focused_stocks=[],
            viewpoint_changes=[],
            quality_flags=QualityFlags(partial=True),
            confidence=0.0,
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _build_report_from_dict(
        self,
        result: dict[str, Any],
        analyses: list[ClusterAnalysis],
        article_ids: list[str],
        cluster_ids: list[str],
        time_range: dict[str, str] | None,
    ) -> SynthesisReport:
        now = datetime.now(UTC).isoformat()
        prev = self._store.load_latest_synthesis()
        prev_id = prev.synthesis_id if prev else ""

        sectors = list(result.get("sector_directions", []))
        for s in sectors:
            with contextlib.suppress(ValueError):
                validate_no_trade_fields(s)

        stocks = list(result.get("focused_stocks", []))
        for s in stocks:
            with contextlib.suppress(ValueError):
                validate_no_trade_fields(s)

        changes = list(result.get("viewpoint_changes", []))
        for c in changes:
            if not c.get("source_clusters"):
                c["source_clusters"] = cluster_ids
            if not c.get("source_article_ids"):
                c["source_article_ids"] = article_ids

        queries = build_suggested_signal_queries(focused_stocks=stocks)
        return SynthesisReport(
            synthesis_id=f"syn-{now[:19].replace(':', '')}",
            generated_at=now,
            source_article_ids=article_ids,
            source_cluster_ids=cluster_ids,
            sector_directions=sectors,
            focused_stocks=stocks,
            viewpoint_changes=changes,
            quality_flags=QualityFlags(),
            confidence=clamp_float(result.get("confidence", 0.5), 0.0, 1.0),
            time_range=time_range or {},
            risks_and_blind_spots=list(result.get("risks_and_blind_spots", [])),
            cross_cluster_contradictions=list(result.get("cross_cluster_contradictions", [])),
            previous_synthesis_id=prev_id,
            suggested_signal_queries=queries,
        )

    @staticmethod
    def _collect_ids(analyses: list[ClusterAnalysis]) -> tuple[list[str], list[str]]:
        article_ids: list[str] = []
        cluster_ids: list[str] = []
        for a in analyses:
            cluster_ids.append(a.cluster_id)
            for aid in a.article_ids:
                if aid not in article_ids:
                    article_ids.append(aid)
        return article_ids, cluster_ids

    @staticmethod
    def _format_analyses(analyses: list[ClusterAnalysis]) -> str:
        parts: list[str] = []
        for a in analyses:
            parts.append(
                f"### Cluster: {a.cluster_id}\n"
                f"Articles: {', '.join(a.article_ids)}\n"
                f"Quality: {a.quality_mode}\n"
                f"Evidence: {json.dumps(a.evidence_sufficiency, ensure_ascii=False)}\n"
                f"Viewpoints: {json.dumps(a.core_viewpoints[:5], ensure_ascii=False)}\n"
                f"Stocks: {json.dumps(a.mentioned_stocks[:5], ensure_ascii=False)}\n"
            )
        return "\n\n".join(parts)[:12000]

    @staticmethod
    def _parse_result(raw: str) -> dict[str, Any] | None:
        text = raw.strip()
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            text = text[brace_start : brace_end + 1]
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        return data
