"""Security regressions for the shared codex-route-runtime identity checks.

The shell functions are extracted verbatim from the tracked script so the
tests exercise the shipped implementation (no inline copies that can drift).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path("scripts/codex_route_runtime.sh")


def _extract_bash_function(name: str) -> str:
    source = _SCRIPT.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}$",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(source)
    assert match is not None, f"function {name} not found in {_SCRIPT}"
    return f"{name}() {{\n{match.group(1)}\n}}\n"


@pytest.fixture(scope="module")
def probe_script() -> str:
    return _extract_bash_function("reject_symlink_ancestors")


def _run_probe(probe_script: str, path: str) -> int:
    result = subprocess.run(
        ["/bin/bash", "-c", f"set -u\n{probe_script}\nreject_symlink_ancestors {path!r}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode


def _run_fin_shape(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """直接执行共享 runtime 的 FIN shape/config gates（不伪造 route binding）。"""
    shape = _extract_bash_function("validate_fin_runtime_shape")
    config = _extract_bash_function("validate_fin_runtime_config_overrides")
    positional = [value for value in arguments if value != "--last"]
    positional_literal = " ".join(shlex.quote(value) for value in positional)
    last_count = sum(value == "--last" for value in arguments)
    script = f"""set -euo pipefail
FIN_FORBIDDEN_OPTION_SEEN=0
LAST_SESSION_SEEN={last_count}
EPHEMERAL_SEEN=0
SANDBOX_MODE=read-only
UNKNOWN_OPTION_SEEN=0
EXEC_SEEN=1
JSON_SEEN=1
POSITIONAL_COUNT={len(positional)}
IGNORE_USER_CONFIG_SEEN=1
IGNORE_RULES_SEEN=1
STRICT_CONFIG_SEEN=1
SKIP_GIT_CHECK_SEEN=1
POSITIONAL_ARGS=({positional_literal})
FIN_IMPORT_ROOT=/tmp/fin-release
FIN_CAPABILITY_MODE=0
CONFIG_OVERRIDES=('approval_policy="never"' 'web_search="disabled"' 'model_reasoning_effort="max"')
DISABLED_FEATURES=()
{shape}
{config}
validate_fin_runtime_shape
validate_fin_runtime_config_overrides
"""
    return subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_fin_consultation_proxy_accepts_live_native_web_override() -> None:
    """The proxy route must preserve the consultation runtime's live-Web mode."""
    shape = _extract_bash_function("validate_fin_runtime_shape")
    config = _extract_bash_function("validate_fin_runtime_config_overrides")
    script = f'''set -euo pipefail
FIN_FORBIDDEN_OPTION_SEEN=0
LAST_SESSION_SEEN=0
EPHEMERAL_SEEN=0
SANDBOX_MODE=read-only
UNKNOWN_OPTION_SEEN=0
EXEC_SEEN=1
JSON_SEEN=1
POSITIONAL_COUNT=3
IGNORE_USER_CONFIG_SEEN=1
IGNORE_RULES_SEEN=1
STRICT_CONFIG_SEEN=1
SKIP_GIT_CHECK_SEEN=1
POSITIONAL_ARGS=(resume 019fce86-2b94-7bc3-bcc7-d99757088c56 prompt)
FIN_IMPORT_ROOT=/tmp/fin-release
FIN_CAPABILITY_MODE=0
CONFIG_OVERRIDES=('approval_policy="never"' 'web_search="live"' 'model_reasoning_effort="max"')
DISABLED_FEATURES=()
{shape}
{config}
validate_fin_runtime_shape
validate_fin_runtime_config_overrides
'''
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert "config override is not allowlisted" not in result.stderr
    assert "web search override is invalid" not in result.stderr
    assert result.returncode == 64
    assert "feature denylist is incomplete" in result.stderr


def _run_configured_route_overrides(
    overrides: list[str],
    *,
    capability_mode: int = 0,
) -> subprocess.CompletedProcess[str]:
    function = _extract_bash_function("validate_configured_route_overrides")
    override_literal = " ".join(shlex.quote(value) for value in overrides)
    script = f"""set -euo pipefail
FIN_CAPABILITY_MODE={capability_mode}
FIN_IMPORT_ROOT=/tmp/fin-release
CONFIG_OVERRIDES=({override_literal})
{function}
validate_configured_route_overrides
"""
    return subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_configured_route_preserves_reasoning_and_web_overrides() -> None:
    result = _run_configured_route_overrides(
        [
            'model_reasoning_effort="ultra"',
            'web_search="live"',
            'approval_policy="never"',
        ]
    )
    assert result.returncode == 0, result.stderr


def test_configured_route_rejects_provider_and_model_config_overrides() -> None:
    for override in (
        'model_provider="attacker"',
        'model_providers.attacker.base_url="https://attacker.invalid"',
        'model="attacker-model"',
    ):
        result = _run_configured_route_overrides([override])
        assert result.returncode == 64
        assert "configured route identity override is not allowlisted" in result.stderr


def test_configured_route_rejects_ambiguous_reasoning_override() -> None:
    result = _run_configured_route_overrides(
        ['model_reasoning_effort="max"', 'model_reasoning_effort="ultra"']
    )
    assert result.returncode == 64
    assert "configured route request override is duplicated" in result.stderr


def test_route_config_args_follow_exec_subcommand() -> None:
    """Provider overrides must appear after the ``exec`` subcommand.

    codex-cli 0.149 ignores ``-c model_provider=...``/``base_url`` placed
    before ``exec`` and falls back to the default api.openai.com endpoint.
    The runtime therefore must emit ``codex exec ... <route args>`` so the
    attested A/B route actually talks to its configured provider.
    """

    source = _SCRIPT.read_text(encoding="utf-8")
    exec_block = source.split('if ((FIN_RUNTIME_MODE)); then', 1)[1]
    # Only the two terminal invocation lines matter; the earlier
    # ``ROUTE_CONFIG_ARGS=()`` declaration must not confuse the assertion.
    tail = exec_block.split("exec \"$CODEX_BINARY\"", 1)[1]
    final_index = tail.index("FINAL_CODEX_ARGS")
    route_index = tail.index("ROUTE_CONFIG_ARGS")
    assert final_index < route_index


def test_shared_codex_binary_is_not_version_pinned() -> None:
    """CLI upgrades must not require editing the route runtime first."""
    source = _SCRIPT.read_text(encoding="utf-8")

    assert "CODEX_BINARY_EXPECTED_SHA256" not in source
    assert "FIN-owned Codex binary SHA drift" not in source
    # File ownership/mode checks remain the runtime identity boundary.
    assert "Codex binary identity drift" in source


def test_auto_user_config_selector_lands_after_exec_subcommand() -> None:
    """Auto-injected ``--ignore-user-config`` must follow the exec subcommand.

    Native codex only accepts the flag after ``exec``; a global-position
    prepend makes every non-FIN exec invocation (review failover) fail argv
    parsing with "unexpected argument".
    """

    function = _extract_bash_function("insert_ignore_user_config_after_exec")
    script = f"""set -euo pipefail
FINAL_CODEX_ARGS=(exec resume 11111111-2222-3333-4444-555555555555 'prompt text')
{function}
insert_ignore_user_config_after_exec
printf '%s\\n' "${{FINAL_CODEX_ARGS[@]}}"
"""
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "exec",
        "--ignore-user-config",
        "resume",
        "11111111-2222-3333-4444-555555555555",
        "prompt text",
    ]


def test_auto_user_config_selector_fails_closed_without_exec() -> None:
    function = _extract_bash_function("insert_ignore_user_config_after_exec")
    script = f"""set -euo pipefail
FINAL_CODEX_ARGS=(-C /tmp 'prompt')
{function}
insert_ignore_user_config_after_exec
"""
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0


def test_fin_runtime_rejects_last_resume_shape() -> None:
    """A3: FIN runtime 形状 gate 拒绝 resume --last（positional=2 已废除）。"""
    result = _run_fin_shape(["resume", "--last", "prompt"])
    assert result.returncode == 64
    assert "invocation shape is invalid" in result.stderr


def test_fin_runtime_rejects_ephemeral_shape() -> None:
    result = _run_fin_shape(["--ephemeral", "prompt"])
    assert result.returncode == 64
    assert "invocation shape is invalid" in result.stderr


def test_fin_runtime_accepts_exact_resume_shape_before_route_selection() -> None:
    """A3: resume <canonical-uuid> <prompt>（positional=3）通过形状 gate。

    未传 21 项 feature denylist → 确定命中后续 denylist gate 并以其消息
    终止（exit 64、denylist 消息），而不是被形状 gate 拒绝——证明形状
    gate 已放行、执行流到达了确定的下游 gate。
    """
    result = _run_fin_shape(["resume", "019fce86-2b94-7bc3-bcc7-d99757088c56", "prompt"])
    assert "invocation shape is invalid" not in result.stderr
    assert result.returncode == 64
    assert "feature denylist is incomplete" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    (
        ["resume", "not-a-uuid", "prompt"],  # 非 canonical UUID
        ["resume", "uuid", "prompt"],  # 非 UUID
        ["resume", "019FCE86-2B94-7BC3-BCC7-D99757088C56", "prompt"],  # 大写 UUID
    ),
)
def test_fin_runtime_rejects_non_canonical_resume_uuid(arguments: list[str]) -> None:
    """A3: resume 的 session id 必须小写 canonical UUID。"""
    result = _run_fin_shape(arguments)
    assert result.returncode == 64
    assert "resume session id is invalid" in result.stderr


def test_fin_runtime_rejects_wrong_positional_order() -> None:
    """A3-R1-F5: prompt 在 resume 前（错序）→ 拒绝。"""
    result = _run_fin_shape(["prompt", "resume", "019fce86-2b94-7bc3-bcc7-d99757088c56"])
    assert result.returncode == 64
    assert "resume must be the first positional" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    (
        ["resume", "019fce86-2b94-7bc3-bcc7-d99757088c56"],  # 2 positional
        ["resume", "019fce86-2b94-7bc3-bcc7-d99757088c56", "a", "b"],  # 4 positional
    ),
)
def test_fin_runtime_rejects_wrong_positional_count(arguments: list[str]) -> None:
    """A3-R1-F5: resume 形状必须是精确 3 个 positional。"""
    result = _run_fin_shape(arguments)
    assert result.returncode == 64
    assert "invocation shape is invalid" in result.stderr


def test_reject_symlink_ancestors_accepts_normal_path(
    probe_script: str,
    tmp_path: Path,
) -> None:
    target = tmp_path / "real" / "sub"
    target.mkdir(parents=True)
    assert _run_probe(probe_script, str(target / "file")) == 0


def test_reject_symlink_ancestors_blocks_nested_symlink(
    probe_script: str,
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    # 嵌套祖先 symlink：/tmp/.../link 是 symlink，路径深层也不得放行。
    assert _run_probe(probe_script, str(link / "sub" / "file")) == 78


def test_reject_symlink_ancestors_tolerates_missing_path(
    probe_script: str,
    tmp_path: Path,
) -> None:
    # 不存在的路径自身不报错（由后续 config/binary 可用性检查 fail closed）。
    assert _run_probe(probe_script, str(tmp_path / "missing" / "deep" / "file")) == 0


def test_reject_symlink_ancestors_accepts_root(probe_script: str) -> None:
    assert _run_probe(probe_script, "/") == 0


def test_configured_route_accepts_validated_dynamic_api_and_model(tmp_path: Path) -> None:
    function = _extract_bash_function("bind_attested_route")
    route_home = Path("/home/ypk/fin-data/codex-routes/codex-proxy-a")
    environment = {
        **os.environ,
        "FIN_CODEX_ROUTE_LAUNCHER_ID": "codex-proxy-a",
        "FIN_CODEX_ROUTE_ID": "codex-proxy-a",
        "FIN_CODEX_ROUTE_HOME": str(route_home),
        "FIN_CODEX_ROUTE_BASE_URL": "https://ai.apiclub.top/v1",
        "FIN_CODEX_ROUTE_MODEL": "gpt-5.6-sol",
        "FIN_CODEX_ROUTE_CONFIG_SHA256": "a" * 64,
        "FIN_CODEX_ROUTE_AUTH_SHA256": "b" * 64,
    }
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            (
                "set -euo pipefail\n"
                f"{function}\n"
                "bind_attested_route\n"
                "printf '%s|%s|%s|%s' \"$SELECTED_ROUTE\" \"$SELECTED_CODEX_HOME\" "
                '"$ROUTE_BASE_URL" "$ROUTE_MODEL"\n'
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "codex-proxy-a|/home/ypk/fin-data/codex-routes/codex-proxy-a|"
        "https://ai.apiclub.top/v1|gpt-5.6-sol"
    )


def test_configured_route_rejects_home_not_derived_from_route_id() -> None:
    function = _extract_bash_function("bind_attested_route")
    environment = {
        **os.environ,
        "FIN_CODEX_ROUTE_LAUNCHER_ID": "codex-proxy-a",
        "FIN_CODEX_ROUTE_ID": "codex-proxy-a",
        "FIN_CODEX_ROUTE_HOME": "/home/ypk/fin-data/codex-routes/proxy-other",
        "FIN_CODEX_ROUTE_BASE_URL": "https://ai.apiclub.top/v1",
        "FIN_CODEX_ROUTE_MODEL": "gpt-5.6-sol",
        "FIN_CODEX_ROUTE_CONFIG_SHA256": "a" * 64,
        "FIN_CODEX_ROUTE_AUTH_SHA256": "b" * 64,
    }
    result = subprocess.run(
        ["/bin/bash", "-c", f"set -euo pipefail\n{function}\nbind_attested_route\n"],
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )

    assert result.returncode == 78
    assert "binding is invalid" in result.stderr
