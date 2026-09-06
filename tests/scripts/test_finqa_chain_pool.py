"""finqa_chain 连接池分层单测（docs/design/llm-pool-layering.md 步2 验收）。

覆盖:_resolve_node 三态(引用成功/悬空/池禁/managed:self)、钉腿拒绝
(无头与语义层)、链序池禁横幅跳过。不发起真实 harness 调用。
"""

from __future__ import annotations

import sys

import pytest

from scripts.finqa_chain import _resolve_node, main

_POOL = {
    "zcode": {
        "type": "harness",
        "harness": "zcode",
        "model": "zhipu/glm-5.3",
        "auth_note": "x",
        "enabled": True,
    },
    "commandcode-pro": {
        "type": "harness",
        "harness": "commandcode",
        "model": "deepseek/deepseek-v4-pro",
        "auth_note": "x",
        "enabled": True,
    },
    "codex-opencode": {
        "type": "harness",
        "harness": "codex",
        "model": "deepseek-v4-pro",
        "auth_note": "x",
        "enabled": False,
    },
    "claude-cc": {
        "type": "harness",
        "harness": "claude",
        "model": "glm-5.3",
        "auth_note": "CC 内部",
        "managed": "self",
    },
}


def test_resolve_merges_harness_and_model_from_pool() -> None:
    node, failure = _resolve_node(
        {"id": "zcode", "conn_ref": "zcode", "enabled": True}, _POOL
    )

    assert failure == ""
    assert node is not None
    assert node["harness"] == "zcode"
    assert node["model"] == "zhipu/glm-5.3"


def test_resolve_unresolved_conn_ref_and_pool_disabled_are_distinct_failures() -> None:
    node, failure = _resolve_node({"id": "x", "conn_ref": "nope"}, _POOL)
    assert node is None
    assert failure == "conn_ref unresolved: nope"

    node, failure = _resolve_node({"id": "codex", "conn_ref": "codex-opencode"}, _POOL)
    assert node is None
    assert failure == "pool disabled: codex-opencode"


def test_resolve_managed_self_needs_no_enabled_switch() -> None:
    node, failure = _resolve_node({"id": "cc", "conn_ref": "claude-cc"}, _POOL)

    assert failure == ""
    assert node is not None
    assert node["model"] == "glm-5.3"


def test_pinned_leg_refused_on_pool_disable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys, "argv", ["finqa", "--node", "codex", "只回复两个字：就绪"]
    )
    monkeypatch.setattr(
        "scripts.finqa_chain._load_nodes",
        lambda: [{"id": "codex", "conn_ref": "codex-opencode", "enabled": True}],
    )
    monkeypatch.setattr("scripts.finqa_chain._load_pool", lambda: _POOL)

    rc = main()

    assert rc == 2  # 钉腿是显式意图:池禁=拒绝(rc≠0),不静默换腿
    assert "pinned leg refused" in capsys.readouterr().err


def test_chain_skips_pool_disabled_leg_with_banner(monkeypatch, capsys) -> None:
    seen: list[list[dict]] = []
    monkeypatch.setattr(sys, "argv", ["finqa", "只回复两个字：就绪"])
    monkeypatch.setattr(
        "scripts.finqa_chain._load_nodes",
        lambda: [
            {"id": "codex", "conn_ref": "codex-opencode", "enabled": True},
            {"id": "commandcode-flash", "conn_ref": "commandcode-flash", "enabled": False},
            {"id": "cmd", "conn_ref": "commandcode-pro", "enabled": True},
        ],
    )
    monkeypatch.setattr("scripts.finqa_chain._load_pool", lambda: _POOL)

    def fake_answer(nodes, questions, timeout_seconds):
        seen.append(nodes)
        return 0

    monkeypatch.setattr("scripts.finqa_chain._answer_via_legs", fake_answer)

    rc = main()

    assert rc == 0
    assert [n["id"] for n in seen[0]] == ["cmd"]  # 池禁腿跳过;节点级禁用测试腿静默不入链
    err = capsys.readouterr().err
    assert "POOL-DISABLED skip codex" in err


def test_chain_never_lets_test_leg_carry_production_traffic(monkeypatch, capsys) -> None:
    """回归钉子（2026-09-06 活体探针抓到）：池禁生产腿后，节点级 enabled:false
    的测试腿不得混入链序接住生产流量。"""
    seen: list[list[dict]] = []
    monkeypatch.setattr(sys, "argv", ["finqa", "只回复两个字：就绪"])
    monkeypatch.setattr(
        "scripts.finqa_chain._load_nodes",
        lambda: [
            {"id": "zcode", "conn_ref": "zcode", "enabled": True},
            {"id": "commandcode-flash", "conn_ref": "commandcode-flash", "enabled": False},
        ],
    )
    disabled = {**_POOL, "zcode": {**_POOL["zcode"], "enabled": False}}
    monkeypatch.setattr("scripts.finqa_chain._load_pool", lambda: disabled)

    def fake_answer(nodes, questions, timeout_seconds):
        seen.append(nodes)
        return 78  # 链空/耗尽也如实透传

    monkeypatch.setattr("scripts.finqa_chain._answer_via_legs", fake_answer)

    rc = main()

    assert rc == 78
    assert seen[0] == []  # 测试腿不入链:宁可耗尽,不给生产流量假答案
    assert "POOL-DISABLED skip zcode" in capsys.readouterr().err
