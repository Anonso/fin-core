"""Codex CLI Agent Runtime Adapter — Codex as a FIN agent runtime.

Read-only Codex CLI adapter that implements AgentRuntimePort.  It builds
an argv list (no shell), invokes ``codex exec --json --ephemeral`` inside
a read-only sandbox with a bounded timeout, parses the JSONL output for the
final ``agent_message``, and returns a sanitized AgentRunResult.

Fail-closed: timeout, nonzero exit, missing final output and malformed
product JSON all produce status="error" with bounded data-gap codes.
Stderr, raw transcripts, session IDs and credentials never leak into the
public payload.

Design: docs/architecture/fin-domain-kernel-agent-runtime.md
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json as _json
import logging
import math
import os
import re
import secrets
import socket
import ssl
import stat
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from fin_analyse.common.owner_only_snapshot import (
    OwnerOnlyJsonSnapshotFile,
    OwnerOnlySnapshotReason,
)
from fin_analyse.common.stall_watchdog import (
    STALL_DETECTION_SECONDS,
)
from fin_analyse.common.stall_watchdog import (
    StallError as CodexStallError,
)
from fin_analyse.common.stall_watchdog import (
    run_with_stall_watchdog as _run_with_stall_watchdog,
)
from fin_analyse.guo_teacher_research.agent_runtime import (
    AgentRunRequest,
    AgentRunResult,
)
from fin_analyse.guo_teacher_research.codex_route_config import is_codex_route_id
from fin_analyse.guo_teacher_research.codex_session_store import (
    CodexSessionArtifactStore,
)
from fin_analyse.guo_teacher_research.local_capability_transport import (
    LocalCapabilityTransportError,
    LocalMcpCapabilityRunner,
)
from fin_analyse.guo_teacher_research.runtime_diagnostics import (
    CodexRuntimeDiagnosticSink,
    CodexRuntimeFailureEvent,
    CodexRuntimeInvocationEvent,
    CodexRuntimeInvocationSink,
    classify_codex_failover_failure,
    new_error_id,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

_VALID_REASONING_EFFORTS: frozenset[str] = frozenset(
    {"low", "medium", "high", "xhigh", "max", "ultra"}
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_AUTH_MODE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,48}$")
_AUTH_IDENTITY_SCHEMA = "fin.codex-auth-identity/v1"
_MAX_AUTH_BYTES = 256 * 1024
_MAX_AUTH_IDENTITY_BYTES = 4 * 1024
# Longest direct turn is 3300s; Codex refreshes about five minutes before exp.
_AUTH_REFRESH_SERIALIZATION_MARGIN_SECONDS = 60 * 60
_AUTH_REFRESH_LOCK_FILENAME = ".fin-auth-refresh.lock"
_OUTPUT_SCHEMA_FILENAME = "fin-output-schema.json"
_OUTPUT_SCHEMA_CONTRACTS = frozenset(
    {
        ("guo_explanation_product", "v1"),
        ("consultation_product", "v1"),
    }
)
_CODEX_RESPONSE_FORMAT_REMOVED_KEYWORDS = frozenset(
    {
        "$schema",
        "allOf",
        "contains",
        "format",
        "maxContains",
        "maxLength",
        "minContains",
        "minLength",
        "uniqueItems",
    }
)
_CODEX_RESPONSE_FORMAT_SUPPORTED_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maximum",
        "maxItems",
        "minimum",
        "minItems",
        "multipleOf",
        "pattern",
        "properties",
        "required",
        "type",
    }
)
_NONZERO_EXIT_PATTERN = re.compile(r"^codex_nonzero_exit:[+-]?\d{1,3}$")
_ADVISORY_PROMPT_ASSET = Path(__file__).with_name(
    "consultation-advisory-prompt.v1.json"
)
_FAILOVER_ELIGIBLE_GAPS = frozenset(
    {
        "codex_timeout",
        "codex_stall",
        "codex_invocation_error",
        "codex_probe_failed",
    }
)
# Attention-budget sentinel: total prompt beyond this raises a warning
# (never a hard gate). Pack budget is 40K chars, so a 50K total only fires on
# genuine fixed-prompt bloat; the fixed contract block has its own 6600 test cap.
_PROMPT_ATTENTION_WARN_CHARS = 50000


def _load_advisory_prompt_asset() -> dict[str, Any]:
    """Load the versioned advisory prompt contract asset (read-only)."""
    return _json.loads(_ADVISORY_PROMPT_ASSET.read_text(encoding="utf-8"))
_DEFAULT_MINIMUM_PRIMARY_SECONDS = 30.0
# 最后一次 capability 活动后超过该窗口仍无进展（且无拒绝）即可 failover。
# 覆盖"先读数据、后卡在 LLM 轮"的最常见 hang 形态（钨 62 分钟复盘）。
_CAPABILITY_ACTIVITY_STALL_SECONDS = 300.0
# 子进程无输出看门狗：超过该窗口无任何 stdout/stderr 事件即终止 + failover。
# 用户决策 2026-08-01：不能被动等满 cap，5 分钟无活动就应主动处理。
# 实现在 fin_analyse/common/stall_watchdog.py（direct 与 capability 路径共用）。

# ── 上游探活(pre-child phase,用户拍板 2026-08-21)──────────────────────
# 探活只做 availability 判定,绝不 fail-closed;三态结果仅
# confirmed_unavailable 触发 failover+cooldown(N3/N6)。
_PROBE_CONNECT_TIMEOUT_SECONDS = 10.0
_PROBE_TOTAL_TIMEOUT_SECONDS = 60.0
_PROBE_BODY_MAX_BYTES = 64 * 1024
# 探活后 child 预算必须为 fallback 保留的最小窗口(N5 用户拍板:
# child = cap - probe_elapsed - reserve;fallback 最小 30s 见 runtime_budget)。
_PROBE_FALLBACK_RESERVE_SECONDS = 30.0
# 早期无产出检测窗口(fresh-only startup marker,N7 best-effort)。
_EARLY_PROGRESS_SECONDS = 90.0


# N4 provider manifest 单一事实源:{auth_mode, origin, token_source}。
# FIN direct 的临时 auth home 只可能是 chatgpt 模式(经 PinnedCodexRuntimeIdentity
# 复制);apiKey 模式属于 proxy route-owned home,不经 direct 探活——任何不匹配
# 配对的凭证来源都不得发起探活请求。
class _FenceClosedError(Exception):
    """Popen 前 fence 已关闭或预算耗尽:不启动 child,返回 fenced 结果。"""


_PROBE_PROVIDER_MANIFEST = frozenset(
    {
        ("chatgpt", "https://api.openai.com"),
    }
)

# The external runtime must not inherit FIN database, bridge, browser/account,
# or unrelated provider credentials from its parent process.  Keep this list
# deliberately small: Codex/proxy authentication belongs to its isolated HOME
# or CODEX_HOME, not ambient FIN secrets.
_SAFE_RUNTIME_ENV_VARS: frozenset[str] = frozenset(
    {
        "CODEX_HOME",
        "COLORTERM",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
    }
)
_ROUTE_RUNTIME_ENV_VARS = frozenset(
    {
        "FIN_CODEX_PROVIDER_API_KEY",
        "FIN_CODEX_ROUTE_ATTESTED",
        "FIN_CODEX_ROUTE_LAUNCHER_ID",
        "FIN_CODEX_ROUTE_LAUNCHER_PATH",
        "FIN_CODEX_ROUTE_ID",
        "FIN_CODEX_ROUTE_HOME",
        "FIN_CODEX_ROUTE_BASE_URL",
        "FIN_CODEX_ROUTE_MODEL",
        "FIN_CODEX_ROUTE_CONFIG_SHA256",
        "FIN_CODEX_ROUTE_AUTH_SHA256",
    }
)


class _OutputSchemaContractError(RuntimeError):
    """The FIN-owned declarative schema cannot be rendered as JSON."""


class _OutputSchemaStagingError(RuntimeError):
    """The private schema file cannot be safely staged or removed."""


def _probe_credentials(auth_home: str) -> tuple[str | None, str | None]:
    """从 auth snapshot 取 {token, origin} 配对(N4: 配对表内才允许探活)。

    chatgpt 模式 → access_token + api.openai.com;apiKey 模式 → OPENAI_API_KEY
    + api.openai.com(与 child 同故障域)。不匹配配对表 → (None, None)。
    """

    try:
        auth = _json.loads((Path(auth_home) / "auth.json").read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(auth, dict):
        return None, None
    if auth.get("auth_mode") == "chatgpt":
        tokens = auth.get("tokens")
        if isinstance(tokens, dict) and isinstance(tokens.get("access_token"), str):
            token = tokens["access_token"]
            if ("chatgpt", "https://api.openai.com") in _PROBE_PROVIDER_MANIFEST:
                return token, "https://api.openai.com"
        return None, None
    # apiKey 模式不在 direct 探活配对表内:不探活(inconclusive),凭证不发出。
    return None, None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """3xx 一律不跟随(冻结契约:重定向计 inconclusive)。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _probe_upstream(
    auth_home: str,
    *,
    model: str,
    timeout_seconds: float = _PROBE_TOTAL_TIMEOUT_SECONDS,
) -> tuple[str, str | None, int | None]:
    """上游可达性探活:带 token 最小 chat 请求(用户拍板 2026-08-21)。

    三态结果:
    - confirmed_unavailable:连接失败/DNS/TLS/总超时/429/5xx(同故障域可信)
    - reachable:2xx
    - inconclusive:401/403/404/408/3xx/配置不匹配/其他(不阻断 primary)

    冻结契约(N4/F4/audit r1-1/r2-1):manifest 配对校验、显式无代理 opener、
    禁重定向、monotonic absolute deadline 60s(每次 I/O 用剩余超时)、响应体
    有界读取(64KB)、请求形态 /v1/chat/completions + messages/max_tokens。
    只把明确的网络/TLS/超时异常归类 confirmed_unavailable,本地编程错误
    不误判为上游不可用(urllib 标准调用,无自定义构造参数)。
    """

    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(float(timeout_seconds))
        or not 0.1 <= float(timeout_seconds) <= _PROBE_TOTAL_TIMEOUT_SECONDS
    ):
        raise ValueError("codex probe timeout is invalid")
    token, origin = _probe_credentials(auth_home)
    if token is None or origin is None:
        return "inconclusive", None, None
    deadline = time.monotonic() + float(timeout_seconds)
    try:
        url = f"{origin}/v1/chat/completions"
        body = _json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        # 连接层 10s 硬上限:socket 预连接验证 TCP/TLS 可达性(audit r3-1);
        # 之后 urllib 请求用剩余 deadline,单次有界读取 ≤ 剩余,总 ≤ 60s。
        sock = socket.create_connection(
            (origin.removeprefix("https://"), 443),
            timeout=min(_PROBE_CONNECT_TIMEOUT_SECONDS, float(timeout_seconds)),
        )
        sock.close()
        remaining = max(0.1, deadline - time.monotonic())
        with opener.open(request, timeout=remaining) as response:
            status = response.status
            remaining = max(0.1, deadline - time.monotonic())
            data = response.read(_PROBE_BODY_MAX_BYTES + 1)
        if len(data) > _PROBE_BODY_MAX_BYTES:
            return "inconclusive", origin, status
        if 200 <= status < 300:
            return "reachable", origin, status
        if status in (401, 403, 404, 408):
            return "inconclusive", origin, status
        if status == 429 or 500 <= status < 600:
            return "confirmed_unavailable", origin, status
        return "inconclusive", origin, status
    except urllib.error.HTTPError as error:
        status = error.code
        if status in (401, 403, 404, 408):
            return "inconclusive", origin, status
        if status == 429 or 500 <= status < 600:
            return "confirmed_unavailable", origin, status
        return "inconclusive", origin, status
    except (TimeoutError, urllib.error.URLError, ssl.SSLError, ConnectionError) as error:
        # 仅明确网络/TLS/超时类异常 → confirmed_unavailable(audit r3-2);
        # 本地编程/配置错误落入下方 inconclusive,不误触发 failover。
        if (
            isinstance(error, urllib.error.URLError)
            and error.reason is not None
            and not isinstance(error.reason, (OSError, TimeoutError, ssl.SSLError))
        ):
            return "inconclusive", origin, None
        return "confirmed_unavailable", origin, None
    except Exception:
        return "inconclusive", origin, None


def _fenced_runtime_result(request: AgentRunRequest, *, model: str) -> AgentRunResult:
    return AgentRunResult(
        status="error",
        payload={},
        data_gaps=["codex_timeout"],
        capability_trace=(
            request.capability_bridge.trace if request.capability_bridge is not None else []
        ),
        provenance={"backend": "codex", "model": model or "unknown"},
    )


_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _valid_uuid_text(value: str) -> bool:
    """Canonical lowercase UUID shape used by Codex continuations."""
    return _UUID_PATTERN.fullmatch(value) is not None


def _extract_thread_id(stdout: str) -> str | None:
    """从 Codex JSONL stdout 提取会话标识（UUIDv7）。

    ``thread.started.thread_id``（direct 路径）；proxy 路径的 codex 0.146
    exec 输出用 ``session_meta.session_id`` 承载同一标识——两者都接受。
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = _json.loads(stripped)
        except _json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "thread.started":
            value = record.get("thread_id")
            if isinstance(value, str) and _valid_uuid_text(value):
                return value
        if isinstance(record, dict) and record.get("type") == "session_meta":
            payload = record.get("payload")
            value = payload.get("session_id") if isinstance(payload, dict) else None
            if isinstance(value, str) and _valid_uuid_text(value):
                return value
    return None


def _extract_consistent_thread_id(stdout: str) -> str | None:
    """提取 stdout 中唯一的 provider session id；冲突（thread.started 与
    session_meta 不同、或出现多个不同 id）返回 None——不一致的回报不能
    通过 exact identity gate（A3-R1-F1）。
    """
    seen: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = _json.loads(stripped)
        except _json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        value: object = None
        if record.get("type") == "thread.started":
            value = record.get("thread_id")
        elif record.get("type") == "session_meta":
            payload = record.get("payload")
            value = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not _valid_uuid_text(value):
            continue
        if seen is not None and seen != value:
            return None  # 冲突事件：thread.started=A 后 session_meta=B
        seen = value
    return seen


def _resume_activity_uncertain(request: AgentRunRequest) -> bool:
    """resume 是否已越过 capability 活动边界（handoff §6.4）。

    活动后失败不确定能否安全重跑 → fail closed，不静默重新执行；只有活动前
    失败才允许丢弃 handle 后 fresh 重来。
    """
    bridge = request.capability_bridge
    if bridge is None:
        return False
    try:
        return bool(bridge.activity_started or bridge.trace)
    except Exception:
        return True


# Item types that are not tool calls when max_tool_calls=0.  An ``error`` item
# may precede a recovered terminal answer; terminal and product validation below
# still fail closed when no usable answer follows. Unknown future tools remain denied.
_NON_TOOL_ITEM_TYPES: frozenset[str] = frozenset(
    {
        "agent_message",
        "error",
        "reasoning",
    }
)

_DISABLED_RUNTIME_FEATURES: tuple[str, ...] = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "workspace_dependencies",
)


def _is_consultation_request(request: AgentRunRequest) -> bool:
    return any(
        item.get("contract_id") == "consultation_product" and item.get("version") == "v1"
        for item in request.product_contracts
        if isinstance(item, dict)
    )


def _web_search_mode(request: AgentRunRequest) -> str:
    if request.budget.get("max_tool_calls") == 0:
        return "disabled"
    return "live" if _is_consultation_request(request) else "disabled"


def _valid_timeout_seconds(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0


def _sanitized_runtime_environment() -> dict[str, str]:
    """Project only the non-secret process context required by Codex."""

    return {name: value for name, value in os.environ.items() if name in _SAFE_RUNTIME_ENV_VARS}


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_ROUTE_LAUNCHER_IDS = frozenset({"codex-proxy-a", "codex-proxy-b"})


def _route_launcher_path(route_id: str) -> Path:
    if route_id not in _ROUTE_LAUNCHER_IDS:
        raise CodexRuntimeIdentityError("codex_route_launcher_invalid")
    return Path.home() / ".local" / "bin" / route_id


def _tracked_route_launcher_path(path: Path) -> Path:
    source_name = {
        "codex-proxy-a": "codex_proxy_a.sh",
        "codex-proxy-b": "codex_proxy_b.sh",
    }.get(path.name)
    if source_name is None:
        raise CodexRuntimeIdentityError("codex_route_launcher_invalid")
    return _PROJECT_ROOT / "scripts" / source_name


def _is_route_launcher(path: str | Path) -> bool:
    candidate = Path(path)
    return candidate in {_route_launcher_path(route_id) for route_id in _ROUTE_LAUNCHER_IDS}


def _validate_route_launcher_bound(wrapper_path: str | Path | None = None) -> None:
    """Validate one installed A/B launcher against its tracked source.

    The watchdog path uses :func:`_open_bound_route_launcher` for fd binding;
    this function is the pathname-level preflight used by composition.
    """

    if wrapper_path is None:
        raise CodexRuntimeIdentityError("codex_route_launcher_invalid")
    wrapper = Path(wrapper_path)
    if not _is_route_launcher(wrapper):
        raise CodexRuntimeIdentityError("codex_route_launcher_invalid")
    tracked = _tracked_route_launcher_path(wrapper)
    for path in (wrapper, tracked):
        try:
            metadata = path.lstat()
        except OSError:
            raise CodexRuntimeIdentityError("codex_route_launcher_unavailable") from None
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o002
            or not (stat.S_IMODE(metadata.st_mode) & 0o100)
        ):
            raise CodexRuntimeIdentityError("codex_route_launcher_unsafe")
    if _sha256_file(wrapper) != _sha256_file(tracked):
        raise CodexRuntimeIdentityError("codex_route_launcher_drifted")


def _open_bound_route_launcher(wrapper: Path) -> int:
    """Open the wrapper and bind its validated bytes to the returned fd.

    The caller executes ``/proc/self/fd/N`` (same pattern as
    :meth:`PinnedCodexRuntimeIdentity.spawn_binding`), so a pathname swap
    after validation cannot change what is executed.  Identity checks run
    against the opened fd (fstat), and the content SHA is read from the same
    fd; the offset is reset so the child reads the script from the start.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(wrapper, flags)
    except OSError:
        raise CodexRuntimeIdentityError("codex_route_launcher_unavailable") from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise CodexRuntimeIdentityError("codex_route_launcher_unsafe")
        try:
            tracked = _tracked_route_launcher_path(wrapper)
        except CodexRuntimeIdentityError:
            raise
        try:
            tracked_metadata = tracked.lstat()
        except OSError:
            raise CodexRuntimeIdentityError("codex_route_launcher_unavailable") from None
        if (
            tracked.is_symlink()
            or not stat.S_ISREG(tracked_metadata.st_mode)
            or tracked_metadata.st_uid != os.geteuid()
            or tracked_metadata.st_nlink != 1
            # release 目录本身 0700（owner-only 隔离），tracked 的 group 写位
            # 无实际风险——git checkout 在 umask 0002 下把 100755 落成 0775。
            # 只拒"其他用户可写"（0o002），避免 release 构建环境误拒。
            or stat.S_IMODE(tracked_metadata.st_mode) & 0o002
        ):
            raise CodexRuntimeIdentityError("codex_route_launcher_unsafe")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if digest.hexdigest() != _sha256_file(tracked):
            raise CodexRuntimeIdentityError("codex_route_launcher_drifted")
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd


class CodexRuntimeIdentityError(RuntimeError):
    """A pinned consultation runtime identity is absent, mutable or drifted."""


class _AuthRefreshFinalizationError(RuntimeError):
    """A child auth rotation could not be validated or durably committed."""


@dataclass(frozen=True, slots=True)
class PinnedCodexRuntimeIdentity:
    """Owner-only executable and authentication policy for direct Codex.

    Validation happens both at construction and immediately before every child
    process invocation.  The adapter therefore never resolves a consultation
    runtime through ambient ``PATH`` or an ambient ``CODEX_HOME``.
    The policy pins the authentication mode, while the user may rotate the
    active account within that mode through the dedicated login home.
    """

    executable: Path
    expected_sha256: str
    codex_home: Path
    expected_auth_identity_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.executable, Path)
            or not isinstance(self.codex_home, Path)
            or not isinstance(self.expected_sha256, str)
            or _SHA256_PATTERN.fullmatch(self.expected_sha256) is None
            or not isinstance(self.expected_auth_identity_sha256, str)
            or _SHA256_PATTERN.fullmatch(self.expected_auth_identity_sha256) is None
        ):
            raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
        self.validate()

    @property
    def auth_path(self) -> Path:
        return self.codex_home / "auth.json"

    @property
    def auth_identity_path(self) -> Path:
        return self.codex_home / "fin-runtime-identity.json"

    def validate(self) -> None:
        descriptor, _auth_payload = self._open_verified_components()
        os.close(descriptor)

    def open_verified_executable(self) -> int:
        """Return one verified executable fd for race-free child launch."""

        descriptor, _auth_payload = self._open_verified_components()
        return descriptor

    def _open_verified_components(self) -> tuple[int, bytes]:
        _require_owner_only_real_directory(self.codex_home)
        auth_payload = _validate_auth_identity(
            auth_path=self.auth_path,
            identity_path=self.auth_identity_path,
            expected_identity_sha256=self.expected_auth_identity_sha256,
        )
        _require_owner_only_real_directory(self.executable.parent)
        descriptor = _open_verified_owner_only_executable(
            self.executable,
            expected_sha256=self.expected_sha256,
        )
        return descriptor, auth_payload

    @contextmanager
    def spawn_binding(
        self,
    ) -> Iterator[tuple[str, tuple[int, ...], str, Callable[[], None]]]:
        """Bind verified snapshots and return an idempotent auth finalizer.

        The caller settles the finalizer after the child exits but before it
        records that child's terminal event; context exit is a safety fallback.
        """

        descriptor, auth_payload = self._open_verified_components()
        refresh_lock = -1
        try:
            if _auth_refresh_may_write(auth_payload):
                os.close(descriptor)
                descriptor = -1
                refresh_lock = _lock_auth_refresh(self.codex_home)
                # A prior waiter may have refreshed while this invocation was
                # blocked. Rebind both executable and auth after taking ownership.
                descriptor, auth_payload = self._open_verified_components()
            try:
                source_before = _file_fingerprint(self.auth_path.lstat())
            except OSError:
                raise CodexRuntimeIdentityError("codex_runtime_identity_invalid") from None
            original_binding = _decode_auth_binding(auth_payload)
            original_document = _decode_auth_document(auth_payload)
            with TemporaryDirectory(prefix="fin-codex-auth-") as temporary:
                auth_home = Path(temporary)
                auth_home.chmod(0o700)
                snapshot_path = auth_home / "auth.json"
                _write_owner_only_auth_snapshot(snapshot_path, auth_payload)
                proc_path = f"/proc/self/fd/{descriptor}"
                if not Path("/proc/self/fd").is_dir():
                    raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
                finalized = False

                def finalize_auth() -> None:
                    nonlocal finalized
                    if finalized:
                        return
                    finalized = True
                    _persist_same_account_auth_refresh(
                        source_path=self.auth_path,
                        original_payload=auth_payload,
                        original_fingerprint=source_before,
                        original_binding=original_binding,
                        original_document=original_document,
                        snapshot_path=snapshot_path,
                    )

                try:
                    yield proc_path, (descriptor,), str(auth_home), finalize_auth
                finally:
                    finalize_auth()
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            if refresh_lock >= 0:
                with suppress(OSError):
                    fcntl.flock(refresh_lock, fcntl.LOCK_UN)
                with suppress(OSError):
                    os.close(refresh_lock)


def _require_normalized_absolute_real_path(path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid") from None
    if not path.is_absolute() or path != resolved:
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")


def _require_owner_only_real_directory(path: Path) -> None:
    _require_normalized_absolute_real_path(path)
    try:
        metadata = path.lstat()
    except OSError:
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")


def _open_verified_owner_only_executable(
    path: Path,
    *,
    expected_sha256: str,
) -> int:
    _require_normalized_absolute_real_path(path)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o700
        ):
            raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
        if not secrets.compare_digest(digest.hexdigest(), expected_sha256):
            raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        opened = descriptor
        descriptor = -1
        return opened
    except CodexRuntimeIdentityError:
        raise
    except OSError:
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_owner_only_real_file(
    path: Path,
    *,
    expected_mode: int,
    max_bytes: int,
) -> bytes:
    _require_normalized_absolute_real_path(path)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_size > max_bytes
        ):
            raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
        payload = os.read(descriptor, max_bytes + 1)
        after = os.fstat(descriptor)
        if len(payload) > max_bytes or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
        return payload
    except CodexRuntimeIdentityError:
        raise
    except OSError:
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _file_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _decode_auth_binding(payload: bytes) -> tuple[str, str]:
    try:
        auth = _json.loads(payload)
    except (UnicodeDecodeError, _json.JSONDecodeError):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid") from None
    if not isinstance(auth, dict):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
    auth_mode = auth.get("auth_mode")
    tokens = auth.get("tokens")
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    if (
        not isinstance(auth_mode, str)
        or _AUTH_MODE_PATTERN.fullmatch(auth_mode) is None
        or not isinstance(account_id, str)
        or not 1 <= len(account_id) <= 512
    ):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
    return auth_mode, account_id


def _decode_auth_document(payload: bytes) -> dict[str, Any]:
    try:
        document = _json.loads(payload)
    except (UnicodeDecodeError, _json.JSONDecodeError):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid") from None
    if not isinstance(document, dict):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
    _decode_auth_binding(payload)
    return document


def _auth_refresh_may_write(payload: bytes) -> bool:
    """Conservatively serialize only invocations that may rotate auth."""

    try:
        document = _decode_auth_document(payload)
        tokens = document["tokens"]
        access_token = tokens["access_token"]
        if not isinstance(access_token, str):
            return True
        parts = access_token.split(".")
        if len(parts) != 3:
            return True
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(encoded))
        expires_at = claims.get("exp") if isinstance(claims, dict) else None
        if (
            not isinstance(expires_at, (int, float))
            or isinstance(expires_at, bool)
            or not math.isfinite(expires_at)
        ):
            return True
        return float(expires_at) <= time.time() + _AUTH_REFRESH_SERIALIZATION_MARGIN_SECONDS
    except (KeyError, TypeError, ValueError, binascii.Error, CodexRuntimeIdentityError):
        return True


def _lock_auth_refresh(codex_home: Path) -> int:
    descriptor = -1
    try:
        descriptor = os.open(
            codex_home / _AUTH_REFRESH_LOCK_FILENAME,
            os.O_RDWR
            | os.O_CREAT
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        result = descriptor
        descriptor = -1
        return result
    except CodexRuntimeIdentityError:
        raise
    except OSError:
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _persist_same_account_auth_refresh(
    *,
    source_path: Path,
    original_payload: bytes,
    original_fingerprint: tuple[int, int, int, int, int],
    original_binding: tuple[str, str],
    original_document: dict[str, Any],
    snapshot_path: Path,
) -> None:
    try:
        candidate = _read_owner_only_real_file(
            snapshot_path,
            expected_mode=0o600,
            max_bytes=_MAX_AUTH_BYTES,
        )
    except CodexRuntimeIdentityError:
        raise _AuthRefreshFinalizationError from None
    if secrets.compare_digest(candidate, original_payload):
        return
    try:
        candidate_document = _decode_auth_document(candidate)
        valid_delta = _decode_auth_binding(candidate) == original_binding and _valid_refresh_delta(
            original_document,
            candidate_document,
        )
    except CodexRuntimeIdentityError:
        valid_delta = False
    if not valid_delta:
        raise _AuthRefreshFinalizationError
    try:
        if _file_fingerprint(source_path.lstat()) != original_fingerprint:
            return
    except OSError:
        raise _AuthRefreshFinalizationError from None

    publication = OwnerOnlyJsonSnapshotFile(
        target=source_path,
        forbidden_root=_PROJECT_ROOT,
        max_bytes=_MAX_AUTH_BYTES,
    ).publish(
        source=snapshot_path,
        candidate_revision=_auth_revision(candidate),
        expected_current_revision=_auth_revision(original_payload),
        apply=True,
        decode_candidate=_decode_auth_binding_for_snapshot,
        decode_current=_decode_auth_binding_for_snapshot,
        compatible=lambda candidate_binding, current_binding: (
            candidate_binding == original_binding and current_binding == original_binding
        ),
    )
    if publication.status in {"PUBLISHED", "EXACT_REPLAY"}:
        return
    if publication.reason in {
        OwnerOnlySnapshotReason.CAS_MISMATCH,
        OwnerOnlySnapshotReason.SOURCE_CHANGED,
        OwnerOnlySnapshotReason.INCOMPATIBLE,
    }:
        return
    raise _AuthRefreshFinalizationError


def _auth_revision(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _decode_auth_binding_for_snapshot(payload: bytes) -> tuple[str, str]:
    try:
        return _decode_auth_binding(payload)
    except CodexRuntimeIdentityError:
        raise ValueError("invalid auth binding") from None


def _valid_refresh_delta(original: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if set(candidate) != set(original):
        return False
    if any(
        candidate[key] != value
        for key, value in original.items()
        if key not in {"tokens", "last_refresh"}
    ):
        return False
    original_tokens = original.get("tokens")
    candidate_tokens = candidate.get("tokens")
    if not isinstance(original_tokens, dict) or not isinstance(candidate_tokens, dict):
        return False
    if set(candidate_tokens) != set(original_tokens):
        return False
    mutable_token_keys = {"id_token", "access_token", "refresh_token"}
    if any(
        candidate_tokens[key] != value
        for key, value in original_tokens.items()
        if key not in mutable_token_keys
    ):
        return False
    if any(
        not isinstance(candidate_tokens.get(key), str) or not candidate_tokens[key]
        for key in mutable_token_keys
    ):
        return False
    if not any(
        candidate_tokens[key] != original_tokens.get(key) for key in mutable_token_keys
    ):
        return False
    original_refresh = _parse_auth_timestamp(original.get("last_refresh"))
    candidate_refresh = _parse_auth_timestamp(candidate.get("last_refresh"))
    return (
        original_refresh is not None
        and candidate_refresh is not None
        and candidate_refresh >= original_refresh
    )


def _parse_auth_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _validate_auth_identity(
    *,
    auth_path: Path,
    identity_path: Path,
    expected_identity_sha256: str,
) -> bytes:
    auth_payload = _read_owner_only_real_file(
        auth_path,
        expected_mode=0o600,
        max_bytes=_MAX_AUTH_BYTES,
    )
    identity_payload = _read_owner_only_real_file(
        identity_path,
        expected_mode=0o600,
        max_bytes=_MAX_AUTH_IDENTITY_BYTES,
    )
    if not secrets.compare_digest(
        hashlib.sha256(identity_payload).hexdigest(),
        expected_identity_sha256,
    ):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
    try:
        auth = _json.loads(auth_payload)
        identity = _json.loads(identity_payload)
    except (UnicodeDecodeError, _json.JSONDecodeError):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid") from None
    if (
        not isinstance(auth, dict)
        or not isinstance(identity, dict)
        or set(identity)
        != {
            "schema_version",
            "auth_mode",
            "account_id_sha256",
        }
        or identity.get("schema_version") != _AUTH_IDENTITY_SCHEMA
    ):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
    auth_mode = auth.get("auth_mode")
    identity_auth_mode = identity.get("auth_mode")
    tokens = auth.get("tokens")
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    manifest_account_hash = identity.get("account_id_sha256")
    if (
        not isinstance(auth_mode, str)
        or _AUTH_MODE_PATTERN.fullmatch(auth_mode) is None
        or identity_auth_mode != auth_mode
        or not isinstance(account_id, str)
        or not 1 <= len(account_id) <= 512
        or not isinstance(manifest_account_hash, str)
        or _SHA256_PATTERN.fullmatch(manifest_account_hash) is None
    ):
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
    # ``account_id_sha256`` remains part of the v1, config-pinned manifest so
    # an unapproved manifest replacement is still rejected. The account it
    # names is deliberately not compared here: FIN consultation follows the
    # user's active account after a dedicated ``codex login`` rotation, while
    # preserving the owner-only home and authentication-mode boundary.
    return auth_payload


def _write_owner_only_auth_snapshot(path: Path, payload: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short auth snapshot write")
            view = view[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
    except CodexRuntimeIdentityError:
        raise
    except OSError:
        raise CodexRuntimeIdentityError("codex_runtime_identity_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def codex_result_allows_failover(
    request: AgentRunRequest,
    result: AgentRunResult,
    *,
    elapsed_seconds: float,
    failover_failure_classes: frozenset[str],
    clock: Callable[[], datetime],
    continuation_is_bound: bool,
) -> bool:
    """Canonical availability-only failover classifier for Codex routes."""
    if result.status != "error":
        return False
    if continuation_is_bound:
        return False
    if result.capability_trace and elapsed_seconds < _CAPABILITY_ACTIVITY_STALL_SECONDS:
        return False
    if request.execution_fence is not None and not request.execution_fence.is_open(at=clock()):
        return False
    bridge = request.capability_bridge
    if bridge is not None:
        try:
            if bridge.rejected:
                return False
            if (
                bridge.activity_started or bridge.trace
            ) and elapsed_seconds < _CAPABILITY_ACTIVITY_STALL_SECONDS:
                return False
        except Exception:
            return False
    gaps = result.data_gaps
    if bool(gaps) and all(gap in _FAILOVER_ELIGIBLE_GAPS for gap in gaps):
        return True
    failure_class = result.provenance.get("runtime_failure_class")
    return (
        len(gaps) == 1
        and isinstance(gaps[0], str)
        and _NONZERO_EXIT_PATTERN.fullmatch(gaps[0]) is not None
        and failure_class in failover_failure_classes
    )


def _declarative_contract_schema(
    contracts: list[dict[str, Any]],
) -> tuple[str, str, frozenset[str], frozenset[str], dict[str, Any] | None] | None:
    """Return one declarative schema, leaving ref-only legacy contracts alone."""

    if not contracts or not any("public_fields" in item for item in contracts):
        return None
    if len(contracts) != 1:
        raise ValueError("declarative contract must be singular")
    contract = contracts[0]
    contract_id = contract.get("contract_id")
    version = contract.get("version")
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("declarative contract id is invalid")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("declarative contract version is invalid")

    normalized: dict[str, frozenset[str]] = {}
    for key in ("required_fields", "optional_fields", "forbidden_fields", "public_fields"):
        raw_fields = contract.get(key, [])
        if (
            not isinstance(raw_fields, list)
            or any(not isinstance(field, str) or not field.strip() for field in raw_fields)
            or len(set(raw_fields)) != len(raw_fields)
        ):
            raise ValueError(f"declarative contract {key} is invalid")
        normalized[key] = frozenset(raw_fields)

    required = normalized["required_fields"]
    optional = normalized["optional_fields"]
    forbidden = normalized["forbidden_fields"]
    public = normalized["public_fields"]
    if not public or not required <= public or not optional <= public or public & forbidden:
        raise ValueError("declarative contract fields are inconsistent")
    json_schema = contract.get("json_schema")
    if json_schema is not None:
        if not isinstance(json_schema, dict):
            raise ValueError("declarative contract JSON schema is invalid")
        try:
            Draft202012Validator.check_schema(json_schema)
        except SchemaError as error:
            raise ValueError("declarative contract JSON schema is invalid") from error
        schema_required = json_schema.get("required")
        schema_properties = json_schema.get("properties")
        if (
            not isinstance(schema_required, list)
            or frozenset(schema_required) != required
            or not isinstance(schema_properties, dict)
            or frozenset(schema_properties) != public
            or json_schema.get("additionalProperties") is not False
        ):
            raise ValueError("declarative contract JSON schema does not match fields")
    return contract_id, version, required, public, json_schema


@contextmanager
def _output_schema_file(schema: dict[str, Any] | None) -> Iterator[str | None]:
    """Materialize one FIN-owned declarative schema for ``codex exec`` only."""

    if schema is None:
        yield None
        return
    try:
        payload = _json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, TypeError, ValueError) as error:
        raise _OutputSchemaContractError("output schema cannot be serialized") from error

    temporary: Any | None = None
    try:
        temporary = TemporaryDirectory(prefix="fin-codex-output-schema-")
        directory = Path(temporary.name)
        directory.chmod(0o700)
        target = directory / _OUTPUT_SCHEMA_FILENAME
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags, 0o600)
        try:
            # The process umask can only remove permissions from os.open's
            # mode. Restore the exact owner-only mode before exposing the
            # pathname to the child.
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("output schema write was incomplete")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        metadata = target.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise OSError("output schema file is unsafe")
    except OSError as error:
        if temporary is not None:
            with suppress(OSError):
                temporary.cleanup()
        raise _OutputSchemaStagingError("output schema staging failed") from error

    try:
        yield str(target)
    finally:
        try:
            temporary.cleanup()
        except OSError as error:
            raise _OutputSchemaStagingError("output schema cleanup failed") from error


def _codex_response_format_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project a detached, only-relaxed Codex Structured Outputs schema."""

    def const_type(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            return "array"
        if isinstance(value, dict):
            return "object"
        raise _OutputSchemaContractError("output schema const is invalid")

    def clone(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: clone(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clone(item) for item in value]
        return value

    def project_schema(value: dict[str, Any]) -> dict[str, Any]:
        projected: dict[str, Any] = {}
        raw_required = value.get("required")
        required_names = (
            {name for name in raw_required if isinstance(name, str)}
            if isinstance(raw_required, list)
            else None
        )
        for keyword, constraint in value.items():
            if keyword in _CODEX_RESPONSE_FORMAT_REMOVED_KEYWORDS:
                continue
            if keyword not in _CODEX_RESPONSE_FORMAT_SUPPORTED_KEYWORDS:
                raise _OutputSchemaContractError("output schema keyword is unsupported")
            if keyword == "properties":
                if not isinstance(constraint, dict):
                    raise _OutputSchemaContractError("output schema properties are invalid")
                properties: dict[str, Any] = {}
                for property_name, property_schema in constraint.items():
                    if not isinstance(property_name, str) or not isinstance(property_schema, dict):
                        raise _OutputSchemaContractError("output schema property is invalid")
                    if required_names is not None and property_name not in required_names:
                        # Provider 的 response_format 强制 required 覆盖全部
                        # properties；可选字段不进 child 强制 schema，由
                        # adapter 返回后的 FIN-side 全量合同校验继续把关。
                        continue
                    properties[property_name] = project_schema(property_schema)
                projected[keyword] = properties
            elif keyword == "items":
                if not isinstance(constraint, dict):
                    raise _OutputSchemaContractError("output schema items are invalid")
                projected[keyword] = project_schema(constraint)
            elif keyword == "anyOf":
                if not isinstance(constraint, list) or any(
                    not isinstance(item, dict) for item in constraint
                ):
                    raise _OutputSchemaContractError("output schema anyOf is invalid")
                projected[keyword] = [project_schema(item) for item in constraint]
            elif keyword == "$defs":
                if not isinstance(constraint, dict):
                    raise _OutputSchemaContractError("output schema definitions are invalid")
                definitions: dict[str, Any] = {}
                for definition_name, definition_schema in constraint.items():
                    if not isinstance(definition_name, str) or not isinstance(
                        definition_schema, dict
                    ):
                        raise _OutputSchemaContractError("output schema definition is invalid")
                    definitions[definition_name] = project_schema(definition_schema)
                projected[keyword] = definitions
            elif keyword == "additionalProperties":
                if constraint is not False:
                    raise _OutputSchemaContractError(
                        "output schema additionalProperties is invalid"
                    )
                projected[keyword] = False
            else:
                projected[keyword] = clone(constraint)
        if "const" in projected and "type" not in projected:
            # Some OpenAI-compatible providers require every property schema
            # to declare a type even when ``const`` already implies it.
            projected["type"] = const_type(projected["const"])
        return projected

    return project_schema(schema)


def _consultation_output_schema(
    declarative_contract: tuple[str, str, frozenset[str], frozenset[str], dict[str, Any] | None]
    | None,
) -> dict[str, Any] | None:
    """Return a child-enforced schema only for this consultation-only lane.

    The adapter continues to validate every declarative contract after the
    child returns.  Passing ``--output-schema`` to the CLI is deliberately
    narrower: only the two consultation products use it.
    """

    if (
        declarative_contract is None
        or (declarative_contract[0], declarative_contract[1]) not in _OUTPUT_SCHEMA_CONTRACTS
    ):
        return None
    canonical_schema = declarative_contract[4]
    if canonical_schema is None:
        return None
    return _codex_response_format_schema(canonical_schema)


# ═══════════════════════════════════════════════════════════════════════════════
# Adapter
# ═══════════════════════════════════════════════════════════════════════════════


class CodexCliAgentRuntimeAdapter:
    """Codex CLI as a read-only FIN agent runtime.

    Constructor-injected dependencies allow testing with a fake process runner.

    Usage::

        adapter = CodexCliAgentRuntimeAdapter(
            codex_bin="codex",
            model="sonnet",
            workspace_path="/path/to/workspace",
            timeout_seconds=300,
        )
        result = adapter.run(agent_request)
    """

    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        model: str = "",
        reported_model: str | None = None,
        execution_fingerprint: str | None = None,
        runtime_environment: Mapping[str, str] | None = None,
        exec_options: tuple[str, ...] = (),
        perform_builtin_probe: bool = True,
        workspace_path: str | None = None,
        timeout_seconds: float = 300.0,
        runner: Any = None,
        local_capability_runner: Any = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        diagnostic_sink: CodexRuntimeDiagnosticSink | None = None,
        invocation_sink: CodexRuntimeInvocationSink | None = None,
        runtime_identity: PinnedCodexRuntimeIdentity | None = None,
        runtime_route: str | None = None,
        session_store: CodexSessionArtifactStore | None = None,
        evidence_sink: Any | None = None,
    ) -> None:
        if runtime_identity is not None:
            runtime_identity.validate()
            if codex_bin != str(runtime_identity.executable):
                raise CodexRuntimeIdentityError("codex_runtime_identity_invalid")
        if runtime_route is not None and not is_codex_route_id(runtime_route):
            raise ValueError("codex runtime route is invalid")
        if reported_model is not None and (
            not isinstance(reported_model, str) or not reported_model or len(reported_model) > 128
        ):
            raise ValueError("codex reported model is invalid")
        if execution_fingerprint is not None and (
            not isinstance(execution_fingerprint, str)
            or _SHA256_PATTERN.fullmatch(execution_fingerprint) is None
        ):
            raise ValueError("codex execution fingerprint is invalid")
        if runtime_environment is not None and (
            not isinstance(runtime_environment, Mapping)
            or not set(runtime_environment) <= _ROUTE_RUNTIME_ENV_VARS
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not value
                or len(value) > 1024
                or "\x00" in value
                for key, value in runtime_environment.items()
            )
        ):
            raise ValueError("codex runtime environment is invalid")
        if (
            not isinstance(exec_options, tuple)
            or len(exec_options) > 32
            or any(
                not isinstance(option, str)
                or not option
                or len(option) > 1024
                or "\x00" in option
                for option in exec_options
            )
        ):
            raise ValueError("codex exec options are invalid")
        if not isinstance(perform_builtin_probe, bool):
            raise ValueError("codex builtin probe setting is invalid")
        self._codex_bin = codex_bin
        self._model = model
        self._reported_model = reported_model if reported_model is not None else model
        self._execution_fingerprint = execution_fingerprint
        self._runtime_environment = dict(runtime_environment or {})
        self._exec_options = exec_options
        self._perform_builtin_probe = perform_builtin_probe
        self._workspace_path = workspace_path
        self._timeout_seconds = timeout_seconds
        self._runner: Any = runner if runner is not None else subprocess.run
        self._local_capability_runner: Any = local_capability_runner
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._diagnostic_sink = diagnostic_sink
        self._evidence_sink = evidence_sink
        self._invocation_sink = invocation_sink
        self._runtime_identity = runtime_identity
        self._runtime_route = runtime_route
        self._session_store = session_store

    def matches_identity(self, identity_hash: str) -> bool:
        """该 handle 是否属于本 runtime（route/executable/model/auth 身份）。

        Phase 3 sticky 路由用：协调器据此把续问 handle 解析回原 route。
        """
        if not isinstance(identity_hash, str):
            return False
        return identity_hash == self._resume_identity_hash()

    def _resume_identity_hash(self) -> str:
        """Resume envelope 身份：route + executable + model + auth identity。

        跨 route/identity 永不 resume（handoff §6.2/§6.5）：主备同模型也算身份
        变化——proxy 各 route 的 wrapper home/auth 不同，direct 与 proxy 的
        executable/auth 也不同。identity_hash 即这些组件的 sha256。
        """
        auth_identity = (
            getattr(self._runtime_identity, "expected_auth_identity_sha256", "")
            if self._runtime_identity is not None
            else ""
        )
        executable_identity = (
            getattr(self._runtime_identity, "expected_sha256", "")
            if self._runtime_identity is not None
            else self._codex_bin
        )
        if self._execution_fingerprint is None:
            material = "\n".join(
                [
                    "fin.codex-runtime-identity/v1",
                    self._runtime_route or "unbound",
                    executable_identity,
                    self._model or "",
                    auth_identity,
                ]
            )
        else:
            material = "\n".join(
                [
                    "fin.codex-runtime-identity/v2",
                    self._runtime_route or "unbound",
                    executable_identity,
                    self._reported_model,
                    auth_identity,
                    self._execution_fingerprint,
                ]
            )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _maybe_capture_session(
        self,
        result: AgentRunResult,
        auth_home: str | None,
    ) -> None:
        """成功后把 direct 路径的 Codex session rollout 捕获为私有快照（3D step 6）。

        proxy 路径的 CODEX_HOME 由 wrapper route-owned 持久持有（session 天然
        可 resume），无需 FIN 侧捕获；direct 路径的临时 auth home 在调用结束即
        销毁，必须在清理前捕获。捕获失败只记日志不改变结果——resume 是可丢弃
        加速层，FIN semantic chain 始终是权威恢复 spine（handoff §4.8）。
        """
        if self._session_store is None or auth_home is None:
            return
        if result.status != "ok":
            return
        envelope = result.opaque_runtime_continuation
        if not isinstance(envelope, dict):
            return
        session_id = envelope.get("session_id")
        product_version = envelope.get("product_version")
        identity_hash = envelope.get("identity_hash")
        if not (
            isinstance(session_id, str)
            and _valid_uuid_text(session_id)
            and isinstance(product_version, int)
            and not isinstance(product_version, bool)
            and product_version >= 1
            and isinstance(identity_hash, str)
            and _SHA256_PATTERN.fullmatch(identity_hash) is not None
        ):
            return
        executable_sha = (
            getattr(self._runtime_identity, "expected_sha256", "")
            if self._runtime_identity is not None
            else ""
        )
        if not _SHA256_PATTERN.fullmatch(executable_sha):
            return
        try:
            self._session_store.capture(
                session_id=session_id,
                product_version=product_version,
                runtime_identity_hash=identity_hash,
                codex_executable_sha256=executable_sha,
                source_home=Path(auth_home),
            )
        except Exception:
            logger.error("Codex session artifact capture failed (details suppressed)")

    @property
    def backend_name(self) -> str:
        """Stable provider identity for production composition diagnostics."""

        return "codex"

    def _workspace_context(self):
        # 未显式固定 workspace 时（生产咨询默认），每次问答新建一个
        # owner-only 空白 scratch cwd（TemporaryDirectory 自动 0700 与
        # 结束删除），不保存 Codex 会话历史；持久 runtime identity 与
        # 认证来源由 direct 的 CODEX_HOME（~/fin-data/codex-runtime-v1）
        # 承载，proxy 路由使用各自 route-owned home
        # （codex-proxy-a/b），direct 会话 rollout
        # 由 CodexSessionArtifactStore 保存。scratch cwd 无需保留。
        if self._workspace_path is not None:
            return nullcontext(self._workspace_path)
        return TemporaryDirectory(prefix="fin-codex-runtime-")

    # ── AgentRuntimePort implementation ─────────────────────────────────────

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Build argv, invoke Codex, parse JSONL, return sanitized result.

        Fail-closed: all error paths return status="error" with bounded
        data-gap codes.
        """
        if request.execution_fence is not None and not request.execution_fence.is_open(
            at=self._clock()
        ):
            return _fenced_runtime_result(request, model=self._reported_model)
        if self._runtime_identity is not None:
            try:
                self._runtime_identity.validate()
            except CodexRuntimeIdentityError:
                return AgentRunResult(
                    status="error",
                    payload={},
                    data_gaps=["codex_runtime_identity_invalid"],
                    provenance={
                        "backend": "codex",
                        "model": self._reported_model or "unknown",
                    },
                )
        try:
            declarative_contract = _declarative_contract_schema(request.product_contracts)
        except (TypeError, ValueError):
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_product_contract_invalid"],
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
            )

        # ── Validate reasoning_effort early — fail closed on invalid values ──
        budget = request.budget
        effort = budget.get("reasoning_effort") if budget else None
        if effort is not None and (
            not isinstance(effort, str) or effort not in _VALID_REASONING_EFFORTS
        ):
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_invalid_reasoning_effort"],
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
            )

        max_tool_calls = budget.get("max_tool_calls") if budget else None
        raw_allowed_capabilities = request.capability_scope.get("allowed_capabilities", [])
        raw_required_capabilities = request.capability_scope.get("required_capabilities", [])
        scope_max_calls = request.capability_scope.get("max_calls")
        selection_strategy = request.capability_scope.get("selection_strategy")
        direct_consultation = _is_consultation_request(request)
        allowed_capabilities_valid = isinstance(
            raw_allowed_capabilities, (list, tuple)
        ) and not any(
            not isinstance(name, str) or not name.strip() for name in raw_allowed_capabilities
        )
        has_allowed_capabilities = allowed_capabilities_valid and bool(raw_allowed_capabilities)
        required_capabilities_valid = isinstance(
            raw_required_capabilities, (list, tuple)
        ) and not any(
            not isinstance(name, str) or not name.strip() for name in raw_required_capabilities
        )
        max_tool_calls_valid = max_tool_calls is None or (
            isinstance(max_tool_calls, int)
            and not isinstance(max_tool_calls, bool)
            and max_tool_calls >= 0
        )
        scoped_budget_valid = (
            isinstance(scope_max_calls, int)
            and not isinstance(scope_max_calls, bool)
            and scope_max_calls > 0
            and isinstance(max_tool_calls, int)
            and not isinstance(max_tool_calls, bool)
            and max_tool_calls > 0
            and scope_max_calls == max_tool_calls
            and selection_strategy
            == ("AGENT_SELECTED" if direct_consultation else "SMALLEST_SUFFICIENT_SUBSET")
        )
        if (
            not allowed_capabilities_valid
            or not required_capabilities_valid
            or not max_tool_calls_valid
            or len(set(raw_allowed_capabilities)) != len(raw_allowed_capabilities)
            or len(set(raw_required_capabilities)) != len(raw_required_capabilities)
            or not set(raw_required_capabilities).issubset(raw_allowed_capabilities)
            or (
                isinstance(scope_max_calls, int)
                and len(raw_required_capabilities) > scope_max_calls
            )
            or (has_allowed_capabilities and not scoped_budget_valid)
            or (
                not has_allowed_capabilities
                and (scope_max_calls is not None or selection_strategy is not None)
            )
        ):
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_capability_scope_invalid"],
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
            )
        allowed_capabilities = tuple(raw_allowed_capabilities)
        needs_local_bridge = bool(allowed_capabilities)
        if request.use_case_ref == "decision_guidance" and not needs_local_bridge:
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_capability_scope_invalid"],
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
            )
        if needs_local_bridge and (
            request.capability_bridge is None or self._local_capability_runner is None
        ):
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_capability_bridge_unavailable"],
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
            )

        timeout_values = (request.timeout_seconds, self._timeout_seconds)
        if not all(_valid_timeout_seconds(value) for value in timeout_values):
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_invalid_timeout"],
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
            )
        request_timeout = float(request.timeout_seconds)
        effective_timeout = min(request_timeout, float(self._timeout_seconds))
        if request.execution_fence is not None:
            remaining = request.execution_fence.remaining_seconds(at=self._clock())
            if remaining <= 0:
                return _fenced_runtime_result(request, model=self._reported_model)
            effective_timeout = min(effective_timeout, remaining)
        # ── Phase 3D：resume 决策（handoff §6.2/§6.4/§6.5）───────────────
        # request 的 opaque_runtime_continuation 是上一轮 FIN 持久化的私有
        # envelope（backend/session_id/identity_hash/product_version）。
        # backend/session_id/identity_hash 全部精确匹配当前 route/identity
        # 才 resume 同一 Codex session；任何不匹配视为 fresh（FIN semantic
        # rehydrate 新建 session），绝不跨 route/identity resume（主备同模型
        # 也算身份变化）。product_version 单调 = 上一轮 + 1（repository 校验
        # 必须等于本轮新版本）。
        resume_session_id: str | None = None
        expected_product_version = 1
        continuation = request.opaque_runtime_continuation
        # A2: 带 continuation 但实际转入 fresh 的调用必须留下 typed 事实,
        # 不得静默冒充 resume。resume 不被接受(外来/损坏 handle)即 fresh;
        # 接受后 resume-before-activity 失败再 fresh 的路径在下方独立标记。
        had_continuation = isinstance(continuation, dict) and bool(continuation)
        if (
            isinstance(continuation, dict)
            and continuation.get("backend") == "codex-cli"
            and isinstance(continuation.get("session_id"), str)
            and _valid_uuid_text(continuation["session_id"])
            and isinstance(continuation.get("identity_hash"), str)
            and continuation["identity_hash"] == self._resume_identity_hash()
            # A3: product_version 必须是严格正整数才接受 exact resume——
            # 缺失/非 int/bool/<=0 的 envelope 不得 resume（非法 version 的
            # handle 无法确定 materialize 的 prior 版本）。
            and isinstance(continuation.get("product_version"), int)
            and not isinstance(continuation.get("product_version"), bool)
            and continuation["product_version"] >= 1
        ):
            resume_session_id = continuation["session_id"]
            expected_product_version = continuation["product_version"] + 1
        identity_hash = self._resume_identity_hash()
        # A2: 带 continuation 但实际转入 fresh → 显式降级事实。只有真实
        # fresh child 启动后才置位（下方在第一个 invocation 前判断）：
        # pre-child schema contract/staging 失败时没有任何 child 启动，
        # 必须保持 False，不得把"选择 fresh"误报成"已转入 fresh"。
        continuity_degraded = False
        try:
            output_schema = _consultation_output_schema(declarative_contract)
            with (
                self._workspace_context() as workspace_path,
                _output_schema_file(output_schema) as output_schema_path,
            ):
                command = self._build_command(
                    request,
                    workspace_path=str(workspace_path),
                    output_schema_path=output_schema_path,
                    resume_session_id=resume_session_id,
                )
                # A1: 每个真实 child 调用独立记录 started/terminated 与 stage。
                # initial(无有效 handle)与 resume(接受 handle)是不同 stage;
                # resume-before-activity 失败后的第二次 fresh child 是
                # fresh_after_resume,不再与 resume 压成一个 invocation。
                invocation_stage = (
                    "resume_runtime" if resume_session_id is not None else "initial_runtime"
                )
                # A2: 真实 fresh child 启动边界才提交降级事实——要么是
                # outer 的 clean-break 私有意图（sticky 已清空 continuation），
                # 要么是带 continuation 但 resume 不被接受（外来/损坏 handle）。
                # pre-child contract/staging 失败不会到达这里，保持 False。
                if request.continuity_degraded_intent or (
                    had_continuation and resume_session_id is None
                ):
                    continuity_degraded = True
                result = self._invoke_command(
                    request=request,
                    command=command,
                    workspace_path=str(workspace_path),
                    needs_local_bridge=needs_local_bridge,
                    allowed_capabilities=allowed_capabilities,
                    effective_timeout=effective_timeout,
                    max_tool_calls=max_tool_calls,
                    reasoning_effort=effort,
                    resume_session_id=resume_session_id,
                    continuation_identity_hash=identity_hash,
                    continuation_product_version=expected_product_version,
                    stage=invocation_stage,
                )
                # A1: error id 在故障 origin 确定、写诊断之前生成一次。未在
                # _invoke_command 内生成 error id 的 error 分支,此处即首个
                # 写诊断点,回填后沿 AgentRunResult 只复制不重生成。
                if result.status == "error" and result.error_id is None:
                    result = replace(result, error_id=new_error_id())
                # resume-before-tool 失败边界（handoff §6.4）：resume 在任何
                # capability 活动前失败 → 丢弃该私有 handle，fresh 重来一次；
                # resume-after-activity 不确定 → fail closed，不静默重执行。
                # A1: resume child 的 failed 终态由 _invoke_command 记录
                # 由 _invoke_command 按失败时刻 activity 状态记录;run() 不再
                # 重复记录;fresh 是第二次真实 child,成对记录。
                if (
                    resume_session_id is not None
                    and result.status == "error"
                    # A completed Agent answer that fails FIN's canonical
                    # schema has crossed the activity boundary.  Retrying it
                    # as fresh would silently ask a second Agent to re-answer.
                    and result.data_gaps
                    not in (
                        ["codex_product_contract_violation"],
                        ["codex_auth_refresh_persistence_failed"],
                    )
                    and not _resume_activity_uncertain(request)
                ):
                    fresh_command = self._build_command(
                        request,
                        workspace_path=str(workspace_path),
                        output_schema_path=output_schema_path,
                        resume_session_id=None,
                    )
                    result = self._invoke_command(
                        request=request,
                        command=fresh_command,
                        workspace_path=str(workspace_path),
                        needs_local_bridge=needs_local_bridge,
                        allowed_capabilities=allowed_capabilities,
                        effective_timeout=effective_timeout,
                        max_tool_calls=max_tool_calls,
                        reasoning_effort=effort,
                        resume_session_id=None,
                        continuation_identity_hash=identity_hash,
                        continuation_product_version=expected_product_version,
                        stage="fresh_after_resume",
                    )
                    if result.status == "error" and result.error_id is None:
                        result = replace(result, error_id=new_error_id())
                    # A2: resume-before-activity 失败后实际执行了 fresh child,
                    # 无论 fresh 成功还是失败都必须显式降级。
                    continuity_degraded = True
                if continuity_degraded and not result.continuity_degraded:
                    result = replace(result, continuity_degraded=True)
                return result
        except _OutputSchemaContractError:
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_product_contract_invalid"],
                capability_trace=(
                    request.capability_bridge.trace if request.capability_bridge is not None else []
                ),
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
                # A2: 降级事实只在真实 fresh child 启动边界置位；pre-child
                # contract 失败没有 child 启动，continuity_degraded 保持初始
                # False（clean-break 意图由 request.continuity_degraded_intent
                # 传递，此处不消费）。
                continuity_degraded=continuity_degraded,
            )
        except _OutputSchemaStagingError:
            logger.error("Codex output schema staging failed (details suppressed for safety)")
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_output_schema_unavailable"],
                capability_trace=(
                    request.capability_bridge.trace if request.capability_bridge is not None else []
                ),
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
                # A2: child 执行后 cleanup 失败时，try 块内已按实际执行路径
                # 置位（含 fresh_after_resume=True），此处必须继承，不得重建
                # 成 False 冒充正常 resume。
                continuity_degraded=continuity_degraded,
            )

    def _invoke_command(
        self,
        *,
        request: AgentRunRequest,
        command: list[str],
        workspace_path: str,
        needs_local_bridge: bool,
        allowed_capabilities: tuple[str, ...],
        effective_timeout: float,
        max_tool_calls: Any,
        reasoning_effort: Any,
        resume_session_id: str | None = None,
        continuation_identity_hash: str | None = None,
        continuation_product_version: int | None = None,
        stage: str = "initial_runtime",
    ) -> AgentRunResult:
        """Run one already-built command and keep child failures bounded.

        A1: 每个真实 child 的 started/terminated 都在本方法内成对记录;
        ``stage`` 由调用方语义决定;失败 classifier 按失败时刻的
        resume-before-activity 语义决定(B2: 不提前计算)。
        """

        # B1: 与 terminated 的 elapsed 同用系统单调钟,避免注入时钟混用。
        started_at = time.monotonic()
        try:
            spawn_binding = (
                self._runtime_identity.spawn_binding()
                if self._runtime_identity is not None
                else nullcontext((self._codex_bin, (), None, lambda: None))
            )
            with spawn_binding as (
                spawn_executable,
                pass_fds,
                auth_home,
                finalize_auth,
            ):
                # A3: direct resume 在 child 启动前把已捕获的 exact session
                # rollout materialize 到本轮临时 auth CODEX_HOME——临时 home
                # 每次调用重建，不 materialize 则底层 codex 找不到 session，
                # exact resume 必然失败。materialize 失败是 best-effort 前置：
                # 不启动 exact child，走既有 resume-before-activity fresh。
                materialize_failed = False
                if resume_session_id is not None and auth_home is not None:
                    # A3-R1-F6: direct 路径缺 session store 或合法 prior version
                    # 时不得启动 exact child——走 materialize-failure→fresh。
                    if (
                        self._session_store is None
                        or continuation_product_version is None
                        or continuation_product_version < 2
                    ):
                        logger.error(
                            "Codex session materialize unavailable (details suppressed for safety)"
                        )
                        materialize_failed = True
                    else:
                        try:
                            # materialize 上一轮已捕获的版本（prior = expected-1）；
                            # expected 是即将写入的新版本，尚未捕获。
                            self._session_store.materialize(
                                session_id=resume_session_id,
                                product_version=continuation_product_version - 1,
                                dest_home=Path(auth_home),
                            )
                        except Exception:
                            # 缺失/损坏 artifact：不启动 exact provider child，
                            # 交给既有 resume-before-activity fresh 边界。
                            logger.error(
                                "Codex session materialize failed (details suppressed for safety)"
                            )
                            materialize_failed = True
                # ── 上游探活(pre-child phase,仅 fresh direct-primary,N3/N6)──
                # probe 只做 availability 判定;confirmed_unavailable 才走
                # codex_probe_failed→failover;reachable/inconclusive 继续 child。
                probe_elapsed = 0.0
                if (
                    resume_session_id is None
                    and self._runtime_route == "direct-primary"
                    and auth_home is not None
                    and self._perform_builtin_probe
                ):
                    probe_started = time.monotonic()
                    probe_outcome, probe_origin, probe_status = _probe_upstream(
                        auth_home,
                        model=self._model,
                    )
                    probe_elapsed = max(0.0, time.monotonic() - probe_started)
                    if probe_outcome == "confirmed_unavailable":
                        probe_error_id = new_error_id()
                        self._record_failure_event(
                            event_kind="probe_failure",
                            occurred_at=self._clock(),
                            model=self._reported_model or "unknown",
                            exit_code=None,
                            stderr="",
                            stdout="",
                            error_id=probe_error_id,
                            elapsed_seconds=probe_elapsed,
                            route=self._runtime_route,
                            stage=stage,
                            probe_origin=probe_origin,
                            http_status=probe_status,
                        )
                        # 成对记录 invocation terminated(failed):probe 不是
                        # child 运行,但 invocation 尝试确实发生且失败。
                        self._record_invocation_terminated(
                            started_at=started_at,
                            status="failed",
                            stage=stage,
                            route=self._runtime_route,
                            classifier="codex_probe_failed",
                            error_id=probe_error_id,
                        )
                        return AgentRunResult(
                            status="error",
                            payload={},
                            data_gaps=["codex_probe_failed"],
                            provenance={
                                "backend": "codex",
                                "model": self._reported_model or "unknown",
                                "runtime_failure_class": "CODEX_CHILD_UPSTREAM",
                            },
                            error_id=probe_error_id,
                        )

                # ── early startup marker(fresh-only best-effort,N7)──
                early_progress_check: Callable[[], bool] | None = None
                if (
                    resume_session_id is None
                    and self._runtime_route == "direct-primary"
                    and auth_home is not None
                ):
                    sessions_root = Path(auth_home) / "sessions"
                    baseline: frozenset[str] = frozenset()
                    try:
                        if sessions_root.is_dir():
                            baseline = frozenset(
                                str(path.relative_to(sessions_root))
                                for path in sessions_root.rglob("rollout-*.jsonl")
                            )
                    except OSError:
                        baseline = frozenset()

                    def marker() -> bool:
                        try:
                            if not sessions_root.is_dir():
                                return False
                            for path in sessions_root.rglob("rollout-*.jsonl"):
                                if str(path.relative_to(sessions_root)) in baseline:
                                    continue
                                # 只接受 owner regular 文件(拒 symlink/目录),
                                # 空/半写文件不算(N7 校验)。
                                try:
                                    metadata = path.lstat()
                                except OSError:
                                    continue
                                if (
                                    not stat.S_ISREG(metadata.st_mode)
                                    or metadata.st_uid != os.getuid()
                                    or metadata.st_size == 0
                                ):
                                    continue
                                return True
                            return False
                        except OSError:
                            return False

                    early_progress_check = marker

                # ── child 预算重算(N5 用户拍板:probe 消耗 cap 内预算)──
                # audit r1-4:预算不足(剩余 < fallback 保留)则不启动 child,
                # 不因 max(30,...) 突破总 cap。
                remaining_after_probe = effective_timeout - probe_elapsed
                if probe_elapsed > 0:
                    # 只有实际探活消耗预算时才扣 reserve(audit r2-2:
                    # 无探活路径不改既有 timeout 契约)。
                    child_timeout = remaining_after_probe - _PROBE_FALLBACK_RESERVE_SECONDS
                    if child_timeout < _DEFAULT_MINIMUM_PRIMARY_SECONDS:
                        child_timeout = remaining_after_probe
                else:
                    child_timeout = remaining_after_probe
                # A1: invocation started 在 probe/budget 检查之后、child Popen
                # 前记录(audit r2-5/r3-4:probe 失败与预算拒绝零 child 事件)。
                self._record_invocation_started(
                    stage=stage,
                    route=self._runtime_route,
                )
                if child_timeout <= 0:
                    budget_error_id = new_error_id()
                    self._record_failure_event(
                        event_kind="timeout",
                        occurred_at=self._clock(),
                        model=self._reported_model or "unknown",
                        exit_code=None,
                        stderr="",
                        stdout="",
                        error_id=budget_error_id,
                        elapsed_seconds=probe_elapsed,
                        route=self._runtime_route,
                        stage=stage,
                    )
                    self._record_invocation_terminated(
                        started_at=started_at,
                        status="failed",
                        stage=stage,
                        route=self._runtime_route,
                        classifier="codex_timeout",
                        error_id=budget_error_id,
                    )
                    return AgentRunResult(
                        status="error",
                        payload={},
                        data_gaps=["codex_timeout"],
                        provenance={
                            "backend": "codex",
                            "model": self._reported_model or "unknown",
                            "runtime_failure_class": "CODEX_CHILD_TIMEOUT",
                        },
                        error_id=budget_error_id,
                    )
                runtime_environment = _sanitized_runtime_environment()
                if auth_home is not None:
                    runtime_environment["CODEX_HOME"] = auth_home
                runtime_environment.update(self._runtime_environment)

                def run_bound(
                    child_command: list[str],
                    *,
                    runner: Any,
                    timeout: float,
                    bind_capabilities: bool,
                ) -> Any:
                    # Popen 前最终 fence/预算检查(audit r2-3):materialize/
                    # probe/marker 准备耗时后 fence 可能已关闭,不得启动 child。
                    if request.execution_fence is not None and not request.execution_fence.is_open(
                        at=self._clock()
                    ):
                        raise _FenceClosedError()
                    if timeout <= 0:
                        raise _FenceClosedError()
                    child_command[0] = spawn_executable
                    process_kwargs: dict[str, Any] = {
                        "cwd": workspace_path,
                        "capture_output": True,
                        "env": runtime_environment,
                        "text": True,
                        "timeout": timeout,
                        "check": False,
                        "shell": False,
                        # codex exec 的 prompt 来自 argv；继承 MCP/调用方管道 stdin
                        # 会让 codex CLI 打印 "Reading additional input from stdin..."
                        # 并可能等待 stdin（A6-R3b 实测：direct 0.145 挂起）。
                        "stdin": subprocess.DEVNULL,
                    }
                    # A3-R1-F2: direct 路径（临时 auth CODEX_HOME）让 child 以
                    # owner-only umask 创建 rollout——不依赖 ambient umask，
                    # 保证 capture 后 store 严格读取成功。proxy 路径由 wrapper
                    # 管理 home，不在此设。
                    if auth_home is not None:
                        process_kwargs["umask"] = 0o077
                    if pass_fds:
                        process_kwargs["pass_fds"] = pass_fds
                    # A/B launcher fd 绑定（codex 审核 High B）：执行对象必须
                    # 是校验过的字节。wrapper 路径精确匹配时，open + 身份/SHA
                    # 校验，改用 /proc/self/fd/N 执行（校验后路径替换不影响
                    # 已打开的 inode）；direct identity 的 spawn_executable
                    # 本身已是 fd 模式，不重复绑定。此处在 run_bound 开头统一
                    # 处理——capability 路径的 LocalMcpCapabilityRunner 同样
                    # 收到绑定后的 command[0]。pass_fds 让子进程继承 fd；
                    # 父进程在 runner 返回后 finally 关闭（不泄漏）。
                    bound_fd: int | None = None
                    try:
                        if _is_route_launcher(spawn_executable):
                            bound_fd = _open_bound_route_launcher(Path(spawn_executable))
                            child_command[0] = f"/proc/self/fd/{bound_fd}"
                            process_kwargs["pass_fds"] = (
                                *process_kwargs.get("pass_fds", ()),
                                bound_fd,
                            )
                        if bind_capabilities:
                            process_kwargs["capability_bridge"] = request.capability_bridge
                            process_kwargs["allowed_capabilities"] = allowed_capabilities
                        # 无输出看门狗：所有 subprocess 系路径（codex CLI、proxy 脚本、
                        # capability 桥）无 stdout/stderr 事件超过
                        # STALL_DETECTION_SECONDS 即终止 + failover（不被动等 cap）。
                        if isinstance(runner, LocalMcpCapabilityRunner):
                            # capability 路径：runner 负责 MCP session 注入与 deadline；
                            # 生产接线已在 runner 上启用 stall watchdog（stall_seconds）。
                            # early marker(fresh-only)同样贯通 capability 路径(N7)。
                            if early_progress_check is not None:
                                process_kwargs["early_progress_check"] = early_progress_check
                                process_kwargs["early_progress_seconds"] = _EARLY_PROGRESS_SECONDS
                            return runner(child_command, **process_kwargs)
                        if runner is subprocess.run:
                            # wrapper 身份绑定已在上方统一处理（fd 绑定 +
                            # /proc/self/fd 执行）。Route policy/launcher marker
                            # comes from the attested runtime environment; this
                            # adapter never derives or switches a provider from a
                            # runner function name.
                            # timeout 由 watchdog 显式接收——process_kwargs 不能再携带
                            # timeout（否则与 timeout= 关键字冲突 → TypeError）。
                            process_kwargs.pop("timeout", None)
                            return _run_with_stall_watchdog(
                                child_command,
                                timeout=timeout,
                                stall_seconds=STALL_DETECTION_SECONDS,
                                early_progress_check=early_progress_check,
                                early_progress_seconds=_EARLY_PROGRESS_SECONDS,
                                **process_kwargs,
                            )
                        return runner(child_command, **process_kwargs)
                    finally:
                        try:
                            if bound_fd is not None:
                                os.close(bound_fd)
                        finally:
                            # Auth rotation is part of this child's terminal
                            # state and must settle before any terminated event.
                            finalize_auth()

                process_runner = (
                    self._local_capability_runner if needs_local_bridge else self._runner
                )
                if materialize_failed:
                    # A3: materialize 失败（缺失/损坏 artifact）→ 不启动 exact
                    # provider child；构造 resume-before-activity 失败结果，
                    # 由 run() 的既有 fresh 边界处理（最多 fresh 一次并标记
                    # DEGRADED_FRESH）。gap 用稳定通用码（私有日志已记录
                    # materialize 细节，不进公开结果）。
                    materialize_error_id = new_error_id()
                    materialize_result = AgentRunResult(
                        status="error",
                        payload={},
                        data_gaps=["agent_runtime_unavailable"],
                        provenance={
                            "backend": "codex",
                            "model": self._reported_model or "unknown",
                            "runtime_failure_class": "session_materialize_failed",
                        },
                        error_id=materialize_error_id,
                    )
                    self._record_child_terminated(
                        started_at=started_at,
                        stage=stage,
                        result=materialize_result,
                        resume_activity_uncertain=False,
                    )
                    return materialize_result
                completed = run_bound(
                    command,
                    runner=process_runner,
                    timeout=child_timeout,
                    bind_capabilities=needs_local_bridge,
                )

                # B2: classifier 按失败时刻的 activity 状态决定(child 运行后
                # 可能已开始 capability 活动)。
                resume_activity_uncertain: bool | None = (
                    _resume_activity_uncertain(request) if stage == "resume_runtime" else None
                )
                if request.execution_fence is not None and not request.execution_fence.is_open(
                    at=self._clock()
                ):
                    # B1: fence 关闭也是 child 终态,记录 failed 后返回。
                    fenced = _fenced_runtime_result(request, model=self._reported_model)
                    if fenced.error_id is None:
                        fenced = replace(fenced, error_id=new_error_id())
                    self._record_child_terminated(
                        started_at=started_at,
                        stage=stage,
                        result=fenced,
                        resume_activity_uncertain=resume_activity_uncertain,
                    )
                    return fenced

                if completed.returncode != 0:
                    # A1: error id 在故障 origin 确定、写诊断之前生成一次;
                    # 同一故障经各层只复制,不重新生成。
                    failure_error_id = new_error_id()
                    self._record_nonzero_diagnostic(completed, error_id=failure_error_id)
                    runtime_failure_class = classify_codex_failover_failure(
                        exit_code=completed.returncode,
                        stdout=completed.stdout if isinstance(completed.stdout, str) else "",
                    )
                    failure_result = AgentRunResult(
                        status="error",
                        payload={},
                        data_gaps=[f"codex_nonzero_exit:{completed.returncode}"],
                        capability_trace=(
                            request.capability_bridge.trace
                            if request.capability_bridge is not None
                            else []
                        ),
                        provenance={
                            "backend": "codex",
                            "model": self._reported_model or "unknown",
                            "runtime_failure_class": runtime_failure_class,
                        },
                        error_id=failure_error_id,
                        exit_code=completed.returncode,
                    )
                    # A1: 非零退出是首 child 的真实终态,独立记录 failed。
                    self._record_child_terminated(
                        started_at=started_at,
                        stage=stage,
                        result=failure_result,
                        resume_activity_uncertain=resume_activity_uncertain,
                    )
                    return failure_result

                capability_trace = (
                    request.capability_bridge.trace if request.capability_bridge is not None else []
                )
                result = self._parse_result(
                    completed.stdout,
                    command,
                    max_tool_calls=max_tool_calls,
                    reasoning_effort=reasoning_effort,
                    capability_trace=capability_trace,
                    product_contracts=request.product_contracts,
                    continuation_identity_hash=continuation_identity_hash,
                    continuation_product_version=continuation_product_version,
                )
                # A1: 真实 child 已正常退出(returncode=0);后续解析/contract
                # 失败仍保留真实 exit=0,不得记成 None。error id 在写诊断前
                # 回填(B1: 终态事件必须携带 error id,不能由 run() 补)。
                if result.exit_code is None:
                    result = replace(result, exit_code=completed.returncode)
                if result.status == "error" and result.error_id is None:
                    result = replace(result, error_id=new_error_id())
                # parse 类失败(exit=0 但缺 terminal/输出畸形)→ parse_failure 事件。
                if (
                    result.status == "error"
                    and result.exit_code == 0
                    and any(
                        gap in result.data_gaps
                        for gap in (
                            "codex_missing_terminal",
                            "codex_missing_final_output",
                            "codex_malformed_product",
                        )
                    )
                ):
                    self._record_failure_event(
                        event_kind="parse_failure",
                        occurred_at=self._clock(),
                        model=self._reported_model or "unknown",
                        exit_code=0,
                        stderr="",
                        stdout="",
                        error_id=result.error_id or "",
                        elapsed_seconds=max(0.0, time.monotonic() - started_at),
                        route=self._runtime_route,
                        stage=stage,
                    )
                # A3: exact resume 的后置校验——回报的 session id 必须与请求
                # 的 exact id 唯一一致；缺失、不同或冲突（thread.started 与
                # session_meta 不一致）都意味着 provider 恢复错误会话，立即
                # 进入 resume 失败边界（不发布、不 capture）。
                if stage == "resume_runtime" and resume_session_id is not None:
                    returned_id = _extract_consistent_thread_id(completed.stdout)
                    if returned_id != resume_session_id:
                        identity_error_id = new_error_id()
                        self._record_child_terminated(
                            started_at=started_at,
                            stage=stage,
                            result=replace(
                                result,
                                status="error",
                                payload={},
                                data_gaps=["agent_runtime_unavailable"],
                                error_id=identity_error_id,
                                opaque_runtime_continuation={},
                            ),
                            resume_activity_uncertain=(
                                _resume_activity_uncertain(request)
                                if stage == "resume_runtime"
                                else False
                            ),
                        )
                        return replace(
                            result,
                            status="error",
                            payload={},
                            data_gaps=["agent_runtime_unavailable"],
                            error_id=identity_error_id,
                            opaque_runtime_continuation={},
                        )
                # The first Agent owns the consultation answer.  A canonical
                # contract violation is an honest failure; FIN must not launch
                # another LLM to rewrite or replace that answer.
                self._record_child_terminated(
                    started_at=started_at,
                    stage=stage,
                    result=result,
                    resume_activity_uncertain=(
                        None
                        if result.data_gaps == ["codex_product_contract_violation"]
                        else resume_activity_uncertain
                    ),
                )
                self._maybe_capture_session(result, auth_home)
                return result

        except _AuthRefreshFinalizationError:
            return self._child_failure_result(
                request=request,
                started_at=started_at,
                stage=stage,
                data_gap="codex_auth_refresh_persistence_failed",
                runtime_failure_class="CODEX_CHILD_AUTH",
            )
        except CodexRuntimeIdentityError:
            # 熔断修复: identity drift 也是 child 终态,成对记录。
            return self._child_failure_result(
                request=request,
                started_at=started_at,
                stage=stage,
                data_gap="codex_runtime_identity_invalid",
            )
        except _FenceClosedError:
            return _fenced_runtime_result(request, model=self._reported_model)

        except subprocess.TimeoutExpired as timeout_error:
            timeout_error_id = new_error_id()
            self._record_failure_event(
                event_kind="timeout",
                occurred_at=self._clock(),
                model=self._reported_model or "unknown",
                exit_code=None,
                stderr="",
                stdout="",
                error_id=timeout_error_id,
                elapsed_seconds=max(0.0, time.monotonic() - started_at),
                route=self._runtime_route,
                stage=stage,
                truncated=getattr(timeout_error, "truncated", False),
                stderr_bytes_total=getattr(timeout_error, "stderr_total_bytes", None),
                stderr_sha256_total=getattr(timeout_error, "stderr_sha256", None),
                stdout_bytes_total=getattr(timeout_error, "stdout_total_bytes", None),
                stdout_sha256_total=getattr(timeout_error, "stdout_sha256", None),
            )
            return self._child_failure_result(
                request=request,
                started_at=started_at,
                stage=stage,
                data_gap="codex_timeout",
                error_id=timeout_error_id,
            )
        except CodexStallError as stall_error:
            # 无输出看门狗：5 分钟无 stdout/stderr 事件即终止（不被动等 cap），
            # codex_stall 在 _FAILOVER_ELIGIBLE_GAPS 内 → 触发 failover。
            stall_error_id = new_error_id()
            self._record_failure_event(
                event_kind="stall",
                occurred_at=self._clock(),
                model=self._reported_model or "unknown",
                exit_code=None,
                stderr="",
                stdout="",
                error_id=stall_error_id,
                elapsed_seconds=max(0.0, time.monotonic() - started_at),
                route=self._runtime_route,
                stage=stage,
                truncated=getattr(stall_error, "truncated", False),
                stderr_bytes_total=getattr(stall_error, "stderr_total_bytes", None),
                stderr_sha256_total=getattr(stall_error, "stderr_sha256", None),
                stdout_bytes_total=getattr(stall_error, "stdout_total_bytes", None),
                stdout_sha256_total=getattr(stall_error, "stdout_sha256", None),
            )
            return self._child_failure_result(
                request=request,
                started_at=started_at,
                stage=stage,
                data_gap="codex_stall",
                runtime_failure_class="CODEX_CHILD_STALL",
                error_id=stall_error_id,
            )
        except LocalCapabilityTransportError:
            return self._child_failure_result(
                request=request,
                started_at=started_at,
                stage=stage,
                data_gap="codex_capability_transport_error",
            )
        except (OSError, subprocess.SubprocessError):
            spawn_error_id = new_error_id()
            self._record_failure_event(
                event_kind="spawn_failure",
                occurred_at=self._clock(),
                model=self._reported_model or "unknown",
                exit_code=None,
                stderr="",
                stdout="",
                error_id=spawn_error_id,
                elapsed_seconds=max(0.0, time.monotonic() - started_at),
                route=self._runtime_route,
                stage=stage,
            )
            return self._child_failure_result(
                request=request,
                started_at=started_at,
                stage=stage,
                data_gap="codex_invocation_error",
                error_id=spawn_error_id,
            )
        except Exception:
            logger.error("Codex runtime failed (details suppressed for safety)")
            return self._child_failure_result(
                request=request,
                started_at=started_at,
                stage=stage,
                data_gap="codex_runtime_internal_error",
            )

    def _record_invocation_started(
        self,
        *,
        stage: str = "initial_runtime",
        route: str | None = None,
    ) -> None:
        if self._invocation_sink is None:
            return
        try:
            self._invocation_sink.record(
                CodexRuntimeInvocationEvent(
                    phase="started",
                    occurred_at=self._clock(),
                    model=self._reported_model or "unknown",
                    stage=stage,  # type: ignore[arg-type]
                    route=route,
                )
            )
        except Exception:
            # Diagnostics are best-effort; a broken sink must never fail the call.
            logger.error("Codex invocation trace start failed (details suppressed for safety)")

    def _record_invocation_terminated(
        self,
        *,
        started_at: float,
        status: str,
        stage: str = "initial_runtime",
        route: str | None = None,
        classifier: str | None = None,
        failover_classifier: str | None = None,
        error_id: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        if self._invocation_sink is None:
            return
        try:
            # 模块级单调钟：不消耗注入的 monotonic（测试确定性时钟可能是有界序列）。
            self._invocation_sink.record(
                CodexRuntimeInvocationEvent(
                    phase="terminated",
                    occurred_at=self._clock(),
                    model=self._reported_model or "unknown",
                    elapsed_seconds=max(0.0, time.monotonic() - started_at),
                    status=status,
                    stage=stage,  # type: ignore[arg-type]
                    route=route,
                    classifier=classifier,
                    failover_classifier=failover_classifier,
                    error_id=error_id,
                    exit_code=exit_code,
                )
            )
        except Exception:
            logger.error("Codex invocation trace end failed (details suppressed for safety)")

    def _record_child_terminated(
        self,
        *,
        started_at: float,
        stage: str,
        result: AgentRunResult,
        resume_activity_uncertain: bool | None = None,
    ) -> None:
        """A1: 一个真实 child 的独立终态(succeeded/failed + 闭集字段)。

        B2: classifier 按失败时刻的 activity 状态决定;resume 且无活动 →
        resume_before_activity_failed,其余用实际 runtime 分类。
        """

        failed = result.status == "error"
        classifier: str | None = None
        if failed:
            if resume_activity_uncertain is False:
                classifier = "resume_before_activity_failed"
            else:
                classifier = result.provenance.get("runtime_failure_class") or "internal_error"
        self._record_invocation_terminated(
            started_at=started_at,
            status="failed" if failed else "succeeded",
            stage=stage,
            route=self._runtime_route,
            classifier=classifier,
            error_id=result.error_id,
            exit_code=result.exit_code,
        )

    def _child_failure_result(
        self,
        *,
        request: AgentRunRequest,
        started_at: float,
        stage: str,
        data_gap: str,
        runtime_failure_class: str | None = None,
        error_id: str | None = None,
    ) -> AgentRunResult:
        """A1/B1: 构造 child 失败结果并在返回前记录 failed 终态。

        error id 在写诊断前生成,沿最终结果链只复制;resume 场景按失败
        时刻 activity 状态决定 classifier(B2)。
        """

        error_id = error_id if error_id is not None else new_error_id()
        provenance: dict[str, object] = {
            "backend": "codex",
            "model": self._reported_model or "unknown",
        }
        if runtime_failure_class is not None:
            provenance["runtime_failure_class"] = runtime_failure_class
        result = AgentRunResult(
            status="error",
            payload={},
            data_gaps=[data_gap],
            capability_trace=(
                request.capability_bridge.trace if request.capability_bridge is not None else []
            ),
            provenance=provenance,
            error_id=error_id,
        )
        resume_activity_uncertain: bool | None = None
        if stage == "resume_runtime":
            resume_activity_uncertain = _resume_activity_uncertain(request)
        self._record_child_terminated(
            started_at=started_at,
            stage=stage,
            result=result,
            resume_activity_uncertain=resume_activity_uncertain,
        )
        return result

    def _record_failure_event(
        self,
        *,
        event_kind: str,
        occurred_at: datetime,
        model: str,
        exit_code: int | None,
        stderr: str,
        stdout: str,
        error_id: str,
        elapsed_seconds: float,
        route: str | None = None,
        stage: str | None = None,
        truncated: bool = False,
        probe_origin: str | None = None,
        http_status: int | None = None,
        stderr_bytes_total: int | None = None,
        stderr_sha256_total: str | None = None,
        stdout_bytes_total: int | None = None,
        stdout_sha256_total: str | None = None,
    ) -> None:
        """记录 v4 判别式失败事件(脱敏摘要,不落原始输出)。

        best-effort:诊断失败不影响调用结果。
        """

        if self._diagnostic_sink is None and self._evidence_sink is None:
            return
        try:
            if self._diagnostic_sink is not None:
                self._diagnostic_sink.record(
                    CodexRuntimeFailureEvent(
                        occurred_at=occurred_at,
                        exit_code=exit_code,
                        model=model,
                        stderr=stderr,
                        stdout=stdout,
                        event_kind=event_kind,
                        error_id=error_id,
                        elapsed_seconds=elapsed_seconds,
                        route=route,
                        stage=stage,
                        truncated=truncated,
                        probe_origin=probe_origin,
                        http_status=http_status,
                        stderr_bytes_total=stderr_bytes_total,
                        stderr_sha256_total=stderr_sha256_total,
                        stdout_bytes_total=stdout_bytes_total,
                        stdout_sha256_total=stdout_sha256_total,
                    )
                )
        except Exception:
            logger.error("Codex private failure diagnostic was not recorded")
        if self._evidence_sink is not None:
            try:
                self._evidence_sink.record(
                    CodexRuntimeFailureEvent(
                        occurred_at=occurred_at,
                        exit_code=exit_code,
                        model=model,
                        stderr=stderr,
                        stdout=stdout,
                        event_kind=event_kind,
                        error_id=error_id,
                        elapsed_seconds=elapsed_seconds,
                        route=route,
                        stage=stage,
                        truncated=truncated,
                        probe_origin=probe_origin,
                        http_status=http_status,
                        stderr_bytes_total=stderr_bytes_total,
                        stderr_sha256_total=stderr_sha256_total,
                        stdout_bytes_total=stdout_bytes_total,
                        stdout_sha256_total=stdout_sha256_total,
                    )
                )
            except Exception:
                logger.error("Codex private failure evidence was not recorded")

    def _record_nonzero_diagnostic(
        self,
        completed: subprocess.CompletedProcess[Any],
        *,
        error_id: str = "",
    ) -> None:
        event = CodexRuntimeFailureEvent(
            occurred_at=self._clock(),
            exit_code=completed.returncode,
            model=self._reported_model or "unknown",
            stderr=completed.stderr if isinstance(completed.stderr, str) else "",
            stdout=completed.stdout if isinstance(completed.stdout, str) else "",
            event_kind="exit_failure",
            error_id=error_id,
            elapsed_seconds=0.0,
            route=self._runtime_route,
            truncated=getattr(completed, "truncated", False),
            stderr_bytes_total=getattr(completed, "stderr_total_bytes", None),
            stderr_sha256_total=getattr(completed, "stderr_sha256", None),
            stdout_bytes_total=getattr(completed, "stdout_total_bytes", None),
            stdout_sha256_total=getattr(completed, "stdout_sha256", None),
        )
        if self._diagnostic_sink is not None:
            try:
                self._diagnostic_sink.record(event)
            except Exception:
                logger.error("Codex private failure diagnostic was not recorded")
        if self._evidence_sink is not None:
            try:
                self._evidence_sink.record(event)
            except Exception:
                logger.error("Codex private failure evidence was not recorded")

    # ── Command building ────────────────────────────────────────────────────

    def _build_command(
        self,
        request: AgentRunRequest,
        *,
        workspace_path: str | None = None,
        output_schema_path: str | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        """Build the argv list — no shell, no credential substitution.

        Phase 3D：initial 路径不再带 ``--ephemeral``（线程落入 CODEX_HOME 的
        session store 以便后续 resume）；resume 路径为
        ``exec <shared> resume <session_id> <prompt>``（-s/-C 等共享安全参数
        必须位于 resume 之前，3A parity 已验证）。initial 与 resume 共享同一
        ``_shared_exec_options()``。
        """
        command: list[str] = [self._codex_bin]
        command.extend(
            self._shared_exec_options(
                request=request,
                workspace_path=workspace_path,
                output_schema_path=output_schema_path,
            )
        )
        # Build the prompt as the positional argument
        prompt = self._build_prompt(request)
        if resume_session_id is not None:
            # A3: exact session resume——恢复 FIN 已持久化的精确 provider
            # session id，不用 --last 的"最近会话"语义（同 route 交错时
            # --last 会漂到另一会话，破坏 A→B→A 确定性）。
            command.extend(["resume", resume_session_id, prompt])
        else:
            command.append(prompt)
        return command

    def _shared_exec_options(
        self,
        *,
        request: AgentRunRequest,
        workspace_path: str | None = None,
        output_schema_path: str | None = None,
    ) -> list[str]:
        """Shared exec options：strict config、read-only sandbox、output schema。

        initial 与 resume 共用；不包含 ``--ephemeral``（Phase 3D 移除——
        首轮线程需持久化才能被后续 ``codex exec resume`` 恢复）。
        """
        resolved_workspace = workspace_path or self._workspace_path or "."
        options: list[str] = [
            "exec",
            "--json",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
        ]
        # Provider overrides must follow --ignore-user-config.  Codex otherwise
        # discards pre-exec overrides together with the user profile.
        options.extend(self._exec_options)

        # Keep the request-scoped reasoning setting first among raw config
        # overrides.  Existing callers inspect this position and, more
        # importantly, the value remains explicit rather than inherited from
        # a user profile that this runtime intentionally ignores.
        budget = request.budget
        if budget:
            effort = budget.get("reasoning_effort")
            if effort is not None:
                options.extend(["-c", f'model_reasoning_effort="{effort}"'])

        options.extend(
            [
                "-c",
                'approval_policy="never"',
                "-c",
                f'web_search="{_web_search_mode(request)}"',
                "-s",
                "read-only",
                "--skip-git-repo-check",
                "-C",
                resolved_workspace,
            ]
        )
        for feature in _DISABLED_RUNTIME_FEATURES:
            options.extend(["--disable", feature])

        if self._model:
            options.extend(["-m", self._model])

        if output_schema_path is not None:
            options.extend(["--output-schema", output_schema_path])

        return options

    def _ordered_runtime_context(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Render G cognition before supporting methods and evidence context."""
        ordered: dict[str, Any] = {}
        for key in (
            "prefetched_g",
            "g_context",
            "shared_knowledge",
            "methodology_projection",
        ):
            if key in context:
                ordered[key] = context[key]
        for key in sorted(context):
            if key not in ordered:
                ordered[key] = context[key]
        return ordered

    def _build_prompt(self, request: AgentRunRequest) -> str:
        """Build a bounded prompt from the FIN request.

        When ``use_case_ref`` is ``generic_research_answer``, the prompt uses
        a neutral generic preamble without FIN branding or registered-contract
        language — this keeps the baseline arm clean for controlled experiments.
        All other use cases preserve the existing FIN prompt behaviour.
        """
        prompt = ""
        if request.use_case_ref == "generic_research_answer":
            prompt = self._build_neutral_prompt(request)
        else:
            prompt = self._build_fin_prompt(request)

        # ── Prepend tool prohibition when max_tool_calls=0 ──
        budget = request.budget
        if budget and budget.get("max_tool_calls") == 0:
            prompt = (
                "IMPORTANT: Do not call any tools. You must respond directly "
                "without using web_search, command_execution, file_read, "
                "file_write, or any other tool.\n\n" + prompt
            )

        # ── Attention-budget sentinel (warning only, never a hard gate) ──
        if len(prompt) > _PROMPT_ATTENTION_WARN_CHARS:
            logging.getLogger(__name__).warning(
                "consultation prompt exceeds %d chars (%d): attention-budget risk",
                _PROMPT_ATTENTION_WARN_CHARS,
                len(prompt),
            )

        return prompt

    def _build_neutral_prompt(self, request: AgentRunRequest) -> str:
        """Neutral generic prompt — no FIN branding, no registered contracts."""
        parts: list[str] = [
            "You are a read-only research analysis assistant.",
        ]

        if request.question:
            parts.append(f"Question: {request.question}")

        parts.append(
            "\nOutput a single JSON object with research_product and display_product "
            "as top-level keys.  The research_product must include answer_summary, "
            "sections, source_level, mainlines, candidates, and shared_brain_references. "
            "The display_product must include display_intent, headline, short_answer, "
            "primary_sections, candidate_profiles, analysis_path_summary, "
            "confidence_boundary, next_actions, omitted_details_summary, and "
            "planner_source.  "
            "The display_product.confidence_boundary must be a JSON object with exactly: "
            '"advisory_only": true, "execution_allowed": false, '
            '"human_confirmation_required": true.  '
            "Do NOT include any execution fields (order, position, "
            "action, buy, sell, target_price, stop_loss)."
        )
        # Request strict top-level boundary booleans
        parts.append(
            "The top-level JSON must include advisory_only=true, execution_allowed=false, "
            "human_confirmation_required=true."
        )
        # Request concise output
        parts.append("Use concise arrays and strings throughout. Avoid verbose descriptions.")

        return "\n".join(parts)

    def _build_fin_prompt(self, request: AgentRunRequest) -> str:
        """FIN prompt: consultation_product is the only supported product.

        Retired research/display and candidate contracts have no production
        callers.  A non-consultation request (tests or contract drift) gets a
        neutral generic prompt instead of the retired research-schema prompt.
        """
        if not _is_consultation_request(request):
            return self._build_neutral_prompt(request)
        return self._build_advisory_prompt(request)

    def _render_confirmed_account_context(self, context_pack: Mapping[str, Any]) -> str:
        """Render server-bound ADVISORY_REAL account facts into the prompt.

        The Hermes -> FIN contract is one natural question; FIN owns context
        binding.  When the consultation prepared a confirmed portfolio option
        (READY or PARTIAL with usable positions), surface those facts directly
        so the Agent answers from them without depending on MCP tool
        registration or an Agent-chosen tool call.  Missing context renders
        nothing (fail-soft).
        """

        if not isinstance(context_pack, dict):
            return ""
        runtime = context_pack.get("runtime_context")
        options = runtime.get("context_options") if isinstance(runtime, dict) else None
        if not isinstance(options, list):
            return ""
        sections: list[str] = []
        for option in options:
            if not isinstance(option, dict) or option.get("owner") != "ADVISORY_REAL":
                continue
            if option.get("status") not in {"READY", "PARTIAL"}:
                continue
            context = option.get("context")
            if not isinstance(context, dict):
                continue
            positions = context.get("positions")
            if not isinstance(positions, list) or not positions:
                continue
            lines: list[str] = []
            boundary = option.get("account_facts_boundary_note")
            if isinstance(boundary, str) and boundary:
                lines.append(boundary)
            as_of = context.get("as_of")
            if isinstance(as_of, str) and as_of:
                lines.append(f"快照时间(as_of): {as_of}")
            for key, label in (
                ("net_assets", "总资产"),
                ("available_cash", "可用资金"),
                ("margin_debt", "融资负债"),
            ):
                value = context.get(key)
                if value is not None:
                    lines.append(f"{label}: {value}")
            for position in positions:
                if not isinstance(position, dict):
                    continue
                instrument = position.get("instrument")
                name = instrument.get("name") if isinstance(instrument, dict) else None
                ticker = instrument.get("ticker") if isinstance(instrument, dict) else None
                display = name if isinstance(name, str) and name else ticker
                if not isinstance(display, str):
                    continue
                if isinstance(ticker, str) and ticker and ticker != display:
                    display = f"{display}({ticker})"
                quantity = position.get("quantity")
                facts = [display]
                if quantity is not None:
                    facts.append(f"持有 {quantity} 股")
                for key, label in (
                    ("average_cost", "成本"),
                    ("reference_price", "参考价"),
                    ("market_value", "市值"),
                ):
                    value = position.get(key)
                    if value is not None:
                        facts.append(f"{label} {value}")
                lines.append("- " + "，".join(facts))
            sections.append(
                "--- CONFIRMED ACCOUNT FACTS (user-confirmed; authoritative only for account "
                "facts, not investment advice) ---\n"
                + "\n".join(lines)
                + "\n--- END CONFIRMED ACCOUNT FACTS ---"
            )
        return "\n\n".join(sections)

    def _build_advisory_prompt(self, request: AgentRunRequest) -> str:
        """Keep FIN advisory close to the same strong Agent used directly.

        The fixed contract text lives in the versioned asset
        ``consultation-advisory-prompt.v1.json`` beside this module; this
        method only assembles the per-request dynamic parts around it.
        """

        asset = _load_advisory_prompt_asset()
        parts = list(asset["preamble"])
        if request.process_guidance:
            parts.append(asset["process_guidance_prefix"] + request.process_guidance)
        raw_allowed = request.capability_scope.get("allowed_capabilities", ())
        allowed = (
            tuple(item for item in raw_allowed if isinstance(item, str))
            if isinstance(raw_allowed, (list, tuple))
            else ()
        )
        if allowed:
            capability_map = [
                f"- {name}: {asset['capability_descriptions'].get(name, 'bounded read-only FIN evidence')}"
                for name in allowed
            ]
            parts.append(
                "Optional FIN capability map (choose zero or more):\n"
                + "\n".join(capability_map)
            )
        if _web_search_mode(request) == "live":
            parts.append(asset["web_search_note"])
        confirmed_accounts = self._render_confirmed_account_context(request.context_pack)
        if confirmed_accounts:
            parts.append(confirmed_accounts)
        parts.append(asset["user_question_block"].replace("{{QUESTION}}", request.question))
        parts.extend(asset["trailer"])
        return "\n\n".join(parts)

    # ── Allowlisted numeric usage fields ─────────────────────────────────────
    _ALLOWLISTED_USAGE_FIELDS: set[str] = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }

    # ── JSONL parsing ───────────────────────────────────────────────────────

    def _parse_result(
        self,
        stdout: str,
        command: list[str],
        max_tool_calls: int | None = None,
        reasoning_effort: str | None = None,
        capability_trace: list[dict[str, Any]] | None = None,
        product_contracts: list[dict[str, Any]] | None = None,
        continuation_identity_hash: str | None = None,
        continuation_product_version: int | None = None,
    ) -> AgentRunResult:
        """Parse Codex JSONL stdout and extract the final agent_message product.

        Returns status="error" with bounded data-gap codes when:
        - No terminal turn.completed found in the output.
        - No agent_message found within the terminal turn.
        - The agent_message text is not valid JSON.
        - Any other parse error.
        - A non-allowlisted tool item is detected when max_tool_calls == 0.

        Phase 3D：成功时捕获 ``thread.started.thread_id``（UUIDv7）并附着
        bounded continuation envelope（backend/session_id/identity_hash/
        product_version）到 ``opaque_runtime_continuation``——仅私有测试可见，
        不进公共 payload/provenance。
        """
        enforce_tool_policy = max_tool_calls is not None and max_tool_calls == 0
        safe_capability_trace = [dict(item) for item in capability_trace or []]

        # A3-R1-F1: 唯一一致 ID——冲突事件（thread.started 与 session_meta
        # 不同）时 thread_id 保持 None，envelope 不附着，防止混淆会话。
        consistent_thread_id = _extract_consistent_thread_id(stdout)

        # ── Phase 1: collect agent_messages and detect terminal state ──
        agent_message_text: str | None = None
        terminal_seen: bool = False
        raw_usage: dict[str, Any] = {}
        thread_id = consistent_thread_id

        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = _json.loads(stripped)
            except _json.JSONDecodeError:
                continue

            if not isinstance(record, dict):
                continue

            # ── Tool policy enforcement: non-allowlisted item == violation ──
            if enforce_tool_policy and record.get("type") in ("item.completed", "item.started"):
                item = record.get("item", {})
                if isinstance(item, dict):
                    item_type = item.get("type", "")
                    if item_type not in _NON_TOOL_ITEM_TYPES:
                        return AgentRunResult(
                            status="error",
                            payload={},
                            data_gaps=["codex_tool_policy_violation"],
                            capability_trace=safe_capability_trace,
                            provenance={
                                "backend": "codex",
                                "model": self._reported_model or "unknown",
                            },
                        )

            if record.get("type") == "turn.completed":
                terminal_seen = True
                # Capture the usage record from the terminal turn
                usage_candidate = record.get("usage")
                if isinstance(usage_candidate, dict):
                    raw_usage = usage_candidate

            # Track the last agent_message (must also have terminal state)
            if record.get("type") == "item.completed":
                item = record.get("item", {})
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    agent_message_text = item.get("text", "")

        # ── No terminal turn.completed → fail closed ──
        if not terminal_seen:
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_missing_terminal"],
                capability_trace=safe_capability_trace,
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
            )

        # ── No agent_message found ──
        if agent_message_text is None:
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_missing_final_output"],
                capability_trace=safe_capability_trace,
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
            )

        # ── Parse the message text as JSON ──
        try:
            product = _json.loads(agent_message_text)
        except _json.JSONDecodeError:
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_malformed_product"],
                capability_trace=safe_capability_trace,
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
            )

        if not isinstance(product, dict):
            return AgentRunResult(
                status="error",
                payload={},
                data_gaps=["codex_malformed_product:not_a_dict"],
                capability_trace=safe_capability_trace,
                provenance={"backend": "codex", "model": self._reported_model or "unknown"},
            )

        # Usage belongs to this Agent invocation.
        sanitized_usage: dict[str, Any] = {}
        for key in self._ALLOWLISTED_USAGE_FIELDS:
            value = raw_usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                sanitized_usage[key] = value

        schema = _declarative_contract_schema(product_contracts or [])
        if schema is not None:
            _contract_id, _version, required, public, nested_schema = schema
            actual_fields = frozenset(product)
            if not required <= actual_fields or not actual_fields <= public:
                return AgentRunResult(
                    status="error",
                    payload={},
                    data_gaps=["codex_product_contract_violation"],
                    capability_trace=safe_capability_trace,
                    provenance={"backend": "codex", "model": self._reported_model or "unknown"},
                    resource_usage=sanitized_usage,
                )
            if nested_schema is not None and any(
                Draft202012Validator(nested_schema).iter_errors(product)
            ):
                return AgentRunResult(
                    status="error",
                    payload={},
                    data_gaps=["codex_product_contract_violation"],
                    capability_trace=safe_capability_trace,
                    provenance={"backend": "codex", "model": self._reported_model or "unknown"},
                    resource_usage=sanitized_usage,
                )

        # ── Sanitized success ──
        provenance: dict[str, Any] = {
            "backend": "codex",
            "model": self._reported_model or "unknown",
        }
        if self._runtime_route is not None:
            provenance["runtime_route"] = self._runtime_route
        # Record effective runtime policy on success so execute_run can prefer
        # observed provenance over arm-requested budget for run_facts.
        runtime_policy: dict[str, Any] = {}
        if reasoning_effort is not None:
            runtime_policy["reasoning_effort"] = reasoning_effort
        if max_tool_calls is not None:
            runtime_policy["max_tool_calls"] = max_tool_calls
        if runtime_policy:
            provenance["runtime_policy"] = runtime_policy

        # Phase 3D：bounded continuation envelope（backend/session_id/
        # identity_hash/product_version）。只有成功且有 thread.started 且调用方
        # 提供身份/版本时才附着；失败路径不带 handle（handle 随成功版本原子推进）。
        opaque_continuation: dict[str, Any] = {}
        if (
            thread_id is not None
            and isinstance(continuation_identity_hash, str)
            and _SHA256_PATTERN.fullmatch(continuation_identity_hash) is not None
            and isinstance(continuation_product_version, int)
            and not isinstance(continuation_product_version, bool)
            and continuation_product_version >= 1
        ):
            opaque_continuation = {
                "backend": "codex-cli",
                "session_id": thread_id,
                "identity_hash": continuation_identity_hash,
                "product_version": continuation_product_version,
            }

        return AgentRunResult(
            status="ok",
            payload=product,
            data_gaps=[],
            capability_trace=safe_capability_trace,
            provenance=provenance,
            opaque_runtime_continuation=opaque_continuation,
            resource_usage=sanitized_usage,
        )
