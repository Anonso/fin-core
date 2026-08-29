"""Root-cut assertions for the read-capability thin server (design §7.1).

The thin server must import and serve without ``production_runtime`` and
``capability_broker`` ever entering ``sys.modules`` — those two modules drag
the full production composition and the MoA engine closure.  These tests
install a meta-path blocker so any attempt raises immediately, then import
the server package fresh inside a subprocess (isolated from this test
process, which already imported half the tree via conftest).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BLOCKED_MODULES = (
    "fin_analyse.guo_teacher_research.production_runtime",
    "fin_analyse.guo_teacher_research.capability_broker",
)

_BLOCKER_SCRIPT = """
import sys

BLOCKED = {blocked!r}
BLOCKED_PREFIXES = {blocked_tuple!r}

class _RootCutBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in BLOCKED or fullname.startswith(BLOCKED_PREFIXES):
            raise AssertionError(
                "root-cut violated: attempted import of " + fullname
            )
        return None

sys.meta_path.insert(0, _RootCutBlocker())
"""


def _run_with_blocker(code: str) -> subprocess.CompletedProcess[str]:
    blocked_tuple = tuple(f"{name}." for name in _BLOCKED_MODULES)
    script = _BLOCKER_SCRIPT.format(
        blocked=list(_BLOCKED_MODULES),
        blocked_tuple=blocked_tuple,
    )
    return subprocess.run(
        [sys.executable, "-c", script + code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=_REPO_ROOT,
    )


class TestThinServerImportClosure:
    def test_server_import_does_not_pull_blocked_modules(self) -> None:
        # Note: the server installs its stdout guard at import time, so any
        # print lands on stderr — the marker is asserted on stderr, and the
        # real stdout must stay empty of stray writes (guard verification).
        result = _run_with_blocker(
            "import fin_analyse.read_capabilities.server as s\n"
            "print('IMPORT_OK', len([m for m in sys.modules if m.startswith('fin_analyse')]))\n"
        )
        assert result.returncode == 0, result.stderr
        assert "root-cut violated" not in result.stderr
        assert "IMPORT_OK" in result.stderr
        assert result.stdout == ""

    def test_provider_and_ready_evidence_do_not_pull_blocked_modules(self) -> None:
        result = _run_with_blocker(
            "import fin_analyse.guo_teacher_research.production_capability_provider\n"
            "import fin_analyse.guo_teacher_research.ready_evidence\n"
            "assert 'fin_analyse.guo_teacher_research.production_runtime' not in sys.modules\n"
            "assert 'fin_analyse.guo_teacher_research.capability_broker' not in sys.modules\n"
            "print('LEAF_OK')\n"
        )
        assert result.returncode == 0, result.stderr
        assert "LEAF_OK" in result.stdout

    def test_wiring_does_not_pull_blocked_modules(self) -> None:
        result = _run_with_blocker(
            "import fin_analyse.read_capabilities.wiring\n"
            "print('WIRING_OK')\n"
        )
        assert result.returncode == 0, result.stderr
        assert "WIRING_OK" in result.stdout

    def test_leaf_types_are_stdlib_only(self) -> None:
        result = _run_with_blocker(
            "import sys\n"
            "import fin_analyse.read_capabilities.types\n"
            "internal = [m for m in sys.modules if m.startswith('fin_analyse')]\n"
            "assert internal == ['fin_analyse', 'fin_analyse.read_capabilities', "
            "'fin_analyse.read_capabilities.types'], internal\n"
            "print('LEAF_TYPES_OK')\n"
        )
        assert result.returncode == 0, result.stderr
        assert "LEAF_TYPES_OK" in result.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
