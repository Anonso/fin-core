from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path("scripts/codex_proxy_a_manual.sh")

ROUTES_YAML = """schema_version: fin.codex-routes/v2
reasoning:
  effort: high
routes:
  - id: codex-proxy-a
    enabled: true
    adapter: codex-responses
    workloads: [consultation, review]
    api:
      base_url: https://ai.apiclub.top
    model:
      id: gpt-5.6-sol
      quality: pinned
  - id: codex-proxy-b
    enabled: true
    adapter: codex-responses
    workloads: [consultation, review]
    api:
      base_url: https://ai.codesonline.dev
    model:
      id: grok-4.6
      quality: pinned
"""


def _write_route_fixture(root: Path) -> None:
    source_home = root / "codex-proxy-a"
    source_home.mkdir(parents=True)
    source_home.chmod(0o700)
    auth = source_home / "auth.json"
    auth.write_text('{"OPENAI_API_KEY":"test-key-123"}\n', encoding="utf-8")
    auth.chmod(0o600)
    yaml_file = root / "codex_routes.yaml"
    yaml_file.write_text(ROUTES_YAML, encoding="utf-8")
    yaml_file.chmod(0o600)


def _fake_codex(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$CODEX_HOME\"\nprintf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_script(root: Path, fake: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        env={
            **os.environ,
            "FIN_CODEX_ROUTE_ROOT": str(root),
            "FIN_CODEX_MANUAL_BINARY": str(fake),
            "FIN_CODEX_ROUTE_CONFIG": str(root / "codex_routes.yaml"),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_manual_entrypoint_uses_separate_home_and_shared_route_config() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "codex-proxy-a-manual" in source
    assert 'MANUAL_HOME="$ROUTE_ROOT/codex-proxy-a-manual"' in source
    assert 'export CODEX_HOME="$MANUAL_HOME"' in source
    assert (
        'ROUTE_CONFIG="${FIN_CODEX_ROUTE_CONFIG:-/home/ypk/fin-data/codex_routes.yaml}"'
        in source
    )
    assert "model_providers.fin_route.base_url=${BASE_URL}" in source
    assert "model_provider=fin_route" in source
    assert 'exec "$CODEX_BINARY"' in source


def test_manual_entrypoint_does_not_use_fin_route_runtime() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "codex-route-runtime" not in source
    assert "FIN_CODEX_ROUTE_LAUNCHER_ID" not in source


def test_manual_entrypoint_uses_yaml_base_url_and_owner_only_home(
    tmp_path: Path,
) -> None:
    root = tmp_path / "routes"
    root.mkdir()
    root.chmod(0o700)
    _write_route_fixture(root)
    fake = tmp_path / "codex"
    _fake_codex(fake)
    result = _run_script(root, fake, "exec", "--", "hello")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == str(root / "codex-proxy-a-manual")
    assert "model_provider=fin_route" in lines
    assert "model_providers.fin_route.base_url=https://ai.apiclub.top" in lines
    assert "model_providers.fin_route.env_key=OPENAI_API_KEY" in lines
    assert "model_providers.fin_route.wire_api=responses" in lines
    assert (root / "codex-proxy-a-manual").stat().st_mode & 0o777 == 0o700
    assert (root / "codex-proxy-a").is_dir()
    assert not (root / "codex-proxy-a" / "config.toml").exists()
