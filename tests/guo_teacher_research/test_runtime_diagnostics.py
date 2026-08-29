from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.runtime_diagnostics import (
    CodexRuntimeDiagnosticError,
    CodexRuntimeFailureEvent,
    OwnerOnlyCodexRuntimeDiagnosticSink,
    classify_codex_failover_failure,
)

NOW = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)


def _codex_cli_http_failure(
    status: int,
    detail: str,
    *,
    reconnect: bool = True,
    preceding_records: tuple[dict[str, object], ...] = (),
) -> str:
    message = f"unexpected status {status}: {detail}, url: https://primary.example/v1/responses"
    records = list(preceding_records)
    if reconnect:
        records.append({"type": "error", "message": f"Reconnecting... 1/5 ({message})"})
    records.append({"type": "turn.failed", "error": {"message": message}})
    return "\n".join(json.dumps(record) for record in records)


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("connection error", "CODEX_CHILD_NETWORK"),
        ("HTTP 401: unauthorized", "CODEX_CHILD_AUTH"),
        ("HTTP 429: rate limit exceeded", "CODEX_CHILD_RATE_LIMIT"),
        ("You've reached your usage limit. Buy more credits.", "CODEX_CHILD_RATE_LIMIT"),
        ("MCP server failed during initialize", "CODEX_CHILD_MCP"),
        ("MCP server connection error", "CODEX_CHILD_MCP"),
        ("unsupported model requested", "CODEX_CHILD_MODEL"),
        ("strict config contains an unknown feature", "CODEX_CHILD_CONFIG"),
        ("HTTP 503: service unavailable", "CODEX_CHILD_UPSTREAM"),
        ("unclassified child failure", "CODEX_CHILD_NONZERO_UNKNOWN"),
    ],
)
def test_owner_only_diagnostic_classifies_without_public_runtime_state(
    tmp_path: Path,
    stderr: str,
    expected: str,
) -> None:
    target = tmp_path / "state" / "diagnostics" / "last-failure.json"
    sink = OwnerOnlyCodexRuntimeDiagnosticSink(
        target=target,
        forbidden_root=Path(__file__).resolve().parents[2],
    )

    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=1,
            model="gpt-test",
            stderr=stderr,
        )
    )

    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["failure_code"] == expected
    assert frozenset(artifact) == frozenset(
        {
            "schema_version",
            "occurred_at",
            "backend",
            "model",
            "event_kind",
            "error_id",
            "elapsed_seconds",
            "route",
            "stage",
            "exit_code",
            "failure_code",
            "failover_class",
            "stderr_sha256",
            "stderr_bytes",
            "stdout_sha256",
            "stdout_bytes",
            "truncated",
            "runtime_errors",
            "probe_origin",
            "http_status",
        }
    )


def test_owner_only_diagnostic_records_decision_classifier_result(
    tmp_path: Path,
) -> None:
    """诊断同时记录决策分类器结果：诊断与决策可能分歧，必须如实保存两者。"""
    target = tmp_path / "state" / "diagnostics" / "last-failure.json"
    sink = OwnerOnlyCodexRuntimeDiagnosticSink(
        target=target,
        forbidden_root=Path(__file__).resolve().parents[2],
    )

    # 真实 502 形态：重连耗尽后的无前缀 terminal error + turn.failed
    message = "unexpected status 502: Bad Gateway, url: https://primary.example/v1/responses"
    records = [
        {"type": "error", "message": f"Reconnecting... {attempt}/5 ({message})"}
        for attempt in range(1, 6)
    ]
    records.append({"type": "error", "error": {"message": message}})
    records.append({"type": "turn.failed", "error": {"message": message}})
    stdout = "\n".join(json.dumps(record) for record in records)

    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=1,
            model="gpt-test",
            stderr="",
            stdout=stdout,
        )
    )

    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["failure_code"] == "CODEX_CHILD_UPSTREAM"
    assert artifact["failover_class"] == "CODEX_CHILD_UPSTREAM"


def test_owner_only_diagnostic_records_decision_divergence(
    tmp_path: Path,
) -> None:
    """孤立 terminal 无 reconnect：诊断文本分类可能给类别，但决策分类器 fail closed。"""
    target = tmp_path / "state" / "diagnostics" / "last-failure.json"
    sink = OwnerOnlyCodexRuntimeDiagnosticSink(
        target=target,
        forbidden_root=Path(__file__).resolve().parents[2],
    )

    stdout = json.dumps(
        {
            "type": "turn.failed",
            "error": {"message": "unexpected status 502: Bad Gateway, url: https://x/v1/responses"},
        }
    )

    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=1,
            model="gpt-test",
            stderr="",
            stdout=stdout,
        )
    )

    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["failure_code"] == "CODEX_CHILD_UPSTREAM"
    assert artifact["failover_class"] == "CODEX_CHILD_UNCLASSIFIED_EXIT"


def test_owner_only_diagnostic_classifies_signal_without_terminal_outcome_as_unclassified(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state" / "diagnostics" / "last-failure.json"
    sink = OwnerOnlyCodexRuntimeDiagnosticSink(
        target=target,
        forbidden_root=Path(__file__).resolve().parents[2],
    )

    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=-9,
            model="gpt-test",
            stderr="",
            stdout="",
        )
    )

    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["failure_code"] == "CODEX_CHILD_UNCLASSIFIED_EXIT"


def test_owner_only_diagnostic_never_persists_child_text_or_runtime_message(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state" / "diagnostics" / "last-failure.json"
    sink = OwnerOnlyCodexRuntimeDiagnosticSink(
        target=target,
        forbidden_root=Path(__file__).resolve().parents[2],
    )
    private_fragments = (
        "账户2198",
        "雅克科技100股",
        "请分析我的真实持仓",
        "token=not-a-real-token",
        "secret-shaped-error-code",
        "secret-shaped-error-type",
    )
    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=1,
            model="gpt-test",
            stderr=(
                "HTTP 401 unauthorized；账户2198；问题：请分析我的真实持仓；token=not-a-real-token"
            ),
            stdout=json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "type": "secret-shaped-error-type",
                        "code": "secret-shaped-error-code",
                        "status": 401,
                        "message": "雅克科技100股；请分析我的真实持仓",
                    },
                },
                ensure_ascii=False,
            ),
        )
    )

    serialized = target.read_text(encoding="utf-8")
    artifact = json.loads(serialized)
    assert artifact["failure_code"] == "CODEX_CHILD_AUTH"
    assert artifact["runtime_errors"] == [
        {
            "event_type": "turn.failed",
            "http_status": 401,
        }
    ]
    assert all(fragment not in serialized for fragment in private_fragments)


def test_agent_message_cannot_change_runtime_failure_classification(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state" / "diagnostics" / "last-failure.json"
    sink = OwnerOnlyCodexRuntimeDiagnosticSink(
        target=target,
        forbidden_root=Path(__file__).resolve().parents[2],
    )
    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=1,
            model="gpt-test",
            stderr="strict config contains an unknown feature",
            stdout=json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "The user's scenario mentions a network error and quota exceeded.",
                    },
                }
            ),
        )
    )

    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["failure_code"] == "CODEX_CHILD_CONFIG"


@pytest.mark.parametrize(
    ("terminal_error", "expected"),
    [
        ({"type": "configuration_error"}, "CODEX_CHILD_CONFIG"),
        ({"type": "semantic_rejection"}, "CODEX_CHILD_NONZERO_UNKNOWN"),
    ],
)
def test_early_network_event_cannot_override_terminal_failure(
    terminal_error: dict[str, object],
    expected: str,
) -> None:
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "network_error",
                        "code": "connection_error",
                    },
                }
            ),
            json.dumps({"type": "turn.failed", "error": terminal_error}),
        )
    )

    assert classify_codex_failover_failure(exit_code=1, stdout=stdout) == expected


def test_structured_configuration_conflict_denies_network_failover() -> None:
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "error",
                    "error": {"type": "configuration_error"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "type": "network_error",
                        "code": "connection_error",
                    },
                }
            ),
        )
    )

    assert classify_codex_failover_failure(exit_code=1, stdout=stdout) == "CODEX_CHILD_CONFIG"


@pytest.mark.parametrize(
    "initial_type",
    [
        "tool_policy_violation",
        "tool_error",
        "product_contract_error",
        "safety_rejection",
        "invalid_request_error",
    ],
)
def test_unknown_structured_event_denies_later_availability_failover(
    initial_type: str,
) -> None:
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.failed",
                    "error": {"type": initial_type},
                }
            ),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "type": "network_error",
                        "code": "connection_error",
                    },
                }
            ),
        )
    )

    assert (
        classify_codex_failover_failure(exit_code=1, stdout=stdout) == "CODEX_CHILD_NONZERO_UNKNOWN"
    )


@pytest.mark.parametrize(
    "terminal_error",
    [
        {"type": "safety_rejection", "status": 500},
        {"type": "tool_policy_violation", "status": 429},
        {"type": "product_contract_violation", "status": 401},
        {"type": "semantic_rejection", "status": 503},
    ],
)
def test_structured_fin_denial_outranks_conflicting_availability_status(
    terminal_error: dict[str, object],
) -> None:
    stdout = json.dumps({"type": "turn.failed", "error": terminal_error})

    assert (
        classify_codex_failover_failure(exit_code=1, stdout=stdout) == "CODEX_CHILD_NONZERO_UNKNOWN"
    )


@pytest.mark.parametrize("error_type", ("product_contract_violation", "safety_rejection"))
def test_structured_fin_denial_outranks_signal_termination(error_type: str) -> None:
    """A late SIGKILL cannot turn an already-denied product into proxy work."""

    stdout = json.dumps(
        {
            "type": "turn.failed",
            "error": {"type": error_type},
        }
    )

    assert (
        classify_codex_failover_failure(exit_code=-9, stdout=stdout)
        == "CODEX_CHILD_NONZERO_UNKNOWN"
    )


@pytest.mark.parametrize("exit_code", (-9, -15))
def test_signal_termination_without_terminal_outcome_is_unclassified(
    exit_code: int,
) -> None:
    assert (
        classify_codex_failover_failure(exit_code=exit_code, stdout="")
        == "CODEX_CHILD_UNCLASSIFIED_EXIT"
    )


@pytest.mark.parametrize(
    "terminal_error",
    (
        {"type": "network_error", "code": "connection_error"},
        {"type": "rate_limit_error"},
        {"type": "server_error"},
        {"status": 402},
    ),
)
def test_structured_availability_label_without_cli_reconnect_is_unclassified(
    terminal_error: dict[str, object],
) -> None:
    stdout = json.dumps({"type": "turn.failed", "error": terminal_error})

    assert (
        classify_codex_failover_failure(exit_code=1, stdout=stdout)
        == "CODEX_CHILD_UNCLASSIFIED_EXIT"
    )


def test_empty_child_output_is_an_unclassified_failure() -> None:
    """Diagnostics preserve the absence of a trustworthy terminal outcome."""

    assert (
        classify_codex_failover_failure(exit_code=1, stdout="") == "CODEX_CHILD_UNCLASSIFIED_EXIT"
    )


def test_message_only_child_error_is_unclassified() -> None:
    stdout = json.dumps(
        {
            "type": "turn.failed",
            "error": {
                "message": "You've reached your usage limit. Buy more credits.",
            },
        }
    )

    assert (
        classify_codex_failover_failure(exit_code=1, stdout=stdout)
        == "CODEX_CHILD_UNCLASSIFIED_EXIT"
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, "CODEX_CHILD_RATE_LIMIT"),
        (502, "CODEX_CHILD_UPSTREAM"),
    ],
)
def test_codex_cli_message_only_http_failure_is_failover_eligible(
    status: int,
    expected: str,
) -> None:
    assert (
        classify_codex_failover_failure(
            exit_code=1,
            stdout=_codex_cli_http_failure(status, "provider error: upstream unavailable"),
        )
        == expected
    )


def test_http_words_outside_codex_cli_error_envelope_remain_unclassified() -> None:
    stdout = json.dumps(
        {
            "type": "turn.failed",
            "error": {
                "message": (
                    "Agent says unexpected status 502, url: https://primary.example/v1/responses"
                ),
            },
        }
    )

    assert (
        classify_codex_failover_failure(exit_code=1, stdout=stdout)
        == "CODEX_CHILD_UNCLASSIFIED_EXIT"
    )


@pytest.mark.parametrize(
    ("conflicting_text", "expected"),
    [
        ("Authentication failed", "CODEX_CHILD_AUTH"),
        ("authentication_error", "CODEX_CHILD_AUTH"),
        ("invalid_auth", "CODEX_CHILD_AUTH"),
        ("invalid_api_key", "CODEX_CHILD_AUTH"),
        ("HTTP 403 Forbidden", "CODEX_CHILD_AUTH"),
        ("permission denied", "CODEX_CHILD_AUTH"),
        ("configuration error", "CODEX_CHILD_CONFIG"),
        ("MCP capability transport failed", "CODEX_CHILD_MCP"),
        ("product contract violation", "CODEX_CHILD_UNCLASSIFIED_EXIT"),
        ("safety rejection", "CODEX_CHILD_UNCLASSIFIED_EXIT"),
    ],
)
def test_codex_cli_http_status_cannot_override_fail_closed_message(
    conflicting_text: str,
    expected: str,
) -> None:
    assert (
        classify_codex_failover_failure(
            exit_code=1,
            stdout=_codex_cli_http_failure(502, conflicting_text),
        )
        == expected
    )


def test_codex_cli_terminal_http_message_without_reconnect_is_unclassified() -> None:
    assert (
        classify_codex_failover_failure(
            exit_code=1,
            stdout=_codex_cli_http_failure(502, "Bad Gateway", reconnect=False),
        )
        == "CODEX_CHILD_UNCLASSIFIED_EXIT"
    )


def test_codex_cli_terminal_error_after_reconnect_stays_failover_eligible() -> None:
    """真实 CLI 形态：重连耗尽后打印无前缀 terminal error，随后 turn.failed。

    仅凭 turn.failed 无法区分"重连序列中的最终错误"与"任意失败文本"；
    只要已出现 reconnect 事件且 terminal 分类一致，无前缀的 terminal error
    不应当使整个分类退回 UNCLASSIFIED。
    """
    message = "unexpected status 502: Bad Gateway, url: https://primary.example/v1/responses"
    records = [
        {"type": "error", "message": f"Reconnecting... {attempt}/5 ({message})"}
        for attempt in range(1, 6)
    ]
    records.append({"type": "error", "error": {"message": message}})
    records.append({"type": "turn.failed", "error": {"message": message}})
    stdout = "\n".join(json.dumps(record) for record in records)
    assert classify_codex_failover_failure(exit_code=1, stdout=stdout) == "CODEX_CHILD_UPSTREAM"


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "connection error"),
        (402, "payment required"),
        (404, "model not found"),
        (418, "service unavailable"),
    ],
)
def test_codex_cli_message_cannot_expand_http_failover_allowlist(
    status: int,
    message: str,
) -> None:
    assert (
        classify_codex_failover_failure(
            exit_code=1,
            stdout=_codex_cli_http_failure(status, message),
        )
        == "CODEX_CHILD_UNCLASSIFIED_EXIT"
    )


@pytest.mark.parametrize(
    "failed_item_message",
    [
        "tool policy violation",
        "product contract violation",
        "capability rejected",
        "ordinary item failure",
    ],
)
def test_message_only_failed_item_vetoes_codex_cli_http_failover(
    failed_item_message: str,
) -> None:
    assert (
        classify_codex_failover_failure(
            exit_code=1,
            stdout=_codex_cli_http_failure(
                502,
                "Bad Gateway",
                preceding_records=(
                    {"type": "item.failed", "error": {"message": failed_item_message}},
                ),
            ),
        )
        == "CODEX_CHILD_UNCLASSIFIED_EXIT"
    )


def test_owner_only_diagnostic_rejects_checkout_and_unsafe_existing_target(
    tmp_path: Path,
) -> None:
    checkout = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="outside the project checkout"):
        OwnerOnlyCodexRuntimeDiagnosticSink(
            target=checkout / "last-failure.json",
            forbidden_root=checkout,
        )

    victim = tmp_path / "victim.json"
    victim.write_text("victim sentinel", encoding="utf-8")
    target = tmp_path / "state" / "diagnostics" / "last-failure.json"
    target.parent.mkdir(parents=True, mode=0o700)
    target.symlink_to(victim)
    sink = OwnerOnlyCodexRuntimeDiagnosticSink(
        target=target,
        forbidden_root=checkout,
    )

    with pytest.raises(CodexRuntimeDiagnosticError):
        sink.record(
            CodexRuntimeFailureEvent(
                occurred_at=NOW,
                exit_code=1,
                model="gpt-test",
                stderr="connection error",
            )
        )

    assert victim.read_text(encoding="utf-8") == "victim sentinel"


def test_owner_only_diagnostic_rejects_hardlink_without_touching_victim(
    tmp_path: Path,
) -> None:
    checkout = Path(__file__).resolve().parents[2]
    victim = tmp_path / "victim.json"
    victim.write_text("victim sentinel", encoding="utf-8")
    victim.chmod(0o600)
    target = tmp_path / "state" / "diagnostics" / "last-failure.json"
    target.parent.mkdir(parents=True, mode=0o700)
    os.link(victim, target)
    sink = OwnerOnlyCodexRuntimeDiagnosticSink(
        target=target,
        forbidden_root=checkout,
    )

    with pytest.raises(CodexRuntimeDiagnosticError):
        sink.record(
            CodexRuntimeFailureEvent(
                occurred_at=NOW,
                exit_code=1,
                model="gpt-test",
                stderr="connection error",
            )
        )

    assert victim.read_text(encoding="utf-8") == "victim sentinel"


# ── v4 discriminative failure events (codex-probe-failure-evidence-20260821) ──


def _v4_sink(tmp_path: Path):
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        OwnerOnlyCodexRuntimeDiagnosticSink,
    )

    return OwnerOnlyCodexRuntimeDiagnosticSink(
        target=tmp_path / "diag.json",
        forbidden_root=Path("/home/ypk/fin-analyse"),
    )


def test_v4_timeout_event_serializes_nullable_exit_code(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        _decode_artifact,
    )

    sink = _v4_sink(tmp_path)
    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=None,
            model="gpt-5.6-sol",
            stderr="",
            stdout="",
            event_kind="timeout",
            error_id="err_1ec0bdfa8920a1247c7019078f9f9356",
            elapsed_seconds=900.5,
            route="direct-primary",
            stage="initial_runtime",
            truncated=True,
        )
    )
    decoded = _decode_artifact((tmp_path / "diag.json").read_bytes())
    assert decoded["event_kind"] == "timeout"
    assert decoded["exit_code"] is None
    assert decoded["error_id"] == "err_1ec0bdfa8920a1247c7019078f9f9356"
    assert decoded["elapsed_seconds"] == 900.5
    assert decoded["route"] == "direct-primary"
    assert decoded["stage"] == "initial_runtime"
    assert decoded["truncated"] is True


def test_v4_artifact_never_contains_stream_text(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        _decode_artifact,
    )

    sink = _v4_sink(tmp_path)
    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=1,
            model="gpt-5.6-sol",
            stderr="secret line with portfolio data",
            stdout="prompt text should not persist",
            event_kind="exit_failure",
            error_id="err_1ec0bdfa8920a1247c7019078f9f9356",
            elapsed_seconds=12.0,
        )
    )
    decoded = _decode_artifact((tmp_path / "diag.json").read_bytes())
    assert "stderr" not in decoded
    assert "stdout" not in decoded
    assert "secret line" not in str(decoded)
    assert "prompt text" not in str(decoded)
    assert decoded["stderr_bytes"] == len(b"secret line with portfolio data")
    assert decoded["stdout_sha256"]


def test_v4_exit_failure_requires_int_exit_code(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        _build_artifact,
    )

    with pytest.raises(CodexRuntimeDiagnosticError):
        _build_artifact(
            CodexRuntimeFailureEvent(
                occurred_at=NOW,
                exit_code=None,
                model="gpt-5.6-sol",
                stderr="",
                stdout="",
                event_kind="exit_failure",
                error_id="err_1ec0bdfa8920a1247c7019078f9f9356",
            )
        )


def test_v4_probe_failure_carries_origin_and_status(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        _decode_artifact,
    )

    sink = _v4_sink(tmp_path)
    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=None,
            model="gpt-5.6-sol",
            stderr="",
            stdout="",
            event_kind="probe_failure",
            error_id="err_1ec0bdfa8920a1247c7019078f9f9356",
            elapsed_seconds=60.0,
            probe_origin="api.openai.com",
            http_status=429,
        )
    )
    decoded = _decode_artifact((tmp_path / "diag.json").read_bytes())
    assert decoded["event_kind"] == "probe_failure"
    assert decoded["probe_origin"] == "api.openai.com"
    assert decoded["http_status"] == 429


def test_v4_rejects_unknown_event_kind() -> None:
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        _build_artifact,
    )

    with pytest.raises(CodexRuntimeDiagnosticError):
        _build_artifact(
            CodexRuntimeFailureEvent(
                occurred_at=NOW,
                exit_code=None,
                model="gpt-5.6-sol",
                stderr="",
                stdout="",
                event_kind="mystery_kind",
                error_id="err_1ec0bdfa8920a1247c7019078f9f9356",
            )
        )


def test_v4_rejects_probe_failure_without_origin_or_status() -> None:
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        _build_artifact,
    )

    with pytest.raises(CodexRuntimeDiagnosticError):
        _build_artifact(
            CodexRuntimeFailureEvent(
                occurred_at=NOW,
                exit_code=None,
                model="gpt-5.6-sol",
                stderr="",
                stdout="",
                event_kind="probe_failure",
                error_id="err_1ec0bdfa8920a1247c7019078f9f9356",
            )
        )


def test_v3_legacy_artifact_still_decodes(tmp_path: Path) -> None:
    import hashlib

    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        _decode_artifact,
    )

    legacy = {
        "schema_version": "fin.codex-runtime-failure-diagnostic/v3",
        "occurred_at": NOW.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "backend": "codex",
        "model": "gpt-5.6-sol",
        "exit_code": 1,
        "failure_code": "CODEX_CHILD_NONZERO_UNKNOWN",
        "failover_class": "CODEX_CHILD_UNCLASSIFIED_EXIT",
        "stderr_sha256": hashlib.sha256(b"x").hexdigest(),
        "stderr_bytes": 1,
        "stdout_sha256": hashlib.sha256(b"y").hexdigest(),
        "stdout_bytes": 1,
        "runtime_errors": [],
    }
    payload = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    decoded = _decode_artifact(payload)
    assert decoded["exit_code"] == 1
    assert decoded["schema_version"] == "fin.codex-runtime-failure-diagnostic/v3"


def test_v2_legacy_artifact_still_decodes_lenient(tmp_path: Path) -> None:
    import hashlib

    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        _decode_artifact_lenient,
    )

    legacy = {
        "schema_version": "fin.codex-runtime-failure-diagnostic/v2",
        "occurred_at": NOW.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "backend": "codex",
        "model": "gpt-5.6-sol",
        "exit_code": 1,
        "failure_code": "CODEX_CHILD_NONZERO_UNKNOWN",
        "stderr_sha256": hashlib.sha256(b"x").hexdigest(),
        "stderr_bytes": 1,
        "stdout_sha256": hashlib.sha256(b"y").hexdigest(),
        "stdout_bytes": 1,
        "runtime_errors": [],
    }
    payload = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    decoded = _decode_artifact_lenient(payload)
    assert decoded["exit_code"] == 1


def test_v4_decoder_rejects_unknown_extra_field(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        _decode_artifact,
    )

    sink = _v4_sink(tmp_path)
    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=None,
            model="gpt-5.6-sol",
            stderr="",
            stdout="",
            event_kind="stall",
            error_id="err_1ec0bdfa8920a1247c7019078f9f9356",
        )
    )
    payload = json.loads((tmp_path / "diag.json").read_bytes())
    payload["evil_field"] = "x"
    with pytest.raises(ValueError):
        _decode_artifact(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )


# ── EvidenceCollectionSink 集合语义 (N2) ────────────────────────────────────


def _evidence_event(error_id: str = "err_1ec0bdfa8920a1247c7019078f9f9356"):
    return CodexRuntimeFailureEvent(
        occurred_at=NOW,
        exit_code=1,
        model="gpt-5.6-sol",
        stderr="",
        stdout="",
        event_kind="exit_failure",
        error_id=error_id,
        elapsed_seconds=1.0,
    )


def test_evidence_collection_writes_entries(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        EvidenceCollectionSink,
    )

    root = tmp_path / "evidence"
    sink = EvidenceCollectionSink(root=root, forbidden_root=Path("/home/ypk/fin-analyse"))
    sink.record(_evidence_event("err_1ec0bdfa8920a1247c7019078f9f9356"))
    entries = list(root.glob("*.json"))
    assert len(entries) == 1
    assert entries[0].stat().st_mode & 0o777 == 0o600
    import json as _json

    artifact = _json.loads(entries[0].read_text(encoding="utf-8"))
    assert artifact["event_kind"] == "exit_failure"
    assert artifact["error_id"] == "err_1ec0bdfa8920a1247c7019078f9f9356"
    assert "stderr" not in artifact and "stdout" not in artifact


def test_evidence_collection_enforces_entry_quota(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        EvidenceCollectionSink,
    )

    root = tmp_path / "evidence"
    sink = EvidenceCollectionSink(
        root=root,
        forbidden_root=Path("/home/ypk/fin-analyse"),
        max_entries=3,
        max_total_bytes=1024 * 1024,
    )
    for i in range(5):
        error_id = f"err_{i:032x}"
        sink.record(_evidence_event(error_id))
    entries = list(root.glob("*.json"))
    # 满额后新写入被拒绝,历史保留前 3 条(audit r3-5:不淘汰历史)。
    assert len(entries) == 3
    names = sorted(e.name for e in entries)
    assert len(names) == 3
    assert all(f"err_{i:032x}" in name for i, name in enumerate(names))


def test_evidence_collection_enforces_ttl(tmp_path: Path) -> None:
    import os
    import time as _time

    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        EvidenceCollectionSink,
    )

    root = tmp_path / "evidence"
    sink = EvidenceCollectionSink(
        root=root,
        forbidden_root=Path("/home/ypk/fin-analyse"),
        max_entries=10,
        max_total_bytes=1024 * 1024,
        ttl_seconds=3600,
    )
    sink.record(_evidence_event("err_1ec0bdfa8920a1247c7019078f9f9356"))
    old_entry = list(root.glob("*.json"))[0]
    # 老化第一个条目(模拟超过 TTL),再写入第二个触发清理。
    old = _time.time() - 7200
    os.utime(old_entry, (old, old))
    sink.record(_evidence_event("err_2ec0bdfa8920a1247c7019078f9f9356"))
    remaining = list(root.glob("*.json"))
    assert len(remaining) == 1
    assert "err_2ec0bdfa" in remaining[0].name


def test_evidence_collection_never_persists_stream_text(tmp_path: Path) -> None:
    from fin_analyse.guo_teacher_research.runtime_diagnostics import (
        EvidenceCollectionSink,
    )

    root = tmp_path / "evidence"
    sink = EvidenceCollectionSink(root=root, forbidden_root=Path("/home/ypk/fin-analyse"))
    sink.record(
        CodexRuntimeFailureEvent(
            occurred_at=NOW,
            exit_code=1,
            model="gpt-5.6-sol",
            stderr="账户2198 secret",
            stdout="prompt text",
            event_kind="exit_failure",
            error_id="err_1ec0bdfa8920a1247c7019078f9f9356",
        )
    )
    serialized = (list(root.glob("*.json"))[0]).read_text(encoding="utf-8")
    assert "账户2198" not in serialized
    assert "prompt text" not in serialized
