"""Capture and verify one official-Hermes FIN arm turn."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tools.effect_evaluation.direct_arm_capture import (
    _ensure_private_dir,
    _resolved_binary,
    _safe_slug,
    _sha256_bytes,
    _sha256_file,
    _text,
    _write_private,
)

_PROFILE = "fin"
_REASONING_EFFORT = "xhigh"
_FIN_TOOL = "mcp__fin_analyse__fin_consultation"
_MODEL = "gpt-5.6-sol"
_SESSION_ID_RE = re.compile(r"(?m)^session_id:\s*([A-Za-z0-9_.-]+)\s*$")
_DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "fin-analyse"
_DEFAULT_DIAGNOSTIC = (
    _DEFAULT_STATE_ROOT
    / "semantic-research-v1"
    / "runtime-diagnostics"
    / "codex-consultation-last-invocation.json"
)
_DEFAULT_HERMES_HOME = Path.home() / ".hermes" / "profiles" / "fin"
_DEFAULT_HERMES_PYTHON = (
    Path.home()
    / ".local/share/hermes-agent/venvs/9f13bbbf8423427e159c78066356ca0e27ca6b74/bin/python"
)
_CURRENT_LINK = Path.home() / ".local/share/fin-analyse/current"
_ROUTE_CONFIG = Path.home() / "fin-data/codex_routes.yaml"
_ACCOUNT_SNAPSHOT = Path.home() / ".config/fin-analyse/actual-advisory-portfolio.v1.json"
_HERMES_BINARY = Path.home() / ".local/bin/hermes"
_CONSULTATION_SKILL = (
    _DEFAULT_HERMES_HOME / "skills/fin-analyse/fin-analyse-consultation/SKILL.md"
)
_CAPTURE_IMPLEMENTATION = Path(__file__).resolve()
_CAPTURE_TEST = (
    Path(__file__).resolve().parents[2]
    / "tests/effect_evaluation/test_fin_arm_capture.py"
)
_SKILL_BUILDER = """\
import sys
from agent.skill_commands import build_skill_invocation_message, resolve_skill_command_key
question = sys.stdin.read()
key = resolve_skill_command_key("fin-analyse-consultation")
message = build_skill_invocation_message(key, question) if key else None
if not message:
    raise SystemExit("skill_expansion_failed")
sys.stdout.write(message)
"""


def _build_command(
    *,
    hermes_bin: str,
    query: str,
    session_id: str | None,
) -> list[str]:
    command = [
        hermes_bin,
        "--profile",
        _PROFILE,
        "chat",
        "-q",
        query,
        "-Q",
        "--source",
        "tool",
        "--reasoning",
        _REASONING_EFFORT,
    ]
    if session_id is not None:
        command.extend(("--resume", session_id, "--no-restore-cwd"))
    return command


def _expand_skill(
    *, question: str, hermes_python: str, hermes_home: Path
) -> tuple[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "HERMES_PLATFORM": "cli",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    completed = subprocess.run(
        [hermes_python, "-B", "-c", _SKILL_BUILDER],
        input=question,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    query = _text(completed.stdout)
    stderr = _text(completed.stderr)
    if completed.returncode != 0 or not query:
        raise ValueError("skill_expansion_failed")
    return query, stderr


def _object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("expected_json_object")
    return value


def _fin_call_arguments(message: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        if function.get("name") != "tool_call":
            continue
        wrapper = _object(function.get("arguments"))
        if wrapper.get("name") == _FIN_TOOL:
            found.append(_object(wrapper.get("arguments")))
    return found


def _fin_result(content: object) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("fin_result_invalid")
    candidates = [line for line in content.splitlines() if line.startswith("{")]
    if not candidates:
        raise ValueError("fin_result_invalid")
    wrapped = _object(candidates[-1])
    structured = _object(wrapped.get("structuredContent"))
    return _object(structured.get("result"))


def _parse_session_export(
    raw: str,
    *,
    expected_session_id: str,
    expected_question: str,
    prior_message_count: int,
) -> dict[str, Any]:
    rows = [line for line in raw.splitlines() if line.strip()]
    if len(rows) != 1:
        raise ValueError("session_export_invalid")
    session = _object(rows[0])
    if session.get("id") != expected_session_id or session.get("source") != "tool":
        raise ValueError("session_identity_mismatch")
    messages = session.get("messages")
    if not isinstance(messages, list) or not 0 <= prior_message_count < len(messages):
        raise ValueError("session_messages_invalid")
    new_messages = messages[prior_message_count:]

    mcp_messages = [
        message
        for message in new_messages
        if isinstance(message, dict) and str(message.get("tool_name", "")).startswith("mcp__")
    ]
    if [message.get("tool_name") for message in mcp_messages] != [_FIN_TOOL]:
        raise ValueError("unexpected_mcp_calls")

    calls = [
        arguments
        for message in new_messages
        if isinstance(message, dict)
        for arguments in _fin_call_arguments(message)
    ]
    if len(calls) != 1:
        raise ValueError("fin_call_count_invalid")
    command = calls[0].get("command")
    if (
        set(calls[0]) != {"command"}
        or not isinstance(command, dict)
        or set(command) != {"action", "question"}
        or command.get("action") != "consult"
    ):
        raise ValueError("fin_command_invalid")
    if command.get("question") != expected_question:
        raise ValueError("question_mismatch")

    result_message = mcp_messages[0]
    result = _fin_result(result_message.get("content"))
    presentation = result.get("presentation")
    presentation_text = presentation.get("text") if isinstance(presentation, dict) else None
    if (
        result.get("status") not in {"completed", "partial"}
        or result.get("problem") is not None
        or result.get("advisory_only") is not True
        or result.get("execution_allowed") is not False
        or not isinstance(presentation_text, str)
        or not presentation_text.strip()
    ):
        raise ValueError("fin_result_failed")

    result_index = new_messages.index(result_message)
    final_answers = [
        message.get("content")
        for message in new_messages[result_index + 1 :]
        if isinstance(message, dict)
        and message.get("role") == "assistant"
        and not message.get("tool_calls")
    ]
    if final_answers != [presentation_text]:
        raise ValueError("presentation_mismatch")

    receipt = result.get("transport_receipt")
    attempt_id = receipt.get("attempt_id") if isinstance(receipt, dict) else None
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("transport_receipt_missing")
    return {
        "status": result["status"],
        "attempt_id": attempt_id,
        "presentation_text": presentation_text,
        "message_count": len(messages),
        "fin_call_count": sum(
            message.get("tool_name") == _FIN_TOOL
            for message in messages
            if isinstance(message, dict)
        ),
        "outer_model": session.get("model"),
        "result_meta": result.get("result_meta"),
    }


def _runtime_diagnostic(
    path: Path, *, started_at: datetime, finished_at: datetime
) -> dict[str, str]:
    snapshot = _object(json.loads(path.read_text(encoding="utf-8")))
    current = _object(snapshot.get("current"))
    occurred_at = datetime.fromisoformat(str(current.get("occurred_at")).replace("Z", "+00:00"))
    if not started_at - timedelta(seconds=2) <= occurred_at <= finished_at + timedelta(seconds=2):
        raise ValueError("runtime_diagnostic_stale")
    if current.get("model") != _MODEL or current.get("status") != "succeeded":
        raise ValueError("runtime_diagnostic_failed")
    route = current.get("route")
    if not isinstance(route, str) or not route:
        raise ValueError("runtime_diagnostic_invalid")
    return {
        "model": _MODEL,
        "status": "succeeded",
        "route": route,
        "occurred_at": occurred_at.isoformat(),
    }


def _assert_continuity(result_meta: object, *, resumed: bool) -> None:
    if not isinstance(result_meta, dict):
        raise ValueError("continuity_mismatch")
    continuity = result_meta.get("continuity")
    generation = result_meta.get("generation")
    if resumed:
        valid = continuity == "CONTINUED_CHAIN" and generation in {
            "FRESH",
            "IDEMPOTENCY_REPLAY",
        }
    else:
        valid = continuity == "NEW_CHAIN" and generation == "FRESH"
    if not valid:
        raise ValueError("continuity_mismatch")


def _scored_identity(freeze_path: Path) -> dict[str, Any]:
    freeze = _object(json.loads(freeze_path.read_text(encoding="utf-8")))
    expected_production = _object(freeze.get("production"))
    expected_capture = _object(freeze.get("capture"))
    completed = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            "hermes-gateway-fin.service",
            "-p",
            "MainPID",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    properties = {}
    for line in _text(completed.stdout).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key] = value
    actual = {
        "current_release": _CURRENT_LINK.resolve().name,
        "gateway_pid": int(properties.get("MainPID", "0")),
        "gateway_state": f"{properties.get('ActiveState')}/{properties.get('SubState')}",
        "route_config_file_sha256": _sha256_file(_ROUTE_CONFIG),
        "account_snapshot_sha256": _sha256_file(_ACCOUNT_SNAPSHOT),
        "hermes_binary_sha256": _sha256_file(_HERMES_BINARY),
        "consultation_skill_sha256": _sha256_file(_CONSULTATION_SKILL),
        "implementation_sha256": _sha256_file(_CAPTURE_IMPLEMENTATION),
        "test_sha256": _sha256_file(_CAPTURE_TEST),
    }
    expected = {
        key: expected_production.get(key)
        for key in (
            "current_release",
            "gateway_pid",
            "gateway_state",
            "route_config_file_sha256",
            "account_snapshot_sha256",
            "hermes_binary_sha256",
            "consultation_skill_sha256",
        )
    }
    expected.update(
        {
            "implementation_sha256": expected_capture.get("implementation_sha256"),
            "test_sha256": expected_capture.get("test_sha256"),
        }
    )
    if completed.returncode != 0 or actual != expected:
        raise ValueError("scored_identity_drift")
    return actual


def _capture_one(
    *,
    question: str,
    state_root: Path,
    campaign_id: str,
    phase: str,
    category: str,
    chain_id: str,
    turn: int,
    item_id: str,
    hermes_bin: str,
    hermes_python: str,
    hermes_home: Path,
    workspace: Path,
    timeout_seconds: int,
    session_id: str | None,
    prior_message_count: int,
    runtime_diagnostic_path: Path,
    scored_freeze_path: Path | None = None,
) -> dict[str, Any]:
    campaign_id = _safe_slug(campaign_id, name="campaign_id")
    phase = _safe_slug(phase, name="phase")
    category = _safe_slug(category, name="category")
    chain_id = _safe_slug(chain_id, name="chain_id")
    item_id = _safe_slug(item_id, name="item_id")
    if not question.strip() or turn < 1:
        raise ValueError("question and turn must be valid")
    identity_before = _scored_identity(scored_freeze_path) if scored_freeze_path else {}

    turn_dir = (
        state_root
        / campaign_id
        / phase
        / chain_id
        / f"turn-{turn:02d}-{item_id}"
    )
    if turn_dir.exists():
        raise FileExistsError(f"turn evidence already exists: {turn_dir}")
    for directory in (
        state_root,
        state_root / campaign_id,
        state_root / campaign_id / phase,
        state_root / campaign_id / phase / chain_id,
        turn_dir,
    ):
        _ensure_private_dir(directory)

    question_path = turn_dir / "question.txt"
    stdout_path = turn_dir / "stdout.txt"
    stderr_path = turn_dir / "stderr.txt"
    export_path = turn_dir / "session.jsonl"
    export_stderr_path = turn_dir / "session-export.stderr.txt"
    expansion_stderr_path = turn_dir / "skill-expansion.stderr.txt"
    record_path = turn_dir / "record.json"
    _write_private(question_path, question)

    expanded_query, expansion_stderr = _expand_skill(
        question=question,
        hermes_python=hermes_python,
        hermes_home=hermes_home,
    )
    _write_private(expansion_stderr_path, expansion_stderr)
    command = _build_command(
        hermes_bin=hermes_bin,
        query=expanded_query,
        session_id=session_id,
    )
    started_at = datetime.now(UTC)
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = int(completed.returncode)
        stdout = _text(completed.stdout)
        stderr = _text(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = -1
        stdout = _text(exc.stdout)
        stderr = _text(exc.stderr)
    _write_private(stdout_path, stdout)
    _write_private(stderr_path, stderr)

    extracted_ids = set(_SESSION_ID_RE.findall(stderr))
    returned_session_id = session_id
    if session_id is None and len(extracted_ids) == 1:
        returned_session_id = next(iter(extracted_ids))
    failure_reasons: list[str] = []
    if timed_out:
        failure_reasons.append("timeout")
    elif exit_code != 0:
        failure_reasons.append("nonzero_exit")
    if returned_session_id is None:
        failure_reasons.append("missing_session_id")
    elif extracted_ids and extracted_ids != {returned_session_id}:
        failure_reasons.append("session_id_mismatch")

    session_export = ""
    export_stderr = ""
    export_exit_code: int | None = None
    if returned_session_id is not None:
        exported = subprocess.run(
            [
                hermes_bin,
                "--profile",
                _PROFILE,
                "sessions",
                "export",
                "-",
                "--format",
                "jsonl",
                "--session-id",
                returned_session_id,
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        export_exit_code = int(exported.returncode)
        session_export = _text(exported.stdout)
        export_stderr = _text(exported.stderr)
        if export_exit_code != 0:
            failure_reasons.append("session_export_failed")
    _write_private(export_path, session_export)
    _write_private(export_stderr_path, export_stderr)

    facts: dict[str, Any] = {}
    if returned_session_id is not None and export_exit_code == 0:
        try:
            facts = _parse_session_export(
                session_export,
                expected_session_id=returned_session_id,
                expected_question=question,
                prior_message_count=prior_message_count,
            )
            _assert_continuity(facts.get("result_meta"), resumed=session_id is not None)
            if stdout.rstrip("\r\n") != facts["presentation_text"]:
                raise ValueError("stdout_presentation_mismatch")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            failure_reasons.append(str(exc))

    finished_at = datetime.now(UTC)
    runtime: dict[str, str] = {}
    if facts:
        try:
            runtime = _runtime_diagnostic(
                runtime_diagnostic_path,
                started_at=started_at,
                finished_at=finished_at,
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            failure_reasons.append(str(exc))

    identity_after: dict[str, Any] = {}
    if scored_freeze_path is not None:
        try:
            identity_after = _scored_identity(scored_freeze_path)
            if identity_after != identity_before:
                raise ValueError("scored_identity_drift")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            failure_reasons.append(str(exc))

    binary = _resolved_binary(hermes_bin)
    presentation_text = facts.pop("presentation_text", None)
    fin_status = facts.pop("status", None)
    record: dict[str, Any] = {
        "schema_version": "fin.r1-fin-arm-capture/v1",
        "campaign_id": campaign_id,
        "phase": phase,
        "category": category,
        "chain_id": chain_id,
        "turn": turn,
        "item_id": item_id,
        "question_sha256": _sha256_bytes(question.encode("utf-8")),
        "model": _MODEL,
        "reasoning_effort": _REASONING_EFFORT,
        "sandbox": "read-only",
        "source": "tool",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "timeout_seconds": timeout_seconds,
        "status": "completed" if not failure_reasons else "failed",
        "failure_reasons": failure_reasons,
        "exit_code": exit_code,
        "session_export_exit_code": export_exit_code,
        "session_id": returned_session_id,
        "resumed_session_id": session_id,
        "session_start": session_id is None,
        "runtime_model": runtime.get("model"),
        "runtime_status": runtime.get("status"),
        "runtime_route": runtime.get("route"),
        "runtime_occurred_at": runtime.get("occurred_at"),
        "production_release": identity_before.get("current_release"),
        "gateway_pid": identity_before.get("gateway_pid"),
        "scored_freeze_sha256": (
            _sha256_file(scored_freeze_path) if scored_freeze_path is not None else None
        ),
        "fin_status": fin_status,
        "presentation_sha256": (
            _sha256_bytes(presentation_text.encode("utf-8"))
            if isinstance(presentation_text, str)
            else None
        ),
        **facts,
        "hermes_binary_sha256": _sha256_file(binary) if binary is not None else None,
        "question_file_sha256": _sha256_file(question_path),
        "stdout_sha256": _sha256_file(stdout_path),
        "stderr_sha256": _sha256_file(stderr_path),
        "session_export_sha256": _sha256_file(export_path),
        "runtime_diagnostic_sha256": _sha256_file(runtime_diagnostic_path),
        "expanded_query_sha256": _sha256_bytes(expanded_query.encode("utf-8")),
        "skill_expansion_stderr_sha256": _sha256_file(expansion_stderr_path),
    }
    _write_private(record_path, json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
    return record


def _load_question(path: Path, *, chain_id: str, item_id: str) -> dict[str, Any]:
    payload = _object(json.loads(path.read_text(encoding="utf-8")))
    matches: list[dict[str, Any]] = []
    for chain in payload.get("chains") or []:
        if isinstance(chain, dict) and chain.get("chain_id") == chain_id:
            for turn in chain.get("turns") or []:
                if isinstance(turn, dict) and turn.get("item_id") == item_id:
                    matches.append({**turn, "category": chain.get("category")})
    if len(matches) != 1 or not isinstance(matches[0].get("question"), str):
        raise ValueError("question_not_found")
    return matches[0]


def _load_prior(
    *, state_root: Path, campaign_id: str, phase: str, chain_id: str, turn: int
) -> tuple[str, int]:
    records = sorted(
        (state_root / campaign_id / phase / chain_id).glob(
            f"turn-{turn - 1:02d}-*/record.json"
        )
    )
    if len(records) != 1:
        raise ValueError("prior_turn_record_missing")
    record = _object(json.loads(records[0].read_text(encoding="utf-8")))
    session_id = record.get("session_id")
    message_count = record.get("message_count")
    if (
        record.get("status") != "completed"
        or not isinstance(session_id, str)
        or not isinstance(message_count, int)
    ):
        raise ValueError("prior_turn_incomplete")
    return session_id, message_count


def _public_summary(record: dict[str, Any]) -> dict[str, Any]:
    session_id = record.get("session_id")
    return {
        "status": record.get("status"),
        "phase": record.get("phase"),
        "chain_id": record.get("chain_id"),
        "turn": record.get("turn"),
        "item_id": record.get("item_id"),
        "failure_reasons": record.get("failure_reasons"),
        "session_id_sha256": (
            hashlib.sha256(session_id.encode("utf-8")).hexdigest()
            if isinstance(session_id, str)
            else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions-file", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--chain-id", required=True)
    parser.add_argument("--item-id", required=True)
    parser.add_argument("--state-root", type=Path, default=_DEFAULT_STATE_ROOT)
    parser.add_argument("--hermes-bin", default="/home/ypk/.local/bin/hermes")
    parser.add_argument("--hermes-python", default=str(_DEFAULT_HERMES_PYTHON))
    parser.add_argument("--hermes-home", type=Path, default=_DEFAULT_HERMES_HOME)
    parser.add_argument(
        "--workspace", type=Path, default=_DEFAULT_HERMES_HOME
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--runtime-diagnostic", type=Path, default=_DEFAULT_DIAGNOSTIC)
    parser.add_argument("--scored-freeze", type=Path)
    args = parser.parse_args(argv)
    if args.phase != "fin" and not args.phase.startswith("calibration"):
        parser.error("phase must be fin or start with calibration")
    if args.phase == "fin" and args.scored_freeze is None:
        parser.error("--scored-freeze is required for phase fin")

    item = _load_question(
        args.questions_file, chain_id=args.chain_id, item_id=args.item_id
    )
    turn = int(item["turn"])
    fresh = args.phase.startswith("calibration") or bool(item.get("fresh_session"))
    if fresh:
        session_id, prior_message_count = None, 0
    else:
        session_id, prior_message_count = _load_prior(
            state_root=args.state_root,
            campaign_id=args.campaign_id,
            phase=args.phase,
            chain_id=args.chain_id,
            turn=turn,
        )
    record = _capture_one(
        question=item["question"],
        state_root=args.state_root,
        campaign_id=args.campaign_id,
        phase=args.phase,
        category=str(item["category"]),
        chain_id=args.chain_id,
        turn=turn,
        item_id=args.item_id,
        hermes_bin=args.hermes_bin,
        hermes_python=args.hermes_python,
        hermes_home=args.hermes_home,
        workspace=args.workspace,
        timeout_seconds=args.timeout_seconds,
        session_id=session_id,
        prior_message_count=prior_message_count,
        runtime_diagnostic_path=args.runtime_diagnostic,
        scored_freeze_path=args.scored_freeze,
    )
    print(json.dumps(_public_summary(record), sort_keys=True))
    return 0 if record["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
