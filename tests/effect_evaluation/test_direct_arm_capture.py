from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.effect_evaluation.direct_arm_capture import (
    _build_command,
    _capture_one,
    _child_env,
    _load_prior_thread_id,
    _public_summary,
    main,
)

_THREAD_ID = "019fc2fe-7ea6-7e32-a20b-357f21429486"


def _completed_stdout(thread_id: str = _THREAD_ID) -> str:
    return "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": thread_id}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}),
            json.dumps({"type": "turn.completed", "usage": {"output_tokens": 12}}),
        )
    )


def test_build_command_is_isolated_sol_xhigh_and_reads_prompt_from_stdin(tmp_path: Path) -> None:
    answer_path = tmp_path / "answer.txt"
    command = _build_command(
        codex_bin="/opt/codex",
        workspace=tmp_path / "empty",
        answer_path=answer_path,
        thread_id=None,
    )

    assert command[:2] == ["/opt/codex", "exec"]
    assert "gpt-5.6-sol" in command
    assert 'model_reasoning_effort="xhigh"' in command
    assert 'web_search="live"' in command
    assert 'approval_policy="never"' in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--strict-config" in command
    assert "--skip-git-repo-check" in command
    assert command[command.index("-s") + 1] == "read-only"
    assert command[command.index("-C") + 1] == str(tmp_path / "empty")
    assert command[command.index("-o") + 1] == str(answer_path)
    assert "resume" not in command


def test_child_env_passes_local_proxy_for_provider_connectivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """direct 臂必须与生产同网络路径：本地代理透传给 codex 子进程。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    child = _child_env()
    assert child["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert child["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert child["NO_PROXY"] == "localhost,127.0.0.1"


def test_build_command_uses_exact_resume_id(tmp_path: Path) -> None:
    command = _build_command(
        codex_bin="codex",
        workspace=tmp_path / "empty",
        answer_path=tmp_path / "answer.txt",
        thread_id=_THREAD_ID,
    )

    assert command[-3:] == ["resume", _THREAD_ID, "-"]
    assert "--last" not in command
    assert "--ephemeral" not in command


def test_capture_writes_separate_owner_only_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        Path(command[command.index("-o") + 1]).write_text("最终回答", encoding="utf-8")
        assert kwargs["input"] == "测试问题"
        return SimpleNamespace(returncode=0, stdout=_completed_stdout(), stderr="diagnostic")

    monkeypatch.setattr("subprocess.run", fake_run)
    record = _capture_one(
        question="测试问题",
        state_root=tmp_path,
        campaign_id="campaign-1",
        category="company",
        chain_id="chain-2",
        turn=1,
        item_id="company-01",
        codex_bin="codex",
        timeout_seconds=30,
        thread_id=None,
    )

    assert record["status"] == "completed"
    assert record["thread_id"] == _THREAD_ID
    assert record["model"] == "gpt-5.6-sol"
    assert record["reasoning_effort"] == "xhigh"
    assert record["question_sha256"]
    turn_dir = tmp_path / "campaign-1" / "direct" / "chain-2" / "turn-01-company-01"
    assert (turn_dir / "question.txt").read_text(encoding="utf-8") == "测试问题"
    assert (turn_dir / "answer.txt").read_text(encoding="utf-8") == "最终回答"
    assert "thread.started" in (turn_dir / "events.jsonl").read_text(encoding="utf-8")
    assert (turn_dir / "stderr.txt").read_text(encoding="utf-8") == "diagnostic"
    campaign_dir = tmp_path / "campaign-1"
    for directory in (
        campaign_dir,
        campaign_dir / "direct-workspaces",
        campaign_dir / "direct-workspaces" / "chain-2",
        campaign_dir / "direct",
        campaign_dir / "direct" / "chain-2",
        turn_dir,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for path in turn_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected_reason"),
    (
        (7, _completed_stdout(), "nonzero_exit"),
        (
            0,
            json.dumps({"type": "thread.started", "thread_id": _THREAD_ID}),
            "missing_turn_completed",
        ),
        (
            0,
            "\n".join(
                (
                    _completed_stdout(),
                    json.dumps(
                        {
                            "type": "thread.started",
                            "thread_id": "019fc2fe-7ea6-7e32-a20b-357f21429487",
                        }
                    ),
                )
            ),
            "missing_or_conflicting_thread",
        ),
    ),
)
def test_capture_fails_closed_on_invalid_runtime_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    expected_reason: str,
) -> None:
    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        Path(command[command.index("-o") + 1]).write_text("回答", encoding="utf-8")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    record = _capture_one(
        question="测试问题",
        state_root=tmp_path,
        campaign_id="campaign-1",
        category="company",
        chain_id="chain-2",
        turn=1,
        item_id=f"case-{expected_reason}",
        codex_bin="codex",
        timeout_seconds=30,
        thread_id=None,
    )

    assert record["status"] == "failed"
    assert expected_reason in record["failure_reasons"]


def test_capture_rejects_resume_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    returned = "019fc2fe-7ea6-7e32-a20b-357f21429487"

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        Path(command[command.index("-o") + 1]).write_text("回答", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=_completed_stdout(returned), stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    record = _capture_one(
        question="继续追问",
        state_root=tmp_path,
        campaign_id="campaign-1",
        category="company",
        chain_id="chain-2",
        turn=2,
        item_id="company-02",
        codex_bin="codex",
        timeout_seconds=30,
        thread_id=_THREAD_ID,
    )

    assert record["status"] == "failed"
    assert "resume_thread_mismatch" in record["failure_reasons"]


def test_capture_refuses_to_overwrite_existing_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        Path(command[command.index("-o") + 1]).write_text("回答", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=_completed_stdout(), stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    kwargs = {
        "question": "测试问题",
        "state_root": tmp_path,
        "campaign_id": "campaign-1",
        "category": "company",
        "chain_id": "chain-2",
        "turn": 1,
        "item_id": "company-01",
        "codex_bin": "codex",
        "timeout_seconds": 30,
        "thread_id": None,
    }
    _capture_one(**kwargs)
    with pytest.raises(FileExistsError):
        _capture_one(**kwargs)


def test_prior_thread_is_loaded_privately_and_stdout_summary_only_contains_hash(
    tmp_path: Path,
) -> None:
    prior_dir = tmp_path / "campaign-1" / "direct" / "chain-2" / "turn-01-company-01"
    prior_dir.mkdir(parents=True)
    (prior_dir / "record.json").write_text(
        json.dumps({"status": "completed", "thread_id": _THREAD_ID}),
        encoding="utf-8",
    )

    assert (
        _load_prior_thread_id(
            state_root=tmp_path,
            campaign_id="campaign-1",
            chain_id="chain-2",
            turn=2,
        )
        == _THREAD_ID
    )
    summary = _public_summary(
        {
            "status": "completed",
            "campaign_id": "campaign-1",
            "chain_id": "chain-2",
            "turn": 2,
            "item_id": "company-02",
            "thread_id": _THREAD_ID,
            "failure_reasons": [],
        }
    )
    assert "thread_id" not in summary
    assert summary["thread_id_sha256"]
    assert _THREAD_ID not in json.dumps(summary)


def test_cli_can_start_a_new_session_on_a_later_campaign_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        Path(command[command.index("-o") + 1]).write_text("新会话回答", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=_completed_stdout(), stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    exit_code = main(
        [
            "--question",
            "上次结论是什么？",
            "--campaign-id",
            "campaign-1",
            "--category",
            "continuity",
            "--chain-id",
            "chain-5",
            "--turn",
            "4",
            "--item-id",
            "continuity-04",
            "--state-root",
            str(tmp_path),
            "--fresh-session",
        ]
    )

    assert exit_code == 0
    record_path = (
        tmp_path / "campaign-1" / "direct" / "chain-5" / "turn-04-continuity-04" / "record.json"
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["session_start"] is True
    assert record["resumed_thread_id"] is None
