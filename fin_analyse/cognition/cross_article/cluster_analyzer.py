"""Phase 2: Cluster analysis with evidence sufficiency and MoA decision matrix.

Analyzes articles within a cluster to extract viewpoints, stock mentions,
viewpoint evolution, and evidence sufficiency. MoA is triggered for
clusters with ≥2 articles or single high-value articles.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fin_analyse.cognition.cross_article.model_policy import CrossArticleModelPolicy
from fin_analyse.cognition.cross_article.models import ArticleRef, ClusterAnalysis
from fin_analyse.cognition.cross_article.synthesis_store import SynthesisStore

logger = logging.getLogger(__name__)

# ── Phase 2 single-model analysis prompt ────────────────────────────────────

_ANALYSIS_PROMPT = """你是星大派文章分析器。以下是同一主题的多篇文章，请分析：

文章列表：
{articles_text}

请输出一个 JSON object，字段如下：
{{
  "core_viewpoints": [
    {{
      "claim": "核心观点",
      "claim_type": "direct_expression / methodology_transfer / inferred_from_logic",
      "confidence": 0.0-1.0,
      "source_articles": ["article_id"],
      "key_quotes": ["原文关键句"],
      "evolution": "新观点 / 强化 / 修正 / 弱化"
    }}
  ],
  "mentioned_stocks": [
    {{
      "company": "公司名",
      "reference_type": "direct_mention / inferred_from_logic",
      "confidence": 0.0-1.0,
      "source_articles": ["article_id"]
    }}
  ],
  "viewpoint_evolution": {{
    "trend": "持续强化 / 新观点 / 修正 / 一致 / 不足",
    "timeline": []
  }},
  "evidence_sufficiency": {{
    "sufficient": true/false,
    "reason": "判断理由",
    "allowed_uses": ["focused_stock", "sector_direction", "observation_only"],
    "confidence_cap": 0.0-1.0
  }},
  "contradictions": [],
  "half_life_assessment": {{"category": "short / medium / long"}},
  "cross_cluster_links": []
}}

分析要点：
1. 核心观点：星大派表达了什么核心判断？
2. 观点演变：时间线上观点是否变化？
3. 标的指向：直接点名还是行业逻辑推导？
4. 确定性与条件：语气是确定还是观察？
5. 时效性：观点的半衰期（短/中/长）
6. 关键引用：支持每个判断的原文关键句

如果只有一篇短文或证据不充分，设置 evidence_sufficiency.sufficient=false，
allowed_uses=["observation_only"]，confidence_cap=0.45。

只输出 JSON，不要加额外文字。"""


class ClusterAnalyzer:
    """Phase 2 cluster analysis with LLM + optional MoA verification.

    Decision matrix:
    - 0 articles → empty insufficient analysis
    - 1 article, weak → single_model + observation_only
    - 1 article, strong → single_model (MoA optional in future)
    - ≥2 articles → single_model (MoA optional in future via moa_engine injection)
    """

    def __init__(
        self,
        store: SynthesisStore,
        policy: CrossArticleModelPolicy,
        moa_engine: Any | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._moa = moa_engine

    # ── public ───────────────────────────────────────────────────────────

    def analyze_cluster(
        self,
        *,
        cluster_id: str,
        articles: list[ArticleRef],
    ) -> ClusterAnalysis:
        """Analyze articles within a cluster. Persists result via store."""

        # Empty cluster → insufficient
        if not articles:
            return self._insufficient_analysis(cluster_id, [], "no articles in cluster")

        # Single article → check evidence strength
        if len(articles) == 1:
            return self._analyze_single_article(cluster_id, articles[0])

        # ≥2 articles → full analysis
        return self._analyze_multi_article(cluster_id, articles)

    # ── internal ─────────────────────────────────────────────────────────

    def _analyze_single_article(self, cluster_id: str, article: ArticleRef) -> ClusterAnalysis:
        """Single article: run analysis but respect evidence limits."""
        content = article.content_excerpt or article.title
        prompt = _ANALYSIS_PROMPT.format(
            articles_text=f"- [{article.article_id}] {article.title}\n  {content[:2000]}"
        )

        result = self._policy.run_phase2_single_model(prompt)
        if result is None:
            return self._insufficient_analysis(
                cluster_id, [article.article_id], "LLM analysis failed"
            )

        analysis = self._build_analysis(cluster_id, [article.article_id], result, "single_model")
        self._store.save_analysis(analysis)
        return analysis

    def _analyze_multi_article(
        self, cluster_id: str, articles: list[ArticleRef]
    ) -> ClusterAnalysis:
        """Multi-article: full single-model analysis (MoA gate reserved)."""
        articles_text = "\n".join(
            f"- [{a.article_id}] {a.published_at} {a.title}\n  {a.content_excerpt[:1500]}"
            for a in articles
        )
        prompt = _ANALYSIS_PROMPT.format(articles_text=articles_text[:8000])

        result = self._policy.run_phase2_single_model(prompt)
        if result is None:
            return self._insufficient_analysis(
                cluster_id,
                [a.article_id for a in articles],
                "LLM analysis failed",
            )

        analysis = self._build_analysis(
            cluster_id,
            [a.article_id for a in articles],
            result,
            "single_model",
        )
        self._store.save_analysis(analysis)
        return analysis

    def _build_analysis(
        self,
        cluster_id: str,
        article_ids: list[str],
        result: dict[str, Any],
        quality_mode: str,
    ) -> ClusterAnalysis:
        now = datetime.now(UTC).isoformat()
        analysis_id = f"ca-{cluster_id}-{now[:10]}"

        return ClusterAnalysis(
            analysis_id=analysis_id,
            cluster_id=cluster_id,
            generated_at=now,
            article_ids=article_ids,
            core_viewpoints=list(result.get("core_viewpoints", [])),
            mentioned_stocks=list(result.get("mentioned_stocks", [])),
            evidence_sufficiency=result.get(
                "evidence_sufficiency",
                {
                    "sufficient": True,
                    "reason": "ok",
                    "allowed_uses": ["focused_stock"],
                    "confidence_cap": 0.85,
                },
            ),
            quality_mode=quality_mode,
            viewpoint_evolution=result.get("viewpoint_evolution", {}),
            contradictions=list(result.get("contradictions", [])),
            half_life_assessment=result.get("half_life_assessment", {}),
            cross_cluster_links=list(result.get("cross_cluster_links", [])),
        )

    @staticmethod
    def _insufficient_analysis(
        cluster_id: str,
        article_ids: list[str],
        reason: str,
    ) -> ClusterAnalysis:
        now = datetime.now(UTC).isoformat()
        return ClusterAnalysis(
            analysis_id=f"ca-{cluster_id}-insufficient",
            cluster_id=cluster_id,
            generated_at=now,
            article_ids=article_ids,
            core_viewpoints=[],
            mentioned_stocks=[],
            evidence_sufficiency={
                "sufficient": False,
                "reason": reason,
                "allowed_uses": ["observation_only"],
                "confidence_cap": 0.45,
            },
            quality_mode="single_model",
        )
