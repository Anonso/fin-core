"""Requests-first Eastmoney transport for bounded on-demand advisory reads."""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

import requests

from fin_analyse.common.bounded_process import run_bounded_command
from fin_analyse.market.qualification_sources.eastmoney_request_contract import (
    EASTMONEY_DAILY_BAR_MAX_RAW_BYTES,
    EastmoneyHttpRequest,
)

_logger = logging.getLogger(__name__)
_SESSION = "fin-eastmoney-on-demand-v1"
_PRIMARY_TIMEOUT_SECONDS = 2.0
_CLOSE_RESERVE_SECONDS = 3.0
_MIN_FALLBACK_SECONDS = 1.0
# 入口残留 sweep：tab close 单次失败只记 warning，标签会残留在 Chrome 里。
# opencli 的 session↔tab 注册表仅存于 daemon 内存，daemon 重启后残留连
# ``tab list`` 都看不见，只能手动关。这里在开新 tab 前枚举同 session 内
# 匹配端点 host 的残留并回收，把泄漏上界压到 1 个/请求；预算不足或任何
# 异常都静默跳过，绝不拖垮本请求。
_SWEEP_MAX_TABS = 3
_SWEEP_MIN_REMAINING_SECONDS = 6.0
_MAX_STDOUT_BYTES = ((EASTMONEY_DAILY_BAR_MAX_RAW_BYTES + 2) // 3) * 4 + 64 * 1024
_TARGET_ID = re.compile(r"^[0-9A-Fa-f]{32}$")
_OPENCLI_PATH = re.compile(r"^[A-Za-z]:\\.+\\npm\\opencli\.ps1$")
_OPENCLI_PROFILE = re.compile(r"^[^\s\x00]{1,256}$")
_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
_WSL_RUN_ROOT = Path("/run")
_WSL_INTEROP_ROOT = _WSL_RUN_ROOT / "WSL"
_WSL_INTEROP_ALIAS = _WSL_INTEROP_ROOT / "1_interop"
_RESOLVE_OPENCLI = (
    "$ErrorActionPreference='Stop';"
    "$expected=Join-Path $env:APPDATA 'npm\\opencli.ps1';"
    "$resolved=(Resolve-Path -LiteralPath $expected).ProviderPath;"
    "$item=Get-Item -LiteralPath $resolved;"
    "if($item.PSIsContainer -or "
    "(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)){exit 3};"
    "[Console]::Out.Write("
    "(ConvertTo-Json -Compress @{path=$item.FullName}))"
)
_RESOLVE_MARKET_OPENCLI = (
    "$ErrorActionPreference='Stop';"
    "$expected=Join-Path $env:APPDATA 'npm\\opencli.ps1';"
    "$resolved=(Resolve-Path -LiteralPath $expected).ProviderPath;"
    "$item=Get-Item -LiteralPath $resolved;"
    "if($item.PSIsContainer -or "
    "(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)){exit 3};"
    "$profile=$null;"
    "$configPath=Join-Path $env:USERPROFILE '.opencli\\browser-profiles.json';"
    "if(Test-Path -LiteralPath $configPath -PathType Leaf){"
    "$config=Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json;"
    "$profile=$config.defaultContextId;"
    "}"
    "$payload=@{path=$item.FullName};"
    "if(-not [String]::IsNullOrWhiteSpace($profile)){"
    "$payload.profile=[string]$profile"
    "};"
    "[Console]::Out.Write((ConvertTo-Json -Compress $payload))"
)
_EVAL_DOCUMENT_TEMPLATE = """
(() => {
  const entries = performance.getEntriesByType('navigation');
  const pres = document.querySelectorAll('pre');
  const status = entries.length === 1 && Number.isInteger(entries[0].responseStatus)
    ? entries[0].responseStatus : null;
  const text = pres.length === 1 ? pres[0].textContent : null;
  const bytes = typeof text === 'string' ? new TextEncoder().encode(text) : null;
  const byteLength = bytes === null ? null : bytes.byteLength;
  let binary = '';
  if (Number.isInteger(byteLength) && byteLength <= MAX_PAYLOAD_BYTES) {
    for (let i = 0; i < byteLength; i += 32768) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
    }
  }
  return {
    url: location.href,
    navigationStatus: status,
    contentType: document.contentType,
    charset: document.characterSet,
    preCount: pres.length,
    byteLength,
    base64: Number.isInteger(byteLength) && byteLength <= MAX_PAYLOAD_BYTES
      ? btoa(binary) : null
  };
})()
""".strip()


class EastmoneyOnDemandTransportError(RuntimeError):
    """Stable failure raised when the bounded Chrome fallback is unusable."""

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


# opencli（Windows OpenCLI Chrome）故障记忆：失败后 TTL 内跳过，避免每次
# 请求都等它的 vsock 超时（WSL interop 层故障时 opencli 会整体不可用）。
# TTL 过后自动重试——opencli 恢复后无需重启即可用回。
_OPENCLI_FAILURE_COOLDOWN_SECONDS = 300.0
_OPENCLI_FAILED_AT: float | None = None

# WSL interop/vsock 故障时第一个 powershell spawn 可能挂满整个剩余预算；
# probe 用独立短预算先验证 Windows 进程 spawn 能力（exit 0），失败即 typed
# 标识环境不可用，与 opencli 自身问题（OPENCLI_COMMAND_FAILED）区分开。
_INTEROP_PROBE_ARGV = (
    _POWERSHELL,
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    "exit 0",
)
_INTEROP_PROBE_SECONDS = 2.0


def _opencli_cooldown_active(monotonic: Callable[[], float]) -> bool:
    global _OPENCLI_FAILED_AT
    if _OPENCLI_FAILED_AT is None:
        return False
    return monotonic() - _OPENCLI_FAILED_AT < _OPENCLI_FAILURE_COOLDOWN_SECONDS


def _mark_opencli_failed(monotonic: Callable[[], float]) -> None:
    global _OPENCLI_FAILED_AT
    _OPENCLI_FAILED_AT = monotonic()


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
            if _remaining(deadline, self._monotonic) < (
                _MIN_FALLBACK_SECONDS + _CLOSE_RESERVE_SECONDS
            ):
                raise _error("FALLBACK_DEADLINE_REACHED") from primary_error
            if _opencli_cooldown_active(self._monotonic):
                raise _error("OPENCLI_COOLDOWN_ACTIVE") from primary_error
            try:
                return self._read_with_opencli(spec, deadline=deadline)
            except EastmoneyOnDemandTransportError:
                _mark_opencli_failed(self._monotonic)
                raise

    def _probe_interop(self, *, deadline: float) -> None:
        """短预算 Windows 进程 spawn 探针：interop 不可用即快速 typed 失败。"""
        remaining = min(
            _remaining(deadline, self._monotonic),
            _INTEROP_PROBE_SECONDS,
        )
        if remaining <= 0:
            raise _error("OPENCLI_DEADLINE_REACHED")
        try:
            completed = self._command_runner(_INTEROP_PROBE_ARGV, timeout=remaining)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise _error("OPENCLI_INTEROP_UNAVAILABLE") from error
        if (
            isinstance(completed.returncode, bool)
            or not isinstance(completed.returncode, int)
            or not isinstance(completed.stdout, bytes)
            or not isinstance(completed.stderr, bytes)
            or len(completed.stdout) > _MAX_STDOUT_BYTES
            or len(completed.stderr) > _MAX_STDOUT_BYTES
        ):
            raise _error("OPENCLI_INTEROP_UNAVAILABLE")
        if completed.returncode != 0:
            raise _error("OPENCLI_INTEROP_UNAVAILABLE")
        if _remaining(deadline, self._monotonic) <= 0:
            raise _error("OPENCLI_DEADLINE_REACHED")

    def _read_with_opencli(
        self,
        spec: EastmoneyHttpRequest,
        *,
        deadline: float,
    ) -> _FallbackResponse:
        self._probe_interop(deadline=deadline)
        opencli_path, opencli_profile = self._resolve_opencli(deadline=deadline)
        self._sweep_residual_tabs(
            opencli_path=opencli_path,
            opencli_profile=opencli_profile,
            spec=spec,
            deadline=deadline,
        )
        target: str | None = None
        try:
            created = self._invoke_json(
                _opencli_browser_argv(
                    opencli_path,
                    "tab",
                    "new",
                    spec.canonical_url,
                    profile=opencli_profile,
                ),
                deadline=deadline,
                reserve_close=True,
            )
            target = _target_from_created(created)
            _validate_created(created, target=target, spec=spec)
            opened = self._invoke_json(
                _opencli_browser_argv(
                    opencli_path,
                    "open",
                    spec.canonical_url,
                    "--tab",
                    target,
                    profile=opencli_profile,
                ),
                deadline=deadline,
                reserve_close=True,
            )
            _validate_created(opened, target=target, spec=spec)
            document = self._invoke_json(
                _opencli_browser_argv(
                    opencli_path,
                    "eval",
                    _eval_document(spec.maximum_payload_bytes),
                    "--tab",
                    target,
                    profile=opencli_profile,
                ),
                deadline=deadline,
                reserve_close=True,
            )
            return _response_from_document(document, spec=spec)
        finally:
            # 用完必须关：close 用独立短预算（主流程超时/失败也要关标签），
            # close 失败不覆盖主错误但记录，避免标签累积残留。
            close_deadline = self._monotonic() + _CLOSE_RESERVE_SECONDS
            if target is not None:
                for attempt in (0, 1):
                    try:
                        closed = self._invoke_json(
                            _opencli_browser_argv(
                                opencli_path,
                                "tab",
                                "close",
                                target,
                                profile=opencli_profile,
                            ),
                            deadline=(
                                close_deadline
                                if attempt == 0
                                else self._monotonic() + _CLOSE_RESERVE_SECONDS
                            ),
                            reserve_close=False,
                        )
                        if closed != {"closed": target}:
                            raise _error("OPENCLI_CLOSE_INVALID")
                        break
                    except Exception:
                        # 首次失败重试一次（独立短预算）：spawn 瞬断或响应
                        # 丢失时立即回收；仍失败才放弃，残留交给下一请求
                        # 的入口 sweep。
                        if attempt == 1:
                            _logger.warning(
                                "opencli tab close failed (target=%s)", target
                            )
            else:
                # tab new 响应超时但标签可能已打开——按 spec endpoint host
                # 枚举并清理残留（quote 与日线端点统一覆盖）。
                try:
                    listed = self._invoke_value(
                        _opencli_browser_argv(
                            opencli_path,
                            "tab",
                            "list",
                            profile=opencli_profile,
                        ),
                        deadline=close_deadline,
                        reserve_close=False,
                    )
                    if not isinstance(listed, list):
                        raise _error("OPENCLI_OUTPUT_INVALID")
                    for tab in listed:
                        if not isinstance(tab, Mapping):
                            continue
                        tab_id = _target_from_inventory(tab)
                        url = tab.get("url")
                        if (
                            tab_id is not None
                            and isinstance(url, str)
                            and _matches_expected_host(url, spec)
                        ):
                            closed = self._invoke_json(
                                _opencli_browser_argv(
                                    opencli_path,
                                    "tab",
                                    "close",
                                    tab_id,
                                    profile=opencli_profile,
                                ),
                                deadline=close_deadline,
                                reserve_close=False,
                            )
                            if closed != {"closed": tab_id}:
                                raise _error("OPENCLI_CLOSE_INVALID")
                except Exception:
                    _logger.warning("opencli residual tab cleanup failed")

    def _sweep_residual_tabs(
        self,
        *,
        opencli_path: str,
        opencli_profile: str,
        spec: EastmoneyHttpRequest,
        deadline: float,
    ) -> None:
        """开新 tab 前回收本 session 内匹配端点 host 的残留 tab（尽力而为）。

        残留来源是上一轮 close 失败（只记 warning）遗留的标签；同 session
        的 ``tab list`` 仍可见即可回收，daemon 重启后的旧残留不可见，只能
        手动清理。预算不足或任何异常都静默跳过，绝不影响本请求。
        """
        try:
            if _remaining(deadline, self._monotonic) < _SWEEP_MIN_REMAINING_SECONDS:
                return
            listed = self._invoke_value(
                _opencli_browser_argv(
                    opencli_path,
                    "tab",
                    "list",
                    profile=opencli_profile,
                ),
                deadline=deadline,
                reserve_close=True,
            )
            if not isinstance(listed, list):
                return
            closed = 0
            for tab in listed:
                if closed >= _SWEEP_MAX_TABS:
                    break
                if _remaining(deadline, self._monotonic) < _SWEEP_MIN_REMAINING_SECONDS:
                    break
                if not isinstance(tab, Mapping):
                    continue
                tab_id = _target_from_inventory(tab)
                url = tab.get("url")
                if (
                    tab_id is None
                    or not isinstance(url, str)
                    or not _matches_expected_host(url, spec)
                ):
                    continue
                closed_payload = self._invoke_json(
                    _opencli_browser_argv(
                        opencli_path,
                        "tab",
                        "close",
                        tab_id,
                        profile=opencli_profile,
                    ),
                    deadline=deadline,
                    reserve_close=True,
                )
                if closed_payload == {"closed": tab_id}:
                    closed += 1
            if closed:
                _logger.info("opencli residual tab sweep closed %d tab(s)", closed)
        except Exception:  # noqa: BLE001 — sweep 尽力而为，绝不覆盖主流程结果
            _logger.warning("opencli residual tab sweep skipped", exc_info=True)

    def _resolve_opencli(self, *, deadline: float) -> tuple[str, str]:
        configured_profile = os.environ.get("FIN_OPENCLI_PROFILE") or None
        if configured_profile is not None and (
            _OPENCLI_PROFILE.fullmatch(configured_profile) is None
        ):
            raise _error("OPENCLI_PROFILE_INVALID")
        payload = self._invoke_json(
            (
                _POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _RESOLVE_OPENCLI if configured_profile is not None else _RESOLVE_MARKET_OPENCLI,
            ),
            deadline=deadline,
            reserve_close=True,
        )
        if set(payload) not in ({"path"}, {"path", "profile"}):
            raise _error("OPENCLI_PATH_INVALID")
        path = payload["path"]
        if not isinstance(path, str) or "\x00" in path or _OPENCLI_PATH.fullmatch(path) is None:
            raise _error("OPENCLI_PATH_INVALID")
        profile = configured_profile or payload.get("profile")
        if not isinstance(profile, str) or _OPENCLI_PROFILE.fullmatch(profile) is None:
            raise _error("OPENCLI_PROFILE_INVALID")
        return path, profile

    def _invoke_json(
        self,
        argv: Sequence[str],
        *,
        deadline: float,
        reserve_close: bool,
    ) -> dict[str, object]:
        value = self._invoke_value(
            argv,
            deadline=deadline,
            reserve_close=reserve_close,
        )
        if not isinstance(value, dict):
            raise _error("OPENCLI_OUTPUT_INVALID")
        return value

    def _invoke_value(
        self,
        argv: Sequence[str],
        *,
        deadline: float,
        reserve_close: bool,
    ) -> object:
        remaining = _remaining(deadline, self._monotonic)
        if reserve_close:
            remaining -= _CLOSE_RESERVE_SECONDS
        if remaining <= 0:
            raise _error("OPENCLI_DEADLINE_REACHED")
        try:
            completed = self._command_runner(tuple(argv), timeout=remaining)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise _error("OPENCLI_COMMAND_FAILED") from error
        if (
            isinstance(completed.returncode, bool)
            or not isinstance(completed.returncode, int)
            or not isinstance(completed.stdout, bytes)
            or not isinstance(completed.stderr, bytes)
            or len(completed.stdout) > _MAX_STDOUT_BYTES
            or len(completed.stderr) > _MAX_STDOUT_BYTES
        ):
            raise _error("OPENCLI_OUTPUT_INVALID")
        if completed.returncode != 0:
            raise _error("OPENCLI_COMMAND_FAILED")
        if _remaining(deadline, self._monotonic) <= 0:
            raise _error("OPENCLI_DEADLINE_REACHED")
        return _strict_json_value(completed.stdout)


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


def _opencli_browser_argv(
    path: str,
    *args: str,
    profile: str | None = None,
) -> tuple[str, ...]:
    prefix = (
        _POWERSHELL,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        path,
    )
    profile_argv = ("--profile", profile) if profile is not None else ()
    return (
        *prefix,
        *profile_argv,
        "browser",
        _SESSION,
        *args,
    )


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


def _eval_document(maximum_payload_bytes: int) -> str:
    return _EVAL_DOCUMENT_TEMPLATE.replace(
        "MAX_PAYLOAD_BYTES",
        str(maximum_payload_bytes),
    )


def _target_from_created(payload: Mapping[str, object]) -> str:
    target = payload.get("page")
    if not isinstance(target, str) or _TARGET_ID.fullmatch(target) is None:
        raise _error("OPENCLI_TARGET_INVALID")
    return target


def _target_from_inventory(payload: Mapping[str, object]) -> str | None:
    target = payload.get("page")
    return (
        target
        if isinstance(target, str) and _TARGET_ID.fullmatch(target) is not None
        else None
    )


def _validate_created(
    payload: Mapping[str, object],
    *,
    target: str,
    spec: EastmoneyHttpRequest,
) -> None:
    url = payload.get("url")
    if (
        set(payload) != {"page", "url"}
        or payload.get("page") != target
        or not isinstance(url, str)
        or not _url_matches(url, spec)
    ):
        raise _error("OPENCLI_TARGET_INVALID")


def _response_from_document(
    payload: Mapping[str, object],
    *,
    spec: EastmoneyHttpRequest,
) -> _FallbackResponse:
    if set(payload) != {
        "url",
        "navigationStatus",
        "contentType",
        "charset",
        "preCount",
        "byteLength",
        "base64",
    }:
        raise _error("OPENCLI_DOCUMENT_INVALID")
    byte_length = payload.get("byteLength")
    if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 0:
        raise _error("OPENCLI_DOCUMENT_INVALID")
    if byte_length > spec.maximum_payload_bytes:
        raise _error("OPENCLI_PAYLOAD_TOO_LARGE")
    encoded = payload.get("base64")
    url = payload.get("url")
    if (
        not isinstance(url, str)
        or not _url_matches(url, spec)
        or payload.get("navigationStatus") != 200
        or payload.get("contentType") != spec.response_content_type
        or payload.get("charset") != spec.response_charset
        or payload.get("preCount") != 1
        or not isinstance(encoded, str)
    ):
        raise _error("OPENCLI_DOCUMENT_INVALID")
    try:
        content = base64.b64decode(encoded, validate=True)
        content.decode("utf-8", errors="strict")
    except (UnicodeError, ValueError) as error:
        raise _error("OPENCLI_DOCUMENT_INVALID") from error
    if len(content) != byte_length:
        raise _error("OPENCLI_DOCUMENT_INVALID")
    return _FallbackResponse(status_code=200, content=content)


def _matches_expected_host(value: str, spec: EastmoneyHttpRequest) -> bool:
    """宽松 host 前缀匹配：仅用于残留 tab 清理，不做完整 URL 资格判定。"""
    try:
        expected = urlsplit(spec.endpoint)
    except (TypeError, ValueError):
        return False
    return value.startswith(f"{expected.scheme}://{expected.hostname}/")


def _url_matches(value: str, spec: EastmoneyHttpRequest) -> bool:
    try:
        parsed = urlsplit(value)
        expected = urlsplit(spec.endpoint)
        query = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port is None
        and parsed.hostname == expected.hostname
        and parsed.path == expected.path
        and parsed.fragment == ""
        and sorted(query) == sorted(spec.query)
    )


def _strict_json_value(payload: bytes) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeError, ValueError) as error:
        raise _error("OPENCLI_OUTPUT_INVALID") from error


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
) -> subprocess.CompletedProcess[bytes]:
    completed = run_bounded_command(
        tuple(argv),
        cwd=Path("/"),
        env=_opencli_environment(),
        timeout=timeout,
        max_output_bytes=_MAX_STDOUT_BYTES,
    )
    return subprocess.CompletedProcess(
        args=completed.args,
        returncode=completed.returncode,
        stdout=completed.stdout.encode("utf-8"),
        stderr=completed.stderr.encode("utf-8"),
    )


def _opencli_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    wsl_interop = source.get("WSL_INTEROP")
    if wsl_interop:
        environment["WSL_INTEROP"] = wsl_interop
    elif wsl_interop is None:
        trusted_alias = _trusted_wsl_interop_alias()
        if trusted_alias is not None:
            environment["WSL_INTEROP"] = trusted_alias
    return environment


def _trusted_wsl_interop_alias(
    *,
    lstat_path: Callable[[Path], os.stat_result] | None = None,
    resolve_path: Callable[[Path], Path] | None = None,
    stat_path: Callable[[Path], os.stat_result] | None = None,
) -> str | None:
    """Return the fixed WSL socket alias only when root controls its full path."""
    read_link_metadata = Path.lstat if lstat_path is None else lstat_path
    read_target_metadata = Path.stat if stat_path is None else stat_path
    try:
        for parent in (_WSL_RUN_ROOT, _WSL_INTEROP_ROOT):
            metadata = read_link_metadata(parent)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_mode & 0o022
            ):
                return None
        alias_metadata = read_link_metadata(_WSL_INTEROP_ALIAS)
        if not stat.S_ISLNK(alias_metadata.st_mode) or alias_metadata.st_uid != 0:
            return None
        resolved = (
            _WSL_INTEROP_ALIAS.resolve(strict=True)
            if resolve_path is None
            else resolve_path(_WSL_INTEROP_ALIAS)
        )
        if resolved.parent != _WSL_INTEROP_ROOT:
            return None
        target_metadata = read_target_metadata(resolved)
        if not stat.S_ISSOCK(target_metadata.st_mode) or target_metadata.st_uid != 0:
            return None
    except (OSError, RuntimeError):
        return None
    return str(_WSL_INTEROP_ALIAS)


__all__ = [
    "EastmoneyOnDemandTransportError",
]
