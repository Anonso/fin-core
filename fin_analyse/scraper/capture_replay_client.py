"""CaptureReplayClient — 以 capture artifact 为传输的 CdpBridgeClient 兼容实现。

CdpBridgeScraper 的解析/去重/过滤/coverage 判定/KB 写入 100% 复用；本客户端只替换
浏览器传输面：js() 按 script sha256 命中 artifact 录制输出，navigate/scroll 无操作，
未录制脚本 fail-closed（bracket [transport_unavailable]，由 classify_cdp_error 分类）。
凭证类 eval（document.cookie）恒返回空串，从不跨 artifact。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .capture_artifact import CaptureArtifact, CapturePage, sha256_hex
from .cdp_diagnostics import CdpBatchResult, CdpBatchStepResult, classify_cdp_error
from .cdp_scraper import _FULL_TEXT_SCRIPT
from .config import GROUP_URL

_FULL_TEXT_SUBSTRING_SCRIPT = "document.body.innerText.substring(0, 5000)"
_COOKIES_SCRIPT = "JSON.stringify(document.cookie)"
_UNRECORDED = "capture artifact has no recorded output for eval script"
_UNRECORDED_NAV = "capture artifact has no recorded page for navigation"
#: topic cursor 脚本内嵌 URL 的 end_time 参数（urlencode 形式；首页无此参数）。
_END_TIME_RE = re.compile(r"end_time=([^&\"']+)")


def _normalize_url(url: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    scheme, netloc, path, query, fragment = urlsplit(url)
    if not query:
        return url
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "_fin_ts"]
    return urlunsplit((scheme, netloc, path, urlencode(kept), fragment))


def _unrecorded(script: str) -> RuntimeError:
    return RuntimeError(f"[transport_unavailable] {_UNRECORDED} {sha256_hex(script)[:8]}")


class CaptureReplayClient:
    """无状态 artifact 回放客户端；构造即绑定已校验的 CaptureArtifact。

    ``startup_wait``/``max_retries``/``purpose``/``lease_store``/``command_runner``/
    ``monotonic`` 为与 OpenCliBridgeClient 同位的兼容参数（本客户端不使用）。
    """

    def __init__(
        self,
        artifact: CaptureArtifact,
        startup_wait: float = 5.0,
        max_retries: int = 2,
        purpose: str | None = None,
        lease_store: object | None = None,
        *,
        deadline_at: datetime | None = None,
        command_runner: Any = None,
        monotonic: Any = None,
    ) -> None:
        self._artifact = artifact
        self._deadline_at = deadline_at
        self._tab_id: str | None = None
        self._error: str | None = None
        self._current_page: CapturePage | None = None
        self._cdp_last_tool = "capture-replay:none"
        self._cdp_last_action = ""

    # ── 生命周期 ──────────────────────────────────────────────

    def start(self) -> bool:
        """Artifact 已在上游校验；直接绑定 group 页与目标 tab。"""
        self._error = None
        self._tab_id = self._artifact.target_tab_id
        self._current_page = self._artifact.group_page()
        if self._current_page is None:
            self._error = "[transport_unavailable] group page missing from artifact"
            return False
        self._set_tool("start")
        return True

    def close(self) -> None:
        """幂等 no-op：无长连接可关。"""
        self._current_page = None

    def get_tabs(self) -> list[dict[str, Any]]:
        return [
            {
                "id": self._artifact.target_tab_id,
                "url": self._artifact.target_url,
                "title": self._artifact.target_title,
            }
        ]

    # ── 浏览器操作（回放）─────────────────────────────────────

    def navigate(self, url: str, wait: float = 3.0, cache_bust: bool = False) -> None:
        """按归一化 URL 匹配 artifact 页记录；未记录页 fail-closed。"""
        self._set_tool("navigate")
        normalized = _normalize_url(url)
        target = _normalize_url(GROUP_URL)
        for page in self._artifact.pages:
            if _normalize_url(page.url) == normalized:
                self._current_page = page
                return
        if normalized == target:
            self._current_page = self._artifact.group_page()
            return
        raise RuntimeError(f"[transport_unavailable] {_UNRECORDED_NAV}: {url[:120]}")

    def js(self, script: str) -> str:
        """按精确特殊串/script sha256 命中录制输出；未录制 fail-closed。"""
        self._set_tool("js")
        page = self._current_page
        if page is None:
            raise RuntimeError(f"[transport_unavailable] {_UNRECORDED}: no current page")
        if script == _COOKIES_SCRIPT:
            # 凭证从不跨 artifact：恒空（与 capture 侧永不录制对应）。
            return ""
        if script == _FULL_TEXT_SCRIPT:
            recorded = page.output_for(_FULL_TEXT_SCRIPT)
            if recorded is not None:
                return recorded
        if script == _FULL_TEXT_SUBSTRING_SCRIPT:
            recorded = page.output_for(_FULL_TEXT_SCRIPT)
            if recorded is not None:
                return recorded[:5000]
        recorded = page.output_for(script)
        if recorded is not None:
            return recorded
        cursor_output = self._cursor_output_for(script)
        if cursor_output is not None:
            return cursor_output
        raise _unrecorded(script)

    def _cursor_output_for(self, script: str) -> str | None:
        """Topic cursor 页按 end_time（URL 键）命中录制输出；首页键为空串。"""
        from urllib.parse import unquote

        match = _END_TIME_RE.search(script)
        end_time = unquote(match.group(1)) if match is not None else ""
        for cursor_page in self._artifact.cursor_pages:
            if cursor_page.end_time == end_time:
                return cursor_page.output
        return None

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
        """回放无操作：录制状态已反映 capture 时的滚动终态。"""
        self._set_tool("scroll")

    def validate_page_state(self, page_text: str | None = None) -> tuple[bool, str]:
        """按回放页全文执行与 OpenCliBridgeClient 一致的校验规则。"""
        text = page_text if page_text is not None else self.js(_FULL_TEXT_SUBSTRING_SCRIPT)
        if not text or len(text) <= 500:
            return False, f"content_empty: {len(text) if text else 0} chars"
        if "登录" in text and "扫码" in text:
            return False, "login_page"
        if not re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", text) and len(text) < 2000:
            return False, f"no_dates_and_insufficient: {len(text)} chars"
        return True, "ok"

    def force_navigate_group(self, url: str, wait: float = 5.0) -> bool:
        """回放式强制导航（cache-bust 归一化后与记录匹配）。"""
        try:
            self.set_cdp_action("force_navigate_group")
            self.navigate(url, wait=wait, cache_bust=True)
            return self.validate_page_state()[0]
        except RuntimeError:
            return False

    def heal_tab_via_new_window(self, url: str, wait: float = 5.0) -> int | None:
        """回放无浏览器可自愈：返回 None（fail-closed，不做虚假自愈）。"""
        self._set_tool("heal")
        return None

    # ── 诊断 trace ────────────────────────────────────────────

    def get_cdp_trace(self) -> dict:
        return {"last_tool": self._cdp_last_tool, "last_action": self._cdp_last_action}

    def set_cdp_action(self, action: str) -> None:
        self._cdp_last_action = action

    def _set_tool(self, tool: str) -> None:
        self._cdp_last_tool = f"capture-replay:{tool}"

    # ── 批量步骤 ──────────────────────────────────────────────

    def batch_execute(self, steps: list[dict]) -> CdpBatchResult:
        """逐 step 回放；required=False 失败继续；返回 ok/partial/failed。"""
        results: list[CdpBatchStepResult] = []
        failed_step: dict[str, Any] | None = None
        saw_optional_failure = False
        initial_tab_id = self._tab_id

        for index, step in enumerate(steps):
            action = str(step.get("action", ""))
            name = str(step.get("name") or action or f"step_{index}")
            required = bool(step.get("required", True))
            self.set_cdp_action(f"batch:{name}")
            try:
                value = self._execute_batch_step(step)
                results.append(
                    CdpBatchStepResult(
                        index=index,
                        action=action,
                        name=name,
                        status="ok",
                        result=value,
                        duration_ms=0,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — 与 OpenCliBridgeClient 语义一致
                kind = classify_cdp_error(str(exc))
                step_result = CdpBatchStepResult(
                    index=index,
                    action=action,
                    name=name,
                    status="failed",
                    error=str(exc),
                    failure_kind=str(kind),
                    duration_ms=0,
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
        return {
            "initial_tab_id": initial_tab_id,
            "final_tab_id": self._tab_id,
            "last_tool": self._cdp_last_tool,
            "last_action": self._cdp_last_action,
        }

    def _execute_batch_step(self, step: dict) -> Any:
        action = str(step.get("action", ""))
        if action == "navigate":
            return self.navigate(str(step["url"]), wait=float(step.get("wait", 3.0)))
        if action == "wait":
            return {"waited_seconds": float(step.get("wait", step.get("seconds", 0.0)))}
        if action == "js":
            return self.js(str(step["script"]))
        if action == "full_text":
            return self.js(_FULL_TEXT_SCRIPT)
        if action == "expand_details":
            # replay 时 artifact 已录制展开后状态，无需操作
            return {"clicked": 0}
        if action == "scroll_by":
            px = int(step.get("px", 4000))
            repeat = int(step.get("repeat", 1))
            for _ in range(max(1, repeat)):
                self.scroll_by(px=px, wait=float(step.get("wait", 1.0)))
            return {"scrolled_px": px, "repeat": max(1, repeat)}
        raise ValueError(f"unknown batch action: {action}")
