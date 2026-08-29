"""Capture one bare-Codex turn for the R1 same-question A/B acceptance.

The direct arm runs in an owner-only empty workspace with no FIN context. It
uses the same fixed model, reasoning effort, web policy, sandbox and disabled
generic feature set as the production consultation runtime. Follow-up turns
resume the exact prior thread unless the campaign explicitly starts a new
session boundary.

Question text, answer text and raw runtime output remain owner-only. Stdout is
content-free metadata suitable for orchestration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fin_analyse.guo_teacher_research.codex_runtime import (
    _DISABLED_RUNTIME_FEATURES,
    _extract_consistent_thread_id,
    _valid_uuid_text,
)

_DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "fin-analyse"
_MODEL = "gpt-5.6-sol"
_REASONING_EFFORT = "xhigh"
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe_slug(value: str, *, name: str) -> str:
    if not _SLUG_RE.fullmatch(value):
        raise ValueError(f"{name} must be a safe non-empty slug")
    return value


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_private(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)
    os.chmod(path, 0o600)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _resolved_binary(codex_bin: str) -> Path | None:
    candidate = shutil.which(codex_bin)
    if candidate is None:
        return None
    return Path(candidate).resolve()


def _build_command(
    *,
    codex_bin: str,
    workspace: Path,
    answer_path: Path,
    thread_id: str | None,
    provider_flags: tuple[str, ...] = (),
) -> list[str]:
    command = [
        codex_bin,
        "exec",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "-c",
        f'model_reasoning_effort="{_REASONING_EFFORT}"',
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="live"',
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "-C",
        str(workspace),
    ]
    for feature in _DISABLED_RUNTIME_FEATURES:
        command.extend(("--disable", feature))
    for flag in provider_flags:
        command.extend(("-c", flag))
    command.extend(("-m", _MODEL, "-o", str(answer_path)))
    if thread_id is not None:
        command.extend(("resume", thread_id, "-"))
    else:
        command.append("-")
    return command


def _runtime_facts(stdout: str) -> tuple[str | None, bool, dict[str, Any]]:
    thread_id = _extract_consistent_thread_id(stdout)
    terminal = False
    usage: dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "turn.completed":
            continue
        terminal = True
        raw_usage = record.get("usage")
        if isinstance(raw_usage, dict):
            usage = raw_usage
    return thread_id, terminal, usage


def _child_env() -> dict[str, str]:
    env = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    # 与生产 consultation runtime 相同的网络路径：本地代理透传给 codex 子进程，
    # 否则原生 codex 直连 provider 端点会 SYN-SENT 挂起（direct-primary 已知问题）。
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "OPENAI_API_KEY"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        env["CODEX_HOME"] = codex_home
    return env


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _load_prior_thread_id(
    *, state_root: Path, campaign_id: str, chain_id: str, turn: int
) -> str | None:
    if turn == 1:
        return None
    campaign_id = _safe_slug(campaign_id, name="campaign_id")
    chain_id = _safe_slug(chain_id, name="chain_id")
    prior_records = sorted(
        (state_root / campaign_id / "direct" / chain_id).glob(f"turn-{turn - 1:02d}-*/record.json")
    )
    if len(prior_records) != 1:
        raise ValueError("later turns require exactly one completed prior turn record")
    prior = json.loads(prior_records[0].read_text(encoding="utf-8"))
    thread_id = prior.get("thread_id")
    if prior.get("status") != "completed" or not isinstance(thread_id, str):
        raise ValueError("prior turn did not complete with an exact thread id")
    if not _valid_uuid_text(thread_id):
        raise ValueError("prior turn thread id is invalid")
    return thread_id


def _public_summary(record: dict[str, Any]) -> dict[str, Any]:
    thread_id = record.get("thread_id")
    return {
        "status": record.get("status"),
        "campaign_id": record.get("campaign_id"),
        "chain_id": record.get("chain_id"),
        "turn": record.get("turn"),
        "item_id": record.get("item_id"),
        "failure_reasons": record.get("failure_reasons", []),
        "thread_id_sha256": _sha256_bytes(thread_id.encode("utf-8"))
        if isinstance(thread_id, str)
        else None,
    }


def _capture_one(
    *,
    question: str,
    state_root: Path,
    campaign_id: str,
    category: str,
    chain_id: str,
    turn: int,
    item_id: str,
    codex_bin: str,
    timeout_seconds: int,
    thread_id: str | None,
    fresh_session: bool = False,
    provider_flags: tuple[str, ...] = (),
) -> dict[str, Any]:
    campaign_id = _safe_slug(campaign_id, name="campaign_id")
    category = _safe_slug(category, name="category")
    chain_id = _safe_slug(chain_id, name="chain_id")
    item_id = _safe_slug(item_id, name="item_id")
    if not question.strip():
        raise ValueError("question must not be empty")
    if turn < 1:
        raise ValueError("turn must be >= 1")
    if turn == 1 and thread_id is not None:
        raise ValueError("turn 1 must be fresh")
    if turn > 1 and thread_id is None and not fresh_session:
        raise ValueError("later turns require an exact thread id or --fresh-session")
    if fresh_session and thread_id is not None:
        raise ValueError("a fresh session cannot resume a thread")

    campaign_dir = state_root / campaign_id
    workspaces_dir = campaign_dir / "direct-workspaces"
    workspace = workspaces_dir / chain_id
    direct_dir = campaign_dir / "direct"
    chain_dir = direct_dir / chain_id
    turn_dir = chain_dir / f"turn-{turn:02d}-{item_id}"
    if turn_dir.exists():
        raise FileExistsError(f"turn evidence already exists: {turn_dir}")
    for directory in (
        state_root,
        campaign_dir,
        workspaces_dir,
        workspace,
        direct_dir,
        chain_dir,
        turn_dir,
    ):
        _ensure_private_dir(directory)

    question_path = turn_dir / "question.txt"
    events_path = turn_dir / "events.jsonl"
    stderr_path = turn_dir / "stderr.txt"
    answer_path = turn_dir / "answer.txt"
    record_path = turn_dir / "record.json"
    _write_private(question_path, question)

    command = _build_command(
        codex_bin=codex_bin,
        workspace=workspace,
        answer_path=answer_path,
        thread_id=thread_id,
        provider_flags=provider_flags,
    )
    started_at = datetime.now(UTC)
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            input=question,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_child_env(),
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
    elapsed = time.monotonic() - started
    finished_at = datetime.now(UTC)

    _write_private(events_path, stdout)
    _write_private(stderr_path, stderr)
    if answer_path.exists():
        os.chmod(answer_path, 0o600)
    answer = answer_path.read_text(encoding="utf-8") if answer_path.exists() else ""
    returned_thread_id, terminal, usage = _runtime_facts(stdout)

    failure_reasons: list[str] = []
    if timed_out:
        failure_reasons.append("timeout")
    elif exit_code != 0:
        failure_reasons.append("nonzero_exit")
    if returned_thread_id is None:
        failure_reasons.append("missing_or_conflicting_thread")
    elif thread_id is not None and returned_thread_id != thread_id:
        failure_reasons.append("resume_thread_mismatch")
    if not terminal:
        failure_reasons.append("missing_turn_completed")
    if not answer.strip():
        failure_reasons.append("missing_final_answer")

    binary = _resolved_binary(codex_bin)
    record: dict[str, Any] = {
        "schema_version": "fin.r1-direct-arm-capture/v2",
        "campaign_id": campaign_id,
        "category": category,
        "chain_id": chain_id,
        "turn": turn,
        "item_id": item_id,
        "question_sha256": _sha256_bytes(question.encode("utf-8")),
        "model": _MODEL,
        "reasoning_effort": _REASONING_EFFORT,
        "web_search": "live",
        "sandbox": "read-only",
        "workspace": "owner_only_empty",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "wall_time_seconds": round(elapsed, 3),
        "timeout_seconds": timeout_seconds,
        "status": "completed" if not failure_reasons else "failed",
        "exit_code": exit_code,
        "failure_reasons": failure_reasons,
        "thread_id": returned_thread_id,
        "resumed_thread_id": thread_id,
        "session_start": thread_id is None,
        "turn_completed": terminal,
        "usage": usage,
        "codex_binary_sha256": _sha256_file(binary) if binary is not None else None,
        "question_file_sha256": _sha256_file(question_path),
        "events_sha256": _sha256_file(events_path),
        "stderr_sha256": _sha256_file(stderr_path),
        "answer_sha256": _sha256_file(answer_path) if answer_path.exists() else None,
    }
    _write_private(record_path, json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    question_group = parser.add_mutually_exclusive_group(required=True)
    question_group.add_argument("--question")
    question_group.add_argument("--question-file", type=Path)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--chain-id", required=True)
    parser.add_argument("--turn", type=int, required=True)
    parser.add_argument("--item-id", required=True)
    parser.add_argument("--state-root", type=Path, default=_DEFAULT_STATE_ROOT)
    parser.add_argument("--codex-bin", default="/home/ypk/.local/bin/codex")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--provider-flag",
        action="append",
        default=[],
        help="extra codex -c provider wiring flag (repeatable); direct arm provider transport",
    )
    parser.add_argument(
        "--fresh-session",
        action="store_true",
        help="start a new Codex thread for a later campaign turn",
    )
    args = parser.parse_args(argv)

    question = (
        args.question
        if args.question is not None
        else args.question_file.read_text(encoding="utf-8")
    )
    thread_id = (
        None
        if args.fresh_session
        else _load_prior_thread_id(
            state_root=args.state_root,
            campaign_id=args.campaign_id,
            chain_id=args.chain_id,
            turn=args.turn,
        )
    )
    record = _capture_one(
        question=question,
        state_root=args.state_root,
        campaign_id=args.campaign_id,
        category=args.category,
        chain_id=args.chain_id,
        turn=args.turn,
        item_id=args.item_id,
        codex_bin=args.codex_bin,
        timeout_seconds=args.timeout_seconds,
        thread_id=thread_id,
        fresh_session=args.fresh_session,
        provider_flags=tuple(args.provider_flag),
    )
    print(json.dumps(_public_summary(record), sort_keys=True))
    return 0 if record["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
