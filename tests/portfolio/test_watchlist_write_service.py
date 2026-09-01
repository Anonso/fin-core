"""Focused tests for the local MCP watchlist write service (add/tag only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef
from fin_analyse.portfolio.user_watchlist import UserWatchlistStore
from fin_analyse.portfolio.watchlist_write_service import WatchlistWriteService
from fin_analyse.consultation.instrument_identity import ConsultationInstrumentIdentity


class _FakeDirectory:
    def lookup(self, value: str) -> tuple[object, ...]:
        hits = {
            "600259": ("600259.SH", "中稀有色"),
            "贵州茅台": ("600519.SH", "贵州茅台"),
        }
        if value not in hits:
            return ()
        symbol, name = hits[value]
        return (type("Entry", (), {"symbol": symbol, "name": name})(),)


class _FakeResolver:
    def __init__(self) -> None:
        self._by_ref = {
            "600259": ("600259.SH", "中稀有色"),
            "600519": ("600519.SH", "贵州茅台"),
            "贵州茅台": ("600519.SH", "贵州茅台"),
        }

    def resolve_many(self, targets: tuple[InstrumentRef, ...]) -> tuple[ConsultationInstrumentIdentity, ...]:
        out = []
        for target in targets:
            ref = target.ticker if target.ticker else target.name
            hit = self._by_ref.get(ref)
            if hit is None:
                out.append(
                    ConsultationInstrumentIdentity(
                        status="UNRESOLVED",
                        semantic_ref=target,
                        market_symbol=None,
                    )
                )
            else:
                symbol, name = hit
                out.append(
                    ConsultationInstrumentIdentity(
                        status="RESOLVED",
                        semantic_ref=target,
                        market_symbol=symbol,
                        source="A_SHARE_DIRECTORY",
                        data_gaps=(),
                    )
                )
        return tuple(out)


def _service(tmp_path: Path) -> WatchlistWriteService:
    store = UserWatchlistStore(root=tmp_path, principal_id="finp_test")
    return WatchlistWriteService(
        store=store,
        resolver=_FakeResolver(),  # type: ignore[arg-type]
        directory=_FakeDirectory(),  # type: ignore[arg-type]
        principal_id="finp_test",
        clock=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )


def test_preview_add_then_apply_forces_assistant_provenance(tmp_path: Path) -> None:
    service = _service(tmp_path)
    preview = service.preview(
        ({"action": "add", "ref": "600259", "tags": ["mainline_ai"]},)
    )
    assert preview["status"] == "PREVIEW_READY"
    assert preview["operations"][0]["provenance"] == "assistant"
    token = preview["candidate_token"]
    assert isinstance(token, str) and token
    assert "确认更新自选股" in preview["confirmation_phrase"]

    applied = service.apply(str(token))
    assert applied["status"] == "APPLIED"
    assert applied["outcomes"][0]["changed"] is True
    entry = service.list()["entries"][0]
    assert entry["provenance"] == "assistant"
    assert entry["tags"] == ["mainline_ai"]


def test_preview_tag_then_apply_adds_tags(tmp_path: Path) -> None:
    service = _service(tmp_path)
    add_token = service.preview(({"action": "add", "ref": "600519"},))["candidate_token"]
    assert service.apply(str(add_token))["status"] == "APPLIED"
    preview = service.preview(
        ({"action": "tag", "ref": "600519", "tags": ["suggest_delete"]},)
    )
    token = preview["candidate_token"]
    applied = service.apply(str(token))
    assert applied["status"] == "APPLIED"
    entry = service.list()["entries"][0]
    assert entry["tags"] == ["suggest_delete"]


def test_remove_action_is_rejected_zero_write(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = service.preview(({"action": "remove", "ref": "600259"},))
    assert result["status"] == "REJECTED"
    assert "watchlist_invalid_action" in result["reason_codes"]
    assert service.list()["entry_count"] == 0


def test_token_single_use_and_expiry(tmp_path: Path) -> None:
    service = _service(tmp_path)
    preview = service.preview(({"action": "add", "ref": "600259"},))
    token = str(preview["candidate_token"])
    assert service.apply(token)["status"] == "APPLIED"
    # 单次使用：复用同一 token 必须拒绝。
    assert service.apply(token)["status"] == "REJECTED"

    preview2 = service.preview(({"action": "add", "ref": "600519"},))
    token2 = str(preview2["candidate_token"])
    now = service._clock()
    service._tokens._tokens[token2]["expires_at"] = now - timedelta(seconds=1)
    assert service.apply(token2)["status"] == "REJECTED"
    assert service.list()["entry_count"] == 1


def test_preview_is_zero_write(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.preview(({"action": "add", "ref": "600259", "tags": ["mainline_ai"]},))
    assert service.list()["entry_count"] == 0
    assert service.list()["revision"] == ""
