from __future__ import annotations

import base64
import json
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from fin_analyse.market.qualification_sources.eastmoney_http_transport import (
    EastmoneyOnDemandTransportError,
    _build_eastmoney_on_demand_http_get,
    _EastmoneyOnDemandTransportCore,
    _is_production_on_demand_http_get,
)
from fin_analyse.market.qualification_sources.eastmoney_request_contract import (
    EastmoneyHttpRequest,
    eastmoney_daily_bar_request,
    eastmoney_quote_request,
)

_RAW_PAYLOAD = b'{"rc":0,"data":{"f57":"002409","f107":0}}'
_TARGET = "A" * 32
_SECOND_TARGET = "B" * 32
_QUOTE_REQUEST = eastmoney_quote_request(symbol="002409", venue="sz")
_DAILY_REQUEST = eastmoney_daily_bar_request(
    symbol="601899",
    venue="sh",
    completed_through="20260730",
)


@pytest.fixture(autouse=True)
def _reset_opencli_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试重置 opencli 故障记忆——避免测试间污染。"""
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    monkeypatch.delenv("FIN_OPENCLI_PROFILE", raising=False)
    transport._OPENCLI_FAILED_AT = None
    yield
    transport._OPENCLI_FAILED_AT = None


@dataclass
class _Response:
    status_code: int
    content: bytes


class _Clock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _OpenCliRunner:
    def __init__(
        self,
        *,
        failure: str | None = None,
        clock: _Clock | None = None,
        payload: bytes = _RAW_PAYLOAD,
        profile: str | None = "windows-default",
        residual_url: str | None = None,
        sweep_tabs: list[dict[str, str]] | None = None,
    ) -> None:
        self.failure = failure
        self.clock = clock
        self.payload = payload
        self.profile = profile
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []
        self.opened_url = ""
        self.residual_url = residual_url
        self.sweep_tabs = sweep_tabs
        self.saw_tab_new = False

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        call = tuple(argv)
        self.calls.append(call)
        self.timeouts.append(timeout)
        if self.clock is not None:
            self.clock.value += 0.5
        if "-Command" in call:
            if call[-1] == "exit 0":
                # interop probe：成功时无输出；失败模式模拟 vsock 故障
                if self.failure == "probe_exit":
                    return _completed(call, stdout=b"", returncode=1)
                if self.failure == "probe_raise":
                    raise OSError("WSL interop vsock unavailable")
                if self.failure == "probe_timeout":
                    raise subprocess.TimeoutExpired(call, timeout)
                return _completed(call, stdout=b"")
            resolved: dict[str, str] = {"path": "C:\\Users\\u\\AppData\\Roaming\\npm\\opencli.ps1"}
            if self.profile is not None:
                resolved["profile"] = self.profile
            return _completed(
                call,
                stdout=json.dumps(resolved).encode(),
            )
        if "new" in call:
            self.opened_url = call[-1]
            self.saw_tab_new = True
            if self.failure == "new_timeout":
                raise subprocess.TimeoutExpired(call, timeout)
            page = "not-a-target" if self.failure == "target" else _TARGET
            url = (
                self.opened_url.replace("push2delay.eastmoney.com", "evil.example")
                if self.failure == "final_url"
                else self.opened_url
            )
            return _completed(
                call,
                stdout=json.dumps({"page": page, "url": url}).encode(),
            )
        if "open" in call:
            url = call[call.index("open") + 1]
            return _completed(
                call,
                stdout=json.dumps({"page": _TARGET, "url": url}).encode(),
                returncode=1 if self.failure == "open" else 0,
            )
        if "eval" in call:
            if self.failure == "eval_timeout":
                raise subprocess.TimeoutExpired(call, timeout)
            document: dict[str, object] = {
                "url": self.opened_url,
                "navigationStatus": 200,
                "contentType": "application/json",
                "charset": "UTF-8",
                "preCount": 1,
                "byteLength": len(self.payload),
                "base64": base64.b64encode(self.payload).decode(),
            }
            if self.failure == "status":
                document["navigationStatus"] = 503
            elif self.failure == "content_type":
                document["contentType"] = "text/html"
            elif self.failure == "pre":
                document["preCount"] = 2
            elif self.failure == "bytes":
                document["byteLength"] = 64 * 1024 + 1
                document["base64"] = None
            return _completed(call, stdout=json.dumps(document).encode())
        if "list" in call:
            if self.failure == "sweep_raise":
                raise OSError("tab list spawn blip")
            # new_timeout 的残留枚举只发生在 tab new 之后（入口 sweep 时
            # 残留尚未"打开"，返回空表）。
            if (
                self.failure == "new_timeout"
                and self.residual_url is not None
                and self.saw_tab_new
            ):
                return _completed(
                    call,
                    stdout=json.dumps(
                        [
                            {"page": _TARGET, "url": self.residual_url},
                            {"page": _SECOND_TARGET, "url": self.residual_url},
                        ]
                    ).encode(),
                )
            if self.sweep_tabs is not None:
                return _completed(call, stdout=json.dumps(self.sweep_tabs).encode())
            return _completed(call, stdout=b"[]")
        closed_target = call[-1] if "close" in call else _TARGET
        return _completed(
            call,
            stdout=json.dumps({"closed": closed_target}).encode(),
            returncode=1 if self.failure == "close" else 0,
        )


@pytest.mark.parametrize(
    ("status_code", "content"),
    [
        (200, _RAW_PAYLOAD),
        (503, b'{"message":"unavailable"}'),
        (200, b'{"rc":0,"data":'),
    ],
)
def test_primary_response_is_returned_unchanged_without_opencli(
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

    runner = _OpenCliRunner()
    response = _transport(primary_get=primary_get, runner=runner).fetch(
        _QUOTE_REQUEST,
        timeout=8.0,
    )

    assert response is expected
    assert calls == [2.0]
    assert runner.calls == []


def test_non_requests_exception_never_enters_opencli() -> None:
    runner = _OpenCliRunner()

    def invalid_primary(*args, **kwargs):
        raise ValueError("adapter failure")

    with pytest.raises(ValueError, match="adapter failure"):
        _transport(primary_get=invalid_primary, runner=runner).fetch(
            _QUOTE_REQUEST,
            timeout=8.0,
        )

    assert runner.calls == []


def test_request_exception_runs_one_exact_target_lifecycle() -> None:
    runner = _OpenCliRunner()

    response = _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert response.status_code == 200
    assert response.content == _RAW_PAYLOAD
    # probe + resolve + sweep(tab list) + tab new + open + eval + close
    assert len(runner.calls) == 7
    assert runner.calls[0][-2:] == ("-Command", "exit 0")  # interop probe
    assert {call[0] for call in runner.calls} == {
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    }
    for call in runner.calls[2:]:
        file_index = call.index("-File")
        assert call[file_index + 2 : file_index + 6] == (
            "--profile",
            "windows-default",
            "browser",
            "fin-eastmoney-on-demand-v1",
        )
    assert runner.calls[2][-2:] == ("tab", "list")  # 入口残留 sweep
    assert runner.calls[3][-5:-1] == (
        "browser",
        "fin-eastmoney-on-demand-v1",
        "tab",
        "new",
    )
    assert runner.calls[3][-1] == runner.opened_url
    assert runner.opened_url == _QUOTE_REQUEST.canonical_url
    assert runner.calls[4][-4:] == (
        "open",
        _QUOTE_REQUEST.canonical_url,
        "--tab",
        _TARGET,
    )
    assert runner.calls[-1][-3:] == ("tab", "close", _TARGET)


def test_opencli_lifecycle_binds_one_resolved_profile() -> None:
    runner = _OpenCliRunner(profile="FIN-MARKET")

    response = _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert response.content == _RAW_PAYLOAD
    for call in runner.calls[3:]:
        file_index = call.index("-File")
        assert call[file_index + 2 : file_index + 6] == (
            "--profile",
            "FIN-MARKET",
            "browser",
            "fin-eastmoney-on-demand-v1",
        )


def test_explicit_profile_pin_overrides_windows_default(monkeypatch) -> None:
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    monkeypatch.setenv("FIN_OPENCLI_PROFILE", "FIN-ZSXQ")
    runner = _OpenCliRunner(profile="windows-default")

    response = _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert response.content == _RAW_PAYLOAD
    assert runner.calls[1][-1] == transport._RESOLVE_OPENCLI
    for call in runner.calls[3:]:
        file_index = call.index("-File")
        assert call[file_index + 2 : file_index + 4] == (
            "--profile",
            "FIN-ZSXQ",
        )


def test_invalid_resolved_profile_fails_before_browser_commands() -> None:
    runner = _OpenCliRunner(profile="not a profile")

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_OPENCLI_PROFILE_INVALID$",
    ):
        _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert len(runner.calls) == 2  # interop probe + local resolver only


def test_missing_resolved_profile_fails_before_browser_commands() -> None:
    runner = _OpenCliRunner(profile=None)

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_OPENCLI_PROFILE_INVALID$",
    ):
        _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert len(runner.calls) == 2  # interop probe + local resolver only


def test_invalid_explicit_profile_pin_fails_before_local_resolver(monkeypatch) -> None:
    monkeypatch.setenv("FIN_OPENCLI_PROFILE", "not a profile")
    runner = _OpenCliRunner(profile="windows-default")

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_OPENCLI_PROFILE_INVALID$",
    ):
        _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert len(runner.calls) == 1  # interop probe only; no resolver/browser command


def test_realistic_daily_payload_fits_the_existing_four_mib_adapter_cap() -> None:
    payload = b"x" * 335_630
    runner = _OpenCliRunner(payload=payload)

    response = _transport(runner=runner).fetch(_DAILY_REQUEST, timeout=8.0)

    assert response.content == payload
    assert runner.calls[-1][-3:] == ("tab", "close", _TARGET)


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("target", "EASTMONEY_ON_DEMAND_OPENCLI_TARGET_INVALID"),
        ("final_url", "EASTMONEY_ON_DEMAND_OPENCLI_TARGET_INVALID"),
        ("status", "EASTMONEY_ON_DEMAND_OPENCLI_DOCUMENT_INVALID"),
        ("content_type", "EASTMONEY_ON_DEMAND_OPENCLI_DOCUMENT_INVALID"),
        ("pre", "EASTMONEY_ON_DEMAND_OPENCLI_DOCUMENT_INVALID"),
        ("bytes", "EASTMONEY_ON_DEMAND_OPENCLI_PAYLOAD_TOO_LARGE"),
    ],
)
def test_invalid_navigation_or_document_still_closes_exact_target(
    failure: str,
    expected_code: str,
) -> None:
    runner = _OpenCliRunner(failure=failure)

    with pytest.raises(EastmoneyOnDemandTransportError, match=f"^{expected_code}$"):
        _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    if failure == "target":
        # probe + resolve + 入口 sweep(tab list) + tab new + 残留枚举(tab list)
        assert len(runner.calls) == 5
    else:
        assert runner.calls[-1][-3:] == ("tab", "close", _TARGET)


def test_all_commands_share_one_deadline_and_reserve_close_budget() -> None:
    clock = _Clock(0.0)
    runner = _OpenCliRunner(clock=clock)

    def primary_get(*args, timeout: float, **kwargs):
        assert timeout == 2.0
        clock.value += 2.0
        raise requests.ConnectionError("remote closed")

    _transport(primary_get=primary_get, runner=runner, clock=clock).fetch(
        _QUOTE_REQUEST,
        timeout=8.0,
    )

    # probe 用独立短预算 2.0s；close 用实测足够的独立 3.0s 预算。
    # 预算紧张（剩余 < 6.0s）时入口 sweep 直接跳过——本请求优先。
    assert runner.timeouts == pytest.approx([2.0, 2.5, 2.0, 1.5, 1.0, 3.0])


def test_entry_sweep_closes_matching_host_residuals_before_tab_new() -> None:
    """残留 tab 缺口：入口 sweep 回收上一轮 close 失败遗留的同端点 tab。"""
    clock = _Clock(value=100.0)
    third = "C" * 32
    runner = _OpenCliRunner(
        clock=clock,
        sweep_tabs=[
            {
                "page": _TARGET,
                "url": "https://push2delay.eastmoney.com/api/qt/stock/get?secid=1.601899",
            },
            {"page": _SECOND_TARGET, "url": "https://wx.zsxq.com/group/15522441811252"},
            {
                "page": third,
                "url": "https://push2delay.eastmoney.com/api/qt/stock/get?secid=0.002409",
            },
        ],
    )

    response = _transport(runner=runner, clock=clock).fetch(_QUOTE_REQUEST, timeout=10.0)

    assert response.status_code == 200
    assert runner.calls[2][-2:] == ("tab", "list")  # sweep 先枚举
    assert runner.calls[3][-3:] == ("tab", "close", _TARGET)  # 再逐个关
    assert runner.calls[4][-3:] == ("tab", "close", third)
    assert runner.calls[5][-3:-1] == ("tab", "new")  # 然后才开本请求的 tab
    close_targets = [call[-1] for call in runner.calls if "close" in call]
    # 只关端点 host 的残留 + 本请求自己的 tab；zsxq tab 不动。
    assert close_targets == [_TARGET, third, _TARGET]
    assert all(target != _SECOND_TARGET for target in close_targets)


def test_entry_sweep_caps_at_three_tabs_per_request() -> None:
    """单次 sweep 最多回收 3 个——大积压分摊到多个请求，不挤占本请求预算。"""
    clock = _Clock(value=100.0)
    runner = _OpenCliRunner(
        clock=clock,
        sweep_tabs=[
            {
                "page": chr(ord("A") + index) * 32,
                "url": "https://push2delay.eastmoney.com/api/qt/stock/get",
            }
            for index in range(5)
        ],
    )

    response = _transport(runner=runner, clock=clock).fetch(_QUOTE_REQUEST, timeout=10.0)

    assert response.status_code == 200
    close_targets = [call[-1] for call in runner.calls if "close" in call]
    # 只 sweep 关 A/B/C 三个，D/E 留给后续请求；最后一个 close 是本请求自己的 tab。
    assert close_targets == ["A" * 32, "B" * 32, "C" * 32, _TARGET]


def test_entry_sweep_failure_never_breaks_the_request() -> None:
    """sweep 的 tab list 抛错（spawn 瞬断）只静默跳过，主请求照常完成。"""
    runner = _OpenCliRunner(failure="sweep_raise")

    response = _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert response.status_code == 200
    assert response.content == _RAW_PAYLOAD


def test_close_failure_retries_once_with_fresh_budget_before_warning() -> None:
    """close 首次失败用独立短预算立即重试一次；仍失败才放弃（warning）。"""
    runner = _OpenCliRunner(failure="close")

    response = _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert response.status_code == 200  # close 失败不覆盖主请求结果
    close_calls = [call for call in runner.calls if "close" in call]
    assert len(close_calls) == 2
    assert runner.timeouts[-2:] == pytest.approx([3.0, 3.0])


def test_exhausted_fallback_budget_and_close_failure_both_fail_closed() -> None:
    clock = _Clock(0.0)
    unused_runner = _OpenCliRunner()

    def late_primary(*args, **kwargs):
        clock.value = 7.0
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

    # 做稳：close 失败不再覆盖主请求结果（标签尽力清理，warning 记录），
    # 请求本身成功返回——避免一次 close 失败让整个读取 fail-closed。
    close_failure = _OpenCliRunner(failure="close")
    response = _transport(runner=close_failure).fetch(_QUOTE_REQUEST, timeout=8.0)
    assert response.status_code == 200


def test_test_core_cannot_be_mutated_into_live_authority() -> None:
    runner = _OpenCliRunner()
    core = _transport(runner=runner)

    assert not _is_production_on_demand_http_get(core)
    with pytest.raises(AttributeError):
        core._production_eligible = True  # type: ignore[attr-defined]
    assert _is_production_on_demand_http_get(_build_eastmoney_on_demand_http_get())


def test_forged_typed_request_cannot_change_endpoint_or_duplicate_query() -> None:
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
    runner = _OpenCliRunner()

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_REQUEST_INVALID$",
    ):
        _transport(runner=runner).fetch(mutated, timeout=8.0)
    assert runner.calls == []


def test_eval_timeout_closes_known_target_and_never_returns_payload() -> None:
    runner = _OpenCliRunner(failure="eval_timeout")

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_OPENCLI_COMMAND_FAILED$",
    ):
        _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert runner.calls[-1][-3:] == ("tab", "close", _TARGET)


def test_open_failure_closes_known_target_and_never_evaluates() -> None:
    runner = _OpenCliRunner(failure="open")

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_OPENCLI_COMMAND_FAILED$",
    ):
        _transport(runner=runner).fetch(_QUOTE_REQUEST, timeout=8.0)

    assert runner.calls[-1][-3:] == ("tab", "close", _TARGET)
    assert not any("eval" in call for call in runner.calls)


def _transport(
    *,
    runner: _OpenCliRunner,
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


def _completed(
    argv: Sequence[str],
    *,
    stdout: bytes,
    returncode: int = 0,
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=argv,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_opencli_environment_prefers_ambient_wsl_interop(monkeypatch) -> None:
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    monkeypatch.setattr(
        transport,
        "_trusted_wsl_interop_alias",
        lambda: pytest.fail("safe alias fallback must not replace ambient identity"),
        raising=False,
    )

    environment = transport._opencli_environment({"WSL_INTEROP": "/run/WSL/987_interop"})

    assert environment["WSL_INTEROP"] == "/run/WSL/987_interop"


def test_opencli_environment_uses_only_the_safe_fixed_interop_alias(monkeypatch) -> None:
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    monkeypatch.setattr(
        transport,
        "_trusted_wsl_interop_alias",
        lambda: "/run/WSL/1_interop",
        raising=False,
    )

    environment = transport._opencli_environment({})

    assert environment["WSL_INTEROP"] == "/run/WSL/1_interop"


def test_trusted_wsl_interop_alias_requires_controlled_parents_and_root_socket() -> None:
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    alias = Path("/run/WSL/1_interop")
    target = Path("/run/WSL/987_interop")
    metadata = {
        Path("/run"): SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0),
        Path("/run/WSL"): SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0),
        alias: SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0),
    }

    resolved = transport._trusted_wsl_interop_alias(
        lstat_path=lambda path: metadata[Path(path)],
        resolve_path=lambda path: target,
        stat_path=lambda path: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o777, st_uid=0),
    )

    assert resolved == str(alias)


@pytest.mark.parametrize(
    ("parent_mode", "alias_mode", "target_path", "target_mode", "target_uid"),
    [
        (0o777, stat.S_IFLNK, "/run/WSL/987_interop", stat.S_IFSOCK, 0),
        (0o755, stat.S_IFREG, "/run/WSL/987_interop", stat.S_IFSOCK, 0),
        (0o755, stat.S_IFLNK, "/tmp/987_interop", stat.S_IFSOCK, 0),
        (0o755, stat.S_IFLNK, "/run/WSL/user/987_interop", stat.S_IFSOCK, 0),
        (0o755, stat.S_IFLNK, "/run/WSL/987_interop", stat.S_IFREG, 0),
        (0o755, stat.S_IFLNK, "/run/WSL/987_interop", stat.S_IFSOCK, 1000),
    ],
)
def test_trusted_wsl_interop_alias_rejects_untrusted_shapes(
    parent_mode: int,
    alias_mode: int,
    target_path: str,
    target_mode: int,
    target_uid: int,
) -> None:
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    alias = Path("/run/WSL/1_interop")
    metadata = {
        Path("/run"): SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0),
        Path("/run/WSL"): SimpleNamespace(st_mode=stat.S_IFDIR | parent_mode, st_uid=0),
        alias: SimpleNamespace(st_mode=alias_mode | 0o777, st_uid=0),
    }

    resolved = transport._trusted_wsl_interop_alias(
        lstat_path=lambda path: metadata[Path(path)],
        resolve_path=lambda path: Path(target_path),
        stat_path=lambda path: SimpleNamespace(
            st_mode=target_mode | 0o777,
            st_uid=target_uid,
        ),
    )

    assert resolved is None


def test_opencli_failure_enters_cooldown_and_skips_next_attempt() -> None:
    """opencli 失败后 TTL 内跳过——不每次等 vsock 超时（用户决策 2026-08-02）。"""
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    clock = _Clock(value=100.0)
    runner = _OpenCliRunner(failure="status", clock=clock)
    transport._OPENCLI_FAILED_AT = None

    # 第一次：requests 失败 → opencli 尝试（失败）→ 记录 cooldown
    with pytest.raises(EastmoneyOnDemandTransportError, match="DOCUMENT_INVALID"):
        _transport(runner=runner, clock=clock).fetch(_QUOTE_REQUEST, timeout=10.0)
    assert transport._OPENCLI_FAILED_AT is not None
    assert transport._OPENCLI_FAILED_AT >= 100.0

    # 第二次（cooldown 内）：直接 OPENCLI_COOLDOWN_ACTIVE，不调 opencli
    runner.calls.clear()
    with pytest.raises(EastmoneyOnDemandTransportError, match="OPENCLI_COOLDOWN_ACTIVE"):
        _transport(runner=runner, clock=clock).fetch(_QUOTE_REQUEST, timeout=10.0)
    assert runner.calls == []

    # TTL 过后：重新尝试 opencli（基于实际失败记录时刻）
    clock.value = transport._OPENCLI_FAILED_AT + transport._OPENCLI_FAILURE_COOLDOWN_SECONDS + 1
    runner2 = _OpenCliRunner(failure="status", clock=clock)
    with pytest.raises(EastmoneyOnDemandTransportError, match="DOCUMENT_INVALID"):
        _transport(runner=runner2, clock=clock).fetch(_QUOTE_REQUEST, timeout=10.0)
    assert runner2.calls != []


def test_opencli_success_clears_cooldown() -> None:
    """opencli 成功后不再被 cooldown 阻断。"""
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    clock = _Clock(value=200.0)
    transport._OPENCLI_FAILED_AT = 100.0  # 模拟之前失败

    def primary_ok(*args, **kwargs):
        return _Response(status_code=200, content=_RAW_PAYLOAD)

    result = _transport(
        runner=_OpenCliRunner(clock=clock), primary_get=primary_ok, clock=clock
    ).fetch(_QUOTE_REQUEST, timeout=10.0)
    assert result.content == _RAW_PAYLOAD  # primary 成功，不触发 cooldown 检查


@pytest.mark.parametrize(
    ("failure",),
    [("probe_exit",), ("probe_raise",), ("probe_timeout",)],
)
def test_interop_probe_failure_is_typed_interop_unavailable_and_marks_cooldown(
    failure: str,
) -> None:
    """vsock/interop 故障：probe 在专用短预算内 typed 失败，无后续 spawn，进入 TTL 冷却。"""
    import fin_analyse.market.qualification_sources.eastmoney_http_transport as transport

    transport._OPENCLI_FAILED_AT = None
    clock = _Clock(value=100.0)
    runner = _OpenCliRunner(failure=failure, clock=clock)

    with pytest.raises(
        EastmoneyOnDemandTransportError,
        match="^EASTMONEY_ON_DEMAND_OPENCLI_INTEROP_UNAVAILABLE$",
    ):
        _transport(runner=runner, clock=clock).fetch(_QUOTE_REQUEST, timeout=10.0)

    assert len(runner.calls) == 1  # 只有 probe 一次 spawn，不再走 resolve/tab/eval
    assert transport._OPENCLI_FAILED_AT is not None
    assert transport._OPENCLI_FAILED_AT >= 100.0

    # TTL 内：直接 OPENCLI_COOLDOWN_ACTIVE，连 probe 都不触发
    runner.calls.clear()
    with pytest.raises(EastmoneyOnDemandTransportError, match="OPENCLI_COOLDOWN_ACTIVE"):
        _transport(runner=runner, clock=clock).fetch(_QUOTE_REQUEST, timeout=10.0)
    assert runner.calls == []


def test_interop_probe_uses_dedicated_budget_not_the_whole_remaining() -> None:
    """probe 预算 = min(剩余, 2.0)——健康时完整生命周期仍走通。"""
    clock = _Clock(value=100.0)
    runner = _OpenCliRunner(clock=clock)

    def late_primary(*args, **kwargs):
        clock.value = 102.0
        raise requests.ConnectionError("remote closed")

    response = _transport(runner=runner, primary_get=late_primary, clock=clock).fetch(
        _QUOTE_REQUEST,
        timeout=10.0,
    )
    assert response.status_code == 200
    assert runner.timeouts[0] == pytest.approx(2.0)  # probe 独立短预算
    assert runner.timeouts[1] == pytest.approx(4.5)  # resolve: 110 - 102.5 - 3.0


def test_daily_bar_tab_new_timeout_enumerates_and_closes_residual_by_endpoint_host() -> None:
    """push2his（日线）tab-new 超时也按 endpoint host 枚举清理残留 tab。"""
    clock = _Clock(0.0)
    runner = _OpenCliRunner(
        failure="new_timeout",
        clock=clock,
        residual_url=("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.601899"),
    )

    with pytest.raises(EastmoneyOnDemandTransportError, match="OPENCLI_COMMAND_FAILED"):
        _transport(runner=runner, clock=clock).fetch(_DAILY_REQUEST, timeout=8.0)

    # probe + resolve + 入口 sweep(tab list) + tab new(超时) + tab list(枚举)
    # + 两个 residual close。
    assert len(runner.calls) == 7
    assert runner.calls[-3][-2:] == ("tab", "list")
    assert runner.calls[-2][-3:] == ("tab", "close", _TARGET)
    assert runner.calls[-1][-3:] == ("tab", "close", _SECOND_TARGET)
    assert runner.timeouts[-3:] == pytest.approx([3.0, 2.5, 2.0])
