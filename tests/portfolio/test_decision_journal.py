"""Focused tests for the decision journal durable store (append-only)."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

import fin_analyse.portfolio.decision_journal as dj
from fin_analyse.portfolio.decision_journal import (
    DecisionJournalAppendError,
    DecisionJournalRevertError,
    DecisionJournalStateError,
    DecisionJournalStore,
)


def _store(tmp_path: Path, principal: str = "finp_test") -> DecisionJournalStore:
    return DecisionJournalStore(root=tmp_path, principal_id=principal)


def _append(store: DecisionJournalStore, **overrides: object):
    fields: dict[str, object] = {
        "decision_type": "buy",
        "decision_date": "2026-09-01",
        "rationale": "估值回到低位，闲钱加仓",
        "symbol": "600519.SH",
    }
    fields.update(overrides)
    return store.append(**fields)  # type: ignore[arg-type]


def test_append_and_query_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mutation = _append(store)
    assert mutation.decision_id.startswith("DJ-2026-09-01-")
    assert mutation.revision.startswith("r1-")

    read = store.query()
    assert read.revision == mutation.revision
    assert len(read.records) == 1
    record = read.records[0]
    assert record.decision_id == mutation.decision_id
    assert record.decision_type == "buy"
    assert record.symbol == "600519.SH"
    assert record.decision_date == "2026-09-01"
    assert record.rationale == "估值回到低位，闲钱加仓"
    assert record.note is None
    assert record.source == "owner_stated"
    assert record.schema_version == "decision-journal.v1"
    assert record.revert_of is None
    assert record.reverted_by is None
    assert record.recorded_at  # UTC isoformat 存在
    assert len(store.audit_events()) == 1
    assert store.audit_events()[0]["operation"] == "append"


def test_query_empty_state_absent_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    read = store.query()
    assert read.records == ()
    assert read.revision == ""
    assert store.get("DJ-2026-09-01-0000") is None
    assert store.audit_events() == ()


def test_query_filters_and_ordering(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _append(store, decision_date="2026-08-01", symbol="600519.SH")
    _append(
        store,
        decision_type="plan",
        decision_date="2026-09-01",
        symbol=None,
        rationale="总仓位降到五成",
    )
    _append(store, decision_date="2026-09-03", symbol="600259.SH")

    newest_first = store.query()
    assert [r.decision_date for r in newest_first.records] == [
        "2026-09-03",
        "2026-09-01",
        "2026-08-01",
    ]
    by_symbol = store.query(symbol="600519.SH")
    assert len(by_symbol.records) == 1
    assert by_symbol.records[0].decision_date == "2026-08-01"
    by_type = store.query(decision_type="plan")
    assert len(by_type.records) == 1
    assert by_type.records[0].symbol is None  # 组合级
    by_range = store.query(date_from="2026-08-15", date_to="2026-09-01")
    assert [r.decision_date for r in by_range.records] == ["2026-09-01"]
    limited = store.query(limit=1)
    assert len(limited.records) == 1
    assert limited.records[0].decision_date == "2026-09-03"


def test_revert_flow_backfills_reverted_by(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plan = _append(
        store,
        decision_type="plan",
        symbol=None,
        decision_date="2026-08-20",
        rationale="持股等复苏",
    )
    revert = _append(
        store,
        decision_type="revert",
        symbol=None,
        decision_date="2026-09-01",
        rationale="逻辑变了，作废该计划",
        revert_of=plan.decision_id,
    )
    read = store.query()
    by_id = {r.decision_id: r for r in read.records}
    assert by_id[plan.decision_id].reverted_by == revert.decision_id
    assert by_id[revert.decision_id].revert_of == plan.decision_id
    assert by_id[revert.decision_id].reverted_by is None

    with pytest.raises(DecisionJournalRevertError):
        _append(
            store,
            decision_type="revert",
            symbol=None,
            revert_of=plan.decision_id,
            rationale="第二次更正必须被拒",
        )
    with pytest.raises(DecisionJournalRevertError):
        _append(
            store,
            decision_type="revert",
            symbol=None,
            revert_of="DJ-2026-09-01-dead",
            rationale="目标不存在必须被拒",
        )


def test_revert_iff_both_directions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(DecisionJournalAppendError):
        _append(
            store,
            decision_type="revert",
            symbol=None,
            revert_of=None,
            rationale="revert 必须带 revert_of",
        )
    with pytest.raises(DecisionJournalAppendError):
        _append(
            store,
            decision_type="buy",
            revert_of="DJ-2026-09-01-dead",
            rationale="非 revert 不得带 revert_of",
        )
    assert store.query().records == ()


def test_iff_enforced_at_sql_level(tmp_path: Path) -> None:
    """表级 CHECK/FK/partial unique 是最后一道防线（绕过 service 直写也拦住）。"""
    store = _store(tmp_path)
    _append(store)
    connection = sqlite3.connect(store._db_path)  # noqa: SLF001 — 测试直查
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            # CHECK：revert 无 revert_of。
            connection.execute(
                "INSERT INTO decisions(decision_id, schema_version, decision_type, "
                "symbol, decision_date, rationale, note, source, revert_of, recorded_at) "
                "VALUES ('DJ-x', 'decision-journal.v1', 'revert', NULL, '2026-09-01', "
                "'r', NULL, 'owner_stated', NULL, '2026-09-01T00:00:00+00:00')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            # CHECK：非 revert 带 revert_of。
            connection.execute(
                "INSERT INTO decisions(decision_id, schema_version, decision_type, "
                "symbol, decision_date, rationale, note, source, revert_of, recorded_at) "
                "VALUES ('DJ-y', 'decision-journal.v1', 'buy', NULL, '2026-09-01', "
                "'r', NULL, 'owner_stated', 'DJ-x', '2026-09-01T00:00:00+00:00')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            # CHECK：闭集外 decision_type。
            connection.execute(
                "INSERT INTO decisions(decision_id, schema_version, decision_type, "
                "symbol, decision_date, rationale, note, source, revert_of, recorded_at) "
                "VALUES ('DJ-z', 'decision-journal.v1', 'other', NULL, '2026-09-01', "
                "'r', NULL, 'owner_stated', NULL, '2026-09-01T00:00:00+00:00')"
            )
    finally:
        connection.close()


def test_owner_only_permissions_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _append(store)
    dir_info = (tmp_path / "decision-journal-v1").lstat()
    db_info = store._db_path.lstat()  # noqa: SLF001 — 测试直查
    assert stat.S_IMODE(dir_info.st_mode) == 0o700
    assert stat.S_IMODE(db_info.st_mode) == 0o600
    assert db_info.st_uid == os.geteuid()
    assert db_info.st_nlink == 1

    os.chmod(store._db_path, 0o644)  # noqa: SLF001
    try:
        with pytest.raises(DecisionJournalStateError):
            _append(store, rationale="坏权限必须 fail closed")
    finally:
        os.chmod(store._db_path, 0o600)
    assert len(store.query().records) == 1  # 复原后照常可用


def test_id_collision_retry_and_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def scripted_id(decision_date: str) -> str:
        calls.append("x")
        suffix = "aaaa" if len(calls) <= 2 else "bbbb"
        return f"DJ-{decision_date}-{suffix}"

    monkeypatch.setattr(dj, "new_decision_id", scripted_id)
    store = _store(tmp_path)
    first = _append(store)
    assert first.decision_id.endswith("-aaaa")
    second = _append(store, rationale="撞号后重试成功")
    assert second.decision_id.endswith("-bbbb")

    def constant_id(decision_date: str) -> str:
        return f"DJ-{decision_date}-aaaa"

    monkeypatch.setattr(dj, "new_decision_id", constant_id)
    with pytest.raises(DecisionJournalAppendError):
        _append(store, rationale="连续撞号必须耗尽报错")
    assert len(store.query().records) == 2


def test_read_path_fails_closed_on_bad_permissions(tmp_path: Path) -> None:
    """外审 P2：读路径同样 owner-only fail-closed（空态仍合法）。"""
    store = _store(tmp_path)
    assert store.query().records == ()  # 空态：库不存在，合法。
    _append(store)
    os.chmod(store._db_path, 0o644)  # noqa: SLF001
    try:
        with pytest.raises(DecisionJournalStateError):
            store.query()
        with pytest.raises(DecisionJournalStateError):
            store.get("DJ-2026-09-01-0000")
        with pytest.raises(DecisionJournalStateError):
            store.audit_events()
    finally:
        os.chmod(store._db_path, 0o600)
    assert len(store.query().records) == 1  # 复原后照常可读

    os.symlink(store._db_path, store._db_path.with_suffix(".link"))  # noqa: SLF001
    link_store = DecisionJournalStore(
        root=tmp_path, principal_id="finp_test"
    )
    link_store._db_path = store._db_path.with_suffix(".link")  # noqa: SLF001
    with pytest.raises(DecisionJournalStateError):
        link_store.query()


def test_revision_and_audit_grow(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _append(store)
    second = _append(
        store, decision_type="sell", symbol="600259.SH", rationale="止盈"
    )
    first_seq = int(first.revision.split("-")[0][1:])
    second_seq = int(second.revision.split("-")[0][1:])
    assert second_seq == first_seq + 1
    events = store.audit_events()
    assert [event["decision_type"] for event in events] == ["buy", "sell"]
    assert [event["result"] for event in events] == ["appended", "appended"]
    assert store.get(second.decision_id) is not None
    assert store.get("DJ-2026-09-01-ffff") is None
