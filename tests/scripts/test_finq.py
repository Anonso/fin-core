"""finq 入口行为：help 旗标只出用法不落账（家规规则 10：账本只收真实使用）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

FINQ = Path(__file__).resolve().parents[2] / "scripts" / "finq"


def _run_finq(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(FINQ), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _log_path(tmp_path: Path) -> Path:
    return tmp_path / "fin-analyse" / "usage-log" / "usage.jsonl"


def test_help_flags_print_usage_without_logging(tmp_path: Path) -> None:
    for flag in ("--help", "-h", "help"):
        proc = _run_finq(tmp_path, flag)
        assert proc.returncode == 0, proc.stderr
        assert "用法" in proc.stdout
        assert not _log_path(tmp_path).exists()


def test_no_args_prints_usage_without_logging(tmp_path: Path) -> None:
    proc = _run_finq(tmp_path)
    assert proc.returncode == 64
    assert not _log_path(tmp_path).exists()


def test_invalid_satisfaction_rejected_without_logging(tmp_path: Path) -> None:
    proc = _run_finq(tmp_path, "测试问题", "maybe")
    assert proc.returncode == 64
    assert not _log_path(tmp_path).exists()


def test_question_log_is_appended(tmp_path: Path) -> None:
    proc = _run_finq(tmp_path, "测试问题", "y")
    assert proc.returncode == 0, proc.stderr
    records = [
        json.loads(line)
        for line in _log_path(tmp_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 1
    assert records[0]["q"] == "测试问题"
    assert records[0]["satisfied"] == "y"
    assert records[0]["not_best"] is None
