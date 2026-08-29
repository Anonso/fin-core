"""Package-owned production composition root for the Windows Chrome CDP scraper.

This is the single production wiring for the ZSXQ scraper module. It constructs
exactly one :class:`ZsxqScraperModule` over one :class:`ScraperRuntimeRepository`
and one :class:`WindowsChromeCdpAdapter`.

The composition is deliberately narrow:

- The factory accepts the SQLite/runtime location, an explicit mutable
  knowledge-base root, and the module's existing timing parameters. It refuses an
  adapter, runner, alternate-backend fallback or a window-days override — the
  fixed rolling 3-day incremental policy (``INCREMENTAL_WINDOW_DAYS = 3``) is
  owned by the CDP scraper and must never be overridable through this seam.
- Construction is inert: no scraper is built and Chrome is never touched until a
  run drives reconciliation.
- The CDP adapter is the only backend — there is no alternate browser-backend
  import or fallback here.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from fin_analyse.guo_teacher_research.g_working_set import (
        GWorkingSetPublicationEvidence,
    )

from .capture_artifact import CaptureArtifact
from .cdp_diagnostics import classify_cdp_error
from .contracts import (
    ReconcileOutcome,
    ZsxqHealth,
    ZsxqHealthRequest,
    ZsxqRunRequest,
    ZsxqRunResult,
)
from .module import ReconcileAdapter, ZsxqScraperModule
from .page_assessment import PageAssessment, PageEvidence, PageState, assess_page
from .runtime_repository import (
    ScraperRuntimeRepository,
    decode_capture_business_projection,
)

#: The two production scraper surfaces, keyed by the module run intent.
#: ``sync`` drives the rolling 3-day group timeline scan; ``watch`` drives the
#: lightweight star-columns/digests scan. Any other mode is rejected visibly
#: rather than silently coerced — a legacy combined trigger value never routes.
_MODE_TO_METHOD = {
    "sync": "run_incremental_with_result",
    "watch": "run_priority_scan",
}

_PROBE_GROUP_URL = "https://wx.zsxq.com/group/15522441811252"
_PROBE_GROUP_ORIGIN = "https://wx.zsxq.com"
_PROBE_GROUP_PATH = "/group/15522441811252"
_PROBE_GROUP_IDENTITY = "zsxq-group-timeline"
_PROBE_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "observed_origin",
        "observed_url_path",
        "url_query_present",
        "url_fragment_present",
        "observed_native_identity",
        "document_ready_state",
        "loading_surface_stable",
        "challenge_present",
        "login_surface_present",
        "qr_scan_surface_present",
        "rate_limit_present",
        "retry_after_seconds",
    }
)
_PROBE_NATIVE_IDENTITIES = frozenset({_PROBE_GROUP_IDENTITY, "zsxq-other-surface"})
_PROBE_CONTROL_FAILURE_CODES = frozenset(
    {
        "bridge_identity_required",
        "bridge_start_failed",
        "bridge_control_failed",
        "extension_disconnected",
        "tab_debugger_conflict",
        "target_extension_command_failed",
        "target_response_timeout",
        "targeted_collection_failed",
    }
)
_PASSIVE_RECONNECT_CODES = frozenset(
    {
        "bridge_start_failed",
        "extension_disconnected",
        "target_response_timeout",
    }
)
_PROBE_DOCUMENT_READY_STATES = frozenset({"loading", "interactive", "complete"})
_LIVE_PROOF_SCHEMA_VERSION = "fin.zsxq-live-proof/v1"
_PRODUCTION_CDP_PRODUCER_ID = "fin.zsxq-production-cdp/v1"
_G_WORKING_SET_PUBLICATION_SCHEMA = "fin.zsxq-g-working-set-publication/v1"
_NO_CHANGE_PRIOR_AFTER_RUN_STARTED_GAP = "g_working_set_no_change_prior_evaluated_after_run_started"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ProbeTabSnapshot:
    """Redacted scriptable-tab inventory; no raw ID, URL or title."""

    tab_fingerprints: tuple[str, ...]
    url_fingerprints: dict[str, str]
    active_tab_fingerprints: frozenset[str]
    window_fingerprints: frozenset[str]
    active_window_fingerprints: frozenset[str]
    tab_count: int


@dataclass(frozen=True)
class GWorkingSetPublicationReceipt:
    """Bounded proof of the post-crawl G publication/evaluation."""

    published: bool
    status: str
    generation: str | None
    evaluated_at: str | None
    source_refs: tuple[str, ...] = ()
    data_gaps: tuple[str, ...] = ()
    freshness: str = "UNKNOWN"
    source_coverage_sha256: str | None = None
    producer_id: str | None = None
    producer_run_id: str | None = None
    producer_run_status: str | None = None
    publication_mode: str | None = None
    prior_generation: str | None = None
    prior_source_refs: tuple[str, ...] | None = None
    prior_source_coverage_sha256: str | None = None
    prior_evaluated_at: str | None = None
    prior_freshness: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> GWorkingSetPublicationReceipt:
        """Strict inverse of :meth:`to_dict` for durable recovery state."""
        fields = {
            "schema_version",
            "published",
            "status",
            "generation",
            "evaluated_at",
            "source_refs",
            "data_gaps",
            "freshness",
            "source_coverage_sha256",
            "producer_id",
            "producer_run_id",
            "producer_run_status",
            "publication_mode",
            "prior_generation",
            "prior_source_refs",
            "prior_source_coverage_sha256",
            "prior_evaluated_at",
            "prior_freshness",
        }
        if set(value) != fields or value.get("schema_version") != _G_WORKING_SET_PUBLICATION_SCHEMA:
            raise ValueError("G working-set publication receipt is invalid")
        source_refs = _publication_string_tuple(
            value.get("source_refs"),
            maximum_items=20_000,
            maximum_length=512,
        )
        data_gaps = _publication_string_tuple(
            value.get("data_gaps"),
            maximum_items=64,
            maximum_length=256,
        )
        raw_prior_refs = value.get("prior_source_refs")
        prior_source_refs = (
            None
            if raw_prior_refs is None
            else _publication_string_tuple(
                raw_prior_refs,
                maximum_items=20_000,
                maximum_length=512,
            )
        )
        string_or_none = (
            "generation",
            "evaluated_at",
            "source_coverage_sha256",
            "producer_id",
            "producer_run_id",
            "producer_run_status",
            "publication_mode",
            "prior_generation",
            "prior_source_coverage_sha256",
            "prior_evaluated_at",
            "prior_freshness",
        )
        if (
            type(value.get("published")) is not bool
            or type(value.get("status")) is not str
            or type(value.get("freshness")) is not str
            or any(
                value.get(name) is not None and type(value.get(name)) is not str
                for name in string_or_none
            )
        ):
            raise ValueError("G working-set publication receipt is invalid")
        return cls(
            published=cast(bool, value["published"]),
            status=cast(str, value["status"]),
            generation=cast(str | None, value["generation"]),
            evaluated_at=cast(str | None, value["evaluated_at"]),
            source_refs=source_refs,
            data_gaps=data_gaps,
            freshness=cast(str, value["freshness"]),
            source_coverage_sha256=cast(str | None, value["source_coverage_sha256"]),
            producer_id=cast(str | None, value["producer_id"]),
            producer_run_id=cast(str | None, value["producer_run_id"]),
            producer_run_status=cast(str | None, value["producer_run_status"]),
            publication_mode=cast(str | None, value["publication_mode"]),
            prior_generation=cast(str | None, value["prior_generation"]),
            prior_source_refs=prior_source_refs,
            prior_source_coverage_sha256=cast(
                str | None,
                value["prior_source_coverage_sha256"],
            ),
            prior_evaluated_at=cast(str | None, value["prior_evaluated_at"]),
            prior_freshness=cast(str | None, value["prior_freshness"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _G_WORKING_SET_PUBLICATION_SCHEMA,
            "published": self.published,
            "status": self.status,
            "generation": self.generation,
            "evaluated_at": self.evaluated_at,
            "source_refs": list(self.source_refs),
            "data_gaps": list(self.data_gaps),
            "freshness": self.freshness,
            "source_coverage_sha256": self.source_coverage_sha256,
            "producer_id": self.producer_id,
            "producer_run_id": self.producer_run_id,
            "producer_run_status": self.producer_run_status,
            "publication_mode": self.publication_mode,
            "prior_generation": self.prior_generation,
            "prior_source_refs": (
                list(self.prior_source_refs) if self.prior_source_refs is not None else None
            ),
            "prior_source_coverage_sha256": self.prior_source_coverage_sha256,
            "prior_evaluated_at": self.prior_evaluated_at,
            "prior_freshness": self.prior_freshness,
        }


@dataclass(frozen=True)
class ProductionCdpCompletionReceipt:
    """Two-axis production result: crawl ledger plus fresh-G completion."""

    run: ZsxqRunResult
    completion_status: str
    g_working_set: GWorkingSetPublicationReceipt | None = None
    completion_data_gaps: tuple[str, ...] = ()

    def verified_completion_data_gaps(self) -> tuple[str, ...]:
        """Return fail-closed gaps for the terminal producer fact."""
        gaps = self.completion_data_gaps
        if (
            not gaps
            and self.run.status in {"succeeded", "no_change"}
            and self.g_working_set is not None
            and self.g_working_set.published
            and self.g_working_set.status == "READY"
        ):
            if _no_change_prior_evaluated_after_run_started(self.run, self.g_working_set):
                return (_NO_CHANGE_PRIOR_AFTER_RUN_STARTED_GAP,)
            if not _valid_ready_terminal_fact(self.run, self.g_working_set):
                return ("g_working_set_terminal_fact_invalid",)
        return gaps

    def verified_completion_status(self) -> str:
        """Derive status instead of trusting the caller-supplied projection."""
        return _production_completion_status(
            self.run,
            self.g_working_set,
            completion_data_gaps=self.verified_completion_data_gaps(),
        )


class WindowsChromeCdpAdapter:
    """Production :class:`ReconcileAdapter` backed by the Windows Chrome CDP scraper.

    Construction is inert — no scraper is built and Chrome is untouched until
    :meth:`run_incremental` drives a run. The rolling 3-day incremental window is
    owned by the CDP scraper and is deliberately not overridable here.

    ``scraper_factory`` is an internal test seam (a callable that builds a scraper
    given ``deadline_at``/``checkpoint``); production leaves it ``None`` so the real
    :class:`CdpBridgeScraper` is imported lazily and built on demand.
    """

    def __init__(
        self,
        *,
        knowledge_base_root: str | Path | None = None,
        scraper_factory: Callable[..., Any] | None = None,
        bridge_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._knowledge_base_root = (
            Path(knowledge_base_root) if knowledge_base_root is not None else None
        )
        self._scraper_factory = scraper_factory
        self._bridge_factory = bridge_factory
        #: Number of scrapers built so far — 0 until a run genuinely touches Chrome.
        self.scraper_builds = 0

    def run_incremental(
        self, *, mode: str, deadline_at: datetime, checkpoint: Callable[[], None]
    ) -> ReconcileOutcome:
        """Map the module trigger onto a CDP scraper surface and reconcile once.

        The mode is validated before anything is built, so an unknown mode fails
        visibly without touching Chrome. ``checkpoint`` is called before the run so
        an already-exhausted total deadline stops the adapter before it starts the
        browser; ``deadline_at``/``checkpoint`` are threaded into the scraper to cap
        its own bounded waits.
        """
        method_name = self._method_for_mode(mode)
        # Cooperative deadline: reject an expired budget before touching Chrome.
        checkpoint()
        try:
            scraper = self._build_scraper(deadline_at=deadline_at, checkpoint=checkpoint)
            with scraper as active:
                result = getattr(active, method_name)()
            return _to_reconcile_outcome(result)
        except Exception as exc:
            _record_cdp_adapter_failure(exc)
            raise

    def probe_page(self, *, deadline_at: datetime) -> PageAssessment:
        """Collect bounded, redacted PageEvidence from one existing group tab.

        The probe owns at most two bridge lifecycles: one initial attempt and, for
        a narrow transport-disconnect allowlist only, one passive reconnect under
        the same absolute deadline. It never navigates, focuses, reloads, opens,
        closes or scrolls a browser tab. A strict before/after tab snapshot and
        in-page URL check fence every collection. Any ambiguity, malformed output,
        inventory drift or cleanup failure propagates fail-closed.
        """
        assessment = self._probe_page_once(deadline_at=deadline_at)
        if (
            assessment.state is PageState.control_failure
            and assessment.reason_code in _PASSIVE_RECONNECT_CODES
            and datetime.now(UTC) < deadline_at
        ):
            assessment = self._probe_page_once(deadline_at=deadline_at)
        return assessment

    def _probe_page_once(self, *, deadline_at: datetime) -> PageAssessment:
        """Run one read-only bridge lifecycle; callers own bounded recovery."""
        bridge = self._build_probe_bridge(deadline_at=deadline_at)
        try:
            if bridge.start() is not True:
                control_failure_code = _probe_control_failure_code(
                    bridge.probe_control_failure_code()
                    if hasattr(bridge, "probe_control_failure_code")
                    else None
                )
                if control_failure_code is not None:
                    return _control_failure_assessment(control_failure_code)
                raise RuntimeError("CDP probe start failed")
            try:
                before, selected_tab_id, selected_fingerprint = _normalize_tab_snapshot(
                    bridge.get_browser_tab_inventory(), select_target=True
                )
                _validate_active_per_window(before)
                if selected_tab_id is None or selected_fingerprint is None:
                    raise RuntimeError("CDP probe requires exactly one allowlisted ZSXQ group tab")
                _validate_selected_target(before, selected_fingerprint)

                payload: object = None
                collection_error: Exception | None = None
                try:
                    payload = bridge.collect_page_evidence_on_tab(selected_tab_id)
                except Exception as exc:
                    collection_error = exc

                after, _, _ = _normalize_tab_snapshot(
                    bridge.get_browser_tab_inventory(), select_target=False
                )
                _validate_snapshot_invariant(before, after, selected_fingerprint)
                if collection_error is not None:
                    raise collection_error
                return assess_page(_page_evidence_from_payload(payload))
            except Exception as exc:
                control_failure_code = _probe_control_failure_code(getattr(exc, "code", None))
                if control_failure_code is not None:
                    return _control_failure_assessment(control_failure_code)
                raise
        finally:
            bridge.close()

    @staticmethod
    def _method_for_mode(mode: str) -> str:
        try:
            return _MODE_TO_METHOD[mode]
        except KeyError:
            raise ValueError(
                f"WindowsChromeCdpAdapter: unknown scrape mode {mode!r}; "
                f"expected one of {sorted(_MODE_TO_METHOD)}"
            ) from None

    def _build_scraper(self, *, deadline_at: datetime, checkpoint: Callable[[], None]) -> Any:
        self.scraper_builds += 1
        kwargs: dict[str, Any] = {
            "deadline_at": deadline_at,
            "checkpoint": checkpoint,
        }
        if self._knowledge_base_root is not None:
            kwargs["knowledge_base_root"] = self._knowledge_base_root
        if self._scraper_factory is not None:
            return self._scraper_factory(**kwargs)
        from .cdp_scraper import CdpBridgeScraper
        from .opencli_bridge_client import OpenCliBridgeClient

        return CdpBridgeScraper(
            **kwargs,
            client_factory=lambda **client_kwargs: OpenCliBridgeClient(**client_kwargs),
        )

    def _build_probe_bridge(self, *, deadline_at: datetime) -> Any:
        if self._bridge_factory is not None:
            return self._bridge_factory(deadline_at=deadline_at)
        from .opencli_bridge_client import OpenCliBridgeClient

        return OpenCliBridgeClient(
            startup_wait=35.0,
            max_retries=0,
            purpose="probe",
            deadline_at=deadline_at,
        )


def _normalize_probe_tab_id(value: object) -> str:
    if isinstance(value, bool):
        raise RuntimeError("CDP browser tab inventory is malformed")
    if isinstance(value, int):
        normalized = str(value)
    elif isinstance(value, str):
        normalized = value
    else:
        raise RuntimeError("CDP browser tab inventory is malformed")
    if (
        not normalized
        or not normalized.isascii()
        or not normalized.isdecimal()
        or normalized != normalized.strip()
        or not 0 < int(normalized) <= 2_147_483_647
    ):
        raise RuntimeError("CDP browser tab inventory is malformed")
    return normalized


def _normalize_probe_window_id(value: object) -> int:
    if type(value) is not int or not 0 < value <= 2_147_483_647:
        raise RuntimeError("CDP browser tab inventory is malformed")
    return value


def _normalize_tab_snapshot(
    raw_tabs: object, *, select_target: bool
) -> tuple[_ProbeTabSnapshot, str | None, str | None]:
    if not isinstance(raw_tabs, list) or not 0 < len(raw_tabs) <= 256:
        raise RuntimeError("CDP browser tab inventory is malformed")
    tab_fingerprints: list[str] = []
    seen_tab_ids: set[str] = set()
    url_fingerprints: dict[str, str] = {}
    active_tab_fingerprints: set[str] = set()
    window_fingerprints: set[str] = set()
    active_window_fingerprints: set[str] = set()
    target_tabs: list[tuple[str, str]] = []
    for raw_tab in raw_tabs:
        if not isinstance(raw_tab, dict):
            raise RuntimeError("CDP browser tab inventory is malformed")
        if set(raw_tab) != {"tabId", "windowId", "url", "active"}:
            raise RuntimeError("CDP browser tab inventory is malformed")
        tab_id = _normalize_probe_tab_id(raw_tab.get("tabId"))
        window_id = _normalize_probe_window_id(raw_tab.get("windowId"))
        url = raw_tab.get("url")
        active = raw_tab.get("active")
        if (
            not isinstance(url, str)
            or not 0 < len(url) <= 2048
            or type(active) is not bool
            or tab_id in seen_tab_ids
        ):
            raise RuntimeError("CDP browser tab inventory is malformed")
        identity_fingerprint = hashlib.sha256(
            f"tab\0{window_id}\0{tab_id}".encode("ascii")
        ).hexdigest()
        window_fingerprint = hashlib.sha256(f"window\0{window_id}".encode("ascii")).hexdigest()
        tab_fingerprints.append(identity_fingerprint)
        window_fingerprints.add(window_fingerprint)
        seen_tab_ids.add(tab_id)
        url_fingerprints[identity_fingerprint] = hashlib.sha256(url.encode("utf-8")).hexdigest()
        if active:
            active_tab_fingerprints.add(identity_fingerprint)
            active_window_fingerprints.add(window_fingerprint)
        if select_target and url == _PROBE_GROUP_URL:
            # The raw tab id is an ephemeral execution handle only. It is returned
            # to the immediate call site but never stored in the retained snapshot.
            target_tabs.append((tab_id, identity_fingerprint))
    if select_target and len(target_tabs) != 1:
        raise RuntimeError("CDP probe requires exactly one allowlisted ZSXQ group tab")
    return (
        _ProbeTabSnapshot(
            # Preserve extension inventory order so even a reorder is visible to
            # the before/after read-only fence. Only hashes survive this boundary.
            tab_fingerprints=tuple(tab_fingerprints),
            url_fingerprints=url_fingerprints,
            active_tab_fingerprints=frozenset(active_tab_fingerprints),
            window_fingerprints=frozenset(window_fingerprints),
            active_window_fingerprints=frozenset(active_window_fingerprints),
            tab_count=len(tab_fingerprints),
        ),
        target_tabs[0][0] if target_tabs else None,
        target_tabs[0][1] if target_tabs else None,
    )


def _validate_active_per_window(snapshot: _ProbeTabSnapshot) -> None:
    if snapshot.active_window_fingerprints != snapshot.window_fingerprints or len(
        snapshot.active_tab_fingerprints
    ) != len(snapshot.window_fingerprints):
        raise RuntimeError("CDP browser tab inventory active-per-window state is malformed")


def _validate_selected_target(snapshot: _ProbeTabSnapshot, selected_fingerprint: str) -> None:
    if selected_fingerprint not in snapshot.tab_fingerprints:
        raise RuntimeError("CDP probe selected tab changed during collection")


def _validate_snapshot_invariant(
    before: _ProbeTabSnapshot,
    candidate: _ProbeTabSnapshot,
    selected_fingerprint: str,
) -> None:
    if before.tab_fingerprints != candidate.tab_fingerprints:
        raise RuntimeError("CDP probe tab set changed during collection")
    if before.url_fingerprints != candidate.url_fingerprints:
        raise RuntimeError("CDP probe URL fingerprint changed during collection")
    if before.active_tab_fingerprints != candidate.active_tab_fingerprints:
        raise RuntimeError("CDP probe active tab set changed during collection")
    _validate_selected_target(candidate, selected_fingerprint)
    if before.window_fingerprints != candidate.window_fingerprints:
        raise RuntimeError("CDP probe window set changed during collection")
    if before.active_window_fingerprints != candidate.active_window_fingerprints:
        raise RuntimeError("CDP probe active window set changed during collection")
    if before.tab_count != candidate.tab_count:
        raise RuntimeError("CDP probe tab count changed during collection")
    _validate_active_per_window(candidate)


def _probe_control_failure_code(raw_code: object) -> str | None:
    value = getattr(raw_code, "value", raw_code)
    if isinstance(value, str) and value in _PROBE_CONTROL_FAILURE_CODES:
        return value
    return None


def _control_failure_assessment(code: str) -> PageAssessment:
    """Build a fully typed, redacted observation when page collection cannot run."""
    return assess_page(
        PageEvidence(
            expected_url_path=_PROBE_GROUP_PATH,
            observed_url_path=None,
            expected_native_identity=_PROBE_GROUP_IDENTITY,
            observed_native_identity=None,
            document_ready_state="loading",
            loading_surface_stable=False,
            control_failure_code=code,
            challenge_present=False,
            login_surface_present=False,
            qr_scan_surface_present=False,
            rate_limit_present=False,
            retry_after_seconds=None,
            visible_text="",
            document_title="",
            url_query="",
            url_fragment="",
        )
    )


def _require_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload[key]
    if type(value) is not bool:
        raise RuntimeError("CDP page evidence payload is malformed")
    return value


def _page_evidence_from_payload(raw_payload: object) -> PageEvidence:
    if not isinstance(raw_payload, dict) or set(raw_payload) != _PROBE_PAYLOAD_KEYS:
        raise RuntimeError("CDP page evidence payload is malformed")
    payload: dict[str, Any] = raw_payload
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise RuntimeError("CDP page evidence payload is malformed")
    if (
        payload["observed_origin"] != _PROBE_GROUP_ORIGIN
        or payload["observed_url_path"] != _PROBE_GROUP_PATH
        or _require_bool(payload, "url_query_present")
        or _require_bool(payload, "url_fragment_present")
    ):
        raise RuntimeError("CDP page evidence payload is malformed")

    native_identity = payload["observed_native_identity"]
    if native_identity is not None and native_identity not in _PROBE_NATIVE_IDENTITIES:
        raise RuntimeError("CDP page evidence payload is malformed")
    ready_state = payload["document_ready_state"]
    if ready_state not in _PROBE_DOCUMENT_READY_STATES:
        raise RuntimeError("CDP page evidence payload is malformed")
    retry_after_seconds = payload["retry_after_seconds"]
    if retry_after_seconds is not None and (
        type(retry_after_seconds) is not int or not 0 <= retry_after_seconds <= 86400
    ):
        raise RuntimeError("CDP page evidence payload is malformed")

    return PageEvidence(
        expected_url_path=_PROBE_GROUP_PATH,
        observed_url_path=payload["observed_url_path"],
        expected_native_identity=_PROBE_GROUP_IDENTITY,
        observed_native_identity=native_identity,
        document_ready_state=ready_state,
        loading_surface_stable=_require_bool(payload, "loading_surface_stable"),
        control_failure_code=None,
        challenge_present=_require_bool(payload, "challenge_present"),
        login_surface_present=_require_bool(payload, "login_surface_present"),
        qr_scan_surface_present=_require_bool(payload, "qr_scan_surface_present"),
        rate_limit_present=_require_bool(payload, "rate_limit_present"),
        retry_after_seconds=retry_after_seconds,
        visible_text="",
        document_title="",
        url_query="",
        url_fragment="",
    )


def _to_reconcile_outcome(result: Any) -> ReconcileOutcome:
    """Convert a scraper ``ScrapeResult`` into the module's ``ReconcileOutcome``.

    An incomplete scraper result is a failed reconciliation even when it saved
    no articles.  Fail closed before projecting counts so the module cannot
    persist it as ``NO_CHANGE``.

    Only the two fields the control ledger consumes cross the seam: the newly
    saved article count becomes ``changed_count`` and the scraper's warnings pass
    through verbatim. The scrape-specific window/diagnostic fields stay inside the
    scraper — the module contract is unchanged.
    """
    if result.scrape_completed is not True:
        failure_kind = result.failure_kind or "scrape_incomplete"
        raise RuntimeError(f"CDP scrape did not complete [{failure_kind}]")
    incomplete_publication = any(
        isinstance(warning, str)
        and (
            warning.startswith(
                (
                    "priority_surface_failed:",
                    "priority_events_failed:",
                    "deep_read_artifacts_failed:",
                    "[DEEP-READ]",
                )
            )
            or warning == "g_working_set_support_repair_failed"
        )
        for warning in result.warnings
    )
    if incomplete_publication:
        raise RuntimeError("CDP scrape completed with incomplete publications")
    return ReconcileOutcome(
        changed_count=int(result.new_count),
        warnings=list(result.warnings),
    )


def _same_live_proof_observation(left: ZsxqHealth, right: ZsxqHealth) -> bool:
    return (
        left.observed_at != ""
        and left.observed_at == right.observed_at
        and left.page_state == right.page_state
        and left.reason_code == right.reason_code
        and left.health_episode_id == right.health_episode_id
    )


def _build_production_cdp_components(
    *,
    runtime_db_path: str | Path,
    knowledge_base_root: str | Path | None = None,
    clock: Callable[[], datetime] | None = None,
    stale_after_seconds: float = 120.0,
    adapter: ReconcileAdapter | None = None,
) -> tuple[ZsxqScraperModule, ScraperRuntimeRepository]:
    repository = ScraperRuntimeRepository(runtime_db_path)
    if adapter is None:
        if knowledge_base_root is None:
            adapter = WindowsChromeCdpAdapter()
        else:
            adapter = WindowsChromeCdpAdapter(knowledge_base_root=knowledge_base_root)
    module = ZsxqScraperModule(
        repository=repository,
        adapter=adapter,
        clock=clock,
        stale_after_seconds=stale_after_seconds,
    )
    return module, repository


def build_production_cdp_module(
    *,
    runtime_db_path: str | Path,
    knowledge_base_root: str | Path,
    clock: Callable[[], datetime] | None = None,
    stale_after_seconds: float = 120.0,
) -> ZsxqScraperModule:
    """Wire the single production ZSXQ scraper module over the CDP adapter.

    The explicit knowledge-base root keeps mutable production content outside an
    immutable release checkout. The factory deliberately exposes no
    adapter/runner injection, alternate browser-backend fallback or window-days
    override.
    """
    module, _repository = _build_production_cdp_components(
        runtime_db_path=runtime_db_path,
        knowledge_base_root=knowledge_base_root,
        clock=clock,
        stale_after_seconds=stale_after_seconds,
    )
    return module


def _run_once_with_adapter(
    *,
    runtime_db_path: str | Path,
    knowledge_base_root: str | Path,
    request: ZsxqRunRequest,
    adapter: ReconcileAdapter,
) -> ProductionCdpCompletionReceipt:
    """Run one reconciliation through the canonical module facade with an adapter.

    Shared lifecycle seam for the live-browser run (``run_production_cdp_once``)
    and the capture-artifact ingest (``run_capture_ingest_once``): callers never
    bind to repository internals and the SQLite connection is closed
    deterministically after every invocation. Once a non-coalesced run is terminal
    and closed, this seam publishes and re-evaluates the G working set. The crawl
    ledger remains unchanged, while the returned completion receipt makes READY a
    separate machine-verifiable success gate. The request's remaining deadline
    budget includes pre-run prior-G evaluation and composition setup; those steps
    cannot reset the module to a fresh full budget.
    """
    deadline_budget_started_at = monotonic()
    prior_g_assessment = _evaluate_g_working_set_before_run(
        knowledge_base_root=knowledge_base_root,
    )
    module, repository = _build_production_cdp_components(
        runtime_db_path=runtime_db_path,
        knowledge_base_root=knowledge_base_root,
        adapter=adapter,
    )
    try:
        elapsed_setup_seconds = max(0.0, monotonic() - deadline_budget_started_at)
        result = module.run(
            replace(
                request,
                deadline_seconds=request.deadline_seconds - elapsed_setup_seconds,
            )
        )
    finally:
        repository.close()
    g_receipt = _publish_g_working_set_after_terminal_run(
        knowledge_base_root=knowledge_base_root,
        result=result,
    )
    g_receipt = _bind_no_change_prior_evidence(
        run=result,
        g_receipt=g_receipt,
        prior_g_assessment=prior_g_assessment,
    )
    completion_data_gaps = _production_completion_data_gaps(
        run=result,
        g_receipt=g_receipt,
        prior_g_assessment=prior_g_assessment,
    )
    return ProductionCdpCompletionReceipt(
        run=result,
        completion_status=_production_completion_status(
            result,
            g_receipt,
            completion_data_gaps=completion_data_gaps,
        ),
        g_working_set=g_receipt,
        completion_data_gaps=completion_data_gaps,
    )


def run_production_cdp_once(
    *,
    runtime_db_path: str | Path,
    knowledge_base_root: str | Path,
    request: ZsxqRunRequest,
) -> ProductionCdpCompletionReceipt:
    """Run one live-browser production reconciliation (canonical default adapter)."""
    return _run_once_with_adapter(
        runtime_db_path=runtime_db_path,
        knowledge_base_root=knowledge_base_root,
        request=request,
        adapter=WindowsChromeCdpAdapter(knowledge_base_root=knowledge_base_root),
    )


def run_capture_ingest_once(
    *,
    runtime_db_path: str | Path,
    knowledge_base_root: str | Path,
    artifact: CaptureArtifact,
    request: ZsxqRunRequest,
) -> ProductionCdpCompletionReceipt:
    """Run one capture-artifact ingest through the same module facade.

    ``artifact`` 必须是调用方已校验的 CaptureArtifact 对象（F-04：校验/导入/
    receipt/归档绑定同一身份，禁止在两次读取间换文件）。校验失败由调用方
    （capture_ingest CLI）fail-fast——不建 ledger 行、不刷新 G。adapter 把录制
    capture 经既有 scraper pipeline 回放，解析/去重/覆盖/KB 写入与 G 发布语义
    与 live 运行完全一致。
    """
    from .capture_replay_client import CaptureReplayClient

    def _replay_scraper_factory(
        *, deadline_at: datetime, checkpoint: Callable[[], None], knowledge_base_root: str | Path
    ) -> Any:
        from .cdp_scraper import CdpBridgeScraper

        return CdpBridgeScraper(
            deadline_at=deadline_at,
            checkpoint=checkpoint,
            knowledge_base_root=knowledge_base_root,
            client_factory=lambda **kwargs: CaptureReplayClient(artifact, **kwargs),
            incremental_cutoff=artifact.cutoff,
        )

    adapter = WindowsChromeCdpAdapter(
        knowledge_base_root=knowledge_base_root,
        scraper_factory=_replay_scraper_factory,
    )
    return _run_capture_once_with_adapter(
        runtime_db_path=runtime_db_path,
        knowledge_base_root=knowledge_base_root,
        request=request,
        adapter=adapter,
        artifact=artifact,
    )


def _run_capture_once_with_adapter(
    *,
    runtime_db_path: str | Path,
    knowledge_base_root: str | Path,
    request: ZsxqRunRequest,
    adapter: ReconcileAdapter,
    artifact: CaptureArtifact,
) -> ProductionCdpCompletionReceipt:
    """Advance capture business once, or resume its durable terminal run."""
    deadline_budget_started_at = monotonic()
    prior_g_assessment: object | None = None
    module, repository = _build_production_cdp_components(
        runtime_db_path=runtime_db_path,
        knowledge_base_root=knowledge_base_root,
        adapter=adapter,
    )
    publication_plan: object | None = None
    try:
        record = repository.read_capture_ingest(
            artifact_run_id=artifact.run_id,
            content_sha256=artifact.content_sha256,
        )
        if record is None:
            raise RuntimeError("capture ingest claim is missing")
        prior_g_assessment = _capture_prior_g_evidence(record.prior_g_json)
        if record.phase in {"BUSINESS_TERMINAL", "PUBLICATION_PREPARED"}:
            result = _capture_business_result(record)
        elif record.phase == "CLAIMED":
            elapsed_setup_seconds = max(0.0, monotonic() - deadline_budget_started_at)
            result = module._run_capture(
                replace(
                    request,
                    deadline_seconds=request.deadline_seconds - elapsed_setup_seconds,
                ),
                artifact_run_id=artifact.run_id,
                content_sha256=artifact.content_sha256,
            )
        else:
            raise RuntimeError("capture ingest phase cannot enter business")
        if result.status in {"succeeded", "no_change"}:
            record = repository.read_capture_ingest(
                artifact_run_id=artifact.run_id,
                content_sha256=artifact.content_sha256,
            )
            if record is None:
                raise RuntimeError("capture ingest claim is missing")
            from fin_analyse.guo_teacher_research.g_working_set import (
                GWorkingSetPublicationPlan,
                GWorkingSetService,
            )

            if record.phase == "BUSINESS_TERMINAL":
                publication_plan = GWorkingSetService(
                    kb_root=Path(knowledge_base_root)
                ).prepare_publication(publication_at=datetime.now(UTC))
                record = repository.prepare_capture_publication(
                    artifact_run_id=artifact.run_id,
                    content_sha256=artifact.content_sha256,
                    ingest_run_id=result.run_id or "",
                    publication_plan_json=json.dumps(
                        publication_plan.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            elif record.phase == "PUBLICATION_PREPARED":
                if record.publication_plan_json is None:
                    raise RuntimeError("capture publication plan is missing")
                publication_plan = GWorkingSetPublicationPlan.from_dict(
                    json.loads(record.publication_plan_json)
                )
            else:
                raise RuntimeError("capture ingest phase cannot enter publication")
    finally:
        repository.close()
    g_receipt = _publish_g_working_set_after_terminal_run(
        knowledge_base_root=knowledge_base_root,
        result=result,
        publication_plan=publication_plan,
    )
    g_receipt = _bind_no_change_prior_evidence(
        run=result,
        g_receipt=g_receipt,
        prior_g_assessment=prior_g_assessment,
    )
    completion_data_gaps = _production_completion_data_gaps(
        run=result,
        g_receipt=g_receipt,
        prior_g_assessment=prior_g_assessment,
    )
    return ProductionCdpCompletionReceipt(
        run=result,
        completion_status=_production_completion_status(
            result,
            g_receipt,
            completion_data_gaps=completion_data_gaps,
        ),
        g_working_set=g_receipt,
        completion_data_gaps=completion_data_gaps,
    )


def _capture_business_result(record: object) -> ZsxqRunResult:
    business_json = getattr(record, "business_json", None)
    ingest_run_id = getattr(record, "ingest_run_id", None)
    if not isinstance(business_json, str) or not isinstance(ingest_run_id, str):
        raise RuntimeError("capture business terminal is incomplete")
    try:
        payload = decode_capture_business_projection(
            business_json,
            ingest_run_id=ingest_run_id,
        )
    except ValueError as error:
        raise RuntimeError("capture business terminal is invalid") from error
    status = payload["status"]
    request_id = payload["request_id"]
    intent = payload["intent"]
    trigger = payload["trigger"]
    run_id = payload["run_id"]
    changed_count = payload["changed_count"]
    attempt = payload["attempt"]
    started_at = payload["started_at"]
    finished_at = payload["finished_at"]
    failure_reason = payload.get("failure_reason")
    assert isinstance(status, str)
    assert isinstance(request_id, str)
    assert isinstance(intent, str)
    assert isinstance(trigger, str)
    assert isinstance(run_id, str)
    assert isinstance(started_at, str)
    assert isinstance(finished_at, str)
    assert isinstance(changed_count, int) and not isinstance(changed_count, bool)
    assert isinstance(attempt, int) and not isinstance(attempt, bool)
    assert failure_reason is None or isinstance(failure_reason, str)
    return ZsxqRunResult(
        status=status,
        request_id=request_id,
        intent=intent,
        trigger=trigger,
        coalesced=False,
        run_id=run_id,
        changed_count=changed_count,
        attempt=attempt,
        started_at=started_at,
        finished_at=finished_at,
        failure_reason=failure_reason,
    )


def _capture_prior_g_evidence(raw: str) -> GWorkingSetPublicationEvidence | None:
    """Restore only the bounded claim-time G publication evidence."""
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if payload == {}:
        return None
    try:
        from fin_analyse.guo_teacher_research.g_working_set import (
            GWorkingSetPublicationEvidence,
        )

        return GWorkingSetPublicationEvidence.from_dict(payload)
    except (TypeError, ValueError):
        return None


def _evaluate_g_working_set_before_run(
    *,
    knowledge_base_root: str | Path,
) -> object | None:
    """Read owner truth before a run so NO_CHANGE cannot invent continuity."""
    try:
        from fin_analyse.guo_teacher_research.g_working_set import GWorkingSetService

        return GWorkingSetService(kb_root=Path(knowledge_base_root)).evaluate()
    except Exception:
        return None


def _publish_g_working_set_after_terminal_run(
    *,
    knowledge_base_root: str | Path,
    result: ZsxqRunResult,
    publication_plan: object | None = None,
) -> GWorkingSetPublicationReceipt | None:
    """Publish/evaluate fresh G only after a run that actually collected.

    A non-collecting terminal run (failed / deadline_exceeded / interrupted)
    returns ``None``: it must neither re-stamp the manifest's ``evaluated_at``
    nor project ``FRESH`` next to a failed status. The prior manifest, if any,
    ages honestly and goes STALE on its own.
    """
    if result.status not in {"succeeded", "no_change"}:
        return None
    try:
        from fin_analyse.guo_teacher_research.g_working_set import (
            GWorkingSetPublicationDisposition,
            GWorkingSetPublicationPlan,
            GWorkingSetService,
            GWorkingSetStatus,
        )

        service = GWorkingSetService(kb_root=Path(knowledge_base_root))
        if publication_plan is None:
            assessment = service.reconcile_and_publish()
        else:
            if not isinstance(publication_plan, GWorkingSetPublicationPlan):
                raise TypeError("capture G publication plan is invalid")
            publication = service.compare_and_publish(publication_plan)
            if publication.disposition is GWorkingSetPublicationDisposition.REJECTED:
                return _unavailable_g_working_set_receipt(
                    f"g_working_set_publication_{str(publication.reason).lower()}",
                    run=result,
                )
            if publication.assessment is None:
                raise RuntimeError("capture G publication assessment is missing")
            assessment = publication.assessment
    except Exception:
        return _unavailable_g_working_set_receipt(
            "g_working_set_publish_failed",
            run=result,
        )
    try:
        status = assessment.status
        if status not in {
            GWorkingSetStatus.READY,
            GWorkingSetStatus.PARTIAL,
            GWorkingSetStatus.STALE,
            GWorkingSetStatus.MISSING,
        }:
            raise RuntimeError("unexpected G working-set status")
        evidence = assessment.to_publication_evidence()
        receipt = GWorkingSetPublicationReceipt(
            published=True,
            status=status.value,
            generation=evidence.generation,
            evaluated_at=evidence.evaluated_at,
            source_refs=evidence.source_refs,
            data_gaps=evidence.data_gaps,
            freshness=(
                "FRESH"
                if status is GWorkingSetStatus.READY
                else "STALE"
                if status is GWorkingSetStatus.STALE
                else "UNKNOWN"
            ),
            source_coverage_sha256=evidence.source_coverage_sha256,
            producer_id=_PRODUCTION_CDP_PRODUCER_ID,
            producer_run_id=result.run_id,
            producer_run_status=result.status,
            publication_mode=("NO_CHANGE" if result.status == "no_change" else "CURRENT_RUN"),
        )
    except Exception:
        return _unavailable_g_working_set_receipt(
            "g_working_set_publication_evidence_invalid",
            run=result,
        )
    _record_g_working_set_completion(
        f"published_{receipt.status.lower()}",
        warning=receipt.status != "READY",
    )
    return receipt


def _unavailable_g_working_set_receipt(
    reason: str,
    *,
    run: ZsxqRunResult,
) -> GWorkingSetPublicationReceipt:
    _record_g_working_set_completion(reason.removeprefix("g_working_set_"), warning=True)
    return GWorkingSetPublicationReceipt(
        published=False,
        status="UNAVAILABLE",
        generation=None,
        evaluated_at=None,
        data_gaps=(reason,),
        producer_id=_PRODUCTION_CDP_PRODUCER_ID,
        producer_run_id=run.run_id,
        producer_run_status=run.status,
        publication_mode=("NO_CHANGE" if run.status == "no_change" else "CURRENT_RUN"),
    )




def _export_g_publication_evidence(
    value: object | None,
) -> GWorkingSetPublicationEvidence | None:
    if value is None:
        return None
    try:
        from fin_analyse.guo_teacher_research.g_working_set import (
            GWorkingSetPublicationEvidence,
        )

        if isinstance(value, GWorkingSetPublicationEvidence):
            return value
        export = getattr(value, "to_publication_evidence", None)
        evidence = export() if callable(export) else None
        return evidence if isinstance(evidence, GWorkingSetPublicationEvidence) else None
    except (AttributeError, TypeError, ValueError):
        return None


def _bind_no_change_prior_evidence(
    *,
    run: ZsxqRunResult,
    g_receipt: GWorkingSetPublicationReceipt | None,
    prior_g_assessment: object | None,
) -> GWorkingSetPublicationReceipt | None:
    """Bind validated pre-run owner evidence into a NO_CHANGE receipt."""
    if run.status != "no_change" or g_receipt is None or not g_receipt.published:
        return g_receipt
    evidence = _export_g_publication_evidence(prior_g_assessment)
    if evidence is None:
        return g_receipt
    status = getattr(getattr(evidence, "status", None), "value", None)
    return replace(
        g_receipt,
        prior_generation=evidence.generation,
        prior_source_refs=evidence.source_refs,
        prior_source_coverage_sha256=evidence.source_coverage_sha256,
        prior_evaluated_at=evidence.evaluated_at,
        prior_freshness=(
            "FRESH" if status == "READY" else "STALE" if status == "STALE" else "UNKNOWN"
        ),
    )


def _production_completion_status(
    run: ZsxqRunResult,
    g_receipt: GWorkingSetPublicationReceipt | None,
    *,
    completion_data_gaps: tuple[str, ...] = (),
) -> str:
    """Derive end-to-end completion without mutating the canonical crawl run."""
    if run.status == "coalesced":
        return "coalesced"
    if run.status in {"failed", "deadline_exceeded", "interrupted"}:
        return "failed"
    if run.status == "partial":
        return "partial"
    if run.status not in {"succeeded", "no_change"}:
        return "failed"
    if g_receipt is None or not g_receipt.published:
        return "failed"
    if completion_data_gaps:
        return "failed"
    return "ready" if g_receipt.status == "READY" else "partial"


def _valid_ready_terminal_fact(
    run: ZsxqRunResult,
    receipt: GWorkingSetPublicationReceipt,
) -> bool:
    if (
        receipt.data_gaps
        or receipt.freshness != "FRESH"
        or not _completion_sha256(receipt.generation)
        or not _completion_sha256(receipt.source_coverage_sha256)
        or not receipt.source_refs
        or len(receipt.source_refs) != len(set(receipt.source_refs))
        or any(
            not source_ref
            or len(source_ref) > 512
            or source_ref != source_ref.strip()
            or not source_ref.isprintable()
            for source_ref in receipt.source_refs
        )
        or receipt.producer_id != _PRODUCTION_CDP_PRODUCER_ID
        or not isinstance(run.run_id, str)
        or not run.run_id
        or len(run.run_id) > 128
        or not run.run_id.isprintable()
        or receipt.producer_run_id != run.run_id
        or receipt.producer_run_status != run.status
    ):
        return False
    started_at = _completion_timestamp(run.started_at)
    finished_at = _completion_timestamp(run.finished_at)
    if started_at is None or finished_at is None or started_at > finished_at:
        return False
    if type(run.changed_count) is not int or (
        (run.status == "succeeded" and run.changed_count <= 0)
        or (run.status == "no_change" and run.changed_count != 0)
    ):
        return False
    evaluated_at = _completion_timestamp(receipt.evaluated_at)
    if evaluated_at is None or evaluated_at < finished_at:
        return False
    if run.status == "succeeded":
        return (
            receipt.publication_mode == "CURRENT_RUN"
            and receipt.prior_generation is None
            and receipt.prior_source_refs is None
            and receipt.prior_source_coverage_sha256 is None
            and receipt.prior_evaluated_at is None
            and receipt.prior_freshness is None
        )
    prior_evaluated_at = _completion_timestamp(receipt.prior_evaluated_at)
    return (
        receipt.publication_mode == "NO_CHANGE"
        and receipt.prior_generation == receipt.generation
        and receipt.prior_source_refs == receipt.source_refs
        and receipt.prior_source_coverage_sha256 == receipt.source_coverage_sha256
        and receipt.prior_freshness == "FRESH"
        and started_at is not None
        and prior_evaluated_at is not None
        and prior_evaluated_at <= started_at
        and prior_evaluated_at <= evaluated_at
    )


def _no_change_prior_evaluated_after_run_started(
    run: ZsxqRunResult,
    receipt: GWorkingSetPublicationReceipt,
) -> bool:
    if run.status != "no_change":
        return False
    started_at = _completion_timestamp(run.started_at)
    prior_evaluated_at = _completion_timestamp(receipt.prior_evaluated_at)
    return (
        started_at is not None
        and prior_evaluated_at is not None
        and prior_evaluated_at > started_at
    )


def _completion_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _publication_string_tuple(
    value: object,
    *,
    maximum_items: int,
    maximum_length: int,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > maximum_items
        or any(
            type(item) is not str
            or not item
            or len(item) > maximum_length
            or item != item.strip()
            or not item.isprintable()
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise ValueError("G working-set publication receipt is invalid")
    return tuple(value)


def _valid_persisted_g_publication(
    run: ZsxqRunResult,
    receipt: GWorkingSetPublicationReceipt,
) -> bool:
    expected_mode = "NO_CHANGE" if run.status == "no_change" else "CURRENT_RUN"
    started_at = _completion_timestamp(run.started_at)
    finished_at = _completion_timestamp(run.finished_at)
    prior_values = (
        receipt.prior_generation,
        receipt.prior_source_refs,
        receipt.prior_source_coverage_sha256,
        receipt.prior_evaluated_at,
        receipt.prior_freshness,
    )
    if (
        run.status not in {"succeeded", "no_change"}
        or receipt.producer_id != _PRODUCTION_CDP_PRODUCER_ID
        or receipt.producer_run_id != run.run_id
        or receipt.producer_run_status != run.status
        or receipt.publication_mode != expected_mode
        or started_at is None
        or finished_at is None
        or started_at > finished_at
        or type(run.changed_count) is not int
        or (run.status == "succeeded" and run.changed_count <= 0)
        or (run.status == "no_change" and run.changed_count != 0)
        or (any(value is None for value in prior_values) and any(
            value is not None for value in prior_values
        ))
    ):
        return False
    if not receipt.published:
        return (
            receipt.status == "UNAVAILABLE"
            and receipt.generation is None
            and receipt.evaluated_at is None
            and not receipt.source_refs
            and bool(receipt.data_gaps)
            and receipt.freshness == "UNKNOWN"
            and receipt.source_coverage_sha256 is None
            and all(value is None for value in prior_values)
        )
    expected_freshness = {
        "READY": "FRESH",
        "PARTIAL": "UNKNOWN",
        "STALE": "STALE",
        "MISSING": "UNKNOWN",
    }
    evaluated_at = _completion_timestamp(receipt.evaluated_at)
    if (
        receipt.status not in expected_freshness
        or receipt.freshness != expected_freshness[receipt.status]
        or not _completion_sha256(receipt.generation)
        or not _completion_sha256(receipt.source_coverage_sha256)
        or evaluated_at is None
        or evaluated_at < finished_at
        or (receipt.status == "READY" and receipt.data_gaps)
        or (run.status == "succeeded" and any(value is not None for value in prior_values))
    ):
        return False
    if all(value is not None for value in prior_values):
        prior_evaluated_at = _completion_timestamp(receipt.prior_evaluated_at)
        if (
            not _completion_sha256(receipt.prior_generation)
            or not _completion_sha256(receipt.prior_source_coverage_sha256)
            or prior_evaluated_at is None
            or prior_evaluated_at > evaluated_at
            or receipt.prior_freshness not in {"FRESH", "STALE", "UNKNOWN"}
        ):
            return False
    return True


def validate_persisted_capture_completion(
    *,
    business: Mapping[str, object],
    g_working_set: object,
    completion_status: str,
    completion_data_gaps: tuple[str, ...],
    prior_g_json: str,
    publication_plan_json: str | None,
) -> tuple[GWorkingSetPublicationReceipt | None, bool]:
    """Validate producer-owned completion truth before ledger terminalization."""
    status = cast(str, business["status"])
    run = ZsxqRunResult(
        status=status,
        request_id=cast(str, business["request_id"]),
        intent=cast(str, business["intent"]),
        trigger=cast(str, business["trigger"]),
        coalesced=False,
        run_id=cast(str, business["run_id"]),
        changed_count=cast(int, business["changed_count"]),
        attempt=cast(int, business["attempt"]),
        started_at=cast(str, business["started_at"]),
        finished_at=cast(str, business["finished_at"]),
        failure_reason=cast(str | None, business.get("failure_reason")),
    )
    if status in {"failed", "deadline_exceeded"}:
        if (
            g_working_set is not None
            or completion_status != "failed"
            or completion_data_gaps
        ):
            raise ValueError("capture completion terminal fact is invalid")
        return None, False
    if status not in {"succeeded", "no_change"}:
        raise ValueError("capture completion terminal fact is invalid")
    try:
        from fin_analyse.guo_teacher_research.g_working_set import (
            GWorkingSetPublicationEvidence,
            GWorkingSetPublicationPlan,
            GWorkingSetStatus,
        )

        publication_plan = GWorkingSetPublicationPlan.from_dict(
            json.loads(publication_plan_json) if publication_plan_json is not None else None
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("capture completion terminal fact is invalid") from error
    if g_working_set is None:
        if completion_status != "failed" or completion_data_gaps:
            raise ValueError("capture completion terminal fact is invalid")
        return None, False
    if not isinstance(g_working_set, Mapping):
        raise ValueError("capture completion terminal fact is invalid")
    receipt = GWorkingSetPublicationReceipt.from_dict(g_working_set)
    if not _valid_persisted_g_publication(run, receipt):
        raise ValueError("capture completion terminal fact is invalid")
    if receipt.published:
        try:
            receipt_evidence_sha256 = GWorkingSetPublicationEvidence(
                status=GWorkingSetStatus(receipt.status),
                generation=cast(str, receipt.generation),
                evaluated_at=cast(str, receipt.evaluated_at),
                source_refs=receipt.source_refs,
                source_coverage_sha256=cast(str, receipt.source_coverage_sha256),
                data_gaps=receipt.data_gaps,
            ).identity_sha256
        except (TypeError, ValueError) as error:
            raise ValueError("capture completion terminal fact is invalid") from error
        if (
            receipt.generation != publication_plan.expected_generation
            or receipt.source_coverage_sha256
            != publication_plan.expected_source_coverage_sha256
            or receipt.evaluated_at != publication_plan.publication_at
            or receipt_evidence_sha256 != publication_plan.expected_evidence_sha256
        ):
            raise ValueError("capture completion terminal fact is invalid")
    if status == "no_change" and receipt.published:
        prior_evidence = _capture_prior_g_evidence(prior_g_json)
        prior_status = getattr(getattr(prior_evidence, "status", None), "value", None)
        prior_freshness = (
            "FRESH" if prior_status == "READY" else "STALE" if prior_status == "STALE" else "UNKNOWN"
        )
        expected_prior = (
            None
            if prior_evidence is None
            else (
                prior_evidence.generation,
                prior_evidence.source_refs,
                prior_evidence.source_coverage_sha256,
                prior_evidence.evaluated_at,
                prior_freshness,
            )
        )
        actual_prior = (
            receipt.prior_generation,
            receipt.prior_source_refs,
            receipt.prior_source_coverage_sha256,
            receipt.prior_evaluated_at,
            receipt.prior_freshness,
        )
        if (expected_prior is None and any(value is not None for value in actual_prior)) or (
            expected_prior is not None and actual_prior != expected_prior
        ):
            raise ValueError("capture completion terminal fact is invalid")
    ready = receipt.published and receipt.status == "READY" and _valid_ready_terminal_fact(
        run,
        receipt,
    )
    if not receipt.published:
        valid_outcome = (
            completion_status == "failed"
            and completion_data_gaps == receipt.data_gaps
        )
    elif receipt.status == "READY":
        valid_outcome = (
            ready and completion_status == "ready" and not completion_data_gaps
        ) or (
            not ready and completion_status == "failed" and bool(completion_data_gaps)
        )
    else:
        valid_outcome = completion_status == "partial" and not completion_data_gaps
    if not valid_outcome:
        raise ValueError("capture completion terminal fact is invalid")
    return receipt, ready


def _production_completion_data_gaps(
    *,
    run: ZsxqRunResult,
    g_receipt: GWorkingSetPublicationReceipt | None,
    prior_g_assessment: object | None,
) -> tuple[str, ...]:
    if g_receipt is not None and not g_receipt.published:
        return g_receipt.data_gaps
    if (
        run.status not in {"succeeded", "no_change"}
        or g_receipt is None
        or not g_receipt.published
        or g_receipt.status != "READY"
    ):
        return ()
    run_finished_at = _completion_timestamp(run.finished_at)
    g_evaluated_at = _completion_timestamp(g_receipt.evaluated_at)
    if run_finished_at is None or g_evaluated_at is None:
        return ("g_working_set_terminal_timestamp_invalid",)
    if g_evaluated_at < run_finished_at:
        return ("g_working_set_evaluated_before_run_finished",)
    if run.status != "no_change":
        return (
            ()
            if _valid_ready_terminal_fact(run, g_receipt)
            else ("g_working_set_terminal_fact_invalid",)
        )
    prior_gaps = getattr(prior_g_assessment, "data_gaps", ())
    if prior_g_assessment is not None and "g_working_set_manifest_missing" in prior_gaps:
        return ("g_working_set_no_change_prior_manifest_missing",)
    prior_status = getattr(getattr(prior_g_assessment, "status", None), "value", None)
    if prior_status == "STALE":
        return ("g_working_set_no_change_prior_stale",)
    if prior_status != "READY":
        return ("g_working_set_no_change_prior_not_ready",)
    prior_evidence = _export_g_publication_evidence(prior_g_assessment)
    if prior_evidence is None:
        return ("g_working_set_no_change_prior_evidence_invalid",)
    if _no_change_prior_evaluated_after_run_started(run, g_receipt):
        return (_NO_CHANGE_PRIOR_AFTER_RUN_STARTED_GAP,)
    if prior_evidence.source_refs != g_receipt.source_refs:
        return ("g_working_set_no_change_source_refs_drift",)
    if prior_evidence.generation != g_receipt.generation:
        return ("g_working_set_no_change_generation_drift",)
    if prior_evidence.source_coverage_sha256 != g_receipt.source_coverage_sha256:
        return ("g_working_set_no_change_source_coverage_drift",)
    return (
        ()
        if _valid_ready_terminal_fact(run, g_receipt)
        else ("g_working_set_terminal_fact_invalid",)
    )


def _completion_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    offset = parsed.utcoffset()
    if (
        parsed.tzinfo is None
        or offset is None
        or offset.total_seconds() != 0
        or parsed.isoformat() != value
    ):
        return None
    return parsed


def _record_g_working_set_completion(status_code: str, *, warning: bool) -> None:
    """Record only a bounded status code; logging itself cannot alter the run."""
    try:
        if warning:
            _LOGGER.warning("g_working_set_completion_status=%s", status_code)
        else:
            _LOGGER.info("g_working_set_completion_status=%s", status_code)
    except Exception:
        return


def _record_cdp_adapter_failure(exc: Exception) -> None:
    """Record only the stable failure kind; preserve the adapter exception."""
    try:
        failure_kind = classify_cdp_error(str(exc))
        _LOGGER.warning("cdp_adapter_failure_kind=%s", failure_kind.value)
    except Exception:
        return


def run_production_cdp_live_proof(
    *,
    runtime_db_path: str | Path,
    deadline_at: datetime,
) -> dict[str, Any]:
    """Collect the internal v1 result for one read-only probe over an empty ledger.

    This package-owned composition/lifecycle seam owns the repository, adapter and
    close lifecycle. The formal CLI producer wraps this internal result with stable
    checkout identity and publishes the commit-bound v2 artifact, so callers never
    cross the :class:`ZsxqScraperModule` facade or bind to SQLite internals.
    """
    module, repository = _build_production_cdp_components(runtime_db_path=runtime_db_path)
    try:
        before_count = repository.health_observation_count()
        if before_count != 0:
            raise RuntimeError("ZSXQ live proof requires an empty runtime ledger")

        probed = module.health(ZsxqHealthRequest(probe=True, deadline_at=deadline_at))
        ledger_read = module.health(ZsxqHealthRequest(probe=False))
        after_count = repository.health_observation_count()
        lease_released = repository.get_active_lease() is None
        ledger_read_matches = _same_live_proof_observation(probed, ledger_read)
        passed = (
            probed.state == "healthy"
            and probed.page_state == "ready"
            and probed.reason_code == "ready"
            and after_count == 1
            and lease_released
            and ledger_read_matches
        )
        return {
            "schema_version": _LIVE_PROOF_SCHEMA_VERSION,
            "status": "passed" if passed else "failed",
            "state": probed.state,
            "page_state": probed.page_state,
            "reason_code": probed.reason_code,
            "observation_count": after_count,
            "lease_released": lease_released,
            "ledger_read_matches": ledger_read_matches,
        }
    finally:
        repository.close()
