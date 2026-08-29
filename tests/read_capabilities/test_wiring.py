"""Wiring smoke tests for the read-capability thin server (design §7.3).

Construction against a real (temp) knowledge root, one happy path per tool,
one unavailable path, one deadline-expired path.  No network: market-side
readers that would hit the wire are only exercised through their failure
(unavailable) paths here; the end-to-end scenario (§7.5) covers real calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fin_analyse.read_capabilities.types import ProductionReadRequest
from fin_analyse.read_capabilities.wiring import (
    READ_TOOL_NAMES,
    ReaderWiring,
    build_reader_wiring,
)


@pytest.fixture()
def kb_root(tmp_path: Path) -> Path:
    """Minimal real knowledge root: an empty agent_context pin file."""

    root = tmp_path / "knowledge-base"
    pinned = root / "runtime" / "agent_context" / "pinned_sources.jsonl"
    pinned.parent.mkdir(parents=True, exist_ok=True)
    pinned.write_text(
        json.dumps(
            {
                "pinned_id": "pinned-test",
                "agent_id": "guo_teacher",
                "source_scope": "g_source",
                "processing_status": "ready",
                "processed_at": "2026-08-01T08:00:00+08:00",
                "processed_title": "pinned-test",
                "guidance_brief": "测试固定认知。",
                "theme_clusters": ["测试"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Point every data root at tmp so the suite never touches real data."""

    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "HOME": str(tmp_path / "home"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


class TestConstruction:
    def test_builds_all_six_tools(self, kb_root: Path, isolated_env: dict[str, str]) -> None:
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        assert wiring.tool_names() == READ_TOOL_NAMES
        assert wiring.unavailable_tools == ()

    def test_market_overview_failure_degrades_to_registered_tool(
        self, kb_root: Path, isolated_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fin_analyse.read_capabilities.wiring as wiring_mod

        def _fail(**kwargs):  # noqa: ANN003
            raise OSError("calendar missing")

        monkeypatch.setattr(wiring_mod, "build_default_a_share_market_overview", _fail)
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        # Tool stays registered; first call reports the typed gap.
        result = wiring.runners["read_market_overview"](
            ProductionReadRequest(question="今天市场怎么样")
        )
        assert "MARKET_OVERVIEW_UNAVAILABLE" in result.data_gaps


class TestHappyPaths:
    def test_read_actual_portfolio_unavailable_without_snapshot(
        self, kb_root: Path, isolated_env: dict[str, str]
    ) -> None:
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        result = wiring.runners["read_actual_portfolio"](
            ProductionReadRequest(question="我的持仓怎么样")
        )
        # No confirmed snapshot exists under the isolated config root.
        assert "actual_portfolio_unavailable" in result.data_gaps
        assert result.value["status"] == "UNKNOWN"

    def test_read_g_context_returns_projection(
        self, kb_root: Path, isolated_env: dict[str, str]
    ) -> None:
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        result = wiring.runners["read_g_context"](
            ProductionReadRequest(question="老师最近怎么看稀土")
        )
        # Layered G projection (pinned/framework/facts/associations).
        assert isinstance(result.value, dict)
        assert "pinned" in result.value

    def test_read_ready_evidence_requires_as_of_filled_by_server(
        self, kb_root: Path, isolated_env: dict[str, str]
    ) -> None:
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        now = datetime.now(UTC)
        result = wiring.runners["read_ready_evidence"](
            ProductionReadRequest(question="最近有什么参考资料", as_of=now)
        )
        assert isinstance(result.value, dict)

    def test_read_market_snapshot_instruments_missing(
        self, kb_root: Path, isolated_env: dict[str, str]
    ) -> None:
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        result = wiring.runners["read_market_snapshot"](
            ProductionReadRequest(question="看看行情")
        )
        assert "market_snapshot_unavailable" in result.data_gaps

    def test_read_margin_evidence_without_instruments(
        self, kb_root: Path, isolated_env: dict[str, str]
    ) -> None:
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        result = wiring.runners["read_margin_evidence"](
            ProductionReadRequest(question="两融情况如何")
        )
        assert isinstance(result.value, dict)


class TestDeadlinePath:
    def test_expired_deadline_surfaces_typed_gap(
        self, kb_root: Path, isolated_env: dict[str, str]
    ) -> None:
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        expired = datetime.now(UTC) - timedelta(seconds=5)
        result = wiring.runners["read_ready_evidence"](
            ProductionReadRequest(
                question="最近有什么参考资料",
                as_of=datetime.now(UTC),
                deadline_at=expired,
            )
        )
        # The provider converts an expired deadline into a typed gap set,
        # never a bare TimeoutError escaping to the caller.
        assert isinstance(result.data_gaps, tuple)


class TestReaderWiringShape:
    def test_runner_mapping_covers_exactly_v1_tools(self) -> None:
        assert set(READ_TOOL_NAMES) == {
            "read_g_context",
            "read_actual_portfolio",
            "read_market_snapshot",
            "read_market_overview",
            "read_margin_evidence",
            "read_ready_evidence",
        }
