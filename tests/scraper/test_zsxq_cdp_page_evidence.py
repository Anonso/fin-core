"""Offline contract tests for the Gate 2B/B2c CDP PageEvidence collector."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fin_analyse.scraper import cdp_runtime
from fin_analyse.scraper.cdp_diagnostics import (
    CdpProbeControlFailureCode,
    CdpProbeControlFailureError,
)
from fin_analyse.scraper.cdp_runtime import WindowsChromeCdpAdapter
from fin_analyse.scraper.page_assessment import PageEvidence, PageState, assess_page

_GROUP_URL = "https://wx.zsxq.com/group/15522441811252"
_GROUP_PATH = "/group/15522441811252"
_GROUP_IDENTITY = "zsxq-group-timeline"
_DEFAULT_PAYLOAD = object()


def _inventory() -> list[dict[str, Any]]:
    return [
        {
            "tabId": 11,
            "windowId": 41,
            "url": "https://example.invalid/private?access_token=tab-secret",
            "active": True,
        },
        {
            "tabId": 22,
            "windowId": 42,
            "url": _GROUP_URL,
            "active": True,
        },
        {
            "tabId": 23,
            "windowId": 42,
            "url": "https://example.invalid/active",
            "active": False,
        },
    ]


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "observed_origin": "https://wx.zsxq.com",
        "observed_url_path": _GROUP_PATH,
        "url_query_present": False,
        "url_fragment_present": False,
        "observed_native_identity": _GROUP_IDENTITY,
        "document_ready_state": "complete",
        "loading_surface_stable": False,
        "challenge_present": False,
        "login_surface_present": False,
        "qr_scan_surface_present": False,
        "rate_limit_present": False,
        "retry_after_seconds": None,
    }
    payload.update(overrides)
    return payload


class _FakeBridge:
    def __init__(
        self,
        *,
        payload: object = _DEFAULT_PAYLOAD,
        inventory_snapshots: list[list[dict[str, Any]]] | None = None,
        start_result: bool = True,
        start_failure_code: CdpProbeControlFailureCode | None = None,
        inventory_error: BaseException | None = None,
        js_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.payload = _payload() if payload is _DEFAULT_PAYLOAD else payload
        self.inventory_snapshots = inventory_snapshots or [_inventory(), _inventory()]
        self.start_result = start_result
        self.start_failure_code = start_failure_code
        self.inventory_error = inventory_error
        self.js_error = js_error
        self.close_error = close_error
        self.start_calls = 0
        self.close_calls = 0
        self.inventory_calls = 0
        self.collection_calls: list[int | str] = []
        self.mutation_calls: list[str] = []

    def start(self) -> bool:
        self.start_calls += 1
        return self.start_result

    def probe_control_failure_code(self) -> CdpProbeControlFailureCode | None:
        return self.start_failure_code

    def get_browser_tab_inventory(self) -> list[dict[str, Any]]:
        if self.inventory_error is not None:
            raise self.inventory_error
        index = self.inventory_calls
        self.inventory_calls += 1
        snapshot = self.inventory_snapshots[min(index, len(self.inventory_snapshots) - 1)]
        return [dict(tab) for tab in snapshot]

    def collect_page_evidence_on_tab(self, tab_id: int | str) -> dict[str, Any]:
        self.collection_calls.append(tab_id)
        if self.js_error is not None:
            raise self.js_error
        return self.payload  # type: ignore[return-value]

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error

    def navigate(self, *args: object, **kwargs: object) -> None:
        self.mutation_calls.append("navigate")
        raise AssertionError("probe must not navigate")

    def open_tab(self, *args: object, **kwargs: object) -> None:
        self.mutation_calls.append("open_tab")
        raise AssertionError("probe must not open a tab")

    def reload(self, *args: object, **kwargs: object) -> None:
        self.mutation_calls.append("reload")
        raise AssertionError("probe must not reload")

    def screenshot(self, *args: object, **kwargs: object) -> None:
        self.mutation_calls.append("screenshot")
        raise AssertionError("probe must not screenshot")

    def scroll_by(self, *args: object, **kwargs: object) -> None:
        self.mutation_calls.append("scroll_by")
        raise AssertionError("probe must not scroll")


class _BridgeFactory:
    def __init__(self, bridge: _FakeBridge) -> None:
        self.bridge = bridge
        self.deadlines: list[datetime] = []

    def __call__(self, *, deadline_at: datetime) -> _FakeBridge:
        self.deadlines.append(deadline_at)
        return self.bridge


class _BridgeSequenceFactory:
    def __init__(self, *bridges: _FakeBridge) -> None:
        self._bridges = iter(bridges)
        self.deadlines: list[datetime] = []

    def __call__(self, *, deadline_at: datetime) -> _FakeBridge:
        self.deadlines.append(deadline_at)
        return next(self._bridges)


def _adapter(bridge: _FakeBridge) -> tuple[WindowsChromeCdpAdapter, _BridgeFactory]:
    factory = _BridgeFactory(bridge)
    return WindowsChromeCdpAdapter(bridge_factory=factory), factory


def test_probe_uses_one_read_only_bridge_lifecycle_and_absolute_deadline() -> None:
    bridge = _FakeBridge()
    adapter, factory = _adapter(bridge)
    deadline = datetime(2026, 7, 14, 12, 0, tzinfo=UTC) + timedelta(seconds=30)

    result = adapter.probe_page(deadline_at=deadline)

    assert result.state is PageState.ready
    assert factory.deadlines == [deadline]
    assert bridge.start_calls == 1
    assert bridge.close_calls == 1
    assert bridge.inventory_calls == 2
    assert bridge.collection_calls == ["22"]
    assert bridge.mutation_calls == []


@pytest.mark.parametrize(
    ("overrides", "expected_state"),
    [
        ({"challenge_present": True}, PageState.challenge),
        (
            {"login_surface_present": True, "qr_scan_surface_present": True},
            PageState.login_required,
        ),
        ({"rate_limit_present": True, "retry_after_seconds": 120}, PageState.rate_limited),
        ({"observed_native_identity": "zsxq-other-surface"}, PageState.wrong_page),
        (
            {"loading_surface_stable": True, "observed_native_identity": None},
            PageState.loading_timeout,
        ),
        ({"observed_native_identity": None}, PageState.dom_changed),
        ({}, PageState.ready),
    ],
)
def test_probe_projects_page_states_through_existing_classifier(
    overrides: dict[str, object], expected_state: PageState
) -> None:
    bridge = _FakeBridge(payload=_payload(**overrides))
    adapter, _ = _adapter(bridge)

    result = adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert result.state is expected_state


@pytest.mark.parametrize(
    ("bridge", "expected_code"),
    [
        (
            _FakeBridge(
                start_result=False,
                start_failure_code=CdpProbeControlFailureCode.BRIDGE_START_FAILED,
            ),
            CdpProbeControlFailureCode.BRIDGE_START_FAILED,
        ),
        (
            _FakeBridge(
                inventory_error=CdpProbeControlFailureError(
                    CdpProbeControlFailureCode.EXTENSION_DISCONNECTED
                )
            ),
            CdpProbeControlFailureCode.EXTENSION_DISCONNECTED,
        ),
        (
            _FakeBridge(
                js_error=CdpProbeControlFailureError(
                    CdpProbeControlFailureCode.BRIDGE_CONTROL_FAILED
                )
            ),
            CdpProbeControlFailureCode.BRIDGE_CONTROL_FAILED,
        ),
        (
            _FakeBridge(
                js_error=CdpProbeControlFailureError(
                    CdpProbeControlFailureCode.TARGETED_COLLECTION_FAILED
                )
            ),
            CdpProbeControlFailureCode.TARGETED_COLLECTION_FAILED,
        ),
        (
            _FakeBridge(
                js_error=CdpProbeControlFailureError(
                    CdpProbeControlFailureCode.TAB_DEBUGGER_CONFLICT
                )
            ),
            CdpProbeControlFailureCode.TAB_DEBUGGER_CONFLICT,
        ),
        (
            _FakeBridge(
                js_error=CdpProbeControlFailureError(
                    CdpProbeControlFailureCode.TARGET_EXTENSION_COMMAND_FAILED
                )
            ),
            CdpProbeControlFailureCode.TARGET_EXTENSION_COMMAND_FAILED,
        ),
        (
            _FakeBridge(
                js_error=CdpProbeControlFailureError(
                    CdpProbeControlFailureCode.TARGET_RESPONSE_TIMEOUT
                )
            ),
            CdpProbeControlFailureCode.TARGET_RESPONSE_TIMEOUT,
        ),
    ],
)
def test_probe_projects_real_typed_cdp_failures_as_control_failure(
    bridge: _FakeBridge,
    expected_code: CdpProbeControlFailureCode,
) -> None:
    adapter, _ = _adapter(bridge)

    result = adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert result.state is PageState.control_failure
    assert result.reason_code == expected_code.value
    assert bridge.close_calls == 1


def test_probe_builds_all_sixteen_fields_and_redacts_sensitive_values(monkeypatch) -> None:
    bridge = _FakeBridge()
    adapter, _ = _adapter(bridge)
    captured: list[PageEvidence] = []

    def _capture(evidence: PageEvidence):
        captured.append(evidence)
        return assess_page(evidence)

    monkeypatch.setattr(cdp_runtime, "assess_page", _capture)

    result = adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert len(captured) == 1
    evidence = captured[0]
    assert len(fields(evidence)) == 16
    assert evidence.expected_url_path == evidence.observed_url_path == _GROUP_PATH
    assert evidence.expected_native_identity == _GROUP_IDENTITY
    assert evidence.observed_native_identity == _GROUP_IDENTITY
    assert evidence.visible_text == ""
    assert evidence.document_title == ""
    assert evidence.url_query == ""
    assert evidence.url_fragment == ""
    serialized = f"{evidence!r}{result.to_dict()!r}"
    assert "tab-secret" not in serialized
    assert "access_token" not in serialized


def test_production_dom_script_requires_bounded_group_scoped_timeline_identity() -> None:
    """Execute native, link, and repeated-structure identity against a real DOM."""
    from playwright.sync_api import sync_playwright

    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "scraper"
        / "zsxq_pages"
        / "timeline_identity_contract.html"
    ).read_text(encoding="utf-8")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(body=fixture, content_type="text/html"))
        page.goto(_GROUP_URL, wait_until="domcontentloaded")

        from fin_analyse.scraper.opencli_bridge_client import _PROBE_EVIDENCE_SCRIPT
        _collector_script = _PROBE_EVIDENCE_SCRIPT  # noqa: N806
        ready_payload = page.evaluate(_collector_script)
        assert ready_payload["observed_native_identity"] == _GROUP_IDENTITY

        page.evaluate(
            "document.querySelector('[data-topic-id=\"123456789\"]')"
            ".removeAttribute('data-topic-id')"
        )
        decoy_payload = page.evaluate(_collector_script)
        assert decoy_payload["observed_native_identity"] is None

        page.evaluate(
            "document.body.insertAdjacentHTML('beforeend', "
            "'<a href=\"/group/15522441811252/topic/987654321\">third visible topic</a>')"
        )
        fallback_payload = page.evaluate(_collector_script)
        assert fallback_payload["observed_native_identity"] == _GROUP_IDENTITY

        page.evaluate(
            "document.querySelectorAll('a[href*=\"/topic/\"]')"
            ".forEach((node) => node.remove());"
            "document.body.insertAdjacentHTML('beforeend', `"
            "<ul>"
            "<li><div class=\"topic-card talk-post\"><time>t1</time></div></li>"
            "<li><div class=\"topic-card talk-post\"><time>t2</time></div></li>"
            "<li><div class=\"topic-card talk-post\"><time>t3</time></div></li>"
            "</ul>`);"
        )
        react_payload = page.evaluate(_collector_script)
        assert react_payload["observed_native_identity"] == _GROUP_IDENTITY
        browser.close()


@pytest.mark.parametrize(
    "inventory",
    [
        [
            {
                "tabId": 11,
                "windowId": 41,
                "url": "https://example.invalid/",
                "active": True,
            }
        ],
        [
            {
                "tabId": 21,
                "windowId": 41,
                "url": _GROUP_URL,
                "active": True,
            },
            {
                "tabId": 22,
                "windowId": 42,
                "url": _GROUP_URL,
                "active": True,
            },
        ],
        [
            {
                "tabId": 22,
                "windowId": 42,
                "url": f"{_GROUP_URL}?access_token=secret",
                "active": True,
            }
        ],
        [
            {
                "tabId": 22,
                "windowId": 42,
                "url": "http://wx.zsxq.com/group/15522441811252",
                "active": True,
            }
        ],
        [
            {
                "tabId": 22,
                "windowId": 42,
                "url": "https://evil.invalid/group/15522441811252",
                "active": True,
            }
        ],
    ],
)
def test_probe_rejects_missing_ambiguous_or_non_allowlisted_surface(
    inventory: list[dict[str, Any]],
) -> None:
    bridge = _FakeBridge(inventory_snapshots=[inventory, inventory])
    adapter, _ = _adapter(bridge)

    with pytest.raises(RuntimeError, match="allowlisted ZSXQ group tab"):
        adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert bridge.collection_calls == []
    assert bridge.close_calls == 1


def test_probe_collects_exact_inactive_target_without_changing_active_tab() -> None:
    inventory = _inventory()
    inventory[1]["active"] = False
    inventory[2]["active"] = True
    bridge = _FakeBridge(inventory_snapshots=[inventory, inventory])
    adapter, _ = _adapter(bridge)

    result = adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert result.state is PageState.ready
    assert bridge.inventory_calls == 2
    assert bridge.collection_calls == ["22"]
    assert bridge.mutation_calls == []
    assert bridge.close_calls == 1


def test_production_probe_bridge_uses_bounded_extension_reconnect_window() -> None:
    adapter = WindowsChromeCdpAdapter()
    deadline = datetime(2026, 7, 14, 12, 0, tzinfo=UTC) + timedelta(seconds=30)

    bridge = adapter._build_probe_bridge(deadline_at=deadline)

    assert bridge._startup_wait == 35.0
    assert bridge._max_retries == 0
    assert bridge._purpose == "probe"
    assert bridge._deadline_at == deadline
    assert bridge._lease_store is None


@pytest.mark.parametrize(
    "failure_code",
    [
        CdpProbeControlFailureCode.BRIDGE_START_FAILED,
        CdpProbeControlFailureCode.EXTENSION_DISCONNECTED,
        CdpProbeControlFailureCode.TARGET_RESPONSE_TIMEOUT,
    ],
)
def test_probe_performs_at_most_one_passive_reconnect_inside_same_deadline(
    failure_code: CdpProbeControlFailureCode,
) -> None:
    if failure_code is CdpProbeControlFailureCode.BRIDGE_START_FAILED:
        first = _FakeBridge(start_result=False, start_failure_code=failure_code)
    else:
        first = _FakeBridge(js_error=CdpProbeControlFailureError(failure_code))
    second = _FakeBridge()
    factory = _BridgeSequenceFactory(first, second)
    adapter = WindowsChromeCdpAdapter(bridge_factory=factory)
    deadline = datetime.now(UTC) + timedelta(seconds=30)

    result = adapter.probe_page(deadline_at=deadline)

    assert result.state is PageState.ready
    assert factory.deadlines == [deadline, deadline]
    assert first.start_calls == first.close_calls == 1
    assert second.start_calls == second.close_calls == 1
    assert first.mutation_calls == second.mutation_calls == []


def test_probe_stops_after_one_passive_reconnect() -> None:
    first = _FakeBridge(
        start_result=False,
        start_failure_code=CdpProbeControlFailureCode.EXTENSION_DISCONNECTED,
    )
    second = _FakeBridge(
        start_result=False,
        start_failure_code=CdpProbeControlFailureCode.EXTENSION_DISCONNECTED,
    )
    forbidden_third = _FakeBridge()
    factory = _BridgeSequenceFactory(first, second, forbidden_third)
    adapter = WindowsChromeCdpAdapter(bridge_factory=factory)
    deadline = datetime.now(UTC) + timedelta(seconds=30)

    result = adapter.probe_page(deadline_at=deadline)

    assert result.state is PageState.control_failure
    assert result.reason_code == CdpProbeControlFailureCode.EXTENSION_DISCONNECTED.value
    assert factory.deadlines == [deadline, deadline]
    assert forbidden_third.start_calls == forbidden_third.close_calls == 0


@pytest.mark.parametrize(
    "failure_code",
    [
        CdpProbeControlFailureCode.BRIDGE_CONTROL_FAILED,
        CdpProbeControlFailureCode.TAB_DEBUGGER_CONFLICT,
        CdpProbeControlFailureCode.TARGET_EXTENSION_COMMAND_FAILED,
        CdpProbeControlFailureCode.TARGETED_COLLECTION_FAILED,
    ],
)
def test_probe_does_not_auto_recover_user_or_contract_failures(
    failure_code: CdpProbeControlFailureCode,
) -> None:
    first = _FakeBridge(js_error=CdpProbeControlFailureError(failure_code))
    forbidden_second = _FakeBridge()
    factory = _BridgeSequenceFactory(first, forbidden_second)
    adapter = WindowsChromeCdpAdapter(bridge_factory=factory)

    result = adapter.probe_page(deadline_at=datetime.now(UTC) + timedelta(seconds=30))

    assert result.state is PageState.control_failure
    assert result.reason_code == failure_code.value
    assert len(factory.deadlines) == 1
    assert forbidden_second.start_calls == forbidden_second.close_calls == 0


@pytest.mark.parametrize(
    "after_inventory",
    [
        _inventory()[1:],
        [
            *_inventory(),
            {
                "tabId": 33,
                "windowId": 43,
                "url": "https://example.invalid/new",
                "active": True,
            },
        ],
        [
            _inventory()[0],
            {
                "tabId": 22,
                "windowId": 42,
                "url": "https://wx.zsxq.com/digests/15522441811252",
                "active": False,
            },
            _inventory()[2],
        ],
        [{**tab, "active": not tab["active"]} for tab in _inventory()],
        [
            {**tab, "windowId": 99 if tab["tabId"] == 22 else tab["windowId"]}
            for tab in _inventory()
        ],
    ],
)
def test_probe_rejects_tab_set_or_selected_url_drift(
    after_inventory: list[dict[str, Any]],
) -> None:
    bridge = _FakeBridge(inventory_snapshots=[_inventory(), after_inventory])
    adapter, _ = _adapter(bridge)

    with pytest.raises(
        RuntimeError,
        match=(
            "tab set changed|URL fingerprint changed|active tab set changed|selected tab changed"
        ),
    ):
        adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert bridge.close_calls == 1


@pytest.mark.parametrize(
    "inventory",
    [
        [
            {
                "tabId": 22,
                "windowId": 42,
                "url": _GROUP_URL,
            }
        ],
        [
            {
                "tabId": 22,
                "windowId": 42,
                "url": _GROUP_URL,
                "active": True,
                "discarded": "no",
            }
        ],
        [
            {
                "tabId": 22,
                "url": _GROUP_URL,
                "active": True,
            }
        ],
        [
            {
                "windowId": 42,
                "url": _GROUP_URL,
                "active": True,
            }
        ],
        [
            {
                "tabId": 22,
                "windowId": 42,
                "url": _GROUP_URL,
                "active": "yes",
            }
        ],
        [
            {
                "tabId": 22,
                "windowId": 42,
                "url": _GROUP_URL,
                "active": True,
            },
            {
                "tabId": 22,
                "windowId": 43,
                "url": "https://example.invalid",
                "active": True,
            },
        ],
        [
            {
                "tabId": 22,
                "windowId": 42,
                "url": _GROUP_URL,
                "active": False,
            },
            {
                "tabId": 23,
                "windowId": 42,
                "url": "https://example.invalid",
                "active": False,
            },
        ],
        [
            {
                "tabId": 22,
                "windowId": 42,
                "url": _GROUP_URL,
                "active": True,
            },
            {
                "tabId": 23,
                "windowId": 42,
                "url": "https://example.invalid",
                "active": True,
            },
        ],
    ],
)
def test_probe_rejects_missing_or_ambiguous_real_inventory_fields(
    inventory: list[dict[str, Any]],
) -> None:
    bridge = _FakeBridge(inventory_snapshots=[inventory, inventory])
    adapter, _ = _adapter(bridge)

    with pytest.raises(RuntimeError, match="browser tab inventory"):
        adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert bridge.collection_calls == []
    assert bridge.close_calls == 1


def test_probe_compares_non_target_url_by_safe_hash_without_leaking_raw_url() -> None:
    after = _inventory()
    after[0] = {
        **after[0],
        "url": "https://example.invalid/private?access_token=after-secret",
    }
    bridge = _FakeBridge(inventory_snapshots=[_inventory(), after])
    adapter, _ = _adapter(bridge)

    with pytest.raises(RuntimeError, match="URL fingerprint changed") as exc_info:
        adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert "tab-secret" not in str(exc_info.value)
    assert "after-secret" not in str(exc_info.value)
    assert "access_token" not in str(exc_info.value)


def test_probe_rejects_inventory_reordering_as_browser_drift() -> None:
    before = _inventory()
    after = [before[2], before[0], before[1]]
    bridge = _FakeBridge(inventory_snapshots=[before, after])
    adapter, _ = _adapter(bridge)

    with pytest.raises(RuntimeError, match="tab set changed"):
        adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert bridge.close_calls == 1


def test_retained_inventory_snapshot_contains_only_hashes_counts_and_status() -> None:
    snapshot, target_tab_id, target_fingerprint = cdp_runtime._normalize_tab_snapshot(
        _inventory(), select_target=True
    )

    assert target_tab_id == "22"
    assert target_fingerprint in snapshot.tab_fingerprints
    assert snapshot.tab_count == 3
    assert {field.name for field in fields(snapshot)} == {
        "tab_fingerprints",
        "url_fingerprints",
        "active_tab_fingerprints",
        "window_fingerprints",
        "active_window_fingerprints",
        "tab_count",
    }
    retained = repr(snapshot)
    assert "access_token" not in retained
    assert "tab-secret" not in retained
    assert _GROUP_URL not in retained


def test_probe_targeted_collection_failure_returns_after_final_fence() -> None:
    failure = CdpProbeControlFailureError(CdpProbeControlFailureCode.TARGETED_COLLECTION_FAILED)
    bridge = _FakeBridge(js_error=failure)
    adapter, factory = _adapter(bridge)
    deadline = datetime(2026, 7, 14, 12, 0, tzinfo=UTC) + timedelta(seconds=30)

    result = adapter.probe_page(deadline_at=deadline)

    assert result.state is PageState.control_failure
    assert result.reason_code == CdpProbeControlFailureCode.TARGETED_COLLECTION_FAILED.value
    assert factory.deadlines == [deadline]
    assert bridge.start_calls == bridge.close_calls == 1
    assert bridge.inventory_calls == 2
    assert bridge.collection_calls == ["22"]
    assert bridge.mutation_calls == []


@pytest.mark.parametrize(
    "intermediate",
    [
        [*_inventory()[1:], _inventory()[0]],
        [
            {**tab, "windowId": 99 if tab["tabId"] == 22 else tab["windowId"]}
            for tab in _inventory()
        ],
        [
            {**tab, "url": "https://example.invalid/drift" if tab["tabId"] == 22 else tab["url"]}
            for tab in _inventory()
        ],
        [{**tab, "active": not tab["active"]} for tab in _inventory()],
    ],
)
def test_probe_targeted_collection_failure_final_fence_drift_fails_closed(
    intermediate: list[dict[str, Any]],
) -> None:
    failure = CdpProbeControlFailureError(CdpProbeControlFailureCode.TARGETED_COLLECTION_FAILED)
    bridge = _FakeBridge(
        inventory_snapshots=[_inventory(), intermediate],
        js_error=failure,
    )
    adapter, _ = _adapter(bridge)

    with pytest.raises(RuntimeError, match="changed"):
        adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert bridge.inventory_calls == 2
    assert bridge.collection_calls == ["22"]
    assert bridge.close_calls == 1


def test_probe_other_typed_failure_is_not_retried_and_still_runs_final_fence() -> None:
    failure = CdpProbeControlFailureError(
        CdpProbeControlFailureCode.TARGET_EXTENSION_COMMAND_FAILED
    )
    bridge = _FakeBridge(js_error=failure)
    adapter, _ = _adapter(bridge)

    result = adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert result.state is PageState.control_failure
    assert result.reason_code == failure.code.value
    assert bridge.inventory_calls == 2
    assert bridge.collection_calls == ["22"]
    assert bridge.close_calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {**_payload(), "visible_text": "private body access_token=secret"},
        {key: value for key, value in _payload().items() if key != "document_ready_state"},
        _payload(schema_version=2),
        _payload(url_query_present=True),
        _payload(url_fragment_present=True),
        _payload(observed_origin="https://evil.invalid"),
        _payload(observed_url_path="/group/999"),
        _payload(challenge_present="yes"),
        _payload(retry_after_seconds=-1),
        {**_payload(), "control_failure_code": "secret-token-value"},
    ],
)
def test_probe_rejects_malformed_partial_unexpected_or_sensitive_payload(payload: object) -> None:
    bridge = _FakeBridge(payload=payload)
    adapter, _ = _adapter(bridge)

    with pytest.raises(RuntimeError, match="page evidence payload"):
        adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert bridge.close_calls == 1


@pytest.mark.parametrize(
    ("bridge", "message"),
    [
        (_FakeBridge(start_result=False), "start failed"),
        (_FakeBridge(js_error=TimeoutError("deadline exhausted")), "deadline exhausted"),
        (_FakeBridge(close_error=RuntimeError("cleanup failed")), "cleanup failed"),
    ],
)
def test_probe_failures_and_cleanup_failure_fail_closed(bridge: _FakeBridge, message: str) -> None:
    adapter, _ = _adapter(bridge)

    with pytest.raises((RuntimeError, TimeoutError), match=message):
        adapter.probe_page(deadline_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC))

    assert bridge.start_calls == 1
    assert bridge.close_calls == 1
