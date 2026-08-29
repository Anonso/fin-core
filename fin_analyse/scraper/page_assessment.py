"""Pure typed page assessment classifier for ZSXQ scraper.

Contract: assess_page(PageEvidence) -> PageAssessment
with exactly eight stable states in fixed precedence order:
  control_failure > challenge > login_required > rate_limited >
  wrong_page > loading_timeout > dom_changed > ready
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PageState(StrEnum):
    """Eight stable page states in classification precedence order."""

    control_failure = "control_failure"
    challenge = "challenge"
    login_required = "login_required"
    rate_limited = "rate_limited"
    wrong_page = "wrong_page"
    loading_timeout = "loading_timeout"
    dom_changed = "dom_changed"
    ready = "ready"


@dataclass(frozen=True)
class PageEvidence:
    """Immutable typed evidence from a single page observation.

    All sixteen fields are required constructor arguments with no defaults.
    """

    expected_url_path: str
    observed_url_path: str | None
    expected_native_identity: str
    observed_native_identity: str | None
    document_ready_state: str
    loading_surface_stable: bool
    control_failure_code: str | None
    challenge_present: bool
    login_surface_present: bool
    qr_scan_surface_present: bool
    rate_limit_present: bool
    retry_after_seconds: int | None
    visible_text: str
    document_title: str
    url_query: str
    url_fragment: str


@dataclass(frozen=True)
class PageAssessment:
    """Immutable assessment result with a redacted wire form."""

    state: PageState
    reason_code: str
    evidence_fingerprint: str
    retry_after_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the redacted wire form.

        Non-rate keys: state, reason_code, evidence_fingerprint.
        Rate-limited adds retry_after_seconds with the typed value.
        """
        d: dict[str, Any] = {
            "state": self.state.value,
            "reason_code": self.reason_code,
            "evidence_fingerprint": self.evidence_fingerprint,
        }
        if self.state == PageState.rate_limited:
            d["retry_after_seconds"] = self.retry_after_seconds
        return d


# ---------------------------------------------------------------------------
# fingerprint helper
# ---------------------------------------------------------------------------

_FINGERPRINT_AXES: tuple[str, ...] = (
    "expected_url_path",
    "observed_url_path",
    "expected_native_identity",
    "observed_native_identity",
    "document_ready_state",
    "loading_surface_stable",
    "control_failure_code",
    "challenge_present",
    "login_surface_present",
    "qr_scan_surface_present",
    "rate_limit_present",
    "retry_after_seconds",
)


def _build_fingerprint(evidence: PageEvidence) -> str:
    """Return a deterministic lowercase SHA-256 hex digest over only the
    safe structural axes, serialised as canonical JSON with stable key
    ordering and compact separators.

    visible_text, document_title, url_query, and url_fragment are
    deliberately excluded so they can never influence classification.
    """
    axes: dict[str, Any] = {k: getattr(evidence, k) for k in _FINGERPRINT_AXES}
    canonical = json.dumps(axes, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------

_LOADING_TIMEOUT_STATES: frozenset[str] = frozenset({"interactive", "complete"})


def assess_page(evidence: PageEvidence) -> PageAssessment:
    """Classify page evidence with strict early-return precedence.

    Precedence (highest first):
      1. control_failure  — non-null control_failure_code
      2. challenge        — independent challenge_present
      3. login_required   — login_surface_present AND qr_scan_surface_present
      4. rate_limited     — rate_limit_present
      5. wrong_page       — URL path mismatch (including missing observed)
                            OR present native identity mismatch
      6. loading_timeout  — stable loading surface AND document_ready_state
                            in {"interactive", "complete"}
      7. dom_changed      — correct path with missing native identity
      8. ready            — matching baseline
    """
    fp = _build_fingerprint(evidence)

    # 1 — control failure
    if evidence.control_failure_code is not None:
        return PageAssessment(
            state=PageState.control_failure,
            reason_code=evidence.control_failure_code,
            evidence_fingerprint=fp,
        )

    # 2 — challenge (independent signal, never inferred from QR/login)
    if evidence.challenge_present:
        return PageAssessment(
            state=PageState.challenge,
            reason_code=PageState.challenge.value,
            evidence_fingerprint=fp,
        )

    # 3 — login required (BOTH surfaces must be present; alone is insufficient)
    if evidence.login_surface_present and evidence.qr_scan_surface_present:
        return PageAssessment(
            state=PageState.login_required,
            reason_code=PageState.login_required.value,
            evidence_fingerprint=fp,
        )

    # 4 — rate limited
    if evidence.rate_limit_present:
        return PageAssessment(
            state=PageState.rate_limited,
            reason_code=PageState.rate_limited.value,
            evidence_fingerprint=fp,
            retry_after_seconds=evidence.retry_after_seconds,
        )

    # 5 — wrong page (path mismatch, or present identity mismatch)
    if evidence.observed_url_path != evidence.expected_url_path:
        return PageAssessment(
            state=PageState.wrong_page,
            reason_code=PageState.wrong_page.value,
            evidence_fingerprint=fp,
        )
    if (
        evidence.observed_native_identity is not None
        and evidence.observed_native_identity != evidence.expected_native_identity
    ):
        return PageAssessment(
            state=PageState.wrong_page,
            reason_code=PageState.wrong_page.value,
            evidence_fingerprint=fp,
        )

    # 6 — loading timeout (stable loading + interactive/complete doc;
    #     a transient "loading" state must NOT trigger this)
    if evidence.loading_surface_stable and evidence.document_ready_state in _LOADING_TIMEOUT_STATES:
        return PageAssessment(
            state=PageState.loading_timeout,
            reason_code=PageState.loading_timeout.value,
            evidence_fingerprint=fp,
        )

    # 7 — DOM changed (correct path but missing native identity;
    #     distinct from a present mismatch which is wrong_page)
    if evidence.observed_native_identity is None:
        return PageAssessment(
            state=PageState.dom_changed,
            reason_code=PageState.dom_changed.value,
            evidence_fingerprint=fp,
        )

    # 8 — ready
    return PageAssessment(
        state=PageState.ready,
        reason_code=PageState.ready.value,
        evidence_fingerprint=fp,
    )
