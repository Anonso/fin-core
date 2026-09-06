"""评分记录 needs_review → 裁决收件箱 producer 单测（D-051 追记 #3）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin_analyse.adjudication.producers import scan_needs_review
from scripts.consume_zsxq_capture_folder import _reconcile_instrument_needs_review_inbox


def _registry(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "instrument_scores.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _row(record_id: str, status: str) -> dict:
    return {"record_id": record_id, "code": "600000", "status": status}


def test_scan_missing_registry_means_zero_pending(tmp_path: Path) -> None:
    result = scan_needs_review(registry_path=tmp_path / "nope.jsonl")
    assert result.disposition == "SCANNED"
    assert result.pending == 0
    assert result.total == 0


def test_scan_counts_needs_review_only(tmp_path: Path) -> None:
    path = _registry(
        tmp_path,
        [_row("r1", "ok"), _row("r2", "needs_review"), _row("r3", "needs_review")],
    )
    result = scan_needs_review(registry_path=path)
    assert result.disposition == "SCANNED"
    assert result.pending == 2
    assert result.total == 3


def test_hook_opens_and_auto_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin_analyse.adjudication.inbox import open_default_inbox
    from fin_analyse.ingestion.instrument_scores import instrument_scores_path

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    kb = tmp_path / "kb"
    kb.mkdir(parents=True)
    monkeypatch.setattr(
        "scripts.consume_zsxq_capture_folder.default_knowledge_base_root",
        lambda: kb,
    )
    registry = instrument_scores_path(kb)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(_row("r1", "needs_review"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    _reconcile_instrument_needs_review_inbox()
    inbox = open_default_inbox()
    row = inbox.get("instrument.needs_review")
    assert row is not None
    assert row.status == "open"
    assert "1 条 needs_review" in row.title
    assert "600000" not in row.title  # 硬边界 3：标的不进推送面
    assert "manage_instrument_scores.py list" in (row.resolution_hint or "")

    # owner confirm → 清零 → auto-resolve
    registry.write_text(
        json.dumps(_row("r1", "ok"), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _reconcile_instrument_needs_review_inbox()
    row = inbox.get("instrument.needs_review")
    assert row is not None
    assert row.status == "resolved"
    assert row.resolve_source == "producer"
