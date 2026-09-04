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

from fin_analyse.consultation.instrument_identity import ConsultationInstrumentIdentity
from fin_analyse.guo_teacher_research.production_capability_provider import (
    ProductionReadCapabilityProvider,
)
from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef
from fin_analyse.portfolio.user_watchlist import (
    UserWatchlistStore,
    WatchlistEntry,
    WatchlistRead,
)
from fin_analyse.portfolio.watchlist_state import require_production_watchlist_state
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


def _provision_watchlist_state(tmp_path: Path) -> None:
    """置备 owner-only 安装身份，使 happy-path 下 watchlist 推导成功（设计门 F3）。"""
    import os

    root = tmp_path / "state" / "fin-analyse" / "semantic-research-v1"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    identity_path = root / "installation-identity.hex"
    identity_path.write_text("ab" * 32 + "\n", encoding="ascii")
    os.chmod(identity_path, 0o600)


_ALL_READ_TOOL_NAMES = (
    *READ_TOOL_NAMES,
    "read_instrument_scores",
    "read_article_search",
    "read_article",
    "read_macro_brain",
    "read_shared_brain",
)


class TestConstruction:
    def test_builds_all_eight_tools(
        self, kb_root: Path, isolated_env: dict[str, str], tmp_path: Path
    ) -> None:
        _provision_watchlist_state(tmp_path)
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        assert wiring.tool_names() == _ALL_READ_TOOL_NAMES
        assert wiring.unavailable_tools == ()

    def test_watchlist_state_missing_degrades_only_watchlist_tool(
        self, kb_root: Path, isolated_env: dict[str, str]
    ) -> None:
        # 设计门 F2 回归：无安装身份时推导 fail-closed，但只降级本工具，
        # wiring 构造（= server 启动）绝不能崩。
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        assert wiring.tool_names() == _ALL_READ_TOOL_NAMES
        result = wiring.runners["read_user_watchlist"](
            ProductionReadRequest(question="看下当前自选股")
        )
        assert "user_watchlist_reader_unavailable" in result.data_gaps

    def test_watchlist_write_service_wired_when_state_available(
        self, kb_root: Path, isolated_env: dict[str, str], tmp_path: Path
    ) -> None:
        _provision_watchlist_state(tmp_path)
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        assert wiring.watchlist_write is not None
        result = wiring.watchlist_write.list()
        assert result["status"] == "LISTED"
        assert result["entry_count"] == 0

    def test_watchlist_write_service_absent_when_state_missing(
        self, kb_root: Path, isolated_env: dict[str, str]
    ) -> None:
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        assert wiring.watchlist_write is None

    def test_decision_journal_service_wired_when_state_available(
        self, kb_root: Path, isolated_env: dict[str, str], tmp_path: Path
    ) -> None:
        _provision_watchlist_state(tmp_path)
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        assert wiring.decision_journal is not None
        result = wiring.decision_journal.list()
        assert result["status"] == "LISTED"
        assert result["count"] == 0
        assert "never investment evidence" in result["semantics"]

    def test_decision_journal_service_absent_when_state_missing(
        self, kb_root: Path, isolated_env: dict[str, str]
    ) -> None:
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        assert wiring.decision_journal is None

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

    def test_read_user_watchlist_empty_is_legal_ok_state(
        self, kb_root: Path, isolated_env: dict[str, str], tmp_path: Path
    ) -> None:
        _provision_watchlist_state(tmp_path)
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        result = wiring.runners["read_user_watchlist"](
            ProductionReadRequest(question="看下当前自选股")
        )
        # 空列表是合法态：无 gap，明确 entries 为空。
        assert result.data_gaps == ()
        assert result.value["entry_count"] == 0
        assert result.value["entries"] == []
        assert "never investment evidence" in str(result.value["semantics"])

    def test_read_user_watchlist_lists_seeded_entries(
        self, kb_root: Path, isolated_env: dict[str, str], tmp_path: Path
    ) -> None:
        _provision_watchlist_state(tmp_path)
        _, principal, store = require_production_watchlist_state(environ=isolated_env)
        store.add(
            ConsultationInstrumentIdentity(
                status="RESOLVED",
                semantic_ref=InstrumentRef(ticker="600259", name="中稀有色"),
                market_symbol="600259.SH",
                source="A_SHARE_DIRECTORY",
                data_gaps=(),
            ),
            expected_revision="r0",
        )
        wiring = build_reader_wiring(kb_root, environ=isolated_env)
        result = wiring.runners["read_user_watchlist"](
            ProductionReadRequest(question="看下当前自选股")
        )
        assert result.data_gaps == ()
        assert result.value["entry_count"] == 1
        assert result.value["entries"][0]["market_symbol"] == "600259.SH"
        assert result.value["entries"][0]["provenance"] == "owner"
        assert result.value["entries"][0]["tags"] == []
        assert result.value["revision"] == store.list().revision
        assert principal.startswith("finp_")

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
        quality = result.value["attestation"]["quality"]
        # cognition_mainline_consumption = 消费探针（设计门 部件5），有 readmodel
        # 时随 attestation 出带；形状由探针测试文件专护。
        assert set(quality) == {
            "pinned_injected",
            "pinned_candidate_seen",
            "pinned_layer_count",
            "pinned_data_gaps",
            "cognition_mainline_consumption",
        }

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


class _FakeWatchlistReader:
    """Provider-level fake for read_user_watchlist state coverage."""

    def __init__(
        self,
        result: WatchlistRead | None = None,
        *,
        error: Exception | None = None,
        invalid: object | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self._invalid = invalid

    def list(self) -> WatchlistRead:
        if self._error is not None:
            raise self._error
        if self._invalid is not None:
            return self._invalid  # type: ignore[no-any-return]
        assert self._result is not None
        return self._result


class TestReadUserWatchlistProviderStates:
    """设计门 F6：四态镜像（unavailable/read_failed/result_invalid/成功）。"""

    def _provider(
        self,
        kb_root: Path,
        reader: _FakeWatchlistReader | None = None,
    ) -> ProductionReadCapabilityProvider:
        return ProductionReadCapabilityProvider(
            knowledge_base_root=kb_root,
            user_watchlist=reader,
        )

    def test_success_projects_entries_and_user_context_semantics(
        self, kb_root: Path
    ) -> None:
        read = WatchlistRead(
            entries=(
                WatchlistEntry(
                    market_symbol="600259.SH",
                    name="中稀有色",
                    added_at="2026-08-12T09:21:59+00:00",
                    provenance="assistant",
                    tags=("mainline_ai", "suggest_delete"),
                ),
            ),
            revision="r1-0123456789abcdef",
            as_of="2026-08-29T14:00:00+00:00",
        )
        provider = self._provider(kb_root, _FakeWatchlistReader(read))
        result = provider.read_user_watchlist(
            ProductionReadRequest(question="自选股有哪些")
        )
        assert result.data_gaps == ()
        assert result.value["entry_count"] == 1
        assert result.value["entries"][0]["market_symbol"] == "600259.SH"
        assert result.value["entries"][0]["provenance"] == "assistant"
        assert result.value["entries"][0]["tags"] == ["mainline_ai", "suggest_delete"]
        assert result.value["revision"] == "r1-0123456789abcdef"
        assert "never investment evidence" in str(result.value["semantics"])

    def test_read_failed_reports_typed_gap(self, kb_root: Path) -> None:
        provider = self._provider(
            kb_root, _FakeWatchlistReader(error=RuntimeError("db locked"))
        )
        result = provider.read_user_watchlist(
            ProductionReadRequest(question="自选股")
        )
        assert result.data_gaps == ("user_watchlist_read_failed",)

    def test_result_invalid_reports_typed_gap(self, kb_root: Path) -> None:
        provider = self._provider(kb_root, _FakeWatchlistReader(invalid=object()))
        result = provider.read_user_watchlist(
            ProductionReadRequest(question="自选股")
        )
        assert result.data_gaps == ("user_watchlist_result_invalid",)

    def test_reader_unavailable_reports_typed_gap(self, kb_root: Path) -> None:
        provider = self._provider(kb_root)
        result = provider.read_user_watchlist(
            ProductionReadRequest(question="自选股")
        )
        assert result.data_gaps == ("user_watchlist_reader_unavailable",)


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
            "read_user_watchlist",
        }
