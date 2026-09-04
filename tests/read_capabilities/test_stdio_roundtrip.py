"""Real stdio JSON-RPC round-trip test for the thin server (design §7.4).

Boots ``python -m fin_analyse.read_capabilities.server`` as a subprocess
against a temp knowledge root, drives initialize → tools/list →
tools/call over real stdin/stdout frames, and asserts the trace lands in
``~/fin-data/trace/read-capability/calls.jsonl`` under an isolated HOME.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def smoke_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    kb = tmp_path / "knowledge-base"
    (kb / "runtime" / "agent_context").mkdir(parents=True)
    (kb / "runtime" / "agent_context" / "pinned_sources.jsonl").write_text(
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
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "config").mkdir()
    env = {
        **os.environ,
        "FIN_KNOWLEDGE_BASE_ROOT": str(kb),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    return home, env


class TestStdioRoundTrip:
    def test_initialize_list_call_trace(self, smoke_env: tuple[Path, dict[str, str]]) -> None:
        home, env = smoke_env
        proc = subprocess.Popen(
            [
                str(_REPO_ROOT / ".venv" / "bin" / "python"),
                "-m",
                "fin_analyse.read_capabilities.server",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=_REPO_ROOT,
            env=env,
        )
        try:
            frames = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "read_actual_portfolio",
                        "arguments": {"question": "roundtrip 我的持仓"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "read_g_context",
                        "arguments": {"question": "roundtrip 老师最近怎么看"},
                    },
                },
            ]
            assert proc.stdin is not None
            for frame in frames:
                proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()

            responses: dict[int, dict] = {}
            for _ in range(4):
                line = proc.stdout.readline()
                assert line, "server closed stdout early"
                payload = json.loads(line)
                if "id" in payload:
                    responses[payload["id"]] = payload
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=15)

        assert "result" in responses[1], responses[1].get("error")
        tool_names = sorted(
            t["name"] for t in responses[2]["result"]["tools"]
        )
        assert tool_names == [
            "read_actual_portfolio",
            "read_article",
            "read_article_search",
            "read_g_context",
            "read_instrument_scores",
            "read_macro_brain",
            "read_margin_evidence",
            "read_market_overview",
            "read_market_snapshot",
            "read_ready_evidence",
            "read_shared_brain",
            "read_user_watchlist",
            "update_user_watchlist",
        ]
        call_result = responses[3]["result"]
        assert not call_result.get("isError", False)
        assert "actual_portfolio_unavailable" in call_result["content"][0]["text"]

        trace_path = home / "fin-data" / "trace" / "read-capability" / "calls.jsonl"
        assert trace_path.exists()
        record = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[0])
        assert record["tool"] == "read_actual_portfolio"
        assert record["status"] == "gaps"
        assert record["schema_version"] == 1

        g_records = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["tool"] == "read_g_context"
        ]
        assert g_records
        g_summary = g_records[-1]["summary"]["g_pinned"]
        assert set(g_summary) == {
            "pinned_injected",
            "pinned_candidate_seen",
            "pinned_layer_count",
            "pinned_data_gaps",
        }
        assert isinstance(g_summary["pinned_layer_count"], int)

    def test_invalid_params_returns_jsonrpc_error(
        self, smoke_env: tuple[Path, dict[str, str]]
    ) -> None:
        _, env = smoke_env
        proc = subprocess.Popen(
            [
                str(_REPO_ROOT / ".venv" / "bin" / "python"),
                "-m",
                "fin_analyse.read_capabilities.server",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=_REPO_ROOT,
            env=env,
        )
        try:
            assert proc.stdin is not None
            proc.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {
                            "name": "read_g_context",
                            "arguments": {"question": "   "},
                        },
                    }
                )
                + "\n"
            )
            proc.stdin.flush()
            line = proc.stdout.readline()
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=15)

        payload = json.loads(line)
        # FastMCP turns an uncaught handler exception into a tool-level
        # error result; assert it is an error, not a hang or a crash.
        assert payload["id"] == 9
        body = payload.get("result", {})
        assert body.get("isError") is True or "error" in payload
