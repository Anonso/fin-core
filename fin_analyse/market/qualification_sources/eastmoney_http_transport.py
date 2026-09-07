"""Requests-first Eastmoney transport for bounded on-demand advisory reads.

Fallback = one WSL-local headless Chrome subprocess (``--dump-dom``), never a
persistent browser: the process exits with the call, so nothing can leak in
any user-visible browser. Windows Chrome is reserved for the ZSXQ domain
(owner 2026-09-07).
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

import requests

from fin_analyse.common.bounded_process import run_bounded_command
from fin_analyse.market.qualification_sources.eastmoney_request_contract import (
    EastmoneyHttpRequest,
)

_logger = logging.getLogger(__name__)
_PRIMARY_TIMEOUT_SECONDS = 2.0
_MIN_FALLBACK_SECONDS = 1.0
# 兜底浏览器故障记忆：typed 失败后 TTL 内跳过，避免每次请求都重付一次
# 无头 spawn 的秒级成本（push2his 被墙时每次注定失败）。TTL 过后自动重试。
_BROWSER_FAILURE_COOLDOWN_SECONDS = 300.0
_BROWSER_FAILED_AT: float | None = None
# 系统代理策略对无头浏览器同样透传（env 即配置：owner 调整 Clash 分流规则
# 后无需改代码即可生效）。
_BROWSER_PROXY_ENV_VARS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)
_BROWSER_CANDIDATES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
)
# DOM 身份门槛标记：Chrome 只对 application/json 顶层导航注入 JSON viewer
# 外壳（实测 Chrome 149）。二进制导航到 HTML 页（劫持/重定向）不会有该标记
# ——漂移时 typed fail-closed，绝不静默放开。
_JSON_VIEWER_MARKER = "json-formatter-container"
# DOM dump 相对原始 JSON 的实体膨胀上界（``&``→``&amp;`` 单字符 5 倍）。
_DOM_EXPANSION_FACTOR = 5
_DOM_WRAPPER_SLACK_BYTES = 64 * 1024
# 浏览器 profile/缓存内部写的 RLIMIT_FSIZE 余量（与 stdout 上限解耦）。
_BROWSER_FSIZE_HEADROOM_BYTES = 64 * 1024 * 1024


class EastmoneyOnDemandTransportError(RuntimeError):
    """Stable failure raised when the bounded headless fallback is unusable."""

    pass


class _HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


_PrimaryGet = Callable[..., _HttpResponse]
_CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True, slots=True)
class _FallbackResponse:
    status_code: int
    content: bytes


def _browser_cooldown_active(monotonic: Callable[[], float]) -> bool:
    global _BROWSER_FAILED_AT
    if _BROWSER_FAILED_AT is None:
        return False
    return monotonic() - _BROWSER_FAILED_AT < _BROWSER_FAILURE_COOLDOWN_SECONDS


def _mark_browser_failed(monotonic: Callable[[], float]) -> None:
    global _BROWSER_FAILED_AT
    _BROWSER_FAILED_AT = monotonic()


class _EastmoneyOnDemandTransportCore:
    """Testable transport logic; this exact type is never LIVE-authorized."""

    __slots__ = ("_command_runner", "_monotonic", "_primary_get")

    def __init__(
        self,
        *,
        primary_get: _PrimaryGet,
        command_runner: _CommandRunner,
        monotonic: Callable[[], float],
    ) -> None:
        self._primary_get = primary_get
        self._command_runner = command_runner
        self._monotonic = monotonic

    def fetch(
        self,
        request: EastmoneyHttpRequest,
        *,
        timeout: float,
    ) -> _HttpResponse:
        spec = _validate_request(request, timeout=timeout)
        deadline = self._monotonic() + float(timeout)
        primary_timeout = min(
            _PRIMARY_TIMEOUT_SECONDS,
            _remaining(deadline, self._monotonic),
        )
        if primary_timeout <= 0:
            raise _error("TRANSPORT_DEADLINE_REACHED")
        try:
            response = self._primary_get(
                spec.endpoint,
                params=spec.params_dict(),
                headers=spec.headers_dict(),
                timeout=primary_timeout,
                allow_redirects=False,
            )
            if _remaining(deadline, self._monotonic) <= 0:
                raise _error("TRANSPORT_DEADLINE_REACHED")
            return response
        except requests.RequestException as primary_error:
            if _remaining(deadline, self._monotonic) < _MIN_FALLBACK_SECONDS:
                raise _error("FALLBACK_DEADLINE_REACHED") from primary_error
            if _browser_cooldown_active(self._monotonic):
                raise _error("BROWSER_COOLDOWN_ACTIVE") from primary_error
            try:
                return self._read_with_browser(spec, deadline=deadline)
            except EastmoneyOnDemandTransportError:
                _mark_browser_failed(self._monotonic)
                raise

    def _read_with_browser(
        self,
        spec: EastmoneyHttpRequest,
        *,
        deadline: float,
    ) -> _FallbackResponse:
        browser = _resolve_browser_binary()
        remaining = _remaining(deadline, self._monotonic)
        if remaining <= 0:
            raise _error("BROWSER_DEADLINE_REACHED")
        # --user-data-dir 每次全新：无 SingletonLock 残留、不依赖 $HOME；
        # rmtree 尽力而为——被墙端点的常态失败路径下每次可残留 ~4MB profile
        # 于 /tmp，依赖系统 tmp 清理（审计 P2-2 量化照录）。
        user_data_dir = Path(tempfile.mkdtemp(prefix="fin-eastmoney-headless-"))
        try:
            argv = (
                browser,
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--disable-extensions",
                "--mute-audio",
                f"--user-data-dir={user_data_dir}",
                "--dump-dom",
                f"--virtual-time-budget={max(1, int(remaining * 1000))}",
                spec.canonical_url,
            )
            try:
                completed = self._command_runner(
                    argv,
                    timeout=remaining,
                    env=_browser_environment(user_data_dir),
                    max_output_bytes=(
                        spec.maximum_payload_bytes * _DOM_EXPANSION_FACTOR
                        + _DOM_WRAPPER_SLACK_BYTES
                    ),
                    # Chrome 会写自己的 profile/缓存（实测 ~4MB）：RLIMIT_FSIZE
                    # 若与 stdout 上限同源（384KB）会打死网络服务进程、无限重启
                    # 挂满预算。stdout 上限不受此影响——仍由执行器轮询强制。
                    fsize_limit_bytes=_BROWSER_FSIZE_HEADROOM_BYTES,
                )
            except (
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                raise _error("BROWSER_COMMAND_FAILED") from error
        finally:
            shutil.rmtree(user_data_dir, ignore_errors=True)
        if (
            isinstance(completed.returncode, bool)
            or not isinstance(completed.returncode, int)
            or not isinstance(completed.stdout, bytes)
            or not isinstance(completed.stderr, bytes)
        ):
            raise _error("BROWSER_OUTPUT_INVALID")
        if completed.returncode != 0:
            raise _error("BROWSER_COMMAND_FAILED")
        if _remaining(deadline, self._monotonic) <= 0:
            raise _error("BROWSER_DEADLINE_REACHED")
        return _response_from_dom(completed.stdout, spec=spec)


class _EastmoneyOnDemandHttpGet:
    """Stateless production facade with fixed real dependencies."""

    __slots__ = ()

    def fetch(
        self,
        request: EastmoneyHttpRequest,
        *,
        timeout: float,
    ) -> _HttpResponse:
        return _EastmoneyOnDemandTransportCore(
            primary_get=requests.get,
            command_runner=_run_command,
            monotonic=time.monotonic,
        ).fetch(request, timeout=timeout)


def _build_eastmoney_on_demand_http_get() -> _EastmoneyOnDemandHttpGet:
    return _EastmoneyOnDemandHttpGet()


def _is_production_on_demand_http_get(value: object) -> bool:
    return type(value) is _EastmoneyOnDemandHttpGet


def _error(code: str) -> EastmoneyOnDemandTransportError:
    return EastmoneyOnDemandTransportError(f"EASTMONEY_ON_DEMAND_{code}")


def _resolve_browser_binary(
    which: Callable[[str], str | None] = None,
) -> str:
    probe = shutil.which if which is None else which
    configured = os.environ.get("FIN_EASTMONEY_BROWSER_BIN")
    if configured is not None:
        candidate = Path(configured)
        # 审计 P3-8：拒绝 /mnt/ 下的 Windows 侧二进制——env pin 不得把
        # Windows Chrome 指回兜底（Windows 浏览器只许 ZSXQ，D-052）。
        if (
            candidate.is_absolute()
            and not str(candidate).startswith("/mnt/")
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ):
            return str(candidate)
        raise _error("BROWSER_UNAVAILABLE")
    for name in _BROWSER_CANDIDATES:
        found = probe(name)
        if found:
            return found
    raise _error("BROWSER_UNAVAILABLE")


def _browser_environment(
    user_data_dir: Path,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "HOME": str(user_data_dir),
    }
    for variable in _BROWSER_PROXY_ENV_VARS:
        value = source.get(variable)
        if value:
            environment[variable] = value
    return environment


class _SinglePreTextCollector(HTMLParser):
    """收集 DOM 中唯一的 ``<pre>`` 文本（实体已由 parser 反转义）。"""

    __slots__ = ("_pre_depth", "chunks", "pre_count")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pre_depth = 0
        self.chunks: list[str] = []
        self.pre_count = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "pre":
            self.pre_count += 1
            self._pre_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._pre_depth:
            self.chunks.append(data)


def _response_from_dom(
    stdout: bytes,
    *,
    spec: EastmoneyHttpRequest,
) -> _FallbackResponse:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _error("BROWSER_DOCUMENT_INVALID") from error
    if _JSON_VIEWER_MARKER not in text:
        raise _error("BROWSER_DOCUMENT_INVALID")
    collector = _SinglePreTextCollector()
    try:
        collector.feed(text)
        collector.close()
    except Exception as error:  # noqa: BLE001 — 解析器异常同样 fail-closed
        raise _error("BROWSER_DOCUMENT_INVALID") from error
    if collector.pre_count != 1 or not collector.chunks:
        raise _error("BROWSER_DOCUMENT_INVALID")
    content = "".join(collector.chunks).encode("utf-8")
    if len(content) > spec.maximum_payload_bytes:
        raise _error("BROWSER_PAYLOAD_TOO_LARGE")
    try:
        _strict_json_value(content)
    except EastmoneyOnDemandTransportError as error:
        raise _error("BROWSER_DOCUMENT_INVALID") from error
    # status_code=200 为兜底合成值：--dump-dom 观测不到 HTTP 状态码，非 200
    # 的 JSON 错误体也会过本门槛；数据正确性由下游 envelope（rc==0）校验
    # fail-closed 兜住（审计 P2-6）。
    return _FallbackResponse(status_code=200, content=content)


def _validate_request(
    request: EastmoneyHttpRequest,
    *,
    timeout: float,
) -> EastmoneyHttpRequest:
    if (
        type(request) is not EastmoneyHttpRequest
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise _error("REQUEST_INVALID")
    try:
        request.validate()
    except (TypeError, ValueError) as error:
        raise _error("REQUEST_INVALID") from error
    return request


def _strict_json_value(payload: bytes) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeError, ValueError) as error:
        raise _error("BROWSER_OUTPUT_INVALID") from error


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def _remaining(deadline: float, monotonic: Callable[[], float]) -> float:
    return max(0.0, deadline - monotonic())


def _run_command(
    argv: Sequence[str],
    *,
    timeout: float,
    env: dict[str, str],
    max_output_bytes: int,
    fsize_limit_bytes: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    completed = run_bounded_command(
        tuple(argv),
        cwd=Path("/"),
        env=env,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        fsize_limit_bytes=fsize_limit_bytes,
    )
    return subprocess.CompletedProcess(
        args=completed.args,
        returncode=completed.returncode,
        stdout=completed.stdout.encode("utf-8"),
        stderr=completed.stderr.encode("utf-8"),
    )


__all__ = [
    "EastmoneyOnDemandTransportError",
]
