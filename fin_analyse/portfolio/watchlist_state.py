"""Production state derivation for the user watchlist (CLI and read wiring).

Single derivation seam for root/principal/store so the operator CLI
(``scripts/manage_user_watchlist.py``) and the read-capability wiring cannot
drift: absolute ``XDG_STATE_HOME`` plus the owner-only local installation
identity, fail-closed on any deviation.  There is no caller-supplied state
root or principal override.  Derivation is read-only (lstat + identity read);
no directory or database is created here.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from fin_analyse.guo_teacher_research.principal_binding import (
    LocalInstallationPrincipalProvider,
)
from fin_analyse.portfolio.user_watchlist import (
    UserWatchlistStateError,
    UserWatchlistStore,
)
from fin_analyse.runtime.state_roots import semantic_research_state_root

_INSTALLATION_NAMESPACE = "fin.local-installation.v1"


def check_watchlist_state_root(root: Path) -> None:
    """Owner-only root validation; an absent root stays legal (empty state)."""
    try:
        info = root.lstat()
    except FileNotFoundError:
        return  # 空态：identity 读取随后 fail closed
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or (stat.S_IMODE(info.st_mode) & 0o077)
    ):
        raise UserWatchlistStateError("watchlist_state_root_invalid")


def require_production_watchlist_state(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, str, UserWatchlistStore]:
    """Resolve root/principal/store or raise a typed fail-closed error."""
    root = semantic_research_state_root(home=home, environ=environ)
    check_watchlist_state_root(root)
    provider = LocalInstallationPrincipalProvider(
        identity_path=root / "installation-identity.hex",
        installation_namespace=_INSTALLATION_NAMESPACE,
    )
    principal = provider.require_binding().principal_id
    return root, principal, UserWatchlistStore(root=root, principal_id=principal)
