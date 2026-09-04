"""Handler-level structural rules for the two decision journal tools.

The custom handlers own their own param validation (no ProductionReadRequest
whitelist); these tests pin the structural rules that guard every action:
which params may travel with which action, and that semantic filter
rejection (unresolvable symbol) surfaces as a REJECTED reason code instead
of a swallowed failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin_analyse.read_capabilities import server as srv


@pytest.fixture()
def journal_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal real knowledge root + isolated state + initialized server."""
    kb = tmp_path / "knowledge-base"
    pinned = kb / "runtime" / "agent_context" / "pinned_sources.jsonl"
    pinned.parent.mkdir(parents=True)
    pinned.write_text(
        json.dumps(
            {
                "pinned_id": "p1",
                "agent_id": "guo_teacher",
                "source_scope": "g_source",
                "processing_status": "ready",
                "processed_at": "2026-08-01T08:00:00+08:00",
                "processed_title": "t",
                "guidance_brief": "b",
                "theme_clusters": [],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    import os

    state_root = tmp_path / "state" / "fin-analyse" / "semantic-research-v1"
    state_root.mkdir(parents=True)
    os.chmod(state_root, 0o700)
    identity = state_root / "installation-identity.hex"
    identity.write_text("ab" * 32 + "\n", encoding="ascii")
    os.chmod(identity, 0o600)
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "HOME": str(tmp_path / "home"),
    }
    srv.initialize(kb, trace_path=tmp_path / "trace" / "calls.jsonl", environ=env)


def _read_handler():
    return srv._make_decision_journal_read_handler()  # noqa: SLF001


def _record_handler():
    return srv._make_record_decision_handler()  # noqa: SLF001


def test_preview_rejects_limit(journal_server: None) -> None:
    with pytest.raises(srv.InvalidParamsError):
        _record_handler()(
            action="preview",
            decision_type="buy",
            rationale="x",
            limit=5,
        )


def test_list_rejects_token(journal_server: None) -> None:
    with pytest.raises(srv.InvalidParamsError):
        _record_handler()(action="list", token="abc")


def test_apply_rejects_decision_fields(journal_server: None) -> None:
    with pytest.raises(srv.InvalidParamsError):
        _record_handler()(action="apply", token="abc", rationale="x")


def test_read_rejects_unknown_decision_type(journal_server: None) -> None:
    with pytest.raises(srv.InvalidParamsError):
        _read_handler()(question="", decision_type="other")


def test_read_unresolvable_symbol_is_typed_rejection(
    journal_server: None,
) -> None:
    result = _read_handler()(question="", symbol="不存在的票")
    assert result["value"]["status"] == "REJECTED"
    assert "decision_journal_symbol_unresolved" in result["data_gaps"]


def test_list_with_filters_roundtrip(journal_server: None) -> None:
    handler = _record_handler()
    preview = handler(
        action="preview",
        decision_type="buy",
        symbol="600519",
        rationale="测试记录",
    )
    assert preview["value"]["status"] == "PREVIEW_READY"
    applied = handler(
        action="apply", token=preview["value"]["candidate_token"]
    )
    assert applied["value"]["status"] == "APPLIED"
    listed = handler(action="list", symbol="600519")  # 裸代码过滤命中
    assert listed["value"]["count"] == 1
    assert listed["data_gaps"] == []
