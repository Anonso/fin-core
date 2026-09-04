"""Production state derivation for the decision journal (CLI and read wiring).

Single derivation seam for root/principal/store so the read-capability
wiring cannot drift: absolute ``XDG_STATE_HOME`` plus the owner-only local
installation identity, fail-closed on any deviation.  There is no
caller-supplied state root or principal override.  Derivation is read-only
(lstat + identity read); no directory or database is created here.  An
absent root stays legal (empty journal); a bad-permission root fails
closed.  Mirrors ``portfolio/watchlist_state.py`` — the store layer does
not import principal_binding.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from fin_analyse.guo_teacher_research.principal_binding import (
    LocalInstallationPrincipalProvider,
)
from fin_analyse.portfolio.decision_journal import (
    DecisionJournalStateError,
    DecisionJournalStore,
)
from fin_analyse.runtime.state_roots import semantic_research_state_root

_INSTALLATION_NAMESPACE = "fin.local-installation.v1"


def check_decision_journal_state_root(root: Path) -> None:
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
        raise DecisionJournalStateError("decision_journal_state_root_invalid")


def require_production_decision_journal_state(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, str, DecisionJournalStore]:
    """Resolve root/principal/store or raise a typed fail-closed error."""
    root = semantic_research_state_root(home=home, environ=environ)
    check_decision_journal_state_root(root)
    provider = LocalInstallationPrincipalProvider(
        identity_path=root / "installation-identity.hex",
        installation_namespace=_INSTALLATION_NAMESPACE,
    )
    principal = provider.require_binding().principal_id
    return root, principal, DecisionJournalStore(root=root, principal_id=principal)
