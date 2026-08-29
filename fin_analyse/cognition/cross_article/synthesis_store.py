"""SynthesisStore — append-only/versioned file storage for cross_article runtime.

This is the ONLY module that writes to knowledge-base/runtime/cross_article/.
All other cross_article modules return pure objects; the store persists them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fin_analyse.cognition.cross_article.models import (
    ClusterAnalysis,
    ClusterInfo,
    DegradationEvent,
    SynthesisReport,
)

logger = logging.getLogger(__name__)


def _default_root() -> Path:
    """Resolve the store root via the production knowledge-root seam."""
    from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

    return default_knowledge_base_root() / "runtime" / "cross_article"


class SynthesisStore:
    """Append-only/versioned store for cross_article runtime state.

    File layout:
        clusters/{cluster_id}/meta.json
        clusters/{cluster_id}/centroid.json
        clusters/{cluster_id}/latest_analysis.json  (pointer)
        clusters/{cluster_id}/analyses/{analysis_id}.json
        syntheses/latest.json  (pointer)
        syntheses/{synthesis_id}.json
        article_cluster_map.json
        cache/state_hash_cache.json
        events/degradation_events.jsonl
        errors/{run_id}.json
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else _default_root()
        self._ensure_dirs()

    # ── cluster_id validation ────────────────────────────────────────────

    @staticmethod
    def validate_cluster_id(cluster_id: str) -> bool:
        """Reject cluster_ids containing path traversal or unsafe chars.

        Only alphanumerics, underscores, hyphens, and dots that aren't '..' are allowed.
        """
        if not cluster_id or not cluster_id.strip():
            return False
        if ".." in cluster_id:
            return False
        if "/" in cluster_id or "\\" in cluster_id:
            return False
        # Allow alphanumeric, Chinese, underscores, hyphens (safe for filenames)
        import re

        return bool(re.match(r"^[\w一-鿿_.\-]+$", cluster_id))

    # ── cluster info ─────────────────────────────────────────────────────

    def save_cluster_info(self, info: ClusterInfo) -> None:
        cluster_dir = self._cluster_dir(info.cluster_id)
        meta = info.to_dict()
        self._atomic_write(cluster_dir / "meta.json", meta)

        # Also write centroid
        centroid = {
            "cluster_id": info.cluster_id,
            "theme": info.theme,
            "summary": info.centroid_summary,
            "updated_at": info.updated_at,
        }
        self._atomic_write(cluster_dir / "centroid.json", centroid)

    def load_cluster_info(self, cluster_id: str) -> ClusterInfo | None:
        path = self._cluster_dir(cluster_id) / "meta.json"
        if not path.exists():
            return None
        return ClusterInfo.from_dict(self._read_json(path))

    def list_clusters(self) -> list[ClusterInfo]:
        clusters_dir = self.root / "clusters"
        if not clusters_dir.exists():
            return []
        result: list[ClusterInfo] = []
        for entry in sorted(clusters_dir.iterdir()):
            if entry.is_dir():
                meta = entry / "meta.json"
                if meta.exists():
                    result.append(ClusterInfo.from_dict(self._read_json(meta)))
        return result

    def load_all_centroids(self) -> list[dict[str, Any]]:
        """Return all cluster centroids for Phase 1 matching."""
        centroids: list[dict[str, Any]] = []
        clusters_dir = self.root / "clusters"
        if not clusters_dir.exists():
            return centroids
        for entry in sorted(clusters_dir.iterdir()):
            if entry.is_dir():
                centroid_path = entry / "centroid.json"
                if centroid_path.exists():
                    centroids.append(self._read_json(centroid_path))
        return centroids

    # ── article → cluster mapping (idempotency) ──────────────────────────

    def set_article_cluster(self, article_id: str, cluster_id: str) -> None:
        mapping = self.load_article_cluster_map()
        if article_id in mapping:
            return  # idempotent: already mapped
        mapping[article_id] = cluster_id
        self._atomic_write(self.root / "article_cluster_map.json", mapping)

    def get_article_cluster(self, article_id: str) -> str | None:
        mapping = self.load_article_cluster_map()
        return mapping.get(article_id)

    def load_article_cluster_map(self) -> dict[str, str]:
        path = self.root / "article_cluster_map.json"
        if not path.exists():
            return {}
        data = self._read_json(path)
        return {str(k): str(v) for k, v in data.items()}

    # ── ClusterAnalysis versioning ───────────────────────────────────────

    def save_analysis(self, analysis: ClusterAnalysis) -> None:
        cluster_dir = self._cluster_dir(analysis.cluster_id)
        analyses_dir = cluster_dir / "analyses"
        analyses_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(analyses_dir / f"{analysis.analysis_id}.json", analysis.to_dict())
        # Update latest pointer
        self._atomic_write(
            cluster_dir / "latest_analysis.json",
            {
                "analysis_id": analysis.analysis_id,
                "generated_at": analysis.generated_at,
            },
        )

    def load_latest_analysis(self, cluster_id: str) -> ClusterAnalysis | None:
        cluster_dir = self._cluster_dir(cluster_id)
        pointer = cluster_dir / "latest_analysis.json"
        if not pointer.exists():
            return None
        ref = self._read_json(pointer)
        return self.load_analysis(cluster_id, ref.get("analysis_id", ""))

    def load_analysis(self, cluster_id: str, analysis_id: str) -> ClusterAnalysis | None:
        if not analysis_id:
            return None
        path = self._cluster_dir(cluster_id) / "analyses" / f"{analysis_id}.json"
        if not path.exists():
            return None
        return ClusterAnalysis.from_dict(self._read_json(path))

    # ── Synthesis versioning ─────────────────────────────────────────────

    def save_synthesis(self, synthesis: SynthesisReport) -> None:
        syn_dir = self.root / "syntheses"
        syn_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(syn_dir / f"{synthesis.synthesis_id}.json", synthesis.to_dict())
        # Update latest pointer
        self._atomic_write(
            syn_dir / "latest.json",
            {
                "synthesis_id": synthesis.synthesis_id,
                "generated_at": synthesis.generated_at,
                "previous_synthesis_id": synthesis.previous_synthesis_id,
            },
        )

    def load_latest_synthesis(self) -> SynthesisReport | None:
        pointer = self.root / "syntheses" / "latest.json"
        if not pointer.exists():
            return None
        ref = self._read_json(pointer)
        return self.load_synthesis(ref.get("synthesis_id", ""))

    def load_synthesis(self, synthesis_id: str) -> SynthesisReport | None:
        if not synthesis_id:
            return None
        path = self.root / "syntheses" / f"{synthesis_id}.json"
        if not path.exists():
            return None
        data = self._read_json(path)
        # Reject pointer-only / malformed synthesis (missing required fields)
        required = {
            "source_article_ids",
            "source_cluster_ids",
            "sector_directions",
            "focused_stocks",
            "viewpoint_changes",
            "quality_flags",
            "confidence",
        }
        if not required <= set(data):
            logger.warning(
                "Skipping malformed synthesis %s: missing %s",
                synthesis_id,
                sorted(required - set(data)),
            )
            return None
        try:
            return SynthesisReport.from_dict(data)
        except Exception as exc:
            logger.warning("Skipping malformed synthesis %s: %s", synthesis_id, exc)
            return None

    # ── state-hash cache ─────────────────────────────────────────────────

    def cache_get(self, state: dict[str, Any]) -> str | None:
        """Return cached synthesis_id for state hash, or None."""
        cache = self._load_cache()
        key = self._state_hash(state)
        return cache.get(key)

    def cache_set(self, state: dict[str, Any], synthesis_id: str) -> None:
        cache = self._load_cache()
        key = self._state_hash(state)
        cache[key] = synthesis_id
        cache_dir = self.root / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(cache_dir / "state_hash_cache.json", cache)

    # ── degradation events ───────────────────────────────────────────────

    def append_degradation_event(self, event: DegradationEvent) -> None:
        events_dir = self.root / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / "degradation_events.jsonl"
        line = event.to_jsonl() + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def list_degradation_events(
        self, *, since: str | None = None, limit: int = 100
    ) -> list[DegradationEvent]:
        path = self.root / "events" / "degradation_events.jsonl"
        if not path.exists():
            return []
        events: list[DegradationEvent] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = DegradationEvent.from_dict(json.loads(line))
                    if since and ev.created_at < since:
                        continue
                    events.append(ev)
                except Exception:
                    logger.debug("Skipping malformed degradation event line")
        return events[-limit:]

    def has_recent_degradation(
        self,
        *,
        fallback_reason: str,
        cache_key: str,
        window: str = "1d",
    ) -> bool:
        """Check if a matching degradation event exists within window."""
        # Simple window handling: "1d" = last 24 hours
        cutoff = ""
        if window == "1d":
            from datetime import timedelta

            cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        for ev in self.list_degradation_events(since=cutoff):
            if ev.fallback_reason == fallback_reason and ev.cache_key == cache_key:
                return True
        return False

    # ── article metadata (for Phase 2 rebuild) ──────────────────────────

    def save_article_meta(self, article_id: str, meta: dict[str, Any]) -> None:
        meta_dir = self.root / "article_meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(meta_dir / f"{article_id}.json", meta)

    def load_article_meta(self, article_id: str) -> dict[str, Any] | None:
        path = self.root / "article_meta" / f"{article_id}.json"
        if not path.exists():
            return None
        return self._read_json(path)

    # ── error log ────────────────────────────────────────────────────────

    def save_error(self, run_id: str, error_data: dict[str, Any]) -> None:
        errors_dir = self.root / "errors"
        errors_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(errors_dir / f"{run_id}.json", error_data)

    # ── internal helpers ─────────────────────────────────────────────────

    def _cluster_dir(self, cluster_id: str) -> Path:
        d = self.root / "clusters" / cluster_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ensure_dirs(self) -> None:
        dirs = [
            self.root / "clusters",
            self.root / "syntheses",
            self.root / "cache",
            self.root / "events",
            self.root / "errors",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        """Write JSON atomically via temp file + rename."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
            return data

    @staticmethod
    def _state_hash(state: dict[str, Any]) -> str:
        canonical = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def _load_cache(self) -> dict[str, str]:
        path = self.root / "cache" / "state_hash_cache.json"
        if not path.exists():
            return {}
        data = self._read_json(path)
        return {str(k): str(v) for k, v in data.items()}
