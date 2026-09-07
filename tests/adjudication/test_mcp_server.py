"""fin-adjudication MCP server handler 单测（对齐 decision_journal_handlers 先例）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fin_analyse.adjudication import mcp_server as srv
from fin_analyse.adjudication.inbox import AdjudicationItem
from fin_analyse.ingestion.instrument_scores import instrument_scores_path


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    kb = tmp_path / "kb"
    kb.mkdir(parents=True)
    monkeypatch.setenv("FIN_KNOWLEDGE_BASE_ROOT", str(kb))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("FIN_DAILY_WORKSPACE_DELIVERY_TARGET", raising=False)
    return kb


def _registry(kb: Path, rows: list[dict]) -> None:
    path = instrument_scores_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _read_registry(kb: Path) -> list[dict]:
    path = instrument_scores_path(kb)
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _audit_lines() -> list[dict]:
    from fin_analyse.runtime.state_roots import adjudication_inbox_state_root

    path = adjudication_inbox_state_root() / srv.AUDIT_FILE_NAME
    if not path.exists():
        return []  # 无变更动作 = 无审计文件
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_adjudication_list_empty_then_seeded(env: Path) -> None:
    handler = srv._make_adjudication_list_handler()
    assert handler() == {"ok": True, "items": []}
    open_default_inbox = srv.open_default_inbox
    open_default_inbox().reconcile(
        AdjudicationItem(item_id="x.1", kind="k", title="待决", resolution_hint="怎么裁"),
        pending=True,
    )
    out = handler()
    assert out["ok"] is True
    assert out["items"][0]["item_id"] == "x.1"
    assert out["items"][0]["resolution_hint"] == "怎么裁"


def test_done_resolves_and_audits(env: Path) -> None:
    handler = srv._make_adjudication_done_handler()
    inbox = srv.open_default_inbox()
    inbox.reconcile(AdjudicationItem(item_id="x.1", kind="k", title="t"), pending=True)
    out = handler("x.1", note="已入档")
    assert out == {"ok": True, "status": "done", "item_id": "x.1"}
    assert inbox.get("x.1").status == "resolved"
    assert inbox.get("x.1").resolve_source == "owner"
    audit = _audit_lines()
    assert audit[0]["action"] == "done"
    assert audit[0]["via"] == "mcp"
    assert audit[0]["note"] == "已入档"


def test_done_not_open_typed_error(env: Path) -> None:
    handler = srv._make_adjudication_done_handler()
    out = handler("ghost")
    assert out["ok"] is False
    assert out["error"] == "adjudication_item_not_open"
    assert _audit_lines() == []  # 失败动作不审计


def test_score_list_pagination_and_clamp(env: Path) -> None:
    _registry(
        env,
        [
            {"record_id": f"r{i}", "code": "600000", "name": f"N{i}",
             "status": "needs_review", "article_date": f"2026-09-0{i + 1}",
             "review_reason": "missing_fields:x"}
            for i in range(3)
        ]
        + [{"record_id": "ok1", "code": "600000", "status": "ok"}],
    )
    handler = srv._make_score_list_handler()
    out = handler(limit=2)
    assert out["ok"] is True
    assert out["pending_total"] == 3
    assert [row["record_id"] for row in out["rows"]] == ["r2", "r1"]  # 日期降序
    page2 = handler(limit=2, offset=2)
    assert [row["record_id"] for row in page2["rows"]] == ["r0"]
    assert handler(limit=999)["rows"] == handler(limit=50)["rows"]  # 钳制


def test_score_confirm_flow_and_idempotence(env: Path) -> None:
    _registry(
        env,
        [{"record_id": "r1", "code": "600000", "status": "needs_review",
          "review_reason": "cross_source_conflict"}],
    )
    handler = srv._make_score_confirm_handler()
    assert handler("ghost")["error"] == "record_not_found"
    out = handler("r1", note="核对无误")
    assert out == {"ok": True, "status": "confirmed", "record_id": "r1"}
    assert _read_registry(env)[0]["status"] == "ok"
    assert _read_registry(env)[0]["review_reason"] is None
    assert handler("r1")["status"] == "already_ok"
    # 审计只记变更：confirmed 一条，already_ok 是 no-op 不记
    audit = _audit_lines()
    assert [line["action"] for line in audit] == ["score_confirm"]


def test_score_drop_removes_row_and_audits(env: Path) -> None:
    _registry(
        env,
        [
            {"record_id": "r1", "status": "needs_review"},
            {"record_id": "r2", "status": "ok"},
        ],
    )
    handler = srv._make_score_drop_handler()
    assert handler("ghost")["error"] == "record_not_found"
    out = handler("r1", note="残行剔除")
    assert out["status"] == "dropped"
    remaining = _read_registry(env)
    assert [row["record_id"] for row in remaining] == ["r2"]
    assert _audit_lines()[0]["action"] == "score_drop"


def test_digest_send_missing_target_typed(env: Path) -> None:
    handler = srv._make_digest_send_handler()
    out = handler()
    assert out["ok"] is False
    assert out["error"] == "digest_target_missing"


def test_registry_closed_set_registered() -> None:
    """工具闭集 = 契约冻结面：多工具/少工具都算契约漂移。"""

    expected = {
        "adjudication_list",
        "adjudication_done",
        "score_list",
        "score_get",
        "score_confirm",
        "score_drop",
        "digest_send",
    }
    import asyncio

    registered = asyncio.run(srv.mcp.list_tools())
    assert {tool.name for tool in registered} == expected


def test_score_id_unique_prefix_resolution(env: Path) -> None:
    """飞书手打 64 位 hex：唯一前缀可代全 id；歧义前缀 typed 拒绝（契约附注）。"""

    _registry(
        env,
        [
            {"record_id": "ab11" * 16, "status": "needs_review"},
            {"record_id": "ab22" * 16, "status": "needs_review"},
            {"record_id": "cc" * 32, "status": "needs_review"},
        ],
    )
    confirm = srv._make_score_confirm_handler()
    assert confirm("ab")["error"] == "record_id_ambiguous"  # 两条共享前缀
    out = confirm("ab1")
    assert out["ok"] is True and out["record_id"] == "ab11" * 16
    assert confirm("zzz")["error"] == "record_not_found"
    drop = srv._make_score_drop_handler()
    assert drop("cc")["status"] == "dropped"


def test_digest_send_failure_typed(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hermes 缺失/超时/非零退出 = DailyWorkspaceDeliveryError → typed（评审 P1）。"""

    from fin_analyse.operations.daily_workspace_delivery import (
        DailyWorkspaceDeliveryError,
    )

    monkeypatch.setenv("FIN_DAILY_WORKSPACE_DELIVERY_TARGET", "feishu:ctl")

    class _BrokenSender:
        def __init__(self, *, target: str, timeout_seconds: float = 30.0) -> None:
            raise DailyWorkspaceDeliveryError("DAILY_WORKSPACE_DELIVERY_OUTCOME_UNKNOWN")

    import fin_analyse.operations.daily_workspace_delivery as delivery

    monkeypatch.setattr(delivery, "HermesCliMessageSender", _BrokenSender)
    out = srv._make_digest_send_handler()()
    assert out["ok"] is False
    assert out["error"] == "digest_send_failed"


def test_stdout_guard_behavior() -> None:
    """guard 契约：write 进 fd2 不抛不涨 stdout、flush 静默、属性透传。"""

    import sys as _sys

    guard = srv._StdoutGuard()
    assert guard.write("ok") == 2  # 字节数
    guard.write(b"bytes-too")
    guard.flush()  # 不得抛
    assert guard.encoding == _sys.__stderr__.encoding  # __getattr__ 透传


def test_startup_missing_kb_root_fails_closed(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIN_KNOWLEDGE_BASE_ROOT", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        srv._kb_root()
    assert excinfo.value.code == 2


def test_stdio_roundtrip_lists_tool_closed_set(env: Path) -> None:
    """真子进程 stdio：initialize + tools/list 必须回到 6 工具闭集（评审 P2）。"""

    import json as _json
    import subprocess
    import sys as _sys

    proc = subprocess.Popen(
        [_sys.executable, "-m", "fin_analyse.adjudication.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**dict(os.environ)},
    )
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
        proc.stdin.write(_json.dumps(request) + "\n")
        proc.stdin.write(_json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.write(
            _json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
        )
        proc.stdin.flush()
        names: set[str] = set()
        for _ in range(3):
            line = proc.stdout.readline()
            if not line:
                break
            payload = _json.loads(line)
            if payload.get("id") == 2:
                names = {tool["name"] for tool in payload["result"]["tools"]}
                break
        assert names == {
            "adjudication_list",
            "adjudication_done",
            "score_list",
            "score_get",
            "score_confirm",
            "score_drop",
            "digest_send",
        }
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_score_get_returns_full_record_with_conflict_detail(env: Path) -> None:
    """v1.1：冲突详情必须可取（owner 经 Hermes 裁决的先决条件）。"""

    detail = [
        {"origin": "article_md.body", "lihao": 9.5, "consensus": 6.6},
        {"origin": "article_md.image_desc_section", "lihao": 7.6, "consensus": 6.8},
    ]
    _registry(
        env,
        [
            {"record_id": "conf1", "code": "000960", "name": "锡业股份",
             "status": "needs_review", "review_reason": "cross_source_conflict",
             "conflict_detail": detail},
            {"record_id": "okrow", "code": "600000", "status": "ok"},
        ],
    )
    handler = srv._make_score_get_handler()
    out = handler("conf")
    assert out["ok"] is True
    assert out["record"]["conflict_detail"] == detail
    assert out["record"]["code"] == "000960"
    assert handler("zzz")["error"] == "record_not_found"
