"""Phase 1: LLM-driven incremental article clustering.

Takes persona-eligible ArticleRefs (already filtered by source_selector)
and assigns each to an existing or new cluster via LLM fingerprint extraction.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fin_analyse.cognition.cross_article.model_policy import CrossArticleModelPolicy
from fin_analyse.cognition.cross_article.models import ArticleRef, ClusterInfo
from fin_analyse.cognition.cross_article.synthesis_store import SynthesisStore

logger = logging.getLogger(__name__)


class ArticleClusterer:
    """Phase 1 incremental clusterer with idempotent article assignment.

    Each article is processed exactly once (article_id → cluster_id mapping).
    LLM extracts a topic fingerprint and suggests cluster membership.
    """

    def __init__(
        self,
        store: SynthesisStore,
        policy: CrossArticleModelPolicy,
    ) -> None:
        self._store = store
        self._policy = policy

    # ── public ───────────────────────────────────────────────────────────

    def cluster_article(self, article: ArticleRef) -> dict[str, Any]:
        """Assign article to cluster. Idempotent: returns existing if mapped."""
        # Store article metadata for Phase 2 rebuild
        self._store.save_article_meta(article.article_id, article.to_dict())

        # Idempotency check
        existing = self._store.get_article_cluster(article.article_id)
        if existing is not None:
            return {
                "action": "skipped",
                "cluster_id": existing,
                "article_id": article.article_id,
                "degraded": False,
            }

        # Build existing cluster list for LLM hint
        existing_clusters = self._store.load_all_centroids()

        # Extract fingerprint via model policy (T0 → T1 → text fallback)
        content = article.content_excerpt or article.title
        fingerprint = self._policy.extract_phase1_fingerprint(
            content, existing_clusters=existing_clusters
        )
        degraded = fingerprint.get("degraded", False)

        # Determine cluster assignment
        hint = fingerprint.get("cluster_hint", {})
        relation = hint.get("relation_to_existing", "新建 cluster")
        target_id = hint.get("target_cluster_id")

        if (
            relation == "属于已有 cluster"
            and target_id
            and not self._store.validate_cluster_id(str(target_id))
        ):
            logger.warning(
                "LLM returned unsafe target_cluster_id: %s, creating new cluster",
                target_id,
            )
            target_id = None
        if relation == "属于已有 cluster" and target_id:
            # Verify target exists
            existing_info = self._store.load_cluster_info(str(target_id))
            if existing_info is not None:
                # Add to existing cluster
                self._store.set_article_cluster(article.article_id, target_id)
                self._update_cluster_meta(target_id, article, fingerprint)
                return {
                    "action": "added",
                    "cluster_id": target_id,
                    "article_id": article.article_id,
                    "degraded": degraded,
                }
            # Target doesn't exist, fall through to create new

        # Create new cluster
        cluster_id = self._new_cluster_id(fingerprint, article.article_id)
        self._store.set_article_cluster(article.article_id, cluster_id)
        self._create_cluster(cluster_id, article, fingerprint)
        return {
            "action": "created",
            "cluster_id": cluster_id,
            "article_id": article.article_id,
            "degraded": degraded,
        }

    # ── helpers ──────────────────────────────────────────────────────────

    def _create_cluster(
        self,
        cluster_id: str,
        article: ArticleRef,
        fingerprint: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        info = ClusterInfo(
            cluster_id=cluster_id,
            theme=fingerprint.get("core_topic", article.title[:30]),
            created_at=now,
            updated_at=now,
            article_ids=[article.article_id],
            centroid_summary=fingerprint.get("core_topic", ""),
        )
        self._store.save_cluster_info(info)

    def _update_cluster_meta(
        self,
        cluster_id: str,
        article: ArticleRef,
        fingerprint: dict[str, Any],
    ) -> None:
        existing = self._store.load_cluster_info(cluster_id)
        if existing is None:
            return
        now = datetime.now(UTC).isoformat()
        article_ids = list(dict.fromkeys(existing.article_ids + [article.article_id]))
        updated = ClusterInfo(
            cluster_id=cluster_id,
            theme=fingerprint.get("core_topic", existing.theme),
            created_at=existing.created_at,
            updated_at=now,
            article_ids=article_ids,
            centroid_summary=fingerprint.get("core_topic", existing.centroid_summary),
        )
        self._store.save_cluster_info(updated)

    @staticmethod
    def _new_cluster_id(fingerprint: dict[str, Any], article_id: str) -> str:
        """Generate a stable cluster_id from topic + article_id."""
        topic = fingerprint.get("core_topic", "unknown")
        # Simple slug: remove special chars, limit length
        slug = "".join(c for c in topic if c.isalnum() or c in "_- ")[:20].strip()
        slug = slug.replace(" ", "_").lower() or "cluster"
        return f"cluster_{slug}_{article_id[-8:]}"
