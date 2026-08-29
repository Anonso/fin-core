"""Rebuild the cognition mainline read-model to follow the G Working Set identity.

The v1 single-revision read-model pins ``pit_working_set_identity`` at build
time.  The production G Working Set manifest identity changes whenever ingested
content changes; without a rebuild the PIT gate fails closed and the whole
cognition projection silently disappears from injection.  This module is the
reliability repair: after ingest, if the Working Set is READY and its identity
differs from the current artifact, rebuild ``generation + 1`` from the same
manual annotation document and republish through the frozen CAS publisher.
Any failure keeps the current artifact untouched (still fail-closed).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from fin_analyse.guo_teacher_research.cognition_mainline_readmodel import (
    _MANIFEST_NAME,
    CognitionMainlinePublisher,
    CognitionMainlineReadModelError,
    CognitionMainlineReadModelReader,
    generate_cognition_mainline_readmodel,
)

_SCHEMA_VERSION = "fin.cognition-mainline-rebuild/v1"


@dataclass(frozen=True)
class RebuildResult:
    """Content-free typed outcome; never the annotation body or article text."""

    schema_version: str = _SCHEMA_VERSION
    disposition: str = "FAILED"
    reason: str | None = None
    prior_identity: str | None = None
    candidate_identity: str | None = None
    generation: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "disposition": self.disposition,
            "reason": self.reason,
            "prior_identity": self.prior_identity,
            "candidate_identity": self.candidate_identity,
            "generation": self.generation,
        }


def _manifest_identity(manifest_path: Path) -> tuple[str, str] | None:
    """Return ``(status, canonical_sha256)`` or None when unreadable/invalid."""

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    status = payload.get("status")
    identity = payload.get("canonical_sha256")
    if (
        not isinstance(status, str)
        or not isinstance(identity, str)
        or len(identity) != 64
    ):
        return None
    return status, identity


def _artifact_state(readmodel_root: Path) -> tuple[str, int, str] | None:
    """Return ``(pit_working_set_identity, generation, file_sha256)`` or None."""

    reader = CognitionMainlineReadModelReader(readmodel_root)
    readout = reader.read()
    if not readout.payload:
        return None
    try:
        file_sha = hashlib.sha256(
            (readmodel_root / _MANIFEST_NAME).read_bytes()
        ).hexdigest()
    except OSError:
        return None
    return (
        str(readout.payload["pit_working_set_identity"]),
        int(readout.generation),
        file_sha,
    )


def rebuild_if_stale(
    *,
    annotation_path: str | Path,
    readmodel_root: str | Path,
    manifest_path: str | Path,
) -> RebuildResult:
    """Rebuild generation+1 when the READY Working Set identity moved on.

    Conditions (frozen with the user 2026-08-21):
      1. Working Set status must be READY;
      2. the rebuilt candidate must pass whole-payload validation;
      3. the identity must have actually changed.
    Any failure keeps the current artifact untouched (still fail-closed);
    the old identity is never silently injected under a new Working Set.
    """

    manifest = _manifest_identity(Path(manifest_path))
    if manifest is None:
        return RebuildResult(disposition="SKIPPED", reason="working_set_manifest_unreadable")
    status, manifest_identity = manifest
    if status != "READY":
        return RebuildResult(disposition="SKIPPED", reason="working_set_not_ready")

    current = _artifact_state(Path(readmodel_root))
    if current is None:
        return RebuildResult(disposition="SKIPPED", reason="readmodel_unavailable")
    prior_identity, current_generation, prior_file_sha = current
    if prior_identity == manifest_identity:
        return RebuildResult(
            disposition="ALREADY_CURRENT",
            prior_identity=prior_identity,
            candidate_identity=manifest_identity,
            generation=current_generation,
        )

    next_generation = current_generation + 1
    try:
        candidate = generate_cognition_mainline_readmodel(
            Path(annotation_path),
            generation=next_generation,
            working_set_identity=manifest_identity,
        )
    except (OSError, CognitionMainlineReadModelError, ValueError) as exc:
        return RebuildResult(
            disposition="FAILED",
            reason=f"annotation_invalid:{type(exc).__name__}",
            prior_identity=prior_identity,
            candidate_identity=manifest_identity,
            generation=next_generation,
        )

    try:
        publication = CognitionMainlinePublisher(Path(readmodel_root)).publish(
            candidate,
            expected_prior_identity=prior_file_sha,
        )
    except Exception as exc:  # noqa: BLE001 - publisher failure must stay typed
        return RebuildResult(
            disposition="FAILED",
            reason=f"publish_failed:{type(exc).__name__}",
            prior_identity=prior_identity,
            candidate_identity=manifest_identity,
            generation=next_generation,
        )

    return RebuildResult(
        disposition=publication.disposition,
        reason=publication.reason,
        prior_identity=prior_identity,
        candidate_identity=manifest_identity,
        generation=next_generation,
    )
