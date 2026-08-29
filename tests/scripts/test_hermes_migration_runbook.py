from __future__ import annotations

import json
import re
import shlex
import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNBOOK = _REPO_ROOT / "hermes-migration/README.md"
_OFFICIAL_RUNTIME_RUNBOOK = _REPO_ROOT / "docs/runbooks/hermes-official-runtime-upgrade.md"
_MIGRATION_ROOT = _REPO_ROOT / "hermes-migration"
_MANIFEST = _MIGRATION_ROOT / "MANIFEST.txt"
_CANONICAL_GATEWAY_UNIT = _MIGRATION_ROOT / "systemd/hermes-gateway-fin.service"
_CANONICAL_GATEWAY_DROP_IN = (
    _MIGRATION_ROOT / "systemd/hermes-gateway-fin.service.d/20-fin-python-safety.conf"
)
_PINNED_HERMES = (
    "/home/ypk/.local/share/hermes-agent/venvs/9f13bbbf8423427e159c78066356ca0e27ca6b74/bin/hermes"
)
_INSTALL_SEGMENT_BEGIN = "# BEGIN FIN_CANONICAL_GATEWAY_UNIT_INSTALL"
_INSTALL_SEGMENT_END = "# END FIN_CANONICAL_GATEWAY_UNIT_INSTALL"
_POST_START_SEGMENT_BEGIN = "# BEGIN FIN_GATEWAY_POST_START_SAFETY_CHECK"
_POST_START_SEGMENT_END = "# END FIN_GATEWAY_POST_START_SAFETY_CHECK"
_PRESTOP_SEGMENT_BEGIN = "# BEGIN FIN_GATEWAY_PRESTOP_RELEASE_AND_PIN_CHECK"
_PRESTOP_SEGMENT_END = "# END FIN_GATEWAY_PRESTOP_RELEASE_AND_PIN_CHECK"


def _gateway_install_segment(runbook: str) -> str:
    assert runbook.count(_INSTALL_SEGMENT_BEGIN) == 1
    assert runbook.count(_INSTALL_SEGMENT_END) == 1
    return runbook.split(_INSTALL_SEGMENT_BEGIN, 1)[1].split(_INSTALL_SEGMENT_END, 1)[0]


def _gateway_post_start_segment(runbook: str) -> str:
    assert runbook.count(_POST_START_SEGMENT_BEGIN) == 1
    assert runbook.count(_POST_START_SEGMENT_END) == 1
    return runbook.split(_POST_START_SEGMENT_BEGIN, 1)[1].split(_POST_START_SEGMENT_END, 1)[0]


def _gateway_prestop_segment(runbook: str) -> str:
    assert runbook.count(_PRESTOP_SEGMENT_BEGIN) == 1
    assert runbook.count(_PRESTOP_SEGMENT_END) == 1
    return runbook.split(_PRESTOP_SEGMENT_BEGIN, 1)[1].split(_PRESTOP_SEGMENT_END, 1)[0]


def _continued_commands(runbook: str, *needles: str) -> list[str]:
    commands: list[str] = []
    lines = runbook.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "env -i \\":
            continue
        command_lines = [line]
        while command_lines[-1].rstrip().endswith("\\"):
            index += 1
            command_lines.append(lines[index])
        command = "\n".join(command_lines)
        if all(needle in command for needle in needles):
            commands.append(command)
    return commands


def _continued_command(runbook: str, *needles: str) -> str:
    commands = _continued_commands(runbook, *needles)
    assert len(commands) == 1
    return commands[0]


def _bash_block_containing(runbook: str, *needles: str) -> str:
    blocks = re.findall(r"```bash\n(.*?)\n```", runbook, flags=re.DOTALL)
    matches = [block for block in blocks if all(needle in block for needle in needles)]
    assert len(matches) == 1
    return matches[0]


def _unit_root_snapshot(root: Path) -> tuple[tuple[str, int, bytes], ...]:
    return tuple(
        (
            path.relative_to(root).as_posix(),
            stat.S_IMODE(path.lstat().st_mode),
            path.read_bytes() if path.is_file() else b"",
        )
        for path in sorted(root.rglob("*"))
    )


def test_runbook_uses_official_hermes_with_stateless_external_integration() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")

    assert "Hermes-Agent 始终使用官方发行版" in runbook
    assert "不在 FIN 仓库维护 fork、补丁或源码覆盖层" in runbook
    assert "无跨请求状态" in runbook
    assert "不保存 execution receipt" in runbook
    assert "`tool_execution`（A5L-1）" in runbook
    assert "整包覆盖调用方同名字段" in runbook


def test_runbook_installs_only_the_current_release_plugin_and_skill_tree() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")

    assert (
        "/home/ypk/.local/share/fin-analyse/current/hermes-migration/plugins/"
        "fin-consultation-first-tool"
    ) in runbook
    assert runbook.count('"$CURRENT/scripts/apply_fin_hermes_external_integration.py" apply') == 1
    assert runbook.count('"$CURRENT/scripts/apply_fin_hermes_external_integration.py" check') == 1
    assert "-m scripts.apply_fin_hermes_external_integration" not in runbook
    assert runbook.count('--expected-current-commit "$FIN_SHA"') == 2
    assert "rsync -a --delete" not in runbook
    assert "精确两份 FIN Skill" in runbook
    assert "不静默覆盖或删除" in runbook
    assert "不创建旧 profile、旧插件或旧 release" in runbook
    assert "不创建 backup、receipt 或 state" in runbook
    assert 'unlink "$PROFILE/plugins/fin-consultation-first-tool"' not in runbook
    assert "切换无需拆除或重建已有链接" in runbook
    assert "hermes plugins enable fin-consultation-first-tool" not in runbook


def test_gateway_runtime_is_bytecode_safe_and_rechecked_after_start() -> None:
    migration_runbook = _RUNBOOK.read_text(encoding="utf-8")
    official_runbook = _OFFICIAL_RUNTIME_RUNBOOK.read_text(encoding="utf-8")
    unit_lines = _CANONICAL_GATEWAY_UNIT.read_text(encoding="utf-8").splitlines()
    safety_environment = (
        'Environment="PYTHONSAFEPATH=1"',
        'Environment="PYTHONNOUSERSITE=1"',
        'Environment="PYTHONDONTWRITEBYTECODE=1"',
    )
    provider_environment_file = (
        "EnvironmentFile=/home/ypk/.config/fin-analyse/llm.env"
    )

    assert all(line not in unit_lines for line in safety_environment)
    assert provider_environment_file not in unit_lines
    assert _CANONICAL_GATEWAY_DROP_IN.read_text(encoding="utf-8") == (
        "[Service]\n"
        + provider_environment_file
        + "\n"
        + "\n".join(safety_environment)
        + "\n"
    )

    runtime_sequence = (
        'install -m 600 "$BASE_SOURCE" "$UNIT_PATH"',
        "install -d -m 700",
        'install -m 600 "$DROP_IN_SOURCE" "$DROP_IN_PATH"',
        "systemctl --user daemon-reload",
        "systemctl --user start hermes-gateway-fin.service",
    )
    release_assertions = (
        ".ready == true",
        ".code.frozen_sync_receipt == true",
        ".code.unexpected_untracked == []",
        ".code.unexpected_ignored == []",
    )
    for runbook in (migration_runbook, official_runbook):
        assert provider_environment_file in runbook
        for line in safety_environment:
            assert line in runbook
        for fragment in (*runtime_sequence, *release_assertions):
            assert fragment in runbook
        assert runbook.count('/scripts/prepare_fin_release.py" check') >= 2
        positions = tuple(runbook.index(fragment) for fragment in runtime_sequence)
        assert positions == tuple(sorted(positions))
        assert "10-official-release.conf" in runbook
        assert "unexpected/obsolete" in runbook
        assert "原子刷新" not in runbook

    assert "refresh_systemd_unit_if_needed()" in official_runbook
    assert "base unit + FIN-owned drop-in" in official_runbook


def test_gateway_install_validation_failure_leaves_temporary_unit_root_unchanged(
    tmp_path: Path,
) -> None:
    for index, runbook_path in enumerate((_RUNBOOK, _OFFICIAL_RUNTIME_RUNBOOK)):
        root = tmp_path / str(index)
        unit_root = root / "systemd/user"
        unit_path = unit_root / "hermes-gateway-fin.service"
        drop_in_dir = unit_path.with_name(f"{unit_path.name}.d")
        drop_in_path = drop_in_dir / "20-fin-python-safety.conf"
        base_source = root / "candidate/hermes-gateway-fin.service"
        drop_in_source = root / "candidate/20-fin-python-safety.conf"
        unit_root.mkdir(parents=True)
        unit_root.chmod(0o700)
        unit_path.write_bytes(b"existing-base\n")
        unit_path.chmod(0o600)
        drop_in_dir.mkdir(mode=0o700)
        (drop_in_dir / "10-official-release.conf").write_bytes(b"obsolete\n")
        base_source.parent.mkdir(parents=True)
        base_source.write_bytes(b"candidate-base\n")
        drop_in_source.write_bytes(_CANONICAL_GATEWAY_DROP_IN.read_bytes())
        before = _unit_root_snapshot(unit_root)

        completed = subprocess.run(
            (
                "/bin/bash",
                "-c",
                "set -euo pipefail\n"
                + _gateway_install_segment(runbook_path.read_text(encoding="utf-8")),
            ),
            check=False,
            capture_output=True,
            env={
                "BASE_SOURCE": str(base_source),
                "DROP_IN_DIR": str(drop_in_dir),
                "DROP_IN_PATH": str(drop_in_path),
                "DROP_IN_SOURCE": str(drop_in_source),
                "PATH": "/usr/bin:/bin",
                "UNIT_PATH": str(unit_path),
                "UNIT_ROOT": str(unit_root),
            },
        )

        assert completed.returncode != 0
        assert completed.stdout == b""
        assert b"unexpected/obsolete gateway drop-in" in completed.stderr
        assert _unit_root_snapshot(unit_root) == before


def test_gateway_install_rejects_unsafe_root_or_base_destination_without_writes(
    tmp_path: Path,
) -> None:
    variants = (
        "group_writable_root",
        "symlink_root",
        "symlink_base",
        "non_regular_base",
        "multi_link_base",
        "multi_link_drop_in",
    )
    for runbook_index, runbook_path in enumerate((_RUNBOOK, _OFFICIAL_RUNTIME_RUNBOOK)):
        segment = _gateway_install_segment(runbook_path.read_text(encoding="utf-8"))
        for variant in variants:
            root = tmp_path / f"{runbook_index}-{variant}"
            real_unit_root = root / "real-systemd-user"
            real_unit_root.mkdir(parents=True)
            real_unit_root.chmod(0o700)
            unit_root = real_unit_root
            if variant == "symlink_root":
                unit_root = root / "systemd/user"
                unit_root.parent.mkdir()
                unit_root.symlink_to(real_unit_root)
            unit_path = unit_root / "hermes-gateway-fin.service"
            drop_in_dir = unit_path.with_name(f"{unit_path.name}.d")
            drop_in_path = drop_in_dir / "20-fin-python-safety.conf"
            base_source = root / "candidate/hermes-gateway-fin.service"
            drop_in_source = root / "candidate/20-fin-python-safety.conf"
            base_source.parent.mkdir()
            base_source.write_bytes(b"candidate-base\n")
            drop_in_source.write_bytes(_CANONICAL_GATEWAY_DROP_IN.read_bytes())

            if variant == "group_writable_root":
                unit_root.chmod(0o770)
            elif variant == "symlink_base":
                outside_base = root / "outside-base"
                outside_base.write_bytes(b"existing-base\n")
                unit_path.symlink_to(outside_base)
            elif variant == "non_regular_base":
                unit_path.mkdir()
            elif variant == "multi_link_base":
                unit_path.write_bytes(b"existing-base\n")
                unit_path.chmod(0o600)
                (root / "second-base-link").hardlink_to(unit_path)
            elif variant == "multi_link_drop_in":
                unit_path.write_bytes(b"existing-base\n")
                unit_path.chmod(0o600)
                drop_in_dir.mkdir(mode=0o700)
                drop_in_path.write_bytes(_CANONICAL_GATEWAY_DROP_IN.read_bytes())
                drop_in_path.chmod(0o600)
                (root / "second-drop-in-link").hardlink_to(drop_in_path)

            before = _unit_root_snapshot(root)
            completed = subprocess.run(
                (
                    "/bin/bash",
                    "-c",
                    "set -euo pipefail\n" + segment,
                ),
                check=False,
                capture_output=True,
                env={
                    "BASE_SOURCE": str(base_source),
                    "DROP_IN_DIR": str(drop_in_dir),
                    "DROP_IN_PATH": str(drop_in_path),
                    "DROP_IN_SOURCE": str(drop_in_source),
                    "PATH": "/usr/bin:/bin",
                    "UNIT_PATH": str(unit_path),
                    "UNIT_ROOT": str(unit_root),
                },
            )

            assert completed.returncode != 0, variant
            assert _unit_root_snapshot(root) == before, variant


def test_gateway_install_validates_root_and_destinations_before_first_write() -> None:
    validation_fragments = (
        'test ! -L "$UNIT_ROOT"',
        'test -d "$UNIT_ROOT"',
        'readlink -f -- "$UNIT_ROOT"',
        "stat -c '%u' \"$UNIT_ROOT\"",
        "8#$UNIT_ROOT_MODE & 022",
        '[ -e "$UNIT_PATH" ]',
        'test ! -L "$UNIT_PATH"',
        'test -f "$UNIT_PATH"',
        'readlink -f -- "$UNIT_PATH"',
        "stat -c '%h:%u:%a' \"$UNIT_PATH\"",
        '[ -L "$DROP_IN_DIR" ]',
        '[ ! -d "$DROP_IN_DIR" ]',
        'readlink -f -- "$DROP_IN_DIR"',
        "stat -c '%u:%a' \"$DROP_IN_DIR\"",
        'find "$DROP_IN_DIR" -mindepth 1 -maxdepth 1 -print0',
        "unexpected/obsolete gateway drop-in",
        'test ! -L "$DROP_IN_PATH"',
        'test -f "$DROP_IN_PATH"',
        'readlink -f -- "$DROP_IN_PATH"',
        "stat -c '%h:%u:%a' \"$DROP_IN_PATH\"",
        'cmp -s "$DROP_IN_SOURCE" "$DROP_IN_PATH"',
    )
    write_sequence = (
        'install -m 600 "$BASE_SOURCE" "$UNIT_PATH"',
        'install -d -m 700 "$DROP_IN_DIR"',
        'install -m 600 "$DROP_IN_SOURCE" "$DROP_IN_PATH"',
    )

    for runbook_path in (_RUNBOOK, _OFFICIAL_RUNTIME_RUNBOOK):
        runbook = runbook_path.read_text(encoding="utf-8")
        segment = _gateway_install_segment(runbook)
        first_write = segment.index(write_sequence[0])
        for fragment in validation_fragments:
            assert segment.index(fragment) < first_write
        for fragment in write_sequence:
            assert runbook.count(fragment) == 1
        write_positions = tuple(segment.index(fragment) for fragment in write_sequence)
        assert write_positions == tuple(sorted(write_positions))


def test_gateway_post_start_safety_is_proven_before_release_readiness() -> None:
    required_environment = (
        "PYTHONSAFEPATH=1",
        "PYTHONNOUSERSITE=1",
        "PYTHONDONTWRITEBYTECODE=1",
    )

    for runbook_path in (_RUNBOOK, _OFFICIAL_RUNTIME_RUNBOOK):
        runbook = runbook_path.read_text(encoding="utf-8")
        segment = _gateway_post_start_segment(runbook)
        assert "--property=DropInPaths --value" in segment
        assert 'test "$ACTIVE_DROP_IN_PATHS" = "$DROP_IN_PATH"' in segment
        assert "--property=MainPID --value" in segment
        assert 'test "$MAIN_PID" -gt 0' in segment
        assert '"/proc/$MAIN_PID/environ"' in segment
        assert "IFS= read -r -d '' entry" in segment
        for expected in required_environment:
            assert expected in segment
        assert "${required%%=*}" in segment
        assert "cat " not in segment
        assert "tr " not in segment
        assert "strings " not in segment

        start = runbook.index("systemctl --user start hermes-gateway-fin.service")
        post_start = runbook.index(_POST_START_SEGMENT_BEGIN)
        readiness = runbook.index(
            '"$CURRENT/scripts/prepare_fin_release.py" check',
            post_start,
        )
        block_start = runbook.rfind("```bash", 0, post_start)
        fail_closed = runbook.index("set -euo pipefail", block_start, post_start)
        daemon_reload = runbook.index("systemctl --user daemon-reload", block_start)
        assert fail_closed < daemon_reload < start
        assert start < post_start < readiness
        acceptance_block = _bash_block_containing(
            runbook,
            _POST_START_SEGMENT_BEGIN,
            '"$CURRENT/scripts/prepare_fin_release.py" check',
        )
        handler = acceptance_block.index("stop_gateway_after_failed_acceptance()")
        disable_inside_handler = acceptance_block.index("trap - EXIT", handler)
        stop_inside_handler = acceptance_block.index(
            "systemctl --user stop hermes-gateway-fin.service",
            handler,
        )
        register = acceptance_block.index(
            """trap 'stop_gateway_after_failed_acceptance "$?"' EXIT"""
        )
        daemon_reload = acceptance_block.index("systemctl --user daemon-reload")
        start_attempted = acceptance_block.index("GATEWAY_START_ATTEMPTED=true")
        start = acceptance_block.index("systemctl --user start hermes-gateway-fin.service")
        readiness = acceptance_block.index('"$CURRENT/scripts/prepare_fin_release.py" check')
        successful_disarm = acceptance_block.rindex("trap - EXIT")
        assert handler < disable_inside_handler < stop_inside_handler < register
        assert register < daemon_reload < start_attempted < start < readiness
        assert readiness < successful_disarm
        assert acceptance_block.count("trap - EXIT") == 2


def test_post_start_failure_stops_gateway_and_preserves_original_status(
    tmp_path: Path,
) -> None:
    required_environment = (
        "PYTHONSAFEPATH=1",
        "PYTHONNOUSERSITE=1",
        "PYTHONDONTWRITEBYTECODE=1",
    )
    modes = (
        "drop_in_paths",
        "main_pid",
        "missing_PYTHONSAFEPATH",
        "missing_PYTHONNOUSERSITE",
        "missing_PYTHONDONTWRITEBYTECODE",
        "readiness",
        "success",
    )
    fake_systemctl = """\
#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
case "$*" in
  "--user daemon-reload" | "--user start hermes-gateway-fin.service")
    exit 0
    ;;
  "--user stop hermes-gateway-fin.service")
    exit 73
    ;;
  *"--property=DropInPaths --value")
    if [ "$FAKE_GATEWAY_MODE" = drop_in_paths ]; then
      printf '/unexpected/drop-in.conf\n'
    else
      printf '%s\n' "$FAKE_DROP_IN_PATH"
    fi
    ;;
  *"--property=MainPID --value")
    if [ "$FAKE_GATEWAY_MODE" = main_pid ]; then
      printf '0\n'
    else
      printf '4242\n'
    fi
    ;;
  *)
    exit 98
    ;;
esac
"""

    for runbook_index, runbook_path in enumerate((_RUNBOOK, _OFFICIAL_RUNTIME_RUNBOOK)):
        runbook = runbook_path.read_text(encoding="utf-8")
        block = _bash_block_containing(
            runbook,
            _POST_START_SEGMENT_BEGIN,
            '"$CURRENT/scripts/prepare_fin_release.py" check',
        )
        for mode in modes:
            root = tmp_path / f"{runbook_index}-{mode}"
            fake_bin = root / "bin"
            fake_bin.mkdir(parents=True)
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(fake_systemctl, encoding="utf-8")
            systemctl.chmod(0o700)
            systemctl_log = root / "systemctl.log"
            current = root / "current"
            (current / ".venv/bin").mkdir(parents=True)
            (current / ".venv/bin/python").symlink_to(_REPO_ROOT / ".venv/bin/python")
            (current / "scripts").mkdir()
            ready = mode != "readiness"
            (current / "scripts/prepare_fin_release.py").write_text(
                "import json\n"
                f"print(json.dumps({{'ready': {ready!r}, "
                "'code': {'frozen_sync_receipt': True, "
                "'unexpected_untracked': [], 'unexpected_ignored': []}}))\n",
                encoding="utf-8",
            )
            missing = mode.removeprefix("missing_") if mode.startswith("missing_") else None
            environ_path = root / "environ"
            environ_path.write_bytes(
                b"".join(
                    f"{entry}\0".encode()
                    for entry in required_environment
                    if entry.split("=", 1)[0] != missing
                )
            )
            unit_path = root / "hermes-gateway-fin.service"
            executable_block = block.replace(
                "UNIT_PATH=/home/ypk/.config/systemd/user/hermes-gateway-fin.service",
                f"UNIT_PATH={shlex.quote(str(unit_path))}",
            ).replace(
                "CURRENT=/home/ypk/.local/share/fin-analyse/current",
                f"CURRENT={shlex.quote(str(current))}",
            )
            executable_block = executable_block.replace(
                'ENVIRON_PATH="/proc/$MAIN_PID/environ"',
                f"ENVIRON_PATH={shlex.quote(str(environ_path))}",
            )
            completed = subprocess.run(
                ("/bin/bash", "-c", executable_block),
                check=False,
                capture_output=True,
                env={
                    "FAKE_DROP_IN_PATH": (f"{unit_path}.d/20-fin-python-safety.conf"),
                    "FAKE_GATEWAY_MODE": mode,
                    "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
                    "HOME": str(root),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                },
            )
            calls = systemctl_log.read_text(encoding="utf-8").splitlines()
            stops = [call for call in calls if call == "--user stop hermes-gateway-fin.service"]
            output = completed.stdout + completed.stderr
            for environment_entry in required_environment:
                assert environment_entry.encode() not in output

            if mode == "success":
                assert completed.returncode == 0
                assert stops == []
            else:
                assert completed.returncode == 1, mode
                assert stops == ["--user stop hermes-gateway-fin.service"], mode


def test_official_runtime_pin_and_updated_fin_release_are_proven_before_stop() -> None:
    pin_checks = (
        'EXPECTED_EXEC_START="ExecStart=$HERMES_RUNTIME/bin/python '
        '-m hermes_cli.main --profile fin gateway run"',
        'EXPECTED_VIRTUAL_ENV="Environment=\\"VIRTUAL_ENV=$HERMES_RUNTIME\\""',
        "grep -oF 'ExecStart=' \"$BASE_SOURCE\" | wc -l",
        'grep -Fxc "$EXPECTED_EXEC_START" "$BASE_SOURCE"',
        "grep -oF 'VIRTUAL_ENV=' \"$BASE_SOURCE\" | wc -l",
        'grep -Fxc "$EXPECTED_VIRTUAL_ENV" "$BASE_SOURCE"',
    )
    readiness_checks = (
        'PRESTOP_CHECK_JSON="$(',
        '"$CANDIDATE/.venv/bin/python" -I -B',
        '"$CANDIDATE/scripts/prepare_fin_release.py" check',
        "--home /home/ypk",
        '--commit "$FIN_SHA"',
        'jq -e --arg commit "$FIN_SHA" \'',
        ".ready == true",
        ".commit == $commit",
    )

    for runbook_path in (_RUNBOOK, _OFFICIAL_RUNTIME_RUNBOOK):
        runbook = runbook_path.read_text(encoding="utf-8")
        segment = _gateway_prestop_segment(runbook)
        for fragment in (*pin_checks, *readiness_checks):
            assert fragment in segment
        assert 'CANDIDATE="/home/ypk/.local/share/fin-analyse/releases/$FIN_SHA"' in runbook
        assert (
            'BASE_SOURCE="$CANDIDATE/hermes-migration/systemd/hermes-gateway-fin.service"'
        ) in runbook
        assert "PYTHONPATH=" not in segment
        assert "-m scripts.prepare_fin_release" not in segment
        assert runbook.index(_PRESTOP_SEGMENT_END) < runbook.index(
            "systemctl --user stop hermes-gateway-fin.service"
        )
        stop = runbook.index("systemctl --user stop hermes-gateway-fin.service")
        activation = runbook.index(
            '"$CANDIDATE/scripts/prepare_fin_release.py" activate',
            stop,
        )
        install = runbook.index('install -m 600 "$BASE_SOURCE" "$UNIT_PATH"', activation)
        assert stop < activation < install

    migration_upgrade = _RUNBOOK.read_text(encoding="utf-8").split("## Hermes 官方升级", 1)[1]
    for fragment in ("停止 gateway 前", "`ExecStart`", "`VIRTUAL_ENV`", "`check.ready=true`"):
        assert fragment in migration_upgrade


def test_prestop_gate_rejects_unquoted_conflicting_virtual_env(
    tmp_path: Path,
) -> None:
    commit = "f" * 40
    hermes_runtime = tmp_path / "official-hermes"
    candidate = tmp_path / "candidate"
    base_source = candidate / "hermes-migration/systemd/hermes-gateway-fin.service"
    interpreter = _REPO_ROOT / ".venv/bin/python"
    (candidate / ".venv/bin").mkdir(parents=True)
    (candidate / ".venv/bin/python").symlink_to(interpreter)
    (candidate / "scripts").mkdir()
    (candidate / "scripts/prepare_fin_release.py").write_text(
        "import json\n"
        f"print(json.dumps({{'ready': True, 'commit': {commit!r}, "
        "'code': {'frozen_sync_receipt': True, "
        "'unexpected_untracked': [], 'unexpected_ignored': []}}))\n",
        encoding="utf-8",
    )
    base_source.parent.mkdir(parents=True)
    base_source.write_text(
        "[Service]\n"
        f"ExecStart={hermes_runtime}/bin/python "
        "-m hermes_cli.main --profile fin gateway run\n"
        f'Environment="VIRTUAL_ENV={hermes_runtime}"\n'
        "Environment=VIRTUAL_ENV=/unexpected/runtime\n",
        encoding="utf-8",
    )
    before = base_source.read_bytes()
    command_env = {
        "BASE_SOURCE": str(base_source),
        "CANDIDATE": str(candidate),
        "FIN_SHA": commit,
        "HERMES_RUNTIME": str(hermes_runtime),
        "PATH": "/usr/bin:/bin",
    }

    for runbook_path in (_RUNBOOK, _OFFICIAL_RUNTIME_RUNBOOK):
        completed = subprocess.run(
            (
                "/bin/bash",
                "-c",
                "set -euo pipefail\n"
                + _gateway_prestop_segment(runbook_path.read_text(encoding="utf-8")),
            ),
            check=False,
            capture_output=True,
            env=command_env,
        )

        assert completed.returncode != 0
        assert base_source.read_bytes() == before


def test_release_operator_commands_ignore_ambient_python_shadow(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    current = tmp_path / "current"
    shadow = tmp_path / "ambient-shadow"
    shadow_marker = tmp_path / "ambient-shadow-ran"
    interpreter = _REPO_ROOT / ".venv/bin/python"
    tracer = """\
import json
import os
import sys

print(json.dumps({
    "argv": sys.argv,
    "env": {
        key: os.environ.get(key)
        for key in (
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONHOME",
            "PYTHONPATH",
        )
    },
}))
"""
    for owner in (candidate, current):
        (owner / ".venv/bin").mkdir(parents=True)
        (owner / ".venv/bin/python").symlink_to(interpreter)
        (owner / "scripts").mkdir()
        (owner / "scripts/prepare_fin_release.py").write_text(
            tracer,
            encoding="utf-8",
        )
    (current / "scripts/apply_fin_hermes_external_integration.py").write_text(
        tracer,
        encoding="utf-8",
    )
    shadow.mkdir()
    (shadow / "python").write_text(
        f"#!/bin/sh\n: > {shadow_marker}\nexit 97\n",
        encoding="utf-8",
    )
    (shadow / "python").chmod(0o700)
    (shadow / "scripts").mkdir()
    (shadow / "scripts/prepare_fin_release.py").write_text(
        f"from pathlib import Path\nPath({str(shadow_marker)!r}).touch()\n",
        encoding="utf-8",
    )
    (shadow / "scripts/apply_fin_hermes_external_integration.py").write_text(
        f"from pathlib import Path\nPath({str(shadow_marker)!r}).touch()\n",
        encoding="utf-8",
    )
    ambient = {
        "CANDIDATE": str(candidate),
        "CURRENT": str(current),
        "FIN_SHA": "f" * 40,
        "HOME": str(tmp_path / "ambient-home"),
        "LANG": "ambient",
        "LC_ALL": "ambient",
        "PATH": f"{shadow}:/usr/bin:/bin",
        "PRIOR_FIN_SHA": "e" * 40,
        "PRIOR_SHA": "e" * 40,
        "PYTHONHOME": str(shadow),
        "PYTHONPATH": str(shadow),
    }
    expected = (
        (
            '"$CANDIDATE/scripts/prepare_fin_release.py" check',
            '"$CANDIDATE/.venv/bin/python" -I -B',
            candidate / "scripts/prepare_fin_release.py",
            "check",
        ),
        (
            '"$CANDIDATE/scripts/prepare_fin_release.py" activate',
            '"$CANDIDATE/.venv/bin/python" -I -B',
            candidate / "scripts/prepare_fin_release.py",
            "activate",
        ),
        (
            '"$CURRENT/scripts/prepare_fin_release.py" check',
            '"$CURRENT/.venv/bin/python" -I -B',
            current / "scripts/prepare_fin_release.py",
            "check",
        ),
    )

    for runbook_path in (_RUNBOOK, _OFFICIAL_RUNTIME_RUNBOOK):
        runbook = runbook_path.read_text(encoding="utf-8")
        for needle, interpreter_command, owned_script, action in expected:
            command = _continued_command(runbook, needle, "--home /home/ypk")
            assert interpreter_command in command
            assert "PYTHONPATH" not in command
            assert " -m " not in command
            completed = subprocess.run(
                (
                    "/bin/bash",
                    "-c",
                    command,
                ),
                check=True,
                capture_output=True,
                env=ambient,
                text=True,
            )
            trace = json.loads(completed.stdout)
            assert trace["argv"][0] == str(owned_script)
            assert trace["argv"][1] == action
            assert trace["argv"][2:4] == ["--home", "/home/ypk"]
            assert trace["env"] == {
                "HOME": "/home/ypk",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHOME": None,
                "PYTHONPATH": None,
            }
        assert "PYTHONPATH=" not in _gateway_prestop_segment(runbook)
        expected_external_commands = 1 if runbook_path == _RUNBOOK else 2
        for action in ("apply", "check"):
            commands = _continued_commands(
                runbook,
                (f'"$CURRENT/scripts/apply_fin_hermes_external_integration.py" {action}'),
                "--home /home/ypk",
            )
            assert len(commands) == expected_external_commands
            for command in commands:
                assert '"$CURRENT/.venv/bin/python" -I -B' in command
                assert "PYTHONPATH" not in command
                assert " -m " not in command
                completed = subprocess.run(
                    (
                        "/bin/bash",
                        "-c",
                        command.replace("<fin-current-full-commit>", "d" * 40),
                    ),
                    check=True,
                    capture_output=True,
                    env=ambient,
                    text=True,
                )
                trace = json.loads(completed.stdout)
                assert trace["argv"][0] == str(
                    current / "scripts/apply_fin_hermes_external_integration.py"
                )
                assert trace["argv"][1:4] == [action, "--home", "/home/ypk"]
                assert trace["argv"][4] == "--expected-current-commit"
                assert trace["env"] == {
                    "HOME": "/home/ypk",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONHOME": None,
                    "PYTHONPATH": None,
                }
        assert "-m scripts.apply_fin_hermes_external_integration" not in runbook

    assert not shadow_marker.exists()


def test_runbook_requires_official_live_canary_and_source_role_boundaries() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")

    assert f"{_PINNED_HERMES} plugins list" in runbook
    assert f"{_PINNED_HERMES} mcp test fin-analyse" in runbook
    assert (
        f"PYTHONDONTWRITEBYTECODE=1 \\\n  {_PINNED_HERMES} \\\n"
        "  chat -Q -q '今天市场主线怎么看？' --source tool"
    ) in runbook
    assert "`list_capabilities`" in runbook
    assert "不是咨询前置路由" in runbook
    assert "自然答案开头" in runbook
    assert "不要求“FIN 认知代理”" in runbook
    assert "不根据正文关键词自行推断" in runbook
    assert "不把任一市场 evidence 写成老师原话或 Z" in runbook
    assert "第二阶段不再调用外部 provider" in runbook


def test_runbook_has_no_retired_strict_control_plane_commands() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")

    for retired in (
        "activate_fin_hermes_consultation_bridge",
        "cleanup_fin_hermes_legacy_consultation",
        "fin_consultation_cutover",
        "hermes_consultation_strict_session_canary",
    ):
        assert retired not in runbook


def test_runbook_clean_break_keeps_scheduler_inventory_empty() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")

    assert "user crontab、FIN profile 与 global Hermes cron" in runbook
    assert "systemd user timer 也为零" in runbook
    assert "一个 owner、一个 job" in runbook
    assert "不得同时保留 cron、Hermes cron 与额外的业务 systemd timer" in runbook
    assert "不是第二 capture scheduler" in runbook
    for retired_path in (
        "runtime/fin-plugins",
        "runtime/turn-execution-receipts",
        "skills/.curator_backups",
        "state-snapshots",
        "/home/ypk/.hermes/skills/.curator_backups",
        "home/.claude/backups",
    ):
        assert retired_path in runbook
    assert "新 canary 成功后" in runbook
    assert "retirement bundle" in runbook
    assert 'rm -rf "$PROFILE"' not in runbook


def test_migration_manifest_matches_the_current_bundle() -> None:
    manifest = _MANIFEST.read_text(encoding="utf-8")
    declared: dict[str, int] = {}
    for line in manifest.splitlines():
        match = re.fullmatch(r"([0-9]+)\s+(.+)", line)
        if match is not None:
            declared[match.group(2)] = int(match.group(1))

    actual = {
        path.relative_to(_MIGRATION_ROOT).as_posix(): path.stat().st_size
        for path in _MIGRATION_ROOT.rglob("*")
        if path.is_file()
        and path != _MANIFEST
        and "__pycache__" not in path.parts  # 运行时生成的字节码不进清单
    }
    assert declared == actual
    assert f"文件数: {len(actual)}" in manifest
    assert f"总体积: canonical ({sum(actual.values())} bytes)" in manifest
