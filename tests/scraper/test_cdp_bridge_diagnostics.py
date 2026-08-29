"""Tests for CDP bridge error classification."""

from fin_analyse.scraper.cdp_diagnostics import CdpFailureKind, classify_cdp_error


class TestClassifyKnownCdpFailures:
    """Plan: Task 1 Step 1 — 覆盖已知 CDP 故障模式"""

    def test_classifies_module_missing(self):
        """No module named cdp_bridge → MODULE_MISSING"""
        kind = classify_cdp_error("No module named cdp_bridge")
        assert kind == CdpFailureKind.MODULE_MISSING

    def test_classifies_tab_debugger_conflict(self):
        """Another debugger is already attached → TAB_DEBUGGER_CONFLICT"""
        kind = classify_cdp_error(
            "CDP fallback failed: Another debugger is already attached to the tab with id: 735041063."
        )
        assert kind == CdpFailureKind.TAB_DEBUGGER_CONFLICT

    def test_classifies_bridge_empty_response(self):
        """Expecting value: line 1 column 1 → BRIDGE_EMPTY_RESPONSE"""
        kind = classify_cdp_error("Expecting value: line 1 column 1 (char 0)")
        assert kind == CdpFailureKind.BRIDGE_EMPTY_RESPONSE

    def test_classifies_connection_closed_as_extension_disconnected(self):
        """McpError: Connection closed → EXTENSION_DISCONNECTED"""
        kind = classify_cdp_error("Connection closed")
        assert kind == CdpFailureKind.EXTENSION_DISCONNECTED

    def test_classifies_mcperror_connection_closed(self):
        """McpError wrapping Connection closed → EXTENSION_DISCONNECTED"""
        kind = classify_cdp_error("McpError: Connection closed")
        assert kind == CdpFailureKind.EXTENSION_DISCONNECTED

    def test_classifies_no_tabs(self):
        """No tabs available → NO_TABS"""
        kind = classify_cdp_error("no tabs")
        assert kind == CdpFailureKind.NO_TABS

    def test_classifies_timeout_as_extension_disconnected(self):
        """Startup timeout → EXTENSION_DISCONNECTED"""
        kind = classify_cdp_error("timeout waiting for Chrome extension")
        assert kind == CdpFailureKind.EXTENSION_DISCONNECTED

    def test_classifies_login_required(self):
        """登录页 detected → LOGIN_REQUIRED"""
        kind = classify_cdp_error("登录页")
        assert kind == CdpFailureKind.LOGIN_REQUIRED

    def test_classifies_content_insufficient(self):
        """Content too short → CONTENT_INSUFFICIENT"""
        kind = classify_cdp_error("内容不足(50字符)")
        assert kind == CdpFailureKind.CONTENT_INSUFFICIENT

    def test_classifies_bridge_start_failed(self):
        """cdp-bridge 连接失败 → BRIDGE_START_FAILED"""
        kind = classify_cdp_error("cdp-bridge 连接失败（Chrome 扩展未连接）")
        assert kind == CdpFailureKind.BRIDGE_START_FAILED

    def test_timeout_waiting_for_cdp_bridge_response_is_recoverable(self):
        """timeout waiting for CDP Bridge response → BRIDGE_EMPTY_RESPONSE (recoverable)"""
        kind = classify_cdp_error("timeout waiting for CDP Bridge response")
        assert kind == CdpFailureKind.BRIDGE_EMPTY_RESPONSE
        assert kind.is_recoverable() is True

    def test_unknown_error_returns_unknown(self):
        """Unrecognized error → UNKNOWN"""
        kind = classify_cdp_error("some random error message")
        assert kind == CdpFailureKind.UNKNOWN

    def test_empty_error_returns_unknown(self):
        """Empty string → UNKNOWN"""
        kind = classify_cdp_error("")
        assert kind == CdpFailureKind.UNKNOWN


class TestFailureKindIsRecoverable:
    """可恢复性判断"""

    def test_tab_debugger_conflict_is_recoverable(self):
        assert CdpFailureKind.TAB_DEBUGGER_CONFLICT.is_recoverable() is True

    def test_bridge_empty_response_is_recoverable(self):
        assert CdpFailureKind.BRIDGE_EMPTY_RESPONSE.is_recoverable() is True

    def test_extension_disconnected_is_recoverable(self):
        assert CdpFailureKind.EXTENSION_DISCONNECTED.is_recoverable() is True

    def test_module_missing_is_not_recoverable(self):
        assert CdpFailureKind.MODULE_MISSING.is_recoverable() is False

    def test_login_required_is_not_recoverable(self):
        assert CdpFailureKind.LOGIN_REQUIRED.is_recoverable() is False

    def test_unknown_is_not_recoverable(self):
        assert CdpFailureKind.UNKNOWN.is_recoverable() is False
