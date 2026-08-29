from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def test_codex_open_uses_codex_with_opencode_go(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    data_dir = tmp_path / "data"
    auth_dir = data_dir / "opencode"
    codex_dir = tmp_path / ".codex"
    capture = tmp_path / "capture"
    bin_dir.mkdir()
    auth_dir.mkdir(parents=True)
    codex_dir.mkdir()

    auth_file = auth_dir / "auth.json"
    auth_file.write_text(
        json.dumps({"opencode-go": {"type": "api", "key": "test-secret"}}),
        encoding="utf-8",
    )
    auth_file.chmod(0o600)
    (codex_dir / "models.json").write_text("{}\n", encoding="utf-8")

    fake_codex = bin_dir / "codex"
    fake_codex.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "printf 'cwd=%s\\n' \"$PWD\" >\"$CODEX_OPEN_CAPTURE\"\n"
        "printf 'key=%s\\n' \"${OPENCODE_GO_API_KEY:+set}\" >>\"$CODEX_OPEN_CAPTURE\"\n"
        "printf '%s\\n' \"$@\" >>\"$CODEX_OPEN_CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o700)

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path),
            "XDG_DATA_HOME": str(data_dir),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "CODEX_OPEN_CAPTURE": str(capture),
        }
    )
    result = subprocess.run(
        ["/bin/bash", "scripts/codex_open.sh", "exec", "--json", "prompt"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    captured = capture.read_text(encoding="utf-8").splitlines()
    assert captured[:2] == ["cwd=/home/ypk/fin-analyse", "key=set"]
    assert captured[2:] == [
        "-c",
        "model_provider=opencode_go",
        "-c",
        "model_providers.opencode_go.name=OpenCode Go",
        "-c",
        "model_providers.opencode_go.base_url=https://opencode.ai/zen/go/v1",
        "-c",
        "model_providers.opencode_go.env_key=OPENCODE_GO_API_KEY",
        "-c",
        "model_providers.opencode_go.wire_api=responses",
        "-c",
        f"model_catalog_json={codex_dir / 'models.json'}",
        "-c",
        "model_reasoning_effort=max",
        "-m",
        "deepseek-v4-pro",
        "--sandbox",
        "read-only",
        "exec",
        "--json",
        "prompt",
    ]


def test_codex_open_rejects_loose_credential_permissions(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    auth_dir = data_dir / "opencode"
    auth_dir.mkdir(parents=True)
    auth_file = auth_dir / "auth.json"
    auth_file.write_text(
        json.dumps({"opencode-go": {"type": "api", "key": "test-secret"}}),
        encoding="utf-8",
    )
    auth_file.chmod(0o644)

    env = dict(os.environ)
    env.update({"HOME": str(tmp_path), "XDG_DATA_HOME": str(data_dir)})
    result = subprocess.run(
        ["/bin/bash", "scripts/codex_open.sh", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 78
    assert "credential file must be owner-only" in result.stderr
    assert "test-secret" not in result.stderr
