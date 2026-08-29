"""CDP Bridge 错误分类 — 把原始异常消息映射为结构化故障类型。

用于:
- `CdpBridgeClient` 在 `_call` / `start` 失败时返回 `CdpFailureKind`
- 保活/健康巡检写入 `scraper_health.json` 的 `failure_kind` 字段
- Hermes cron 脚本根据 `failure_kind` 决定重试/告警/人工介入
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CdpFailureKind(StrEnum):
    """CDP Bridge 故障分类（字符串枚举，方便 JSON 序列化）"""

    MODULE_MISSING = "module_missing"
    EXTENSION_DISCONNECTED = "extension_disconnected"
    NO_TABS = "no_tabs"
    TAB_DEBUGGER_CONFLICT = "tab_debugger_conflict"
    BRIDGE_EMPTY_RESPONSE = "bridge_empty_response"
    LOGIN_REQUIRED = "login_required"
    CONTENT_INSUFFICIENT = "content_insufficient"
    WINDOW_COVERAGE_INCOMPLETE = "window_coverage_incomplete"
    BRIDGE_START_FAILED = "bridge_start_failed"
    TARGET_INVALID = "target_invalid"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    CDP_LOCK_TIMEOUT = "cdp_lock_timeout"
    UNKNOWN = "unknown"

    def is_recoverable(self) -> bool:
        """返回该故障类型是否可通过重试自动恢复。

        - 可恢复: tab 冲突、空响应、扩展断开 — 重建 session/tab 后可能恢复
        - 不可恢复: 模块缺失、需登录、内容不足 — 需要人工介入
        """
        return self in _RECOVERABLE_KINDS


_RECOVERABLE_KINDS = frozenset(
    {
        CdpFailureKind.TAB_DEBUGGER_CONFLICT,
        CdpFailureKind.BRIDGE_EMPTY_RESPONSE,
        CdpFailureKind.EXTENSION_DISCONNECTED,
        CdpFailureKind.NO_TABS,
        CdpFailureKind.BRIDGE_START_FAILED,
        CdpFailureKind.CDP_LOCK_TIMEOUT,
    }
)


# ── 分类规则（按优先级从高到低） ──────────────────────────

_CLASSIFICATION_RULES: list[tuple[str, CdpFailureKind]] = [
    # module_missing: 子进程报 No module named cdp_bridge
    (r"No module named cdp_bridge", CdpFailureKind.MODULE_MISSING),
    # tab_debugger_conflict: Chrome 报 Another debugger already attached
    (r"Another debugger is already attached", CdpFailureKind.TAB_DEBUGGER_CONFLICT),
    # bridge_empty_response: CDP Bridge 超时无响应 — 映射为 bridge_empty_response 以保持 Hermes contract
    (r"timeout waiting for CDP Bridge response", CdpFailureKind.BRIDGE_EMPTY_RESPONSE),
    # cdp-bridge can return a successful MCP envelope containing its own
    # execution-timeout placeholder.  It is not page content and must enter the
    # same bounded session recovery path as an empty response.
    (
        r"No response data in \d+(?:\.\d+)?s "
        r"\((?:ACK received, script may still be running|no ACK, script may not have been delivered)\)$",
        CdpFailureKind.BRIDGE_EMPTY_RESPONSE,
    ),
    # bridge_empty_response: JSON 解析失败 — 空响应或非 JSON 响应
    (r"Expecting value: line 1 column 1", CdpFailureKind.BRIDGE_EMPTY_RESPONSE),
    # cdp_lock_timeout: CDP 中央锁超时 — 独立 canonical kind，区分于 extension_disconnected
    (r"cdp_lock_timeout", CdpFailureKind.CDP_LOCK_TIMEOUT),
    # extension_disconnected: MCP session 断开
    (r"Connection closed", CdpFailureKind.EXTENSION_DISCONNECTED),
    (r"McpError", CdpFailureKind.EXTENSION_DISCONNECTED),
    # no_tabs: 扩展连上了但没有 tab
    (r"no tabs", CdpFailureKind.NO_TABS),
    # extension_disconnected: 启动超时
    (r"timeout waiting for Chrome extension", CdpFailureKind.EXTENSION_DISCONNECTED),
    # login_required: 页面显示登录/扫码（中文消息 + OpenCLI validate_page_state）
    (r"登录页", CdpFailureKind.LOGIN_REQUIRED),
    (r"login_page", CdpFailureKind.LOGIN_REQUIRED),
    # content_insufficient: 页面内容太少
    (r"内容不足", CdpFailureKind.CONTENT_INSUFFICIENT),
    # target_invalid: 目标 ZSXQ group tab 缺失/无效（opencli 码 + probe 断言），
    # 不能误报为 bridge_start_failed
    (r"OPENCLI_NO_TAB", CdpFailureKind.TARGET_INVALID),
    (r"OPENCLI_TARGET_INVALID", CdpFailureKind.TARGET_INVALID),
    (r"CDP probe requires exactly one allowlisted ZSXQ group tab", CdpFailureKind.TARGET_INVALID),
    # transport_unavailable: opencli 命令失败/超时/路径不可用
    (r"OPENCLI_COMMAND_FAILED", CdpFailureKind.TRANSPORT_UNAVAILABLE),
    (r"OPENCLI_COMMAND_TIMEOUT", CdpFailureKind.TRANSPORT_UNAVAILABLE),
    (r"OPENCLI_PATH_INVALID", CdpFailureKind.TRANSPORT_UNAVAILABLE),
    (r"OPENCLI_DEADLINE_REACHED", CdpFailureKind.TRANSPORT_UNAVAILABLE),
    # bridge_start_failed: cdp-bridge 连接失败
    (r"cdp-bridge 连接失败", CdpFailureKind.BRIDGE_START_FAILED),
    (r"CDP Bridge 连接失败", CdpFailureKind.BRIDGE_START_FAILED),
]

_COMPILED_RULES: list[tuple[re.Pattern, CdpFailureKind]] = [
    (re.compile(pattern, re.IGNORECASE), kind) for pattern, kind in _CLASSIFICATION_RULES
]


# Bracket [kind] pattern: extracts the failure kind from wrapper messages like
# "CDP Bridge 连接失败 [extension_disconnected]: cdp_lock_timeout..."
_BRACKET_KIND_RE = re.compile(r"\[([a-z_]+)\]")


def classify_cdp_error(error_message: str) -> CdpFailureKind:
    """将 CDP 错误消息分类为 `CdpFailureKind`。

    按优先级从高到低匹配:
    1. Bracket [kind] annotation (最高优先级 — 显式标注覆盖所有 regex 规则)
    2. Regex rules by priority
    3. UNKNOWN fallback

    Args:
        error_message: 原始错误/异常消息字符串

    Returns:
        匹配的故障类型
    """
    if not error_message or not isinstance(error_message, str):
        return CdpFailureKind.UNKNOWN

    # Step 1: bracket [kind] syntax — highest priority.
    # This catches wrapper messages like
    #   "CDP Bridge 连接失败 [extension_disconnected]: cdp_lock_timeout..."
    # where the caller has already classified the error and annotated it.
    # BUT: [unknown] is NOT a valid bracket kind — it means the caller
    # couldn't classify. Fall through to regex rules instead.
    bracket_match = _BRACKET_KIND_RE.search(error_message)
    if bracket_match:
        candidate = bracket_match.group(1)
        if candidate and candidate != "unknown":
            try:
                return CdpFailureKind(candidate)
            except ValueError:
                pass  # not a valid CdpFailureKind — fall through to regex rules

    for pattern, kind in _COMPILED_RULES:
        if pattern.search(error_message):
            return kind

    return CdpFailureKind.UNKNOWN


# ── probe 控制失败码与 batch 结果容器（2026-08-19 自 legacy_cdp 迁移）────────
# 类型被 opencli 传输/回放/ledger 借用，随旧传输退役迁至本模块，语义不变。

_PROBE_CONTROL_ERROR_PREFIX = "fin_probe_control:"


class CdpProbeControlFailureCode(StrEnum):
    """Sanitized control-plane failures exposed by the read-only probe seam."""

    BRIDGE_START_FAILED = "bridge_start_failed"
    BRIDGE_IDENTITY_REQUIRED = "bridge_identity_required"
    BRIDGE_CONTROL_FAILED = "bridge_control_failed"
    EXTENSION_DISCONNECTED = "extension_disconnected"
    TAB_DEBUGGER_CONFLICT = "tab_debugger_conflict"
    TARGET_EXTENSION_COMMAND_FAILED = "target_extension_command_failed"
    TARGET_RESPONSE_TIMEOUT = "target_response_timeout"
    TARGETED_COLLECTION_FAILED = "targeted_collection_failed"


class CdpProbeControlFailureError(RuntimeError):
    """Typed, redacted CDP transport/tool failure safe for PageEvidence."""

    def __init__(self, code: CdpProbeControlFailureCode) -> None:
        self.code = code
        super().__init__(f"CDP probe control failure: {code.value}")


def _probe_control_code_from_message(message: str) -> CdpProbeControlFailureCode | None:
    for code in CdpProbeControlFailureCode:
        if f"{_PROBE_CONTROL_ERROR_PREFIX}{code.value}" in message:
            return code
    return None


@dataclass
class CdpBatchStepResult:
    """Result for one step inside CdpBridgeClient.batch_execute()."""

    index: int
    action: str
    name: str
    status: str
    result: Any = None
    error: str = ""
    failure_kind: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "name": self.name,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "failure_kind": self.failure_kind,
            "duration_ms": self.duration_ms,
        }


@dataclass
class CdpBatchResult:
    """Structured result for a browser batch execution."""

    status: str
    steps: list[CdpBatchStepResult] = field(default_factory=list)
    failed_step: dict[str, Any] | None = None
    fallback_used: bool = False
    cdp_trace: dict[str, Any] = field(default_factory=dict)

    def result_by_name(self, name: str) -> Any:
        for step in self.steps:
            if step.name == name and step.status == "ok":
                return step.result
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "failed_step": self.failed_step,
            "fallback_used": self.fallback_used,
            "cdp_trace": self.cdp_trace,
        }
