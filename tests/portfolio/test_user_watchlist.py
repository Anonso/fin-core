"""Focused tests for the user-maintained typed A-share watchlist store.

Covers: revision CAS (required, empty-state "r0", cross-instance interleaving),
single-transaction add/remove with audit-failure rollback, O_EXCL first-write
race recovery with winner validation, fail-closed owner-only path validation
(no silent chmod), principal scoping and zero-write reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fin_analyse.consultation.instrument_identity import ConsultationInstrumentIdentity
from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef
from fin_analyse.portfolio.user_watchlist import (
    UserWatchlistAddError,
    UserWatchlistConflictError,
    UserWatchlistDuplicateError,
    UserWatchlistMissingError,
    UserWatchlistRemoveError,
    UserWatchlistStateError,
    UserWatchlistStore,
)


def _identity(ticker: str = "600259", name: str = "广晟有色") -> ConsultationInstrumentIdentity:
    return ConsultationInstrumentIdentity(
        status="RESOLVED",
        semantic_ref=InstrumentRef(ticker=ticker, name=name),
        market_symbol=ticker,
        source="A_SHARE_DIRECTORY",
        data_gaps=(),
    )


def _store(tmp_path: Path, principal: str = "finp_test") -> UserWatchlistStore:
    return UserWatchlistStore(root=tmp_path, principal_id=principal)


# ── CAS / revision semantics ────────────────────────────────────────────────

def test_add_and_list_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.add(_identity(), expected_revision="r0")
    assert result.changed is True
    read = store.list()
    assert [e.market_symbol for e in read.entries] == ["600259"]
    assert [e.name for e in read.entries] == ["广晟有色"]
    assert read.revision != ""
    assert read.as_of is not None


def test_missing_list_is_empty_and_zero_write(tmp_path: Path) -> None:
    """查询不存在的自选清单不得创建 state 或 SQLite journal。"""
    read = _store(tmp_path).list()

    assert read.entries == ()
    assert read.revision == ""
    assert list(tmp_path.iterdir()) == []


def test_expected_revision_is_required_no_cas_free_write(tmp_path: Path) -> None:
    """写入口必须携带显式 expected_revision；不存在无 CAS 通道。"""
    store = _store(tmp_path)
    with pytest.raises(TypeError):
        store.add(_identity())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        store.remove(_identity())  # type: ignore[call-arg]
    assert store.list().entries == ()


def test_empty_state_accepts_r0(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.add(_identity(), expected_revision="r0")
    assert result.changed is True
    assert store.list().revision != "r0"


def test_empty_state_rejects_other_expected_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(UserWatchlistConflictError):
        store.add(_identity(), expected_revision="")
    with pytest.raises(UserWatchlistConflictError):
        store.add(_identity(), expected_revision="r1-anything")
    assert store.list().entries == ()


def test_duplicate_add_is_honest_and_zero_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_identity(), expected_revision="r0")
    before = store.list()
    with pytest.raises(UserWatchlistDuplicateError):
        store.add(_identity(), expected_revision=before.revision)
    after = store.list()
    assert [e.market_symbol for e in after.entries] == [e.market_symbol for e in before.entries]
    assert after.revision == before.revision


def test_remove_and_missing_remove(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_identity(), expected_revision="r0")
    removed = store.remove(_identity(), expected_revision=store.list().revision)
    assert removed.changed is True
    assert store.list().entries == ()
    with pytest.raises(UserWatchlistMissingError):
        store.remove(_identity(), expected_revision=store.list().revision)


def test_invalid_identity_without_market_symbol_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    unresolved = ConsultationInstrumentIdentity(
        status="UNRESOLVED",
        semantic_ref=InstrumentRef(name="不存在的股票"),
        market_symbol=None,
        data_gaps=("instrument_identity_unresolved",),
    )
    with pytest.raises(UserWatchlistAddError):
        store.add(unresolved, expected_revision="r0")
    assert store.list().entries == ()


def test_cas_rejects_stale_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_identity(), expected_revision="r0")
    rev_before = store.list().revision
    store.add(_identity(ticker="600519", name="贵州茅台"), expected_revision=rev_before)
    with pytest.raises(UserWatchlistConflictError):
        store.add(
            _identity(ticker="000960", name="锡业股份"),
            expected_revision=rev_before,  # stale: 已被上一步变更取代
        )
    assert {e.market_symbol for e in store.list().entries} == {"600259", "600519"}


def test_cas_accepts_current_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_identity(), expected_revision="r0")
    current = store.list().revision
    store.add(
        _identity(ticker="600519", name="贵州茅台"),
        expected_revision=current,
    )
    assert {e.market_symbol for e in store.list().entries} == {"600259", "600519"}


def test_interleaved_same_revision_across_instances(tmp_path: Path) -> None:
    """两个实例以同一 expected_revision 交错：先胜者提交，后者 CAS 冲突零写。"""
    store_a = _store(tmp_path)
    store_b = _store(tmp_path)
    store_a.add(_identity(), expected_revision="r0")
    with pytest.raises(UserWatchlistConflictError):
        store_b.add(_identity(ticker="600519", name="贵州茅台"), expected_revision="r0")
    assert {e.market_symbol for e in store_b.list().entries} == {"600259"}


def test_stale_replay_is_rejected_zero_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_identity(), expected_revision="r0")
    rev = store.list().revision
    # 重放同一 add(携带当前 revision)→ duplicate 终态 no-op,零写、不重复
    with pytest.raises(UserWatchlistDuplicateError):
        store.add(_identity(), expected_revision=rev)
    assert [e.market_symbol for e in store.list().entries] == ["600259"]


# ── 单事务原子性 ────────────────────────────────────────────────────────────

def test_audit_failure_rolls_back_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit 写入失败必须回滚整个事务：无 entry、revision 不前进、audit 无残留。"""
    store = _store(tmp_path)
    store.add(_identity(), expected_revision="r0")
    before = store.list().revision

    def boom(cursor, **kwargs):
        raise UserWatchlistAddError("audit_injected_failure")

    monkeypatch.setattr(store, "_audit", boom)
    with pytest.raises(UserWatchlistAddError):
        store.add(_identity(ticker="600519", name="贵州茅台"), expected_revision=before)

    after = store.list()
    assert [e.market_symbol for e in after.entries] == ["600259"]
    assert after.revision == before  # 无 partial bump
    assert len(store.audit_events()) == 1  # 只有第一条的 audit


def test_remove_audit_failure_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    store.add(_identity(), expected_revision="r0")
    store.add(_identity(ticker="600519", name="贵州茅台"), expected_revision=store.list().revision)
    before = store.list().revision

    def boom(cursor, **kwargs):
        raise UserWatchlistRemoveError("audit_injected_failure")

    monkeypatch.setattr(store, "_audit", boom)
    with pytest.raises(UserWatchlistRemoveError):
        store.remove(_identity(), expected_revision=before)

    after = store.list()
    assert [e.market_symbol for e in after.entries] == ["600259", "600519"]
    assert after.revision == before


# ── O_EXCL 首写竞态 ─────────────────────────────────────────────────────────

def test_eexist_race_recovers_winner_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """exists() 误报 False 后 O_EXCL 撞 EEXIST：复验 winner 对象并正常继续写入。"""
    store = _store(tmp_path)
    store.add(_identity(), expected_revision="r0")  # winner 先建库
    winner_revision = store.list().revision

    db_path = store._db_path
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        return False if self == db_path else real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    result = store.add(
        _identity(ticker="600519", name="贵州茅台"), expected_revision=winner_revision
    )
    assert result.changed is True
    assert {e.market_symbol for e in store.list().entries} == {"600259", "600519"}


def test_eexist_race_rejects_insecure_winner_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """EEXIST 后 winner 对象不安全：fail-closed，不静默 chmod。"""
    store = _store(tmp_path)
    db_path = store._db_path
    db_path.parent.mkdir(mode=0o700)
    db_path.write_bytes(b"")  # 0644（受 umask 影响）

    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        return False if self == db_path else real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    with pytest.raises(UserWatchlistStateError):
        store.add(_identity(), expected_revision="r0")
    assert db_path.stat().st_mode & 0o077 != 0  # 未被静默修正


# ── fail-closed owner-only 路径校验 ─────────────────────────────────────────

def test_insecure_root_rejected_zero_write(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o755)
    store = UserWatchlistStore(root=root, principal_id="finp_test")
    with pytest.raises(UserWatchlistStateError):
        store.add(_identity(), expected_revision="r0")
    assert list(root.iterdir()) == []


def test_insecure_existing_db_rejected_no_chmod(tmp_path: Path) -> None:
    store = _store(tmp_path)
    db_path = store._db_path
    db_path.parent.mkdir(mode=0o700)
    db_path.write_bytes(b"not a database")  # 0644
    with pytest.raises(UserWatchlistStateError):
        store.add(_identity(), expected_revision="r0")
    assert db_path.stat().st_mode & 0o077 != 0  # 拒绝而非 chmod


def test_symlink_db_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"")
    store._db_path.parent.mkdir(mode=0o700)
    store._db_path.symlink_to(target)
    with pytest.raises(UserWatchlistStateError):
        store.add(_identity(), expected_revision="r0")


def test_multi_link_db_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    db_path = store._db_path
    db_path.parent.mkdir(mode=0o700)
    db_path.write_bytes(b"")
    os_link = tmp_path / "hardlink.db"
    os_link.hardlink_to(db_path)
    with pytest.raises(UserWatchlistStateError):
        store.add(_identity(), expected_revision="r0")


def test_owner_only_permissions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_identity(), expected_revision="r0")
    db = next(tmp_path.rglob("*.sqlite3"))
    assert db.stat().st_mode & 0o077 == 0
    assert db.parent.stat().st_mode & 0o077 == 0


def test_principal_pattern_enforced(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _store(tmp_path, principal="/etc/passwd")
    with pytest.raises(ValueError):
        _store(tmp_path, principal="")
    with pytest.raises(ValueError):
        _store(tmp_path, principal=".")
    with pytest.raises(ValueError):
        _store(tmp_path, principal="finp_a/b")
    # binding 层产出的合法形状可接受（首字符允许字母或数字，后续允许 . _ : -）
    _store(tmp_path, principal="finp_7fb51032abc.def-xyz:01")


# ── audit / scoping ─────────────────────────────────────────────────────────

def test_audit_events_recorded_without_conversation_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_identity(), expected_revision="r0")
    store.add(_identity(ticker="600519", name="贵州茅台"), expected_revision=store.list().revision)
    store.remove(_identity(), expected_revision=store.list().revision)
    events = store.audit_events()
    assert len(events) == 3
    for event in events:
        assert "对话" not in json.dumps(event, ensure_ascii=False)
        assert "prompt" not in json.dumps(event, ensure_ascii=False)
        assert event["operation"] in {"add", "remove"}


def test_principal_scoping(tmp_path: Path) -> None:
    store_a = _store(tmp_path, principal="finp_a")
    store_b = _store(tmp_path, principal="finp_b")
    store_a.add(_identity(), expected_revision="r0")
    assert store_b.list().entries == ()
    assert store_a.list().entries != ()
