"""Tests for the stateless opencli CLI transport (OpenCliBridgeClient).

All tests run against a fake command runner — no real PowerShell/opencli/WSL
interop is ever touched. The fake mirrors eastmoney's ``_OpenCliRunner`` pattern.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from fin_analyse.scraper.cdp_diagnostics import CdpProbeControlFailureCode
from fin_analyse.scraper.cdp_runtime import WindowsChromeCdpAdapter
from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper
from fin_analyse.scraper.opencli_bridge_client import (
    OpenCliBridgeClient,
    OpenCliBridgeError,
    _browser_argv,
    _wrap_eval_script,
)

_OPENCLI_PATH_JSON = b'{"path":"C:\\\\Users\\\\u\\\\AppData\\\\Roaming\\\\npm\\\\opencli.ps1"}'
_TAB_ID = "A" * 32
_GROUP_URL = "https://wx.zsxq.com/group/15522441811252"
_GROUP_TEXT = "2026-08-01 10:00:00 生猪 涨价 分析内容" * 40  # >500 chars

_PROBE_PAYLOAD = {
    "schema_version": 1,
    "observed_origin": "https://wx.zsxq.com",
    "observed_url_path": "/group/15522441811252",
    "url_query_present": False,
    "url_fragment_present": False,
    "observed_native_identity": "zsxq-group-timeline",
    "document_ready_state": "complete",
    "loading_surface_stable": False,
    "challenge_present": False,
    "login_surface_present": False,
    "qr_scan_surface_present": False,
    "rate_limit_present": False,
    "retry_after_seconds": None,
}


def _completed(
    call: tuple[str, ...],
    stdout: bytes,
    *,
    returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=call, returncode=returncode, stdout=stdout, stderr=b"")


class _OpenCliRunner:
    """argv 分支 fake；可注入 failure 与 tab 列表。"""

    def __init__(
        self,
        *,
        failure: str | None = None,
        tabs: list[dict] | None = None,
        text: str = _GROUP_TEXT,
    ) -> None:
        self.failure = failure
        self.tabs = tabs or []
        self.text = text
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        call = tuple(argv)
        self.calls.append(call)
        self.timeouts.append(timeout)
        if self.failure == "timeout":
            raise subprocess.TimeoutExpired(call, timeout)
        if self.failure == "output_limit":
            raise RuntimeError("bounded_process_output_limit_exceeded")
        if "-Command" in call:
            if self.failure == "resolve":
                return _completed(call, b'{"path":"C:\\\\bad\\\\path.exe"}')
            return _completed(call, _OPENCLI_PATH_JSON)
        if "tab" in call and "list" in call:
            if self.failure == "tab_list":
                return _completed(call, b"[]", returncode=1)
            return _completed(call, json.dumps(self.tabs).encode())
        if "tab" in call and "new" in call:
            if self.failure == "target":
                return _completed(call, b'{"page":"not-a-target","url":"x"}')
            return _completed(call, json.dumps({"page": _TAB_ID, "url": call[-1]}).encode())
        if "tab" in call and "select" in call:
            return _completed(call, json.dumps({"selected": call[-1]}).encode())
        if "open" in call:
            if self.failure == "open_fail":
                return _completed(call, b"", returncode=1)
            return _completed(call, json.dumps({"url": call[-1], "page": _TAB_ID}).encode())
        if "scroll" in call:
            return _completed(call, b"Scrolled down")
        if "eval" in call:
            script = call[call.index("eval") + 1]
            if "schema_version" in script:
                return _completed(call, json.dumps(_PROBE_PAYLOAD).encode())
            if "document.body.innerText" in script:
                return _completed(call, self.text.encode("utf-8"))
            if self.failure == "eval_fail":
                return _completed(call, b"", returncode=1)
            return _completed(call, json.dumps({"status": 200, "ok": True}).encode())
        return _completed(call, b"{}")


def _client(
    runner: _OpenCliRunner,
    *,
    purpose: str = "scrape",
    deadline_at: datetime | None = None,
    start: bool = True,
    tabs: list[dict] | None = None,
) -> OpenCliBridgeClient:
    if tabs is not None:
        runner.tabs = tabs
    client = OpenCliBridgeClient(
        startup_wait=5.0,
        purpose=purpose,
        deadline_at=deadline_at,
        command_runner=runner,
        monotonic=lambda: 1.0,
    )
    if start:
        assert client.start() is True
    return client


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fin_analyse.scraper.opencli_bridge_client.time.sleep", lambda s: None)


@pytest.fixture(autouse=True)
def _fixed_node_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试固定 node.exe 解析，避免依赖本机 /mnt/c 布局。"""
    monkeypatch.setattr(
        "fin_analyse.scraper.opencli_bridge_client._resolve_node_executable",
        lambda npm_dir: "C:\\Program Files\\nodejs\\node.exe",
    )


def _probe_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """probe 生命周期需要合法 FIN_CDP_BRIDGE_TOKEN。"""
    monkeypatch.setenv("FIN_CDP_BRIDGE_TOKEN", "p" * 32)


# ── 底层 / 预算 ─────────────────────────────────────────────


class TestStartAndResolve:
    def test_start_resolves_path_and_probes_session(self):
        runner = _OpenCliRunner()
        client = _client(runner)
        assert client._opencli_path is not None
        assert client._opencli_path.endswith("opencli.ps1")
        assert any("tab" in c and "list" in c for c in runner.calls)

    def test_start_adopts_existing_zsxq_tab(self):
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": "zsxq"}])
        client = _client(runner)
        assert client._tab_id == _TAB_ID

    def test_start_false_on_resolve_failure(self):
        runner = _OpenCliRunner(failure="resolve")
        client = OpenCliBridgeClient(command_runner=runner, monotonic=lambda: 1.0)
        assert client.start() is False
        assert client._error == "OPENCLI_PATH_INVALID"

    def test_start_false_on_timeout(self):
        runner = _OpenCliRunner(failure="timeout")
        client = OpenCliBridgeClient(command_runner=runner, monotonic=lambda: 1.0)
        assert client.start() is False
        assert client._error == "OPENCLI_COMMAND_TIMEOUT"

    def test_start_false_when_deadline_exhausted(self):
        runner = _OpenCliRunner()
        client = OpenCliBridgeClient(
            command_runner=runner,
            monotonic=lambda: 1.0,
            deadline_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        assert client.start() is False
        assert client._error == "OPENCLI_DEADLINE_REACHED"
        assert runner.calls == []

    def test_error_code_validation(self):
        with pytest.raises(ValueError):
            OpenCliBridgeError("NOPE")
        error = OpenCliBridgeError("COMMAND_FAILED")
        assert str(error) == "OPENCLI_COMMAND_FAILED"
        assert error.code == "COMMAND_FAILED"


class TestInvokeErrorMapping:
    def test_returncode_failure_maps_to_command_failed(self):
        runner = _OpenCliRunner(failure="tab_list")
        client = _client(runner, start=False)
        client._opencli_path = "C:\\u\\AppData\\Roaming\\npm\\opencli.ps1"
        with pytest.raises(OpenCliBridgeError) as exc:
            client.get_tabs()
        assert exc.value.code == "COMMAND_FAILED"

    def test_output_limit_maps_to_output_too_large(self):
        runner = _OpenCliRunner(failure="output_limit")
        client = _client(runner, start=False)
        client._opencli_path = "C:\\u\\AppData\\Roaming\\npm\\opencli.ps1"
        client._tab_id = _TAB_ID
        with pytest.raises(OpenCliBridgeError) as exc:
            client.js("document.body.innerText")
        assert exc.value.code == "OUTPUT_TOO_LARGE"


def test_node_environment_reuses_shared_safe_wsl_interop(monkeypatch) -> None:
    import fin_analyse.scraper.opencli_bridge_client as bridge

    monkeypatch.setattr(
        bridge,
        "_opencli_environment",
        lambda: {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "WSL_INTEROP": "/run/WSL/1_interop",
        },
    )

    environment = bridge._node_environment("C:\\u\\AppData\\Roaming\\npm\\opencli.ps1")

    assert environment["WSL_INTEROP"] == "/run/WSL/1_interop"


# ── js 语义 ─────────────────────────────────────────────────


class TestJsSemantics:
    def test_js_passes_string_result_through_verbatim(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])
        client = _client(runner)
        result = client.js("document.body.innerText")
        assert result == _GROUP_TEXT
        eval_call = next(c for c in runner.calls if "eval" in c)
        assert "--tab" in eval_call and _TAB_ID in eval_call

    def test_js_json_parses_object_result(self):
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])
        client = _client(runner)
        result = client.js_json("(async () => ({status: 200, ok: true}))()")
        assert result == {"status": 200, "ok": True}

    def test_js_json_raw_fallback(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])
        client = _client(runner)
        result = client.js_json("document.body.innerText")
        assert set(result) == {"_raw"}
        assert result["_raw"] == _GROUP_TEXT

    def test_eval_wraps_top_level_return(self):
        assert _wrap_eval_script("document.body.innerText") == "document.body.innerText"
        wrapped = _wrap_eval_script("return 1;")
        assert wrapped.startswith("(async () => {")
        assert "return 1;" in wrapped

    def test_js_without_tab_raises_no_tab(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner()
        client = _client(runner, start=False)
        client._opencli_path = "C:\\u\\AppData\\Roaming\\npm\\opencli.ps1"
        with pytest.raises(OpenCliBridgeError) as exc:
            client.js("document.body.innerText")
        assert exc.value.code == "NO_TAB"

    def test_large_payload_round_trip(self, monkeypatch):
        _no_sleep(monkeypatch)
        big_text = "内容" * 200_000  # ~1.2 MiB chars
        runner = _OpenCliRunner(
            tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}], text=big_text
        )
        client = _client(runner)
        result = client.js("document.body.innerText")
        assert result == big_text

    def test_oversized_payload_fails_explicitly(self, monkeypatch):
        _no_sleep(monkeypatch)
        huge_text = "内容" * 450_000  # ~2.6 MiB chars > _MAX_STDOUT_BYTES
        runner = _OpenCliRunner(
            tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}], text=huge_text
        )
        client = _client(runner)
        with pytest.raises(OpenCliBridgeError) as exc:
            client.js("document.body.innerText")
        assert exc.value.code == "OUTPUT_TOO_LARGE"


# ── 页面操作 ────────────────────────────────────────────────


class TestPageOperations:
    def test_navigate_creates_tab_when_none(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner()
        client = _client(runner)
        client.navigate(_GROUP_URL, wait=3.0)
        assert client._tab_id == _TAB_ID
        assert any("tab" in c and "new" in c for c in runner.calls)

    def test_navigate_uses_open_on_existing_tab(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])
        client = _client(runner)
        client.navigate(_GROUP_URL, wait=3.0)
        assert not any("tab" in c and "new" in c for c in runner.calls)
        assert any("open" in c for c in runner.calls)

    def test_navigate_cache_bust_appends_fin_ts(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])
        client = _client(runner)
        client.navigate(_GROUP_URL, wait=3.0, cache_bust=True)
        open_call = next(c for c in runner.calls if "open" in c)
        assert "_fin_ts=" in open_call[open_call.index("open") + 1]
        assert "--tab" in open_call and open_call[-1] == _TAB_ID

    def test_scroll_by_sends_scroll_down_amount(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])
        client = _client(runner)
        client.scroll_by(px=4000, wait=1.0)
        scroll_call = next(c for c in runner.calls if "scroll" in c)
        assert "down" in scroll_call and "4000" in scroll_call

    def test_validate_page_state_ok(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])
        client = _client(runner)
        is_valid, reason = client.validate_page_state()
        assert is_valid is True and reason == "ok"

    def test_validate_page_state_login(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(
            tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}],
            text="请登录后查看 扫码登录 请先完成登录后再继续使用本页面功能 " * 30,
        )
        client = _client(runner)
        is_valid, reason = client.validate_page_state()
        assert is_valid is False and reason == "login_page"

    def test_validate_page_state_empty(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(
            tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}], text="short"
        )
        client = _client(runner)
        is_valid, reason = client.validate_page_state()
        assert is_valid is False and reason.startswith("content_empty")

    def test_force_navigate_group_success(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])
        client = _client(runner)
        assert client.force_navigate_group(_GROUP_URL, wait=5.0) is True

    def test_force_navigate_group_failure(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(
            tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}], text="short"
        )
        client = _client(runner)
        assert client.force_navigate_group(_GROUP_URL, wait=5.0) is False

    def test_heal_tab_via_new_window(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner()
        client = _client(runner)
        probe_id = client.heal_tab_via_new_window(_GROUP_URL, wait=5.0)
        assert probe_id == 1
        assert client._tab_id == _TAB_ID
        assert any("tab" in c and "select" in c for c in runner.calls)


# ── batch_execute ───────────────────────────────────────────


class TestBatchExecute:
    @staticmethod
    def _client(runner: _OpenCliRunner) -> OpenCliBridgeClient:
        return _client(runner, tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])

    def test_batch_ok_status_and_result_by_name(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner()
        client = self._client(runner)
        result = client.batch_execute(
            [
                {"action": "js", "name": "body", "script": "document.body.innerText"},
                {"action": "scroll_by", "name": "scroll", "px": 4000, "wait": 0.0},
            ]
        )
        assert result.status == "ok"
        assert result.result_by_name("body") == _GROUP_TEXT
        assert result.result_by_name("scroll") == {"scrolled_px": 4000, "repeat": 1}

    def test_batch_partial_when_optional_step_fails(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(failure="open_fail")  # open 命令失败
        client = OpenCliBridgeClient(
            startup_wait=5.0,
            purpose="scrape",
            command_runner=runner,
            monotonic=lambda: 1.0,
        )
        client._opencli_path = "C:\\u\\AppData\\Roaming\\npm\\opencli.ps1"
        client._tab_id = _TAB_ID
        result = client.batch_execute(
            [
                {"action": "js", "name": "body", "script": "document.body.innerText"},
                {"action": "navigate", "name": "nav", "url": _GROUP_URL, "required": False},
            ]
        )
        assert result.status == "partial"
        assert result.failed_step is not None
        assert result.failed_step["action"] == "navigate"

    def test_batch_failed_when_required_step_fails(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner(failure="open_fail")
        client = OpenCliBridgeClient(
            startup_wait=5.0,
            purpose="scrape",
            command_runner=runner,
            monotonic=lambda: 1.0,
        )
        client._opencli_path = "C:\\u\\AppData\\Roaming\\npm\\opencli.ps1"
        client._tab_id = _TAB_ID
        result = client.batch_execute([{"action": "navigate", "name": "nav", "url": _GROUP_URL}])
        assert result.status == "failed"
        assert result.failed_step is not None

    def test_batch_trace_contains_tab_ids(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner()
        client = self._client(runner)
        result = client.batch_execute([{"action": "js", "name": "body", "script": "1"}])
        assert result.cdp_trace["initial_tab_id"] == _TAB_ID
        assert result.cdp_trace["final_tab_id"] == _TAB_ID

    def test_batch_unknown_action_raises(self, monkeypatch):
        _no_sleep(monkeypatch)
        runner = _OpenCliRunner()
        client = self._client(runner)
        result = client.batch_execute([{"action": "nope", "name": "x"}])
        assert result.status == "failed"
        assert "unknown batch action" in result.failed_step["error"]


# ── probe ───────────────────────────────────────────────────


class TestProbeSupport:
    def test_inventory_normalizes_hex_page_ids_to_decimal(self, monkeypatch):
        _probe_token(monkeypatch)
        runner = _OpenCliRunner(
            tabs=[
                {"id": _TAB_ID, "url": _GROUP_URL, "title": ""},
                {"id": "B" * 32, "url": "https://chatgpt.com/", "title": ""},
            ]
        )
        client = _client(runner, purpose="probe")
        inventory = client.get_browser_tab_inventory()
        assert len(inventory) == 2
        assert all(str(tab["tabId"]).isdecimal() for tab in inventory)
        assert inventory[0]["tabId"] == 1
        assert inventory[1]["tabId"] == 2

    def test_collect_page_evidence_returns_13_key_payload(self, monkeypatch):
        _probe_token(monkeypatch)
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])
        client = _client(runner, purpose="probe")
        client.get_browser_tab_inventory()  # 填充 hex→十进制映射
        payload = client.collect_page_evidence_on_tab(1)
        assert set(payload) == set(_PROBE_PAYLOAD)
        assert payload["observed_native_identity"] == "zsxq-group-timeline"

    def test_collect_page_evidence_invalid_tab_raises(self, monkeypatch):
        _probe_token(monkeypatch)
        runner = _OpenCliRunner(tabs=[{"id": _TAB_ID, "url": _GROUP_URL, "title": ""}])
        client = _client(runner, purpose="probe")
        with pytest.raises(Exception) as exc:
            client.collect_page_evidence_on_tab(99)
        assert isinstance(exc.value, OpenCliBridgeError)
        assert exc.value.code == "TARGET_INVALID"

    def test_probe_requires_identity_token(self, monkeypatch):
        monkeypatch.delenv("FIN_CDP_BRIDGE_TOKEN", raising=False)
        runner = _OpenCliRunner()
        client = OpenCliBridgeClient(
            startup_wait=5.0,
            max_retries=0,
            purpose="probe",
            command_runner=runner,
            monotonic=lambda: 1.0,
        )
        assert client.start() is False
        assert (
            client.probe_control_failure_code()
            is CdpProbeControlFailureCode.BRIDGE_IDENTITY_REQUIRED
        )
        assert runner.calls == []  # 未发起任何子进程

    def test_probe_control_failure_code_mapping(self, monkeypatch):
        _probe_token(monkeypatch)
        runner = _OpenCliRunner(failure="timeout")
        client = OpenCliBridgeClient(
            startup_wait=5.0,
            max_retries=0,
            purpose="probe",
            command_runner=runner,
            monotonic=lambda: 1.0,
        )
        assert client.start() is False
        assert (
            client.probe_control_failure_code() is CdpProbeControlFailureCode.EXTENSION_DISCONNECTED
        )

        runner2 = _OpenCliRunner(failure="resolve")
        client2 = OpenCliBridgeClient(
            startup_wait=5.0,
            max_retries=0,
            purpose="probe",
            command_runner=runner2,
            monotonic=lambda: 1.0,
        )
        assert client2.start() is False
        assert (
            client2.probe_control_failure_code() is CdpProbeControlFailureCode.BRIDGE_START_FAILED
        )

    def test_close_is_idempotent_noop(self):
        runner = _OpenCliRunner()
        client = _client(runner)
        client.close()
        client.close()
        assert client._probe_id_map == {}


# ── 装配 / 集成 ─────────────────────────────────────────────


class TestTransportSelection:
    def test_build_scraper_sets_opencli_client_factory(self):
        from fin_analyse.scraper.opencli_bridge_client import OpenCliBridgeClient

        adapter = WindowsChromeCdpAdapter()
        scraper = adapter._build_scraper(
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            checkpoint=lambda: None,
        )
        assert scraper._client_factory is not None
        built = scraper._client_factory(
            startup_wait=20.0,
            purpose="scrape",
            lease_store=None,
            deadline_at=None,
        )
        assert isinstance(built, OpenCliBridgeClient)
        assert built._command_runner is not None

    def test_build_probe_bridge_opencli(self):
        from fin_analyse.scraper.opencli_bridge_client import OpenCliBridgeClient

        adapter = WindowsChromeCdpAdapter()
        bridge = adapter._build_probe_bridge(deadline_at=datetime.now(UTC))
        assert isinstance(bridge, OpenCliBridgeClient)
        assert bridge._purpose == "probe"
        assert bridge._max_retries == 0

class TestScraperClientFactorySeam:
    def test_factory_receives_scrape_kwargs(self, tmp_path):
        captured: dict = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return _FakeClient()

        scraper = CdpBridgeScraper(
            knowledge_base_root=tmp_path,
            client_factory=factory,
        )
        assert scraper.start() is True
        assert captured["purpose"] == "scrape"
        assert captured["startup_wait"] > 0
        assert "deadline_at" in captured
        scraper.close()
        assert _FakeClient.closed is True

    def test_default_path_still_builds_cdp_client(self, monkeypatch, tmp_path):
        # 默认（无 factory）仍走 CdpBridgeClient —— 由既有 test_cdp_production_paths 覆盖
        scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
        assert scraper._client_factory is None


class _FakeClient:
    closed = False

    def start(self) -> bool:
        return True

    def close(self) -> None:
        _FakeClient.closed = True


def test_browser_argv_shape():
    argv = _browser_argv("C:\\u\\AppData\\Roaming\\npm\\opencli.ps1", "eval", "1", "--tab", "A")
    assert argv[-6:] == (
        "browser",
        "fin-zsxq-scraper-v1",
        "eval",
        "1",
        "--tab",
        "A",
    )
    assert argv[0] == "/mnt/c/Program Files/nodejs/node.exe"
    assert argv[1].endswith("main.js")
    assert "node_modules\\@jackwener\\opencli" in argv[1]
