"""Read-only observability for deep-read artifact availability.

Deep-read artifacts are a background-generated cache: the public gateway path
returns ``DEEP_READ_ARTIFACT_UNAVAILABLE`` when the pair is missing, stale,
corrupt, or unreadable — but until this module existed there was no way to
observe *how often* and *why* the cache missed.  This service classifies each
requested article's artifact state so cache-miss paths are measurable instead
of masquerading as success (decision map: 后台生成/新鲜度/可用率决定实际质量 —
需建立 artifact availability 与质量观测，不让 cache miss 伪装成功).

Pure read-only: reporting never creates artifacts, directories, or any state.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fin_analyse.cognition.deep_read_artifacts import DeepReadArtifactService


@dataclass(frozen=True)
class ArticleArtifactState:
    """Per-article artifact classification."""

    article_id: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class DeepReadAvailabilityReport:
    """Aggregated availability across the requested article set."""

    total: int
    by_article: dict[str, ArticleArtifactState] = field(default_factory=dict)

    @property
    def ready(self) -> int:
        return sum(1 for s in self.by_article.values() if s.status == "READY")

    @property
    def missing(self) -> int:
        return sum(1 for s in self.by_article.values() if s.status == "MISSING_ARTIFACT")

    @property
    def stale(self) -> int:
        return sum(1 for s in self.by_article.values() if s.status == "STALE")

    @property
    def corrupt(self) -> int:
        return sum(1 for s in self.by_article.values() if s.status == "CORRUPT")

    @property
    def unreadable(self) -> int:
        return sum(1 for s in self.by_article.values() if s.status == "UNREADABLE")

    @property
    def unknown(self) -> int:
        return sum(1 for s in self.by_article.values() if s.status == "UNKNOWN")

    @property
    def availability_rate(self) -> float:
        """Fraction of articles with a fresh usable artifact (0.0..1.0)."""
        if self.total == 0:
            return 0.0
        return self.ready / self.total

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "ready": self.ready,
            "missing_artifact": self.missing,
            "stale": self.stale,
            "corrupt": self.corrupt,
            "unreadable": self.unreadable,
            "unknown": self.unknown,
            "availability_rate": self.availability_rate,
            "by_article": {
                article_id: {"status": state.status, "detail": state.detail}
                for article_id, state in sorted(self.by_article.items())
            },
        }


class DeepReadAvailabilityService:
    """Classify deep-read artifact availability for a set of articles."""

    def __init__(self, kb_root: Path) -> None:
        self._kb_root = kb_root
        self._artifact_service = DeepReadArtifactService(kb_root=kb_root)

    def report(self, article_ids: Sequence[str]) -> DeepReadAvailabilityReport:
        """Classify each article id and aggregate the result.

        Raises:
            ValueError: ``article_ids`` is empty (no silent no-op).
        """
        if not article_ids:
            raise ValueError("article_ids must not be empty")

        states: dict[str, ArticleArtifactState] = {}
        for article_id in article_ids:
            states[article_id] = self._classify(article_id)
        return DeepReadAvailabilityReport(total=len(article_ids), by_article=states)

    # -- classification -----------------------------------------------------

    def _classify(self, article_id: str) -> ArticleArtifactState:
        article_path = self._resolve_article(article_id)
        if article_path is None:
            return ArticleArtifactState(article_id, "UNKNOWN", "article not resolved")

        safe_key = DeepReadArtifactService._safe_artifact_key(article_id)
        root = self._kb_root / "runtime" / "cognition" / "deep_read_artifacts"
        full_path = root / "full" / f"{safe_key}.json"
        compact_path = root / "compact" / f"{safe_key}.json"
        if not full_path.is_file() or not compact_path.is_file():
            return ArticleArtifactState(
                article_id, "MISSING_ARTIFACT", "full or compact artifact file absent"
            )

        try:
            article_hash = self._compute_article_hash(article_path)
            full_hash = self._read_artifact_content_hash(full_path)
            if full_hash is None:
                return ArticleArtifactState(
                    article_id, "CORRUPT", "full artifact unreadable or malformed"
                )
            if full_hash != article_hash:
                return ArticleArtifactState(
                    article_id, "STALE", "artifact content_hash does not match article"
                )
            pair = self._artifact_service.load_fresh_pair(article_id, article_path)
            if pair is None:
                return ArticleArtifactState(
                    article_id, "CORRUPT", "hash matches but pair failed to validate"
                )
            return ArticleArtifactState(article_id, "READY")
        except Exception as exc:  # security/boundary failures propagate as classified state
            return ArticleArtifactState(
                article_id, "UNREADABLE", f"artifact store rejected read: {type(exc).__name__}"
            )

    def _resolve_article(self, article_id: str) -> Path | None:
        """Resolve one canonical article strictly through ``index.json``.

        The canonical ``id`` must match exactly one entry; the entry's ``file``
        must be a safe basename inside ``articles/`` and an optional ``path``
        must agree with it.  No glob and no ``articles/<id>.md`` guessing, so
        date-prefixed production files resolve and escape attempts stay
        UNKNOWN.
        """
        entry = self._index_entry(article_id)
        if entry is None:
            return None
        candidate = self._candidate_article_path(entry)
        if candidate is None:
            return None
        try:
            metadata = candidate.lstat()
        except OSError:
            return None
        # Symlinks and non-regular files are not accepted article sources.
        if not stat.S_ISREG(metadata.st_mode):
            return None
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            return None
        try:
            articles_dir = (self._kb_root / "articles").resolve(strict=False)
        except OSError:
            return None
        if resolved.parent != articles_dir or not resolved.is_file():
            return None
        if not self._article_identity_matches(resolved, article_id):
            return None
        return resolved

    def _index_entry(self, article_id: str) -> dict[str, Any] | None:
        """Return the unique canonical index entry for an article id."""
        try:
            raw = (self._kb_root / "index.json").read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ):
            return None
        articles = data.get("articles") if isinstance(data, dict) else None
        if not isinstance(articles, list):
            return None
        matches = [
            article
            for article in articles
            if isinstance(article, dict) and article.get("id") == article_id
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def _candidate_article_path(self, entry: dict[str, Any]) -> Path | None:
        """Derive the article path from the entry's ``file``/``path`` fields."""
        file_value = entry.get("file")
        path_value = entry.get("path")
        if isinstance(file_value, str) and file_value:
            file_name = file_value.strip()
            if file_name != file_value or not _is_safe_basename(file_name):
                return None
            if path_value is not None:
                if not isinstance(path_value, str):
                    return None
                try:
                    path_name = Path(path_value).name
                except (OSError, ValueError):
                    return None
                if path_name != file_name:
                    return None
            return self._kb_root / "articles" / file_name
        if isinstance(path_value, str) and path_value:
            try:
                candidate = Path(path_value)
            except (OSError, ValueError):
                return None
            if not candidate.is_absolute() or candidate.name in {".", ".."}:
                return None
            return candidate
        return None

    @staticmethod
    def _article_identity_matches(path: Path, article_id: str) -> bool:
        """Require the article front-matter ``id`` to equal the index id."""
        try:
            text = path.read_bytes()[:4096].decode("utf-8", errors="replace")
        except OSError:
            return False
        match = re.match(r"^---\s*\r?\n(.*?)\r?\n---", text, re.DOTALL)
        if match is None:
            return False
        id_line = re.search(r"^id:\s*(.+?)\s*$", match.group(1), re.MULTILINE)
        if id_line is None:
            return False
        return id_line.group(1).strip().strip("'\"") == article_id

    @staticmethod
    def _compute_article_hash(article_path: Path) -> str:
        return hashlib.sha256(article_path.read_bytes()).hexdigest()

    @staticmethod
    def _read_artifact_content_hash(artifact_path: Path) -> str | None:
        """Return the artifact envelope's content_hash, or None if malformed."""
        try:
            raw = artifact_path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("content_hash")
        if not isinstance(value, str) or not value:
            return None
        return value


def _is_safe_basename(name: str) -> bool:
    """Reject empty, traversal, nested, and absolute article file names."""
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and name == Path(name).name
    )
