"""Rebuild the cognition mainline read-model to follow the G Working Set identity.

The v1 single-revision read-model pins ``pit_working_set_identity`` at build
time.  The production G Working Set manifest identity changes whenever ingested
content changes; without a rebuild the PIT gate fails closed and the whole
cognition projection silently disappears from injection.  This module is the
reliability repair: after ingest, if the Working Set is READY and the artifact
is stale, rebuild ``generation + 1`` from the same manual annotation document
and republish through the frozen CAS publisher.
Any failure keeps the current artifact untouched (still fail-closed).

Staleness conditions (first three frozen with the user 2026-08-21; the
annotation fingerprint added 2026-09-03, design gate g-mainline-growth-v1):
  1. Working Set status must be READY;
  2. the rebuilt candidate must pass whole-payload validation;
  3. the Working Set identity must have actually changed; or
  4. the annotation document (name + content) moved past the published
     baseline sidecar ``annotation.sha256`` — a missing sidecar is an unknown
     baseline and triggers exactly one self-healing rebuild.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
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
_SIDECAR_NAME = "annotation.sha256"


@dataclass(frozen=True)
class RebuildResult:
    """Content-free typed outcome; never the annotation body or article text."""

    schema_version: str = _SCHEMA_VERSION
    disposition: str = "FAILED"
    reason: str | None = None
    prior_identity: str | None = None
    candidate_identity: str | None = None
    generation: int | None = None
    # Which staleness condition fired (``identity_changed`` /
    # ``annotation_changed`` / ``both``); None when the outcome was decided
    # before the comparison (early SKIPPED).
    trigger: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "disposition": self.disposition,
            "reason": self.reason,
            "prior_identity": self.prior_identity,
            "candidate_identity": self.candidate_identity,
            "generation": self.generation,
            "trigger": self.trigger,
        }


def _annotation_fingerprint(annotation_path: Path) -> str | None:
    """SHA-256 over (document name, document bytes); None when unreadable.

    The document *name* is part of the fingerprint on purpose (design gate
    2026-09-03): a rename with unchanged content must still count as a change,
    so the artifact's ``annotation_ref`` can never keep pointing at a file
    that no longer exists.
    """

    try:
        body = annotation_path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256()
    digest.update(annotation_path.name.encode("utf-8"))
    digest.update(b"\n")
    digest.update(body)
    return digest.hexdigest()


def _sidecar_state(readmodel_root: Path) -> str | None:
    """Return the published annotation baseline, or None when unknown."""

    try:
        raw = (Path(readmodel_root) / _SIDECAR_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    raw = raw.strip()
    return raw if len(raw) == 64 else None


def _write_sidecar(readmodel_root: Path, fingerprint: str) -> None:
    """Persist the annotation baseline next to the published artifact (0600).

    Called only after a successful publication: the sidecar may lag the
    artifact (crash before the write -> one redundant self-healing rebuild
    next round), but it is never written for an artifact that does not
    already reflect the fingerprint.  A corrupt/partial sidecar reads back
    as an unknown baseline, which self-heals the same way.
    """

    path = Path(readmodel_root) / _SIDECAR_NAME
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, (fingerprint + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)


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

    Conditions (first three frozen with the user 2026-08-21; the annotation
    fingerprint added 2026-09-03, design gate g-mainline-growth-v1):
      1. Working Set status must be READY;
      2. the rebuilt candidate must pass whole-payload validation;
      3. the identity must have actually changed; or
      4. the annotation document (name + content) moved past the published
         baseline sidecar (missing sidecar = unknown baseline -> one
         self-healing rebuild).
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

    fingerprint = _annotation_fingerprint(Path(annotation_path))
    if fingerprint is None:
        return RebuildResult(
            disposition="SKIPPED",
            reason="annotation_unreadable",
            prior_identity=prior_identity,
            candidate_identity=manifest_identity,
            generation=current_generation,
        )
    identity_changed = prior_identity != manifest_identity
    annotation_changed = fingerprint != _sidecar_state(Path(readmodel_root))
    if not identity_changed and not annotation_changed:
        return RebuildResult(
            disposition="ALREADY_CURRENT",
            prior_identity=prior_identity,
            candidate_identity=manifest_identity,
            generation=current_generation,
        )
    trigger = (
        "both"
        if identity_changed and annotation_changed
        else "identity_changed"
        if identity_changed
        else "annotation_changed"
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
            trigger=trigger,
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
            trigger=trigger,
        )

    if publication.disposition == "PUBLISHED":
        # Baseline persists only behind a successful publication; failure to
        # write it self-heals as one redundant rebuild on the next round and
        # must never turn a published artifact into a reported failure.
        with suppress(OSError):
            _write_sidecar(Path(readmodel_root), fingerprint)

    return RebuildResult(
        disposition=publication.disposition,
        reason=publication.reason,
        prior_identity=prior_identity,
        candidate_identity=manifest_identity,
        generation=next_generation,
        trigger=trigger,
    )
