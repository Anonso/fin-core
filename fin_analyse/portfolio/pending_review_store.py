"""Durable pending review state: owner-only JSON per principal.

Each principal has at most one pending review.  A new preview atomically
supersedes the prior one.  Confirm consumes the frozen candidate with CAS.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from fin_analyse.portfolio.portfolio_review_types import PendingReview


def _pending_dir(environ: dict[str, str] | None = None) -> Path:
    import os as _os

    env = environ or _os.environ
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local/state"
    return base / "fin-analyse" / "portfolio-pending-review"


def _digest(data: str) -> str:
    import hashlib

    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _identity_digest(principal_id: str, interaction: object) -> str:
    parts = [
        principal_id,
        getattr(interaction, "profile_name", ""),
        getattr(interaction, "platform", ""),
        getattr(interaction, "session_key", ""),
        getattr(interaction, "subject_kind", ""),
        getattr(interaction, "subject_id", ""),
    ]
    return _digest("\x00".join(parts))


class PendingReviewStore:
    """Owner-only JSON store for one pending review per principal."""

    def __init__(
        self,
        *,
        environ: dict[str, str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._dir = _pending_dir(environ)
        self._clock = clock

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        return time.time()

    def _path(self, principal_id: str) -> Path:
        safe = _digest(principal_id)[:16]
        return self._dir / f"{safe}.json"

    def _lock_path(self, principal_id: str) -> Path:
        safe = _digest(principal_id)[:16]
        return self._dir / f"{safe}.lock"

    @contextmanager
    def _locked(self, principal_id: str) -> Iterator[int]:
        """Acquire an exclusive file lock for this principal's pending review."""
        self._dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock_path = self._lock_path(principal_id)
        lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield lock_fd
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def save(
        self,
        principal_id: str,
        review: PendingReview,
    ) -> None:
        """Atomically save a new pending review, superseding any prior one.

        Acquires the per-principal lock to prevent interleaving with
        confirm_under_lock.
        """
        self._dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        with suppress(OSError):
            self._dir.chmod(0o700)
        with self._locked(principal_id):
            # Best-effort hygiene: purge an expired residue before overwriting.
            # clear_expired does not take the lock itself (no deadlock) and is
            # exception-safe; the file is replaced right after either way.
            self.clear_expired(principal_id)
            target = self._path(principal_id)
            payload = json.dumps(
                {
                    "candidate_snapshot": review.candidate_snapshot,
                    "candidate_revision": review.candidate_revision,
                    "base_revision": review.base_revision,
                    "readable_preview": review.readable_preview,
                    "identity_digest": review.identity_digest,
                    "preview_turn": review.preview_turn,
                    "session_id": review.session_id,
                    "issued_at": review.issued_at,
                    "ttl_seconds": review.ttl_seconds,
                    "as_of_source": review.as_of_source,
                    "recorded_at": review.recorded_at,
                    "terminal_receipt": None,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            fd = -1
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(
                    dir=self._dir,
                    prefix=".pending-",
                    suffix=".tmp",
                )
                os.fchmod(fd, 0o600)
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("write made no progress")
                    remaining = remaining[written:]
                os.fsync(fd)
                os.close(fd)
                fd = -1
                os.rename(tmp, target)
                tmp = None
            finally:
                if fd >= 0:
                    os.close(fd)
                if tmp is not None:
                    with suppress(FileNotFoundError):
                        os.unlink(tmp)

    def load(self, principal_id: str) -> PendingReview | None:
        """Load pending review, including expired ones.  Caller checks .expired."""
        path = self._path(principal_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("terminal_receipt") is not None:
            return None
        try:
            return PendingReview(
                candidate_snapshot=data["candidate_snapshot"],
                candidate_revision=data["candidate_revision"],
                base_revision=data["base_revision"],
                readable_preview=data["readable_preview"],
                identity_digest=data["identity_digest"],
                preview_turn=data["preview_turn"],
                session_id=data.get("session_id"),
                issued_at=float(data["issued_at"]),
                ttl_seconds=int(data.get("ttl_seconds", 900)),
                as_of_source=data.get("as_of_source", "SYSTEM"),
                recorded_at=(
                    float(data["recorded_at"])
                    if data.get("recorded_at") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def consume(
        self,
        principal_id: str,
        *,
        receipt: str,
        expected_candidate_revision: str | None = None,
        expected_identity_digest: str | None = None,
    ) -> PendingReview | None:
        """Consume the pending review with optional CAS guards.

        If expected_candidate_revision or expected_identity_digest is provided,
        the on-disk review must match or the consume is rejected (returns None).
        Uses file locking to prevent TOCTOU races between load and write.
        """
        path = self._path(principal_id)
        try:
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 2 * 1024 * 1024)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            if data.get("terminal_receipt") is not None:
                return None
            # CAS checks
            if (
                expected_candidate_revision is not None
                and data.get("candidate_revision") != expected_candidate_revision
            ):
                return None
            if (
                expected_identity_digest is not None
                and data.get("identity_digest") != expected_identity_digest
            ):
                return None
            try:
                review = PendingReview(
                    candidate_snapshot=data["candidate_snapshot"],
                    candidate_revision=data["candidate_revision"],
                    base_revision=data["base_revision"],
                    readable_preview=data["readable_preview"],
                    identity_digest=data["identity_digest"],
                    preview_turn=data["preview_turn"],
                    session_id=data.get("session_id"),
                    issued_at=float(data["issued_at"]),
                    ttl_seconds=int(data.get("ttl_seconds", 900)),
                    as_of_source=data.get("as_of_source", "SYSTEM"),
                    recorded_at=(
                        float(data["recorded_at"])
                        if data.get("recorded_at") is not None
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError):
                return None
            data["terminal_receipt"] = receipt
            payload = json.dumps(
                data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            tmp = None
            try:
                tmp_fd, tmp = tempfile.mkstemp(
                    dir=self._dir,
                    prefix=".pending-",
                    suffix=".tmp",
                )
                os.fchmod(tmp_fd, 0o600)
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(tmp_fd, remaining)
                    if written <= 0:
                        raise OSError("write made no progress")
                    remaining = remaining[written:]
                os.fsync(tmp_fd)
                os.close(tmp_fd)
                os.rename(tmp, path)
                tmp = None
            finally:
                if tmp is not None:
                    with suppress(FileNotFoundError):
                        os.unlink(tmp)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return review

    def confirm_under_lock(
        self,
        principal_id: str,
        *,
        publish: Callable[[PendingReview], tuple[str, str | None, tuple[str, ...]]],
    ) -> tuple[PendingReview | None, str, str | None, tuple[str, ...]]:
        """Load → publish → consume under a single per-principal lock.

        The ``publish`` callback receives the loaded review and returns
        ``(status, candidate_revision, reason_codes)`` where status is one of
        "PUBLISHED", "UNCHANGED", or a failure status.

        Returns ``(review, status, candidate_revision, reason_codes)``.  If
        the review was consumed, ``review`` is the consumed review; if not
        (CAS mismatch, already consumed, etc.), ``review`` is None.
        """
        with self._locked(principal_id):
            review = self.load(principal_id)
            if review is None:
                return None, "NO_PENDING_REVIEW", None, ()
            status, candidate_revision, reason_codes = publish(review)
            if status in {"PUBLISHED", "UNCHANGED"}:
                receipt = (
                    f"published:{candidate_revision}"
                    if status == "PUBLISHED"
                    else f"unchanged:{candidate_revision}"
                )
                consumed = self.consume(
                    principal_id,
                    receipt=receipt,
                    expected_candidate_revision=review.candidate_revision,
                    expected_identity_digest=review.identity_digest,
                )
                if consumed is None:
                    return review, "OUTCOME_UNKNOWN", candidate_revision, (
                        "PENDING_CONSUME_FAILED",
                    )
                return consumed, status, candidate_revision, reason_codes
            return review, status, candidate_revision, reason_codes

    def clear_expired(self, principal_id: str) -> bool:
        review = self.load(principal_id)
        if review is not None and not review.expired:
            return False
        path = self._path(principal_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False


__all__ = ["PendingReviewStore"]
