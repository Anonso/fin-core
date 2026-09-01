"""BUG-014 回归：priority outbox 追加路径必须收敛回 owner-only 0600。"""

from __future__ import annotations

import os
import stat

from fin_analyse.cognition.priority_articles import _append_jsonl


def test_append_jsonl_tightens_existing_loose_file(tmp_path) -> None:
    path = tmp_path / "priority_events.jsonl"
    path.write_text('{"a": 1}\n', encoding="utf-8")
    os.chmod(path, 0o664)

    assert _append_jsonl(path, {"b": 2}) is True

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == '{"b": 2}'


def test_append_jsonl_new_file_is_owner_only(tmp_path) -> None:
    path = tmp_path / "priority_analysis_jobs.jsonl"

    assert _append_jsonl(path, {"c": 3}) is True

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
