from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from fin_analyse.common.bounded_process import run_bounded_command


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_output_limit_kills_the_exact_process_group_and_descendant(
    tmp_path: Path,
    stream_name: str,
) -> None:
    maximum = 4096
    child_pid_path = tmp_path / "child.pid"
    script = (
        "import os, pathlib, sys, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    while True: time.sleep(1)\n"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child))\n"
        f"stream = sys.{stream_name}.buffer\n"
        f"stream.write(b'x' * {maximum + 1})\n"
        "stream.flush()\n"
        "time.sleep(5)\n"
    )

    with pytest.raises(
        RuntimeError,
        match="^bounded_process_output_limit_exceeded$",
    ):
        run_bounded_command(
            ("/usr/bin/python3", "-c", script),
            cwd=tmp_path,
            env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
            timeout=2,
            max_output_bytes=maximum,
        )

    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 1
    while _process_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_exists(child_pid)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_kills_the_exact_process_group_and_descendant(tmp_path: Path) -> None:
    """挂起命令（vsock/interop 故障的等价物）在 timeout 内 TimeoutExpired，组内后代同杀。"""
    child_pid_path = tmp_path / "child.pid"
    script = (
        "import os, pathlib, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    while True: time.sleep(1)\n"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded_command(
            ("/usr/bin/python3", "-c", script),
            cwd=tmp_path,
            env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
            timeout=2,
            max_output_bytes=4096,
        )
    elapsed = time.monotonic() - started
    # deadline 在 Popen 前设定（含 spawn 开销），wait 循环 10ms 轮询；
    # TimeoutExpired 在 elapsed≈timeout 确定性触发。1.5× 预算锁住
    # 「在 timeout 内快速失败」语义，同时保留 CI 抖动余量。
    assert elapsed < 3.0
    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 1
    while _process_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_exists(child_pid)

