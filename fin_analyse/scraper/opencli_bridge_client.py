"""无状态 opencli CLI 传输：驱动 Windows Chrome 内已登录的 ZSXQ。

与 ``CdpBridgeClient``（WSL 常驻 playwright-mcp + WS 扩展桥接）不同，本客户端
每次浏览器操作都是一次有界、无状态的 PowerShell 子进程调用：客户端零常驻状态，
失败形态从 "ACK 后无响应" 变成可精确定位的 ``OPENCLI_*`` 错误码。

底层复用 ``eastmoney_http_transport`` 已验证的 opencli 资产（resolve/invoke/
WSL_INTEROP 环境/严格 JSON），scraper 的 DOM 解析与增量逻辑不动。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fin_analyse.common.bounded_process import run_bounded_command
from fin_analyse.market.qualification_sources.eastmoney_http_transport import (
    _POWERSHELL,
    _RESOLVE_OPENCLI,
    _opencli_environment,
    _strict_json_value,
)
from fin_analyse.scraper.cdp_diagnostics import (
    CdpBatchResult,
    CdpBatchStepResult,
    CdpProbeControlFailureCode,
    CdpProbeControlFailureError,
    _probe_control_code_from_message,
    classify_cdp_error,
)
from fin_analyse.scraper.cdp_probe_identity import PROBE_TOKEN_ENV, probe_token_is_valid

_ZSXQ_SESSION = "fin-zsxq-scraper-v1"
_EXPAND_DETAILS_SCRIPT = """(function() {
    const links = document.querySelectorAll('a, span, div');
    for (const el of links) {
        if (el.textContent.trim() === '查看详情') {
            el.click();
            return 'clicked';
        }
    }
    return 'done';
})()"""
_DEFAULT_COMMAND_TIMEOUT_SECONDS = 25.0
_CLOSE_RESERVE_SECONDS = 0.75
#: innerText 大 payload：文章正文可达 15K+ chars，group 页更大；live 验证后校准。
_MAX_EVAL_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_STDOUT_BYTES = _MAX_EVAL_OUTPUT_BYTES + 64 * 1024
_OPENCLI_PATH = re.compile(r"^[A-Za-z]:\\.+\\npm\\opencli\.ps1$")
_TARGET_ID = re.compile(r"^[0-9A-Fa-f]{32}$")
_TOP_LEVEL_RETURN_RE = re.compile(r"^\s*return\b", re.MULTILINE)

_ERROR_CODES = (
    "PATH_INVALID",
    "COMMAND_FAILED",
    "COMMAND_TIMEOUT",
    "OUTPUT_TOO_LARGE",
    "OUTPUT_INVALID",
    "DEADLINE_REACHED",
    "NO_TAB",
    "TARGET_INVALID",
    "TARGETED_COLLECTION_FAILED",
)


class OpenCliBridgeError(RuntimeError):
    """Stable failure; message is exactly ``OPENCLI_<CODE>``."""

    def __init__(self, code: str) -> None:
        if code not in _ERROR_CODES:
            raise ValueError(f"unknown OPENCLI error code: {code!r}")
        self.code = code
        super().__init__(f"OPENCLI_{code}")


def _error(code: str) -> OpenCliBridgeError:
    return OpenCliBridgeError(code)


def _windows_parent(windows_path: str) -> str:
    """Windows 路径（反斜杠分隔）的父目录；避免 Linux pathlib 把反斜杠当普通字符。"""
    sep_index = windows_path.rfind("\\")
    if sep_index <= 0:
        raise _error("PATH_INVALID")
    return windows_path[:sep_index]


def _browser_argv(opencli_path: str, *args: str) -> tuple[str, ...]:
    """node 直调 opencli main.js（绕开 PowerShell -File 的长参数破坏）。

    ``opencli_path`` 形如 ``C:\\Users\\<u>\\AppData\\Roaming\\npm\\opencli.ps1``：
    main.js 在同目录 ``node_modules\\@jackwener\\opencli\\dist\\src\\main.js``，
    node.exe 优先同目录（npm shim 的 fallback），否则 ``Program Files\\nodejs``。
    argv[0] 用 /mnt 路径（Linux exec 可见），脚本参数用 Windows 路径（node 解析）。
    """
    npm_dir = _windows_parent(opencli_path)
    main_js = f"{npm_dir}\\node_modules\\@jackwener\\opencli\\dist\\src\\main.js"
    node_exe = _resolve_node_executable(npm_dir)
    return (
        _wsl_path_of(node_exe),
        main_js,
        "browser",
        _ZSXQ_SESSION,
        *args,
    )


def _resolve_node_executable(npm_dir: str) -> str:
    """解析 Windows node.exe 的 Windows 路径（先 npm 目录，后 Program Files）。"""
    npm_node = f"{npm_dir}\\node.exe"
    if os.path.exists(_wsl_path_of(npm_node)):
        return npm_node
    program_files_node = "C:\\Program Files\\nodejs\\node.exe"
    if os.path.exists(_wsl_path_of(program_files_node)):
        return program_files_node
    raise _error("PATH_INVALID")


def _wsl_path_of(windows_path: str) -> str:
    """Windows 盘符路径 → WSL /mnt/c/... 路径（存在性探测与 exec 用）。"""
    drive = windows_path[0].lower()
    rest = windows_path[2:].replace("\\", "/")
    return f"/mnt/{drive}{rest}"


def _npm_dir_from_main_js(main_js: str) -> str:
    """从 main.js 路径反推 npm 目录（固定相对位校验）。"""
    parts = main_js.split("\\")
    if parts[-6:] != ["node_modules", "@jackwener", "opencli", "dist", "src", "main.js"]:
        raise _error("PATH_INVALID")
    return "\\".join(parts[:-6])


def _node_environment(opencli_path: str) -> dict[str, str]:
    """node 直调所需的 Windows 环境变量（从 opencli.ps1 路径推导）。"""
    npm_dir = _windows_parent(opencli_path)
    user_dir = _windows_parent(npm_dir)
    local_dir = f"{user_dir}\\AppData\\Local"
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "APPDATA": npm_dir,
        "LOCALAPPDATA": local_dir,
        "USERPROFILE": user_dir,
        "HOMEDRIVE": f"{user_dir[:2]}",
        "HOMEPATH": user_dir[2:].replace("\\", "/"),
        "TEMP": f"{local_dir}\\Temp",
        "TMP": f"{local_dir}\\Temp",
    }
    wsl_interop = _opencli_environment().get("WSL_INTEROP")
    if wsl_interop:
        environment["WSL_INTEROP"] = wsl_interop
    return environment


def _wrap_eval_script(script: str) -> str:
    """包成 async IIFE，仅在脚本含顶层 return 时（普通表达式脚本原样）。"""
    if _TOP_LEVEL_RETURN_RE.search(script):
        return f"(async () => {{\n{script}\n}})()"
    return script


def _remaining(deadline_at: datetime | None, now: datetime | None = None) -> float | None:
    """总 deadline 剩余秒数；未设 deadline 时返回 None。"""
    if deadline_at is None:
        return None
    now = now or datetime.now(UTC)
    return (deadline_at - now).total_seconds()


def _cap_wait(wait: float, deadline_at: datetime | None) -> float:
    """有界等待按剩余预算封顶（永不小于 0）。"""
    remaining = _remaining(deadline_at)
    if remaining is None:
        return wait
    return max(0.0, min(wait, remaining))


def _deadline_reached(deadline_at: datetime | None) -> bool:
    remaining = _remaining(deadline_at)
    return remaining is not None and remaining <= 0


def _production_command_runner(
    argv: tuple[str, ...],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """生产包装：run_bounded_command（prlimit 输出上限 + 进程组 kill）+ bytes 视图。

    node 直调的 argv 首项是 node.exe、次项是 opencli main.js；环境由
    ``_node_environment`` 提供（PowerShell 解析路径阶段仍走 WSL_INTEROP）。
    """
    if argv and argv[0] == _POWERSHELL:
        # resolve 阶段：PowerShell -Command 查询 opencli.ps1 路径（短参数无破坏风险）。
        completed = run_bounded_command(
            tuple(argv),
            cwd=Path("/"),
            env=_opencli_environment(),
            timeout=timeout,
            max_output_bytes=_MAX_STDOUT_BYTES,
        )
    else:
        # browser 阶段：node 直调，argv 长脚本不被 PowerShell 重引用破坏。
        npm_dir = _npm_dir_from_main_js(argv[1])
        opencli_path = f"{npm_dir}\\opencli.ps1"
        completed = run_bounded_command(
            tuple(argv),
            cwd=Path(_wsl_path_of(npm_dir)),
            env=_node_environment(opencli_path),
            timeout=timeout,
            max_output_bytes=_MAX_STDOUT_BYTES,
        )
    return subprocess.CompletedProcess(
        args=completed.args,
        returncode=completed.returncode,
        stdout=completed.stdout.encode("utf-8"),
        stderr=completed.stderr.encode("utf-8"),
    )


#: 探针证据采集脚本 — 内嵌固定 PageEvidence collector，
#: 返回 cdp_runtime._page_evidence_from_payload 要求的 13-key payload。
_PROBE_EVIDENCE_SCRIPT = r"""
(async () => {
  const MAX_SURFACE_TEXT_CHARS = 2048;
  const LOADING_SELECTOR = [
    '[class*="loading"]',
    '[class*="skeleton"]',
    '[aria-busy="true"]',
    '[role="progressbar"]',
  ].join(", ");
  const CHALLENGE_SELECTOR = [
    '[class*="captcha"]',
    '[class*="verify"]',
    'iframe[src*="captcha"]',
  ].join(", ");
  const LOGIN_SELECTOR = [
    '[class*="login"]',
    '[class*="sign-in"]',
    '[class*="signin"]',
    'form[action*="login"]',
  ].join(", ");
  const QR_SELECTOR = [
    'canvas[class*="qr"]',
    'img[class*="qr"]',
    '[class*="qrcode"]',
  ].join(", ");
  const STATUS_SELECTOR = [
    '[role="alert"]',
    '[role="status"]',
    '[class*="toast"]',
    '[class*="notice"]',
    '[class*="error"]',
  ].join(", ");
  const TIMELINE_ITEM_SELECTOR = [
    '[class*="topic"]',
    '[class*="talk"]',
    '[class*="post"]',
  ].join(", ");
  const TIMELINE_TIME_SELECTOR = 'time, [class*="date"], [class*="time"]';
  const TIMELINE_LIST_SELECTOR = "li";

  const isVisible = (node) => {
    if (!node || typeof node.getBoundingClientRect !== "function") return false;
    try {
      if (node.hidden || node.getAttribute?.("aria-hidden") === "true") return false;
      const style = getComputedStyle(node);
      if (
        style.display === "none"
        || ["hidden", "collapse"].includes(style.visibility)
        || Number(style.opacity) === 0
      ) return false;
      const rect = node.getBoundingClientRect();
      return Number(rect.width) > 0 && Number(rect.height) > 0;
    } catch (_error) {
      return false;
    }
  };

  const visibleNodes = (selector) => Array.from(document.querySelectorAll(selector))
    .filter(isVisible);

  const surfaceText = (nodes) => nodes
    .map((node) => String(node.innerText || ""))
    .join("\n")
    .slice(0, MAX_SURFACE_TEXT_CHARS);

  const groupTopicLinks = () => Array.from(document.querySelectorAll("a[href]"))
    .map((link) => {
      try {
        const candidate = new URL(String(link.getAttribute("href") || ""), location.origin);
        if (candidate.origin !== location.origin) return null;
        const match = candidate.pathname.match(
          /^\/group\/15522441811252\/topic\/(\d+)\/?$/
        );
        return match ? {link, topicId: match[1]} : null;
      } catch (_error) {
        return null;
      }
    })
    .filter((value) => value !== null);

  const sample = () => {
    const nativeTopicIds = new Set(
      Array.from(document.querySelectorAll("[data-topic-id]"))
        .map((node) => String(node.getAttribute("data-topic-id") || ""))
        .filter((value) => /^\d+$/.test(value))
    );
    const links = groupTopicLinks();
    const nativeMatch = links.some(({topicId}) => nativeTopicIds.has(topicId));
    const visibleTopicIds = new Set(
      links.filter(({link}) => isVisible(link)).map(({topicId}) => topicId)
    );
    const repeatedTimelineSurface = visibleNodes(TIMELINE_ITEM_SELECTOR).length >= 3
      && visibleNodes(TIMELINE_TIME_SELECTOR).length >= 3
      && visibleNodes(TIMELINE_LIST_SELECTOR).length >= 3;
    return {
      hasTimelineIdentity: nativeMatch
        || visibleTopicIds.size >= 3
        || repeatedTimelineSurface,
      loadingPresent: visibleNodes(LOADING_SELECTOR).length > 0,
    };
  };

  const first = sample();
  await new Promise((resolve) => setTimeout(resolve, 200));
  const second = sample();
  const challengeNodes = visibleNodes(CHALLENGE_SELECTOR);
  const loginNodes = visibleNodes(LOGIN_SELECTOR);
  const qrNodes = visibleNodes(QR_SELECTOR);
  const statusText = surfaceText(visibleNodes(STATUS_SELECTOR));
  const loginText = surfaceText([...loginNodes, ...qrNodes]);

  const challengePresent = challengeNodes.length > 0;
  const loginSurfacePresent = loginNodes.length > 0
    || (qrNodes.length > 0 && /登录网页版|请登录|登录后|log\s*in|sign\s*in/i.test(loginText));
  const qrScanSurfacePresent = qrNodes.length > 0
    || /扫码登录|微信扫码|二维码|scan\s*(the\s*)?qr/i.test(loginText);
  const rateLimitPresent = /请求过于频繁|访问过于频繁|稍后重试|too many requests|rate limit/i
    .test(statusText);

  let retryAfterSeconds = null;
  if (rateLimitPresent) {
    const seconds = statusText.match(/(?:等待|wait)\s*(\d{1,5})\s*(?:秒|seconds?)/i);
    const minutes = statusText.match(
      /(?:等待|wait|建议等待)?\s*(\d{1,4})\s*(?:分钟|minutes?)/i
    );
    if (seconds) retryAfterSeconds = Math.min(Number(seconds[1]), 86400);
    else if (minutes) retryAfterSeconds = Math.min(Number(minutes[1]) * 60, 86400);
  }

  return {
    schema_version: 1,
    observed_origin: location.origin,
    observed_url_path: location.pathname,
    url_query_present: Boolean(location.search),
    url_fragment_present: Boolean(location.hash),
    observed_native_identity: second.hasTimelineIdentity ? "zsxq-group-timeline" : null,
    document_ready_state: document.readyState,
    loading_surface_stable: !first.hasTimelineIdentity
      && !second.hasTimelineIdentity
      && first.loadingPresent
      && second.loadingPresent,
    challenge_present: challengePresent,
    login_surface_present: loginSurfacePresent,
    qr_scan_surface_present: qrScanSurfacePresent,
    rate_limit_present: rateLimitPresent,
    retry_after_seconds: retryAfterSeconds,
  };
})()
""".strip()


class OpenCliBridgeClient:
    """无状态 opencli 客户端，实现 :class:`CdpBridgeClient` 的爬虫所需接口。

    ``start()`` 解析 opencli.ps1 路径并探测 session 存活；此后每次操作一次有界
    PowerShell 子进程。``command_runner``/``monotonic`` 为测试注入 seam。
    """

    def __init__(
        self,
        startup_wait: float = 5.0,
        max_retries: int = 2,
        purpose: str | None = None,
        lease_store: object | None = None,  # 兼容参数：session 名即租约
        *,
        deadline_at: datetime | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._startup_wait = float(startup_wait)
        self._max_retries = int(max_retries)
        self._purpose = purpose
        #: 兼容参数：opencli 以 session 名作为租约，不消费 lease_store。
        self._lease_store = lease_store
        self._deadline_at = deadline_at
        self._command_runner = command_runner or _production_command_runner
        self._monotonic = monotonic or time.monotonic
        self._opencli_path: str | None = None
        self._tab_id: str | None = None
        self._error: str | None = None
        self._cdp_last_tool = "opencli:none"
        self._cdp_last_action = ""
        self._cdp_trace: dict[str, Any] = {}
        self._probe_id_map: dict[str, int] = {}
        self._probe_next_id = 1

    # ── 底层原语 ──────────────────────────────────────────────

    def _remaining_budget(self, *, reserve_close: bool) -> float:
        remaining = _remaining(self._deadline_at)
        if remaining is None:
            return _DEFAULT_COMMAND_TIMEOUT_SECONDS
        if reserve_close:
            remaining -= _CLOSE_RESERVE_SECONDS
        return max(0.0, remaining)

    def _invoke(self, argv: Sequence[str], *, reserve_close: bool = False) -> bytes:
        budget = self._remaining_budget(reserve_close=reserve_close)
        if budget <= 0:
            raise _error("DEADLINE_REACHED")
        try:
            completed = self._command_runner(tuple(argv), timeout=budget)
        except subprocess.TimeoutExpired as exc:
            raise _error("COMMAND_TIMEOUT") from exc
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            if isinstance(exc, RuntimeError) and "output_limit" in str(exc):
                raise _error("OUTPUT_TOO_LARGE") from exc
            raise _error("COMMAND_FAILED") from exc
        if (
            isinstance(completed.returncode, bool)
            or not isinstance(completed.returncode, int)
            or not isinstance(completed.stdout, bytes)
            or not isinstance(completed.stderr, bytes)
        ):
            raise _error("OUTPUT_INVALID")
        if len(completed.stdout) > _MAX_STDOUT_BYTES or len(completed.stderr) > _MAX_STDOUT_BYTES:
            raise _error("OUTPUT_TOO_LARGE")
        if completed.returncode != 0:
            raise _error("COMMAND_FAILED")
        if _deadline_reached(self._deadline_at):
            raise _error("DEADLINE_REACHED")
        return completed.stdout

    def _invoke_json(
        self, argv: Sequence[str], *, reserve_close: bool = False
    ) -> dict[str, object]:
        stdout = self._invoke(argv, reserve_close=reserve_close)
        value = _strict_json_value(stdout)
        if not isinstance(value, dict):
            raise _error("OUTPUT_INVALID")
        return value

    def _resolve_opencli(self) -> str:
        payload = self._invoke_json(
            (
                _POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _RESOLVE_OPENCLI,
            ),
            reserve_close=True,
        )
        if set(payload) != {"path"}:
            raise _error("PATH_INVALID")
        path = payload["path"]
        if not isinstance(path, str) or "\x00" in path or _OPENCLI_PATH.fullmatch(path) is None:
            raise _error("PATH_INVALID")
        return path

    def _require_path(self) -> str:
        if self._opencli_path is None:
            raise _error("COMMAND_FAILED")
        return self._opencli_path

    def _require_tab(self) -> str:
        if self._tab_id is None or _TARGET_ID.fullmatch(self._tab_id) is None:
            raise _error("NO_TAB")
        return self._tab_id

    def _set_tool(self, tool: str) -> None:
        self._cdp_last_tool = f"opencli:{tool}"

    # ── 生命周期 ──────────────────────────────────────────────

    def start(self) -> bool:
        """解析 opencli 路径、探测 session 存活，并复用已有 ZSXQ group tab。

        失败置 ``_error`` 并返回 False。复用已打开的 group tab（用户保持
        ZSXQ 打开的工作流），避免每次 run 都新开 tab。
        """
        self._error = None
        try:
            if _deadline_reached(self._deadline_at):
                raise _error("DEADLINE_REACHED")
            if self._purpose == "probe" and not probe_token_is_valid(
                os.environ.get(PROBE_TOKEN_ENV)
            ):
                self._error = (
                    f"fin_probe_control:{CdpProbeControlFailureCode.BRIDGE_IDENTITY_REQUIRED.value}"
                )
                return False
            self._opencli_path = self._resolve_opencli()
            self._set_tool("tab_list")
            tabs = self.get_tabs()  # session 存活探测（tab 为 0 也视为 bridge 可用）
            for tab in tabs:
                if "wx.zsxq.com/group/" in str(tab.get("url") or ""):
                    self._tab_id = str(tab["id"])
                    break
            return True
        except OpenCliBridgeError as exc:
            self._error = exc.args[0] if exc.args else "OPENCLI_COMMAND_FAILED"
            return False

    def close(self) -> None:
        """幂等 no-op：无长连接可关；清 probe 归一化映射。"""
        self._probe_id_map.clear()
        self._probe_next_id = 1

    # ── 浏览器操作 ────────────────────────────────────────────

    def navigate(self, url: str, wait: float = 3.0, cache_bust: bool = False) -> None:
        """导航 session 当前 tab；无 tab 时先 ``tab new``。"""
        target = url
        if cache_bust:
            separator = "&" if "?" in url else "?"
            target = f"{url}{separator}_fin_ts={int(self._monotonic() * 1000)}"
        self._set_tool("navigate")
        if self._tab_id is None:
            created = self._invoke_json(
                _browser_argv(self._require_path(), "tab", "new", target),
                reserve_close=True,
            )
            self._tab_id = _target_from_created(created)
            # tab new 会复用 session 内同 URL tab；显式 select 固定默认 tab。
            self._invoke_json(
                _browser_argv(self._require_path(), "tab", "select", self._tab_id),
                reserve_close=True,
            )
        else:
            # 始终显式 --tab，避免 session 活动 tab 漂移导致导航错页面。
            self._invoke_json(
                _browser_argv(self._require_path(), "open", target, "--tab", self._tab_id),
                reserve_close=True,
            )
        # opencli ``open`` 内置 wait 2s；补足调用方等待预算。
        time.sleep(_cap_wait(max(0.0, wait - 2.0), self._deadline_at))

    def js(self, script: str) -> str:
        """执行 JS 并返回 opencli stdout：string 结果原样、对象 JSON 序列化。"""
        code = _wrap_eval_script(script)
        stdout = self._invoke(
            _browser_argv(self._require_path(), "eval", code, "--tab", self._require_tab()),
        )
        return _decode_eval_stdout(stdout)

    def js_json(self, script: str) -> dict[str, Any]:
        raw = self.js(script)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {"_raw": raw}
        if not isinstance(value, dict):
            return {"_raw": raw}
        return value

    def scroll_by(self, px: int = 4000, wait: float = 1.0) -> None:
        """向下滚动；opencli ``scroll down --amount <px>``。"""
        self._set_tool("scroll")
        self._invoke(
            _browser_argv(self._require_path(), "scroll", "down", "--amount", str(int(px))),
        )
        time.sleep(_cap_wait(wait, self._deadline_at))

    def validate_page_state(self, page_text: str | None = None) -> tuple[bool, str]:
        """验证当前 Tab 页面内容是否可用于抓取（规则与 CdpBridgeClient 一致）。"""
        try:
            text = (
                page_text
                if page_text is not None
                else self.js("document.body.innerText.substring(0, 5000)")
            )
        except Exception as exc:  # noqa: BLE001 — 与 CdpBridgeClient 语义一致
            return False, f"js_error: {exc}"

        if not text or len(text) <= 500:
            return False, f"content_empty: {len(text) if text else 0} chars"

        if "登录" in text and "扫码" in text:
            return False, "login_page"

        if not re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", text) and len(text) < 2000:
            return False, f"no_dates_and_insufficient: {len(text)} chars"

        return True, "ok"

    def force_navigate_group(self, url: str, wait: float = 5.0) -> bool:
        """强制导航（cache-bust）当前 tab + 等待 SPA 水合 + 页面状态校验。"""
        try:
            self.set_cdp_action("force_navigate_group")
            self.navigate(url, wait=wait, cache_bust=True)
            time.sleep(_cap_wait(2.0, self._deadline_at))
            return self.validate_page_state()[0]
        except OpenCliBridgeError:
            return False

    def heal_tab_via_new_window(self, url: str, wait: float = 5.0) -> int | None:
        """新开 tab 并切换 session 默认 tab；返回 probe 归一化 tab id 或 None。"""
        try:
            self._set_tool("tab_new")
            created = self._invoke_json(
                _browser_argv(self._require_path(), "tab", "new", url),
                reserve_close=True,
            )
            new_tab = _target_from_created(created)
            self._tab_id = new_tab
            self._invoke_json(
                _browser_argv(self._require_path(), "tab", "select", new_tab),
                reserve_close=True,
            )
            time.sleep(_cap_wait(wait, self._deadline_at))
            return self._probe_tab_id(new_tab)
        except OpenCliBridgeError:
            return None

    def get_tabs(self) -> list[dict[str, Any]]:
        """``tab list`` 归一化为 ``{id, url, title}`` 列表（容错）。"""
        self._set_tool("tab_list")
        stdout = self._invoke(_browser_argv(self._require_path(), "tab", "list"))
        try:
            raw = json.loads(stdout.decode("utf-8"))
        except (TypeError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        normalized: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            tab_id = _entry_tab_id(entry)
            if tab_id is None:
                continue
            normalized.append(
                {
                    "id": tab_id,
                    "url": str(entry.get("url") or ""),
                    "title": str(entry.get("title") or ""),
                }
            )
        return normalized

    # ── 诊断 trace ────────────────────────────────────────────

    def get_cdp_trace(self) -> dict:
        """恢复诊断 trace（initial/final tab id、last_tool、last_action 等）。"""
        trace = dict(self._cdp_trace)
        trace["last_tool"] = self._cdp_last_tool
        trace["last_action"] = self._cdp_last_action
        return trace

    def set_cdp_action(self, action: str) -> None:
        self._cdp_last_action = action

    # ── 批量步骤 ──────────────────────────────────────────────

    def batch_execute(self, steps: list[dict]) -> CdpBatchResult:
        """逐 step 执行；required=False 失败继续；返回 ok/partial/failed。"""
        results: list[CdpBatchStepResult] = []
        failed_step: dict[str, Any] | None = None
        saw_optional_failure = False
        initial_tab_id = self._tab_id

        for index, step in enumerate(steps):
            action = str(step.get("action", ""))
            name = str(step.get("name") or action or f"step_{index}")
            required = bool(step.get("required", True))
            started = self._monotonic()
            self.set_cdp_action(f"batch:{name}")

            try:
                value = self._execute_batch_step(step)
                duration_ms = int((self._monotonic() - started) * 1000)
                results.append(
                    CdpBatchStepResult(
                        index=index,
                        action=action,
                        name=name,
                        status="ok",
                        result=value,
                        duration_ms=duration_ms,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — 与 CdpBridgeClient 语义一致
                duration_ms = int((self._monotonic() - started) * 1000)
                kind = classify_cdp_error(str(exc))
                step_result = CdpBatchStepResult(
                    index=index,
                    action=action,
                    name=name,
                    status="failed",
                    error=str(exc),
                    failure_kind=str(kind),
                    duration_ms=duration_ms,
                )
                results.append(step_result)
                failed_step = step_result.to_dict()
                if required:
                    return CdpBatchResult(
                        status="failed",
                        steps=results,
                        failed_step=failed_step,
                        cdp_trace=self._batch_trace(initial_tab_id),
                    )
                saw_optional_failure = True

        return CdpBatchResult(
            status="partial" if saw_optional_failure else "ok",
            steps=results,
            failed_step=failed_step,
            cdp_trace=self._batch_trace(initial_tab_id),
        )

    def _batch_trace(self, initial_tab_id: str | None) -> dict[str, Any]:
        trace = dict(self._cdp_trace)
        trace["initial_tab_id"] = initial_tab_id
        trace["final_tab_id"] = self._tab_id
        trace["last_tool"] = self._cdp_last_tool
        trace["last_action"] = self._cdp_last_action
        return trace

    def _execute_batch_step(self, step: dict) -> Any:
        action = str(step.get("action", ""))
        if action == "navigate":
            url = str(step["url"])
            wait = float(step.get("wait", 3.0))
            cache_bust = bool(step.get("cache_bust", False))
            return self.navigate(url, wait=wait, cache_bust=cache_bust)
        if action == "wait":
            wait = float(step.get("wait", step.get("seconds", 0.0)))
            time.sleep(_cap_wait(wait, self._deadline_at))
            return {"waited_seconds": wait}
        if action == "js":
            return self.js(str(step["script"]))
        if action == "full_text":
            return self.js("document.body.innerText")
        if action == "expand_details":
            max_clicks = int(step.get("max_clicks", 20))
            clicked = 0
            for _ in range(max_clicks):
                result = self.js(_EXPAND_DETAILS_SCRIPT)
                if "done" in str(result):
                    break
                clicked += 1
                time.sleep(0.5)
            return {"clicked": clicked}
        if action == "scroll_by":
            px = int(step.get("px", 4000))
            wait = float(step.get("wait", 1.0))
            repeat = int(step.get("repeat", 1))
            for _ in range(max(1, repeat)):
                self.scroll_by(px=px, wait=wait)
            return {"scrolled_px": px, "repeat": max(1, repeat)}
        raise ValueError(f"unknown batch action: {action}")

    # ── probe 支持 ────────────────────────────────────────────

    def _probe_tab_id(self, page_id: str) -> int:
        if page_id not in self._probe_id_map:
            self._probe_id_map[page_id] = self._probe_next_id
            self._probe_next_id += 1
        return self._probe_id_map[page_id]

    def _page_id_for_tab(self, tab_id: int | str) -> str:
        normalized = str(tab_id)
        if _TARGET_ID.fullmatch(normalized) is not None:
            return normalized
        for page_id, probe_id in self._probe_id_map.items():
            if probe_id == int(normalized):
                return page_id
        raise _error("TARGET_INVALID")

    def get_browser_tab_inventory(self) -> list[dict[str, Any]]:
        """probe 只读 inventory：十进制 tabId（兼容 ``_normalize_probe_tab_id``）。"""
        inventory: list[dict[str, Any]] = []
        for tab in self.get_tabs():
            page_id = str(tab["id"])
            if _TARGET_ID.fullmatch(page_id) is None:
                continue
            inventory.append(
                {
                    "tabId": self._probe_tab_id(page_id),
                    "windowId": 1,
                    "url": tab["url"],
                    "active": False,
                }
            )
        return inventory

    def collect_page_evidence_on_tab(self, tab_id: int | str) -> dict[str, Any]:
        """在指定 tab 上 eval 13-key 探针证据脚本；输出不合格抛控制失败。"""
        page_id = self._page_id_for_tab(tab_id)
        self._set_tool("probe_evidence")
        try:
            raw = self.js_on_tab(page_id, _PROBE_EVIDENCE_SCRIPT)
        except OpenCliBridgeError as exc:
            raise CdpProbeControlFailureError(
                CdpProbeControlFailureCode.TARGETED_COLLECTION_FAILED
            ) from exc
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise CdpProbeControlFailureError(
                CdpProbeControlFailureCode.TARGETED_COLLECTION_FAILED
            ) from exc
        if not isinstance(payload, dict):
            raise CdpProbeControlFailureError(CdpProbeControlFailureCode.TARGETED_COLLECTION_FAILED)
        return payload

    def js_on_tab(self, tab_id: str, script: str) -> str:
        """在指定 tab 上执行 JS（probe 专用，不做 tab 状态切换）。"""
        code = _wrap_eval_script(script)
        stdout = self._invoke(
            _browser_argv(self._require_path(), "eval", code, "--tab", tab_id),
        )
        return _decode_eval_stdout(stdout)

    def probe_control_failure_code(self) -> CdpProbeControlFailureCode | None:
        """probe 生命周期内 typed 启动失败码；非 probe 或正常时返回 None。"""
        if self._purpose != "probe" or not self._error:
            return None
        marker_code = _probe_control_code_from_message(self._error)
        if marker_code is not None:
            return marker_code
        if "TARGETED_COLLECTION_FAILED" in self._error:
            return CdpProbeControlFailureCode.TARGETED_COLLECTION_FAILED
        if "COMMAND_TIMEOUT" in self._error:
            return CdpProbeControlFailureCode.EXTENSION_DISCONNECTED
        if any(marker in self._error for marker in ("COMMAND_FAILED", "PATH_INVALID", "NO_TAB")):
            return CdpProbeControlFailureCode.BRIDGE_START_FAILED
        return None


def _target_from_created(created: dict[str, object]) -> str:
    page = created.get("page")
    if not isinstance(page, str) or _TARGET_ID.fullmatch(page) is None:
        raise _error("TARGET_INVALID")
    return page


def _entry_tab_id(entry: dict[str, Any]) -> str | None:
    for key in ("id", "page", "targetId", "target"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int):
            return str(value)
    if "index" in entry and isinstance(entry["index"], int):
        return str(entry["index"])
    return None


def _decode_eval_stdout(stdout: bytes) -> str:
    """eval stdout → str：string 结果原样（去尾换行）、对象 JSON 序列化。"""
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return stdout.decode("utf-8", errors="replace")
    stripped = text.rstrip("\n")
    try:
        value = json.loads(stripped)
    except (TypeError, ValueError):
        return stripped
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return stripped
