"""Focused tests for the shared watchlist production-state derivation seam.

Covers: environ/home passthrough (no monkeypatch isolation), owner-only root
validation (fail closed, never repaired), missing identity (PrincipalBinding
fail closed), and the principal digest the store filename derives from.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.principal_binding import PrincipalBindingError
from fin_analyse.portfolio.user_watchlist import UserWatchlistStateError
from fin_analyse.portfolio.watchlist_state import (
    _INSTALLATION_NAMESPACE,
    check_watchlist_state_root,
    require_production_watchlist_state,
)


def _provision(root: Path, identity: str = "ab" * 32) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    identity_path = root / "installation-identity.hex"
    identity_path.write_text(identity + "\n", encoding="ascii")
    os.chmod(identity_path, 0o600)
    return root


def _state_root(tmp_path: Path) -> Path:
    return tmp_path / "state" / "fin-analyse" / "semantic-research-v1"


def test_happy_path_derives_principal_and_store(tmp_path: Path) -> None:
    root = _provision(_state_root(tmp_path))
    out_root, principal, store = require_production_watchlist_state(
        environ={"XDG_STATE_HOME": str(tmp_path / "state")},
    )
    assert out_root == root
    expected = hashlib.sha256(
        _INSTALLATION_NAMESPACE.encode("utf-8") + b"\0" + bytes.fromhex("ab" * 32)
    ).hexdigest()
    assert principal == f"finp_{expected}"
    # store 的数据库落位在同一 principal 名下（读路径零副作用，文件尚不存在）。
    assert not (root / "user-watchlist-v1" / f"{principal}.sqlite3").exists()


def test_environ_passthrough_selects_state_home(tmp_path: Path) -> None:
    _provision(_state_root(tmp_path))
    out_root, _, _ = require_production_watchlist_state(
        environ={"XDG_STATE_HOME": str(tmp_path / "state")},
    )
    assert out_root == _state_root(tmp_path)


def test_missing_identity_fails_closed(tmp_path: Path) -> None:
    # root 缺失或存在但无 identity 文件：两条路径都必须 fail closed。
    with pytest.raises(PrincipalBindingError):
        require_production_watchlist_state(
            environ={"XDG_STATE_HOME": str(tmp_path / "state")},
        )
    _state_root(tmp_path).mkdir(parents=True)
    os.chmod(_state_root(tmp_path), 0o700)
    with pytest.raises(PrincipalBindingError):
        require_production_watchlist_state(
            environ={"XDG_STATE_HOME": str(tmp_path / "state")},
        )


def test_loose_root_permissions_fail_closed(tmp_path: Path) -> None:
    root = _state_root(tmp_path)
    root.mkdir(parents=True)
    os.chmod(root, 0o755)
    with pytest.raises(UserWatchlistStateError, match="watchlist_state_root_invalid"):
        require_production_watchlist_state(
            environ={"XDG_STATE_HOME": str(tmp_path / "state")},
        )


def test_root_as_regular_file_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "state" / "fin-analyse" / "semantic-research-v1"
    root.parent.mkdir(parents=True)
    root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(UserWatchlistStateError, match="watchlist_state_root_invalid"):
        check_watchlist_state_root(root)


def test_identity_file_permissions_are_strict(tmp_path: Path) -> None:
    root = _provision(_state_root(tmp_path))
    identity_path = root / "installation-identity.hex"
    info = identity_path.lstat()
    assert stat.S_IMODE(info.st_mode) == 0o600
