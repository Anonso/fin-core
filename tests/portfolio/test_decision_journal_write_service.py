"""Focused tests for the local MCP decision journal write service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef
from fin_analyse.consultation.instrument_identity import ConsultationInstrumentIdentity
from fin_analyse.portfolio.decision_journal import DecisionJournalStore
from fin_analyse.portfolio.decision_journal_write_service import (
    DecisionJournalWriteService,
)


class _FakeResolver:
    def __init__(self) -> None:
        self._by_ref = {
            "600519": "600519.SH",
            "贵州茅台": "600519.SH",
            "600259": "600259.SH",
        }

    def resolve_many(
        self, targets: tuple[InstrumentRef, ...]
    ) -> tuple[ConsultationInstrumentIdentity, ...]:
        out = []
        for target in targets:
            ref = target.ticker or target.name
            symbol = self._by_ref.get(ref or "")
            if symbol is None:
                out.append(
                    ConsultationInstrumentIdentity(
                        status="UNRESOLVED",
                        semantic_ref=target,
                        market_symbol=None,
                    )
                )
            else:
                out.append(
                    ConsultationInstrumentIdentity(
                        status="RESOLVED",
                        semantic_ref=InstrumentRef(ticker=symbol, name=ref),
                        market_symbol=symbol,
                        source="A_SHARE_DIRECTORY",
                        data_gaps=(),
                    )
                )
        return tuple(out)


def _service(
    tmp_path: Path,
    store: DecisionJournalStore | None = None,
    clock=None,
) -> DecisionJournalWriteService:
    return DecisionJournalWriteService(
        store=store or DecisionJournalStore(root=tmp_path, principal_id="finp_test"),
        resolver=_FakeResolver(),  # type: ignore[arg-type]
        principal_id="finp_test",
        clock=clock or (lambda: datetime(2026, 9, 1, 12, 0, tzinfo=UTC)),
    )


def _preview(service: DecisionJournalWriteService, **overrides: object):
    fields: dict[str, object] = {
        "decision_type": "buy",
        "symbol": "600519",
        "rationale": "估值回到低位",
        "note": "仓位 5%",
    }
    fields.update(overrides)
    return service.preview(**fields)


def test_preview_then_apply_roundtrip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    preview = _preview(service)
    assert preview["status"] == "PREVIEW_READY"
    draft = preview["draft"]
    assert draft["symbol"] == "600519.SH"  # 入库前归一为 canonical
    assert draft["decision_date"] == "2026-09-01"  # 默认 = clock 的 CST 日历日
    assert "确认记录决策：买入 600519.SH" in preview["confirmation_phrase"]
    assert "决策日 2026-09-01" in preview["confirmation_phrase"]
    assert "理由：估值回到低位。" in preview["confirmation_phrase"]
    assert "备注：仓位 5%。" in preview["confirmation_phrase"]
    assert service.list()["count"] == 0  # preview 零写

    applied = service.apply(preview["candidate_token"])
    assert applied["status"] == "APPLIED"
    assert applied["decision_id"].startswith("DJ-2026-09-01-")
    records = service.list()["records"]
    assert len(records) == 1
    assert records[0]["symbol"] == "600519.SH"
    assert records[0]["source"] == "owner_stated"  # 服务端强制
    assert records[0]["reverted_by"] is None


def test_closed_set_and_field_bounds_reject(tmp_path: Path) -> None:
    service = _service(tmp_path)
    cases = [
        ({"decision_type": "other"}, "decision_journal_type_invalid"),
        ({"decision_type": None}, "decision_journal_type_invalid"),
        ({"rationale": None}, "decision_journal_rationale_required"),
        ({"rationale": "   "}, "decision_journal_rationale_required"),
        ({"rationale": "x" * 2001}, "decision_journal_rationale_too_long"),
        ({"note": "x" * 501}, "decision_journal_note_too_long"),
        ({"decision_date": "2026-02-30"}, "decision_journal_date_invalid"),
        ({"decision_date": "09/01"}, "decision_journal_date_invalid"),
        ({"symbol": "不存在的票"}, "decision_journal_symbol_unresolved"),
    ]
    for overrides, expected_code in cases:
        rejected = _preview(service, **overrides)
        assert rejected["status"] == "REJECTED", overrides
        assert expected_code in rejected["reason_codes"], overrides
    assert service.list()["count"] == 0


def test_name_ref_normalizes_and_unresolved_rejects(tmp_path: Path) -> None:
    service = _service(tmp_path)
    preview = _preview(service, symbol="贵州茅台")
    assert preview["draft"]["symbol"] == "600519.SH"
    assert "600519.SH" in preview["confirmation_phrase"]


def test_default_date_is_cst_calendar_day(tmp_path: Path) -> None:
    # UTC 08-31 17:00 = 北京 09-01 01:00 → 默认决策日跨到 09-01。
    service = _service(
        tmp_path, clock=lambda: datetime(2026, 8, 31, 17, 0, tzinfo=UTC)
    )
    preview = _preview(service)
    assert preview["draft"]["decision_date"] == "2026-09-01"


def test_portfolio_level_record_without_symbol(tmp_path: Path) -> None:
    service = _service(tmp_path)
    preview = _preview(
        service, symbol=None, decision_type="plan", rationale="总仓位降到五成"
    )
    assert preview["draft"]["symbol"] is None
    assert "组合级" in preview["confirmation_phrase"]
    applied = service.apply(preview["candidate_token"])
    assert applied["status"] == "APPLIED"
    record = service.list()["records"][0]
    assert record["symbol"] is None


def test_revert_flow_single_correction(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan_token = _preview(
        service,
        decision_type="plan",
        symbol=None,
        rationale="持股等复苏",
    )["candidate_token"]
    applied = service.apply(plan_token)
    plan_id = applied["decision_id"]

    preview = _preview(
        service,
        decision_type="revert",
        symbol=None,
        rationale="逻辑变了，作废该计划",
        revert_of=plan_id,
    )
    assert preview["status"] == "PREVIEW_READY"
    assert f"更正对象：{plan_id}" in preview["confirmation_phrase"]
    revert = service.apply(preview["candidate_token"])
    assert revert["status"] == "APPLIED"
    revert_id = revert["decision_id"]

    records = {
        r["decision_id"]: r for r in service.list()["records"]
    }
    assert records[plan_id]["reverted_by"] == revert_id
    assert records[revert_id]["revert_of"] == plan_id

    # 目标已被更正：preview 即拒（每记录至多一次更正）。
    again = _preview(
        service,
        decision_type="revert",
        symbol=None,
        rationale="第二次更正必须被拒",
        revert_of=plan_id,
    )
    assert again["status"] == "REJECTED"
    assert "decision_journal_revert_target_already_reverted" in again["reason_codes"]

    missing = _preview(
        service,
        decision_type="revert",
        symbol=None,
        rationale="目标不存在必须被拒",
        revert_of="DJ-2026-09-01-dead",
    )
    assert "decision_journal_revert_target_missing" in missing["reason_codes"]

    # 非 revert 带 revert_of：preview 即拒。
    invalid = _preview(
        service, revert_of=plan_id, rationale="非 revert 不得带 revert_of"
    )
    assert "decision_journal_revert_of_invalid" in invalid["reason_codes"]


def test_token_single_use_expiry_and_restart(tmp_path: Path) -> None:
    service = _service(tmp_path)
    token = _preview(service)["candidate_token"]
    assert service.apply(token)["status"] == "APPLIED"
    # 单次使用：复用同一 token 必须拒绝。
    assert service.apply(token)["status"] == "REJECTED"

    token2 = _preview(service)["candidate_token"]
    now = service._clock()
    service._tokens._tokens[token2]["expires_at"] = now - timedelta(seconds=1)  # noqa: SLF001
    assert service.apply(token2)["status"] == "REJECTED"
    assert service.list()["count"] == 1

    # 重启失效：新 service 实例（新 token manager）不认旧 token。
    fresh = _service(tmp_path, store=service._store)  # noqa: SLF001
    token3 = _preview(service)["candidate_token"]
    assert fresh.apply(token3)["status"] == "REJECTED"
    assert fresh.list()["count"] == 1


def test_apply_commit_failure_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    token = _preview(service)["candidate_token"]

    class _BrokenStore:
        def append(self, **kwargs: object):
            raise RuntimeError("disk on fire")

        def get(self, decision_id: str):
            return None

    real_store = service._store  # noqa: SLF001
    service._store = _BrokenStore()  # type: ignore[assignment]  # noqa: SLF001
    failed = service.apply(token)
    assert failed["status"] == "REJECTED"
    assert "decision_journal_append_failed" in failed["reason_codes"]

    # token 已消费不复活：换回真 store 重放同一 token 仍拒绝、零行落库。
    service._store = real_store  # type: ignore[assignment]  # noqa: SLF001
    assert service.apply(token)["status"] == "REJECTED"
    assert service.list()["count"] == 0


def test_query_filters_and_semantics(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for overrides in (
        {"symbol": "600519", "rationale": "低吸"},
        {"symbol": "600259", "rationale": "稀土主线"},
        {"decision_type": "plan", "symbol": None, "rationale": "降仓位"},
    ):
        token = _preview(service, **overrides)["candidate_token"]
        assert service.apply(token)["status"] == "APPLIED"

    by_symbol = service.query(symbol="600259.SH")
    assert by_symbol["count"] == 1
    assert by_symbol["records"][0]["decision_id"].startswith("DJ-")
    by_type = service.query(decision_type="plan")
    assert by_type["count"] == 1
    assert by_type["records"][0]["symbol"] is None
    listed = service.list()
    assert listed["status"] == "LISTED"
    assert listed["count"] == 3
    dates = [r["decision_date"] for r in listed["records"]]
    assert dates == sorted(dates, reverse=True)  # 最新在前
    assert "never investment evidence" in listed["semantics"]
    capped = service.query(limit=500)
    assert capped["count"] == 3  # 上限 200，静默封顶不报错
