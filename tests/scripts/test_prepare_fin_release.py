from __future__ import annotations

import ast
import errno
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import venv
from pathlib import Path

import pytest

import scripts.prepare_fin_release as release_tool
from scripts.prepare_fin_release import (
    ReleaseLayout,
    activate_release,
    capture_previous_release_readiness,
    inspect_release,
    locked_ready_release,
    locked_release_read_only,
    main,
    prepare_release_bindings,
    record_frozen_sync,
    verify_recorded_previous_release_readiness,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FROZEN_PRE_HANDOFF_CHECKER_COMMIT = "71d86ee975a6b9ba5d31d9f74e8da42da918fc22"
_FROZEN_LEGACY_CHECKER_COMMIT = "822d069d8aceba28a76186950d85583c3f9ec5ba"
_FROZEN_ACTIVE_LEGACY_CHECKER_COMMIT = "575f12a30f99397f47ce1e5397fb627a78acfdae"
_FROZEN_PREVIOUS_CURRENT_CHECKER_COMMIT = "bd324f39e8b51e15315c58c0f67cb1a52e29c390"
_FROZEN_COMPATIBLE_CURRENT_CHECKER_COMMIT = "a8d21c6d3ab7d83309c5d9bf0c53dbaa1da1d8fd"
_FROZEN_BRIDGE_CURRENT_CHECKER_COMMIT = "be2239f9ba4263bd029e92b153841583991b35e2"
_FROZEN_ACTIVE_CURRENT_CHECKER_COMMIT = "c4d59b871c261e7d6b701f8e6e500e0f2c483c11"
_FROZEN_LIVE_CURRENT_CHECKER_COMMIT = "2ea93f02effa5adb1ec0c9ceae88d9e7b1e81b05"
_FROZEN_PRODUCTION_CURRENT_CHECKER_COMMIT = "2b79b58e3ab2e53809def6fffd900473fcba485d"
_FROZEN_CUTOVER_CURRENT_CHECKER_COMMIT = "eccf8ef78883819aaca4a2c93fd0236d61629cfa"
_FROZEN_LATEST_CURRENT_CHECKER_COMMIT = "df7cea1edf507f44442df061b7684071cd05477f"
_FROZEN_FOUR_TARGET_CHECKER_COMMIT = "707a288003ecee7eb5308bd0924f56c2155beb9e"
_FROZEN_DEPLOYED_FOUR_TARGET_CHECKER_COMMIT = (
    "75915b778795c64dc334588b947ed876690349b2"
)
_FROZEN_CHECKER_LINEAGE = (
    (
        _FROZEN_PRE_HANDOFF_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.PRE_HANDOFF,
    ),
    (
        "ef9ee1b99b6ce499dd2f68be92deba74258210e4",
        release_tool._SpecialHandoffModeContract.LEGACY_V2_TWO_TARGET,
    ),
    (
        "5d2e808d74aa831263472b89a4edd9bb44183b56",
        release_tool._SpecialHandoffModeContract.LEGACY_V2_TWO_TARGET,
    ),
    (
        _FROZEN_LEGACY_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.LEGACY_V2_TWO_TARGET,
    ),
    (
        _FROZEN_ACTIVE_LEGACY_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.LEGACY_V2_TWO_TARGET,
    ),
    (
        _FROZEN_PREVIOUS_CURRENT_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        _FROZEN_COMPATIBLE_CURRENT_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        _FROZEN_BRIDGE_CURRENT_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        _FROZEN_ACTIVE_CURRENT_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        _FROZEN_LIVE_CURRENT_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        _FROZEN_PRODUCTION_CURRENT_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        _FROZEN_CUTOVER_CURRENT_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        _FROZEN_LATEST_CURRENT_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        _FROZEN_FOUR_TARGET_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.CURRENT_V2_FOUR_TARGET,
    ),
    (
        _FROZEN_DEPLOYED_FOUR_TARGET_CHECKER_COMMIT,
        release_tool._SpecialHandoffModeContract.CURRENT_V2_FOUR_TARGET,
    ),
)


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _frozen_checker_source_at(commit: str) -> str:
    completed = subprocess.run(
        ("git", "show", f"{commit}:scripts/prepare_fin_release.py"),
        cwd=_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _string_tuple_from_checker_source(source: str, variable: str) -> tuple[str, ...]:
    assignments = {
        statement.targets[0].id: statement.value
        for statement in ast.parse(source).body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    }

    def resolve(node: ast.expr) -> tuple[str, ...]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return (node.value,)
        if isinstance(node, ast.Name):
            return resolve(assignments[node.id])
        if isinstance(node, ast.Starred):
            return resolve(node.value)
        if isinstance(node, ast.Tuple):
            return tuple(relative for element in node.elts for relative in resolve(element))
        raise AssertionError(f"unsupported frozen checker tuple expression: {ast.dump(node)}")

    value = assignments.get(variable)
    return () if value is None else resolve(value)


def _critical_runtime_files_from_checker_source(source: str) -> tuple[str, ...]:
    return _string_tuple_from_checker_source(source, "_CRITICAL_RUNTIME_FILES")


def _required_regular_files_from_checker_source(source: str) -> tuple[str, ...]:
    return _string_tuple_from_checker_source(source, "_REQUIRED_REGULAR_FILES")


def _authorize_checker_source_for_test(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> str:
    source_sha256 = hashlib.sha256(source.encode()).hexdigest()
    monkeypatch.setattr(
        release_tool,
        "_FROZEN_CHECKER_CONTRACTS_BY_SHA256",
        (
            *release_tool._FROZEN_CHECKER_CONTRACTS_BY_SHA256,
            (
                source_sha256,
                release_tool._SpecialHandoffModeContract.PRE_HANDOFF,
            ),
        ),
    )
    return source_sha256


def _seed_release(
    tmp_path: Path,
    *,
    record_sync: bool = True,
    extra_tracked_files: tuple[str, ...] = (),
) -> ReleaseLayout:
    home = tmp_path / "home"
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "scripts").mkdir()
    for relative in (
        "fin_analyse/__init__.py",
        "fin_analyse/common/execution_control.py",
        "fin_analyse/consultation/runtime_budget.py",
        "fin_analyse/consultation/runtime_budget.v1.json",
        "fin_analyse/consultation/presentation.py",
        "fin_analyse/claims/config_loader.py",
        "fin_analyse/gateway/tool_presentation.py",
        "fin_analyse/market/evidence_plan.py",
        "fin_analyse/market/market_evidence_plan.v1.json",
        "fin_analyse/runtime/hermes_managed_assets.py",
        "fin_analyse/runtime/hermes_managed_assets.v1.json",
        "fin_analyse/runtime_probe.py",
        "fin_analyse/gateway/mcp_server.py",
        "fin_analyse/gateway/tool_surface.py",
        "fin_analyse/guo_teacher_research/__init__.py",
        "fin_analyse/guo_teacher_research/agent_runtime.py",
        "fin_analyse/guo_teacher_research/capability_broker.py",
        "fin_analyse/guo_teacher_research/codex_runtime.py",
        "fin_analyse/guo_teacher_research/local_capability_transport.py",
        "fin_analyse/guo_teacher_research/production_runtime.py",
        "fin_analyse/guo_teacher_research/semantic_service.py",
        "fin_analyse/guo_teacher_research/semantic_state.py",
        "fin_analyse/guo_teacher_research/use_case_runner.py",
        "fin_analyse/guo_teacher_research/g_working_set.py",
        "fin_analyse/scraper/cdp_runtime.py",
        "fin_analyse/scraper/scheduled_run.py",
        "scripts/apply_fin_hermes_external_integration.py",
        "scripts/consultation_runtime_canary.py",
        "scripts/consultation_runtime_canary_launcher.py",
        "scripts/codex_route_runtime.sh",
        "fin_analyse/guo_teacher_research/codex_route_binding.py",
        "scripts/codex_proxy_a.sh",
        "scripts/codex_proxy_b.sh",
        "scripts/prepare_fin_release.py",
        "scripts/capture_zsxq_windows.cjs",
        "scripts/consume_zsxq_capture_folder.py",
        "scripts/zsxq_windows_incremental_scheduler.py",
        "hermes-migration/skills/fin-analyse/fin-analyse-consultation/SKILL.md",
        "hermes-migration/skills/fin-analyse/fin-analyse-ops/SKILL.md",
        "config/llm.yaml.example",
        "tools/runtime_probe.py",
        *extra_tracked_files,
    ):
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# release test asset\n", encoding="utf-8")
    worker_service = "scripts/fin-semantic-research-worker.service"
    if worker_service in extra_tracked_files:
        (staging / worker_service).write_text(
            "WorkingDirectory=@FIN_REPO_ROOT@\n",
            encoding="utf-8",
        )
    (staging / "scripts/prepare_fin_release.py").write_text(
        Path(release_tool.__file__).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (staging / "scripts/prepare_fin_release.py").chmod(0o644)
    for relative in (
        "hermes-migration/MANIFEST.txt",
        "hermes-migration/cron/jobs.json",
        "hermes-migration/profile-config/config.yaml",
        "hermes-migration/plugins/fin-consultation-first-tool/__init__.py",
        "hermes-migration/plugins/fin-consultation-first-tool/plugin.yaml",
        "hermes-migration/plugins/fin-consultation-first-tool/runtime_plugin.py",
        "hermes-migration/profile-memory/SOUL.md",
        "hermes-migration/profile-memory/MEMORY.md",
        "hermes-migration/profile-memory/USER.md",
        "hermes-migration/systemd/hermes-gateway-fin.service",
        "hermes-migration/systemd/hermes-gateway-fin.service.d/20-fin-python-safety.conf",
        "hermes-migration/skills/fin-analyse/fin-analyse-test/SKILL.md",
    ):
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# release test asset\n", encoding="utf-8")
    (staging / "pyproject.toml").write_text("[project]\nname = 'fin-test'\n", encoding="utf-8")
    (staging / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (staging / ".gitignore").write_text(
        ".venv/\n.env\nknowledge-base/runtime/\nknowledge-base/market-cache/\n__pycache__/\n",
        encoding="utf-8",
    )
    _git("init", cwd=staging)
    _git("config", "user.email", "fin-test@example.invalid", cwd=staging)
    _git("config", "user.name", "FIN Test", cwd=staging)
    _git("add", ".", cwd=staging)
    _git("commit", "-m", "seed release", cwd=staging)
    commit = _git("rev-parse", "HEAD", cwd=staging)
    _git("checkout", "--detach", commit, cwd=staging)

    layout = ReleaseLayout(home=home, commit=commit)
    layout.releases_root.mkdir(parents=True)
    layout.data_root.chmod(0o700)
    layout.releases_root.chmod(0o700)
    staging.rename(layout.release_root)
    layout.release_root.chmod(0o700)
    venv.EnvBuilder(with_pip=False, symlinks=True).create(layout.release_root / ".venv")
    assert (layout.release_root / ".venv/bin/python").exists()

    layout.env_file.parent.mkdir(parents=True, mode=0o700)
    layout.env_file.parent.chmod(0o700)
    layout.env_file.write_text("FIN_TEST=1\n", encoding="utf-8")
    layout.env_file.chmod(0o600)
    layout.runtime_root.mkdir(parents=True, mode=0o700)
    layout.market_cache_root.mkdir(parents=True, mode=0o700)
    layout.shared_root.chmod(0o700)
    layout.runtime_root.chmod(0o700)
    layout.market_cache_root.chmod(0o700)
    if record_sync:
        record_frozen_sync(layout)
    return layout


def test_plugin_pycache_path_allowed_is_bounded(tmp_path: Path) -> None:
    release_tool = importlib.import_module("scripts.prepare_fin_release")
    plugins = (
        tmp_path
        / "hermes-migration"
        / "plugins"
        / "fin-consultation-first-tool"
    )
    plugins.mkdir(parents=True)
    (plugins / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plugins / "__pycache__").mkdir()
    pyc = plugins / "__pycache__" / "__init__.cpython-311.pyc"
    pyc.write_bytes(b"x" * 64)

    relative = "hermes-migration/plugins/fin-consultation-first-tool/__pycache__/__init__.cpython-311.pyc"
    assert release_tool._plugin_pycache_path_allowed(tmp_path, relative)
    assert not release_tool._plugin_pycache_path_allowed(
        tmp_path,
        "hermes-migration/plugins/fin-consultation-first-tool/__pycache__/orphan.cpython-311.pyc",
    )
    assert not release_tool._plugin_pycache_path_allowed(
        tmp_path,
        "hermes-migration/plugins/fin-consultation-first-tool/__init__.py",
    )
    assert not release_tool._plugin_pycache_path_allowed(
        tmp_path,
        "fin_analyse/__pycache__/module.cpython-311.pyc",
    )
    pyc.write_bytes(b"y" * (64 * 1024 + 1))
    assert not release_tool._plugin_pycache_path_allowed(tmp_path, relative)


def test_git_identity_allows_bounded_plugin_pycache_and_rejects_orphans(
    tmp_path: Path,
) -> None:
    release_tool = importlib.import_module("scripts.prepare_fin_release")
    layout = _seed_release(
        tmp_path,
        extra_tracked_files=(
            "hermes-migration/plugins/fin-consultation-first-tool/runtime_plugin.py",
        ),
    )
    plugin = layout.release_root / "hermes-migration/plugins/fin-consultation-first-tool"
    pycache = plugin / "__pycache__"
    pycache.mkdir()
    (pycache / "runtime_plugin.cpython-311.pyc").write_bytes(b"z" * 128)
    (pycache / "orphan.cpython-311.pyc").write_bytes(b"z" * 128)

    status = release_tool._git_identity_status(layout)

    assert status["unexpected_ignored"] == (
        "hermes-migration/plugins/fin-consultation-first-tool/__pycache__/orphan.cpython-311.pyc",
    )
    assert status["known_plugin_pycache"] == (
        "hermes-migration/plugins/fin-consultation-first-tool/__pycache__/runtime_plugin.cpython-311.pyc",
    )

    (pycache / "orphan.cpython-311.pyc").unlink()
    status = release_tool._git_identity_status(layout)
    assert status["unexpected_ignored"] == ()
    assert len(status["known_plugin_pycache"]) == 1


def _immutable_source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if relative.parts[0] in {".git", ".venv"}:
            continue
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
            payload = b""
        elif stat.S_ISLNK(metadata.st_mode):
            kind = "symlink"
            payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
            payload = path.read_bytes()
        else:
            raise AssertionError(f"unsupported release entry: {relative.as_posix()}")
        digest.update(f"{relative.as_posix()}\0{kind}\0{metadata.st_mode:o}\0".encode())
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _seed_schema1_prior_release(
    tmp_path: Path,
    candidate: ReleaseLayout,
    *,
    checker_sentinel: Path | None = None,
    checker_source_override: str | None = None,
) -> ReleaseLayout:
    staging = tmp_path / "schema1-prior-staging"
    (staging / "scripts").mkdir(parents=True)
    (staging / "scripts/prepare_fin_release.py").write_text(
        """\
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def venv_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for current_path, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        parent = Path(current_path)
        for name in (*directories, *files):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                kind = "directory"
                payload = b""
            elif stat.S_ISLNK(metadata.st_mode):
                kind = "symlink"
                payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
            elif stat.S_ISREG(metadata.st_mode):
                kind = "file"
                payload = bytes.fromhex(sha256_file(path))
            else:
                raise ValueError(f"unsupported venv entry: {relative}")
            digest.update(f"{relative}\\0{kind}\\0{mode:o}\\0".encode())
            digest.update(payload)
            digest.update(b"\\0")
    return digest.hexdigest()


def python_identity(release_root: Path) -> dict[str, str]:
    interpreter = release_root / ".venv/bin/python"
    completed = subprocess.run(
        (
            str(interpreter),
            "-B",
            "-I",
            "-c",
            "import json,sys;print(json.dumps({'executable':sys.executable,"
            "'prefix':sys.prefix,'base_prefix':sys.base_prefix},sort_keys=True))",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode != 0:
        raise ValueError("prior interpreter failed")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise ValueError("prior interpreter identity is not an object")
    return payload


parser = argparse.ArgumentParser()
parser.add_argument("action", choices=("check",))
parser.add_argument("--home", type=Path, required=True)
parser.add_argument("--commit", required=True)
arguments = parser.parse_args()
release_root = (
    arguments.home / ".local/share/fin-analyse/releases" / arguments.commit
)
current = arguments.home / ".local/share/fin-analyse/current"
try:
    git_status = subprocess.run(
        (
            "git",
            "--no-optional-locks",
            "-C",
            str(release_root),
            "status",
            "--porcelain",
            "--untracked-files=no",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        },
    )
    ignored_status = subprocess.run(
        (
            "git",
            "--no-optional-locks",
            "-C",
            str(release_root),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        },
    )
    unexpected_ignored = [
        path
        for path in ignored_status.stdout.splitlines()
        if not (
            path == ".venv"
            or path.startswith(".venv/")
            or path
            in {
                ".env",
                "knowledge-base/runtime",
                "knowledge-base/market-cache",
            }
        )
    ]
    receipt = json.loads(
        (release_root / ".fin-frozen-sync.json").read_text(encoding="utf-8")
    )
    uv_digest = sha256_file(release_root / "uv.lock")
    frozen_venv_digest = venv_digest(release_root / ".venv")
    target = current.resolve(strict=True)
    ready = bool(
        set(receipt)
        == {
            "schema_version",
            "commit",
            "uv_lock_sha256",
            "venv_sha256",
            "python_identity",
        }
        and git_status.returncode == 0
        and not git_status.stdout
        and ignored_status.returncode == 0
        and not unexpected_ignored
        and receipt["schema_version"] == 1
        and receipt["commit"] == arguments.commit
        and receipt["uv_lock_sha256"] == uv_digest
        and receipt["venv_sha256"] == frozen_venv_digest
        and receipt["python_identity"] == python_identity(release_root)
        and target == release_root
        and (release_root / ".env").resolve(strict=True)
        == arguments.home / ".config/fin-analyse/fin.env"
        and (release_root / "knowledge-base/runtime").resolve(strict=True)
        == arguments.home / ".local/share/fin-analyse/shared/knowledge-base/runtime"
        and (release_root / "knowledge-base/market-cache").resolve(strict=True)
        == arguments.home / ".local/share/fin-analyse/shared/knowledge-base/market-cache"
    )
except (KeyError, OSError, ValueError, json.JSONDecodeError):
    target = None
    ready = False
target_release = {
    "ready": ready,
    "commit": arguments.commit,
    "release_root": str(release_root),
}
payload = {
    **target_release,
    "current": str(current),
    "current_pointer": {
        "exists": current.exists(),
        "is_symlink": current.is_symlink(),
        "target": str(target) if target is not None else None,
        "target_commit": arguments.commit if target == release_root else None,
        "valid_commit_bound_target": ready,
        "target_is_candidate": target == release_root,
        "target_release": target_release,
    },
}
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if ready else 1)
""",
        encoding="utf-8",
    )
    if checker_source_override is not None:
        (staging / "scripts/prepare_fin_release.py").write_text(
            checker_source_override,
            encoding="utf-8",
        )
    (staging / "scripts/prepare_fin_release.py").chmod(0o644)
    if checker_sentinel is not None:
        checker = staging / "scripts/prepare_fin_release.py"
        checker.write_text(
            checker.read_text(encoding="utf-8").replace(
                "parser = argparse.ArgumentParser()",
                f"Path({str(checker_sentinel)!r}).write_text('ran', encoding='utf-8')\n"
                "parser = argparse.ArgumentParser()",
            ),
            encoding="utf-8",
        )
    (staging / "scripts/fin-semantic-research-worker.service").write_text(
        "WorkingDirectory=@FIN_REPO_ROOT@\n",
        encoding="utf-8",
    )
    for relative in (
        "fin_analyse/runtime_probe.py",
        "hermes-migration/plugins/fin-consultation-first-tool/runtime_plugin.py",
        "tools/runtime_probe.py",
    ):
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# runtime bytecode source\n", encoding="utf-8")
    (staging / "pyproject.toml").write_text(
        "[project]\nname = 'fin-schema1-prior'\n",
        encoding="utf-8",
    )
    (staging / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (staging / ".gitignore").write_text(
        ".venv/\n.env\nknowledge-base/runtime\nknowledge-base/market-cache\n__pycache__/\n",
        encoding="utf-8",
    )
    _git("init", cwd=staging)
    _git("config", "user.email", "fin-test@example.invalid", cwd=staging)
    _git("config", "user.name", "FIN Test", cwd=staging)
    _git("add", ".", cwd=staging)
    _git("commit", "-m", "seed schema-v1 prior release", cwd=staging)
    commit = _git("rev-parse", "HEAD", cwd=staging)
    _git("checkout", "--detach", commit, cwd=staging)

    prior = ReleaseLayout(home=candidate.home, commit=commit)
    staging.rename(prior.release_root)
    prior.release_root.chmod(0o700)
    venv.EnvBuilder(with_pip=False, symlinks=True).create(prior.release_root / ".venv")

    receipt = {
        "schema_version": 1,
        "commit": prior.commit,
        "uv_lock_sha256": release_tool._sha256_file(prior.release_root / "uv.lock"),
        "venv_sha256": release_tool._venv_digest(prior.release_root / ".venv"),
        "python_identity": release_tool._python_identity(prior),
    }
    receipt_path = prior.release_root / ".fin-frozen-sync.json"
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)

    (prior.release_root / ".env").symlink_to(prior.env_file)
    knowledge_base = prior.release_root / "knowledge-base"
    knowledge_base.mkdir()
    (knowledge_base / "runtime").symlink_to(prior.runtime_root, target_is_directory=True)
    (knowledge_base / "market-cache").symlink_to(
        prior.market_cache_root,
        target_is_directory=True,
    )
    return prior


def _seed_runtime_bytecode_quarantine_pair(
    tmp_path: Path,
    *,
    checker_source_override: str | None = None,
) -> tuple[ReleaseLayout, ReleaseLayout]:
    if checker_source_override is None:
        with pytest.MonkeyPatch.context() as frozen_context:
            return _seed_frozen_checker_prior_pair(
                tmp_path,
                frozen_context,
                checker_source=_frozen_checker_source_at(_FROZEN_PRE_HANDOFF_CHECKER_COMMIT),
            )
    candidate = _seed_release(tmp_path)
    prepare_release_bindings(candidate)
    prior = _seed_schema1_prior_release(
        tmp_path,
        candidate,
        checker_source_override=checker_source_override,
    )
    candidate.current_link.symlink_to(prior.release_root.relative_to(candidate.current_link.parent))
    return candidate, prior


def _frozen_checker_source_with_target_mode(
    *,
    config_mode_line: str | None,
) -> str:
    source = _frozen_checker_source_at(_FROZEN_LEGACY_CHECKER_COMMIT)
    current = """\
_SPECIAL_HANDOFF_MODE_TARGETS = (
    ("scripts/consultation_runtime_canary_launcher.py", "file", 0o600),
    ("hermes-migration/plugins/fin-consultation-first-tool", "directory", 0o700),
)
"""
    replacement_lines = [
        "_SPECIAL_HANDOFF_MODE_TARGETS = (",
        '    ("scripts/consultation_runtime_canary_launcher.py", "file", 0o600),',
        '    ("hermes-migration/plugins/fin-consultation-first-tool", "directory", 0o700),',
    ]
    if config_mode_line is not None:
        replacement_lines.append(config_mode_line)
    replacement_lines.append(")")
    replacement = "\n".join(replacement_lines) + "\n"
    assert source.count(current) == 1
    return source.replace(current, replacement)


_FROZEN_CURRENT_CONFIG_MODE_LINE = '    ("config", "directory", 0o755),  # frozen-current-contract'


def _frozen_four_target_checker_source() -> str:
    source = _frozen_checker_source_with_target_mode(
        config_mode_line=_FROZEN_CURRENT_CONFIG_MODE_LINE,
    )
    marker = f"{_FROZEN_CURRENT_CONFIG_MODE_LINE}\n)"
    replacement = (
        f"{_FROZEN_CURRENT_CONFIG_MODE_LINE}\n"
        '    ("hermes-migration/cron/jobs.json", "file", 0o600),\n'
        ")"
    )
    assert source.count(marker) == 1
    return source.replace(marker, replacement)


def _run_versioned_release_action(
    layout: ReleaseLayout,
    action: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            str(layout.release_root / ".venv/bin/python"),
            "-I",
            "-B",
            str(layout.release_root / "scripts/prepare_fin_release.py"),
            action,
            "--home",
            str(layout.home),
            "--commit",
            layout.commit,
        ),
        cwd=layout.release_root,
        env={
            "HOME": str(layout.home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )


def _seed_frozen_checker_prior_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    checker_source: str,
) -> tuple[ReleaseLayout, ReleaseLayout]:
    candidate_seed = tmp_path / "candidate"
    candidate_seed.mkdir()
    candidate = _seed_release(candidate_seed)
    prepare_release_bindings(candidate)
    frozen_checker = tmp_path / "frozen-prior-prepare-fin-release.py"
    frozen_checker.write_text(
        checker_source,
        encoding="utf-8",
    )
    with monkeypatch.context() as frozen_context:
        frozen_context.setattr(release_tool, "__file__", str(frozen_checker))
        prior_seed = tmp_path / "prior-seed"
        prior_seed.mkdir()
        seeded_prior = _seed_release(
            prior_seed,
            record_sync=False,
            extra_tracked_files=tuple(
                relative
                for relative in _required_regular_files_from_checker_source(checker_source)
                if relative not in release_tool._REQUIRED_REGULAR_FILES
            ),
        )

    prior = ReleaseLayout(home=candidate.home, commit=seeded_prior.commit)
    seeded_prior.release_root.rename(prior.release_root)
    for relative, target in (
        (".env", prior.env_file),
        ("knowledge-base/runtime", prior.runtime_root),
        ("knowledge-base/market-cache", prior.market_cache_root),
    ):
        binding = prior.release_root / relative
        binding.parent.mkdir(parents=True, exist_ok=True)
        binding.unlink(missing_ok=True)
        binding.symlink_to(target, target_is_directory=relative != ".env")

    record = _run_versioned_release_action(prior, "record-sync")
    assert record.returncode == 0, record.stderr
    prepared = _run_versioned_release_action(prior, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    candidate.current_link.symlink_to(prior.release_root.relative_to(candidate.current_link.parent))
    return candidate, prior


def _seed_frozen_v2_handoff_prior_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_mode_line: str | None = None,
) -> tuple[ReleaseLayout, ReleaseLayout]:
    return _seed_frozen_checker_prior_pair(
        tmp_path,
        monkeypatch,
        checker_source=_frozen_checker_source_with_target_mode(
            config_mode_line=config_mode_line,
        ),
    )


def _write_runtime_bytecode(
    prior: ReleaseLayout,
    source_relative: str = "scripts/prepare_fin_release.py",
    *,
    payload: bytes = b"test-runtime-bytecode\n",
) -> Path:
    source = prior.release_root / source_relative
    bytecode = Path(importlib.util.cache_from_source(str(source)))
    bytecode.parent.mkdir(parents=True, exist_ok=True)
    bytecode.parent.chmod(0o700)
    bytecode.write_bytes(payload)
    bytecode.chmod(0o644)
    return bytecode


def _run_runtime_bytecode_quarantine(
    candidate: ReleaseLayout,
    prior: ReleaseLayout,
    *extra_arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            str(candidate.release_root / ".venv/bin/python"),
            "-I",
            "-B",
            str(candidate.release_root / "scripts/prepare_fin_release.py"),
            "quarantine-runtime-bytecode",
            "--home",
            str(candidate.home),
            "--commit",
            candidate.commit,
            "--expected-current-commit",
            prior.commit,
            *extra_arguments,
        ),
        cwd=candidate.release_root,
        env={
            "HOME": str(candidate.home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )


def _run_runtime_bytecode_quarantine_preflight(
    candidate: ReleaseLayout,
    prior: ReleaseLayout,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            str(candidate.release_root / ".venv/bin/python"),
            "-I",
            "-B",
            str(candidate.release_root / "scripts/prepare_fin_release.py"),
            "preflight-runtime-bytecode-quarantine",
            "--home",
            str(candidate.home),
            "--commit",
            candidate.commit,
            "--expected-current-commit",
            prior.commit,
        ),
        cwd=candidate.release_root,
        env={
            "HOME": str(candidate.home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )


def _run_degraded_current_cutover(
    candidate: ReleaseLayout,
    prior: ReleaseLayout,
    action: str,
    *,
    cutover_sha256: str | None = None,
    prior_current_pointer_sha256: str | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        str(candidate.release_root / ".venv/bin/python"),
        "-I",
        "-B",
        str(candidate.release_root / "scripts/prepare_fin_release.py"),
        action,
        "--home",
        str(candidate.home),
        "--commit",
        candidate.commit,
        "--degraded-prior-commit",
        prior.commit,
    ]
    if cutover_sha256 is not None:
        arguments.extend(("--expected-cutover-sha256", cutover_sha256))
    if prior_current_pointer_sha256 is not None:
        arguments.extend(
            (
                "--expected-prior-current-pointer-sha256",
                prior_current_pointer_sha256,
            )
        )
    return subprocess.run(
        arguments,
        cwd=candidate.release_root,
        env={
            "HOME": str(candidate.home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )


def _run_prior_release_check(
    candidate: ReleaseLayout,
    prior: ReleaseLayout,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            str(candidate.release_root / ".venv/bin/python"),
            "-I",
            "-B",
            str(prior.release_root / "scripts/prepare_fin_release.py"),
            "check",
            "--home",
            str(candidate.home),
            "--commit",
            prior.commit,
        ),
        cwd=prior.release_root,
        env={
            "HOME": str(candidate.home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )


def _file_snapshot(path: Path) -> tuple[int, int, int, int, int, int, str]:
    metadata = path.lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        release_tool._sha256_file(path),
    )


def test_prepare_and_atomically_activate_commit_bound_release(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)

    prepared = prepare_release_bindings(layout)
    activated = activate_release(layout)

    assert prepared["ready"] is True
    assert prepared["changed"] is True
    assert activated == {
        "active": True,
        "changed": True,
        "commit": layout.commit,
        "current": str(layout.current_link),
        "release_root": str(layout.release_root),
    }
    assert layout.current_link.is_symlink()
    assert layout.current_link.resolve(strict=True) == layout.release_root
    assert (layout.release_root / ".env").resolve(strict=True) == layout.env_file
    assert (layout.release_root / "knowledge-base/runtime").resolve(strict=True) == (
        layout.runtime_root
    )
    assert (layout.release_root / "knowledge-base/market-cache").resolve(strict=True) == (
        layout.market_cache_root
    )
    assert inspect_release(layout)["ready"] is True
    pointer = inspect_release(layout)["current_pointer"]
    assert pointer["exists"] is True
    assert pointer["is_symlink"] is True
    assert pointer["target"] == str(layout.release_root)
    assert pointer["target_commit"] == layout.commit
    assert pointer["valid_commit_bound_target"] is True
    assert pointer["target_is_candidate"] is True
    assert pointer["target_release"]["ready"] is True
    assert os.stat(layout.current_link.parent).st_uid == os.geteuid()


def test_activate_accepts_expected_fully_ready_pre_handoff_prior(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    prior_pointer = inspect_release(candidate)["current_pointer"]

    activated = activate_release(
        candidate,
        expected_current_commit=prior.commit,
    )

    assert prior_pointer["valid_commit_bound_target"] is True
    assert prior_pointer["target_release"]["contract_owner"] == "target_release"
    assert activated["changed"] is True
    assert candidate.current_link.resolve(strict=True) == candidate.release_root


def test_activate_switches_current_while_external_plugin_link_exists(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    live_link = candidate.home / ".hermes/profiles/fin/plugins/fin-consultation-first-tool"
    live_link.parent.mkdir(parents=True)
    live_link.symlink_to(
        candidate.release_root / "hermes-migration/plugins/fin-consultation-first-tool"
    )

    activated = activate_release(candidate, expected_current_commit=prior.commit)

    assert activated["changed"] is True
    assert candidate.current_link.resolve(strict=True) == candidate.release_root
    assert live_link.is_symlink()


def test_versioned_prior_checker_cannot_execute_repository_fsmonitor(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    sentinel = tmp_path / "prior-fsmonitor-ran"
    hook = tmp_path / "prior-fsmonitor"
    hook.write_text(
        f"#!/bin/sh\n/usr/bin/touch {sentinel}\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    _git("config", "core.fsmonitor", str(hook), cwd=prior.release_root)
    subprocess.run(
        ("/usr/bin/git", "-C", str(prior.release_root), "status", "--porcelain"),
        check=False,
        capture_output=True,
        text=True,
    )
    assert sentinel.exists()
    sentinel.unlink()

    payload, _snapshot = release_tool._validate_versioned_previous_release(
        candidate,
        prior,
    )

    assert payload["ready"] is True
    assert not sentinel.exists()


def test_versioned_check_never_executes_same_inode_rewrite_after_allowlist_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_frozen_v2_handoff_prior_pair(
        tmp_path,
        monkeypatch,
    )
    checker = prior.release_root / release_tool._VERSIONED_CHECKER
    original_source = checker.read_bytes()
    original_metadata = checker.stat()
    original_mode = stat.S_IMODE(original_metadata.st_mode)
    sentinel = tmp_path / "mutable-checker-path-ran"
    malicious_source = (
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('ran', encoding='utf-8')\n"
        "print('{}')\n"
    ).encode()
    real_popen = release_tool.subprocess.Popen
    rewrite_count = 0

    def restore_original_source() -> None:
        checker.write_bytes(original_source)
        checker.chmod(original_mode)
        os.utime(
            checker,
            ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
        )

    class RestoreAfterWait:
        def __init__(self, process: subprocess.Popen[bytes]) -> None:
            self._process = process

        @property
        def pid(self) -> int:
            return self._process.pid

        def wait(self, timeout: float | None = None) -> int:
            try:
                return self._process.wait(timeout=timeout)
            finally:
                restore_original_source()

    def rewrite_path_before_spawn(args, *popen_args, **kwargs):
        nonlocal rewrite_count
        if isinstance(args, (list, tuple)) and release_tool._VERSIONED_CHECK_BOOTSTRAP in args:
            rewrite_count += 1
            checker.write_bytes(malicious_source)
            checker.chmod(original_mode)
            os.utime(
                checker,
                ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
            )
            assert checker.stat().st_ino == original_metadata.st_ino
            return RestoreAfterWait(real_popen(args, *popen_args, **kwargs))
        return real_popen(args, *popen_args, **kwargs)

    monkeypatch.setattr(
        release_tool.subprocess,
        "Popen",
        rewrite_path_before_spawn,
    )

    with (
        release_tool._quarantine_release_descriptors(
            candidate,
            prior,
        ) as descriptors,
        pytest.raises(
            RuntimeError,
            match="prior release versioned check failed",
        ),
    ):
        release_tool._run_versioned_previous_check(
            candidate,
            prior,
            previous_fd=descriptors.previous_fd,
            expected_checker_sha256=hashlib.sha256(original_source).hexdigest(),
        )

    assert rewrite_count == 1
    assert not sentinel.exists()
    assert checker.read_bytes() == original_source
    assert checker.stat().st_ino == original_metadata.st_ino
    assert stat.S_IMODE(checker.stat().st_mode) == original_mode
    assert checker.stat().st_mtime_ns == original_metadata.st_mtime_ns


def test_versioned_prior_bootstrap_forces_hook_path_and_preserves_non_git_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker_source = """\
import json
import os
import subprocess
import sys
from subprocess import PIPE, Popen

environment = {
    key: value
    for key, value in os.environ.items()
    if not key.startswith("GIT_")
}
git_process = Popen(
    ("git", "config", "--get", "core.hooksPath"),
    env=environment,
    stdout=PIPE,
    stderr=PIPE,
    text=True,
)
hook_path, git_error = git_process.communicate()
from_import_process = Popen(
    (sys.executable, "-I", "-c", "print('runtime-probe-ok')"),
    stdout=PIPE,
    stderr=PIPE,
    text=True,
)
from_import_output, from_import_error = from_import_process.communicate()
direct_process = subprocess.Popen(
    (sys.executable, "-I", "-c", "print('direct-runtime-probe-ok')"),
    stdout=PIPE,
    stderr=PIPE,
    text=True,
)
direct_output, direct_error = direct_process.communicate()
print(json.dumps({
    "direct_error": direct_error,
    "direct_output": direct_output.strip(),
    "direct_returncode": direct_process.returncode,
    "from_import_error": from_import_error,
    "from_import_output": from_import_output.strip(),
    "from_import_returncode": from_import_process.returncode,
    "git_error": git_error,
    "git_returncode": git_process.returncode,
    "hook_path": hook_path.strip(),
}, sort_keys=True))
"""
    checker_sha256 = _authorize_checker_source_for_test(
        monkeypatch,
        checker_source,
    )
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(
        tmp_path,
        checker_source_override=checker_source,
    )

    payload = release_tool._run_versioned_previous_check(
        candidate,
        prior,
        expected_checker_sha256=checker_sha256,
    )

    assert payload == {
        "direct_error": "",
        "direct_output": "direct-runtime-probe-ok",
        "direct_returncode": 0,
        "from_import_error": "",
        "from_import_output": "runtime-probe-ok",
        "from_import_returncode": 0,
        "git_error": "",
        "git_returncode": 0,
        "hook_path": "/dev/null",
    }


@pytest.mark.parametrize(
    "attack",
    (
        "positional_executable",
        "positional_shell",
        "keyword_executable",
        "keyword_shell",
        "string_check_output",
    ),
)
def test_versioned_prior_bootstrap_rejects_hostile_process_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    sentinel = tmp_path / f"hostile-{attack}-ran"
    hostile_git = tmp_path / f"hostile-{attack}-git"
    hostile_git.write_text(
        f"#!/bin/sh\n/usr/bin/touch {sentinel}\n",
        encoding="utf-8",
    )
    hostile_git.chmod(0o700)
    if attack == "positional_executable":
        checker_source = (
            f"from subprocess import Popen\nPopen(('innocent',), -1, {str(hostile_git)!r}).wait()\n"
        )
    elif attack == "positional_shell":
        checker_source = (
            "from subprocess import Popen\n"
            f"Popen(({str(hostile_git)!r},), -1, None, None, None, None, None, True, True).wait()"
            "\n"
        )
    elif attack == "keyword_executable":
        checker_source = (
            "import subprocess\n"
            f"subprocess.Popen(('innocent',), executable={str(hostile_git)!r}).wait()\n"
        )
    elif attack == "keyword_shell":
        checker_source = (
            f"import subprocess\nsubprocess.Popen(({str(hostile_git)!r},), shell=True).wait()\n"
        )
    else:
        checker_source = f"import subprocess\nsubprocess.check_output({str(hostile_git)!r})\n"
    authorized_source = f"{checker_source}print('{{}}')\n"
    checker_sha256 = _authorize_checker_source_for_test(
        monkeypatch,
        authorized_source,
    )
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(
        tmp_path,
        checker_source_override=authorized_source,
    )

    failure: RuntimeError | None = None
    try:
        release_tool._run_versioned_previous_check(
            candidate,
            prior,
            expected_checker_sha256=checker_sha256,
        )
    except RuntimeError as error:
        failure = error

    assert not sentinel.exists()
    assert failure is not None
    assert str(failure) == "prior release versioned check failed"


def test_previous_readiness_freezes_contract_and_rejects_receipt_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    monkeypatch.setattr(
        release_tool,
        "record_frozen_sync",
        lambda _layout: (_ for _ in ()).throw(AssertionError("rollback must not record sync")),
    )

    snapshot = capture_previous_release_readiness(candidate, prior)

    assert snapshot["ready"] is True
    assert snapshot["immutable_snapshot"]["prior_receipt_schema_version"] == 2
    assert verify_recorded_previous_release_readiness(snapshot, candidate, prior)["ready"] is True

    receipt = prior.release_root / ".fin-frozen-sync.json"
    replacement = receipt.with_name(".receipt.replacement")
    replacement.write_bytes(receipt.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, receipt)

    with pytest.raises(RuntimeError, match="readiness drifted"):
        verify_recorded_previous_release_readiness(snapshot, candidate, prior)


def test_previous_readiness_accepts_active_legacy_checker_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker_source = _frozen_checker_source_at(_FROZEN_ACTIVE_LEGACY_CHECKER_COMMIT)
    candidate, prior = _seed_frozen_checker_prior_pair(
        tmp_path,
        monkeypatch,
        checker_source=checker_source,
    )
    legacy_critical_file = "scripts/fresh_g_market_postfix_canary.py"
    assert legacy_critical_file not in release_tool._CRITICAL_RUNTIME_FILES
    assert legacy_critical_file in _critical_runtime_files_from_checker_source(checker_source)
    assert (prior.release_root / legacy_critical_file).is_file()
    current_before = os.readlink(candidate.current_link)

    snapshot = capture_previous_release_readiness(candidate, prior)

    assert snapshot["ready"] is True
    assert snapshot["immutable_snapshot"]["prior_receipt_schema_version"] == 2
    assert os.readlink(candidate.current_link) == current_before


def test_previous_readiness_accepts_active_current_checker_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker_source = _frozen_checker_source_at(_FROZEN_ACTIVE_CURRENT_CHECKER_COMMIT)
    candidate, prior = _seed_frozen_checker_prior_pair(
        tmp_path,
        monkeypatch,
        checker_source=checker_source,
    )
    current_before = os.readlink(candidate.current_link)

    try:
        snapshot = capture_previous_release_readiness(candidate, prior)
    finally:
        assert os.readlink(candidate.current_link) == current_before

    receipt = release_tool._read_sync_receipt(prior)
    assert receipt is not None
    assert snapshot["ready"] is True
    assert snapshot["immutable_snapshot"]["prior_receipt_schema_version"] == 2
    assert receipt["handoff_binding_sha256"] == release_tool._special_handoff_binding_sha256(
        prior,
        targets=release_tool._special_handoff_targets_for_contract(
            release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET
        ),
    )
    assert (
        release_tool._special_handoff_contract_from_checker_source(checker_source.encode())
        is release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET
    )


def test_activate_accepts_exact_ready_prior_without_embedded_checker_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker_source = _frozen_checker_source_with_target_mode(
        config_mode_line=_FROZEN_CURRENT_CONFIG_MODE_LINE,
    )
    checker_sha256 = hashlib.sha256(checker_source.encode()).hexdigest()
    assert checker_sha256 not in dict(release_tool._FROZEN_CHECKER_CONTRACTS_BY_SHA256)
    candidate, prior = _seed_frozen_checker_prior_pair(
        tmp_path,
        monkeypatch,
        checker_source=checker_source,
    )
    prior_check = _run_versioned_release_action(prior, "check")
    assert prior_check.returncode == 0, prior_check.stderr

    activated = activate_release(candidate, expected_current_commit=prior.commit)

    assert activated["changed"] is True
    assert candidate.current_link.resolve(strict=True) == candidate.release_root


def test_activate_checks_expected_current_before_running_prior_contract(
    tmp_path: Path,
) -> None:
    candidate = _seed_release(tmp_path)
    prepare_release_bindings(candidate)
    sentinel = tmp_path / "prior-checker-ran"
    prior = _seed_schema1_prior_release(
        tmp_path,
        candidate,
        checker_sentinel=sentinel,
    )
    candidate.current_link.symlink_to(prior.release_root.relative_to(candidate.current_link.parent))
    pointer_before = os.readlink(candidate.current_link)

    with pytest.raises(ValueError, match="expected current commit is required"):
        activate_release(candidate)
    with pytest.raises(RuntimeError, match="does not match the expected commit"):
        activate_release(candidate, expected_current_commit="f" * 40)

    assert not sentinel.exists()
    assert os.readlink(candidate.current_link) == pointer_before


def test_activate_rejects_prior_venv_drift_without_executing_it(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    pointer_before = os.readlink(candidate.current_link)
    sentinel = tmp_path / "drifted-prior-python-ran"
    interpreter = prior.release_root / ".venv/bin/python"
    interpreter.unlink()
    interpreter.write_text(
        f"#!/bin/sh\nprintf ran > {sentinel}\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)

    with pytest.raises(RuntimeError, match="not a fully ready release"):
        activate_release(candidate, expected_current_commit=prior.commit)

    assert not sentinel.exists()
    assert os.readlink(candidate.current_link) == pointer_before


def test_activate_rejects_tracked_prior_checker_drift(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    pointer_before = os.readlink(candidate.current_link)
    with (prior.release_root / "scripts/prepare_fin_release.py").open(
        "a",
        encoding="utf-8",
    ) as stream:
        stream.write("\n# drift\n")

    with pytest.raises(RuntimeError, match="not a fully ready release"):
        activate_release(candidate, expected_current_commit=prior.commit)

    assert os.readlink(candidate.current_link) == pointer_before


@pytest.mark.parametrize("index_flag", ("--skip-worktree", "--assume-unchanged"))
def test_activate_never_executes_hidden_prior_checker_drift(
    tmp_path: Path,
    index_flag: str,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    pointer_before = os.readlink(candidate.current_link)
    checker = prior.release_root / release_tool._VERSIONED_CHECKER
    sentinel = tmp_path / "hidden-prior-checker-ran"
    _git("update-index", index_flag, "--", release_tool._VERSIONED_CHECKER, cwd=prior.release_root)
    source = checker.read_text(encoding="utf-8")
    future = "from __future__ import annotations\n"
    assert source.count(future) == 1
    checker.write_text(
        source.replace(
            future,
            future
            + "from pathlib import Path\n"
            + f"Path({str(sentinel)!r}).write_text('ran', encoding='utf-8')\n",
        ),
        encoding="utf-8",
    )
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=prior.release_root) == ""

    with pytest.raises(RuntimeError, match="not a fully ready release"):
        activate_release(candidate, expected_current_commit=prior.commit)

    assert not sentinel.exists()
    assert os.readlink(candidate.current_link) == pointer_before


def test_activate_never_executes_replace_object_checker(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    pointer_before = os.readlink(candidate.current_link)
    checker = prior.release_root / release_tool._VERSIONED_CHECKER
    sentinel = tmp_path / "replace-object-prior-checker-ran"
    source = checker.read_text(encoding="utf-8")
    future = "from __future__ import annotations\n"
    checker.write_text(
        source.replace(
            future,
            future
            + "from pathlib import Path\n"
            + f"Path({str(sentinel)!r}).write_text('ran', encoding='utf-8')\n",
        ),
        encoding="utf-8",
    )
    _git("add", release_tool._VERSIONED_CHECKER, cwd=prior.release_root)
    _git("commit", "-m", "malicious replacement", cwd=prior.release_root)
    replacement_commit = _git("rev-parse", "HEAD", cwd=prior.release_root)
    _git("replace", prior.commit, replacement_commit, cwd=prior.release_root)
    _git("update-ref", "HEAD", prior.commit, cwd=prior.release_root)
    assert _git("rev-parse", "HEAD", cwd=prior.release_root) == prior.commit
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=prior.release_root) == ""
    assert _git(
        "ls-files", "-v", "--", release_tool._VERSIONED_CHECKER, cwd=prior.release_root
    ).startswith("H ")

    with pytest.raises(RuntimeError, match="not a fully ready release"):
        activate_release(candidate, expected_current_commit=prior.commit)

    assert not sentinel.exists()
    assert os.readlink(candidate.current_link) == pointer_before


def test_activate_rejects_malformed_prior_check_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _seed_release(tmp_path)
    prepare_release_bindings(candidate)
    checker_source = "print('not-json')\n"
    _authorize_checker_source_for_test(monkeypatch, checker_source)
    prior = _seed_schema1_prior_release(
        tmp_path,
        candidate,
        checker_source_override=checker_source,
    )
    candidate.current_link.symlink_to(prior.release_root.relative_to(candidate.current_link.parent))
    pointer_before = os.readlink(candidate.current_link)

    with pytest.raises(RuntimeError, match="not a fully ready release"):
        activate_release(candidate, expected_current_commit=prior.commit)

    assert os.readlink(candidate.current_link) == pointer_before


def test_activate_times_out_prior_check_without_changing_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _seed_release(tmp_path)
    prepare_release_bindings(candidate)
    checker_source = "import time\ntime.sleep(60)\n"
    _authorize_checker_source_for_test(monkeypatch, checker_source)
    prior = _seed_schema1_prior_release(
        tmp_path,
        candidate,
        checker_source_override=checker_source,
    )
    candidate.current_link.symlink_to(prior.release_root.relative_to(candidate.current_link.parent))
    pointer_before = os.readlink(candidate.current_link)
    monkeypatch.setattr(release_tool, "_VERSIONED_CHECK_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(RuntimeError, match="not a fully ready release"):
        activate_release(candidate, expected_current_commit=prior.commit)

    assert os.readlink(candidate.current_link) == pointer_before


def test_activate_enforces_prior_check_output_limit_before_checker_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _seed_release(tmp_path)
    prepare_release_bindings(candidate)
    sentinel = tmp_path / "oversized-prior-check-continued"
    checker_source = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.stdout.write('x' * 1000000)\n"
        "sys.stdout.flush()\n"
        f"Path({str(sentinel)!r}).write_text('continued', encoding='utf-8')\n"
    )
    _authorize_checker_source_for_test(monkeypatch, checker_source)
    prior = _seed_schema1_prior_release(
        tmp_path,
        candidate,
        checker_source_override=checker_source,
    )
    candidate.current_link.symlink_to(prior.release_root.relative_to(candidate.current_link.parent))
    pointer_before = os.readlink(candidate.current_link)
    monkeypatch.setattr(release_tool, "_VERSIONED_CHECK_MAX_OUTPUT_BYTES", 4096)

    with pytest.raises(RuntimeError, match="not a fully ready release"):
        activate_release(candidate, expected_current_commit=prior.commit)

    assert not sentinel.exists()
    assert os.readlink(candidate.current_link) == pointer_before


def test_activate_rechecks_prior_snapshot_at_pointer_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    third_release = candidate.releases_root / ("e" * 40)
    third_release.mkdir(mode=0o700)
    original_snapshot = release_tool._versioned_check_snapshot
    snapshot_calls = 0

    def retarget_before_commit(
        candidate_layout: ReleaseLayout,
        previous_layout: ReleaseLayout,
    ) -> dict[str, object]:
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 3:
            candidate.current_link.unlink()
            candidate.current_link.symlink_to(
                third_release.relative_to(candidate.current_link.parent)
            )
        return original_snapshot(candidate_layout, previous_layout)

    monkeypatch.setattr(
        release_tool,
        "_versioned_check_snapshot",
        retarget_before_commit,
    )

    with pytest.raises(RuntimeError, match="changed during activation"):
        activate_release(candidate, expected_current_commit=prior.commit)

    assert snapshot_calls == 3
    assert candidate.current_link.resolve(strict=True) == third_release
    assert not os.path.lexists(candidate.current_link.parent / f".current.tmp-{os.getpid()}")


def test_candidate_cannot_downgrade_itself_to_schema1_readiness(tmp_path: Path) -> None:
    candidate = _seed_release(tmp_path)
    receipt_path = candidate.release_root / ".fin-frozen-sync.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["schema_version"] = 1
    receipt.pop("runtime_imports")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    assert inspect_release(candidate)["ready"] is False


def test_activation_retry_is_idempotent_with_stale_expected_prior(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)

    first = activate_release(candidate, expected_current_commit=prior.commit)
    second = activate_release(candidate, expected_current_commit=prior.commit)

    assert first["changed"] is True
    assert second["changed"] is False


def test_locked_ready_release_holds_shared_lock_and_revalidates(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    prepare_release_bindings(layout)
    lock_path = layout.data_root / ".release.lock"

    with locked_ready_release(layout) as status:
        assert status["ready"] is True
        contender = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)

    contender = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(contender)


def test_read_only_release_lock_requires_an_existing_lock_without_creating_it(
    tmp_path: Path,
) -> None:
    layout = ReleaseLayout(home=tmp_path, commit="a" * 40)
    layout.data_root.mkdir(parents=True)
    layout.data_root.chmod(0o700)
    lock_path = layout.data_root / ".release.lock"

    with (
        pytest.raises(PermissionError, match="existing FIN release lock"),
        locked_release_read_only(layout),
    ):
        raise AssertionError("missing read-only lock must not enter")
    assert not os.path.lexists(lock_path)

    os.mkfifo(lock_path, mode=0o600)
    with (
        pytest.raises(PermissionError, match="owner-only 0600 file"),
        locked_release_read_only(layout),
    ):
        raise AssertionError("special-file read-only lock must not enter")
    lock_path.unlink()

    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)
    before = lock_path.stat()
    with locked_release_read_only(layout):
        contender = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)
    after = lock_path.stat()

    assert (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "wrong_mode"])
def test_read_only_release_lock_rejects_unsafe_installed_identity(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    layout = ReleaseLayout(home=tmp_path, commit="a" * 40)
    layout.data_root.mkdir(parents=True)
    layout.data_root.chmod(0o700)
    lock_path = layout.data_root / ".release.lock"
    target = layout.data_root / "lock-target"
    target.write_bytes(b"")
    target.chmod(0o600)
    if unsafe_kind == "symlink":
        lock_path.symlink_to(target)
    elif unsafe_kind == "hardlink":
        lock_path.hardlink_to(target)
    else:
        lock_path.write_bytes(b"")
        lock_path.chmod(0o640)

    with pytest.raises(PermissionError), locked_release_read_only(layout):
        raise AssertionError("unsafe read-only release lock must not enter")


def test_read_only_release_lock_rechecks_installed_identity_after_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = ReleaseLayout(home=tmp_path, commit="a" * 40)
    layout.data_root.mkdir(parents=True)
    layout.data_root.chmod(0o700)
    lock_path = layout.data_root / ".release.lock"
    replacement = layout.data_root / ".replacement.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)
    replacement.write_bytes(b"")
    replacement.chmod(0o600)
    real_flock = fcntl.flock
    replaced = False

    def replace_before_lock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        if operation == fcntl.LOCK_SH and not replaced:
            replaced = True
            os.replace(replacement, lock_path)
        real_flock(descriptor, operation)

    monkeypatch.setattr(release_tool.fcntl, "flock", replace_before_lock)
    with (
        pytest.raises(PermissionError, match="changed while acquiring shared lock"),
        locked_release_read_only(layout),
    ):
        raise AssertionError("replaced read-only release lock must not enter")
    assert replaced is True


def test_locked_ready_release_rejects_entry_or_exit_drift(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    with pytest.raises(RuntimeError, match="not fully ready"), locked_ready_release(layout):
        raise AssertionError("unready release body must not execute")

    prepare_release_bindings(layout)
    with (
        pytest.raises(RuntimeError, match="changed while locked"),
        locked_ready_release(layout),
    ):
        (layout.release_root / "rogue.py").write_text("drift\n", encoding="utf-8")


def test_inspect_release_proves_prior_current_without_mutating_it(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    prior_commit = "b" * 40
    prior_release = layout.releases_root / prior_commit
    prior_release.mkdir()
    layout.current_link.symlink_to(prior_release.relative_to(layout.current_link.parent))

    before = os.readlink(layout.current_link)
    status = inspect_release(layout)

    pointer = status["current_pointer"]
    assert pointer["exists"] is True
    assert pointer["is_symlink"] is True
    assert pointer["target"] == str(prior_release)
    assert pointer["target_commit"] == prior_commit
    assert pointer["valid_commit_bound_target"] is False
    assert pointer["target_is_candidate"] is False
    assert pointer["target_release"]["ready"] is False
    assert os.readlink(layout.current_link) == before


def test_inspect_release_accepts_only_a_fully_prepared_prior_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, prior_layout = _seed_frozen_v2_handoff_prior_pair(
        tmp_path,
        monkeypatch,
    )

    pointer = inspect_release(layout)["current_pointer"]

    assert pointer["target_commit"] == prior_layout.commit
    assert pointer["valid_commit_bound_target"] is True
    assert pointer["target_is_candidate"] is False
    assert pointer["target_release"]["ready"] is True


def test_prepare_is_idempotent_and_activation_is_idempotent(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)

    first_prepare = prepare_release_bindings(layout)
    second_prepare = prepare_release_bindings(layout)
    first_activation = activate_release(layout)
    second_activation = activate_release(layout)

    assert first_prepare["changed"] is True
    assert second_prepare["changed"] is False
    assert first_activation["changed"] is True
    assert second_activation["changed"] is False


def test_activate_canonicalizes_absolute_same_release_pointer(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    prepare_release_bindings(layout)
    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    layout.current_link.symlink_to(layout.release_root)

    activated = activate_release(layout)

    assert activated["changed"] is True
    assert os.readlink(layout.current_link) == str(
        layout.release_root.relative_to(layout.current_link.parent)
    )
    assert layout.current_link.resolve(strict=True) == layout.release_root
    assert activate_release(layout)["changed"] is False


def test_activate_canonicalizes_same_release_pointer_in_secure_0750_data_root(
    tmp_path: Path,
) -> None:
    layout = _seed_release(tmp_path)
    prepare_release_bindings(layout)
    layout.data_root.chmod(0o750)
    layout.current_link.symlink_to(layout.release_root)

    activated = activate_release(layout)

    assert activated["changed"] is True
    assert os.readlink(layout.current_link) == str(
        layout.release_root.relative_to(layout.current_link.parent)
    )


def test_same_release_pointer_canonicalization_closes_data_fd_on_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path)
    prepare_release_bindings(layout)
    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    layout.current_link.symlink_to(layout.release_root)
    original_open_directory_fd = release_tool._open_directory_fd
    original_unlink = release_tool.os.unlink
    data_fd: int | None = None

    def capture_data_fd(*args, **kwargs) -> int:
        nonlocal data_fd
        descriptor = original_open_directory_fd(*args, **kwargs)
        data_fd = descriptor
        return descriptor

    def fail_temporary_cleanup(path, *args, **kwargs) -> None:
        if str(path).startswith(".current.tmp-"):
            raise OSError("injected temporary cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(release_tool, "_open_directory_fd", capture_data_fd)
    monkeypatch.setattr(release_tool.os, "unlink", fail_temporary_cleanup)

    with pytest.raises(OSError, match="injected temporary cleanup failure"):
        activate_release(layout)

    assert data_fd is not None
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(data_fd)


def test_same_release_pointer_canonicalization_preserves_concurrent_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path)
    prepare_release_bindings(layout)
    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    layout.current_link.symlink_to(layout.release_root)
    intruder = layout.releases_root / ("b" * 40)
    intruder.mkdir(mode=0o700)
    original_exchange = release_tool._rename_exchange
    swapped = False

    def exchange_after_concurrent_selection(
        first_parent_fd: int,
        first_name: str,
        second_parent_fd: int,
        second_name: str,
    ) -> None:
        nonlocal swapped
        if not swapped and second_name == "current":
            replacement = layout.current_link.parent / ".current.concurrent"
            replacement.symlink_to(intruder.relative_to(layout.current_link.parent))
            os.replace(replacement, layout.current_link)
            swapped = True
        original_exchange(
            first_parent_fd,
            first_name,
            second_parent_fd,
            second_name,
        )

    monkeypatch.setattr(release_tool, "_rename_exchange", exchange_after_concurrent_selection)

    with pytest.raises(RuntimeError, match="current release pointer changed"):
        activate_release(layout)

    assert swapped is True
    assert layout.current_link.resolve(strict=True) == intruder
    assert not list(layout.current_link.parent.glob(".current.tmp-*"))


def test_prepare_rejects_attached_release_branch(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    _git("switch", "-c", "mutable-release", cwd=layout.release_root)

    with pytest.raises(ValueError, match="detached HEAD"):
        prepare_release_bindings(layout)


@pytest.mark.parametrize("returncode", (125, 128))
def test_prepare_does_not_treat_symbolic_ref_failure_as_detached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    layout = _seed_release(tmp_path)
    original_run_git = release_tool._run_git

    def run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        if arguments == ("symbolic-ref", "-q", "HEAD"):
            return subprocess.CompletedProcess(arguments, returncode, "", "")
        return original_run_git(root, *arguments)

    monkeypatch.setattr(release_tool, "_run_git", run_git)

    assert release_tool._git_identity_status(layout)["detached"] is False
    with pytest.raises(ValueError, match="detached HEAD"):
        prepare_release_bindings(layout)


def test_prepare_rejects_tracked_release_modifications(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    (layout.release_root / "pyproject.toml").write_text(
        "[project]\nname = 'modified-release'\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="tracked modifications"):
        prepare_release_bindings(layout)


def test_prepare_rejects_untracked_release_code(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    (layout.release_root / "rogue.py").write_text("raise SystemExit\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected untracked"):
        prepare_release_bindings(layout)


@pytest.mark.parametrize(
    "relative",
    (
        "fin_analyse/consultation/presentation.py",
        "fin_analyse/claims/config_loader.py",
        "fin_analyse/gateway/tool_presentation.py",
        "fin_analyse/market/evidence_plan.py",
        "fin_analyse/runtime/hermes_managed_assets.py",
        "fin_analyse/gateway/mcp_server.py",
    ),
)
def test_runtime_import_probe_rejects_broken_entrypoint_import(
    tmp_path: Path,
    relative: str,
) -> None:
    layout = _seed_release(tmp_path)
    (layout.release_root / relative).write_text(
        "import missing_release_dependency\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="entrypoint import probe"):
        release_tool._runtime_import_probe(layout)


def test_runtime_import_probe_survives_mcp_stdout_guard(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    (layout.release_root / "fin_analyse/gateway/mcp_server.py").write_text(
        """\
import sys


class StdoutGuard:
    def write(self, text: str) -> int:
        return sys.__stderr__.write(text)

    def flush(self) -> None:
        sys.__stderr__.flush()


sys.stdout = StdoutGuard()
""",
        encoding="utf-8",
    )

    assert release_tool._runtime_import_probe(layout)["ready"] is True


@pytest.mark.parametrize(
    "relative",
    (
        "scripts/apply_fin_hermes_external_integration.py",
        "scripts/prepare_fin_release.py",
        "scripts/capture_zsxq_windows.cjs",
        "scripts/consume_zsxq_capture_folder.py",
        "scripts/zsxq_windows_incremental_scheduler.py",
        "fin_analyse/consultation/presentation.py",
        "fin_analyse/gateway/mcp_server.py",
        "fin_analyse/guo_teacher_research/semantic_state.py",
        "fin_analyse/guo_teacher_research/g_working_set.py",
        "fin_analyse/scraper/cdp_runtime.py",
        "fin_analyse/scraper/scheduled_run.py",
        "hermes-migration/cron/jobs.json",
        "hermes-migration/plugins/fin-consultation-first-tool/plugin.yaml",
        "hermes-migration/skills/fin-analyse/fin-analyse-consultation/SKILL.md",
        "hermes-migration/skills/fin-analyse/fin-analyse-ops/SKILL.md",
        "hermes-migration/systemd/hermes-gateway-fin.service",
        "hermes-migration/systemd/hermes-gateway-fin.service.d/20-fin-python-safety.conf",
    ),
)
def test_release_status_requires_every_current_runtime_asset(
    tmp_path: Path,
    relative: str,
) -> None:
    layout = _seed_release(tmp_path)
    (layout.release_root / relative).unlink()

    status = inspect_release(layout)

    assert status["ready"] is False
    assert status["code"]["required_files"][relative] is False
    error_type = PermissionError if relative in release_tool._CRITICAL_RUNTIME_FILES else ValueError
    error_pattern = (
        "single-link"
        if relative in release_tool._CRITICAL_RUNTIME_FILES
        else "tracked modifications"
    )
    with pytest.raises(error_type, match=error_pattern):
        prepare_release_bindings(layout)


def test_release_status_treats_fin_python_safety_drop_in_as_critical(
    tmp_path: Path,
) -> None:
    layout = _seed_release(tmp_path)
    relative = "hermes-migration/systemd/hermes-gateway-fin.service.d/20-fin-python-safety.conf"
    (layout.release_root / relative).unlink()

    status = inspect_release(layout)

    assert status["ready"] is False
    assert status["critical_runtime_files"][relative] is False
    with pytest.raises(PermissionError, match="single-link"):
        prepare_release_bindings(layout)


def test_prepare_rejects_venv_drift_after_frozen_sync_receipt(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    (layout.release_root / ".venv/sitecustomize.py").write_text(
        "raise SystemExit\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="frozen-sync receipt"):
        prepare_release_bindings(layout)


def test_prepare_accepts_venv_bytecode_drift_after_frozen_sync_receipt(
    tmp_path: Path,
) -> None:
    layout = _seed_release(tmp_path)
    site_packages = next((layout.release_root / ".venv/lib").glob("python*/site-packages"))
    cache = site_packages / "__pycache__"
    cache.mkdir(mode=0o775)
    bytecode = cache / "runtime_created.cpython-313.pyc"
    bytecode.write_bytes(b"test venv bytecode\n")
    bytecode.chmod(0o600)

    status = prepare_release_bindings(layout)

    assert status["ready"] is True
    assert status["code"]["frozen_sync_receipt"] is True


def test_venv_tree_digest_ignores_pycache_and_tracks_real_drift(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    (venv / "lib").mkdir(parents=True)
    (venv / "lib" / "sitecustomize.py").write_text("x=1\n", encoding="utf-8")
    before = release_tool._venv_digest(venv)

    cache = venv / "lib" / "__pycache__"
    cache.mkdir()
    (cache / "sitecustomize.cpython-313.pyc").write_bytes(b"bytecode\n")
    assert release_tool._venv_digest(venv) == before

    (venv / "lib" / "real.py").write_text("y=2\n", encoding="utf-8")
    assert release_tool._venv_digest(venv) != before


def test_record_sync_converges_special_handoff_modes_without_changing_identity(
    tmp_path: Path,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    launcher_relative = "scripts/consultation_runtime_canary_launcher.py"
    plugin_relative = "hermes-migration/plugins/fin-consultation-first-tool"
    config_relative = "config"
    cron_relative = "hermes-migration/cron/jobs.json"
    launcher = layout.release_root / launcher_relative
    plugin = layout.release_root / plugin_relative
    config = layout.release_root / config_relative
    cron = layout.release_root / cron_relative
    plugin_child = plugin / "__init__.py"
    config_child = config / "llm.yaml.example"
    launcher.chmod(0o664)
    plugin.chmod(0o775)
    config.chmod(0o775)
    cron.chmod(0o664)
    plugin_child.chmod(0o664)
    launcher_before = launcher.stat()
    launcher_bytes = launcher.read_bytes()
    plugin_before = plugin.stat()
    config_before = config.stat()
    cron_before = cron.stat()
    cron_bytes = cron.read_bytes()
    plugin_child_before = plugin_child.stat()
    plugin_child_bytes = plugin_child.read_bytes()
    config_child_before = config_child.stat()
    config_child_bytes = config_child.read_bytes()

    result = record_frozen_sync(layout)

    assert result["handoff_modes"] == {
        "changed": True,
        "targets": [
            {"path": launcher_relative, "changed": True},
            {"path": plugin_relative, "changed": True},
            {"path": config_relative, "changed": True},
            {"path": cron_relative, "changed": True},
        ],
    }
    receipt_payload = json.loads(
        (layout.release_root / ".fin-frozen-sync.json").read_text(encoding="utf-8")
    )
    assert receipt_payload["schema_version"] == 3
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        receipt_payload["handoff_binding_sha256"],
    )
    launcher_after = launcher.stat()
    plugin_after = plugin.stat()
    config_after = config.stat()
    cron_after = cron.stat()
    assert stat.S_IMODE(launcher_after.st_mode) == 0o600
    assert stat.S_IMODE(plugin_after.st_mode) == 0o700
    assert stat.S_IMODE(config_after.st_mode) == 0o755
    assert stat.S_IMODE(cron_after.st_mode) == 0o600
    assert (launcher_after.st_dev, launcher_after.st_ino) == (
        launcher_before.st_dev,
        launcher_before.st_ino,
    )
    assert launcher.read_bytes() == launcher_bytes
    assert (plugin_after.st_dev, plugin_after.st_ino) == (
        plugin_before.st_dev,
        plugin_before.st_ino,
    )
    assert (config_after.st_dev, config_after.st_ino) == (
        config_before.st_dev,
        config_before.st_ino,
    )
    assert (cron_after.st_dev, cron_after.st_ino) == (
        cron_before.st_dev,
        cron_before.st_ino,
    )
    assert cron.read_bytes() == cron_bytes
    plugin_child_after = plugin_child.stat()
    assert stat.S_IMODE(plugin_child_after.st_mode) == 0o664
    assert (plugin_child_after.st_dev, plugin_child_after.st_ino) == (
        plugin_child_before.st_dev,
        plugin_child_before.st_ino,
    )
    assert plugin_child.read_bytes() == plugin_child_bytes
    config_child_after = config_child.stat()
    assert stat.S_IMODE(config_child_after.st_mode) == stat.S_IMODE(config_child_before.st_mode)
    assert (config_child_after.st_dev, config_child_after.st_ino) == (
        config_child_before.st_dev,
        config_child_before.st_ino,
    )
    assert config_child.read_bytes() == config_child_bytes
    repeated = record_frozen_sync(layout)
    assert repeated["handoff_modes"] == {
        "changed": False,
        "targets": [
            {"path": launcher_relative, "changed": False},
            {"path": plugin_relative, "changed": False},
            {"path": config_relative, "changed": False},
            {"path": cron_relative, "changed": False},
        ],
    }

    status = prepare_release_bindings(layout)

    assert status["ready"] is True
    assert status["code"]["tracked_clean"] is True
    assert status["code"]["frozen_sync_receipt"] is True
    assert status["handoff_modes"] == {
        launcher_relative: True,
        plugin_relative: True,
        config_relative: True,
        cron_relative: True,
    }


def test_frozen_sync_receipt_binds_hermes_plugin_contents(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    plugin_entrypoint = (
        layout.release_root / "hermes-migration/plugins/fin-consultation-first-tool/__init__.py"
    )

    plugin_entrypoint.write_text("# post-receipt plugin drift\n", encoding="utf-8")

    assert release_tool._sync_receipt_valid(layout) is False


@pytest.mark.parametrize("index_flag", ("--skip-worktree", "--assume-unchanged"))
@pytest.mark.parametrize(
    "relative",
    (
        "hermes-migration/plugins/fin-consultation-first-tool/__init__.py",
        "hermes-migration/plugins/fin-consultation-first-tool/plugin.yaml",
    ),
)
def test_release_readiness_reports_hidden_hermes_plugin_drift_as_unsafe(
    tmp_path: Path,
    relative: str,
    index_flag: str,
) -> None:
    layout = _seed_release(tmp_path)
    _git("update-index", index_flag, "--", relative, cwd=layout.release_root)
    (layout.release_root / relative).write_text(
        "# hidden plugin drift\n",
        encoding="utf-8",
    )
    assert (
        _git(
            "status",
            "--porcelain",
            "--untracked-files=no",
            cwd=layout.release_root,
        )
        == ""
    )

    status = release_tool.inspect_candidate_release(layout)

    assert status["ready"] is False
    assert status["critical_runtime_files"][relative] is False


def test_record_sync_rejects_symlinked_special_handoff_file(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    target = tmp_path / "launcher-target.py"
    target.write_bytes(launcher.read_bytes())
    target.chmod(0o600)
    launcher.unlink()
    launcher.symlink_to(target)

    with pytest.raises(PermissionError, match="critical runtime files"):
        record_frozen_sync(layout)

    assert launcher.is_symlink()
    assert not (layout.release_root / ".fin-frozen-sync.json").exists()


def test_record_sync_rejects_hardlinked_special_handoff_file(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    alias = tmp_path / "launcher-hardlink.py"
    os.link(launcher, alias)

    with pytest.raises(PermissionError, match="critical runtime files"):
        record_frozen_sync(layout)

    assert launcher.stat().st_nlink == 2
    assert alias.stat().st_ino == launcher.stat().st_ino
    assert not (layout.release_root / ".fin-frozen-sync.json").exists()


def test_record_sync_rejects_special_handoff_file_with_wrong_type(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    launcher.unlink()
    os.mkfifo(launcher)

    with pytest.raises(PermissionError, match="critical runtime files"):
        record_frozen_sync(layout)

    assert stat.S_ISFIFO(launcher.lstat().st_mode)
    assert not (layout.release_root / ".fin-frozen-sync.json").exists()


@pytest.mark.parametrize(
    "invalid_relative",
    (
        "hermes-migration/plugins/fin-consultation-first-tool",
        "config",
        "hermes-migration/cron/jobs.json",
    ),
)
def test_later_handoff_target_preflight_failure_has_zero_partial_mode_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_relative: str,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    plugin = layout.release_root / "hermes-migration/plugins/fin-consultation-first-tool"
    config = layout.release_root / "config"
    cron = layout.release_root / "hermes-migration/cron/jobs.json"
    launcher.chmod(0o664)
    plugin.chmod(0o775)
    config.chmod(0o775)
    cron.chmod(0o664)
    invalid = layout.release_root / invalid_relative
    invalid_identity = (invalid.stat().st_dev, invalid.stat().st_ino)
    original_fstat = os.fstat

    def fstat_with_non_owner_target(descriptor: int) -> os.stat_result:
        metadata = original_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == invalid_identity:
            fields = list(metadata)
            fields[4] = metadata.st_uid + 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(release_tool.os, "fstat", fstat_with_non_owner_target)

    with pytest.raises(PermissionError, match="handoff targets failed preflight"):
        record_frozen_sync(layout)

    assert stat.S_IMODE(launcher.stat().st_mode) == 0o664
    assert stat.S_IMODE(plugin.stat().st_mode) == 0o775
    assert stat.S_IMODE(config.stat().st_mode) == 0o775
    assert stat.S_IMODE(cron.stat().st_mode) == 0o664
    assert not (layout.release_root / ".fin-frozen-sync.json").exists()


def test_record_sync_rejects_intermediate_handoff_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    plugins = layout.release_root / "hermes-migration/plugins"
    plugin = plugins / "fin-consultation-first-tool"
    launcher.chmod(0o664)
    plugin.chmod(0o775)
    launcher_identity = (launcher.stat().st_dev, launcher.stat().st_ino)
    original_fchmod = os.fchmod
    swapped = False

    def fchmod_then_replace_plugins(descriptor: int, mode: int) -> None:
        nonlocal swapped
        original_fchmod(descriptor, mode)
        metadata = os.fstat(descriptor)
        if swapped or (metadata.st_dev, metadata.st_ino) != launcher_identity:
            return
        detached_plugins = tmp_path / "detached-plugins"
        plugins.rename(detached_plugins)
        plugins.mkdir()
        replacement = plugins / "fin-consultation-first-tool"
        replacement.mkdir()
        replacement.chmod(0o775)
        for source in (detached_plugins / "fin-consultation-first-tool").iterdir():
            target = replacement / source.name
            target.write_bytes(source.read_bytes())
            target.chmod(stat.S_IMODE(source.stat().st_mode))
        swapped = True

    monkeypatch.setattr(release_tool.os, "fchmod", fchmod_then_replace_plugins)

    with pytest.raises(PermissionError, match="canonical binding"):
        record_frozen_sync(layout)

    assert swapped is True
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o600
    assert stat.S_IMODE(plugin.stat().st_mode) == 0o775
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=layout.release_root) == ""
    assert not (layout.release_root / ".fin-frozen-sync.json").exists()


def test_record_sync_rejects_higher_handoff_ancestor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    plugin = layout.release_root / "hermes-migration/plugins/fin-consultation-first-tool"
    hermes = layout.release_root / "hermes-migration"
    release_root_metadata = layout.release_root.stat()
    hermes_mode = stat.S_IMODE(hermes.stat().st_mode)
    hermes_identity = (hermes.stat().st_dev, hermes.stat().st_ino)
    launcher.chmod(0o664)
    plugin.chmod(0o775)
    launcher_identity = (launcher.stat().st_dev, launcher.stat().st_ino)
    original_fchmod = os.fchmod
    replacement_identity: tuple[int, int] | None = None

    def fchmod_then_replace_hermes(descriptor: int, mode: int) -> None:
        nonlocal replacement_identity
        original_fchmod(descriptor, mode)
        metadata = os.fstat(descriptor)
        if (
            replacement_identity is not None
            or (metadata.st_dev, metadata.st_ino) != launcher_identity
        ):
            return
        detached_hermes = tmp_path / "detached-hermes-migration"
        hermes.rename(detached_hermes)
        hermes.mkdir()
        hermes.chmod(hermes_mode)
        for child in tuple(detached_hermes.iterdir()):
            child.rename(hermes / child.name)
        replacement = hermes.stat()
        replacement_identity = (replacement.st_dev, replacement.st_ino)
        os.utime(
            layout.release_root,
            ns=(release_root_metadata.st_atime_ns, release_root_metadata.st_mtime_ns),
        )

    monkeypatch.setattr(release_tool.os, "fchmod", fchmod_then_replace_hermes)

    with pytest.raises(PermissionError, match="canonical binding"):
        record_frozen_sync(layout)

    assert replacement_identity is not None
    assert replacement_identity != hermes_identity
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=layout.release_root) == ""
    assert not (layout.release_root / ".fin-frozen-sync.json").exists()


def test_record_sync_rejects_scripts_ancestor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    scripts = layout.release_root / "scripts"
    launcher = scripts / "consultation_runtime_canary_launcher.py"
    plugin = layout.release_root / "hermes-migration/plugins/fin-consultation-first-tool"
    scripts_mode = stat.S_IMODE(scripts.stat().st_mode)
    scripts_identity = (scripts.stat().st_dev, scripts.stat().st_ino)
    launcher.chmod(0o664)
    plugin.chmod(0o775)
    launcher_identity = (launcher.stat().st_dev, launcher.stat().st_ino)
    original_fchmod = os.fchmod
    replacement_identity: tuple[int, int] | None = None

    def fchmod_then_replace_scripts(descriptor: int, mode: int) -> None:
        nonlocal replacement_identity
        original_fchmod(descriptor, mode)
        metadata = os.fstat(descriptor)
        if (
            replacement_identity is not None
            or (metadata.st_dev, metadata.st_ino) != launcher_identity
        ):
            return
        detached_scripts = tmp_path / "detached-scripts"
        scripts.rename(detached_scripts)
        scripts.mkdir()
        scripts.chmod(scripts_mode)
        for child in tuple(detached_scripts.iterdir()):
            child.rename(scripts / child.name)
        replacement = scripts.stat()
        replacement_identity = (replacement.st_dev, replacement.st_ino)

    monkeypatch.setattr(release_tool.os, "fchmod", fchmod_then_replace_scripts)

    with pytest.raises(PermissionError, match="handoff target"):
        record_frozen_sync(layout)

    assert replacement_identity is not None
    assert replacement_identity != scripts_identity
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=layout.release_root) == ""
    assert not (layout.release_root / ".fin-frozen-sync.json").exists()


def test_record_sync_rejects_release_root_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    release_root = layout.release_root
    launcher = release_root / "scripts/consultation_runtime_canary_launcher.py"
    plugin = release_root / "hermes-migration/plugins/fin-consultation-first-tool"
    release_root_identity = (
        release_root.stat().st_dev,
        release_root.stat().st_ino,
    )
    launcher.chmod(0o664)
    plugin.chmod(0o775)
    launcher_identity = (launcher.stat().st_dev, launcher.stat().st_ino)
    original_fchmod = os.fchmod
    replacement_identity: tuple[int, int] | None = None

    def fchmod_then_replace_release_root(descriptor: int, mode: int) -> None:
        nonlocal replacement_identity
        original_fchmod(descriptor, mode)
        metadata = os.fstat(descriptor)
        if (
            replacement_identity is not None
            or (metadata.st_dev, metadata.st_ino) != launcher_identity
        ):
            return
        detached_release = tmp_path / "detached-release-root"
        release_root.rename(detached_release)
        release_root.mkdir(mode=0o700)
        for child in tuple(detached_release.iterdir()):
            child.rename(release_root / child.name)
        replacement = release_root.stat()
        replacement_identity = (replacement.st_dev, replacement.st_ino)

    monkeypatch.setattr(release_tool.os, "fchmod", fchmod_then_replace_release_root)

    with pytest.raises(
        PermissionError,
        match=r"handoff (?:target changed|canonical binding)",
    ):
        record_frozen_sync(layout)

    assert replacement_identity is not None
    assert replacement_identity != release_root_identity
    assert _git("status", "--porcelain", "--untracked-files=no", cwd=release_root) == ""
    assert not (release_root / ".fin-frozen-sync.json").exists()


def test_record_sync_rejects_mode_drift_at_receipt_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    original_atomic_write = release_tool._atomic_write_receipt

    def atomic_write_then_drift(path: Path, payload: dict[str, object]) -> object:
        publication = original_atomic_write(path, payload)
        launcher.chmod(0o644)
        return publication

    monkeypatch.setattr(
        release_tool,
        "_atomic_write_receipt",
        atomic_write_then_drift,
    )

    with pytest.raises(PermissionError, match="receipt publication boundary"):
        record_frozen_sync(layout)

    assert stat.S_IMODE(launcher.stat().st_mode) == 0o644
    assert not receipt.exists()
    assert release_tool._sync_receipt_valid(layout) is False


def test_record_sync_revalidates_handoff_after_final_runtime_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    original_runtime_import_probe = release_tool._runtime_import_probe

    def runtime_probe_then_drift(
        candidate: ReleaseLayout,
    ) -> dict[str, object]:
        result = original_runtime_import_probe(candidate)
        if receipt.exists():
            launcher.chmod(0o644)
        return result

    monkeypatch.setattr(
        release_tool,
        "_runtime_import_probe",
        runtime_probe_then_drift,
    )

    with pytest.raises(PermissionError, match="receipt publication boundary"):
        record_frozen_sync(layout)

    assert stat.S_IMODE(launcher.stat().st_mode) == 0o644
    assert not receipt.exists()


def test_repeat_record_removes_only_its_new_receipt_after_boundary_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    original_atomic_write = release_tool._atomic_write_receipt

    def atomic_write_then_drift(path: Path, payload: dict[str, object]) -> object:
        publication = original_atomic_write(path, payload)
        launcher.chmod(0o644)
        return publication

    monkeypatch.setattr(
        release_tool,
        "_atomic_write_receipt",
        atomic_write_then_drift,
    )

    with pytest.raises(PermissionError, match="receipt publication boundary"):
        record_frozen_sync(layout)

    assert not receipt.exists()
    assert release_tool._sync_receipt_valid(layout) is False


def test_cleanup_collision_cannot_revive_failed_receipt_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    original_atomic_write = release_tool._atomic_write_receipt
    collision: Path | None = None

    def atomic_write_then_collide(path: Path, payload: dict[str, object]) -> object:
        nonlocal collision
        publication = original_atomic_write(path, payload)
        collision = path.parent / (
            f".{path.name}.invalid-{os.getpid()}-{publication.metadata_stamp[1]}"
        )
        collision.write_bytes(b"reserved cleanup target\n")
        collision.chmod(0o600)
        launcher.chmod(0o644)
        return publication

    monkeypatch.setattr(
        release_tool,
        "_atomic_write_receipt",
        atomic_write_then_collide,
    )

    with pytest.raises(PermissionError, match="receipt publication boundary"):
        record_frozen_sync(layout)

    assert collision is not None
    assert collision.read_bytes() == b"reserved cleanup target\n"
    assert receipt.exists()

    collision.unlink()
    launcher.chmod(0o600)

    assert release_tool._sync_receipt_valid(layout) is False


def test_reversible_gid_drift_cannot_revive_failed_receipt_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    original_atomic_write = release_tool._atomic_write_receipt
    original_fstat = os.fstat
    publication_descriptor: int | None = None
    gid_drifted = False

    def fstat_with_reversible_gid_drift(descriptor: int) -> os.stat_result:
        metadata = original_fstat(descriptor)
        if gid_drifted and descriptor == publication_descriptor:
            fields = list(metadata)
            fields[5] = metadata.st_gid + 1
            return os.stat_result(fields)
        return metadata

    def atomic_write_then_drift(path: Path, payload: dict[str, object]) -> object:
        nonlocal gid_drifted, publication_descriptor
        publication = original_atomic_write(path, payload)
        publication_descriptor = publication.descriptor
        gid_drifted = True
        launcher.chmod(0o644)
        return publication

    monkeypatch.setattr(release_tool.os, "fstat", fstat_with_reversible_gid_drift)
    monkeypatch.setattr(
        release_tool,
        "_atomic_write_receipt",
        atomic_write_then_drift,
    )

    with pytest.raises(PermissionError, match="receipt publication boundary"):
        record_frozen_sync(layout)

    gid_drifted = False
    launcher.chmod(0o600)

    assert not receipt.exists() or receipt.read_bytes() == b""
    assert release_tool._sync_receipt_valid(layout) is False


def test_unknown_replacement_cannot_preserve_aside_failed_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    original_atomic_write = release_tool._atomic_write_receipt
    aside = receipt.parent / ".aside-exact-frozen-sync"
    unknown_payload = b"unknown replacement must remain untouched\n"

    def atomic_write_then_hide(path: Path, payload: dict[str, object]) -> object:
        publication = original_atomic_write(path, payload)
        path.rename(aside)
        path.write_bytes(unknown_payload)
        path.chmod(0o600)
        launcher.chmod(0o644)
        return publication

    monkeypatch.setattr(
        release_tool,
        "_atomic_write_receipt",
        atomic_write_then_hide,
    )

    with pytest.raises(PermissionError, match="receipt publication boundary"):
        record_frozen_sync(layout)

    assert receipt.read_bytes() == unknown_payload
    preserved_unknown = receipt.parent / ".preserved-unknown-frozen-sync"
    receipt.rename(preserved_unknown)
    aside.rename(receipt)
    launcher.chmod(0o600)

    assert preserved_unknown.read_bytes() == unknown_payload
    assert release_tool._sync_receipt_valid(layout) is False


def test_hardlink_cannot_preserve_failed_receipt_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    alias = tmp_path / "failed-publication-alias"
    original_atomic_write = release_tool._atomic_write_receipt

    def atomic_write_then_link(path: Path, payload: dict[str, object]) -> object:
        publication = original_atomic_write(path, payload)
        os.link(path, alias)
        launcher.chmod(0o644)
        return publication

    monkeypatch.setattr(
        release_tool,
        "_atomic_write_receipt",
        atomic_write_then_link,
    )

    with pytest.raises(PermissionError, match="receipt publication boundary"):
        record_frozen_sync(layout)

    assert alias.exists()
    assert alias.read_bytes() == b""
    alias.unlink()
    launcher.chmod(0o600)

    assert release_tool._sync_receipt_valid(layout) is False


def test_record_sync_preserves_unknown_receipt_replacement_after_boundary_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path, record_sync=False)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    launcher = layout.release_root / "scripts/consultation_runtime_canary_launcher.py"
    original_atomic_write = release_tool._atomic_write_receipt
    replacement_identity: tuple[int, int] | None = None
    replacement_payload: bytes | None = None

    def atomic_write_then_replace(path: Path, payload: dict[str, object]) -> object:
        nonlocal replacement_identity, replacement_payload
        publication = original_atomic_write(path, payload)
        replacement = path.parent / ".unknown-frozen-sync-replacement"
        replacement_payload = path.read_bytes()
        replacement.write_bytes(replacement_payload)
        replacement.chmod(0o600)
        os.replace(replacement, path)
        metadata = path.stat()
        replacement_identity = (metadata.st_dev, metadata.st_ino)
        return publication

    monkeypatch.setattr(
        release_tool,
        "_atomic_write_receipt",
        atomic_write_then_replace,
    )

    with pytest.raises(PermissionError, match="receipt publication boundary"):
        record_frozen_sync(layout)

    assert replacement_identity is not None
    assert replacement_payload is not None
    assert receipt.exists()
    assert (receipt.stat().st_dev, receipt.stat().st_ino) == replacement_identity
    assert receipt.read_bytes() == replacement_payload
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o600
    assert release_tool._sync_receipt_valid(layout) is False


@pytest.mark.parametrize(
    ("relative", "drift_mode", "expected_mode"),
    (
        ("scripts/consultation_runtime_canary_launcher.py", 0o644, 0o600),
        ("hermes-migration/plugins/fin-consultation-first-tool", 0o755, 0o700),
        ("config", 0o775, 0o755),
        ("hermes-migration/cron/jobs.json", 0o664, 0o600),
    ),
)
def test_release_readiness_fails_closed_on_special_handoff_mode_drift(
    tmp_path: Path,
    relative: str,
    drift_mode: int,
    expected_mode: int,
) -> None:
    layout = _seed_release(tmp_path)
    assert prepare_release_bindings(layout)["ready"] is True
    target = layout.release_root / relative

    target.chmod(drift_mode)
    drifted = release_tool.inspect_candidate_release(layout)

    assert drifted["ready"] is False
    assert drifted["handoff_modes"][relative] is False
    assert drifted["code"]["tracked_clean"] is True
    assert drifted["code"]["frozen_sync_receipt"] is False

    target.chmod(expected_mode)
    restored = release_tool.inspect_candidate_release(layout)

    assert restored["ready"] is True
    assert restored["handoff_modes"][relative] is True


def test_record_frozen_sync_rejects_fake_python(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    receipt.unlink()
    interpreter = layout.release_root / ".venv/bin/python"
    interpreter.unlink()
    interpreter.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    interpreter.chmod(0o755)

    with pytest.raises(ValueError, match="Python identity"):
        record_frozen_sync(layout)


def test_record_frozen_sync_does_not_execute_candidate_site_hooks(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    receipt.unlink()
    site_packages = next((layout.release_root / ".venv/lib").glob("python*/site-packages"))
    pth_sentinel = tmp_path / "candidate-pth-ran"
    sitecustomize_sentinel = tmp_path / "candidate-sitecustomize-ran"
    (site_packages / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(sitecustomize_sentinel)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    (site_packages / "candidate.pth").write_text(
        "import pathlib; "
        f"pathlib.Path({str(pth_sentinel)!r}).write_text('ran')\n"
        "import sitecustomize\n",
        encoding="utf-8",
    )

    result = record_frozen_sync(layout)

    assert result["recorded"] is True
    assert receipt.is_file()
    assert not pth_sentinel.exists()
    assert not sitecustomize_sentinel.exists()


def test_prepare_rejects_corrupt_frozen_sync_receipt(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    (layout.release_root / ".fin-frozen-sync.json").write_text(
        '{"schema_version":"wrong"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="frozen-sync receipt"):
        prepare_release_bindings(layout)


def test_prepare_rejects_non_owner_only_environment_file(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    layout.env_file.chmod(0o644)

    with pytest.raises(PermissionError, match="owner-only 0600"):
        prepare_release_bindings(layout)


def test_prepare_refuses_to_replace_unexpected_release_binding(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    (layout.release_root / ".env").write_text("UNEXPECTED=1\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="unexpected release binding"):
        prepare_release_bindings(layout)


def test_prepare_preflights_all_bindings_without_partial_install(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    conflict = layout.release_root / "knowledge-base/market-cache"
    conflict.parent.mkdir(parents=True)
    conflict.write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="unexpected release binding"):
        prepare_release_bindings(layout)

    assert not os.path.lexists(layout.release_root / ".env")
    assert not os.path.lexists(layout.release_root / "knowledge-base/runtime")


def test_prepare_rejects_group_or_other_writable_data_root(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    layout.data_root.chmod(0o777)

    with pytest.raises(PermissionError, match="secure owner-controlled directory"):
        prepare_release_bindings(layout)


@pytest.mark.parametrize("mode", [0o755, 0o770])
def test_prepare_requires_exact_owner_only_release_root(
    tmp_path: Path,
    mode: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path)
    layout.release_root.chmod(mode)

    def fail_if_inspected(*_args, **_kwargs):
        raise AssertionError("unsafe release code must not be inspected")

    monkeypatch.setattr(release_tool, "_run_git", fail_if_inspected)
    monkeypatch.setattr(release_tool, "_sync_receipt_valid", fail_if_inspected)
    status = inspect_release(layout)

    assert status["secure_directories"][str(layout.release_root)] is False
    with pytest.raises(PermissionError, match="secure owner-controlled directory"):
        prepare_release_bindings(layout)
    assert not os.path.lexists(layout.release_root / ".env")


def test_prepare_accepts_group_writable_inner_files_except_special_handoff_targets(
    tmp_path: Path,
) -> None:
    layout = _seed_release(tmp_path)
    special_targets = {
        relative for relative, _kind, _mode in release_tool._SPECIAL_HANDOFF_MODE_TARGETS
    }
    for relative in release_tool._CRITICAL_RUNTIME_FILES:
        if relative not in special_targets:
            (layout.release_root / relative).chmod(0o664)
    for relative in (
        "hermes-migration/plugins/fin-consultation-first-tool/__init__.py",
        "hermes-migration/plugins/fin-consultation-first-tool/plugin.yaml",
    ):
        (layout.release_root / relative).chmod(0o664)

    prepared = prepare_release_bindings(layout)

    assert prepared["ready"] is True


def test_prepare_rejects_hardlinked_critical_file_before_git_or_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path)
    os.link(
        layout.release_root / "fin_analyse/guo_teacher_research/capability_broker.py",
        layout.home / "capability-broker-copy.py",
    )

    def fail_if_inspected(*_args, **_kwargs):
        raise AssertionError("hardlinked release code must not be inspected")

    monkeypatch.setattr(release_tool, "_run_git", fail_if_inspected)
    monkeypatch.setattr(release_tool, "_sync_receipt_valid", fail_if_inspected)

    status = inspect_release(layout)

    assert status["ready"] is False
    assert (
        status["critical_runtime_files"]["fin_analyse/guo_teacher_research/capability_broker.py"]
        is False
    )
    with pytest.raises(PermissionError, match="single-link"):
        prepare_release_bindings(layout)


def test_noncritical_runtime_hardlink_precedes_receipt_and_import_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path)
    relative = "fin_analyse/guo_teacher_research/semantic_service.py"
    os.link(layout.release_root / relative, layout.home / "semantic-service-copy.py")

    def fail_if_observed(*_args, **_kwargs):
        raise AssertionError("untrusted runtime code must not reach receipt or import validation")

    monkeypatch.setattr(release_tool, "_sync_receipt_valid", fail_if_observed)

    status = inspect_release(layout)

    assert status["ready"] is False
    assert status["code"]["required_files"][relative] is False
    with pytest.raises(FileNotFoundError, match="required files"):
        prepare_release_bindings(layout)


def test_sync_receipt_validation_rejects_noncritical_hardlink_before_import_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path)
    os.link(
        layout.release_root / "fin_analyse/guo_teacher_research/semantic_service.py",
        layout.home / "semantic-service-copy.py",
    )

    def fail_if_probed(*_args, **_kwargs):
        raise AssertionError("untrusted runtime code must not be imported")

    monkeypatch.setattr(release_tool, "_runtime_import_probe", fail_if_probed)

    assert release_tool._sync_receipt_valid(layout) is False


def test_runtime_probe_rejects_transitive_source_hardlink_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path)
    os.link(
        layout.release_root / "fin_analyse/common/execution_control.py",
        layout.home / "execution-control-copy.py",
    )

    def fail_if_started(*_args, **_kwargs):
        raise AssertionError("runtime subprocess must not start before full source-tree validation")

    monkeypatch.setattr(release_tool.subprocess, "run", fail_if_started)

    with pytest.raises(ValueError, match="source tree"):
        release_tool._runtime_import_probe(layout)


@pytest.mark.parametrize(
    "filename",
    (
        f"rogue{importlib.machinery.BYTECODE_SUFFIXES[0]}",
        f"rogue{importlib.machinery.EXTENSION_SUFFIXES[0]}",
    ),
)
def test_runtime_probe_rejects_precompiled_source_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    layout = _seed_release(tmp_path)
    artifact = layout.release_root / "fin_analyse/common" / filename
    artifact.write_bytes(b"candidate import artifact")

    def fail_if_started(*_args, **_kwargs):
        raise AssertionError("runtime subprocess must not start before import artifact validation")

    monkeypatch.setattr(release_tool.subprocess, "run", fail_if_started)

    with pytest.raises(ValueError, match="source tree"):
        release_tool._runtime_import_probe(layout)


def test_release_git_observation_disables_repository_fsmonitor(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    sentinel = tmp_path / "release-fsmonitor-ran"
    hook = tmp_path / "release-fsmonitor"
    hook.write_text(
        f"#!/bin/sh\n/usr/bin/touch {sentinel}\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    _git("config", "core.fsmonitor", str(hook), cwd=layout.release_root)

    completed = release_tool._run_git(
        layout.release_root,
        "status",
        "--porcelain",
        "--untracked-files=no",
    )

    assert completed.returncode == 0
    assert not sentinel.exists()


def test_prior_release_boundary_precedes_prior_git_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _seed_release(tmp_path)
    prepare_release_bindings(candidate)
    prior = _seed_schema1_prior_release(tmp_path, candidate)
    prior.release_root.chmod(0o755)
    original_run_git = release_tool._run_git

    def reject_prior_git(root: Path, *args: str):
        if root == prior.release_root:
            raise AssertionError("unsafe prior release must not be inspected")
        return original_run_git(root, *args)

    monkeypatch.setattr(release_tool, "_run_git", reject_prior_git)

    with pytest.raises(PermissionError, match="secure owner-controlled directory"):
        release_tool._require_versioned_previous_baseline(candidate, prior)


@pytest.mark.parametrize("current_kind", ["file", "dangling", "outside"])
def test_activate_rejects_invalid_existing_current_pointer(
    tmp_path: Path,
    current_kind: str,
) -> None:
    layout = _seed_release(tmp_path)
    prepare_release_bindings(layout)
    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    if current_kind == "file":
        layout.current_link.write_text("not a symlink\n", encoding="utf-8")
        expected_error: type[Exception] = FileExistsError
    elif current_kind == "dangling":
        layout.current_link.symlink_to(layout.data_root / "missing-release")
        expected_error = FileNotFoundError
    else:
        outside = layout.home / "outside-release"
        outside.mkdir()
        layout.current_link.symlink_to(outside)
        expected_error = PermissionError

    with pytest.raises(expected_error):
        activate_release(layout)


def test_activate_revalidates_existing_current_release_under_lock(tmp_path: Path) -> None:
    layout = _seed_release(tmp_path)
    prepare_release_bindings(layout)
    prior_release = layout.releases_root / ("b" * 40)
    prior_release.mkdir(mode=0o700)
    layout.current_link.symlink_to(prior_release.relative_to(layout.current_link.parent))
    pointer_before = os.readlink(layout.current_link)

    with pytest.raises(RuntimeError, match="not a fully ready release"):
        activate_release(layout, expected_current_commit="b" * 40)

    assert os.readlink(layout.current_link) == pointer_before
    assert layout.current_link.resolve(strict=True) == prior_release


def test_activate_rejects_receipt_replacement_during_final_readiness_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _seed_release(tmp_path)
    prepare_release_bindings(layout)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    original_runtime_import_probe = release_tool._runtime_import_probe
    probe_count = 0
    replacement_identity: tuple[int, int] | None = None

    def runtime_probe_then_replace_receipt(
        candidate: ReleaseLayout,
    ) -> dict[str, object]:
        nonlocal probe_count, replacement_identity
        result = original_runtime_import_probe(candidate)
        probe_count += 1
        if probe_count == 2:
            replacement = receipt.parent / ".replacement-frozen-sync"
            replacement.write_bytes(receipt.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, receipt)
            metadata = receipt.stat()
            replacement_identity = (metadata.st_dev, metadata.st_ino)
        return result

    monkeypatch.setattr(
        release_tool,
        "_runtime_import_probe",
        runtime_probe_then_replace_receipt,
    )

    with pytest.raises(RuntimeError, match="candidate release changed during activation"):
        activate_release(layout)

    assert probe_count == 2
    assert replacement_identity is not None
    assert not os.path.lexists(layout.current_link)
    assert release_tool.inspect_candidate_release(layout)["ready"] is False


def test_release_cli_checks_prepares_and_activates_with_json_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = _seed_release(tmp_path)
    common = ("--home", str(layout.home), "--commit", layout.commit)
    (layout.release_root / ".fin-frozen-sync.json").unlink()

    assert main(("record-sync", *common)) == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is True
    assert main(("check", *common)) == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False
    assert main(("prepare", *common)) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is True
    assert main(("activate", *common)) == 0
    assert json.loads(capsys.readouterr().out)["active"] is True


def test_release_cli_activates_only_the_expected_prior(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)

    assert (
        main(
            (
                "activate",
                "--home",
                str(candidate.home),
                "--commit",
                candidate.commit,
                "--expected-current-commit",
                prior.commit,
            )
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["active"] is True
    assert payload["changed"] is True
    assert candidate.current_link.resolve(strict=True) == candidate.release_root


def test_quarantine_runtime_bytecode_moves_one_cache_and_preserves_receipt(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode_payload = b"runtime-bytecode-payload\n"
    bytecode = _write_runtime_bytecode(prior, payload=bytecode_payload)
    cache_directory = bytecode.parent
    receipt = prior.release_root / ".fin-frozen-sync.json"
    receipt_before = _file_snapshot(receipt)
    candidate_receipt = candidate.release_root / ".fin-frozen-sync.json"
    candidate_receipt_before = _file_snapshot(candidate_receipt)

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["schema_version"] == "fin.runtime-bytecode-quarantine/v2"
    assert result["status"] == "quarantined"
    assert result["ready"] is True
    assert result["changed"] is True
    assert result["current_unchanged"] is True
    assert result["frozen_sync_receipt_unchanged"] is True
    assert result["candidate_commit"] == candidate.commit
    assert result["prior_commit"] == prior.commit
    quarantine = result["quarantine"]
    assert re.fullmatch(r"[0-9a-f]{64}", quarantine["inventory_sha256"])
    assert quarantine["cache_directories"] == ["scripts/__pycache__"]
    assert quarantine["file_count"] == 1
    assert quarantine["total_bytes"] == len(bytecode_payload)
    [artifact] = quarantine["artifacts"]
    assert artifact["cache_directory"] == "scripts/__pycache__"
    assert artifact["file_count"] == 1
    assert artifact["total_bytes"] == len(bytecode_payload)
    expected_quarantine = candidate.data_root / (
        f".runtime-bytecode-quarantine-{prior.commit}-{quarantine['inventory_sha256']}-0000"
    )
    assert artifact["path"] == str(expected_quarantine)
    data_root_metadata = candidate.data_root.stat()
    assert expected_quarantine.parent == candidate.data_root
    assert data_root_metadata.st_uid == os.geteuid()
    assert stat.S_IMODE(data_root_metadata.st_mode) == 0o700
    assert not os.path.lexists(cache_directory)
    assert (expected_quarantine / bytecode.name).read_bytes() == bytecode_payload
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert _file_snapshot(receipt) == receipt_before
    assert _file_snapshot(candidate_receipt) == candidate_receipt_before


def test_quarantine_runtime_bytecode_publishes_flat_without_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    source_cache = bytecode.parent
    source_identity = source_cache.stat()
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"

    def reject_mkdir(
        _path: str | Path,
        _mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        del dir_fd
        raise AssertionError("runtime bytecode quarantine must not call mkdir")

    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    monkeypatch.setattr(release_tool.os, "mkdir", reject_mkdir)

    result = release_tool.quarantine_active_release_runtime_bytecode(
        candidate,
        expected_current_commit=prior.commit,
    )

    quarantine = result["quarantine"]
    expected_name = (
        f".runtime-bytecode-quarantine-{prior.commit}-{quarantine['inventory_sha256']}-0000"
    )
    [artifact] = quarantine["artifacts"]
    published = Path(artifact["path"])
    assert result["ready"] is True
    assert published == candidate.data_root / expected_name
    published_identity = published.stat()
    assert (published_identity.st_dev, published_identity.st_ino) == (
        source_identity.st_dev,
        source_identity.st_ino,
    )
    assert (published / bytecode.name).read_bytes() == b"test-runtime-bytecode\n"
    assert not source_cache.exists()


def test_quarantine_runtime_bytecode_uses_no_data_root_git_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    _write_runtime_bytecode(prior)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    data_root_metadata = candidate.data_root.stat()
    data_root_identity = data_root_metadata.st_dev, data_root_metadata.st_ino
    original_open = release_tool.os.open

    def reject_data_root_git_helper(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if (
            path == "git"
            and dir_fd is not None
            and (
                os.fstat(dir_fd).st_dev,
                os.fstat(dir_fd).st_ino,
            )
            == data_root_identity
        ):
            raise AssertionError("quarantine must not create a data-root Git helper")
        return original_open(
            path,
            flags,
            mode,
            dir_fd=dir_fd,
        )

    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    monkeypatch.setattr(release_tool.os, "open", reject_data_root_git_helper)

    result = release_tool.quarantine_active_release_runtime_bytecode(
        candidate,
        expected_current_commit=prior.commit,
    )

    assert result["ready"] is True
    assert not (candidate.data_root / "git").exists()
    assert not (candidate.data_root / "git-held-original").exists()


def test_quarantine_runtime_bytecode_makes_real_prior_checker_ready(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)

    before = _run_prior_release_check(candidate, prior)
    assert before.returncode != 0
    assert json.loads(before.stdout)["ready"] is False

    completed = _run_runtime_bytecode_quarantine(candidate, prior)
    assert completed.returncode == 0, completed.stderr

    after = _run_prior_release_check(candidate, prior)
    assert after.returncode == 0, after.stderr
    assert json.loads(after.stdout)["ready"] is True
    assert not bytecode.exists()


@pytest.mark.parametrize(
    ("source_relative", "expected_cache"),
    (
        ("fin_analyse/runtime_probe.py", "fin_analyse/__pycache__"),
        (
            "hermes-migration/plugins/fin-consultation-first-tool/runtime_plugin.py",
            "hermes-migration/plugins/fin-consultation-first-tool/__pycache__",
        ),
    ),
)
def test_quarantine_runtime_bytecode_accepts_each_other_allowed_root(
    tmp_path: Path,
    source_relative: str,
    expected_cache: str,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior, source_relative)

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["quarantine"]["cache_directories"] == [expected_cache]
    [artifact] = result["quarantine"]["artifacts"]
    assert artifact["cache_directory"] == expected_cache
    assert not bytecode.exists()
    assert (Path(artifact["path"]) / bytecode.name).read_bytes() == (b"test-runtime-bytecode\n")


def test_quarantine_runtime_bytecode_empty_inventory_is_idempotently_ready(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    receipt = prior.release_root / ".fin-frozen-sync.json"
    receipt_before = _file_snapshot(receipt)

    preflight = _run_runtime_bytecode_quarantine_preflight(candidate, prior)

    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout) == {
        "candidate_commit": candidate.commit,
        "current_unchanged": True,
        "frozen_sync_receipt_unchanged": True,
        "prior_commit": prior.commit,
        "quarantine": None,
        "ready": True,
        "schema_version": "fin.runtime-bytecode-quarantine-preflight/v1",
        "status": "already-ready",
        "would_change": False,
    }
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )
    assert _file_snapshot(receipt) == receipt_before

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "candidate_commit": candidate.commit,
        "changed": False,
        "current_unchanged": True,
        "frozen_sync_receipt_unchanged": True,
        "prior_commit": prior.commit,
        "quarantine": None,
        "ready": True,
        "schema_version": "fin.runtime-bytecode-quarantine/v2",
        "status": "already-ready",
    }
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )
    assert _file_snapshot(receipt) == receipt_before


def test_quarantine_runtime_bytecode_moves_multiple_caches_as_one_inventory(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    first = _write_runtime_bytecode(prior)
    second = _write_runtime_bytecode(prior, "fin_analyse/runtime_probe.py")
    first.parent.chmod(0o775)
    second.parent.chmod(0o775)

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["schema_version"] == "fin.runtime-bytecode-quarantine/v2"
    assert result["status"] == "quarantined"
    assert result["ready"] is True
    assert result["changed"] is True
    quarantine = result["quarantine"]
    assert quarantine["cache_directories"] == [
        "fin_analyse/__pycache__",
        "scripts/__pycache__",
    ]
    assert quarantine["file_count"] == 2
    artifacts = {
        artifact["cache_directory"]: Path(artifact["path"]) for artifact in quarantine["artifacts"]
    }
    assert set(artifacts) == set(quarantine["cache_directories"])
    assert (artifacts["scripts/__pycache__"] / first.name).read_bytes() == (
        b"test-runtime-bytecode\n"
    )
    assert (artifacts["fin_analyse/__pycache__"] / second.name).read_bytes() == (
        b"test-runtime-bytecode\n"
    )
    assert not first.exists()
    assert not second.exists()
    assert candidate.current_link.resolve(strict=True) == prior.release_root


def test_runtime_bytecode_quarantine_preflight_is_read_only_and_matches_apply(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    first = _write_runtime_bytecode(prior)
    second = _write_runtime_bytecode(prior, "fin_analyse/runtime_probe.py")
    first.parent.chmod(0o775)
    second.parent.chmod(0o775)
    receipt = prior.release_root / ".fin-frozen-sync.json"
    receipt_before = _file_snapshot(receipt)

    completed = _run_runtime_bytecode_quarantine_preflight(candidate, prior)

    assert completed.returncode == 0, completed.stderr
    preflight = json.loads(completed.stdout)
    assert preflight["schema_version"] == ("fin.runtime-bytecode-quarantine-preflight/v1")
    assert preflight["status"] == "ready-to-quarantine"
    assert preflight["ready"] is True
    assert preflight["would_change"] is True
    assert preflight["current_unchanged"] is True
    assert first.exists()
    assert second.exists()
    assert _file_snapshot(receipt) == receipt_before
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )

    applied = _run_runtime_bytecode_quarantine(candidate, prior)

    assert applied.returncode == 0, applied.stderr
    result = json.loads(applied.stdout)
    assert preflight["quarantine"] == result["quarantine"]


def test_runtime_bytecode_quarantine_accepts_frozen_v2_prior_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_frozen_v2_handoff_prior_pair(
        tmp_path,
        monkeypatch,
    )
    bytecode = _write_runtime_bytecode(prior)
    receipt = prior.release_root / ".fin-frozen-sync.json"
    receipt_before = _file_snapshot(receipt)
    prior_config = prior.release_root / "config"
    assert stat.S_IMODE(prior_config.stat().st_mode) == 0o775

    preflight = _run_runtime_bytecode_quarantine_preflight(candidate, prior)

    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout)["status"] == "ready-to-quarantine"
    assert bytecode.exists()
    assert _file_snapshot(receipt) == receipt_before

    applied = _run_runtime_bytecode_quarantine(candidate, prior)

    assert applied.returncode == 0, applied.stderr
    result = json.loads(applied.stdout)
    assert result["status"] == "quarantined"
    assert result["ready"] is True
    assert not bytecode.exists()
    assert _file_snapshot(receipt) == receipt_before
    assert _run_prior_release_check(candidate, prior).returncode == 0
    assert stat.S_IMODE(prior_config.stat().st_mode) == 0o775


def test_frozen_handoff_contract_accepts_only_exact_known_checker_sources() -> None:
    expected_lineage = tuple(
        (
            hashlib.sha256(_frozen_checker_source_at(commit).encode()).hexdigest(),
            contract,
        )
        for commit, contract in _FROZEN_CHECKER_LINEAGE
    )
    assert expected_lineage == release_tool._FROZEN_CHECKER_CONTRACTS_BY_SHA256

    for commit, expected_contract in _FROZEN_CHECKER_LINEAGE:
        source = _frozen_checker_source_at(commit)
        assert (
            release_tool._special_handoff_contract_from_checker_source(source.encode())
            is expected_contract
        )

    current = _frozen_checker_source_with_target_mode(
        config_mode_line=_FROZEN_CURRENT_CONFIG_MODE_LINE
    )
    with pytest.raises(
        ValueError,
        match="prior release checker handoff contract is invalid",
    ):
        release_tool._special_handoff_contract_from_checker_source(current.encode())

    three_target_contract = (
        ("scripts/consultation_runtime_canary_launcher.py", "file", 0o600),
        ("hermes-migration/plugins/fin-consultation-first-tool", "directory", 0o700),
        ("config", "directory", 0o755),
    )
    assert (
        release_tool._special_handoff_targets_for_contract(
            release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET
        )
        == three_target_contract
    )
    four_target_contract = (
        *three_target_contract,
        ("hermes-migration/cron/jobs.json", "file", 0o600),
    )
    assert (
        release_tool._special_handoff_targets_for_contract(
            release_tool._SpecialHandoffModeContract.CURRENT_V2_FOUR_TARGET
        )
        == four_target_contract
    )
    assert four_target_contract == release_tool._SPECIAL_HANDOFF_MODE_TARGETS


@pytest.mark.parametrize(
    "source",
    (
        pytest.param(
            (
                f"_SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                '_SPECIAL_HANDOFF_MODE_TARGETS += (("config", "directory", 0o755),)\n'
            ),
            id="augmented-assignment",
        ),
        pytest.param(
            (
                f"_SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "if True:\n"
                f"    _SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
            ),
            id="nested-rebinding",
        ),
        pytest.param(
            (
                "if True:\n"
                f"    _SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
            ),
            id="nested-only-binding",
        ),
        pytest.param(
            (
                f"_SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "def _SPECIAL_HANDOFF_MODE_TARGETS():\n"
                "    pass\n"
            ),
            id="named-definition",
        ),
        pytest.param(
            (
                f"_SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "import json as _SPECIAL_HANDOFF_MODE_TARGETS\n"
            ),
            id="import-binding",
        ),
        pytest.param(
            (
                f"_SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "try:\n"
                "    pass\n"
                "except Exception as _SPECIAL_HANDOFF_MODE_TARGETS:\n"
                "    pass\n"
            ),
            id="exception-binding",
        ),
        pytest.param(
            (
                f"_SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "match 1:\n"
                "    case _SPECIAL_HANDOFF_MODE_TARGETS:\n"
                "        pass\n"
            ),
            id="match-capture-binding",
        ),
        pytest.param(
            (
                f"_SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "globals()['_SPECIAL_HANDOFF_MODE_TARGETS'] = "
                f"{release_tool._SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
            ),
            id="globals-rebinding",
        ),
        pytest.param(
            (
                f"_SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "exec('_SPECIAL_HANDOFF_MODE_TARGETS = ()')\n"
            ),
            id="exec-rebinding",
        ),
        pytest.param(
            (
                "import sys\n"
                f"_SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "target = '_SPECIAL_HANDOFF_MODE_TARGETS'\n"
                f"current = {release_tool._SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "sys.modules[__name__].__setattr__(target, current)\n"
            ),
            id="module-setattr-rebinding",
        ),
        pytest.param(
            (
                "import operator\n"
                "import sys\n"
                f"_SPECIAL_HANDOFF_MODE_TARGETS = "
                f"{release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "target = '_SPECIAL_HANDOFF_MODE_TARGETS'\n"
                f"current = {release_tool._SPECIAL_HANDOFF_MODE_TARGETS!r}\n"
                "namespace = operator.attrgetter('__dict__')(sys.modules[__name__])\n"
                "operator.setitem(namespace, target, current)\n"
            ),
            id="operator-namespace-rebinding",
        ),
    ),
)
def test_frozen_handoff_contract_rejects_unknown_source_even_if_literal_looks_supported(
    source: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="prior release checker handoff contract is invalid",
    ):
        release_tool._special_handoff_contract_from_checker_source(source.encode())


def test_quarantine_compatibility_binds_all_facts_to_held_prior_release_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_frozen_v2_handoff_prior_pair(
        tmp_path,
        monkeypatch,
    )
    replacement = prior.release_root.parent / f".replacement-{prior.commit}"
    detached = prior.release_root.parent / f".detached-{prior.commit}"
    shutil.copytree(prior.release_root, replacement, symlinks=True)

    prior.release_root.rename(detached)
    replacement.rename(prior.release_root)
    try:
        record = _run_versioned_release_action(prior, "record-sync")
        assert record.returncode == 0, record.stderr
        prepared = _run_versioned_release_action(prior, "prepare")
        assert prepared.returncode == 0, prepared.stderr
        replacement_fd = release_tool._open_directory_fd(
            prior.release_root,
            owner_only=True,
        )
        try:
            replacement_checker = release_tool._release_file_snapshot(
                replacement_fd,
                release_tool._VERSIONED_CHECKER,
            )
            release_tool._require_runtime_bytecode_quarantine_receipt_compatible(
                prior,
                previous_fd=replacement_fd,
                expected_checker=replacement_checker,
            )
        finally:
            os.close(replacement_fd)
    finally:
        prior.release_root.rename(replacement)
        detached.rename(prior.release_root)

    (prior.release_root / "uv.lock").write_text(
        "version = 1\n# held-root drift\n",
        encoding="utf-8",
    )
    previous_fd = release_tool._open_directory_fd(
        prior.release_root,
        owner_only=True,
    )
    try:
        expected_checker = release_tool._release_file_snapshot(
            previous_fd,
            release_tool._VERSIONED_CHECKER,
        )
        prior.release_root.rename(detached)
        replacement.rename(prior.release_root)
        try:
            with pytest.raises(
                ValueError,
                match="prior frozen-sync receipt cannot be restored by source quarantine",
            ):
                release_tool._require_runtime_bytecode_quarantine_receipt_compatible(
                    prior,
                    previous_fd=previous_fd,
                    expected_checker=expected_checker,
                )
        finally:
            prior.release_root.rename(replacement)
            detached.rename(prior.release_root)
    finally:
        os.close(previous_fd)

    assert candidate.current_link.resolve(strict=True) == prior.release_root


@pytest.mark.parametrize(
    "drift_kind",
    (
        pytest.param("tracked", id="tracked-code"),
        pytest.param("binding", id="common-binding"),
    ),
)
def test_quarantine_preflight_does_not_splice_path_replacement_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_kind: str,
) -> None:
    candidate, prior = _seed_frozen_v2_handoff_prior_pair(
        tmp_path,
        monkeypatch,
    )
    bytecode = _write_runtime_bytecode(prior)
    replacement = prior.release_root.parent / f".replacement-{prior.commit}"
    detached = prior.release_root.parent / f".detached-{prior.commit}"
    shutil.copytree(prior.release_root, replacement, symlinks=True)
    if drift_kind == "tracked":
        (prior.release_root / "pyproject.toml").write_text(
            "[project]\nname = 'drifted-prior'\n",
            encoding="utf-8",
        )
        function_name = "_git_identity_status"
    else:
        binding = prior.release_root / ".env"
        binding.unlink()
        binding.symlink_to(prior.runtime_root, target_is_directory=True)
        function_name = "_release_boundary_status"

    original = getattr(release_tool, function_name)
    swap_count = 0

    def inspect_with_path_replacement(
        layout: ReleaseLayout,
        *args,
        **kwargs,
    ):
        nonlocal swap_count
        if layout.commit != prior.commit:
            return original(layout, *args, **kwargs)
        prior.release_root.rename(detached)
        replacement.rename(prior.release_root)
        swap_count += 1
        try:
            return original(layout, *args, **kwargs)
        finally:
            prior.release_root.rename(replacement)
            detached.rename(prior.release_root)

    monkeypatch.setattr(release_tool, function_name, inspect_with_path_replacement)
    monkeypatch.setattr(
        release_tool,
        "__file__",
        str(candidate.release_root / "scripts/prepare_fin_release.py"),
    )

    with pytest.raises((PermissionError, RuntimeError, ValueError)):
        release_tool.preflight_active_release_runtime_bytecode_quarantine(
            candidate,
            expected_current_commit=prior.commit,
        )

    assert swap_count > 0
    assert bytecode.exists()
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )


@pytest.mark.parametrize(
    "operation",
    (
        pytest.param("preflight", id="preflight"),
        pytest.param("apply", id="apply-rollback"),
    ),
)
def test_quarantine_rejects_versioned_checker_root_swap_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    candidate, prior = _seed_frozen_v2_handoff_prior_pair(
        tmp_path,
        monkeypatch,
    )
    replacement = prior.release_root.parent / f".checker-replacement-{prior.commit}"
    detached = prior.release_root.parent / f".checker-detached-{prior.commit}"
    shutil.copytree(prior.release_root, replacement, symlinks=True)
    prior.release_root.rename(detached)
    replacement.rename(prior.release_root)
    try:
        replacement_record = _run_versioned_release_action(prior, "record-sync")
        assert replacement_record.returncode == 0, replacement_record.stderr
        replacement_prepare = _run_versioned_release_action(prior, "prepare")
        assert replacement_prepare.returncode == 0, replacement_prepare.stderr
    finally:
        prior.release_root.rename(replacement)
        detached.rename(prior.release_root)
    replacement_inode = replacement.stat().st_ino
    bytecode = _write_runtime_bytecode(prior) if operation == "apply" else None
    prior_times = prior.release_root.stat()
    releases_times = prior.releases_root.stat()
    original_check = release_tool._run_versioned_previous_check
    swap_count = 0

    def check_with_root_swap(
        observed_candidate: ReleaseLayout,
        observed_prior: ReleaseLayout,
        *,
        previous_fd: int | None = None,
        expected_checker_sha256: str | None = None,
    ) -> dict[str, object]:
        nonlocal swap_count
        prior.release_root.rename(detached)
        replacement.rename(prior.release_root)
        swap_count += 1
        try:
            assert prior.release_root.stat().st_ino == replacement_inode
            payload = original_check(
                observed_candidate,
                observed_prior,
                previous_fd=previous_fd,
                expected_checker_sha256=expected_checker_sha256,
            )
            assert payload["ready"] is True
            return payload
        finally:
            prior.release_root.rename(replacement)
            detached.rename(prior.release_root)
            os.utime(
                prior.release_root,
                ns=(prior_times.st_atime_ns, prior_times.st_mtime_ns),
                follow_symlinks=False,
            )
            os.utime(
                prior.releases_root,
                ns=(releases_times.st_atime_ns, releases_times.st_mtime_ns),
                follow_symlinks=False,
            )

    monkeypatch.setattr(
        release_tool,
        "__file__",
        str(candidate.release_root / "scripts/prepare_fin_release.py"),
    )
    monkeypatch.setattr(
        release_tool,
        "_run_versioned_previous_check",
        check_with_root_swap,
    )

    with pytest.raises(
        RuntimeError,
        match="release identity changed during prior-release validation",
    ):
        if operation == "preflight":
            release_tool.preflight_active_release_runtime_bytecode_quarantine(
                candidate,
                expected_current_commit=prior.commit,
            )
        else:
            release_tool.quarantine_active_release_runtime_bytecode(
                candidate,
                expected_current_commit=prior.commit,
            )

    assert swap_count == 1
    assert prior.release_root.stat().st_mtime_ns == prior_times.st_mtime_ns
    assert prior.releases_root.stat().st_mtime_ns == releases_times.st_mtime_ns
    if bytecode is not None:
        assert bytecode.exists()
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )


def test_quarantine_compatibility_uses_one_held_venv_for_digest_and_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_frozen_v2_handoff_prior_pair(
        tmp_path,
        monkeypatch,
    )
    venv_root = prior.release_root / ".venv"
    replacement = prior.release_root.parent / f".replacement-venv-{prior.commit}"
    detached = prior.release_root.parent / f".detached-venv-{prior.commit}"
    shutil.copytree(venv_root, replacement, symlinks=True)
    interpreter = venv_root / "bin/python"
    interpreter.unlink()
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o777)
    receipt = prior.release_root / ".fin-frozen-sync.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["venv_sha256"] = release_tool._venv_digest(venv_root)
    receipt.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    previous_fd = release_tool._open_directory_fd(
        prior.release_root,
        owner_only=True,
    )
    expected_checker = release_tool._release_file_snapshot(
        previous_fd,
        release_tool._VERSIONED_CHECKER,
    )
    original_digest = release_tool._venv_tree_digest
    swapped = False

    def digest_then_swap(root: Path) -> str:
        nonlocal swapped
        digest = original_digest(root)
        if not swapped:
            venv_root.rename(detached)
            replacement.rename(venv_root)
            swapped = True
        return digest

    monkeypatch.setattr(release_tool, "_venv_tree_digest", digest_then_swap)
    try:
        with pytest.raises(
            ValueError,
            match="prior frozen-sync receipt cannot be restored by source quarantine",
        ):
            release_tool._require_runtime_bytecode_quarantine_receipt_compatible(
                prior,
                previous_fd=previous_fd,
                expected_checker=expected_checker,
            )
    finally:
        os.close(previous_fd)
        if swapped:
            venv_root.rename(replacement)
            detached.rename(venv_root)

    assert swapped is True
    assert candidate.current_link.resolve(strict=True) == prior.release_root


def test_runtime_bytecode_quarantine_rejects_receipt_selected_legacy_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_frozen_v2_handoff_prior_pair(
        tmp_path,
        monkeypatch,
        config_mode_line=_FROZEN_CURRENT_CONFIG_MODE_LINE,
    )
    bytecode = _write_runtime_bytecode(prior)
    receipt = prior.release_root / ".fin-frozen-sync.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["handoff_binding_sha256"] = release_tool._special_handoff_binding_sha256(
        prior,
        targets=release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS,
    )
    receipt.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    receipt_before = _file_snapshot(receipt)
    checker_sha256 = release_tool._sha256_file(prior.release_root / release_tool._VERSIONED_CHECKER)
    monkeypatch.setattr(
        release_tool,
        "_FROZEN_CHECKER_CONTRACTS_BY_SHA256",
        (
            *release_tool._FROZEN_CHECKER_CONTRACTS_BY_SHA256,
            (
                checker_sha256,
                release_tool._SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
            ),
        ),
    )
    monkeypatch.setattr(
        release_tool,
        "__file__",
        str(candidate.release_root / "scripts/prepare_fin_release.py"),
    )

    with pytest.raises(
        ValueError,
        match="prior frozen-sync receipt cannot be restored by source quarantine",
    ):
        release_tool.preflight_active_release_runtime_bytecode_quarantine(
            candidate,
            expected_current_commit=prior.commit,
        )
    assert bytecode.exists()
    assert _file_snapshot(receipt) == receipt_before
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )


def test_runtime_bytecode_quarantine_rejects_unknown_frozen_target_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_frozen_v2_handoff_prior_pair(
        tmp_path,
        monkeypatch,
        config_mode_line='    ("config", "directory", 0o750),',
    )
    bytecode = _write_runtime_bytecode(prior)
    receipt = prior.release_root / ".fin-frozen-sync.json"
    receipt_before = _file_snapshot(receipt)

    preflight = _run_runtime_bytecode_quarantine_preflight(candidate, prior)

    assert preflight.returncode != 0
    assert "prior frozen-sync receipt cannot be restored by source quarantine" in (preflight.stderr)
    assert bytecode.exists()
    assert _file_snapshot(receipt) == receipt_before
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )


@pytest.mark.parametrize(
    "remove_publication_identity",
    (
        pytest.param(False, id="publication-retained"),
        pytest.param(True, id="publication-removed"),
    ),
)
def test_runtime_bytecode_quarantine_rejects_handoff_receipt_with_removed_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remove_publication_identity: bool,
) -> None:
    candidate, prior = _seed_frozen_v2_handoff_prior_pair(
        tmp_path,
        monkeypatch,
    )
    bytecode = _write_runtime_bytecode(prior)
    receipt = prior.release_root / ".fin-frozen-sync.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert "publication_identity" in payload
    payload.pop("handoff_binding_sha256")
    if remove_publication_identity:
        payload.pop("publication_identity")
    receipt.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    receipt_before = _file_snapshot(receipt)

    preflight = _run_runtime_bytecode_quarantine_preflight(candidate, prior)

    assert preflight.returncode != 0
    assert "prior frozen-sync receipt cannot be restored by source quarantine" in (preflight.stderr)
    assert bytecode.exists()
    assert _file_snapshot(receipt) == receipt_before
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )


def test_candidate_readiness_never_accepts_historical_two_target_hash(
    tmp_path: Path,
) -> None:
    layout = _seed_release(tmp_path)
    receipt = layout.release_root / ".fin-frozen-sync.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["handoff_binding_sha256"] = release_tool._special_handoff_binding_sha256(
        layout,
        targets=release_tool._LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS,
    )
    receipt.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    assert release_tool._sync_receipt_valid(layout) is False
    assert release_tool.inspect_candidate_release(layout)["ready"] is False


def test_runtime_bytecode_quarantine_preflight_rejects_stale_prior_venv(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    venv_drift = prior.release_root / ".venv/runtime-created.cpython-313.pyc"
    venv_drift.write_bytes(b"runtime venv bytecode\n")
    venv_drift.chmod(0o600)

    completed = _run_runtime_bytecode_quarantine_preflight(candidate, prior)

    assert completed.returncode != 0
    assert "prior frozen-sync receipt cannot be restored by source quarantine" in (completed.stderr)
    assert bytecode.exists()
    assert venv_drift.exists()
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )


def test_runtime_bytecode_quarantine_preflight_does_not_execute_prior_interpreter(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    sentinel = tmp_path / "quarantine-prior-python-ran"
    interpreter = prior.release_root / ".venv/bin/python"
    interpreter.unlink()
    interpreter.write_text(
        f"#!/bin/sh\nprintf ran > {sentinel}\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    receipt_path = prior.release_root / ".fin-frozen-sync.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["venv_sha256"] = release_tool._venv_digest(prior.release_root / ".venv")
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    completed = _run_runtime_bytecode_quarantine_preflight(candidate, prior)

    assert completed.returncode != 0
    assert "prior frozen-sync receipt cannot be restored by source quarantine" in (completed.stderr)
    assert not sentinel.exists()
    assert bytecode.exists()
    assert candidate.current_link.resolve(strict=True) == prior.release_root


def test_degraded_current_cutover_rejects_combined_source_drift_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    source_bytecode = _write_runtime_bytecode(prior)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    preflight = release_tool.preflight_degraded_current_cutover(
        candidate,
        degraded_prior_commit=prior.commit,
    )
    pointer_before = os.readlink(candidate.current_link)
    source_bytecode.write_bytes(b"drifted runtime bytecode\n")

    with pytest.raises(RuntimeError, match="cutover plan changed"):
        release_tool.activate_degraded_current_cutover(
            candidate,
            degraded_prior_commit=prior.commit,
            expected_cutover_sha256=preflight["cutover_sha256"],
            expected_prior_current_pointer_sha256=(preflight["prior_current_pointer_sha256"]),
        )

    assert os.readlink(candidate.current_link) == pointer_before
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert source_bytecode.read_bytes() == b"drifted runtime bytecode\n"


def test_degraded_current_cutover_rejects_same_target_pointer_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    preflight = release_tool.preflight_degraded_current_cutover(
        candidate,
        degraded_prior_commit=prior.commit,
    )
    replacement = candidate.current_link.parent / ".current-same-target-replacement"
    replacement.symlink_to(prior.release_root.relative_to(candidate.current_link.parent))
    os.replace(replacement, candidate.current_link)

    with pytest.raises(RuntimeError, match="pointer changed after preflight"):
        release_tool.activate_degraded_current_cutover(
            candidate,
            degraded_prior_commit=prior.commit,
            expected_cutover_sha256=preflight["cutover_sha256"],
            expected_prior_current_pointer_sha256=(preflight["prior_current_pointer_sha256"]),
        )

    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert bytecode.exists()
    assert not os.path.lexists(candidate.current_link.parent / f".current.tmp-{os.getpid()}")


def test_degraded_current_cutover_does_not_overwrite_pointer_exchange_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    preflight = release_tool.preflight_degraded_current_cutover(
        candidate,
        degraded_prior_commit=prior.commit,
    )
    intruder = ReleaseLayout(home=candidate.home, commit="c" * 40)
    intruder.release_root.mkdir(mode=0o700)
    original_exchange = release_tool._rename_exchange
    exchange_calls = 0

    def replace_current_before_first_exchange(*args, **kwargs):
        nonlocal exchange_calls
        if exchange_calls == 0:
            replacement = candidate.current_link.parent / ".current-race-replacement"
            replacement.symlink_to(intruder.release_root.relative_to(candidate.current_link.parent))
            os.replace(replacement, candidate.current_link)
        exchange_calls += 1
        return original_exchange(*args, **kwargs)

    monkeypatch.setattr(
        release_tool,
        "_rename_exchange",
        replace_current_before_first_exchange,
    )

    with pytest.raises(RuntimeError, match="current pointer exchange"):
        release_tool.activate_degraded_current_cutover(
            candidate,
            degraded_prior_commit=prior.commit,
            expected_cutover_sha256=preflight["cutover_sha256"],
            expected_prior_current_pointer_sha256=(preflight["prior_current_pointer_sha256"]),
        )

    assert exchange_calls == 2
    assert candidate.current_link.resolve(strict=True) == intruder.release_root
    assert bytecode.exists()
    assert not os.path.lexists(candidate.current_link.parent / f".current.tmp-{os.getpid()}")


def test_degraded_current_cutover_rejects_wrong_digest_without_pointer_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    preflight = release_tool.preflight_degraded_current_cutover(
        candidate,
        degraded_prior_commit=prior.commit,
    )
    pointer_before = os.readlink(candidate.current_link)

    with pytest.raises(RuntimeError, match="cutover plan changed"):
        release_tool.activate_degraded_current_cutover(
            candidate,
            degraded_prior_commit=prior.commit,
            expected_cutover_sha256="0" * 64,
            expected_prior_current_pointer_sha256=(preflight["prior_current_pointer_sha256"]),
        )

    assert os.readlink(candidate.current_link) == pointer_before
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert bytecode.exists()


def test_degraded_current_cutover_rejects_prior_interpreter_drift_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    sentinel = tmp_path / "degraded-prior-python-ran"
    interpreter = prior.release_root / ".venv/bin/python"
    interpreter.unlink()
    interpreter.write_text(
        f"#!/bin/sh\nprintf ran > {sentinel}\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))

    with pytest.raises(RuntimeError, match="degraded prior Python identity"):
        release_tool.preflight_degraded_current_cutover(
            candidate,
            degraded_prior_commit=prior.commit,
        )

    assert not sentinel.exists()
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert bytecode.exists()


def test_degraded_current_cutover_auto_rolls_back_failed_post_swap_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    preflight = release_tool.preflight_degraded_current_cutover(
        candidate,
        degraded_prior_commit=prior.commit,
    )
    original_plan = release_tool._degraded_current_cutover_plan
    plan_calls = 0

    def drift_after_pointer_swap(*args, **kwargs):
        nonlocal plan_calls
        authority, projection = original_plan(*args, **kwargs)
        plan_calls += 1
        if plan_calls == 2:
            authority = {**authority, "post_swap_drift": True}
        return authority, projection

    monkeypatch.setattr(
        release_tool,
        "_degraded_current_cutover_plan",
        drift_after_pointer_swap,
    )

    with pytest.raises(RuntimeError, match="cutover plan changed"):
        release_tool.activate_degraded_current_cutover(
            candidate,
            degraded_prior_commit=prior.commit,
            expected_cutover_sha256=preflight["cutover_sha256"],
            expected_prior_current_pointer_sha256=(preflight["prior_current_pointer_sha256"]),
        )

    assert plan_calls == 2
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert bytecode.exists()
    assert not os.path.lexists(candidate.current_link.parent / f".current.tmp-{os.getpid()}")


def test_degraded_current_cutover_cli_round_trip(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    receipt = prior.release_root / ".fin-frozen-sync.json"
    receipt_before = _file_snapshot(receipt)

    preflight_completed = _run_degraded_current_cutover(
        candidate,
        prior,
        "preflight-degraded-current-cutover",
    )

    assert preflight_completed.returncode == 0, preflight_completed.stderr
    preflight = json.loads(preflight_completed.stdout)
    activated_completed = _run_degraded_current_cutover(
        candidate,
        prior,
        "activate-degraded-current-cutover",
        cutover_sha256=preflight["cutover_sha256"],
        prior_current_pointer_sha256=preflight["prior_current_pointer_sha256"],
    )
    assert activated_completed.returncode == 0, activated_completed.stderr
    activated = json.loads(activated_completed.stdout)
    assert activated["active"] is True
    assert candidate.current_link.resolve(strict=True) == candidate.release_root

    rollback_completed = _run_degraded_current_cutover(
        candidate,
        prior,
        "rollback-degraded-current-cutover",
        cutover_sha256=preflight["cutover_sha256"],
        prior_current_pointer_sha256=preflight["prior_current_pointer_sha256"],
    )

    assert rollback_completed.returncode == 0, rollback_completed.stderr
    rolled_back = json.loads(rollback_completed.stdout)
    assert rolled_back["active_prior"] is True
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert bytecode.exists()
    assert _file_snapshot(receipt) == receipt_before


def test_degraded_current_cutover_cli_accepts_group_writable_confined_bytecode(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    bytecode.chmod(0o664)

    assert stat.S_IMODE(candidate.release_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(prior.release_root.stat().st_mode) == 0o700

    preflight_completed = _run_degraded_current_cutover(
        candidate,
        prior,
        "preflight-degraded-current-cutover",
    )

    assert preflight_completed.returncode == 0, preflight_completed.stderr
    preflight = json.loads(preflight_completed.stdout)
    activated_completed = _run_degraded_current_cutover(
        candidate,
        prior,
        "activate-degraded-current-cutover",
        cutover_sha256=preflight["cutover_sha256"],
        prior_current_pointer_sha256=preflight["prior_current_pointer_sha256"],
    )

    assert activated_completed.returncode == 0, activated_completed.stderr
    assert json.loads(activated_completed.stdout)["active"] is True
    assert candidate.current_link.resolve(strict=True) == candidate.release_root
    assert stat.S_IMODE(bytecode.stat().st_mode) == 0o664


@pytest.mark.parametrize(
    "mode",
    (0o654, 0o645, 0o646, 0o4644, 0o2644, 0o1644),
    ids=(
        "group-executable",
        "other-executable",
        "other-writable",
        "setuid",
        "setgid",
        "sticky",
    ),
)
@pytest.mark.parametrize(
    "action",
    ("quarantine-preflight", "degraded-cutover-preflight"),
)
def test_runtime_bytecode_cli_preflights_reject_unsafe_file_modes(
    tmp_path: Path,
    mode: int,
    action: str,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    bytecode.chmod(mode)
    bytecode_before = _file_snapshot(bytecode)
    pointer_before = os.readlink(candidate.current_link)

    completed = (
        _run_runtime_bytecode_quarantine_preflight(candidate, prior)
        if action == "quarantine-preflight"
        else _run_degraded_current_cutover(
            candidate,
            prior,
            "preflight-degraded-current-cutover",
        )
    )

    assert completed.returncode != 0
    assert "runtime bytecode entries have an unsafe mode" in completed.stderr
    assert _file_snapshot(bytecode) == bytecode_before
    assert os.readlink(candidate.current_link) == pointer_before
    assert candidate.current_link.resolve(strict=True) == prior.release_root


@pytest.mark.parametrize("release_role", ("candidate", "prior"))
@pytest.mark.parametrize("stage", ("preflight", "activation"))
def test_degraded_current_cutover_cli_rejects_non_0700_release_root(
    tmp_path: Path,
    release_role: str,
    stage: str,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    bytecode_before = _file_snapshot(bytecode)
    pointer_before = os.readlink(candidate.current_link)
    preflight: dict[str, object] | None = None
    if stage == "activation":
        preflight_completed = _run_degraded_current_cutover(
            candidate,
            prior,
            "preflight-degraded-current-cutover",
        )
        assert preflight_completed.returncode == 0, preflight_completed.stderr
        preflight = json.loads(preflight_completed.stdout)

    release = candidate if release_role == "candidate" else prior
    release.release_root.chmod(0o755)
    completed = (
        _run_degraded_current_cutover(
            candidate,
            prior,
            "preflight-degraded-current-cutover",
        )
        if preflight is None
        else _run_degraded_current_cutover(
            candidate,
            prior,
            "activate-degraded-current-cutover",
            cutover_sha256=str(preflight["cutover_sha256"]),
            prior_current_pointer_sha256=str(preflight["prior_current_pointer_sha256"]),
        )
    )

    assert completed.returncode != 0
    assert _file_snapshot(bytecode) == bytecode_before
    assert os.readlink(candidate.current_link) == pointer_before
    assert candidate.current_link.resolve(strict=True) == prior.release_root


def test_quarantine_runtime_bytecode_rolls_back_when_prior_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    second_bytecode = _write_runtime_bytecode(
        prior,
        "fin_analyse/runtime_probe.py",
    )
    cache_directory = bytecode.parent
    second_cache_directory = second_bytecode.parent
    receipt = prior.release_root / ".fin-frozen-sync.json"
    receipt_before = _file_snapshot(receipt)

    def reject_versioned_check(*args, **kwargs):
        raise RuntimeError("prior release versioned check failed")

    monkeypatch.setattr(
        release_tool,
        "__file__",
        str(candidate.release_root / "scripts/prepare_fin_release.py"),
    )
    monkeypatch.setattr(
        release_tool,
        "_run_versioned_previous_check",
        reject_versioned_check,
    )

    with pytest.raises(RuntimeError, match="prior release versioned check failed"):
        release_tool.quarantine_active_release_runtime_bytecode(
            candidate,
            expected_current_commit=prior.commit,
        )
    assert bytecode.exists()
    assert second_bytecode.exists()
    assert cache_directory.is_dir()
    assert second_cache_directory.is_dir()
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )
    assert candidate.current_link.resolve(strict=True) == prior.release_root
    assert _file_snapshot(receipt) == receipt_before


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "error_match"),
    (
        ("_RUNTIME_BYTECODE_MAX_FILES", 1, "file-count limit"),
        ("_RUNTIME_BYTECODE_MAX_BYTES", 3, "byte limit"),
    ),
)
def test_quarantine_runtime_bytecode_enforces_aggregate_multi_cache_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int,
    error_match: str,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    first = _write_runtime_bytecode(prior, payload=b"aa")
    second = _write_runtime_bytecode(
        prior,
        "fin_analyse/runtime_probe.py",
        payload=b"bb",
    )
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"

    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    monkeypatch.setattr(release_tool, limit_name, limit_value)

    with pytest.raises(ValueError, match=error_match):
        release_tool.quarantine_active_release_runtime_bytecode(
            candidate,
            expected_current_commit=prior.commit,
        )

    assert first.read_bytes() == b"aa"
    assert second.read_bytes() == b"bb"
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )


def test_quarantine_runtime_bytecode_rolls_back_first_cache_when_second_move_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    first = _write_runtime_bytecode(prior)
    second = _write_runtime_bytecode(prior, "fin_analyse/runtime_probe.py")
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    data_root_metadata = candidate.data_root.stat()
    data_root_identity = data_root_metadata.st_dev, data_root_metadata.st_ino
    original_rename = release_tool._rename_noreplace
    publish_calls = 0

    def fail_second_publish(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal publish_calls
        destination_identity = (
            os.fstat(destination_parent_fd).st_dev,
            os.fstat(destination_parent_fd).st_ino,
        )
        if source_name == "__pycache__" and destination_identity == data_root_identity:
            publish_calls += 1
            if publish_calls == 2:
                raise OSError(errno.EIO, "second publish failed")
        original_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    monkeypatch.setattr(release_tool, "_rename_noreplace", fail_second_publish)

    with pytest.raises(OSError, match="second publish failed"):
        release_tool.quarantine_active_release_runtime_bytecode(
            candidate,
            expected_current_commit=prior.commit,
        )

    assert first.read_bytes() == b"test-runtime-bytecode\n"
    assert second.read_bytes() == b"test-runtime-bytecode\n"
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )


@pytest.mark.parametrize("drift_kind", ("tracked", "ignored", "empty_cache"))
def test_quarantine_runtime_bytecode_rolls_back_if_prior_drifts_after_successful_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_kind: str,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    original_validate = release_tool._validate_versioned_previous_release

    def validate_then_drift(
        observed_candidate: ReleaseLayout,
        observed_prior: ReleaseLayout,
        *,
        previous_fd: int | None = None,
        releases_fd: int | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        result = original_validate(
            observed_candidate,
            observed_prior,
            previous_fd=previous_fd,
            releases_fd=releases_fd,
        )
        if drift_kind == "tracked":
            (prior.release_root / "fin_analyse/runtime_probe.py").write_text(
                "# drifted after successful prior check\n",
                encoding="utf-8",
            )
        else:
            cache = prior.release_root / "fin_analyse/__pycache__"
            cache.mkdir()
            cache.chmod(0o700)
            if drift_kind == "ignored":
                late_bytecode = cache / "runtime_probe.cpython-313.pyc"
                late_bytecode.write_bytes(b"late ignored drift\n")
                late_bytecode.chmod(0o644)
        return result

    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    monkeypatch.setattr(
        release_tool,
        "_validate_versioned_previous_release",
        validate_then_drift,
    )

    with pytest.raises((RuntimeError, ValueError)):
        release_tool.quarantine_active_release_runtime_bytecode(
            candidate,
            expected_current_commit=prior.commit,
        )

    assert bytecode.exists()
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )


def test_quarantine_runtime_bytecode_rejects_wrong_operator_source(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)

    with pytest.raises(PermissionError, match="candidate operator"):
        release_tool.quarantine_active_release_runtime_bytecode(
            candidate,
            expected_current_commit=prior.commit,
        )

    assert bytecode.exists()
    assert candidate.current_link.resolve(strict=True) == prior.release_root


@pytest.mark.parametrize(
    "action",
    (
        "preflight-runtime-bytecode-quarantine",
        "quarantine-runtime-bytecode",
    ),
)
def test_quarantine_runtime_bytecode_cli_requires_expected_current(
    tmp_path: Path,
    action: str,
) -> None:
    candidate = _seed_release(tmp_path)

    with pytest.raises(SystemExit):
        main(
            (
                action,
                "--home",
                str(candidate.home),
                "--commit",
                candidate.commit,
            )
        )


def test_quarantine_runtime_bytecode_rejects_non_pyc_entry(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    cache_directory = prior.release_root / "scripts/__pycache__"
    cache_directory.mkdir()
    cache_directory.chmod(0o700)
    unexpected = cache_directory / "notes.txt"
    unexpected.write_text("not bytecode\n", encoding="utf-8")

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert unexpected.exists()


def test_quarantine_runtime_bytecode_rejects_symlink_entry(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    source = prior.release_root / "scripts/prepare_fin_release.py"
    bytecode = Path(importlib.util.cache_from_source(str(source)))
    bytecode.parent.mkdir()
    bytecode.parent.chmod(0o700)
    bytecode.symlink_to(source)

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert bytecode.is_symlink()


def test_quarantine_runtime_bytecode_rejects_hardlinked_entry(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    alias = tmp_path / "bytecode-hardlink"
    os.link(bytecode, alias)

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert bytecode.exists()
    assert alias.exists()


def test_quarantine_runtime_bytecode_rejects_executable_entry(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    bytecode.chmod(0o744)

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert bytecode.exists()


@pytest.mark.parametrize("mode", (0o702, 0o1700))
def test_quarantine_runtime_bytecode_rejects_unsafe_cache_directory_mode(
    tmp_path: Path,
    mode: int,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    bytecode.parent.chmod(mode)

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert "without special or other-write mode" in completed.stderr
    assert bytecode.exists()


def test_quarantine_runtime_bytecode_rejects_more_than_256_files(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    cache_directory = prior.release_root / "scripts/__pycache__"
    cache_directory.mkdir()
    cache_directory.chmod(0o700)
    for index in range(257):
        (cache_directory / f"runtime_probe_{index}.cpython-313.pyc").write_bytes(b"bytecode\n")

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert "file-count limit" in completed.stderr
    assert len(tuple(cache_directory.iterdir())) == 257


def test_quarantine_runtime_bytecode_rejects_more_than_64_mib(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    with bytecode.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert "byte limit" in completed.stderr
    assert bytecode.stat().st_size == 64 * 1024 * 1024 + 1


def test_quarantine_runtime_bytecode_rejects_non_owner_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    original_stat = os.stat

    def stat_with_wrong_owner(
        path: str | bytes | int | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        metadata = original_stat(
            path,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if isinstance(path, str) and path == bytecode.name and dir_fd is not None:
            fields = list(metadata)
            fields[4] = metadata.st_uid + 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    monkeypatch.setattr(release_tool.os, "stat", stat_with_wrong_owner)

    with pytest.raises(PermissionError, match="owner-owned"):
        release_tool.quarantine_active_release_runtime_bytecode(
            candidate,
            expected_current_commit=prior.commit,
        )

    assert bytecode.exists()


def test_quarantine_runtime_bytecode_rejects_special_file(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    source = prior.release_root / "scripts/prepare_fin_release.py"
    bytecode = Path(importlib.util.cache_from_source(str(source)))
    bytecode.parent.mkdir()
    bytecode.parent.chmod(0o700)
    os.mkfifo(bytecode)

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert stat.S_ISFIFO(bytecode.lstat().st_mode)


def test_quarantine_runtime_bytecode_rejects_cache_subdirectory(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    nested = prior.release_root / "scripts/__pycache__/nested"
    nested.mkdir(parents=True)
    bytecode = nested / "prepare_fin_release.cpython-313.pyc"
    bytecode.write_bytes(b"nested\n")

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert bytecode.exists()


def test_quarantine_runtime_bytecode_rejects_untracked_source_mapping(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    source = prior.release_root / "scripts/not_tracked.py"
    bytecode = Path(importlib.util.cache_from_source(str(source)))
    bytecode.parent.mkdir()
    bytecode.parent.chmod(0o700)
    bytecode.write_bytes(b"unbound\n")

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert bytecode.exists()


def test_quarantine_runtime_bytecode_rejects_cache_outside_allowed_roots(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior, "tools/runtime_probe.py")

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert bytecode.exists()


def test_quarantine_runtime_bytecode_rejects_additional_ignored_path(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    info_exclude = prior.release_root / ".git/info/exclude"
    with info_exclude.open("a", encoding="utf-8") as stream:
        stream.write("runtime-junk\n")
    ignored = prior.release_root / "runtime-junk"
    ignored.write_text("unexpected ignored path\n", encoding="utf-8")

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert bytecode.exists()
    assert ignored.exists()


def test_quarantine_runtime_bytecode_rejects_additional_untracked_path(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    untracked = prior.release_root / "runtime-junk"
    untracked.write_text("unexpected untracked path\n", encoding="utf-8")

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert bytecode.exists()
    assert untracked.exists()


def test_quarantine_runtime_bytecode_rejects_tracked_drift(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    tracked = prior.release_root / "fin_analyse/runtime_probe.py"
    tracked.write_text("# drifted tracked source\n", encoding="utf-8")

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert bytecode.exists()


def test_quarantine_runtime_bytecode_rejects_current_commit_mismatch(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    wrong_prior = ReleaseLayout(home=candidate.home, commit="f" * 40)

    completed = _run_runtime_bytecode_quarantine(candidate, wrong_prior)

    assert completed.returncode != 0
    assert bytecode.exists()
    assert candidate.current_link.resolve(strict=True) == prior.release_root


def test_quarantine_runtime_bytecode_rejects_current_pointer_cas_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"
    original_boundary = release_tool._require_runtime_bytecode_prior_boundary
    calls = 0

    def boundary(
        observed_candidate: ReleaseLayout,
        observed_prior: ReleaseLayout,
        *,
        previous_fd: int,
    ) -> release_tool._RuntimeBytecodeInventory | None:
        nonlocal calls
        result = original_boundary(
            observed_candidate,
            observed_prior,
            previous_fd=previous_fd,
        )
        calls += 1
        if calls == 2:
            replacement = candidate.data_root / ".current-test-replacement"
            replacement.symlink_to(
                candidate.release_root.relative_to(candidate.current_link.parent)
            )
            os.replace(replacement, candidate.current_link)
        return result

    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    monkeypatch.setattr(
        release_tool,
        "_require_runtime_bytecode_prior_boundary",
        boundary,
    )

    with pytest.raises(RuntimeError, match="current release pointer changed"):
        release_tool.quarantine_active_release_runtime_bytecode(
            candidate,
            expected_current_commit=prior.commit,
        )

    assert bytecode.exists()
    assert candidate.current_link.resolve(strict=True) == candidate.release_root


def test_quarantine_runtime_bytecode_rejects_candidate_not_ready(tmp_path: Path) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    (candidate.release_root / "pyproject.toml").write_text(
        "[project]\nname = 'drifted-candidate'\n",
        encoding="utf-8",
    )

    completed = _run_runtime_bytecode_quarantine(candidate, prior)

    assert completed.returncode != 0
    assert bytecode.exists()


def test_quarantine_runtime_bytecode_rejects_cross_device_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    candidate_checker = candidate.release_root / "scripts/prepare_fin_release.py"

    def reject_cross_device(_source_fd: int, _destination_fd: int) -> None:
        raise OSError(errno.EXDEV, "cross-device test")

    monkeypatch.setattr(release_tool, "__file__", str(candidate_checker))
    monkeypatch.setattr(release_tool, "_require_same_device", reject_cross_device)

    with pytest.raises(OSError, match="cross-device"):
        release_tool.quarantine_active_release_runtime_bytecode(
            candidate,
            expected_current_commit=prior.commit,
        )

    assert bytecode.exists()
    assert not any(
        path.name.startswith(".runtime-bytecode-quarantine-")
        for path in candidate.data_root.iterdir()
    )


def test_quarantine_runtime_bytecode_rejects_existing_quarantine(
    tmp_path: Path,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    cache_directory = bytecode.parent
    first = _run_runtime_bytecode_quarantine(candidate, prior)
    assert first.returncode == 0, first.stderr
    first_result = json.loads(first.stdout)
    [artifact] = first_result["quarantine"]["artifacts"]
    published = Path(artifact["path"])
    os.rename(published, cache_directory)
    published.mkdir(mode=0o700)
    published_identity = published.stat()

    second = _run_runtime_bytecode_quarantine(candidate, prior)

    assert second.returncode != 0
    assert bytecode.exists()
    assert (published.stat().st_dev, published.stat().st_ino) == (
        published_identity.st_dev,
        published_identity.st_ino,
    )
    assert not tuple(published.iterdir())


def test_quarantine_runtime_bytecode_rollback_conflict_never_clobbers_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    bytecode = _write_runtime_bytecode(prior)
    cache_directory = bytecode.parent

    def create_conflict_then_reject(*args, **kwargs):
        cache_directory.mkdir()
        (cache_directory / "conflict.cpython-313.pyc").write_bytes(b"conflict\n")
        raise RuntimeError("prior release versioned check failed")

    monkeypatch.setattr(
        release_tool,
        "__file__",
        str(candidate.release_root / "scripts/prepare_fin_release.py"),
    )
    monkeypatch.setattr(
        release_tool,
        "_run_versioned_previous_check",
        create_conflict_then_reject,
    )

    with pytest.raises(
        RuntimeError,
        match="runtime bytecode quarantine failed and no-clobber rollback failed",
    ):
        release_tool.quarantine_active_release_runtime_bytecode(
            candidate,
            expected_current_commit=prior.commit,
        )
    conflict = cache_directory / "conflict.cpython-313.pyc"
    assert conflict.read_bytes() == b"conflict\n"
    assert not bytecode.exists()
    published = tuple(
        path
        for path in candidate.data_root.iterdir()
        if re.fullmatch(
            r"\.runtime-bytecode-quarantine-[0-9a-f]{40}-[0-9a-f]{64}-[0-9]{4}",
            path.name,
        )
    )
    assert len(published) == 1
    assert (published[0] / bytecode.name).read_bytes() == b"test-runtime-bytecode\n"


@pytest.mark.parametrize(
    ("argument", "value"),
    (
        ("--source", "/tmp/source"),
        ("--quarantine", "/tmp/quarantine"),
        ("--path", "/tmp/path"),
        ("--force", None),
        ("--overwrite", None),
    ),
)
def test_quarantine_runtime_bytecode_cli_rejects_path_and_override_arguments(
    tmp_path: Path,
    argument: str,
    value: str | None,
) -> None:
    candidate, prior = _seed_runtime_bytecode_quarantine_pair(tmp_path)
    arguments = [
        "quarantine-runtime-bytecode",
        "--home",
        str(candidate.home),
        "--commit",
        candidate.commit,
        "--expected-current-commit",
        prior.commit,
        argument,
    ]
    if value is not None:
        arguments.append(value)

    with pytest.raises(SystemExit):
        main(tuple(arguments))


def test_runtime_bytecode_runbook_uses_candidate_mediated_prior_check() -> None:
    runbook = (
        Path(__file__).resolve().parents[2] / "docs/runbooks/hermes-official-runtime-upgrade.md"
    ).read_text(encoding="utf-8")
    quarantine_section = runbook.split(
        "### 6.1 只恢复有界 runtime bytecode 污染",
        maxsplit=1,
    )[1]
    degraded_section = runbook.split(
        "### 6.2 从已知可运行但 checker degraded 的 current 无损切换",
        maxsplit=1,
    )[1]

    assert '"$PRIOR/scripts/prepare_fin_release.py" check' not in runbook
    assert "candidate bootstrap-mediated prior check" in runbook
    assert "fin.runtime-bytecode-quarantine-preflight/v1" in runbook
    assert "fin.runtime-bytecode-quarantine/v2" in runbook
    assert ".quarantine.cache_directories" in runbook
    assert ".quarantine.artifacts" in runbook
    assert "全部 cache 合计最多 256 个文件、64 MiB" in runbook
    assert "prior frozen-sync receipt cannot be restored by source quarantine" in runbook
    assert "systemctl --user reset-failed hermes-gateway-fin.service" in runbook
    preflight_index = quarantine_section.index("\n    preflight-runtime-bytecode-quarantine \\")
    stop_index = quarantine_section.index("systemctl --user stop hermes-gateway-fin.service")
    apply_index = quarantine_section.index(
        "\n    quarantine-runtime-bytecode \\",
        stop_index,
    )
    assert preflight_index < stop_index < apply_index
    degraded_preflight_index = degraded_section.index("\n    preflight-degraded-current-cutover \\")
    degraded_stop_index = degraded_section.index("systemctl --user stop hermes-gateway-fin.service")
    degraded_activate_index = degraded_section.index("\n    activate-degraded-current-cutover \\")
    degraded_start_index = degraded_section.index(
        "systemctl --user start hermes-gateway-fin.service"
    )
    assert (
        degraded_preflight_index
        < degraded_stop_index
        < degraded_activate_index
        < degraded_start_index
    )
    assert "fin.degraded-current-cutover-preflight/v1" in degraded_section
    assert "fin.degraded-current-cutover/v1" in degraded_section
    assert "rollback-degraded-current-cutover" in degraded_section
    assert "fin.degraded-current-cutover-rollback/v1" in degraded_section
    assert "75915b778795c64dc334588b947ed876690349b2" in degraded_section
    assert "f677745263ff363ae7d31c9f5f9d15c68164d539ff900ada5d1368cbcc4b02ab" in (
        degraded_section
    )
    assert "d7944eb96581e10c018ae70cd7f8511db815c65a15ee602880c8357a74380080" in (
        degraded_section
    )
    assert "5b935140e64243c72a927d17759e13a775b434d8" in degraded_section
    assert degraded_section.count("--expected-prior-current-pointer-sha256") == 2
    rollback_section = degraded_section.split(
        'if test "$CUTOVER_ACCEPTED" != true; then',
        maxsplit=1,
    )[1]
    rollback_stop_index = rollback_section.index("systemctl --user stop hermes-gateway-fin.service")
    rollback_pid_index = rollback_section.index("--property MainPID --value")
    rollback_inactive_index = rollback_section.index('test "$ROLLBACK_UNIT_STATE" = inactive')
    rollback_pointer_index = rollback_section.index(
        'test "$(readlink -f "$FIN_DATA/current")" = "$CANDIDATE"'
    )
    rollback_action_index = rollback_section.index("\n      rollback-degraded-current-cutover \\")
    assert (
        rollback_stop_index
        < rollback_pid_index
        < rollback_inactive_index
        < rollback_pointer_index
        < rollback_action_index
    )
