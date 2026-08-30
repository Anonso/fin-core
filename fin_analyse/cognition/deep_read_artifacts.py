"""DeepReadArtifactService — pre-computed article deep-read artifact lifecycle.

Small, deep module: manages generation, freshness checking, atomic write,
and loading of full and compact deep-read artifacts for ZSXQ articles.

Artifact directory layout (under kb_root/runtime/cognition/deep_read_artifacts/):
  full/<article_id>.json     — complete ZsxqApprenticeResult.to_dict()
  compact/<article_id>.json  — reduced structure for injection/display

Each artifact is a JSON envelope:
  {
    "artifact_version": "deep_read_artifact_v1",
    "article_id": "...",
    "content_hash": "<sha256 of article file>",
    "pipeline_version": "1.0.0",
    "generated_at": "<ISO datetime>",
    "generation_id": "<shared full/compact generation id>",
    "detail": "full|compact",
    "payload": { ... }
  }

Freshness is determined by matching article_id + content_hash +
artifact_version + pipeline_version and by requiring the full and compact
envelopes to share one non-empty generation_id and generated_at. Artifacts
that explicitly report an unavailable LLM backend or an LLM extraction
failure remain retryable. A successful extraction may legitimately produce no
units; an empty payload alone is not treated as a failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fin_analyse.cognition.llm import CognitionCompletionControl

logger = logging.getLogger(__name__)

ARTIFACT_VERSION = "deep_read_artifact_v1"
PIPELINE_VERSION = "1.0.0"
_MAX_ARTICLE_BYTES = 4 * 1024 * 1024
_MAX_FULL_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_COMPACT_ARTIFACT_BYTES = 2 * 1024 * 1024
_OWNER_ONLY_DIRECTORY_MODE = 0o700
_OWNER_ONLY_FILE_MODE = 0o600
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW_DIRECTORY_OPEN_FLAGS = _DIRECTORY_OPEN_FLAGS | getattr(os, "O_NOFOLLOW", 0)
_READ_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_RETRYABLE_BACKEND_WARNING = "LLM backend unavailable"
_RETRYABLE_EXTRACTION_WARNING_RE = re.compile(r"\bLLM extraction (?:failed|error)\b", re.IGNORECASE)


def _has_retryable_backend_failure(payload: dict[str, Any]) -> bool:
    warnings = payload.get("warnings")
    return isinstance(warnings, list) and any(
        isinstance(warning, str) and _RETRYABLE_BACKEND_WARNING in warning for warning in warnings
    )


def _has_retryable_extraction_failure(payload: dict[str, Any]) -> bool:
    warnings = payload.get("warnings")
    return isinstance(warnings, list) and any(
        isinstance(warning, str) and _RETRYABLE_EXTRACTION_WARNING_RE.search(warning)
        for warning in warnings
    )


#: vision 故障警告（zsxq_apprentice 折叠时的两种前缀）。
_VISION_FAILURE_WARNING_RE = re.compile(r"^(vision data gaps: |\[vision\] )")


def _has_retryable_vision_failure(payload: dict[str, Any]) -> bool:
    """空产物 + vision 故障 = 补做候选（2026-08-30 owner 拍板）。

    举证：08-18/19 两篇锐评正文可提取却因 vision 链故障整篇空壳且永不
    补做——vision 本为 best-effort 上下文，不应永久封死文本提取的结果。
    稳定边界（故意收窄）：仅「units 为空 + 警告含 vision 故障前缀」才
    候选；有单元的产物不因 vision 缺席翻旧账（best-effort 语义保持，
    存量零扰动）；文本兜底（central-idea）成功即 units>0，自然恢复 fresh。
    """
    units = payload.get("units")
    if isinstance(units, list) and units:
        return False
    warnings = payload.get("warnings")
    return isinstance(warnings, list) and any(
        isinstance(warning, str) and _VISION_FAILURE_WARNING_RE.match(warning)
        for warning in warnings
    )


#: 补做窗口：发布后 7 天内才随定时深化补做。owner 2026-08-30 追加边界：
#: 难识别图片类必须有终态，不得无限占用补做队列。锚 = source.published_at
#: （重生成不刷新，天然防「每次重试都续期」死循环）；超龄 = 终态诚实空
#: （原因链保留在产物里，force 手动重生成不受限）；发布时间缺失/不可解析
#: → 保守不补（宁可漏做，不做无界重试）。
_VISION_RETRY_WINDOW = timedelta(days=7)


def _vision_failure_pending_retry(payload: dict[str, Any]) -> bool:
    if not _has_retryable_vision_failure(payload):
        return False
    published = str((payload.get("source") or {}).get("published_at") or "").strip()
    try:
        published_at = datetime.fromisoformat(published)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
    except ValueError:
        return False
    return datetime.now(UTC) - published_at <= _VISION_RETRY_WINDOW


@dataclass(frozen=True)
class _FileSnapshot:
    raw: bytes
    sha256: str
    modified_at: str


@dataclass(frozen=True)
class _DirectoryLink:
    """One held directory and its exact link from an already-held parent."""

    parent_fd: int
    name: str
    fd: int
    device: int
    inode: int
    owner_only: bool


@dataclass(frozen=True)
class _ArtifactStoreHandles:
    """Held descriptors for one verified artifact-store generation."""

    kb_fd: int
    links: tuple[_DirectoryLink, ...]
    root_fd: int
    full_fd: int
    compact_fd: int

    @property
    def all_fds(self) -> tuple[int, ...]:
        return (self.kb_fd, *(link.fd for link in self.links))


@dataclass(frozen=True)
class _ArtifactTargetIdentity:
    device: int
    inode: int
    owner: int
    links: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


class _ArtifactStoreBoundaryError(ValueError):
    """The artifact owner path or one of its immutable identities is unsafe."""


@dataclass(frozen=True)
class DeepReadArtifactPair:
    """One validated full/compact artifact generation."""

    generation_id: str
    generated_at: str
    content_hash: str
    compact_raw_sha256: str
    article_modified_at: str
    full: dict[str, Any]
    compact: dict[str, Any]


class DeepReadArtifactService:
    """Generates, caches, and loads deep-read artifacts for ZSXQ articles.

    Public interface (small surface):
      - ensure_artifacts(article_id, article_path, force=False) -> dict
      - load_compact(article_id) -> dict | None
      - load_full(article_id) -> dict | None
      - load_fresh_pair(article_id, article_path) -> DeepReadArtifactPair | None
      - is_fresh(article_id, article_path) -> bool
    """

    def __init__(self, kb_root: Path) -> None:
        self._kb_root = Path(kb_root).absolute()
        self._artifacts_root = self._kb_root / "runtime" / "cognition" / "deep_read_artifacts"
        self._full_dir = self._artifacts_root / "full"
        self._compact_dir = self._artifacts_root / "compact"

    # ── Safe artifact key ───────────────────────────────────────────────

    _SAFE_KEY_RE = re.compile(r"^[a-zA-Z0-9_\-\.\:]+$")

    @classmethod
    def _safe_artifact_key(cls, article_id: str) -> str:
        """Return a path-safe key for artifact file naming.

        Rules:
        - Safe id: only ASCII alphanumeric, _, -, ., : AND not empty, ., ..
        - Unsafe id: ``unsafe_<sha256(raw_id)[:16]>``
        """
        if not article_id or article_id in (".", ".."):
            return f"unsafe_{hashlib.sha256(article_id.encode()).hexdigest()[:16]}"
        if cls._SAFE_KEY_RE.match(article_id):
            return article_id
        return f"unsafe_{hashlib.sha256(article_id.encode()).hexdigest()[:16]}"

    @staticmethod
    def _resolve_in_dir(base_dir: Path, filename: str) -> Path:
        """Resolve one filename below a real, non-symlink directory."""
        lexical_base = base_dir.absolute()
        if ".." in lexical_base.parts or lexical_base.is_symlink():
            raise ValueError(f"Artifact base directory is unsafe: {base_dir}")
        resolved_base = lexical_base.resolve(strict=False)
        if resolved_base != lexical_base:
            raise ValueError(f"Artifact base directory is unsafe: {base_dir}")
        resolved = (lexical_base / filename).resolve()
        if not resolved.is_relative_to(resolved_base):
            raise ValueError(
                f"Artifact path escape attempt: {filename!r} resolves outside {base_dir}"
            )
        return resolved

    # ── Public API ──────────────────────────────────────────────────────

    def ensure_artifacts(
        self,
        article_id: str,
        article_path: str | Path,
        *,
        force: bool = False,
        control: CognitionCompletionControl | None = None,
    ) -> dict[str, Any]:
        """Ensure full and compact artifacts exist and are fresh.

        Returns a status dict with fields:
          article_id, status (cache_hit|generated|retryable|missing|error),
          full_path, compact_path, content_hash, generated_at,
          data_gaps, warnings
        """
        if control is not None:
            control.checkpoint_or_raise()
        path = Path(article_path)
        data_gaps: list[str] = []
        warnings: list[str] = []

        if not path.exists():
            return {
                "article_id": article_id,
                "status": "error",
                "full_path": None,
                "compact_path": None,
                "content_hash": None,
                "generated_at": None,
                "data_gaps": ["article_file_missing"],
                "warnings": [f"Article file not found: {path}"],
            }

        try:
            content_hash = self._compute_content_hash(path)
        except (OSError, ValueError):
            return {
                "article_id": article_id,
                "status": "error",
                "full_path": None,
                "compact_path": None,
                "content_hash": None,
                "generated_at": None,
                "data_gaps": ["article_file_invalid"],
                "warnings": ["Article file boundary invalid"],
            }
        safe_key = self._safe_artifact_key(article_id)
        full_name = f"{safe_key}.json"
        compact_name = f"{safe_key}.json"

        fresh_pair = (
            None if force else self._load_fresh_pair_internal(article_id, safe_key, content_hash)
        )
        if fresh_pair is not None:
            if control is not None:
                control.checkpoint_or_raise()
            return {
                "article_id": article_id,
                "status": "cache_hit",
                "full_path": str(self._full_dir / full_name),
                "compact_path": str(self._compact_dir / compact_name),
                "content_hash": content_hash,
                "generated_at": fresh_pair.generated_at,
                "data_gaps": [],
                "warnings": [],
            }

        store = self._open_artifact_store(create=True)
        if store is None:
            return self._artifact_store_error_result(article_id, content_hash)
        try:
            self._preflight_artifact_target(store.full_fd, full_name)
            self._preflight_artifact_target(store.compact_fd, compact_name)
            if not self._verify_artifact_store(store):
                raise _ArtifactStoreBoundaryError("artifact store identity changed")
        except (OSError, ValueError):
            return self._artifact_store_error_result(article_id, content_hash)
        finally:
            self._close_artifact_store(store)

        # Generate artifacts via ZsxqCognitionApprentice
        try:
            from fin_analyse.cognition.zsxq_apprentice import ZsxqCognitionApprentice

            apprentice = ZsxqCognitionApprentice(
                runtime_root=self._kb_root / "runtime" / "cognition"
            )
            result = (
                apprentice.deep_read(path)
                if control is None
                else apprentice.deep_read(path, control=control)
            )
            if control is not None:
                control.checkpoint_or_raise()
            result_dict = result.to_dict()

            generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
            generation_id = uuid4().hex

            # Build full artifact
            full_artifact = self._build_envelope(
                article_id=article_id,
                content_hash=content_hash,
                generated_at=generated_at,
                generation_id=generation_id,
                detail="full",
                payload=result_dict,
            )

            # Build compact artifact
            compact_payload = self._build_compact_payload(
                result_dict, article_id, generated_at, generation_id=generation_id
            )
            compact_artifact = self._build_envelope(
                article_id=article_id,
                content_hash=content_hash,
                generated_at=generated_at,
                generation_id=generation_id,
                detail="compact",
                payload=compact_payload,
            )

            store = self._open_artifact_store(create=False)
            if store is None:
                raise _ArtifactStoreBoundaryError("artifact store unavailable")
            retryable = False
            try:
                self._preflight_artifact_target(store.full_fd, full_name)
                self._preflight_artifact_target(store.compact_fd, compact_name)
                if control is not None:
                    control.checkpoint_or_raise()
                publication = (
                    control.fence.publication(at=datetime.now(UTC))
                    if control is not None
                    else nullcontext(True)
                )
                with publication as publication_open:
                    if not publication_open:
                        raise TimeoutError("deep-read artifact publication deadline exhausted")
                    self._atomic_write(
                        store,
                        store.full_fd,
                        full_name,
                        full_artifact,
                        max_bytes=_MAX_FULL_ARTIFACT_BYTES,
                    )
                    self._atomic_write(
                        store,
                        store.compact_fd,
                        compact_name,
                        compact_artifact,
                        max_bytes=_MAX_COMPACT_ARTIFACT_BYTES,
                    )
                    written_pair = self._load_fresh_pair_from_store(
                        store,
                        article_id,
                        safe_key,
                        content_hash,
                        allow_retryable_failure=False,
                    )
                    if written_pair is None or not self._verify_artifact_store(store):
                        # The pair was written, but the normal fresh-pair
                        # validator rejected it (retryable backend/extraction
                        # warnings).  Keep the generation record, yet never
                        # present it as usable success.
                        retryable_pair = self._load_fresh_pair_from_store(
                            store,
                            article_id,
                            safe_key,
                            content_hash,
                            allow_retryable_failure=True,
                        )
                        if retryable_pair is None or not self._verify_artifact_store(
                            store
                        ):
                            raise _ArtifactStoreBoundaryError(
                                "written artifact pair invalid"
                            )
                        retryable = True
            finally:
                self._close_artifact_store(store)

            if result.warnings:
                warnings.extend(result.warnings)

            if retryable:
                return {
                    "article_id": article_id,
                    "status": "retryable",
                    "full_path": str(self._full_dir / full_name),
                    "compact_path": str(self._compact_dir / compact_name),
                    "content_hash": content_hash,
                    "generated_at": generated_at,
                    "data_gaps": data_gaps,
                    "warnings": warnings,
                }
            return {
                "article_id": article_id,
                "status": "generated",
                "full_path": str(self._full_dir / full_name),
                "compact_path": str(self._compact_dir / compact_name),
                "content_hash": content_hash,
                "generated_at": generated_at,
                "data_gaps": data_gaps,
                "warnings": warnings,
            }

        except _ArtifactStoreBoundaryError:
            return self._artifact_store_error_result(article_id, content_hash)
        except Exception as exc:
            logger.warning("Deep read artifact generation failed for %s: %s", article_id, exc)
            return {
                "article_id": article_id,
                "status": "error",
                "full_path": None,
                "compact_path": None,
                "content_hash": content_hash if path.exists() else None,
                "generated_at": None,
                "data_gaps": ["deep_read_generation_failed"],
                "warnings": [str(exc)],
            }

    def load_compact(self, article_id: str) -> dict[str, Any] | None:
        """Load the compact artifact payload for an article, or None."""
        safe_key = self._safe_artifact_key(article_id)
        store = self._open_artifact_store(create=False)
        if store is None:
            return None
        try:
            loaded = self._read_artifact_snapshot_at(
                store.compact_fd,
                f"{safe_key}.json",
                max_bytes=_MAX_COMPACT_ARTIFACT_BYTES,
            )
            if loaded is None or not self._verify_artifact_store(store):
                return None
            data, _ = loaded
            payload = data.get("payload")
            return cast(dict[str, Any], payload) if isinstance(payload, dict) else None
        finally:
            self._close_artifact_store(store)

    def load_full(self, article_id: str) -> dict[str, Any] | None:
        """Load the full artifact payload for an article, or None."""
        safe_key = self._safe_artifact_key(article_id)
        store = self._open_artifact_store(create=False)
        if store is None:
            return None
        try:
            loaded = self._read_artifact_snapshot_at(
                store.full_fd,
                f"{safe_key}.json",
                max_bytes=_MAX_FULL_ARTIFACT_BYTES,
            )
            if loaded is None or not self._verify_artifact_store(store):
                return None
            data, _ = loaded
            payload = data.get("payload")
            return cast(dict[str, Any], payload) if isinstance(payload, dict) else None
        finally:
            self._close_artifact_store(store)

    def is_fresh(self, article_id: str, article_path: str | Path) -> bool:
        """Check whether a fresh artifact exists for the given article."""
        path = Path(article_path)
        if not path.exists():
            return False
        try:
            content_hash = self._compute_content_hash(path)
        except (OSError, ValueError):
            return False
        safe_key = self._safe_artifact_key(article_id)
        return self._is_fresh_internal(article_id, safe_key, content_hash)

    def load_fresh_pair(
        self,
        article_id: str,
        article_path: str | Path,
    ) -> DeepReadArtifactPair | None:
        """Load one validated full/compact generation, or fail closed."""
        path = Path(article_path)
        try:
            article = self._read_file_snapshot(path, max_bytes=_MAX_ARTICLE_BYTES)
        except (OSError, ValueError):
            return None
        safe_key = self._safe_artifact_key(article_id)
        return self._load_fresh_pair_internal(
            article_id,
            safe_key,
            article.sha256,
            article_modified_at=article.modified_at,
        )

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _compute_content_hash(article_path: Path) -> str:
        return DeepReadArtifactService._read_file_snapshot(
            article_path,
            max_bytes=_MAX_ARTICLE_BYTES,
        ).sha256

    @staticmethod
    def _read_file_snapshot(path: Path, *, max_bytes: int) -> _FileSnapshot:
        """Read one owned regular file through a stable, no-follow descriptor."""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or before.st_size < 0
                or before.st_size > max_bytes
            ):
                raise ValueError("deep-read file boundary invalid")
            remaining = before.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    raise ValueError("deep-read file truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise ValueError("deep-read file grew while reading")
            after = os.fstat(fd)
            stable_identity = (
                before.st_dev,
                before.st_ino,
                before.st_uid,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
            )
            if stable_identity != (
                after.st_dev,
                after.st_ino,
                after.st_uid,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError("deep-read file changed while reading")
            raw = b"".join(chunks)
            return _FileSnapshot(
                raw=raw,
                sha256=hashlib.sha256(raw).hexdigest(),
                modified_at=datetime.fromtimestamp(before.st_mtime, tz=UTC).isoformat(),
            )
        finally:
            os.close(fd)

    def _open_kb_root(self) -> int:
        """Open the approved KB root without following any path component."""
        path = self._kb_root
        if not path.is_absolute() or ".." in path.parts:
            raise _ArtifactStoreBoundaryError("knowledge-base root invalid")
        current_fd = os.open("/", _DIRECTORY_OPEN_FLAGS)
        try:
            for part in path.parts[1:]:
                next_fd = os.open(part, _NOFOLLOW_DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            current = os.fstat(current_fd)
            if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.getuid():
                raise _ArtifactStoreBoundaryError("knowledge-base root ownership invalid")
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    @staticmethod
    def _open_directory_at(
        parent_fd: int,
        name: str,
        *,
        create: bool,
        owner_only: bool,
    ) -> _DirectoryLink:
        if not name or "/" in name or name in {".", ".."}:
            raise _ArtifactStoreBoundaryError("artifact directory name invalid")
        if create:
            try:
                os.mkdir(name, _OWNER_ONLY_DIRECTORY_MODE, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
        fd = os.open(name, _NOFOLLOW_DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        try:
            current = os.fstat(fd)
            if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.getuid():
                raise _ArtifactStoreBoundaryError("artifact directory ownership invalid")
            if (
                owner_only
                and create
                and stat.S_IMODE(current.st_mode) != _OWNER_ONLY_DIRECTORY_MODE
            ):
                os.fchmod(fd, _OWNER_ONLY_DIRECTORY_MODE)
                os.fsync(fd)
                current = os.fstat(fd)
            mode = stat.S_IMODE(current.st_mode)
            if owner_only and mode != _OWNER_ONLY_DIRECTORY_MODE:
                raise _ArtifactStoreBoundaryError("artifact directory mode invalid")
            linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(linked.st_mode)
                or linked.st_dev != current.st_dev
                or linked.st_ino != current.st_ino
            ):
                raise _ArtifactStoreBoundaryError("artifact directory identity invalid")
            return _DirectoryLink(
                parent_fd=parent_fd,
                name=name,
                fd=fd,
                device=current.st_dev,
                inode=current.st_ino,
                owner_only=owner_only,
            )
        except Exception:
            os.close(fd)
            raise

    def _open_artifact_store(self, *, create: bool) -> _ArtifactStoreHandles | None:
        opened_fds: list[int] = []
        try:
            kb_fd = self._open_kb_root()
            opened_fds.append(kb_fd)
            runtime = self._open_directory_at(
                kb_fd,
                "runtime",
                create=create,
                owner_only=False,
            )
            opened_fds.append(runtime.fd)
            cognition = self._open_directory_at(
                runtime.fd,
                "cognition",
                create=create,
                owner_only=False,
            )
            opened_fds.append(cognition.fd)
            artifact_root = self._open_directory_at(
                cognition.fd,
                "deep_read_artifacts",
                create=create,
                owner_only=True,
            )
            opened_fds.append(artifact_root.fd)
            full = self._open_directory_at(
                artifact_root.fd,
                "full",
                create=create,
                owner_only=True,
            )
            opened_fds.append(full.fd)
            compact = self._open_directory_at(
                artifact_root.fd,
                "compact",
                create=create,
                owner_only=True,
            )
            opened_fds.append(compact.fd)
            store = _ArtifactStoreHandles(
                kb_fd=kb_fd,
                links=(runtime, cognition, artifact_root, full, compact),
                root_fd=artifact_root.fd,
                full_fd=full.fd,
                compact_fd=compact.fd,
            )
            if not self._verify_artifact_store(store):
                raise _ArtifactStoreBoundaryError("artifact store identity invalid")
            return store
        except (OSError, RuntimeError, ValueError):
            for fd in reversed(opened_fds):
                with suppress(OSError):
                    os.close(fd)
            return None

    def _verify_artifact_store(self, store: _ArtifactStoreHandles) -> bool:
        try:
            reopened_kb_fd = self._open_kb_root()
            try:
                expected_kb = os.fstat(store.kb_fd)
                current_kb = os.fstat(reopened_kb_fd)
                if (expected_kb.st_dev, expected_kb.st_ino) != (
                    current_kb.st_dev,
                    current_kb.st_ino,
                ):
                    return False
            finally:
                os.close(reopened_kb_fd)
            for link in store.links:
                held = os.fstat(link.fd)
                linked = os.stat(link.name, dir_fd=link.parent_fd, follow_symlinks=False)
                mode = stat.S_IMODE(held.st_mode)
                if (
                    not stat.S_ISDIR(held.st_mode)
                    or not stat.S_ISDIR(linked.st_mode)
                    or held.st_uid != os.getuid()
                    or (held.st_dev, held.st_ino) != (link.device, link.inode)
                    or (linked.st_dev, linked.st_ino) != (link.device, link.inode)
                    or (link.owner_only and mode != _OWNER_ONLY_DIRECTORY_MODE)
                ):
                    return False
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    @staticmethod
    def _close_artifact_store(store: _ArtifactStoreHandles) -> None:
        for fd in reversed(store.all_fds):
            with suppress(OSError):
                os.close(fd)

    @staticmethod
    def _target_identity(raw: os.stat_result) -> _ArtifactTargetIdentity:
        return _ArtifactTargetIdentity(
            device=raw.st_dev,
            inode=raw.st_ino,
            owner=raw.st_uid,
            links=raw.st_nlink,
            mode=raw.st_mode,
            size=raw.st_size,
            modified_ns=raw.st_mtime_ns,
            changed_ns=raw.st_ctime_ns,
        )

    @classmethod
    def _read_target_identity(
        cls,
        directory_fd: int,
        filename: str,
    ) -> _ArtifactTargetIdentity | None:
        try:
            raw = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(raw.st_mode)
            or raw.st_uid != os.getuid()
            or raw.st_nlink != 1
            or stat.S_IMODE(raw.st_mode) & 0o022
            or raw.st_size < 0
        ):
            raise _ArtifactStoreBoundaryError("artifact target invalid")
        return cls._target_identity(raw)

    @classmethod
    def _preflight_artifact_target(
        cls,
        directory_fd: int,
        filename: str,
    ) -> _ArtifactTargetIdentity | None:
        legacy_tmp = f"{filename}.tmp"
        try:
            os.stat(legacy_tmp, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise _ArtifactStoreBoundaryError("legacy artifact temp path occupied")
        return cls._read_target_identity(directory_fd, filename)

    @classmethod
    def _read_file_snapshot_at(
        cls,
        directory_fd: int,
        filename: str,
        *,
        max_bytes: int,
    ) -> _FileSnapshot:
        linked_before = cls._read_target_identity(directory_fd, filename)
        if linked_before is None or linked_before.size > max_bytes:
            raise _ArtifactStoreBoundaryError("artifact file boundary invalid")
        fd = os.open(filename, _READ_FILE_FLAGS, dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            if cls._target_identity(before) != linked_before:
                raise _ArtifactStoreBoundaryError("artifact file identity invalid")
            remaining = before.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    raise _ArtifactStoreBoundaryError("artifact file truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise _ArtifactStoreBoundaryError("artifact file grew while reading")
            after = os.fstat(fd)
            linked_after = cls._read_target_identity(directory_fd, filename)
            if cls._target_identity(after) != linked_before or linked_after != linked_before:
                raise _ArtifactStoreBoundaryError("artifact file changed while reading")
            raw = b"".join(chunks)
            return _FileSnapshot(
                raw=raw,
                sha256=hashlib.sha256(raw).hexdigest(),
                modified_at=datetime.fromtimestamp(before.st_mtime, tz=UTC).isoformat(),
            )
        finally:
            os.close(fd)

    def _is_fresh_internal(self, article_id: str, safe_key: str, content_hash: str) -> bool:
        """Check whether a complete, same-generation artifact pair exists."""
        return self._load_fresh_pair_internal(article_id, safe_key, content_hash) is not None

    def _load_fresh_pair_internal(
        self,
        article_id: str,
        safe_key: str,
        content_hash: str,
        *,
        article_modified_at: str = "",
    ) -> DeepReadArtifactPair | None:
        store = self._open_artifact_store(create=False)
        if store is None:
            return None
        try:
            pair = self._load_fresh_pair_from_store(
                store,
                article_id,
                safe_key,
                content_hash,
                article_modified_at=article_modified_at,
            )
            return pair if self._verify_artifact_store(store) else None
        finally:
            self._close_artifact_store(store)

    def _load_fresh_pair_from_store(
        self,
        store: _ArtifactStoreHandles,
        article_id: str,
        safe_key: str,
        content_hash: str,
        *,
        article_modified_at: str = "",
        allow_retryable_failure: bool = False,
    ) -> DeepReadArtifactPair | None:
        full_artifact = self._read_artifact_snapshot_at(
            store.full_fd,
            f"{safe_key}.json",
            max_bytes=_MAX_FULL_ARTIFACT_BYTES,
        )
        compact_artifact = self._read_artifact_snapshot_at(
            store.compact_fd,
            f"{safe_key}.json",
            max_bytes=_MAX_COMPACT_ARTIFACT_BYTES,
        )

        if full_artifact is None or compact_artifact is None:
            return None
        full_data, _ = full_artifact
        compact_data, compact_snapshot = compact_artifact

        envelopes_match = (
            full_data.get("article_id") == article_id
            and full_data.get("content_hash") == content_hash
            and full_data.get("artifact_version") == ARTIFACT_VERSION
            and full_data.get("pipeline_version") == PIPELINE_VERSION
            and full_data.get("detail") == "full"
            and compact_data.get("article_id") == article_id
            and compact_data.get("content_hash") == content_hash
            and compact_data.get("artifact_version") == ARTIFACT_VERSION
            and compact_data.get("pipeline_version") == PIPELINE_VERSION
            and compact_data.get("detail") == "compact"
        )
        if not envelopes_match:
            return None

        generation_id = full_data.get("generation_id")
        generated_at = full_data.get("generated_at")
        full_payload = full_data.get("payload")
        compact_payload = compact_data.get("payload")
        if (
            not isinstance(generation_id, str)
            or not generation_id
            or compact_data.get("generation_id") != generation_id
            or not isinstance(generated_at, str)
            or not generated_at
            or compact_data.get("generated_at") != generated_at
            or not isinstance(full_payload, dict)
            or not isinstance(compact_payload, dict)
        ):
            return None
        if not allow_retryable_failure and (
            _has_retryable_backend_failure(full_payload)
            or _has_retryable_backend_failure(compact_payload)
            or _has_retryable_extraction_failure(full_payload)
            or _has_retryable_extraction_failure(compact_payload)
            or _vision_failure_pending_retry(full_payload)
            or _vision_failure_pending_retry(compact_payload)
        ):
            return None

        return DeepReadArtifactPair(
            generation_id=generation_id,
            generated_at=generated_at,
            content_hash=content_hash,
            compact_raw_sha256=compact_snapshot.sha256,
            article_modified_at=article_modified_at,
            full=cast(dict[str, Any], full_payload),
            compact=cast(dict[str, Any], compact_payload),
        )

    @classmethod
    def _read_artifact_snapshot_at(
        cls,
        directory_fd: int,
        filename: str,
        *,
        max_bytes: int,
    ) -> tuple[dict[str, Any], _FileSnapshot] | None:
        try:
            snapshot = cls._read_file_snapshot_at(
                directory_fd,
                filename,
                max_bytes=max_bytes,
            )
            data = json.loads(snapshot.raw.decode("utf-8"))
            if not isinstance(data, dict):
                return None
            return cast(dict[str, Any], data), snapshot
        except (UnicodeDecodeError, json.JSONDecodeError, OSError, RecursionError, ValueError):
            return None

    def _build_envelope(
        self,
        article_id: str,
        content_hash: str,
        generated_at: str,
        generation_id: str,
        detail: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "artifact_version": ARTIFACT_VERSION,
            "article_id": article_id,
            "content_hash": content_hash,
            "pipeline_version": PIPELINE_VERSION,
            "generated_at": generated_at,
            "generation_id": generation_id,
            "detail": detail,
            "payload": payload,
        }

    @staticmethod
    def _build_compact_payload(
        full_result: dict[str, Any],
        article_id: str,
        generated_at: str,
        generation_id: str = "",
    ) -> dict[str, Any]:
        """Build a compact payload from full ZsxqApprenticeResult.to_dict().

        Compact does NOT include:
          - Full evidence_chains (raw details)
          - Full unit bodies (just core thesis info)
          - clocks (internal freshness tracking)
          - raw warnings from extraction
        """
        source = full_result.get("source", {})
        units: list[dict[str, Any]] = full_result.get("units", [])
        theme_clusters: list[dict[str, Any]] = full_result.get("theme_clusters", [])
        suggestions: list[dict[str, Any]] = full_result.get("suggestions", [])

        # methodology_rules 输入合同(G 方法论层验收 1):只保留 unit_type=
        # methodology_rule 的单元,老师原话与学徒推演分列;畸形条目逐条跳过。
        # 计数关系保留:methodology_rule 单元继续计入 unit_count 与 core_theses,
        # 语义门 no_extractable_units 判定不变;本字段是附加视图。
        methodology_rules: list[dict[str, Any]] = []
        for unit in units:
            if not isinstance(unit, dict):
                continue
            if str(unit.get("unit_type", "")) != "methodology_rule":
                continue
            title = str(unit.get("title", "")).strip()
            rule = str(unit.get("thesis", "")).strip()
            if not title or not rule:
                continue
            try:
                confidence = float(unit.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            evidence = unit.get("original_evidence") or []
            teacher_quote = " ".join(str(e) for e in evidence if isinstance(e, str) and e.strip())[
                :500
            ]
            # M1:老师原话非空是投影前条件——无原话的方法论规则不可信,不进入合同。
            if not teacher_quote:
                continue
            # M2:与闭合 reader 合同一致——最多保留 32 条,超量截断(防整份被拒)。
            if len(methodology_rules) >= 32:
                continue
            methodology_rules.append(
                {
                    "title": title[:50],
                    "rule": rule,
                    "teacher_quote": teacher_quote,
                    "apprentice_interpretation": str(unit.get("apprentice_interpretation", "")),
                    "related_topics": [
                        str(t)
                        for t in (unit.get("related_topics") or [])
                        if isinstance(t, str) and t
                    ],
                    "confidence": confidence,
                    "source_id": str(unit.get("source_id", "")),
                    "article_id": article_id,
                    "published_at": str(source.get("published_at", "")),
                    "generation_id": generation_id,
                }
            )

        # Core theses: keep title/thesis/related_companies/related_topics/confidence
        core_theses: list[dict[str, Any]] = []
        for unit in units:
            core_theses.append(
                {
                    "title": unit.get("title", ""),
                    "thesis": unit.get("thesis", ""),
                    "related_companies": unit.get("related_companies", []),
                    "related_topics": unit.get("related_topics", []),
                    "confidence": unit.get("confidence", 0.0),
                }
            )

        # Theme clusters: keep name/active_status/unit_count/core_theses
        compact_clusters: list[dict[str, Any]] = []
        for cluster in theme_clusters:
            compact_clusters.append(
                {
                    "name": cluster.get("name", ""),
                    "active_status": cluster.get("active_status", ""),
                    "unit_count": len(cluster.get("unit_ids", [])),
                    "core_theses": cluster.get("core_theses", []),
                }
            )

        # Suggestions: keep level/summary/tracking_indicators/risk_boundaries/
        # allowed_usage/forbidden_usage/confidence
        compact_suggestions: list[dict[str, Any]] = []
        for sug in suggestions:
            compact_suggestions.append(
                {
                    "level": sug.get("suggestion_level", sug.get("level", "")),
                    "summary": sug.get("summary", ""),
                    "tracking_indicators": sug.get("tracking_indicators", []),
                    "risk_boundaries": sug.get("risk_boundaries", []),
                    "allowed_usage": sug.get("allowed_usage", []),
                    "forbidden_usage": sug.get("forbidden_usage", []),
                    "confidence": sug.get("confidence", 0.0),
                }
            )

        # Build injectable summary text
        injectable_parts: list[str] = []
        title = source.get("title", "") or full_result.get("source", {}).get("title", "")
        if title:
            injectable_parts.append(f"文章: {title}")
        if core_theses:
            thesis_lines = [f"  - {t['title']}: {t['thesis']}" for t in core_theses[:5]]
            injectable_parts.append("核心论点:\n" + "\n".join(thesis_lines))
        if compact_clusters:
            cluster_names = [c["name"] for c in compact_clusters[:5]]
            injectable_parts.append(f"主题聚类: {', '.join(cluster_names)}")

        injectable_summary = "\n".join(injectable_parts)

        usage_boundary = (
            "research/advisory only — 不得用于自动调仓、绕过风控或作为直接买卖信号。"
            "本内容来源于教师文章深度阅读，仅用于研究参考和上下文注入。"
        )

        return {
            "article_id": article_id,
            "title": title,
            "column": source.get("column", ""),
            "published_at": source.get("published_at", ""),
            "source_rank": source.get("source_rank", ""),
            "generation_id": generation_id,
            "methodology_rules": methodology_rules,
            "unit_count": len(units),
            "theme_count": len(theme_clusters),
            "suggestion_count": len(suggestions),
            "core_theses": core_theses,
            "mapping_facts": (
                full_result.get("mapping_facts", {})
                if isinstance(full_result.get("mapping_facts"), dict)
                else {}
            ),
            "theme_clusters": compact_clusters,
            "suggestions": compact_suggestions,
            "injectable_summary": injectable_summary,
            "warnings": full_result.get("warnings", []),
            "usage_boundary": usage_boundary,
            "generated_at": generated_at,
        }

    @staticmethod
    def _artifact_store_error_result(
        article_id: str,
        content_hash: str,
    ) -> dict[str, Any]:
        return {
            "article_id": article_id,
            "status": "error",
            "full_path": None,
            "compact_path": None,
            "content_hash": content_hash,
            "generated_at": None,
            "data_gaps": ["deep_read_artifact_store_invalid"],
            "warnings": ["Deep read artifact store boundary invalid"],
        }

    @classmethod
    def _target_matches(
        cls,
        directory_fd: int,
        filename: str,
        expected: _ArtifactTargetIdentity | None,
    ) -> bool:
        try:
            return cls._read_target_identity(directory_fd, filename) == expected
        except (OSError, ValueError):
            return False

    @staticmethod
    def _same_replaced_file(
        before_replace: _ArtifactTargetIdentity,
        after_replace: _ArtifactTargetIdentity | None,
    ) -> bool:
        if after_replace is None:
            return False
        return (
            before_replace.device,
            before_replace.inode,
            before_replace.owner,
            before_replace.links,
            before_replace.mode,
            before_replace.size,
            before_replace.modified_ns,
        ) == (
            after_replace.device,
            after_replace.inode,
            after_replace.owner,
            after_replace.links,
            after_replace.mode,
            after_replace.size,
            after_replace.modified_ns,
        )

    @classmethod
    def _unlink_temp_if_same(
        cls,
        directory_fd: int,
        temp_name: str,
        expected: _ArtifactTargetIdentity | None,
    ) -> None:
        if expected is None or not cls._target_matches(directory_fd, temp_name, expected):
            return
        with suppress(OSError):
            os.unlink(temp_name, dir_fd=directory_fd)

    def _atomic_write(
        self,
        store: _ArtifactStoreHandles,
        directory_fd: int,
        filename: str,
        data: dict[str, Any],
        *,
        max_bytes: int,
    ) -> None:
        """Write one artifact through a held real directory descriptor."""
        encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > max_bytes:
            raise _ArtifactStoreBoundaryError("generated artifact oversized")
        target_before = self._preflight_artifact_target(directory_fd, filename)
        if not self._verify_artifact_store(store):
            raise _ArtifactStoreBoundaryError("artifact store identity changed")

        temp_name = f".{filename}.{uuid4().hex}.tmp"
        temp_fd: int | None = None
        temp_identity: _ArtifactTargetIdentity | None = None
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            temp_fd = os.open(
                temp_name,
                flags,
                _OWNER_ONLY_FILE_MODE,
                dir_fd=directory_fd,
            )
            os.fchmod(temp_fd, _OWNER_ONLY_FILE_MODE)
            written = 0
            while written < len(encoded):
                count = os.write(temp_fd, encoded[written:])
                if count <= 0:
                    raise _ArtifactStoreBoundaryError("artifact temp write truncated")
                written += count
            os.fsync(temp_fd)
            temp_stat = os.fstat(temp_fd)
            temp_identity = self._target_identity(temp_stat)
            linked_temp = self._read_target_identity(directory_fd, temp_name)
            if (
                not stat.S_ISREG(temp_stat.st_mode)
                or temp_stat.st_uid != os.getuid()
                or temp_stat.st_nlink != 1
                or stat.S_IMODE(temp_stat.st_mode) != _OWNER_ONLY_FILE_MODE
                or temp_stat.st_size != len(encoded)
                or linked_temp != temp_identity
            ):
                raise _ArtifactStoreBoundaryError("artifact temp identity invalid")
            if not self._verify_artifact_store(store) or not self._target_matches(
                directory_fd, filename, target_before
            ):
                raise _ArtifactStoreBoundaryError("artifact target identity changed")
            if self._preflight_artifact_target(directory_fd, filename) != target_before:
                raise _ArtifactStoreBoundaryError("artifact target identity changed")
            if (
                self._target_identity(os.fstat(temp_fd)) != temp_identity
                or self._read_target_identity(directory_fd, temp_name) != temp_identity
            ):
                raise _ArtifactStoreBoundaryError("artifact temp identity changed")
            os.replace(
                temp_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            final_identity = self._read_target_identity(directory_fd, filename)
            if not self._same_replaced_file(
                temp_identity,
                final_identity,
            ) or not self._verify_artifact_store(store):
                raise _ArtifactStoreBoundaryError("artifact replace identity invalid")
            os.fsync(directory_fd)
            temp_identity = None
        except _ArtifactStoreBoundaryError:
            raise
        except OSError as exc:
            raise _ArtifactStoreBoundaryError("artifact atomic write failed") from exc
        finally:
            if temp_fd is not None:
                cleanup_identity = temp_identity
                with suppress(OSError):
                    cleanup_identity = self._target_identity(os.fstat(temp_fd))
                self._unlink_temp_if_same(directory_fd, temp_name, cleanup_identity)
                with suppress(OSError):
                    os.close(temp_fd)
            else:
                self._unlink_temp_if_same(directory_fd, temp_name, temp_identity)
