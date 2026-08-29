"""Stall watchdog: partial-output snapshots and early progress marker.

Covers codex-probe-failure-evidence-20260821 slice step 1:
- timeout/stall exceptions carry bounded stdout/stderr tails, total byte
  counts and full-stream sha256 (N8: total/tail/truncated semantics);
- optional early progress check: kills the child when the marker stays
  false beyond the window (latch once seen), default is a no-op.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time

import pytest

from fin_analyse.common.stall_watchdog import (
    StallError,
    run_with_stall_watchdog,
)


def _child(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def _sleep_child(seconds: float) -> list[str]:
    return _child(f"import time; time.sleep({seconds})")


# ── early progress marker ───────────────────────────────────────────────


def test_early_progress_never_true_kills_within_window(tmp_path) -> None:
    marker = tmp_path / "marker"
    started = time.monotonic()
    with pytest.raises(StallError):
        run_with_stall_watchdog(
            _sleep_child(10),
            timeout=30.0,
            stall_seconds=60.0,
            early_progress_check=lambda: marker.exists(),
            early_progress_seconds=1.0,
        )
    elapsed = time.monotonic() - started
    # 窗口 1s + 轮询 0.5s 粒度,必须远小于 stall(60s)/timeout(30s)
    assert elapsed < 5.0


def test_early_progress_latches_after_marker(tmp_path) -> None:
    marker = tmp_path / "marker"
    state = {"created": False}

    def check() -> bool:
        if not state["created"]:
            state["created"] = True
            marker.write_text("x")
        return marker.exists()

    result = run_with_stall_watchdog(
        _sleep_child(0.4),
        timeout=30.0,
        stall_seconds=60.0,
        early_progress_check=check,
        early_progress_seconds=10.0,
    )
    assert result.returncode == 0


def test_early_progress_default_is_noop() -> None:
    # 默认参数(None/0.0)行为与历史一致:不检查 marker,不提前杀。
    result = run_with_stall_watchdog(
        _sleep_child(0.3),
        timeout=30.0,
        stall_seconds=60.0,
    )
    assert result.returncode == 0


def test_early_progress_latch_does_not_recheck_after_seen(tmp_path) -> None:
    # marker 出现后即使文件被删除也不再检查(永久 latch)。
    marker = tmp_path / "marker"
    marker.write_text("x")
    seen = {"n": 0}

    def check() -> bool:
        seen["n"] += 1
        return marker.exists()

    result = run_with_stall_watchdog(
        _sleep_child(0.4),
        timeout=30.0,
        stall_seconds=60.0,
        early_progress_check=check,
        early_progress_seconds=1.0,
    )
    assert result.returncode == 0
    assert seen["n"] == 1  # latch:只检查一次


# ── partial output snapshots ─────────────────────────────────────────────


def test_timeout_carries_partial_output_snapshot() -> None:
    child = _child(
        "import sys, time;"
        "sys.stdout.write('hello-out'); sys.stdout.flush();"
        "sys.stderr.write('err-line'); sys.stderr.flush();"
        "time.sleep(10)"
    )
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        run_with_stall_watchdog(child, timeout=1.0, stall_seconds=60.0)
    error = excinfo.value
    assert b"hello-out" in error.stdout_tail
    assert b"err-line" in error.stderr_tail
    assert error.stdout_total_bytes >= 9
    assert error.stderr_total_bytes >= 8
    assert hashlib.sha256(b"hello-out").hexdigest() == error.stdout_sha256
    assert error.truncated is False


def test_stall_carries_partial_output_snapshot() -> None:
    child = _child(
        "import sys, time;"
        "sys.stdout.write('hello-out'); sys.stdout.flush();"
        "time.sleep(10)"
    )
    with pytest.raises(StallError) as excinfo:
        run_with_stall_watchdog(child, timeout=30.0, stall_seconds=1.0)
    error = excinfo.value
    assert b"hello-out" in error.stdout_tail
    assert error.stdout_total_bytes >= 9
    assert error.stdout_sha256 == hashlib.sha256(b"hello-out").hexdigest()
    assert error.stderr_total_bytes == 0


def test_tail_truncated_beyond_bound() -> None:
    # 输出超过 tail 上限时:truncated=True,hash 仍为完整流。
    chunk = b"x" * 8192
    child = _child(
        f"import sys, time; sys.stdout.buffer.write({chunk!r}); "
        "sys.stdout.flush(); time.sleep(10)"
    )
    with pytest.raises(StallError) as excinfo:
        run_with_stall_watchdog(child, timeout=30.0, stall_seconds=1.0)
    error = excinfo.value
    assert error.truncated is True
    assert len(error.stdout_tail) <= 4096
    assert error.stdout_total_bytes == 8192
    assert error.stdout_sha256 == hashlib.sha256(chunk).hexdigest()


def test_successful_run_has_no_exception_snapshot() -> None:
    result = run_with_stall_watchdog(
        _child("import sys; sys.stdout.write('ok')"),
        timeout=30.0,
        stall_seconds=60.0,
    )
    assert result.returncode == 0
    assert result.stdout == "ok"
