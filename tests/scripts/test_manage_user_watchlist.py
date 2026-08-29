"""Focused tests for the user-watchlist management CLI (production semantics).

The CLI derives root (absolute XDG_STATE_HOME) and principal (owner-only
installation identity) itself; refs arrive as UTF-8 hex tokens only; output
schema is user-watchlist-management-result.v2 with frozen exit codes
(0 success / 1 arg-internal / 2 state-identity unavailable / 3 noop /
4 unresolvable-non-canonical / 5 CAS conflict).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.manage_user_watchlist import main

_IDENTITY = "a" * 64


def _token(ref: str) -> str:
    return ref.encode("utf-8").hex()


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Production-shaped state: absolute XDG_STATE_HOME + owner-only identity."""
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    root = state_home / "fin-analyse" / "semantic-research-v1"
    root.mkdir(parents=True, mode=0o700)
    identity = root / "installation-identity.hex"
    identity.write_text(_IDENTITY + "\n")
    os.chmod(identity, 0o600)
    return root


def _out(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)


# ── 默认生产语义 / 身份与 root 派生 ────────────────────────────────────────

def test_list_empty_zero_write(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    payload = _out(capsys)
    assert payload["schema_version"] == "user-watchlist-management-result.v2"
    assert payload["outcome"] == "listed"
    assert payload["entries"] == []
    assert payload["revision"] == ""


def test_principal_derived_from_installation_identity(state: Path, capsys) -> None:
    assert main(["add", "--ref-token", _token("600259"), "--apply"]) == 0
    dbs = list((state / "user-watchlist-v1").glob("*.sqlite3"))
    assert len(dbs) == 1
    assert dbs[0].name.startswith("finp_")
    assert dbs[0].name != "finp_default.sqlite3"


def test_relative_xdg_fails_closed(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    assert main(["list"]) == 2
    payload = _out(capsys)
    assert payload["outcome"] == "unavailable"
    assert payload["ok"] is False


def test_missing_identity_fails_closed(tmp_path, capsys, monkeypatch) -> None:
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    assert main(["list"]) == 2
    payload = _out(capsys)
    assert payload["outcome"] == "unavailable"
    assert payload["error"] == "authentication_required"


def test_insecure_state_root_fails_closed(tmp_path, capsys, monkeypatch) -> None:
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    root = state_home / "fin-analyse" / "semantic-research-v1"
    root.mkdir(parents=True, mode=0o755)  # 不安全
    identity = root / "installation-identity.hex"
    identity.write_text(_IDENTITY + "\n")
    os.chmod(identity, 0o600)
    assert main(["list"]) == 2
    assert _out(capsys)["outcome"] == "unavailable"


def test_help_works_without_state(state: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


# ── ref token 边界 ──────────────────────────────────────────────────────────

def test_invalid_hex_token_exits_4(state: Path, capsys) -> None:
    assert main(["add", "--ref-token", "zzzz", "--apply"]) == 4
    payload = _out(capsys)
    assert payload["outcome"] == "unresolved"
    assert payload["error"] == "watchlist_ref_token_invalid"


def test_non_utf8_hex_token_exits_4(state: Path, capsys) -> None:
    assert main(["add", "--ref-token", "fffe", "--apply"]) == 4
    assert _out(capsys)["error"] == "watchlist_ref_token_invalid"


def test_unresolved_ref_exits_4_zero_write(state: Path, capsys) -> None:
    assert main(["add", "--ref-token", _token("不存在的股票"), "--apply"]) == 4
    payload = _out(capsys)
    assert payload["outcome"] == "unresolved"
    assert main(["list"]) == 0
    assert _out(capsys)["entries"] == []


def test_non_canonical_name_exits_4_zero_write(state: Path, capsys) -> None:
    """归一化可命中但输入非 canonical（含空格/大小写变体）→ 拒绝零写。"""
    assert main(["add", "--ref-token", _token("三 花智控"), "--apply"]) == 4
    payload = _out(capsys)
    assert payload["error"] == "watchlist_ref_not_canonical_name"
    assert main(["list"]) == 0
    assert _out(capsys)["entries"] == []


# ── 生命周期 / no-op / conflict ─────────────────────────────────────────────

def test_add_preview_then_apply(state: Path, capsys) -> None:
    assert main(["add", "--ref-token", _token("600259")]) == 0
    preview = _out(capsys)
    assert preview["preview"] is True
    assert preview["market_symbol"] == "600259.SH"
    assert main(["list"]) == 0
    assert _out(capsys)["entries"] == []  # preview 零写

    assert main(["add", "--ref-token", _token("600259"), "--apply"]) == 0
    applied = _out(capsys)
    assert applied["changed"] is True
    assert applied["outcome"] == "applied"

    assert main(["list"]) == 0
    entries = _out(capsys)["entries"]
    assert [e["market_symbol"] for e in entries] == ["600259.SH"]
    assert entries[0]["name"] == "中稀有色"


def test_add_by_canonical_name(state: Path, capsys) -> None:
    assert main(["add", "--ref-token", _token("三花智控"), "--apply"]) == 0
    assert _out(capsys)["changed"] is True
    assert main(["list"]) == 0
    entries = _out(capsys)["entries"]
    assert entries[0]["market_symbol"] == "002050.SZ"


def test_duplicate_exits_3_noop(state: Path, capsys) -> None:
    assert main(["add", "--ref-token", _token("600259"), "--apply"]) == 0
    _out(capsys)
    assert main(["add", "--ref-token", _token("600259"), "--apply"]) == 3
    payload = _out(capsys)
    assert payload["outcome"] == "noop"
    assert payload["changed"] is False
    assert payload["error"] == "watchlist_duplicate_symbol"
    assert payload["current_revision"] != ""


def test_remove_then_missing_exits_3(state: Path, capsys) -> None:
    assert main(["add", "--ref-token", _token("600259"), "--apply"]) == 0
    _out(capsys)
    assert main(["remove", "--ref-token", _token("600259"), "--apply"]) == 0
    assert _out(capsys)["changed"] is True
    assert main(["remove", "--ref-token", _token("600259"), "--apply"]) == 3
    payload = _out(capsys)
    assert payload["outcome"] == "noop"
    assert payload["error"] == "watchlist_missing_symbol"


def test_cas_conflict_exits_5(
    state: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin_analyse.portfolio.user_watchlist import UserWatchlistStore, WatchlistRead

    assert main(["add", "--ref-token", _token("600259"), "--apply"]) == 0
    _out(capsys)
    real_list = UserWatchlistStore.list

    def stale_list(self):
        read = real_list(self)
        return WatchlistRead(entries=read.entries, revision="r0", as_of=read.as_of)

    monkeypatch.setattr(UserWatchlistStore, "list", stale_list)
    assert main(["add", "--ref-token", _token("600519"), "--apply"]) == 5
    payload = _out(capsys)
    assert payload["outcome"] == "conflict"
    assert payload["changed"] is False
    assert payload["error"] == "watchlist_revision_conflict"


def test_external_write_between_read_and_apply_is_conflict(
    state: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O4 frozen (audit round 2): an external write after the CLI's start-of-run
    read and before apply must be a CAS conflict (exit 5, zero write) — the
    operator's start-of-run revision stays authoritative, it is not silently
    folded into the new revision."""
    from fin_analyse.consultation.instrument_identity import (
        AShareConsultationInstrumentIdentityResolver,
    )
    from fin_analyse.guo_teacher_research.semantic_contract import InstrumentRef
    from fin_analyse.portfolio.user_watchlist import UserWatchlistStore

    assert main(["add", "--ref-token", _token("600259"), "--apply"]) == 0
    _out(capsys)  # r0 -> r1

    real_add = UserWatchlistStore.add
    resolver = AShareConsultationInstrumentIdentityResolver()

    def add_after_external_write(self, identity, *, expected_revision):
        # 模拟 read 与 apply 之间的外部写入（600519 提交成功，r1 -> r2）
        ext_identity = resolver.resolve_many((InstrumentRef(ticker="600519"),))[0]
        real_add(self, ext_identity, expected_revision=expected_revision)
        # CLI 仍用启动时 revision（r1）CAS -> 冲突
        return real_add(self, identity, expected_revision=expected_revision)

    monkeypatch.setattr(UserWatchlistStore, "add", add_after_external_write)
    assert main(["add", "--ref-token", _token("000876"), "--apply"]) == 5
    payload = _out(capsys)
    assert payload["outcome"] == "conflict"
    assert payload["changed"] is False
    assert payload["error"] == "watchlist_revision_conflict"
    # 外部写入保留、CLI 目标零写
    assert main(["list"]) == 0
    symbols = {e["market_symbol"] for e in _out(capsys)["entries"]}
    assert symbols == {"600259.SH", "600519.SH"}


def test_principal_scoping_across_identities(tmp_path, capsys, monkeypatch) -> None:
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    root = state_home / "fin-analyse" / "semantic-research-v1"
    root.mkdir(parents=True, mode=0o700)
    identity = root / "installation-identity.hex"
    identity.write_text("b" * 64 + "\n")
    os.chmod(identity, 0o600)

    assert main(["add", "--ref-token", _token("600259"), "--apply"]) == 0
    assert _out(capsys)["changed"] is True

    # 换回主 identity：互不可见（不同 principal 库）
    identity.write_text(_IDENTITY + "\n")
    os.chmod(identity, 0o600)
    assert main(["list"]) == 0
    assert _out(capsys)["entries"] == []


def test_no_cas_free_cli_path(state: Path) -> None:
    """CLI 无 --state-root/--principal 覆盖参数；未知参数 exit 1。"""
    assert main(["--state-root", "/tmp", "list"]) == 1
    assert main(["--principal", "finp_x", "list"]) == 1
