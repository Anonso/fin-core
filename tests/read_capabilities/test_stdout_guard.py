"""Stdout-guard test for the thin server (design §7.4).

A stray ``print`` after importing the server module must not reach the
real stdout (it would corrupt the JSON-RPC stream); it lands on stderr.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestStdoutGuard:
    def test_stray_print_redirected_to_stderr(self) -> None:
        code = (
            "import sys\n"
            "import fin_analyse.read_capabilities.server\n"
            "print('STRAY_OUTPUT')\n"
            "sys.stderr.write('REAL_STDOUT_MARKER_CHECK')\n"
            "import os\n"
            "os.write(1, b'')  # no-op; real stdout must stay empty\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=_REPO_ROOT,
        )
        assert result.returncode == 0, result.stderr
        assert "STRAY_OUTPUT" not in result.stdout
        assert "STRAY_OUTPUT" in result.stderr
        assert result.stdout == ""

    def test_logging_never_writes_to_stdout(self) -> None:
        code = (
            "import logging\n"
            "import fin_analyse.read_capabilities.server\n"
            "logging.getLogger('stray').info('LOG_LINE')\n"
            "print('AFTER')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=_REPO_ROOT,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
