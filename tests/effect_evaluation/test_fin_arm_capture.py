from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.effect_evaluation.fin_arm_capture as fin_capture
from tools.effect_evaluation.fin_arm_capture import (
    _assert_continuity,
    _build_command,
    _capture_one,
    _expand_skill,
    _parse_session_export,
    _scored_identity,
)


def _session_export(
    *, question: str = "原问题", status: str = "partial", extra_command: bool = False
) -> str:
    result = {
        "status": status,
        "problem": None,
        "advisory_only": True,
        "execution_allowed": False,
        "presentation": {"text": "FIN 原样答案"},
        "transport_receipt": {"attempt_id": "att_123"},
        "result_meta": {"continuity": "NEW_CHAIN", "generation": "FRESH"},
    }
    wrapped_result = {
        "result": json.dumps(result, ensure_ascii=False),
        "structuredContent": {"result": json.dumps(result, ensure_ascii=False)},
    }
    command = {"action": "consult", "question": question}
    if extra_command:
        command["memory_event"] = {"kind": "invented"}
    wrapper_args = {
        "name": "mcp__fin_analyse__fin_consultation",
        "arguments": {"command": command},
    }
    messages = [
        {"role": "user", "content": "skill scaffolding"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "tool_call",
                        "arguments": json.dumps(wrapper_args, ensure_ascii=False),
                    }
                }
            ],
        },
        {
            "role": "tool",
            "tool_name": "mcp__fin_analyse__fin_consultation",
            "content": (
                '<untrusted_tool_result source="mcp__fin_analyse__fin_consultation">\n'
                "warning\n\n"
                f"{json.dumps(wrapped_result, ensure_ascii=False)}\n"
                "</untrusted_tool_result>"
            ),
        },
        {"role": "assistant", "content": "FIN 原样答案", "tool_calls": []},
    ]
    return json.dumps(
        {
            "id": "20260824_140000_abc123",
            "source": "tool",
            "model": "mimo-v2.5",
            "messages": messages,
        },
        ensure_ascii=False,
    )


def test_build_command_uses_official_quiet_skill_entry_and_exact_resume(tmp_path: Path) -> None:
    command = _build_command(
        hermes_bin="/opt/hermes",
        query="expanded skill message",
        session_id="20260824_140000_abc123",
    )

    assert command[:4] == ["/opt/hermes", "--profile", "fin", "chat"]
    assert command[command.index("-q") + 1] == "expanded skill message"
    assert "-Q" in command
    assert command[command.index("--source") + 1] == "tool"
    assert command[command.index("--reasoning") + 1] == "xhigh"
    assert command[command.index("--resume") + 1] == "20260824_140000_abc123"
    assert "--in" not in command


def test_expand_skill_uses_official_builder_and_preserves_question(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        assert command[:2] == ["/opt/hermes-python", "-B"]
        assert kwargs["input"] == "原问题"
        assert kwargs["env"]["HERMES_HOME"] == str(tmp_path)
        return SimpleNamespace(returncode=0, stdout="expanded skill message", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _expand_skill(
        question="原问题",
        hermes_python="/opt/hermes-python",
        hermes_home=tmp_path,
    ) == ("expanded skill message", "")


def test_parse_session_export_requires_one_exact_safe_fin_answer() -> None:
    facts = _parse_session_export(
        _session_export(),
        expected_session_id="20260824_140000_abc123",
        expected_question="原问题",
        prior_message_count=0,
    )

    assert facts["status"] == "partial"
    assert facts["attempt_id"] == "att_123"
    assert facts["presentation_text"] == "FIN 原样答案"
    assert facts["message_count"] == 4
    assert facts["fin_call_count"] == 1


def test_parse_session_export_accepts_the_public_completed_status() -> None:
    facts = _parse_session_export(
        _session_export(status="completed"),
        expected_session_id="20260824_140000_abc123",
        expected_question="原问题",
        prior_message_count=0,
    )

    assert facts["status"] == "completed"


@pytest.mark.parametrize(
    ("question", "status", "reason"),
    (
        ("被改写的问题", "partial", "question_mismatch"),
        ("原问题", "unavailable", "fin_result_failed"),
    ),
)
def test_parse_session_export_fails_closed(
    question: str, status: str, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        _parse_session_export(
            _session_export(question=question, status=status),
            expected_session_id="20260824_140000_abc123",
            expected_question="原问题",
            prior_message_count=0,
        )


def test_parse_session_export_rejects_extra_command_fields() -> None:
    with pytest.raises(ValueError, match="fin_command_invalid"):
        _parse_session_export(
            _session_export(extra_command=True),
            expected_session_id="20260824_140000_abc123",
            expected_question="原问题",
            prior_message_count=0,
        )


def test_continuity_rejects_silent_fresh_on_resume() -> None:
    _assert_continuity(
        {"continuity": "NEW_CHAIN", "generation": "FRESH"}, resumed=False
    )
    _assert_continuity(
        {"continuity": "CONTINUED_CHAIN", "generation": "FRESH"}, resumed=True
    )
    with pytest.raises(ValueError, match="continuity_mismatch"):
        _assert_continuity(
            {"continuity": "DEGRADED_FRESH", "generation": "FRESH"}, resumed=True
        )


def test_scored_identity_matches_frozen_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "releases" / "abc123"
    release.mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(release)
    files = {}
    for name in ("routes", "account", "hermes", "skill", "capture", "capture-test"):
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        files[name] = path
    digest = lambda path: fin_capture._sha256_file(path)  # noqa: E731
    freeze = tmp_path / "freeze.json"
    freeze.write_text(
        json.dumps(
            {
                "production": {
                    "current_release": "abc123",
                    "gateway_pid": 123,
                    "gateway_state": "active/running",
                    "route_config_file_sha256": digest(files["routes"]),
                    "account_snapshot_sha256": digest(files["account"]),
                    "hermes_binary_sha256": digest(files["hermes"]),
                    "consultation_skill_sha256": digest(files["skill"]),
                },
                "capture": {
                    "implementation_sha256": digest(files["capture"]),
                    "test_sha256": digest(files["capture-test"]),
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(fin_capture, "_CURRENT_LINK", current)
    monkeypatch.setattr(fin_capture, "_ROUTE_CONFIG", files["routes"])
    monkeypatch.setattr(fin_capture, "_ACCOUNT_SNAPSHOT", files["account"])
    monkeypatch.setattr(fin_capture, "_HERMES_BINARY", files["hermes"])
    monkeypatch.setattr(fin_capture, "_CONSULTATION_SKILL", files["skill"])
    monkeypatch.setattr(fin_capture, "_CAPTURE_IMPLEMENTATION", files["capture"])
    monkeypatch.setattr(fin_capture, "_CAPTURE_TEST", files["capture-test"])
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="MainPID=123\nActiveState=active\nSubState=running\n",
            stderr="",
        ),
    )

    assert _scored_identity(freeze)["gateway_pid"] == 123


def test_capture_writes_private_verified_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diagnostic = tmp_path / "last-invocation.json"

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        if "-c" in command:
            return SimpleNamespace(returncode=0, stdout="expanded skill message", stderr="")
        if "sessions" in command:
            return SimpleNamespace(returncode=0, stdout=_session_export(), stderr="")
        diagnostic.write_text(
            json.dumps(
                {
                    "current": {
                        "model": "gpt-5.6-sol",
                        "status": "succeeded",
                        "route": "direct-primary",
                        "occurred_at": datetime.now(UTC).isoformat(),
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            returncode=0,
            stdout="FIN 原样答案\n",
            stderr="session_id: 20260824_140000_abc123\n",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    record = _capture_one(
        question="原问题",
        state_root=tmp_path,
        campaign_id="campaign-1",
        phase="calibration",
        category="holdings-action",
        chain_id="chain-1",
        turn=1,
        item_id="c1t1",
        hermes_bin="/opt/hermes",
        hermes_python="/opt/hermes-python",
        hermes_home=tmp_path,
        workspace=tmp_path,
        timeout_seconds=30,
        session_id=None,
        prior_message_count=0,
        runtime_diagnostic_path=diagnostic,
    )

    assert record["status"] == "completed"
    assert record["runtime_model"] == "gpt-5.6-sol"
    assert record["runtime_status"] == "succeeded"
    turn_dir = tmp_path / "campaign-1" / "calibration" / "chain-1" / "turn-01-c1t1"
    for path in turn_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "FIN 原样答案" not in (turn_dir / "record.json").read_text(encoding="utf-8")
