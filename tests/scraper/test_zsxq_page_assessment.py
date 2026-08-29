"""RED contract tests for the pure PageAssessment module.

Interface contract: assess_page(PageEvidence) -> PageAssessment
with exactly eight stable states and precedence:
  control_failure > challenge > login_required > rate_limited >
  wrong_page > loading_timeout > dom_changed > ready

Only acceptable RED: collection-time ModuleNotFoundError for
fin_analyse.scraper.page_assessment.  No xfail, skip, or try/except.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_type_hints

from fin_analyse.scraper.page_assessment import (
    PageAssessment,
    PageEvidence,
    PageState,
    assess_page,
)

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "scraper" / "zsxq_pages"

_EXPECTED_STATES = [
    "control_failure",
    "challenge",
    "login_required",
    "rate_limited",
    "wrong_page",
    "loading_timeout",
    "dom_changed",
    "ready",
]

_ADJACENT_PAIRS: list[tuple[str, str]] = [
    ("control_failure", "challenge"),
    ("challenge", "login_required"),
    ("login_required", "rate_limited"),
    ("rate_limited", "wrong_page"),
    ("wrong_page", "loading_timeout"),
    ("loading_timeout", "dom_changed"),
    ("dom_changed", "ready"),
]

_EXPECTED_PATH = "/group/414118881818181"
_EXPECTED_IDENTITY = "zsxq-topic-feed"


def _load_page_states() -> dict:
    """Return the deserialized page_states.json fixture."""
    return json.loads((_FIXTURE_DIR / "page_states.json").read_text(encoding="utf-8"))


def _case_by_name(name: str) -> dict:
    data = _load_page_states()
    for c in data["cases"]:
        if c["name"] == name:
            return c
    raise KeyError(name)


def _ready_base(**overrides: object) -> PageEvidence:
    """Build a baseline ready PageEvidence, overridden by keyword args."""
    kwargs: dict[str, object] = {
        "expected_url_path": _EXPECTED_PATH,
        "observed_url_path": _EXPECTED_PATH,
        "expected_native_identity": _EXPECTED_IDENTITY,
        "observed_native_identity": _EXPECTED_IDENTITY,
        "document_ready_state": "complete",
        "loading_surface_stable": False,
        "control_failure_code": None,
        "challenge_present": False,
        "login_surface_present": False,
        "qr_scan_surface_present": False,
        "rate_limit_present": False,
        "retry_after_seconds": None,
        "visible_text": "",
        "document_title": "",
        "url_query": "",
        "url_fragment": "",
    }
    kwargs.update(overrides)
    return PageEvidence(**kwargs)


def _evidence_for_fixture_case(case: dict) -> PageEvidence:
    """Map a page_states.json case dict to a typed PageEvidence.

    Conflict names are made honest:
      valid_67              → ready base
      login_shell_67        → both login + QR, no challenge, path present, identity missing
      challenge_over_login  → independent challenge + login + QR, identity missing
      rate_over_wrong_page  → rate + retry 120 + present mismatched native identity
      loading_over_missing_dom → stable loading after interactive + missing native identity
      dom_changed           → correct path, missing identity, no stable loading
      wrong_page_over_loading → present mismatched identity + stable loading
      control_over_challenge  → extension_disconnected + independent challenge
    """
    name: str = case["name"]
    visible_text: str = case["visible_text"]

    if name == "valid_67":
        return _ready_base(visible_text=visible_text)

    elif name == "login_shell_67":
        return _ready_base(
            visible_text=visible_text,
            login_surface_present=True,
            qr_scan_surface_present=True,
            observed_native_identity=None,
        )

    elif name == "challenge_over_login":
        return _ready_base(
            visible_text=visible_text,
            challenge_present=True,
            login_surface_present=True,
            qr_scan_surface_present=True,
            observed_native_identity=None,
        )

    elif name == "rate_over_wrong_page":
        return _ready_base(
            visible_text=visible_text,
            rate_limit_present=True,
            retry_after_seconds=case["retry_after_seconds"],
            observed_native_identity="zsxq-login-page",
        )

    elif name == "loading_over_missing_dom":
        return _ready_base(
            visible_text=visible_text,
            loading_surface_stable=True,
            document_ready_state="interactive",
            observed_native_identity=None,
        )

    elif name == "dom_changed":
        return _ready_base(
            visible_text=visible_text,
            observed_native_identity=None,
        )

    elif name == "wrong_page_over_loading":
        return _ready_base(
            visible_text=visible_text,
            observed_native_identity="zsxq-login-page",
            loading_surface_stable=True,
            document_ready_state="interactive",
        )

    elif name == "control_over_challenge":
        return _ready_base(
            visible_text=visible_text,
            control_failure_code=case["control_failure_code"],
            challenge_present=True,
        )

    else:
        raise ValueError(f"Unknown fixture case name: {name!r}")


# ===================================================================
# 1. test_page_state_values_are_exact
# ===================================================================


def test_page_state_values_are_exact() -> None:
    """Assert exact PageState values/order and pin negative semantics.

    - Lone login (no QR) does NOT classify as login_required.
    - Lone QR (no login) does NOT classify as login_required.
    - Independent challenge_present DOES classify as challenge.
    - Stable loading with document_ready_state='loading' does NOT
      produce loading_timeout.
    """
    # Exact values and order
    assert [s.value for s in PageState] == _EXPECTED_STATES
    assert len(list(PageState)) == 8

    # PageAssessment.state must be the closed PageState type, not plain str
    assert get_type_hints(PageAssessment)["state"] is PageState, (
        "PageAssessment.state must be PageState"
    )
    assert isinstance(assess_page(_ready_base()).state, PageState)

    # Lone login (no QR) → not login_required
    r = assess_page(
        _ready_base(
            login_surface_present=True,
            qr_scan_surface_present=False,
        )
    )
    assert r.state != "login_required"
    assert r.state == "ready"

    # Lone QR (no login) → not login_required
    r = assess_page(
        _ready_base(
            login_surface_present=False,
            qr_scan_surface_present=True,
        )
    )
    assert r.state != "login_required"
    assert r.state == "ready"

    # Independent challenge (no login/QR) → challenge
    r = assess_page(_ready_base(challenge_present=True))
    assert r.state == "challenge"

    # Mismatched observed URL path → wrong_page (native identity stays matching baseline)
    r = assess_page(_ready_base(observed_url_path="/group/999999999999999"))
    assert r.state == "wrong_page"

    # Stable loading + document_ready_state='loading' → NOT loading_timeout
    r = assess_page(
        _ready_base(
            loading_surface_stable=True,
            document_ready_state="loading",
        )
    )
    assert r.state != "loading_timeout"
    assert r.state == "ready"


# ===================================================================
# 2. test_page_state_fixture_matrix
# ===================================================================


def test_page_state_fixture_matrix() -> None:
    """Load unchanged page_states.json, build typed PageEvidence for every
    fixture case, call assess_page, and assert the expected state.

    Also asserts:
    - rate_over_wrong_page retry_after_seconds == 120
    - control_over_challenge reason_code == 'extension_disconnected'
    - Both 67-character samples remain exactly 67 without text-as-signal.
    """
    data = _load_page_states()
    cases = data["cases"]
    by_name = {c["name"]: c for c in cases}

    # Exactly 8 cases with unique names
    assert len(cases) == 8
    assert len(by_name) == 8

    # Expected state per fixture case name
    expected = {
        "valid_67": "ready",
        "login_shell_67": "login_required",
        "challenge_over_login": "challenge",
        "rate_over_wrong_page": "rate_limited",
        "loading_over_missing_dom": "loading_timeout",
        "dom_changed": "dom_changed",
        "wrong_page_over_loading": "wrong_page",
        "control_over_challenge": "control_failure",
    }
    actual = {k: v["expected_state"] for k, v in by_name.items()}
    assert actual == expected

    # Build evidence and call assess_page for EVERY case
    for case in cases:
        evidence = _evidence_for_fixture_case(case)
        result = assess_page(evidence)
        assert result.state == case["expected_state"], (
            f"{case['name']!r}: expected {case['expected_state']}, got {result.state}"
        )

    # --- rate assessment: retry exactly 120 ---
    rate_case = by_name["rate_over_wrong_page"]
    rate_result = assess_page(_evidence_for_fixture_case(rate_case))
    assert rate_result.state == "rate_limited"
    assert rate_result.retry_after_seconds == 120

    # --- control assessment: reason_code exactly extension_disconnected ---
    ctrl_case = by_name["control_over_challenge"]
    ctrl_result = assess_page(_evidence_for_fixture_case(ctrl_case))
    assert ctrl_result.state == "control_failure"
    assert ctrl_result.reason_code == "extension_disconnected"

    # --- both 67-character samples remain exactly 67 ---
    valid_text = by_name["valid_67"]["visible_text"]
    login_text = by_name["login_shell_67"]["visible_text"]
    assert len(valid_text) == 67
    assert len(login_text) == 67

    # Prove text does not determine classification
    ready_from_login_text = assess_page(_ready_base(visible_text=login_text))
    assert ready_from_login_text.state == "ready"


# ===================================================================
# 3. test_equal_67_character_pages_follow_typed_signals
# ===================================================================


def test_equal_67_character_pages_follow_typed_signals() -> None:
    """Two 67-char pages classified by typed signals, not visible text.

    Swap visible text while preserving the same typed results, proving
    that QR does not mean challenge.
    """
    valid_text = _case_by_name("valid_67")["visible_text"]
    login_text = _case_by_name("login_shell_67")["visible_text"]
    assert len(valid_text) == 67
    assert len(login_text) == 67

    # Typed ready signals → ready
    ready_result = assess_page(_ready_base(visible_text=valid_text))
    assert ready_result.state == "ready"

    # Typed login_required signals (both login+QR, no challenge) → login_required
    login_result = assess_page(
        _ready_base(
            visible_text=login_text,
            login_surface_present=True,
            qr_scan_surface_present=True,
        )
    )
    assert login_result.state == "login_required"

    # Swap texts — same typed results, proving QR ≠ challenge
    ready_with_login_text = assess_page(_ready_base(visible_text=login_text))
    assert ready_with_login_text.state == "ready"

    login_with_valid_text = assess_page(
        _ready_base(
            visible_text=valid_text,
            login_surface_present=True,
            qr_scan_surface_present=True,
        )
    )
    assert login_with_valid_text.state == "login_required"


# ===================================================================
# 4. test_visible_text_length_never_changes_typed_result
# ===================================================================


def test_visible_text_length_never_changes_typed_result() -> None:
    """For lengths 0..4000 prove ready and login_required typed results
    and their fingerprints are invariant across text length."""
    lengths = [0, 1, 66, 67, 68, 500, 501, 1999, 2000, 4000]
    ready_fps: list[str] = []
    login_fps: list[str] = []

    for length in lengths:
        filler = "A" * length

        # Ready evidence
        rr = assess_page(_ready_base(visible_text=filler))
        assert rr.state == "ready", f"length={length}: expected ready, got {rr.state}"
        ready_fps.append(rr.evidence_fingerprint)

        # Login_required evidence (both login+QR)
        lr = assess_page(
            _ready_base(
                visible_text=filler,
                login_surface_present=True,
                qr_scan_surface_present=True,
            )
        )
        assert lr.state == "login_required", (
            f"length={length}: expected login_required, got {lr.state}"
        )
        login_fps.append(lr.evidence_fingerprint)

    # Fingerprints invariant across all lengths within the same state
    assert len(set(ready_fps)) == 1, "ready fingerprints vary with text length"
    assert len(set(login_fps)) == 1, "login_required fingerprints vary with text length"
    # Different states produce different fingerprints
    assert ready_fps[0] != login_fps[0]


# ===================================================================
# 5. test_page_state_priority
# ===================================================================


def test_page_state_priority() -> None:
    """Cover all seven adjacent precedence pairs.

    Each PageEvidence contains the higher-priority trigger while
    retaining the lower-priority trigger/baseline.  Dictionary merge
    is NOT used — every evidence is built explicitly so no signal
    is silently overwritten.
    """
    # Pair 1: control + challenge → control_failure
    r = assess_page(
        _ready_base(
            control_failure_code="extension_disconnected",
            challenge_present=True,
        )
    )
    assert r.state == "control_failure"

    # Pair 2: challenge + login + QR → challenge
    r = assess_page(
        _ready_base(
            challenge_present=True,
            login_surface_present=True,
            qr_scan_surface_present=True,
        )
    )
    assert r.state == "challenge"

    # Pair 3: login + QR + rate → login_required
    r = assess_page(
        _ready_base(
            login_surface_present=True,
            qr_scan_surface_present=True,
            rate_limit_present=True,
            retry_after_seconds=5,
        )
    )
    assert r.state == "login_required"

    # Pair 4: rate + identity mismatch → rate_limited
    r = assess_page(
        _ready_base(
            rate_limit_present=True,
            retry_after_seconds=30,
            observed_native_identity="zsxq-login-page",
        )
    )
    assert r.state == "rate_limited"

    # Pair 5: identity mismatch + stable loading → wrong_page
    r = assess_page(
        _ready_base(
            observed_native_identity="zsxq-login-page",
            loading_surface_stable=True,
            document_ready_state="interactive",
        )
    )
    assert r.state == "wrong_page"

    # Pair 6: stable loading + missing native identity → loading_timeout
    r = assess_page(
        _ready_base(
            loading_surface_stable=True,
            document_ready_state="complete",
            observed_native_identity=None,
        )
    )
    assert r.state == "loading_timeout"

    # Pair 7: missing native identity + otherwise-ready → dom_changed
    r = assess_page(
        _ready_base(
            observed_native_identity=None,
        )
    )
    assert r.state == "dom_changed"


# ===================================================================
# 6. test_page_assessment_wire_form_is_redacted_and_deterministic
# ===================================================================


def test_page_assessment_wire_form_is_redacted_and_deterministic() -> None:
    """PageAssessment.to_dict() redacted wire form contract.

    - Non-rate keys: state / reason_code / evidence_fingerprint.
    - Rate adds retry_after_seconds exactly 120.
    - Control wire reason_code is exactly 'extension_disconnected'.
    - Fingerprint matches [0-9a-f]{64} and is deterministic.
    - No raw visible text, title, query, fragment, native identity,
      or path evidence appears in the serialized wire.
    - Excluded-axis invariance: visible_text / document_title /
      url_query / url_fragment changes preserve state and fingerprint.
    - Included-axis sensitivity: different control codes or retry
      values produce different fingerprints with the same state.
    - No guessed literal digest assertion.
    """
    valid_text = _case_by_name("valid_67")["visible_text"]

    # --- Non-rate to_dict shape ---
    r = assess_page(_ready_base(visible_text=valid_text))
    d = r.to_dict()
    assert set(d.keys()) == {"state", "reason_code", "evidence_fingerprint"}
    assert isinstance(d["state"], str)
    assert isinstance(d["reason_code"], str)
    assert d["state"] in _EXPECTED_STATES

    # --- Rate to_dict shape (adds retry_after_seconds == 120) ---
    rr = assess_page(_ready_base(rate_limit_present=True, retry_after_seconds=120))
    rd = rr.to_dict()
    assert rd["retry_after_seconds"] == 120
    assert set(rd.keys()) == {
        "state",
        "reason_code",
        "evidence_fingerprint",
        "retry_after_seconds",
    }

    # --- Control wire reason_code is exactly extension_disconnected ---
    cr = assess_page(_ready_base(control_failure_code="extension_disconnected"))
    assert cr.reason_code == "extension_disconnected"
    cd = cr.to_dict()
    assert cd["reason_code"] == "extension_disconnected"
    assert cd["state"] == "control_failure"

    # --- Fingerprint matches [0-9a-f]{64} and is deterministic ---
    r1 = assess_page(_ready_base(visible_text=valid_text))
    r2 = assess_page(_ready_base(visible_text=valid_text))
    assert re.fullmatch(r"[0-9a-f]{64}", r1.evidence_fingerprint) is not None
    assert r1.evidence_fingerprint == r2.evidence_fingerprint

    # --- No raw evidence payload in serialized wire ---
    r3 = assess_page(
        _ready_base(
            visible_text="secret content 12345",
            document_title="sensitive title",
            url_query="token=abc123",
            url_fragment="#private",
        )
    )
    d3 = r3.to_dict()
    wire_str_values = [v for v in d3.values() if isinstance(v, str)]
    for sv in wire_str_values:
        assert "secret" not in sv
        assert "sensitive" not in sv
        assert "token" not in sv
        assert "abc123" not in sv
        assert "private" not in sv
        assert _EXPECTED_PATH not in sv
        assert _EXPECTED_IDENTITY not in sv

    # --- Excluded-axis invariance ---
    # visible_text, document_title, url_query, url_fragment changes
    # must NOT change state or fingerprint.
    a = assess_page(
        _ready_base(
            visible_text="AAAA",
            document_title="T1",
            url_query="?a=1",
            url_fragment="#x",
        )
    )
    b = assess_page(
        _ready_base(
            visible_text="BBBB",
            document_title="T2",
            url_query="?b=2",
            url_fragment="#y",
        )
    )
    assert a.state == b.state == "ready"
    assert a.evidence_fingerprint == b.evidence_fingerprint

    # --- Included-axis sensitivity: different control codes ---
    # Same state (control_failure), different fingerprints.
    c1 = assess_page(_ready_base(control_failure_code="extension_disconnected"))
    c2 = assess_page(_ready_base(control_failure_code="tab_crashed"))
    assert c1.state == c2.state == "control_failure"
    assert c1.evidence_fingerprint != c2.evidence_fingerprint

    # --- Included-axis sensitivity: different retry values ---
    # Same state (rate_limited), different fingerprints.
    rt1 = assess_page(_ready_base(rate_limit_present=True, retry_after_seconds=30))
    rt2 = assess_page(_ready_base(rate_limit_present=True, retry_after_seconds=60))
    assert rt1.state == rt2.state == "rate_limited"
    assert rt1.evidence_fingerprint != rt2.evidence_fingerprint
