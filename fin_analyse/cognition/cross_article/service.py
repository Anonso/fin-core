"""CrossArticleSynthesisService — the sole public deep module interface.

Callers never touch cluster files, MoA roles, cache keys, or versioning
directly. They only call ingest_articles() or get_synthesis().
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fin_analyse.cognition.cross_article.article_clusterer import ArticleClusterer
from fin_analyse.cognition.cross_article.cluster_analyzer import ClusterAnalyzer
from fin_analyse.cognition.cross_article.cross_synthesizer import (
    CrossClusterSynthesizer,
)
from fin_analyse.cognition.cross_article.model_policy import CrossArticleModelPolicy
from fin_analyse.cognition.cross_article.models import (
    ArticleRef,
    CrossArticleSynthesisResponse,
    DegradationEvent,
    IngestionResult,
    QualityFlags,
)
from fin_analyse.cognition.cross_article.source_selector import (
    CrossArticleSourceSelector,
)
from fin_analyse.cognition.cross_article.synthesis_store import SynthesisStore
from fin_analyse.utils.ids import stable_id

logger = logging.getLogger(__name__)


class CrossArticleSynthesisService:
    """Public interface for cross-article cognitive synthesis.

    Inject backends and optional MoA engine; the service wires together
    Phase 1 (clustering), Phase 2 (analysis), and Phase 3 (synthesis).

    Invariants:
    - ingest_articles() never throws; failures become IngestionResult fields.
    - get_synthesis() never throws; failures return stale/fallback.
    - All writes go through SynthesisStore.
    - Persona/pattern/trace stores are never written.
    """

    def __init__(
        self,
        store: SynthesisStore | None = None,
        t0_backend: Any | None = None,
        t1_backend: Any | None = None,
        moa_engine: Any | None = None,
    ) -> None:
        self._store = store or SynthesisStore()
        self._policy = CrossArticleModelPolicy(t0_backend=t0_backend, t1_backend=t1_backend)
        self._moa = moa_engine
        self._selector = CrossArticleSourceSelector()
        self._clusterer = ArticleClusterer(store=self._store, policy=self._policy)
        self._analyzer = ClusterAnalyzer(
            store=self._store,
            policy=self._policy,
            moa_engine=moa_engine,
        )

        # Build MoA reference backends for Phase 3
        reference_backends: dict[str, Any] = {}
        if t0_backend is not None:
            reference_backends["t0"] = t0_backend
        if t1_backend is not None:
            reference_backends["t1"] = t1_backend

        # Create MoA engine if not injected and we have backends
        if self._moa is None and t0_backend is not None and len(reference_backends) >= 2:
            from fin_analyse.moa.engine import MoAEngine

            self._moa = MoAEngine(
                aggregator_backend=t0_backend,
                aggregator_backend_name="t0",
                reference_backends=reference_backends,
            )

        self._synthesizer = CrossClusterSynthesizer(
            store=self._store,
            aggregator_backend=t0_backend,
            moa_engine=self._moa,
            reference_backends=reference_backends,
        )

    # ── public API ───────────────────────────────────────────────────────

    def ingest_articles(self, articles: list[ArticleRef]) -> IngestionResult:
        """Best-effort article ingestion. Never blocks scraper success.

        Only persona-eligible articles are processed. Non-eligible articles
        are counted as skipped.
        """
        processed = 0
        skipped = 0
        degraded = 0
        skip_reasons: list[dict[str, str]] = []
        warnings: list[str] = []

        for article in articles:
            try:
                result = self._clusterer.cluster_article(article)
                action = result.get("action", "unknown")
                if action == "skipped":
                    skipped += 1
                    continue
                if result.get("degraded"):
                    degraded += 1
                    warnings.append(f"degraded clustering for {article.article_id}")
                processed += 1
            except Exception as exc:
                skipped += 1
                skip_reasons.append(
                    {
                        "article_id": article.article_id,
                        "reason": f"ingestion_error: {exc}",
                    }
                )
                logger.warning("Ingest failed for %s: %s", article.article_id, exc)

        # After ingestion, run Phase 2 for clusters with new articles
        try:
            self._run_phase2_for_dirty_clusters()
        except Exception as exc:
            warnings.append(f"Phase 2 post-ingest failed: {exc}")

        return IngestionResult(
            processed=processed,
            skipped=skipped,
            degraded=degraded,
            skip_reasons=skip_reasons,
            warnings=warnings,
        )

    def get_synthesis(
        self,
        *,
        topic: str | None = None,
        since: str | None = None,
        cluster_ids: list[str] | None = None,
        include_analyses: bool = False,
    ) -> CrossArticleSynthesisResponse:
        """Return latest synthesis (cached or freshly generated)."""
        warnings: list[str] = []

        # Load all clusters
        clusters = self._store.list_clusters()
        if not clusters:
            return CrossArticleSynthesisResponse(
                synthesis=None,
                clusters=[],
                previous_synthesis_id=None,
                generated_at=datetime.now(UTC).isoformat(),
                quality_flags=QualityFlags(partial=True).to_dict(),
                warnings=["NO_CROSS_ARTICLE_DATA"],
            )

        # Filter clusters
        if cluster_ids:
            cid_set = set(cluster_ids)
            clusters = [c for c in clusters if c.cluster_id in cid_set]
        if topic:
            topic_lower = topic.lower()
            clusters = [
                c
                for c in clusters
                if topic_lower in c.theme.lower() or topic_lower in c.centroid_summary.lower()
            ]

        # ── since date filtering ──
        since_gaps: list[str] = []
        if since:
            clusters, since_gaps = self._filter_clusters_by_since(clusters, since, report_gaps=True)
            warnings.extend(since_gaps)

        if not clusters:
            return CrossArticleSynthesisResponse(
                synthesis=None,
                clusters=[],
                previous_synthesis_id=None,
                generated_at=datetime.now(UTC).isoformat(),
                quality_flags=QualityFlags(partial=True).to_dict(),
                warnings=warnings
                or [f"no clusters match topic={topic}" if topic else "no clusters available"],
            )

        # Load analyses for each cluster
        analyses = []
        for c in clusters:
            analysis = self._store.load_latest_analysis(c.cluster_id)
            if analysis is not None:
                analyses.append(analysis)

        if not analyses:
            return CrossArticleSynthesisResponse(
                synthesis=None,
                clusters=[c.to_dict() for c in clusters],
                previous_synthesis_id=None,
                generated_at=datetime.now(UTC).isoformat(),
                quality_flags=QualityFlags(partial=True).to_dict(),
                warnings=["no cluster analyses available; run Phase 2 first"],
            )

        # Check state-hash cache
        state = {
            "topic": topic,
            "since": since,
            "cluster_ids": sorted(c.cluster_id for c in clusters),
            "analysis_ids": sorted(a.analysis_id for a in analyses),
        }
        cached_sid = self._store.cache_get(state)

        prev = self._store.load_latest_synthesis()
        quality = QualityFlags()

        if cached_sid and prev and prev.synthesis_id == cached_sid:
            quality.cache_hit = True
            result_syn: dict[str, Any] | None = prev.to_dict()
        else:
            # Generate Phase 3 synthesis
            try:
                report = self._synthesizer.synthesize(analyses=analyses)
                self._store.cache_set(state, report.synthesis_id)
                quality = report.quality_flags
                result_syn = report.to_dict()
            except Exception as exc:
                logger.warning("Phase 3 synthesis failed: %s", exc)
                self._write_degradation_event("synthesis_error", state)
                # Try stale
                if prev is not None:
                    quality = QualityFlags(stale=True, fallback=True)
                    result_syn = prev.to_dict()
                else:
                    result_syn = None
                    quality = QualityFlags(fallback=True, partial=True)
                    warnings.append(f"synthesis failed: {exc}")

        response_clusters = [c.to_dict() for c in clusters]

        # Optionally include Phase 2 analyses
        if include_analyses and result_syn is not None:
            analysis_dicts = [a.to_dict() for a in analyses]
            if result_syn:
                result_syn = dict(result_syn)
                result_syn["_cluster_analyses"] = analysis_dicts

        return CrossArticleSynthesisResponse(
            synthesis=result_syn,
            clusters=response_clusters,
            previous_synthesis_id=prev.synthesis_id if prev else None,
            generated_at=datetime.now(UTC).isoformat(),
            quality_flags=quality.to_dict(),
            warnings=warnings,
        )

    # ── internal ─────────────────────────────────────────────────────────

    def _run_phase2_for_dirty_clusters(self) -> None:
        """Run Phase 2 analysis for clusters with un-analyzed articles."""
        for c in self._store.list_clusters():
            latest = self._store.load_latest_analysis(c.cluster_id)
            if latest is not None and set(latest.article_ids) == set(c.article_ids):
                continue  # already analyzed with same articles

            # Rebuild ArticleRefs from stored article metadata
            articles = self._load_articles_for_cluster(c.article_ids)
            if not articles:
                logger.warning("No article metadata for cluster %s, skipping Phase 2", c.cluster_id)
                continue
            try:
                self._analyzer.analyze_cluster(cluster_id=c.cluster_id, articles=articles)
            except Exception as exc:
                logger.warning("Phase 2 failed for cluster %s: %s", c.cluster_id, exc)

    def _load_articles_for_cluster(self, article_ids: list[str]) -> list[ArticleRef]:
        """Load ArticleRefs from stored metadata or knowledge-base index."""
        articles: list[ArticleRef] = []
        for aid in article_ids:
            # Try stored metadata first
            meta = self._store.load_article_meta(aid)
            if meta is not None:
                try:
                    articles.append(ArticleRef.from_dict(meta))
                    continue
                except Exception:
                    pass
            # Fallback: try to read from knowledge-base index
            ref = self._load_article_from_index(aid)
            if ref is not None:
                articles.append(ref)
        return articles

    @staticmethod
    def _load_article_from_index(article_id: str) -> ArticleRef | None:
        """Try to build an ArticleRef from knowledge-base/index.json."""
        try:
            import json
            from pathlib import Path

            from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

            kb_root = default_knowledge_base_root()
            index_path = kb_root / "index.json"
            if not index_path.exists():
                return None
            data = json.loads(index_path.read_text())
            items = data if isinstance(data, list) else data.get("articles", [])
            for item in items:
                if str(item.get("id", "")) == article_id:
                    path = item.get("path", "") or str(
                        kb_root / "articles" / f"{article_id}.md"
                    )
                    p = Path(path)
                    content = ""
                    if p.exists():
                        content = p.read_text()[:2000]
                    return ArticleRef(
                        article_id=article_id,
                        title=str(item.get("title", "")),
                        published_at=str(item.get("date", "")),
                        column=str(item.get("column", "")),
                        path=str(p),
                        source_classification="teacher_original",
                        persona_eligible=True,
                        content_excerpt=content,
                        metadata={"score": item.get("score")},
                    )
        except Exception:
            pass
        return None

    def _write_degradation_event(self, reason: str, state: dict[str, Any]) -> None:
        """Write degradation event if not already deduped recently."""
        if self._store.has_recent_degradation(
            fallback_reason=reason, cache_key=str(hash(str(state)))
        ):
            return
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        event = DegradationEvent(
            event_id=stable_id("ca-deg", reason, now, prefix="ca-deg-"),
            created_at=now,
            fallback_reason=reason,
            cache_key=str(hash(str(state))),
        )
        self._store.append_degradation_event(event)

    # ── since date filtering ──────────────────────────────────────────────

    def _filter_clusters_by_since(
        self,
        clusters: list[Any],
        since: str,
        *,
        report_gaps: bool = False,
    ) -> tuple[list[Any], list[str]]:
        """Filter clusters to those with at least one article published >= since.

        Returns (filtered_clusters, gaps).
        Clusters are `ClusterInfo` objects (primary path) or dicts (test path).
        """
        gaps: list[str] = []
        filtered: list[Any] = []

        for c in clusters:
            # Support both ClusterInfo objects and dicts (tests)
            article_ids: list[str] = (
                c.article_ids if hasattr(c, "article_ids") else list(c.get("article_ids", []))
            )

            if not article_ids:
                filtered.append(c)  # empty cluster — include, don't filter silently
                if report_gaps:
                    cid = getattr(c, "cluster_id", c.get("cluster_id", "?"))
                    gaps.append(f"cluster_{cid}_no_articles")
                continue

            # Try to get article dates
            article_dates: dict[str, str] = {}
            has_date_metadata = False
            for aid in article_ids:
                pub = self._try_get_article_published_at(c, aid)
                if pub:
                    article_dates[aid] = pub
                    has_date_metadata = True

            if not has_date_metadata:
                # No date info available — include cluster conservatively but report gap
                filtered.append(c)
                if report_gaps:
                    cid = getattr(c, "cluster_id", c.get("cluster_id", "?"))
                    gaps.append(
                        f"since_filter_unavailable: cluster {cid} "
                        f"has no article published_at metadata"
                    )
                continue

            if self._any_article_after_since(article_dates, since):
                filtered.append(c)

        if not filtered and not gaps:
            gaps.append("since_filter_excluded_all_clusters")

        return filtered, gaps

    @staticmethod
    def _any_article_after_since(article_dates: dict[str, str], since: str) -> bool:
        """Return True if any article in the dict has published_at >= since."""
        return any(published_at >= since for published_at in article_dates.values())

    def _try_get_article_published_at(self, cluster: Any, article_id: str) -> str:
        """Extract published_at for an article from cluster metadata or store.

        For ClusterInfo objects, reads article_meta from the store.
        For dict-based clusters (tests), checks centroid_published_at.
        """
        # Dict-based (test) path: use centroid_published_at as proxy
        if isinstance(cluster, dict):
            return str(cluster.get("centroid_published_at", ""))

        # ClusterInfo object: try store's article_meta
        meta = self._store.load_article_meta(article_id)
        if meta:
            return str(meta.get("published_at", ""))
        return ""


def ingest_eligible_from_index(
    index_path: str | None = None,
    *,
    t0_backend=None,
    t1_backend=None,
    dry_run: bool = False,
) -> dict:
    """从 index.json 扫描星大派文章并批量 ingest 到跨文章综合管道。

    入选规则：
    - 星大派特刊 / 星大派锐评 → 直接入选
    - 星大派好问题 / 好问题 / 问题回答 / 回答问题 → LLM 判定

    Returns: {eligible, ingested, skipped, qa_skipped, warnings}
    """
    import json
    from pathlib import Path

    from fin_analyse.cognition.cross_article.models import ArticleRef
    from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

    index_p = (
        Path(index_path)
        if index_path is not None
        else default_knowledge_base_root() / "index.json"
    )
    if not index_p.exists():
        return {
            "eligible": 0,
            "ingested": 0,
            "skipped": 0,
            "qa_skipped": 0,
            "warnings": ["no_index"],
        }

    data = json.loads(index_p.read_text())
    all_articles = data if isinstance(data, list) else data.get("articles", [])

    xdp_columns = {"星大派特刊", "星大派锐评"}
    qa_columns = {"星大派好问题", "好问题", "问题回答", "回答问题"}

    kb_base = index_p.parent
    all_with_path = []
    for a in all_articles:
        raw_path = a.get("path", "")
        p = (
            Path(raw_path).resolve()
            if raw_path
            else (kb_base / "articles" / f"{a.get('id', '')}.md").resolve()
        )
        try:
            p.relative_to(kb_base)
        except ValueError:
            continue
        if p.exists():
            a["_path"] = str(p)
            all_with_path.append(a)

    # Lazy-load backends if not provided
    if t0_backend is None:
        from fin_analyse.claims.config_loader import create_backends_from_config

        backends = create_backends_from_config()
        t0_name = "glm53" if backends.get("glm53") is not None else "deepseek"
        t0_backend = backends.get(t0_name)
        t1_order = ("deepseek", "qwen") if t0_name == "glm53" else ("glm53", "qwen")
        t1_backend = next((backends[name] for name in t1_order if name in backends), t0_backend)

    eligible = []
    qa_skipped = 0

    # Collect candidates first — LLM judge is expensive, only run if not dry_run
    xdp_candidates = []
    qa_candidates = []
    for a in all_with_path:
        col = str(a.get("column", ""))
        aid = str(a.get("id", ""))
        p = Path(a["_path"])
        content = p.read_text()[:2000]

        if col in xdp_columns:
            xdp_candidates.append(
                ArticleRef(
                    article_id=aid,
                    title=str(a.get("title", "")),
                    published_at=str(a.get("date", "")),
                    column=col,
                    path=str(p),
                    source_classification="teacher_original",
                    persona_eligible=True,
                    content_excerpt=content,
                    metadata={"score": a.get("score")},
                )
            )
        elif col in qa_columns:
            qa_candidates.append(a)

    eligible = list(xdp_candidates)

    if not dry_run:
        from fin_analyse.cognition.cross_article.good_question_judge import LlmGoodQuestionJudge

        judge = LlmGoodQuestionJudge(backend=t0_backend)
        for a in qa_candidates:
            p = Path(a["_path"])
            content = p.read_text()[:2000]
            if judge(a):
                eligible.append(
                    ArticleRef(
                        article_id=str(a.get("id", "")),
                        title=str(a.get("title", "")),
                        published_at=str(a.get("date", "")),
                        column=str(a.get("column", "")),
                        path=str(p),
                        source_classification="teacher_original",
                        persona_eligible=True,
                        content_excerpt=content,
                        metadata={"score": a.get("score"), "persona_gate": "llm_judge"},
                    )
                )
            else:
                qa_skipped += 1
    else:
        # dry_run: count all QA candidates as "would be judged"
        qa_skipped = 0  # not actually skipped, just not judged yet

    if dry_run:
        return {
            "eligible": len(eligible),
            "ingested": 0,
            "skipped": 0,
            "qa_skipped": qa_skipped,
            "warnings": [],
        }

    if not eligible:
        return {
            "eligible": 0,
            "ingested": 0,
            "skipped": 0,
            "qa_skipped": qa_skipped,
            "warnings": [],
        }

    svc = CrossArticleSynthesisService(t0_backend=t0_backend, t1_backend=t1_backend)
    result = svc.ingest_articles(eligible)

    return {
        "eligible": len(eligible),
        "ingested": result.processed,
        "skipped": result.skipped,
        "qa_skipped": qa_skipped,
        "warnings": result.warnings,
    }
