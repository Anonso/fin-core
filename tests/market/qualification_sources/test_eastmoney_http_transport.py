from __future__ import annotations

import html
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests

from fin_analyse.market.qualification_sources.eastmoney_http_transport import (
    EastmoneyOnDemandTransportError,
    _browser_environment,
    _build_eastmoney_on_demand_http_get,
    _EastmoneyOnDemandTransportCore,
    _is_production_on_demand_http_get,
    _resolve_browser_binary,
    _response_from_dom,
)
from fin_analyse.market.qualification_sources.eastmoney_request_contract import (
    EastmoneyHttpRequest,
    eastmoney_daily_bar_request,
    eastmoney_quote_request,
)

_RAW_PAYLOAD = b'{"rc":0,"data":{"f57":"002409","f107":0}}'
_QUOTE_REQUEST = eastmoney_quote_request(symbol="002409", venue="sz")
_DAILY_REQUEST = eastmoney_daily_bar_request(
    symbol="601899",
    venue="sh",
    completed_through="20260730",
)


def _dom_document(payload: bytes) -> bytes:
    """模拟 Chrome ``--dump-dom`` 对 application/json 顶层导航的输出形态
    （单 ``<pre>`` + JSON viewer 外壳；文本节点按 Chrome 规则实体转义）。"""
    escaped = html.escape(payload.decode("utf-8"), quote=False)
    return (
        '<html><head><meta charset="utf-8"></head><body><pre>'
        + escaped
        + '</pre><div class="json-formatter-container"></div></body></html>'
    ).encode("utf-8")


def _error_page_dom() -> bytes:
    return (
        b'<!DOCTYPE html><html dir="ltr" lang="en"><head>'
        b'<meta charset="utf-8"><title>push2his.eastmoney.com</title>'
        b"</head><body></body></html>"
    )


@pytest.fixture(autouse=True)
def _reset_browser_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试重置浏览器故障记忆与配置 env——避免测试间污染。"""
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    monkeypatch.delenv("FIN_EASTMONEY_BROWSER_BIN", raising=False)
    transport._BROWSER_FAILED_AT = None
    yield
    transport._BROWSER_FAILED_AT = None


@pytest.fixture()
def fake_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    binary = tmp_path / "fake-chrome"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("FIN_EASTMONEY_BROWSER_BIN", str(binary))
    return str(binary)


@dataclass
class _Response:
    status_code: int
    content: bytes


class _Clock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _BrowserRunner:
    """记录一次无头浏览器 spawn 的全部边界（argv/timeout/env/容量）。"""

    def __init__(
        self,
        *,
        stdout: bytes = _dom_document(_RAW_PAYLOAD),
        returncode: int = 0,
        raise_exc: Exception | None = None,
        clock: _Clock | None = None,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.raise_exc = raise_exc
        self.clock = clock
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []
        self.envs: list[dict[str, str]] = []
        self.caps: list[int] = []
        self.fsize_limits: list[int | None] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: dict[str, str],
        max_output_bytes: int,
        fsize_limit_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        call = tuple(argv)
        self.calls.append(call)
        self.timeouts.append(timeout)
        self.envs.append(env)
        self.caps.append(max_output_bytes)
        self.fsize_limits.append(fsize_limit_bytes)
        if self.clock is not None:
            self.clock.value += 0.5
        if self.raise_exc is not None:
            raise self.raise_exc
        return subprocess.CompletedProcess(
            args=call,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=b"",
        )


@pytest.mark.parametrize(
    ("status_code", "content"),
    [
        (200, _RAW_PAYLOAD),
        (503, b'{"message":"unavailable"}'),
        (200, b'{"rc":0,"data":'),
    ],
)
def test_primary_response_is_returned_unchanged_without_browser(
    status_code: int,
    content: bytes,
) -> None:
    expected = _Response(status_code=status_code, content=content)
    calls: list[float] = []

    def primary_get(
        url: str,
        *,
        params,
        headers,
        timeout: float,
        allow_redirects: bool,
    ):
        assert url == _QUOTE_REQUEST.endpoint
        assert params == _QUOTE_REQUEST.params_dict()
        assert headers == _QUOTE_REQUEST.headers_dict()
        assert allow_redirects is False
        calls.append(timeout)
        return expected

    runner = _BrowserRunner()
    response = _transport(primary_get=primary_get, runner=runner).fetch(
        _QUOTE_REQUEST,
        timeout=8.0,
    )

    assert response is expected
    assert calls == [2.0]
    assert runner.calls == []


def test_non_requests_exception_never_enters_browser() -> None:
    runner = _BrowserRunner()

    def invalid_primary(*args, **kwargs):
        raise ValueError("adapter failure")

    with pytest.raises(ValueError, match="adapter failure"):
        _transport(primary_get=invalid_primary, runner=runner).fetch(
            _QUOTE_REQUEST,
            timeout=8.0,
        )

    assert runner.calls == []


def test_browser_fallback_runs_one_bounded_subprocess(fake_browser: str) -> None:
    runner = _BrowserRunner()

    response = _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert response.status_code == 200
    assert response.content == _RAW_PAYLOAD
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == fake_browser
    assert "--headless" in argv
    assert "--dump-dom" in argv
    assert any(flag.startswith("--user-data-dir=") for flag in argv)
    assert any(flag.startswith("--virtual-time-budget=") for flag in argv)
    assert argv[-1] == _QUOTE_REQUEST.canonical_url


def test_dom_entities_are_unescaped_before_payload_parse(fake_browser: str) -> None:
    payload = b'{"a":"x&y <z> \xe4\xb8\x8a\xe8\xaf\x81"}'
    runner = _BrowserRunner(stdout=_dom_document(payload))

    response = _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert response.content == payload


def test_error_page_without_json_viewer_fails_closed(fake_browser: str) -> None:
    runner = _BrowserRunner(stdout=_error_page_dom())

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_BROWSER_DOCUMENT_INVALID$",
    ):
        _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)


@pytest.mark.parametrize(
    "stdout",
    [
        # JSON viewer 标记在但 pre 数量非 1（劫持页/多 pre 页不可信）。
        b'<html><body><div class="json-formatter-container"></div></body></html>',
        b'<html><body><pre>{"a":1}</pre><pre>{"b":2}</pre>'
        b'<div class="json-formatter-container"></div></body></html>',
        # 标记在、pre 在、但内容不是合法 JSON（重复键同样拒绝）。
        b'<html><body><pre>{"a":1,"a":2}</pre>'
        b'<div class="json-formatter-container"></div></body></html>',
        b"<html><body><pre>not json</pre>"
        b'<div class="json-formatter-container"></div></body></html>',
    ],
)
def test_malformed_dom_fails_closed(fake_browser: str, stdout: bytes) -> None:
    runner = _BrowserRunner(stdout=stdout)

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_BROWSER_DOCUMENT_INVALID$",
    ):
        _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)


def test_payload_over_contract_cap_fails_closed(fake_browser: str) -> None:
    payload = b"x" * (_QUOTE_REQUEST.maximum_payload_bytes + 1)
    runner = _BrowserRunner(stdout=_dom_document(payload))

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_BROWSER_PAYLOAD_TOO_LARGE$",
    ):
        _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)


def test_realistic_daily_payload_fits_the_existing_four_mib_adapter_cap(
    fake_browser: str,
) -> None:
    payload = b'{"d":"' + b"x" * 335_630 + b'"}'
    runner = _BrowserRunner(stdout=_dom_document(payload))

    response = _transport(runner=runner).fetch(_DAILY_REQUEST, timeout=8.0)

    assert response.content == payload


def test_stdout_cap_covers_dom_entity_expansion(fake_browser: str) -> None:
    runner = _BrowserRunner()

    _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    # 实体最坏 5 倍膨胀（& → &amp;）+ DOM 外壳余量；上限由有界执行器强制。
    assert runner.caps == [
        _QUOTE_REQUEST.maximum_payload_bytes * 5 + 64 * 1024,
    ]
    # fsize 与 stdout 上限解耦：浏览器 profile 内部写需要独立余量（否则
    # RLIMIT_FSIZE 打死网络服务进程、无限重启挂满预算，2026-09-07 实弹实证）。
    assert runner.fsize_limits == [64 * 1024 * 1024]


@pytest.mark.parametrize(
    "raise_exc",
    [
        OSError("headless spawn failed"),
        subprocess.TimeoutExpired(("google-chrome",), 6.0),
    ],
)
def test_spawn_failure_is_typed_and_marks_cooldown(
    fake_browser: str,
    raise_exc: Exception,
) -> None:
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    clock = _Clock(value=100.0)
    runner = _BrowserRunner(raise_exc=raise_exc, clock=clock)

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_BROWSER_COMMAND_FAILED$",
    ):
        _transport(runner=runner, clock=clock).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert transport._BROWSER_FAILED_AT is not None
    assert transport._BROWSER_FAILED_AT >= 100.0


def test_nonzero_exit_is_typed_command_failed(fake_browser: str) -> None:
    runner = _BrowserRunner(returncode=1)

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_BROWSER_COMMAND_FAILED$",
    ):
        _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)


def test_browser_budget_shares_one_deadline(fake_browser: str) -> None:
    clock = _Clock(0.0)
    runner = _BrowserRunner(clock=clock)

    def primary_get(*args, timeout: float, **kwargs):
        assert timeout == 2.0
        clock.value += 2.0
        raise requests.ConnectionError("remote closed")

    _transport(primary_get=primary_get, runner=runner, clock=clock).fetch(
        _QUOTE_REQUEST,
        timeout=8.0,
    )

    # 主路耗 2s 后，兜底拿全部剩余预算（无 close 预备位——子进程随调用结束）。
    assert runner.timeouts == pytest.approx([6.0])
    budget_flag = next(
        flag for flag in runner.calls[0] if flag.startswith("--virtual-time-budget=")
    )
    assert budget_flag == "--virtual-time-budget=6000"


def test_browser_env_isolates_home_and_passes_proxy_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data_dir = tmp_path / "profile"
    environment = _browser_environment(
        user_data_dir,
        environ={
            "LANG": "ignored",
            "HTTPS_PROXY": "http://172.25.16.1:7897",
            "https_proxy": "http://172.25.16.1:7897",
            "WSL_INTEROP": "/run/WSL/1_interop",
            "PATH": "/some/ambient/path",
        },
    )

    assert environment == {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "HOME": str(user_data_dir),
        "HTTPS_PROXY": "http://172.25.16.1:7897",
        "https_proxy": "http://172.25.16.1:7897",
    }


def test_user_data_dir_is_fresh_per_call_and_removed_after(
    fake_browser: str,
) -> None:
    runner = _BrowserRunner()

    _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)
    _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    dirs = [
        flag.split("=", 1)[1]
        for call in runner.calls
        for flag in call
        if flag.startswith("--user-data-dir=")
    ]
    assert len(dirs) == 2
    assert dirs[0] != dirs[1]
    assert all(not Path(directory).exists() for directory in dirs)


def test_browser_failure_enters_cooldown_and_skips_next_attempt(
    fake_browser: str,
) -> None:
    """兜底失败后 TTL 内跳过——不每次重付无头 spawn 成本。"""
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    clock = _Clock(value=100.0)
    runner = _BrowserRunner(stdout=_error_page_dom(), clock=clock)

    with pytest.raises(EastmoneyOnDemandTransportError, match="DOCUMENT_INVALID"):
        _transport(runner=runner, clock=clock).fetch(_QUOTE_REQUEST, timeout=10.0)
    assert transport._BROWSER_FAILED_AT is not None

    runner.calls.clear()
    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="BROWSER_COOLDOWN_ACTIVE",
    ):
        _transport(runner=runner, clock=clock).fetch(_QUOTE_REQUEST, timeout=10.0)
    assert runner.calls == []

    clock.value = (
        transport._BROWSER_FAILED_AT + transport._BROWSER_FAILURE_COOLDOWN_SECONDS + 1
    )
    runner2 = _BrowserRunner(stdout=_error_page_dom(), clock=clock)
    with pytest.raises(EastmoneyOnDemandTransportError, match="DOCUMENT_INVALID"):
        _transport(runner=runner2, clock=clock).fetch(_QUOTE_REQUEST, timeout=10.0)
    assert runner2.calls != []


def test_primary_success_does_not_consult_cooldown(fake_browser: str) -> None:
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    transport._BROWSER_FAILED_AT = 100.0
    clock = _Clock(value=200.0)

    def primary_ok(*args, **kwargs):
        return _Response(status_code=200, content=_RAW_PAYLOAD)

    result = _transport(
        runner=_BrowserRunner(clock=clock), primary_get=primary_ok, clock=clock
    ).fetch(_QUOTE_REQUEST, timeout=10.0)

    assert result.content == _RAW_PAYLOAD


def test_exhausted_fallback_budget_fails_closed_without_spawn() -> None:
    clock = _Clock(0.0)
    unused_runner = _BrowserRunner()

    def late_primary(*args, **kwargs):
        clock.value = 7.5
        raise requests.ConnectionError("late remote close")

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_FALLBACK_DEADLINE_REACHED$",
    ):
        _transport(primary_get=late_primary, runner=unused_runner, clock=clock).fetch(
            _QUOTE_REQUEST,
            timeout=8.0,
        )
    assert unused_runner.calls == []


def test_test_core_cannot_be_mutated_into_live_authority(fake_browser: str) -> None:
    runner = _BrowserRunner()
    core = _transport(runner=runner)

    assert not _is_production_on_demand_http_get(core)
    with pytest.raises(AttributeError):
        core._production_eligible = True  # type: ignore[attr-defined]
    assert _is_production_on_demand_http_get(_build_eastmoney_on_demand_http_get())


def test_forged_typed_request_cannot_change_endpoint_or_duplicate_query(
    fake_browser: str,
) -> None:
    with pytest.raises(ValueError, match="^invalid Eastmoney HTTP request contract$"):
        EastmoneyHttpRequest(
            kind="quote",
            endpoint="https://evil.example/collect",
            query=(*_QUOTE_REQUEST.query, ("secid", "1.601899")),
            headers=_QUOTE_REQUEST.headers,
            maximum_payload_bytes=_QUOTE_REQUEST.maximum_payload_bytes,
        )
    mutated = eastmoney_quote_request(symbol="002409", venue="sz")
    object.__setattr__(mutated, "endpoint", "https://evil.example/collect")
    runner = _BrowserRunner()

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_REQUEST_INVALID$",
    ):
        _transport(runner=runner).fetch(mutated, timeout=8.0)
    assert runner.calls == []


def test_resolve_browser_binary_prefers_valid_env_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "pinned-chrome"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("FIN_EASTMONEY_BROWSER_BIN", str(binary))

    assert _resolve_browser_binary() == str(binary)


def test_resolve_browser_binary_rejects_invalid_env_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIN_EASTMONEY_BROWSER_BIN", str(tmp_path / "missing"))
    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_BROWSER_UNAVAILABLE$",
    ):
        _resolve_browser_binary()
    monkeypatch.setenv("FIN_EASTMONEY_BROWSER_BIN", "relative/chrome")
    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_BROWSER_UNAVAILABLE$",
    ):
        _resolve_browser_binary()


def test_resolve_browser_binary_fails_closed_without_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_BROWSER_UNAVAILABLE$",
    ):
        _resolve_browser_binary(which=lambda name: None)


def test_response_from_dom_rejects_empty_pre_body() -> None:
    stdout = (
        b'<html><body><pre></pre>'
        b'<div class="json-formatter-container"></div></body></html>'
    )
    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_BROWSER_DOCUMENT_INVALID$",
    ):
        _response_from_dom(stdout, spec=_QUOTE_REQUEST)


def _transport(
    *,
    runner: _BrowserRunner,
    primary_get=None,
    clock: _Clock | None = None,
) -> _EastmoneyOnDemandTransportCore:
    def connection_failure(*args, **kwargs):
        raise requests.ConnectionError("remote closed")

    return _EastmoneyOnDemandTransportCore(
        primary_get=primary_get or connection_failure,
        command_runner=runner,
        monotonic=clock or (lambda: 10.0),
    )
