#!/usr/bin/env python3
"""Validate, bind and atomically activate one commit-bound FIN release.

This module deliberately does not create Git worktrees, copy production data,
run ``uv sync`` or touch services.  Those operations remain explicit cutover
steps; this module owns only the release filesystem contract.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import resource
import signal
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_FULL_SHA256 = re.compile(r"[0-9a-f]{64}")
_SYSTEM_GIT = "/usr/bin/git"
_GIT_TIMEOUT_SECONDS = 10.0
_MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
_CRITICAL_RUNTIME_FILES = (
    "fin_analyse/consultation/runtime_budget.py",
    "fin_analyse/consultation/runtime_budget.v1.json",
    "fin_analyse/runtime/hermes_managed_assets.py",
    "fin_analyse/runtime/hermes_managed_assets.v1.json",
    "fin_analyse/consultation/presentation.py",
    "fin_analyse/claims/config_loader.py",
    "fin_analyse/gateway/tool_presentation.py",
    "fin_analyse/market/evidence_plan.py",
    "fin_analyse/market/market_evidence_plan.v1.json",
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
    "hermes-migration/systemd/hermes-gateway-fin.service",
    "hermes-migration/systemd/hermes-gateway-fin.service.d/20-fin-python-safety.conf",
    "hermes-migration/plugins/fin-consultation-first-tool/__init__.py",
    "hermes-migration/plugins/fin-consultation-first-tool/plugin.yaml",
    "hermes-migration/profile-memory/SOUL.md",
    "hermes-migration/profile-memory/MEMORY.md",
    "hermes-migration/profile-memory/USER.md",
    "hermes-migration/skills/fin-analyse/fin-analyse-consultation/SKILL.md",
    "hermes-migration/skills/fin-analyse/fin-analyse-ops/SKILL.md",
    "fin_analyse/guo_teacher_research/agent_runtime.py",
    "fin_analyse/guo_teacher_research/capability_broker.py",
    "fin_analyse/guo_teacher_research/codex_runtime.py",
    "fin_analyse/guo_teacher_research/local_capability_transport.py",
    "fin_analyse/guo_teacher_research/production_runtime.py",
)
_LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS = (
    ("scripts/consultation_runtime_canary_launcher.py", "file", 0o600),
    ("hermes-migration/plugins/fin-consultation-first-tool", "directory", 0o700),
)
_V2_THREE_TARGET_SPECIAL_HANDOFF_MODE_TARGETS = (
    ("scripts/consultation_runtime_canary_launcher.py", "file", 0o600),
    ("hermes-migration/plugins/fin-consultation-first-tool", "directory", 0o700),
    ("config", "directory", 0o755),
)
_SPECIAL_HANDOFF_MODE_TARGETS = (
    *_V2_THREE_TARGET_SPECIAL_HANDOFF_MODE_TARGETS,
    ("hermes-migration/cron/jobs.json", "file", 0o600),
)
_RUNTIME_PYTHON_SOURCE_TREES = (
    "fin_analyse",
    "scripts",
)
_REQUIRED_REGULAR_FILES = (
    "pyproject.toml",
    "uv.lock",
    "fin_analyse/__init__.py",
    "fin_analyse/gateway/mcp_server.py",
    "fin_analyse/gateway/tool_surface.py",
    *_CRITICAL_RUNTIME_FILES,
    "fin_analyse/guo_teacher_research/semantic_service.py",
    "fin_analyse/guo_teacher_research/semantic_state.py",
    "fin_analyse/guo_teacher_research/use_case_runner.py",
    "fin_analyse/guo_teacher_research/g_working_set.py",
    "fin_analyse/scraper/cdp_runtime.py",
    "fin_analyse/scraper/scheduled_run.py",
    "hermes-migration/MANIFEST.txt",
    "hermes-migration/cron/jobs.json",
    "hermes-migration/profile-config/config.yaml",
    "hermes-migration/plugins/fin-consultation-first-tool/__init__.py",
    "hermes-migration/plugins/fin-consultation-first-tool/plugin.yaml",
)
_REQUIRED_REAL_DIRECTORIES = (
    "fin_analyse",
    "hermes-migration/plugins/fin-consultation-first-tool",
    "hermes-migration/profile-memory",
    "hermes-migration/skills/fin-analyse",
)
_VENV_PYTHON = ".venv/bin/python"
_SYNC_RECEIPT = ".fin-frozen-sync.json"
_SYNC_RECEIPT_SCHEMA = 3
_MAX_SYNC_RECEIPT_BYTES = 128 * 1024
_VERSIONED_CHECKER = "scripts/prepare_fin_release.py"
_VERSIONED_CHECK_TIMEOUT_SECONDS = 30.0
_VERSIONED_CHECK_MAX_OUTPUT_BYTES = 1024 * 1024
_VERSIONED_CHECKER_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_MFD_CLOEXEC = 0x0001
_MFD_ALLOW_SEALING = 0x0002
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_F_SEAL_SEAL = 0x0001
_F_SEAL_SHRINK = 0x0002
_F_SEAL_GROW = 0x0004
_F_SEAL_WRITE = 0x0008
_VERSIONED_CHECK_BOOTSTRAP = """\
import os
import subprocess
import sys

_real_popen = subprocess.Popen


def _command_basename(value):
    try:
        return os.path.basename(os.fsdecode(os.fspath(value)))
    except (TypeError, ValueError, UnicodeError) as error:
        raise RuntimeError("prior checker subprocess executable is invalid") from error


def _guarded_popen(args, *popen_args, **kwargs):
    if len(popen_args) > 1:
        raise RuntimeError("prior checker positional process control is forbidden")
    if kwargs.get("shell", False):
        raise RuntimeError("prior checker shell subprocesses are forbidden")
    if kwargs.get("executable") is not None:
        raise RuntimeError("prior checker executable overrides are forbidden")
    if isinstance(args, (str, bytes, os.PathLike)):
        raise RuntimeError("prior checker string subprocesses are forbidden")
    if not isinstance(args, (list, tuple)) or not args:
        raise RuntimeError("prior checker subprocess argv is invalid")

    command_basename = _command_basename(args[0])
    if command_basename == "git":
        args = (
            "/usr/bin/git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.untrackedCache=false",
            *args[1:],
        )
    elif command_basename.casefold().startswith("git"):
        raise RuntimeError("prior checker suspicious Git executable is forbidden")
    return _real_popen(args, *popen_args, **kwargs)


subprocess.Popen = _guarded_popen
checker = sys.argv[1]
source_descriptor = int(sys.argv[2])
with os.fdopen(os.dup(source_descriptor), "rb") as source_stream:
    checker_source = source_stream.read()
sys.argv = [checker, *sys.argv[3:]]
namespace = {
    "__name__": "__main__",
    "__file__": checker,
    "__cached__": None,
    "__loader__": None,
    "__package__": None,
    "__spec__": None,
}
exec(compile(checker_source, checker, "exec"), namespace, namespace)
"""
_RUNTIME_ENTRYPOINT_MODULES = (
    "fin_analyse.gateway.mcp_server",
    "fin_analyse.gateway.tool_surface",
    "fin_analyse.consultation.presentation",
    "fin_analyse.claims.config_loader",
    "fin_analyse.gateway.tool_presentation",
    "fin_analyse.market.evidence_plan",
    "fin_analyse.runtime.hermes_managed_assets",
    "fin_analyse.guo_teacher_research.production_runtime",
    "fin_analyse.guo_teacher_research.semantic_service",
    "fin_analyse.guo_teacher_research.semantic_state",
    "fin_analyse.guo_teacher_research.g_working_set",
    "fin_analyse.scraper.cdp_runtime",
    "fin_analyse.scraper.scheduled_run",
    "scripts.apply_fin_hermes_external_integration",
    "scripts.consume_zsxq_capture_folder",
    "scripts.prepare_fin_release",
    "scripts.zsxq_windows_incremental_scheduler",
)
_ALLOWED_IGNORED_PATHS = {
    ".env",
    "knowledge-base/runtime",
    "knowledge-base/market-cache",
}
_PLUGIN_PYCACHE_ROOT = "hermes-migration/plugins"
_PLUGIN_PYCACHE_MAX_FILES = 16
_PLUGIN_PYCACHE_MAX_TOTAL_BYTES = 256 * 1024
_PLUGIN_PYCACHE_MAX_SINGLE_BYTES = 64 * 1024
_RUNTIME_BYTECODE_ALLOWED_ROOTS = (
    "fin_analyse",
    "scripts",
    "hermes-migration/plugins/fin-consultation-first-tool",
)
_RUNTIME_BYTECODE_INVENTORY_SCHEMA = "fin.runtime-bytecode-inventory/v2"
_RUNTIME_BYTECODE_QUARANTINE_SCHEMA = "fin.runtime-bytecode-quarantine/v2"
_RUNTIME_BYTECODE_QUARANTINE_PREFLIGHT_SCHEMA = "fin.runtime-bytecode-quarantine-preflight/v1"
_DEGRADED_CURRENT_CUTOVER_AUTHORITY_SCHEMA = "fin.degraded-current-cutover-authority/v1"
_DEGRADED_CURRENT_CUTOVER_PREFLIGHT_SCHEMA = "fin.degraded-current-cutover-preflight/v1"
_DEGRADED_CURRENT_CUTOVER_SCHEMA = "fin.degraded-current-cutover/v1"
_DEGRADED_CURRENT_CUTOVER_ROLLBACK_SCHEMA = "fin.degraded-current-cutover-rollback/v1"
_RUNTIME_BYTECODE_MAX_FILES = 256
_RUNTIME_BYTECODE_MAX_BYTES = 64 * 1024 * 1024
_RUNTIME_BYTECODE_MAX_SCAN_DIRECTORIES = 16_384
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2


@dataclass(frozen=True)
class ReleaseLayout:
    """Standard owner-local filesystem layout for one FIN release."""

    home: Path
    commit: str

    def __post_init__(self) -> None:
        if not self.home.is_absolute():
            raise ValueError("home must be an absolute path")
        if _FULL_COMMIT.fullmatch(self.commit) is None:
            raise ValueError("commit must be one lowercase 40-character Git SHA")

    @property
    def data_root(self) -> Path:
        return self.home / ".local/share/fin-analyse"

    @property
    def releases_root(self) -> Path:
        return self.data_root / "releases"

    @property
    def release_root(self) -> Path:
        return self.releases_root / self.commit

    @property
    def current_link(self) -> Path:
        return self.data_root / "current"

    @property
    def env_file(self) -> Path:
        return self.home / ".config/fin-analyse/fin.env"

    @property
    def shared_root(self) -> Path:
        return self.data_root / "shared/knowledge-base"

    @property
    def runtime_root(self) -> Path:
        return self.shared_root / "runtime"

    @property
    def market_cache_root(self) -> Path:
        return self.shared_root / "market-cache"


@dataclass(frozen=True)
class _RuntimeBytecodeFile:
    name: str
    relative_path: str
    source_relative_path: str
    size: int
    mode: int
    sha256: str
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class _RuntimeBytecodeCache:
    cache_directory: Path
    relative_cache_directory: str
    cache_identity: tuple[int, int, int, int, int, int]
    files: tuple[_RuntimeBytecodeFile, ...]
    total_bytes: int
    source_parent_fd: int = field(compare=False, repr=False)
    cache_fd: int = field(compare=False, repr=False)
    cache_name: str = field(compare=False)


@dataclass(frozen=True)
class _RuntimeBytecodeInventory:
    caches: tuple[_RuntimeBytecodeCache, ...]
    files: tuple[_RuntimeBytecodeFile, ...]
    inventory_sha256: str
    total_bytes: int


@dataclass(frozen=True)
class _QuarantineReleaseDescriptors:
    data_fd: int
    releases_fd: int
    candidate_fd: int
    previous_fd: int


@dataclass(frozen=True)
class _SpecialHandoffModeBinding:
    relative: str
    kind: str
    expected_mode: int
    parent_fd: int
    descriptor: int
    release_root_stamp: tuple[int, int, int, int, int, int, int, int]
    ancestor_stamps: tuple[tuple[int, int, int, int, int, int, int, int], ...]
    identity: tuple[int, int, int, int, int, int, int]
    content_sha256: str | None
    initial_mode: int


class _SpecialHandoffModeContract(Enum):
    PRE_HANDOFF = "pre-handoff"
    LEGACY_V2_TWO_TARGET = "legacy-v2-two-target"
    CURRENT_V2_THREE_TARGET = "current-v2-three-target"
    CURRENT_V2_FOUR_TARGET = "current-v2-four-target"


_FROZEN_CHECKER_CONTRACTS_BY_SHA256 = (
    (
        "94c50cac2ae99b6555fd7f6264ebee49a4e0e68ca3a90e588eb218cf38ee7476",
        _SpecialHandoffModeContract.PRE_HANDOFF,
    ),
    (
        "5d38bb0ecdbb09c89cda0ef9bb2c1fab826341cb44c632d1538ddd81f5cf115b",
        _SpecialHandoffModeContract.LEGACY_V2_TWO_TARGET,
    ),
    (
        "77310e42788540082ebe8869e8897d8729d7310ae6b4f732a22cd34895bb7866",
        _SpecialHandoffModeContract.LEGACY_V2_TWO_TARGET,
    ),
    (
        "6bccfcd6246b8adc89fc377246a3a7f7c724ec51831705a94d357a3fd72baeeb",
        _SpecialHandoffModeContract.LEGACY_V2_TWO_TARGET,
    ),
    (
        "fd4f0179068f79349a6d1a6399748ede2167b8dd0a4b9e34e72102ca18d70702",
        _SpecialHandoffModeContract.LEGACY_V2_TWO_TARGET,
    ),
    (
        "a7c41f8e8cf01285ed021c6b981f0e3344d3192f6e6f3448f809c395c664604b",
        _SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        "c1240aa40dea1c4bf55b9113d024e78d5d5b7acdfc607421f629630b05f8fe2b",
        _SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        "0414995183117c990990b67a91228f6b71d04540abc9d61a90a512162c2d79bb",
        _SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        "f8011b0dee3256d1ef368f5ca773075c0d9229f063d3dd4949028b523772b317",
        _SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        "8eeb4a2fb67569c89e1140731ee24d1418bacf4ee3243416bf25d514ba068b9c",
        _SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        "66e40f06a9240173a457b82c6181f72994241f8a92f3904844565256526597bc",
        _SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        "856595d1b42beb326554489fcb760d1dbd156b8cf338b516bf45e2677c5f90ec",
        _SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        "c6054dabc00860ea7aff51afd2a4f8fb0ba90c302a47c9e58148db81456edd53",
        _SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET,
    ),
    (
        "52674a5226f47c559b8b7e5931eae318627a3d1635ee9553789f195c5db4c9b8",
        _SpecialHandoffModeContract.CURRENT_V2_FOUR_TARGET,
    ),
    (
        "421707140db81f58c893da8c034962886150fed0f295658589a52a0213b17d81",
        _SpecialHandoffModeContract.CURRENT_V2_FOUR_TARGET,
    ),
)


@dataclass(frozen=True)
class _PublishedSyncReceipt:
    descriptor: int = field(compare=False, repr=False)
    metadata_stamp: tuple[int, int, int, int, int, int, int, int]
    content_sha256: str
    invalidation_identity: tuple[int, int, int, int]
    cleanup_identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _SyncReceiptBinding:
    descriptor: int = field(compare=False, repr=False)
    metadata_stamp: tuple[int, int, int, int, int, int, int, int]
    content_sha256: str
    payload: dict[str, Any] = field(compare=False, repr=False)
    require_publication_identity: bool


def _run_git(
    release_root: Path,
    *args: str,
    root_fd: int | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    try:
        completed = subprocess.run(
            (
                _SYSTEM_GIT,
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(release_root),
                *args,
            ),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=_GIT_TIMEOUT_SECONDS,
            pass_fds=() if root_fd is None else (root_fd,),
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 125, "", "")
    if (
        len(completed.stdout.encode("utf-8", errors="surrogateescape")) > _MAX_GIT_OUTPUT_BYTES
        or len(completed.stderr.encode("utf-8", errors="surrogateescape")) > _MAX_GIT_OUTPUT_BYTES
    ):
        return subprocess.CompletedProcess(completed.args, 125, "", "")
    return completed


def _owner_mode(path: Path, expected_mode: int, *, kind: str) -> bool:
    try:
        metadata = path.stat()
    except OSError:
        return False
    if path.is_symlink() or metadata.st_uid != os.geteuid():
        return False
    if kind == "file" and not stat.S_ISREG(metadata.st_mode):
        return False
    if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        return False
    return stat.S_IMODE(metadata.st_mode) == expected_mode


def _owner_controlled_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and metadata.st_nlink == 1
    )


def _owner_controlled_real_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
    )


def _path_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    metadata = path.lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _link_matches(link: Path, target: Path) -> bool:
    if not link.is_symlink():
        return False
    try:
        return link.resolve(strict=True) == target.resolve(strict=True)
    except OSError:
        return False


def _is_resolved_executable(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return resolved.is_file() and os.access(path, os.X_OK)


def _git_paths(
    root: Path,
    *args: str,
    root_fd: int | None = None,
) -> tuple[str, ...] | None:
    result = _run_git(root, *args) if root_fd is None else _run_git(root, *args, root_fd=root_fd)
    if result.returncode != 0:
        return None
    return tuple(line for line in result.stdout.splitlines() if line)


def _ignored_path_allowed(relative: str) -> bool:
    normalized = relative.rstrip("/")
    return (
        normalized == ".venv"
        or normalized.startswith(".venv/")
        or normalized in (_ALLOWED_IGNORED_PATHS)
    )


def _plugin_pycache_path_allowed(root: Path, relative: str) -> bool:
    """Bound the one class of regenerable plugin bytecode cache.

    Hermes CLI imports the FIN plugin without ``PYTHONDONTWRITEBYTECODE``, so
    CPython regenerates ``__pycache__/<module>.cpython-311.pyc`` inside the
    release.  A pyc is a deterministic cache of tracked source and cannot
    change release semantics — the same reason schema-3 venv digests prune
    ``__pycache__``.  Accept only a ``.pyc`` whose sibling ``.py`` source is
    a tracked release file, within a hard size bound; the caller additionally
    caps the whole plugin cache (files/total bytes).
    """
    if not relative.startswith(_PLUGIN_PYCACHE_ROOT + "/"):
        return False
    if "__pycache__" not in relative.split("/") or not relative.endswith(".pyc"):
        return False
    try:
        path = root / relative
        if not path.is_file() or path.stat().st_size > _PLUGIN_PYCACHE_MAX_SINGLE_BYTES:
            return False
    except OSError:
        return False
    parts = relative.split("/")
    filename = parts[-1]
    if ".cpython-" not in filename:
        return False
    stem = filename.split(".cpython-", 1)[0]
    if not stem:
        return False
    try:
        pycache_index = parts.index("__pycache__")
    except ValueError:
        return False
    source_relative = "/".join((*parts[:pycache_index], stem + ".py"))
    return (root / source_relative).is_file()


def _git_identity_status(
    layout: ReleaseLayout,
    *,
    required_regular_files: Sequence[str] = _REQUIRED_REGULAR_FILES,
    required_real_directories: Sequence[str] = _REQUIRED_REAL_DIRECTORIES,
    release_fd: int | None = None,
    allow_plugin_pycache: bool = True,
) -> dict[str, Any]:
    root = layout.release_root if release_fd is None else _directory_fd_path(release_fd)
    if release_fd is None:
        real_release_root = root.is_dir() and not root.is_symlink()
    else:
        root_metadata = os.fstat(release_fd)
        real_release_root = bool(
            stat.S_ISDIR(root_metadata.st_mode) and root_metadata.st_uid == os.geteuid()
        )

    def observe_git(*arguments: str) -> subprocess.CompletedProcess[str]:
        if release_fd is None:
            return _run_git(root, *arguments)
        return _run_git(root, *arguments, root_fd=release_fd)

    head = observe_git("rev-parse", "HEAD") if real_release_root else None
    head_commit = head.stdout.strip() if head and head.returncode == 0 else None
    top = observe_git("rev-parse", "--show-toplevel") if real_release_root else None
    try:
        top_level_matches = bool(
            top is not None
            and top.returncode == 0
            and Path(top.stdout.strip()).resolve(strict=True) == root.resolve(strict=True)
        )
    except OSError:
        top_level_matches = False
    symbolic = observe_git("symbolic-ref", "-q", "HEAD") if real_release_root else None
    detached = bool(
        symbolic is not None
        and symbolic.returncode == 1
        and not symbolic.stdout
        and not symbolic.stderr
    )
    status = (
        observe_git("status", "--porcelain", "--untracked-files=no") if real_release_root else None
    )
    tracked_clean = bool(
        status is not None and status.returncode == 0 and not status.stdout.strip()
    )
    untracked = (
        _git_paths(
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            root_fd=release_fd,
        )
        if real_release_root
        else None
    )
    ignored = (
        _git_paths(
            root,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            root_fd=release_fd,
        )
        if real_release_root
        else None
    )
    unexpected_untracked = (
        None
        if untracked is None
        else tuple(
            path
            for path in untracked
            if path != _SYNC_RECEIPT and path.rstrip("/") not in _ALLOWED_IGNORED_PATHS
        )
    )
    if ignored is None:
        unexpected_ignored = None
        known_plugin_pycache: tuple[str, ...] = ()
    else:
        if allow_plugin_pycache:
            known_plugin_pycache = tuple(
                path for path in ignored if _plugin_pycache_path_allowed(root, path)
            )
            if (
                len(known_plugin_pycache) <= _PLUGIN_PYCACHE_MAX_FILES
                and sum((root / path).stat().st_size for path in known_plugin_pycache)
                <= _PLUGIN_PYCACHE_MAX_TOTAL_BYTES
            ):
                unexpected_ignored = tuple(
                    path
                    for path in ignored
                    if not _ignored_path_allowed(path) and path not in known_plugin_pycache
                )
            else:
                known_plugin_pycache = ()
                unexpected_ignored = tuple(
                    path for path in ignored if not _ignored_path_allowed(path)
                )
        else:
            known_plugin_pycache = ()
            unexpected_ignored = tuple(
                path for path in ignored if not _ignored_path_allowed(path)
            )
    required_files: dict[str, bool] = {
        relative: _owner_controlled_regular_file(root / relative)
        for relative in required_regular_files
    }
    required_directories = {
        relative: _real_nonempty_file_tree(root / relative)
        for relative in required_real_directories
    }
    required_files[_VENV_PYTHON] = _is_resolved_executable(root / _VENV_PYTHON)
    return {
        "real_release_root": real_release_root,
        "head_commit": head_commit,
        "commit_matches": head_commit == layout.commit,
        "top_level_matches": top_level_matches,
        "detached": detached,
        "tracked_clean": tracked_clean,
        "unexpected_untracked": unexpected_untracked,
        "unexpected_ignored": unexpected_ignored,
        "known_plugin_pycache": known_plugin_pycache,
        "required_files": required_files,
        "required_directories": required_directories,
    }


def _real_nonempty_file_tree(root: Path) -> bool:
    if not root.is_dir() or root.is_symlink():
        return False
    file_count = 0
    try:
        for path in root.rglob("*"):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                return False
            if stat.S_ISREG(metadata.st_mode):
                file_count += 1
            elif not stat.S_ISDIR(metadata.st_mode):
                return False
    except OSError:
        return False
    return file_count > 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _venv_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        # Bytecode caches are derived, rebuildable state: their presence or
        # absence must not change the frozen venv identity.  This is what
        # makes the scheduled checkpoint immune to stray ``.pyc`` pollution
        # (previously ``DAILY_WORKSPACE_SCHEDULED_RELEASE_UNSAFE`` after any
        # Python process ran in the release venv without
        # ``PYTHONDONTWRITEBYTECODE=1``).
        directories[:] = [name for name in directories if name != "__pycache__"]
        files.sort()
        current_path = Path(current)
        for name in (*directories, *files):
            path = current_path / name
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
                payload = bytes.fromhex(_sha256_file(path))
            else:
                raise ValueError(f"unsupported virtual-environment entry: {relative}")
            digest.update(f"{relative}\0{kind}\0{mode:o}\0".encode())
            digest.update(payload)
            digest.update(b"\0")
    return digest.hexdigest()


def _venv_digest(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("release virtual environment must be a real directory")
    return _venv_tree_digest(root)


def _venv_digest_from_fd(venv_fd: int) -> str:
    return _venv_tree_digest(_directory_fd_path(venv_fd))


@contextmanager
def _bound_venv_directory_fd(release_fd: int) -> Iterator[int]:
    venv_fd = _open_directory_fd(".venv", dir_fd=release_fd)
    try:
        initial = _stat_identity(os.fstat(venv_fd))
        path_initial = _stat_identity(
            os.stat(
                ".venv",
                dir_fd=release_fd,
                follow_symlinks=False,
            )
        )
        if initial != path_initial:
            raise RuntimeError("release virtual environment binding changed")
        yield venv_fd
        if (
            _stat_identity(os.fstat(venv_fd)) != initial
            or _stat_identity(
                os.stat(
                    ".venv",
                    dir_fd=release_fd,
                    follow_symlinks=False,
                )
            )
            != initial
        ):
            raise RuntimeError("release virtual environment binding changed")
    finally:
        os.close(venv_fd)


def _venv_digest_from_release_fd(release_fd: int) -> str:
    with _bound_venv_directory_fd(release_fd) as venv_fd:
        return _venv_digest_from_fd(venv_fd)


def _python_identity(layout: ReleaseLayout) -> dict[str, str]:
    interpreter = layout.release_root / _VENV_PYTHON
    venv_root = layout.release_root / ".venv"
    try:
        resolved_interpreter = interpreter.resolve(strict=True)
        interpreter_metadata = resolved_interpreter.stat()
        resolved_release_root = layout.release_root.resolve(strict=True)
    except OSError:
        raise ValueError("release Python identity does not match its virtual environment") from None
    if (
        resolved_interpreter == resolved_release_root
        or resolved_release_root in resolved_interpreter.parents
        or not stat.S_ISREG(interpreter_metadata.st_mode)
        or interpreter_metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(interpreter_metadata.st_mode) & 0o022
    ):
        raise ValueError("release Python identity does not match its virtual environment")
    probe = (
        "import json,pathlib,sys;"
        "venv=pathlib.Path(sys.argv[1]);"
        "entry=pathlib.Path(sys.argv[2]);"
        "actual=pathlib.Path(sys.executable);"
        "ok=bool(sys.flags.isolated and sys.flags.no_site and "
        "sys.dont_write_bytecode and "
        "actual.resolve(strict=True)==entry.resolve(strict=True));"
        "payload={'executable':str(entry),'prefix':str(venv),"
        "'base_prefix':str(pathlib.Path(sys.base_prefix).resolve(strict=True))};"
        "print(json.dumps(payload,sort_keys=True));"
        "raise SystemExit(0 if ok else 1)"
    )
    result = subprocess.run(
        (
            str(interpreter),
            "-I",
            "-S",
            "-B",
            "-c",
            probe,
            str(venv_root),
            str(interpreter),
        ),
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        payload: object = json.loads(result.stdout) if result.returncode == 0 else None
        if not isinstance(payload, dict):
            raise TypeError("release interpreter identity must be an object")
        executable = payload.get("executable")
        prefix = payload.get("prefix")
        base_prefix = payload.get("base_prefix")
        if (
            not isinstance(executable, str)
            or not isinstance(prefix, str)
            or not isinstance(base_prefix, str)
        ):
            raise TypeError("release interpreter identity fields must be strings")
        identity = {
            "executable": executable,
            "prefix": prefix,
            "base_prefix": base_prefix,
        }
        prefix_matches = Path(identity["prefix"]).resolve(strict=True) == (
            venv_root.resolve(strict=True)
        )
        executable_matches = Path(identity["executable"]).resolve(strict=True) == (
            resolved_interpreter
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        prefix_matches = executable_matches = False
        identity = {}
    if not prefix_matches or not executable_matches:
        raise ValueError("release Python identity does not match its virtual environment")
    return identity


def _require_stored_python_identity_bound(
    layout: ReleaseLayout,
    value: object,
    *,
    release_fd: int | None = None,
    venv_fd: int | None = None,
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RuntimeError("degraded prior Python identity is invalid")
    executable = value.get("executable")
    prefix = value.get("prefix")
    base_prefix = value.get("base_prefix")
    if not all(isinstance(item, str) for item in (executable, prefix, base_prefix)):
        raise RuntimeError("degraded prior Python identity is invalid")
    assert isinstance(executable, str)
    assert isinstance(prefix, str)
    assert isinstance(base_prefix, str)
    canonical_venv_root = layout.release_root / ".venv"
    canonical_interpreter = layout.release_root / _VENV_PYTHON
    opened_venv_fd = -1
    try:
        if release_fd is None:
            if venv_fd is not None:
                raise RuntimeError("degraded prior Python identity is invalid")
            access_release_root = layout.release_root
            access_venv_root = canonical_venv_root
        else:
            access_release_root = _directory_fd_path(release_fd)
            if venv_fd is None:
                opened_venv_fd = _open_directory_fd(".venv", dir_fd=release_fd)
                bound_venv_fd = opened_venv_fd
            else:
                bound_venv_fd = venv_fd
            if _stat_identity(os.fstat(bound_venv_fd)) != _stat_identity(
                os.stat(
                    ".venv",
                    dir_fd=release_fd,
                    follow_symlinks=False,
                )
            ):
                raise RuntimeError("degraded prior Python identity is invalid")
            access_venv_root = _directory_fd_path(bound_venv_fd)
        access_interpreter = access_venv_root / "bin/python"
        resolved_release_root = access_release_root.resolve(strict=True)
        resolved_venv_root = access_venv_root.resolve(strict=True)
        resolved_interpreter = access_interpreter.resolve(strict=True)
        resolved_base_prefix = Path(base_prefix).resolve(strict=True)
        interpreter_metadata = resolved_interpreter.stat()
        base_prefix_metadata = resolved_base_prefix.stat()
    except OSError as error:
        raise RuntimeError("degraded prior Python identity is invalid") from error
    finally:
        if opened_venv_fd >= 0:
            os.close(opened_venv_fd)
    if (
        Path(executable) != canonical_interpreter
        or Path(prefix) != canonical_venv_root
        or resolved_venv_root.parent != resolved_release_root
        or (release_fd is None and resolved_venv_root != canonical_venv_root)
        or resolved_interpreter == resolved_release_root
        or resolved_release_root in resolved_interpreter.parents
        or not stat.S_ISREG(interpreter_metadata.st_mode)
        or interpreter_metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(interpreter_metadata.st_mode) & 0o022
        or not stat.S_ISDIR(base_prefix_metadata.st_mode)
        or base_prefix_metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(base_prefix_metadata.st_mode) & 0o022
    ):
        raise RuntimeError("degraded prior Python identity is invalid")
    return {
        "executable": executable,
        "prefix": prefix,
        "base_prefix": base_prefix,
    }


def _python_source_tree_trusted(root: Path) -> bool:
    try:
        root_metadata = root.lstat()
    except OSError:
        return False
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.geteuid()
    ):
        return False
    python_file_count = 0
    pending = [root]
    try:
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    metadata = entry.stat(follow_symlinks=False)
                    path = Path(entry.path)
                    if stat.S_ISLNK(metadata.st_mode):
                        return False
                    if stat.S_ISDIR(metadata.st_mode):
                        if metadata.st_uid != os.geteuid():
                            return False
                        pending.append(path)
                    elif stat.S_ISREG(metadata.st_mode):
                        if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
                            return False
                        if any(
                            path.name.endswith(suffix)
                            for suffix in (
                                *importlib.machinery.BYTECODE_SUFFIXES,
                                *importlib.machinery.EXTENSION_SUFFIXES,
                            )
                        ):
                            return False
                        if path.suffix == ".py":
                            python_file_count += 1
                    else:
                        return False
    except OSError:
        return False
    return python_file_count > 0


def _runtime_probe_assets_trusted(layout: ReleaseLayout) -> bool:
    return bool(
        all(
            _owner_controlled_regular_file(layout.release_root / relative)
            for relative in _REQUIRED_REGULAR_FILES
        )
        and all(
            _python_source_tree_trusted(layout.release_root / relative)
            for relative in _RUNTIME_PYTHON_SOURCE_TREES
        )
    )


def _release_site_packages(layout: ReleaseLayout) -> Path:
    candidates = tuple(
        path
        for library_root in (
            layout.release_root / ".venv/lib",
            layout.release_root / ".venv/lib64",
        )
        if library_root.is_dir() and not library_root.is_symlink()
        for path in library_root.glob("python*/site-packages")
        if path.is_dir() and not path.is_symlink()
    )
    if len(candidates) != 1:
        raise ValueError("release virtual environment must contain one site-packages directory")
    site_packages = candidates[0]
    try:
        resolved = site_packages.resolve(strict=True)
    except OSError:
        raise ValueError("release site-packages path is invalid") from None
    if resolved != site_packages or layout.release_root not in resolved.parents:
        raise ValueError("release site-packages path escapes the release")
    return site_packages


def _runtime_import_probe(layout: ReleaseLayout) -> dict[str, Any]:
    if not _runtime_probe_assets_trusted(layout):
        raise ValueError("release runtime source tree is not owner-controlled and single-link")
    interpreter = layout.release_root / _VENV_PYTHON
    site_packages = _release_site_packages(layout)
    modules_sha256 = hashlib.sha256(
        json.dumps(_RUNTIME_ENTRYPOINT_MODULES, separators=(",", ":")).encode()
    ).hexdigest()
    probe = (
        "import importlib,json,os,pathlib,sys;"
        "root=pathlib.Path(sys.argv[1]).resolve(strict=True);"
        "site_packages=pathlib.Path(sys.argv[2]).resolve(strict=True);"
        "modules=json.loads(sys.argv[3]);"
        "sys.path.insert(0,str(site_packages));"
        "sys.path.insert(0,str(root));"
        "loaded=[importlib.import_module(name) for name in modules];"
        "paths=[pathlib.Path(module.__file__).resolve(strict=True) for module in loaded];"
        "ok=all(path==root or root in path.parents for path in paths);"
        "payload=(json.dumps({'ready':ok},sort_keys=True)+'\\n').encode();"
        "os.write(1,payload);"
        "raise SystemExit(0 if ok else 1)"
    )
    result = subprocess.run(
        (
            str(interpreter),
            "-B",
            "-I",
            "-S",
            "-c",
            probe,
            str(layout.release_root),
            str(site_packages),
            json.dumps(_RUNTIME_ENTRYPOINT_MODULES, separators=(",", ":")),
        ),
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict) or payload != {"ready": True}:
        raise ValueError("release runtime entrypoint import probe failed")
    return {
        "ready": True,
        "module_count": len(_RUNTIME_ENTRYPOINT_MODULES),
        "modules_sha256": modules_sha256,
    }


def _expected_sync_receipt(layout: ReleaseLayout, *, run_python: bool) -> dict[str, Any]:
    if run_python and not _runtime_probe_assets_trusted(layout):
        raise ValueError("release runtime source tree is not owner-controlled and single-link")
    identity = _python_identity(layout) if run_python else None
    runtime_imports = _runtime_import_probe(layout) if run_python else None
    return {
        "schema_version": _SYNC_RECEIPT_SCHEMA,
        "commit": layout.commit,
        "uv_lock_sha256": _sha256_file(layout.release_root / "uv.lock"),
        "venv_sha256": _venv_digest(layout.release_root / ".venv"),
        "python_identity": identity,
        "runtime_imports": runtime_imports,
        "handoff_binding_sha256": _special_handoff_binding_sha256(layout),
    }


def _read_bounded_sync_receipt(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_SYNC_RECEIPT_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_SYNC_RECEIPT_BYTES:
                raise ValueError("frozen-sync receipt exceeds its byte bound")
        return b"".join(chunks)
    finally:
        os.lseek(descriptor, 0, os.SEEK_SET)


@contextmanager
def _bound_sync_receipt(
    layout: ReleaseLayout,
    *,
    require_publication_identity: bool = False,
    release_fd: int | None = None,
) -> Iterator[_SyncReceiptBinding]:
    parent_fd = -1
    descriptor = -1
    try:
        parent_fd = (
            _open_directory_fd(layout.release_root, owner_only=True)
            if release_fd is None
            else _duplicate_owner_only_directory_fd(release_fd)
        )
        descriptor = _open_regular_fd(_SYNC_RECEIPT, dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        path_metadata = os.stat(
            _SYNC_RECEIPT,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _MAX_SYNC_RECEIPT_BYTES
            or _special_handoff_metadata_stamp(metadata)
            != _special_handoff_metadata_stamp(path_metadata)
        ):
            raise PermissionError("frozen-sync receipt binding is invalid")
        encoded = _read_bounded_sync_receipt(descriptor)
        payload = json.loads(encoded.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("frozen-sync receipt payload is invalid")
        if require_publication_identity and payload.get("publication_identity") != list(
            _receipt_publication_identity(metadata)
        ):
            raise PermissionError("frozen-sync receipt publication identity changed")
        yield _SyncReceiptBinding(
            descriptor=descriptor,
            metadata_stamp=_special_handoff_metadata_stamp(metadata),
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            payload=payload,
            require_publication_identity=require_publication_identity,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def _sync_receipt_binding_current(
    layout: ReleaseLayout,
    binding: _SyncReceiptBinding,
    *,
    release_fd: int | None = None,
) -> bool:
    parent_fd = -1
    try:
        parent_fd = (
            _open_directory_fd(layout.release_root, owner_only=True)
            if release_fd is None
            else _duplicate_owner_only_directory_fd(release_fd)
        )
        path_metadata = os.stat(
            _SYNC_RECEIPT,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        descriptor_metadata = os.fstat(binding.descriptor)
        if (
            _special_handoff_metadata_stamp(path_metadata) != binding.metadata_stamp
            or _special_handoff_metadata_stamp(descriptor_metadata) != binding.metadata_stamp
            or _sha256_fd(binding.descriptor) != binding.content_sha256
        ):
            return False
        return bool(
            not binding.require_publication_identity
            or binding.payload.get("publication_identity")
            == list(_receipt_publication_identity(descriptor_metadata))
        )
    except OSError:
        return False
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _read_sync_receipt(
    layout: ReleaseLayout,
    *,
    require_publication_identity: bool = False,
    release_fd: int | None = None,
) -> dict[str, Any] | None:
    try:
        with _bound_sync_receipt(
            layout,
            require_publication_identity=require_publication_identity,
            release_fd=release_fd,
        ) as binding:
            if not _sync_receipt_binding_current(
                layout,
                binding,
                release_fd=release_fd,
            ):
                return None
            return binding.payload
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None


def _sync_receipt_valid(layout: ReleaseLayout) -> bool:
    if not _runtime_probe_assets_trusted(layout):
        return False
    try:
        with _bound_sync_receipt(
            layout,
            require_publication_identity=True,
        ) as binding:
            receipt = binding.payload
            expected = {
                "schema_version": _SYNC_RECEIPT_SCHEMA,
                "commit": layout.commit,
                "uv_lock_sha256": _sha256_file(layout.release_root / "uv.lock"),
                "venv_sha256": _venv_digest(layout.release_root / ".venv"),
            }
            for key, value in expected.items():
                if receipt.get(key) != value:
                    return False
            if receipt.get("python_identity") != _python_identity(layout) or receipt.get(
                "runtime_imports"
            ) != _runtime_import_probe(layout):
                return False
            if not _sync_receipt_binding_current(layout, binding):
                return False
            if receipt.get("handoff_binding_sha256") != _special_handoff_binding_sha256(layout):
                return False
            return _sync_receipt_binding_current(layout, binding)
    except (OSError, ValueError):
        return False


def _secure_owner_directory(path: Path) -> bool:
    if not _owner_controlled_real_directory(path):
        return False
    try:
        return stat.S_IMODE(path.lstat().st_mode) & 0o022 == 0
    except OSError:
        return False


def _require_secure_directories(
    layout: ReleaseLayout,
    *,
    release_fd: int | None = None,
) -> None:
    # The exact owner-only release root is the confinement boundary for checkout
    # and virtual-environment descendants whose modes legitimately reflect umask.
    release_root_secure = (
        _owner_mode(layout.release_root, 0o700, kind="directory")
        if release_fd is None
        else bool(
            stat.S_ISDIR((metadata := os.fstat(release_fd)).st_mode)
            and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700
        )
    )
    if not release_root_secure:
        raise PermissionError(f"expected secure owner-controlled directory: {layout.release_root}")
    for path in (
        layout.data_root,
        layout.releases_root,
        layout.env_file.parent,
        layout.shared_root,
    ):
        if not _secure_owner_directory(path):
            raise PermissionError(f"expected secure owner-controlled directory: {path}")


def _critical_runtime_file_status(
    layout: ReleaseLayout,
    *,
    release_fd: int | None = None,
) -> dict[str, bool]:
    release_root = layout.release_root if release_fd is None else _directory_fd_path(release_fd)
    status = {
        relative: _owner_controlled_regular_file(release_root / relative)
        for relative in _CRITICAL_RUNTIME_FILES
    }
    if release_fd is None:
        release_root_secure = _owner_mode(
            layout.release_root,
            0o700,
            kind="directory",
        )
    else:
        metadata = os.fstat(release_fd)
        release_root_secure = bool(
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700
        )
    if not release_root_secure or not all(status.values()):
        return status
    index_safe = dict.fromkeys(_CRITICAL_RUNTIME_FILES, False)
    inventory = (
        _run_git(
            release_root,
            "ls-files",
            "-v",
            "-z",
            "--",
            *_CRITICAL_RUNTIME_FILES,
        )
        if release_fd is None
        else _run_git(
            release_root,
            "ls-files",
            "-v",
            "-z",
            "--",
            *_CRITICAL_RUNTIME_FILES,
            root_fd=release_fd,
        )
    )
    if inventory.returncode == 0:
        for record in inventory.stdout.split("\0"):
            if len(record) > 2 and record.startswith("H ") and record[2:] in index_safe:
                index_safe[record[2:]] = True
    return {relative: trusted and index_safe[relative] for relative, trusted in status.items()}


def _require_critical_runtime_files(layout: ReleaseLayout) -> None:
    invalid = [
        relative
        for relative, trusted in _critical_runtime_file_status(layout).items()
        if not trusted
    ]
    if invalid:
        raise PermissionError(
            "release critical runtime files must be owner-controlled single-link "
            "tracked/index-safe files: " + ", ".join(invalid)
        )


@contextmanager
def _release_lock(layout: ReleaseLayout) -> Iterator[None]:
    if not _secure_owner_directory(layout.data_root):
        raise PermissionError(f"expected secure owner-controlled directory: {layout.data_root}")
    lock_path = layout.data_root / ".release.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PermissionError("FIN release lock must be an owner-only 0600 file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _atomic_write_receipt(path: Path, payload: dict[str, Any]) -> _PublishedSyncReceipt:
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}"
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        receipt_payload = {
            **payload,
            "publication_identity": list(_receipt_publication_identity(os.fstat(descriptor))),
        }
        encoded = (
            json.dumps(receipt_payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        metadata = os.fstat(descriptor)
        _require_special_handoff_metadata(metadata, kind="file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("frozen-sync receipt publication is not owner-only")
        os.replace(temporary, path)
        publication = _PublishedSyncReceipt(
            descriptor=descriptor,
            metadata_stamp=_special_handoff_metadata_stamp(metadata),
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            invalidation_identity=_receipt_invalidation_capability_identity(metadata),
            cleanup_identity=_receipt_cleanup_capability_identity(metadata),
        )
        try:
            _fsync_directory(path.parent)
        except BaseException:
            with suppress(OSError):
                _remove_exact_published_receipt(path, publication)
            raise
        descriptor = -1
        return publication
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(temporary):
            temporary.unlink()


def _published_receipt_current(
    path: Path,
    publication: _PublishedSyncReceipt,
) -> bool:
    try:
        current = os.stat(path, follow_symlinks=False)
        return bool(
            _special_handoff_metadata_stamp(current) == publication.metadata_stamp
            and _special_handoff_metadata_stamp(os.fstat(publication.descriptor))
            == publication.metadata_stamp
            and _sha256_fd(publication.descriptor) == publication.content_sha256
        )
    except OSError:
        return False


def _remove_exact_published_receipt(
    path: Path,
    publication: _PublishedSyncReceipt,
) -> bool:
    try:
        descriptor_metadata = os.fstat(publication.descriptor)
    except OSError:
        return False
    if (
        _receipt_invalidation_capability_identity(descriptor_metadata)
        != publication.invalidation_identity
    ):
        return False
    os.ftruncate(publication.descriptor, 0)
    os.fsync(publication.descriptor)
    invalidated_metadata = os.fstat(publication.descriptor)
    if (
        _receipt_invalidation_capability_identity(invalidated_metadata)
        != publication.invalidation_identity
    ):
        return False
    parent_fd = _open_directory_fd(path.parent, owner_only=True)
    quarantine_name = f".{path.name}.invalid-{os.getpid()}-{publication.metadata_stamp[1]}"
    try:
        try:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return False
        if _receipt_cleanup_capability_identity(current) != publication.cleanup_identity:
            return False
        try:
            _rename_noreplace(parent_fd, path.name, parent_fd, quarantine_name)
        except OSError:
            return False
        quarantined = os.stat(
            quarantine_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            _receipt_cleanup_capability_identity(quarantined) != publication.cleanup_identity
            or _receipt_cleanup_capability_identity(os.fstat(publication.descriptor))
            != publication.cleanup_identity
            or quarantined.st_size != 0
            or _sha256_fd(publication.descriptor) != hashlib.sha256(b"").hexdigest()
        ):
            with suppress(OSError):
                _rename_noreplace(parent_fd, quarantine_name, parent_fd, path.name)
                _fsync_directory(path.parent)
            return False
        os.unlink(quarantine_name, dir_fd=parent_fd)
        _fsync_directory(path.parent)
        return True
    finally:
        os.close(parent_fd)


def record_frozen_sync(layout: ReleaseLayout) -> dict[str, Any]:
    """Record evidence for an externally completed ``uv sync --frozen``."""

    with _release_lock(layout):
        _require_secure_directories(layout)
        _require_critical_runtime_files(layout)
        code = _git_identity_status(layout)
        _require_code_identity(code, layout)
        handoff_modes = _converge_special_handoff_modes(layout)
        _require_code_identity(_git_identity_status(layout), layout)
        receipt = _expected_sync_receipt(layout, run_python=True)
        receipt_path = layout.release_root / _SYNC_RECEIPT
        publication = _atomic_write_receipt(receipt_path, receipt)
        try:
            if (
                not _sync_receipt_valid(layout)
                or not _published_receipt_current(receipt_path, publication)
                or receipt["handoff_binding_sha256"] != _special_handoff_binding_sha256(layout)
                or not _published_receipt_current(receipt_path, publication)
            ):
                raise PermissionError("frozen-sync receipt publication boundary changed")
        except BaseException:
            with suppress(OSError):
                _remove_exact_published_receipt(receipt_path, publication)
            raise
        finally:
            os.close(publication.descriptor)
        return {
            "recorded": True,
            "commit": layout.commit,
            "receipt": str(layout.release_root / _SYNC_RECEIPT),
            "handoff_modes": handoff_modes,
        }


def _code_status(
    layout: ReleaseLayout,
    *,
    validate_frozen_sync: bool = True,
) -> dict[str, Any]:
    code = _git_identity_status(layout)
    identity_valid = False
    try:
        _require_code_identity(code, layout)
    except (FileNotFoundError, ValueError):
        pass
    else:
        identity_valid = True
    return {
        **code,
        "frozen_sync_receipt": (
            _sync_receipt_valid(layout) if validate_frozen_sync and identity_valid else False
        ),
    }


def _unobserved_code_status() -> dict[str, Any]:
    required_files = dict.fromkeys(_REQUIRED_REGULAR_FILES, False)
    required_files[_VENV_PYTHON] = False
    return {
        "real_release_root": False,
        "head_commit": None,
        "commit_matches": False,
        "top_level_matches": False,
        "detached": False,
        "tracked_clean": False,
        "unexpected_untracked": None,
        "unexpected_ignored": None,
        "known_plugin_pycache": None,
        "required_files": required_files,
        "required_directories": dict.fromkeys(_REQUIRED_REAL_DIRECTORIES, False),
        "frozen_sync_receipt": False,
    }


def _require_code_identity(code: dict[str, Any], layout: ReleaseLayout) -> None:
    if not code["real_release_root"]:
        raise FileNotFoundError(f"release root is not a real directory: {layout.release_root}")
    if not code["top_level_matches"]:
        raise ValueError("release root is not the exact Git top-level checkout")
    if not code["commit_matches"]:
        raise ValueError("release HEAD does not match its commit-bound directory")
    if not code["detached"]:
        raise ValueError("release checkout must have detached HEAD")
    if not code["tracked_clean"]:
        raise ValueError("release checkout has tracked modifications")
    if code["unexpected_untracked"] is None or code["unexpected_ignored"] is None:
        raise ValueError("release checkout inventory could not be read")
    if code["unexpected_untracked"]:
        raise ValueError(
            "release checkout has unexpected untracked paths: "
            + ", ".join(code["unexpected_untracked"])
        )
    if code["unexpected_ignored"]:
        raise ValueError(
            "release checkout has unexpected ignored paths: "
            + ", ".join(code["unexpected_ignored"])
        )
    missing = [path for path, present in code["required_files"].items() if not present]
    if missing:
        raise FileNotFoundError(f"release is missing required files: {', '.join(missing)}")
    missing_directories = [
        path for path, present in code["required_directories"].items() if not present
    ]
    if missing_directories:
        raise FileNotFoundError(
            "release is missing required trees: " + ", ".join(missing_directories)
        )


def _release_boundary_status(
    layout: ReleaseLayout,
    *,
    release_fd: int | None = None,
) -> dict[str, dict[str, bool]]:
    release_root = layout.release_root if release_fd is None else _directory_fd_path(release_fd)
    secure_directories = {
        str(path): _secure_owner_directory(path)
        for path in (
            layout.data_root,
            layout.releases_root,
            layout.env_file.parent,
            layout.shared_root,
        )
    }
    if release_fd is None:
        secure_directories[str(layout.release_root)] = _owner_mode(
            layout.release_root,
            0o700,
            kind="directory",
        )
    else:
        metadata = os.fstat(release_fd)
        secure_directories[str(layout.release_root)] = bool(
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700
        )
    return {
        "stable_assets": {
            "env_file": _owner_mode(layout.env_file, 0o600, kind="file"),
            "runtime_root": _owner_mode(layout.runtime_root, 0o700, kind="directory"),
            "market_cache_root": _owner_mode(
                layout.market_cache_root,
                0o700,
                kind="directory",
            ),
        },
        "secure_directories": secure_directories,
        "critical_runtime_files": _critical_runtime_file_status(
            layout,
            release_fd=release_fd,
        ),
        "handoff_modes": _special_handoff_mode_status(
            layout,
            release_fd=release_fd,
        ),
        "bindings": {
            ".env": _link_matches(release_root / ".env", layout.env_file),
            "knowledge-base/runtime": _link_matches(
                release_root / "knowledge-base/runtime",
                layout.runtime_root,
            ),
            "knowledge-base/market-cache": _link_matches(
                release_root / "knowledge-base/market-cache",
                layout.market_cache_root,
            ),
        },
    }


def _current_pointer_status(layout: ReleaseLayout) -> dict[str, Any]:
    """Inspect ``current`` without following an invalid pointer or mutating it."""

    current = layout.current_link
    result: dict[str, Any] = {
        "exists": os.path.lexists(current),
        "is_symlink": current.is_symlink(),
        "target": None,
        "target_commit": None,
        "valid_commit_bound_target": False,
        "target_is_candidate": False,
        "target_release": None,
    }
    if not result["exists"] or not result["is_symlink"]:
        return result
    try:
        target = current.resolve(strict=True)
        releases_root = layout.releases_root.resolve(strict=True)
    except OSError:
        return result
    result["target"] = str(target)
    if target.parent != releases_root or _FULL_COMMIT.fullmatch(target.name) is None:
        return result
    result["target_commit"] = target.name
    target_layout = ReleaseLayout(home=layout.home, commit=target.name)
    try:
        result["target_is_candidate"] = target == layout.release_root.resolve(strict=True)
    except OSError:
        result["target_is_candidate"] = False
    target_release = (
        _release_status(target_layout)
        if result["target_is_candidate"]
        else _previous_release_status(layout, target_layout)
    )
    result["target_release"] = target_release
    result["valid_commit_bound_target"] = bool(target_release["ready"])
    return result


def _release_status(layout: ReleaseLayout) -> dict[str, Any]:
    """Inspect one commit-bound release without recursively inspecting ``current``."""

    boundary = _release_boundary_status(layout)
    stable_assets = boundary["stable_assets"]
    secure_directories = boundary["secure_directories"]
    critical_runtime_files = boundary["critical_runtime_files"]
    handoff_modes = boundary["handoff_modes"]
    bindings = boundary["bindings"]
    trusted_bootstrap = bool(
        secure_directories.get(str(layout.release_root), False)
        and all(critical_runtime_files.values())
    )
    code = _code_status(layout) if trusted_bootstrap else _unobserved_code_status()
    ready = all(
        (
            code["real_release_root"],
            code["commit_matches"],
            code["top_level_matches"],
            code["detached"],
            code["tracked_clean"],
            code["unexpected_untracked"] == (),
            code["unexpected_ignored"] == (),
            code["frozen_sync_receipt"],
            all(code["required_files"].values()),
            all(code["required_directories"].values()),
            all(stable_assets.values()),
            all(secure_directories.values()),
            all(critical_runtime_files.values()),
            all(handoff_modes.values()),
            all(bindings.values()),
        )
    )
    return {
        "ready": ready,
        "commit": layout.commit,
        "release_root": str(layout.release_root),
        "code": code,
        "stable_assets": stable_assets,
        "secure_directories": secure_directories,
        "critical_runtime_files": critical_runtime_files,
        "handoff_modes": handoff_modes,
        "bindings": bindings,
    }


def _versioned_check_snapshot(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    *,
    previous_fd: int | None = None,
    releases_fd: int | None = None,
) -> dict[str, object]:
    if (previous_fd is None) != (releases_fd is None):
        raise ValueError("prior release generation descriptors are incomplete")
    current_metadata = previous.current_link.lstat()
    if not stat.S_ISLNK(current_metadata.st_mode) or current_metadata.st_uid != os.geteuid():
        raise PermissionError("current release pointer must be an owner-controlled symlink")
    if previous.current_link.resolve(strict=True) != previous.release_root.resolve(strict=True):
        raise RuntimeError("current release pointer changed before prior-release validation")
    previous_identity: object
    checker_identity: object
    checker_sha256: object
    receipt_identity: object
    receipt_sha256: object
    previous_generation: tuple[int, ...] | None = None
    releases_generation: tuple[int, ...] | None = None
    if previous_fd is None:
        previous_identity = _path_identity(previous.release_root)
        checker_identity = _path_identity(previous.release_root / _VERSIONED_CHECKER)
        checker_sha256 = _sha256_file(previous.release_root / _VERSIONED_CHECKER)
        receipt_identity = _path_identity(previous.release_root / _SYNC_RECEIPT)
        receipt_sha256 = _sha256_file(previous.release_root / _SYNC_RECEIPT)
    else:
        assert releases_fd is not None
        previous_identity = _stat_identity(os.fstat(previous_fd))
        previous_generation = _directory_generation_stamp(os.fstat(previous_fd))
        releases_generation = _directory_generation_stamp(os.fstat(releases_fd))
        if (
            _path_identity(previous.release_root) != previous_identity
            or _directory_generation_stamp(previous.release_root.lstat()) != previous_generation
            or _directory_generation_stamp(previous.releases_root.lstat()) != releases_generation
        ):
            raise RuntimeError("prior release root changed before versioned validation")
        checker_snapshot = _release_file_snapshot(previous_fd, _VERSIONED_CHECKER)
        receipt_snapshot = _release_file_snapshot(previous_fd, _SYNC_RECEIPT)
        checker_identity = checker_snapshot["identity"]
        checker_sha256 = checker_snapshot["sha256"]
        receipt_identity = receipt_snapshot["identity"]
        receipt_sha256 = receipt_snapshot["sha256"]
    snapshot: dict[str, object] = {
        "current_identity": _path_identity(previous.current_link),
        "current_target": os.readlink(previous.current_link),
        "candidate_identity": _path_identity(candidate.release_root),
        "previous_identity": previous_identity,
        "checker_identity": checker_identity,
        "checker_sha256": checker_sha256,
        "receipt_identity": receipt_identity,
        "receipt_sha256": receipt_sha256,
    }
    if previous_generation is not None and releases_generation is not None:
        snapshot["previous_generation"] = previous_generation
        snapshot["releases_generation"] = releases_generation
    return snapshot


def _require_versioned_previous_baseline(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    *,
    previous_fd: int | None = None,
) -> None:
    _require_prepared_assets(candidate)
    _require_secure_directories(previous, release_fd=previous_fd)
    if previous_fd is None:
        checker = previous.release_root / _VERSIONED_CHECKER
        if not _owner_controlled_regular_file(checker):
            raise PermissionError("prior release checker must be an owner-controlled regular file")
    else:
        _release_file_snapshot(previous_fd, _VERSIONED_CHECKER)
    # The prior release owns its own required-file contract.  Do not apply the
    # candidate's current ``_REQUIRED_REGULAR_FILES`` here: a release cutover
    # is precisely where tracked runtime files may be renamed or retired.  The
    # sealed prior checker below validates the complete historical contract;
    # this pre-check only needs the checker itself plus the common filesystem
    # boundary so it can be executed safely.
    code = _git_identity_status(
        previous,
        required_regular_files=(_VERSIONED_CHECKER,),
        required_real_directories=(),
        release_fd=previous_fd,
    )
    _require_code_identity(code, previous)
    if not _critical_runtime_file_status(previous, release_fd=previous_fd)[_VERSIONED_CHECKER]:
        raise PermissionError("prior release checker must be tracked and index-safe")
    if previous_fd is None:
        receipt_owner_mode = _owner_mode(
            previous.release_root / _SYNC_RECEIPT,
            0o600,
            kind="file",
        )
    else:
        receipt_fd = _open_regular_fd(_SYNC_RECEIPT, dir_fd=previous_fd)
        try:
            receipt_owner_mode = stat.S_IMODE(os.fstat(receipt_fd).st_mode) == 0o600
        finally:
            os.close(receipt_fd)
    if not receipt_owner_mode:
        raise PermissionError("prior release receipt must be an owner-only 0600 file")
    boundary = _release_boundary_status(
        previous,
        release_fd=previous_fd,
    )
    if not all(
        value
        for group_name in ("stable_assets", "secure_directories", "bindings")
        for value in boundary[group_name].values()
    ):
        raise RuntimeError("prior release violates the common release boundary")


def _run_versioned_previous_check(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    *,
    previous_fd: int | None = None,
    expected_checker_sha256: str,
) -> dict[str, Any]:
    interpreter = candidate.release_root / _VENV_PYTHON
    previous_root = (
        previous.release_root if previous_fd is None else _directory_fd_path(previous_fd)
    )
    checker = previous_root / _VERSIONED_CHECKER
    if previous_fd is None:
        checker_descriptor = _open_regular_fd(str(checker), dir_fd=None)
    else:
        checker_parts = Path(_VERSIONED_CHECKER).parts
        checker_parent_fd = _open_descendant_directory_fd(
            previous_fd,
            checker_parts[:-1],
        )
        try:
            checker_descriptor = _open_regular_fd(
                checker_parts[-1],
                dir_fd=checker_parent_fd,
            )
        finally:
            os.close(checker_parent_fd)
    try:
        checker_before = os.fstat(checker_descriptor)
        checker_source = _read_bounded_versioned_checker_source(checker_descriptor)
        checker_after = os.fstat(checker_descriptor)
    finally:
        os.close(checker_descriptor)
    checker_source_sha256 = hashlib.sha256(checker_source).hexdigest()
    if (
        _directory_generation_stamp(checker_before) != _directory_generation_stamp(checker_after)
        or checker_source_sha256 != expected_checker_sha256
    ):
        raise RuntimeError("prior release checker changed before sealed execution")
    committed_checker = _run_git(
        previous_root,
        "rev-parse",
        "--verify",
        f"{previous.commit}:{_VERSIONED_CHECKER}",
        root_fd=previous_fd,
    )
    committed_checker_oid = committed_checker.stdout.strip()
    checker_blob = hashlib.sha1(usedforsecurity=False)
    checker_blob.update(f"blob {len(checker_source)}\0".encode())
    checker_blob.update(checker_source)
    if (
        committed_checker.returncode != 0
        or _FULL_COMMIT.fullmatch(committed_checker_oid) is None
        or checker_blob.hexdigest() != committed_checker_oid
    ):
        raise RuntimeError("prior release checker does not match its exact commit")
    with (
        _sealed_checker_source_fd(checker_source) as checker_source_fd,
        tempfile.TemporaryFile() as stdout,
        tempfile.TemporaryFile() as stderr,
    ):
        environment = {
            "GIT_CONFIG_COUNT": "3",
            "HOME": str(candidate.home),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_KEY_1": "core.hooksPath",
            "GIT_CONFIG_KEY_2": "core.untrackedCache",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_VALUE_1": "/dev/null",
            "GIT_CONFIG_VALUE_2": "false",
            "GIT_OPTIONAL_LOCKS": "0",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        process = subprocess.Popen(
            (
                str(interpreter),
                "-I",
                "-B",
                "-c",
                _VERSIONED_CHECK_BOOTSTRAP,
                str(checker),
                str(checker_source_fd),
                "check",
                "--home",
                str(candidate.home),
                "--commit",
                previous.commit,
            ),
            cwd=previous_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            preexec_fn=_limit_versioned_check_output,
            start_new_session=True,
            pass_fds=(
                (checker_source_fd,) if previous_fd is None else (previous_fd, checker_source_fd)
            ),
        )
        try:
            return_code = process.wait(timeout=_VERSIONED_CHECK_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise RuntimeError("prior release versioned check timed out") from None
        stdout_size = os.fstat(stdout.fileno()).st_size
        stderr_size = os.fstat(stderr.fileno()).st_size
        if (
            stdout_size > _VERSIONED_CHECK_MAX_OUTPUT_BYTES
            or stderr_size > _VERSIONED_CHECK_MAX_OUTPUT_BYTES
        ):
            raise RuntimeError("prior release versioned check exceeded its output bound")
        stdout.seek(0)
        stderr.seek(0)
        raw_stdout = stdout.read()
        raw_stderr = stderr.read()
    if return_code != 0 or raw_stderr:
        raise RuntimeError("prior release versioned check failed")
    try:
        payload = json.loads(raw_stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("prior release versioned check returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise RuntimeError("prior release versioned check did not return an object")
    return payload


def _limit_versioned_check_output() -> None:
    configured_limit = _VERSIONED_CHECK_MAX_OUTPUT_BYTES
    _, inherited_hard_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    effective_limit = (
        configured_limit
        if inherited_hard_limit == resource.RLIM_INFINITY
        else min(configured_limit, inherited_hard_limit)
    )
    resource.setrlimit(resource.RLIMIT_FSIZE, (effective_limit, effective_limit))


def _require_versioned_check_identity(
    payload: dict[str, Any],
    previous: ReleaseLayout,
) -> None:
    pointer = payload.get("current_pointer")
    target_release = pointer.get("target_release") if isinstance(pointer, dict) else None
    expected_root = str(previous.release_root)
    if not (
        payload.get("ready") is True
        and payload.get("commit") == previous.commit
        and payload.get("release_root") == expected_root
        and payload.get("current") == str(previous.current_link)
        and isinstance(pointer, dict)
        and pointer.get("exists") is True
        and pointer.get("is_symlink") is True
        and pointer.get("target") == expected_root
        and pointer.get("target_commit") == previous.commit
        and pointer.get("valid_commit_bound_target") is True
        and pointer.get("target_is_candidate") is True
        and isinstance(target_release, dict)
        and target_release.get("ready") is True
        and target_release.get("commit") == previous.commit
        and target_release.get("release_root") == expected_root
    ):
        raise RuntimeError("prior release versioned check returned mismatched identity")


def _validate_versioned_previous_release(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    *,
    previous_fd: int | None = None,
    releases_fd: int | None = None,
) -> tuple[dict[str, Any], dict[str, object]]:
    if (previous_fd is None) != (releases_fd is None):
        raise ValueError("prior release generation descriptors are incomplete")
    if previous_fd is None:
        _require_versioned_previous_baseline(candidate, previous)
        before = _versioned_check_snapshot(candidate, previous)
        expected_checker_sha256 = before.get("checker_sha256")
        if not isinstance(expected_checker_sha256, str):
            raise RuntimeError("prior release checker snapshot is invalid")
        payload = _run_versioned_previous_check(
            candidate,
            previous,
            expected_checker_sha256=expected_checker_sha256,
        )
    else:
        _require_versioned_previous_baseline(
            candidate,
            previous,
            previous_fd=previous_fd,
        )
        before = _versioned_check_snapshot(
            candidate,
            previous,
            previous_fd=previous_fd,
            releases_fd=releases_fd,
        )
        expected_checker_sha256 = before.get("checker_sha256")
        if not isinstance(expected_checker_sha256, str):
            raise RuntimeError("prior release checker snapshot is invalid")
        payload = _run_versioned_previous_check(
            candidate,
            previous,
            previous_fd=previous_fd,
            expected_checker_sha256=expected_checker_sha256,
        )
    _require_versioned_check_identity(payload, previous)
    if previous_fd is None:
        _require_versioned_previous_baseline(candidate, previous)
        after = _versioned_check_snapshot(candidate, previous)
    else:
        _require_versioned_previous_baseline(
            candidate,
            previous,
            previous_fd=previous_fd,
        )
        after = _versioned_check_snapshot(
            candidate,
            previous,
            previous_fd=previous_fd,
            releases_fd=releases_fd,
        )
    if before != after:
        raise RuntimeError("release identity changed during prior-release validation")
    return payload, after


def _immutable_previous_readiness_snapshot(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    *,
    prior_current_target: str,
) -> dict[str, Any]:
    """Capture version-neutral filesystem identity for an accepted prior release."""

    receipt_path = previous.release_root / _SYNC_RECEIPT
    receipt_payload = _read_sync_receipt(previous)
    if receipt_payload is None:
        raise RuntimeError("prior frozen-sync receipt is unavailable")
    code = _git_identity_status(
        previous,
        required_regular_files=(_VERSIONED_CHECKER,),
        required_real_directories=(),
    )
    boundary = _release_boundary_status(previous)

    def canonical(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    return {
        "candidate_release_identity": list(_path_identity(candidate.release_root)),
        "prior_release_identity": list(_path_identity(previous.release_root)),
        "prior_current_target_before_drain": prior_current_target,
        "prior_checker_identity": list(_path_identity(previous.release_root / _VERSIONED_CHECKER)),
        "prior_checker_sha256": _sha256_file(previous.release_root / _VERSIONED_CHECKER),
        "prior_receipt_identity": list(_path_identity(receipt_path)),
        "prior_receipt_sha256": _sha256_file(receipt_path),
        "prior_receipt_payload_sha256": hashlib.sha256(canonical(receipt_payload)).hexdigest(),
        "prior_receipt_schema_version": receipt_payload.get("schema_version"),
        "prior_receipt_commit": receipt_payload.get("commit"),
        "prior_uv_lock_sha256": receipt_payload.get("uv_lock_sha256"),
        "prior_venv_sha256": receipt_payload.get("venv_sha256"),
        "prior_python_identity": receipt_payload.get("python_identity"),
        "prior_runtime_imports": receipt_payload.get("runtime_imports"),
        "prior_handoff_binding_sha256": receipt_payload.get("handoff_binding_sha256"),
        "prior_receipt_publication_identity": receipt_payload.get("publication_identity"),
        "prior_git_identity_sha256": hashlib.sha256(canonical(code)).hexdigest(),
        "prior_release_boundary_sha256": hashlib.sha256(canonical(boundary)).hexdigest(),
    }


def capture_previous_release_readiness(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
) -> dict[str, Any]:
    """Run the prior release's own checker and freeze a rollback proof.

    The prior checker is the contract owner, so this remains valid when the
    prior release predates the current frozen-sync schema.  The returned
    snapshot is deliberately sufficient for a later read-only comparison and
    never needs to call ``record_frozen_sync``.
    """

    if candidate.commit == previous.commit:
        raise ValueError("candidate and prior release commits must differ")
    with _release_lock(candidate):
        payload, versioned_snapshot = _validate_versioned_previous_release(
            candidate,
            previous,
        )
        prior_current_target = versioned_snapshot.get("current_target")
        if not isinstance(prior_current_target, str):
            raise RuntimeError("prior release current target is not recorded")
        immutable = _immutable_previous_readiness_snapshot(
            candidate,
            previous,
            prior_current_target=prior_current_target,
        )
        payload_digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return {
            "schema_version": "fin.previous-release-readiness/v1",
            "ready": True,
            "candidate_commit": candidate.commit,
            "prior_commit": previous.commit,
            "prior_check_payload_sha256": payload_digest,
            "prior_check_release_root": str(previous.release_root),
            "immutable_snapshot": immutable,
        }


def verify_recorded_previous_release_readiness(
    snapshot: Mapping[str, Any],
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
) -> dict[str, Any]:
    """Verify a frozen prior proof without running its checker or writing state."""

    if (
        snapshot.get("schema_version") != "fin.previous-release-readiness/v1"
        or snapshot.get("ready") is not True
        or snapshot.get("candidate_commit") != candidate.commit
        or snapshot.get("prior_commit") != previous.commit
    ):
        raise RuntimeError("recorded prior release readiness identity is invalid")
    recorded = snapshot.get("immutable_snapshot")
    if not isinstance(recorded, dict):
        raise RuntimeError("recorded prior release readiness snapshot is invalid")
    with _release_lock(candidate):
        _require_versioned_previous_baseline(candidate, previous)
        prior_current_target = recorded.get("prior_current_target_before_drain")
        if not isinstance(prior_current_target, str):
            raise RuntimeError("recorded prior release current target is invalid")
        current = _immutable_previous_readiness_snapshot(
            candidate,
            previous,
            prior_current_target=prior_current_target,
        )
        if current != recorded:
            raise RuntimeError("prior release readiness drifted after preflight")
    return {
        "schema_version": "fin.previous-release-readiness-verification/v1",
        "ready": True,
        "candidate_commit": candidate.commit,
        "prior_commit": previous.commit,
        "immutable_snapshot_sha256": hashlib.sha256(
            json.dumps(
                recorded,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }


def _previous_release_status(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
) -> dict[str, Any]:
    try:
        _validate_versioned_previous_release(candidate, previous)
    except (OSError, RuntimeError, ValueError):
        ready = False
    else:
        ready = True
    return {
        "ready": ready,
        "commit": previous.commit,
        "release_root": str(previous.release_root),
        "contract_owner": "target_release",
        "check_protocol": "prepare_fin_release/check",
    }


@contextmanager
def locked_release(layout: ReleaseLayout) -> Iterator[None]:
    """Hold the owner-only release lock without reinterpreting release schema.

    Cross-version cutover preflight uses this narrow seam while it snapshots
    immutable historical artifacts.  It deliberately does not call the
    current release's readiness validator because an accepted prior release
    may own an older frozen-sync schema.
    """

    with _release_lock(layout):
        yield


def _release_lock_metadata_stamp(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@contextmanager
def locked_release_read_only(layout: ReleaseLayout) -> Iterator[None]:
    """Hold the existing release lock without creating or modifying filesystem state."""

    if not _secure_owner_directory(layout.data_root):
        raise PermissionError(f"expected secure owner-controlled directory: {layout.data_root}")
    lock_path = layout.data_root / ".release.lock"
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, flags)
        metadata = os.fstat(descriptor)
        installed = lock_path.lstat()
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise PermissionError(
            "an existing FIN release lock is required for read-only locking"
        ) from error
    identity = _release_lock_metadata_stamp(metadata)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or identity != _release_lock_metadata_stamp(installed)
    ):
        os.close(descriptor)
        raise PermissionError("existing FIN release lock must be an owner-only 0600 file")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        try:
            locked_metadata = os.fstat(descriptor)
            locked_installed = lock_path.lstat()
        except OSError:
            raise PermissionError(
                "existing FIN release lock changed while acquiring shared lock"
            ) from None
        if (
            _release_lock_metadata_stamp(locked_metadata) != identity
            or _release_lock_metadata_stamp(locked_installed) != identity
        ):
            raise PermissionError("existing FIN release lock changed while acquiring shared lock")
        yield
    finally:
        os.close(descriptor)


@contextmanager
def locked_ready_release(layout: ReleaseLayout) -> Iterator[dict[str, Any]]:
    """Hold the release lock while one fully-ready candidate is executed.

    The lock is shared with prepare/activate.  Full readiness and the release
    directory identity are checked on both sides of the caller's operation so
    evidence cannot be emitted for a candidate that drifted mid-run.
    """

    with _release_lock(layout):
        try:
            before_identity = layout.release_root.lstat()
        except OSError as error:
            raise RuntimeError("candidate release is not fully ready") from error
        before = _release_status(layout)
        if before.get("ready") is not True:
            raise RuntimeError("candidate release is not fully ready")
        yield before
        try:
            after_identity = layout.release_root.lstat()
        except OSError as error:
            raise RuntimeError("candidate release changed while locked") from error
        after = _release_status(layout)
        if (before_identity.st_dev, before_identity.st_ino) != (
            after_identity.st_dev,
            after_identity.st_ino,
        ) or after.get("ready") is not True:
            raise RuntimeError("candidate release changed while locked")


def inspect_release(layout: ReleaseLayout) -> dict[str, Any]:
    """Inspect one release and prove any existing ``current`` target is deployable."""

    return {
        **_release_status(layout),
        "current": str(layout.current_link),
        "current_pointer": _current_pointer_status(layout),
    }


def inspect_candidate_release(layout: ReleaseLayout) -> dict[str, Any]:
    """Inspect only one candidate release without consulting ``current``.

    This read-only seam is for preflight callers that must neither create the
    release lock nor execute a previous release's versioned checker.  All Git
    commands used by the underlying status path disable optional locks.
    """

    return _release_status(layout)


def _require_prepared_assets(layout: ReleaseLayout) -> None:
    _require_secure_directories(layout)
    _require_critical_runtime_files(layout)
    code = _code_status(layout)
    _require_code_identity(code, layout)
    if not code["frozen_sync_receipt"]:
        raise ValueError("release frozen-sync receipt is missing, corrupt or stale")
    if not _owner_mode(layout.env_file, 0o600, kind="file"):
        raise PermissionError("stable FIN environment file must be an owner-only 0600 file")
    for path in (layout.runtime_root, layout.market_cache_root):
        if not _owner_mode(path, 0o700, kind="directory"):
            raise PermissionError(
                f"stable FIN data root must be an owner-only 0700 directory: {path}"
            )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _directory_generation_stamp(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_directory_fd(
    path: str | Path,
    *,
    dir_fd: int | None = None,
    owner_only: bool = False,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, dir_fd=dir_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise PermissionError("directory descriptor is not owner-controlled")
        if owner_only and stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PermissionError("directory descriptor must have mode 0700")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _duplicate_owner_only_directory_fd(descriptor: int) -> int:
    duplicate = os.dup(descriptor)
    try:
        metadata = os.fstat(duplicate)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise PermissionError("directory descriptor must be owner-controlled mode 0700")
        return duplicate
    except BaseException:
        os.close(duplicate)
        raise


def _directory_fd_path(descriptor: int) -> Path:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise PermissionError("directory descriptor is not owner-controlled")
    path = Path("/proc/self/fd") / str(descriptor)
    if _stat_identity(path.stat()) != _stat_identity(metadata):
        raise RuntimeError("directory descriptor path binding changed")
    return path


def _open_regular_fd(name: str, *, dir_fd: int | None) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=dir_fd)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise PermissionError("file descriptor is not owner-controlled and single-link")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _special_handoff_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _require_special_handoff_metadata(metadata: os.stat_result, *, kind: str) -> None:
    owner = os.geteuid()
    if metadata.st_uid != owner:
        raise PermissionError("special handoff target is not owner-controlled")
    if kind == "file":
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PermissionError("special handoff file must be a single-link regular file")
        return
    if kind == "directory":
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_nlink < 2:
            raise PermissionError("special handoff directory is invalid")
        return
    raise ValueError("unsupported special handoff target kind")


def _special_handoff_metadata_stamp(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _receipt_publication_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return _special_handoff_metadata_stamp(metadata)[:6]


def _receipt_cleanup_capability_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
    )


def _receipt_invalidation_capability_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_uid,
    )


def _open_special_handoff_parent_fd(
    release_fd: int,
    parts: Sequence[str],
) -> tuple[int, tuple[tuple[int, int, int, int, int, int, int, int], ...]]:
    current = os.dup(release_fd)
    ancestor_stamps: list[tuple[int, int, int, int, int, int, int, int]] = []
    try:
        for part in parts:
            if not part or part in {".", ".."} or "/" in part:
                raise ValueError("unsafe special handoff ancestor component")
            following = _open_directory_fd(part, dir_fd=current)
            try:
                metadata = os.fstat(following)
                _require_special_handoff_metadata(metadata, kind="directory")
                ancestor_stamps.append(_special_handoff_metadata_stamp(metadata))
            except BaseException:
                os.close(following)
                raise
            os.close(current)
            current = following
        return current, tuple(ancestor_stamps)
    except BaseException:
        os.close(current)
        raise


@contextmanager
def _special_handoff_mode_bindings(
    layout: ReleaseLayout,
    *,
    targets: Sequence[tuple[str, str, int]] = _SPECIAL_HANDOFF_MODE_TARGETS,
    release_fd: int | None = None,
) -> Iterator[tuple[_SpecialHandoffModeBinding, ...]]:
    descriptors: list[int] = []
    bindings: list[_SpecialHandoffModeBinding] = []
    try:
        try:
            bound_release_fd = (
                _open_directory_fd(layout.release_root, owner_only=True)
                if release_fd is None
                else _duplicate_owner_only_directory_fd(release_fd)
            )
            descriptors.append(bound_release_fd)
            release_root_stamp = _special_handoff_metadata_stamp(os.fstat(bound_release_fd))
            for relative, kind, expected_mode in targets:
                parts = Path(relative).parts
                parent_fd, ancestor_stamps = _open_special_handoff_parent_fd(
                    bound_release_fd,
                    parts[:-1],
                )
                descriptors.append(parent_fd)
                if kind == "file":
                    descriptor = _open_regular_fd(parts[-1], dir_fd=parent_fd)
                else:
                    descriptor = _open_directory_fd(parts[-1], dir_fd=parent_fd)
                descriptors.append(descriptor)
                descriptor_metadata = os.fstat(descriptor)
                path_metadata = os.stat(
                    parts[-1],
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                _require_special_handoff_metadata(descriptor_metadata, kind=kind)
                _require_special_handoff_metadata(path_metadata, kind=kind)
                if _special_handoff_metadata_stamp(
                    descriptor_metadata
                ) != _special_handoff_metadata_stamp(path_metadata):
                    raise PermissionError("special handoff target binding changed")
                bindings.append(
                    _SpecialHandoffModeBinding(
                        relative=relative,
                        kind=kind,
                        expected_mode=expected_mode,
                        parent_fd=parent_fd,
                        descriptor=descriptor,
                        release_root_stamp=release_root_stamp,
                        ancestor_stamps=ancestor_stamps,
                        identity=_special_handoff_identity(descriptor_metadata),
                        content_sha256=_special_handoff_content_sha256(
                            descriptor=descriptor,
                            kind=kind,
                            relative=relative,
                        ),
                        initial_mode=stat.S_IMODE(descriptor_metadata.st_mode),
                    )
                )
        except (OSError, ValueError) as error:
            raise PermissionError("release special handoff targets failed preflight") from error
        yield tuple(bindings)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _require_special_handoff_binding(
    binding: _SpecialHandoffModeBinding,
    *,
    exact_mode: bool,
) -> None:
    try:
        descriptor_metadata = os.fstat(binding.descriptor)
        path_metadata = os.stat(
            Path(binding.relative).name,
            dir_fd=binding.parent_fd,
            follow_symlinks=False,
        )
        _require_special_handoff_metadata(descriptor_metadata, kind=binding.kind)
        _require_special_handoff_metadata(path_metadata, kind=binding.kind)
        if (
            _special_handoff_metadata_stamp(descriptor_metadata)
            != _special_handoff_metadata_stamp(path_metadata)
            or _special_handoff_identity(descriptor_metadata) != binding.identity
            or (
                binding.content_sha256 is not None
                and _special_handoff_content_sha256(
                    descriptor=binding.descriptor,
                    kind=binding.kind,
                    relative=binding.relative,
                )
                != binding.content_sha256
            )
            or (exact_mode and stat.S_IMODE(descriptor_metadata.st_mode) != binding.expected_mode)
        ):
            raise PermissionError("release special handoff target changed")
    except OSError as error:
        raise PermissionError("release special handoff target changed") from error


def _require_special_handoff_canonical_bindings(
    layout: ReleaseLayout,
    bindings: tuple[_SpecialHandoffModeBinding, ...],
    *,
    targets: Sequence[tuple[str, str, int]] = _SPECIAL_HANDOFF_MODE_TARGETS,
    release_fd: int | None = None,
) -> None:
    try:
        with _special_handoff_mode_bindings(
            layout,
            targets=targets,
            release_fd=release_fd,
        ) as canonical_bindings:
            if len(canonical_bindings) != len(bindings):
                raise PermissionError("release special handoff canonical binding changed")
            for binding, canonical in zip(bindings, canonical_bindings, strict=True):
                _require_special_handoff_binding(canonical, exact_mode=True)
                if (
                    canonical.relative != binding.relative
                    or canonical.kind != binding.kind
                    or canonical.expected_mode != binding.expected_mode
                    or canonical.release_root_stamp != binding.release_root_stamp
                    or canonical.ancestor_stamps != binding.ancestor_stamps
                    or canonical.identity != binding.identity
                    or canonical.content_sha256 != binding.content_sha256
                ):
                    raise PermissionError("release special handoff canonical binding changed")
    except (OSError, ValueError) as error:
        raise PermissionError("release special handoff canonical binding changed") from error


def _special_handoff_binding_sha256(
    layout: ReleaseLayout,
    *,
    targets: Sequence[tuple[str, str, int]] = _SPECIAL_HANDOFF_MODE_TARGETS,
    release_fd: int | None = None,
) -> str:
    with _special_handoff_mode_bindings(
        layout,
        targets=targets,
        release_fd=release_fd,
    ) as bindings:
        for binding in bindings:
            _require_special_handoff_binding(binding, exact_mode=True)
        _require_special_handoff_canonical_bindings(
            layout,
            bindings,
            targets=targets,
            release_fd=release_fd,
        )
        payload = [
            {
                "path": binding.relative,
                "kind": binding.kind,
                "expected_mode": binding.expected_mode,
                "release_root": list(binding.release_root_stamp[:5]),
                "ancestors": [list(stamp[:5]) for stamp in binding.ancestor_stamps],
                "target_identity": list(binding.identity),
                "content_sha256": binding.content_sha256,
            }
            for binding in bindings
        ]
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _special_handoff_content_sha256(
    *,
    descriptor: int,
    kind: str,
    relative: str,
) -> str | None:
    if kind == "file":
        return _sha256_fd(descriptor)
    if kind != "directory" or relative != "hermes-migration/plugins/fin-consultation-first-tool":
        return None
    digest = hashlib.sha256()
    plugin_runtime_files = tuple(
        critical_relative
        for critical_relative in _CRITICAL_RUNTIME_FILES
        if Path(critical_relative).parent.as_posix() == relative
    )
    if not plugin_runtime_files:
        raise PermissionError("special handoff directory has no bound runtime files")
    for plugin_relative in plugin_runtime_files:
        name = Path(plugin_relative).name
        plugin_descriptor = _open_regular_fd(name, dir_fd=descriptor)
        try:
            content_sha256 = _sha256_fd(plugin_descriptor)
        finally:
            os.close(plugin_descriptor)
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(content_sha256))
        digest.update(b"\0")
    return digest.hexdigest()


def _converge_special_handoff_modes(layout: ReleaseLayout) -> dict[str, Any]:
    with _special_handoff_mode_bindings(layout) as bindings:
        changes = tuple(binding.initial_mode != binding.expected_mode for binding in bindings)
        for binding, changed in zip(bindings, changes, strict=True):
            if changed:
                os.fchmod(binding.descriptor, binding.expected_mode)
        for binding in bindings:
            _require_special_handoff_binding(binding, exact_mode=True)
        _require_special_handoff_canonical_bindings(layout, bindings)
        return {
            "changed": any(changes),
            "targets": [
                {"path": binding.relative, "changed": changed}
                for binding, changed in zip(bindings, changes, strict=True)
            ],
        }


def _special_handoff_mode_status(
    layout: ReleaseLayout,
    *,
    release_fd: int | None = None,
) -> dict[str, bool]:
    status = {relative: False for relative, _kind, _mode in _SPECIAL_HANDOFF_MODE_TARGETS}
    try:
        with _special_handoff_mode_bindings(
            layout,
            release_fd=release_fd,
        ) as bindings:
            for binding in bindings:
                _require_special_handoff_binding(binding, exact_mode=True)
            _require_special_handoff_canonical_bindings(
                layout,
                bindings,
                release_fd=release_fd,
            )
            for binding in bindings:
                status[binding.relative] = True
    except PermissionError:
        pass
    return status


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _open_descendant_directory_fd(root_fd: int, parts: Sequence[str]) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            if not part or part in {".", ".."} or "/" in part:
                raise ValueError("unsafe directory component")
            following = _open_directory_fd(part, dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


@contextmanager
def _quarantine_release_descriptors(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
) -> Iterator[_QuarantineReleaseDescriptors]:
    descriptors: list[int] = []
    try:
        data_fd = _open_directory_fd(candidate.data_root, owner_only=True)
        descriptors.append(data_fd)
        releases_fd = _open_directory_fd("releases", dir_fd=data_fd, owner_only=True)
        descriptors.append(releases_fd)
        candidate_fd = _open_directory_fd(
            candidate.commit,
            dir_fd=releases_fd,
            owner_only=True,
        )
        descriptors.append(candidate_fd)
        previous_fd = _open_directory_fd(
            previous.commit,
            dir_fd=releases_fd,
            owner_only=True,
        )
        descriptors.append(previous_fd)
        yield _QuarantineReleaseDescriptors(
            data_fd=data_fd,
            releases_fd=releases_fd,
            candidate_fd=candidate_fd,
            previous_fd=previous_fd,
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _release_file_snapshot(release_fd: int, relative: str) -> dict[str, object]:
    parts = Path(relative).parts
    parent_fd = _open_descendant_directory_fd(release_fd, parts[:-1])
    try:
        descriptor = _open_regular_fd(parts[-1], dir_fd=parent_fd)
        try:
            metadata = os.fstat(descriptor)
            return {
                "identity": _stat_identity(metadata),
                "sha256": _sha256_fd(descriptor),
            }
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _read_bounded_versioned_checker_source(descriptor: int) -> bytes:
    metadata = os.fstat(descriptor)
    if metadata.st_size > _VERSIONED_CHECKER_MAX_SOURCE_BYTES:
        raise ValueError("prior release checker exceeds its source byte bound")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(
            descriptor,
            min(
                64 * 1024,
                _VERSIONED_CHECKER_MAX_SOURCE_BYTES + 1 - total,
            ),
        )
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _VERSIONED_CHECKER_MAX_SOURCE_BYTES:
            raise ValueError("prior release checker exceeds its source byte bound")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


@contextmanager
def _sealed_checker_source_fd(source: bytes) -> Iterator[int]:
    libc = ctypes.CDLL(None, use_errno=True)
    memfd_create = getattr(libc, "memfd_create", None)
    if memfd_create is None:
        raise RuntimeError("sealed prior checker execution is unavailable")
    memfd_create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    memfd_create.restype = ctypes.c_int
    descriptor = memfd_create(
        b"fin-prior-release-checker",
        _MFD_CLOEXEC | _MFD_ALLOW_SEALING,
    )
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    try:
        remaining = memoryview(source)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(errno.EIO, "sealed prior checker write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        expected_seals = _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
        fcntl.fcntl(descriptor, _F_ADD_SEALS, expected_seals)
        observed_seals = fcntl.fcntl(descriptor, _F_GET_SEALS)
        if observed_seals & expected_seals != expected_seals:
            raise RuntimeError("prior checker source descriptor is not fully sealed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield descriptor
    finally:
        os.close(descriptor)


def _special_handoff_contract_from_checker_source(
    source: bytes,
) -> _SpecialHandoffModeContract:
    source_sha256 = hashlib.sha256(source).hexdigest()
    for expected_sha256, contract in _FROZEN_CHECKER_CONTRACTS_BY_SHA256:
        if source_sha256 == expected_sha256:
            return contract
    raise ValueError("prior release checker handoff contract is invalid")


def _frozen_special_handoff_contract(
    previous_fd: int,
    expected_checker: Mapping[str, object],
) -> _SpecialHandoffModeContract:
    expected_identity = expected_checker.get("identity")
    expected_sha256 = expected_checker.get("sha256")
    if not isinstance(expected_identity, tuple) or not isinstance(expected_sha256, str):
        raise ValueError("prior release checker snapshot is invalid")
    parts = Path(_VERSIONED_CHECKER).parts
    parent_fd = _open_descendant_directory_fd(previous_fd, parts[:-1])
    try:
        descriptor = _open_regular_fd(parts[-1], dir_fd=parent_fd)
        try:
            before = os.fstat(descriptor)
            path_before = os.stat(
                parts[-1],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            source = _read_bounded_versioned_checker_source(descriptor)
            source_sha256 = hashlib.sha256(source).hexdigest()
            contract = _special_handoff_contract_from_checker_source(source)
            after = os.fstat(descriptor)
            path_after = os.stat(
                parts[-1],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                _stat_identity(before) != expected_identity
                or _stat_identity(path_before) != expected_identity
                or _stat_identity(after) != expected_identity
                or _stat_identity(path_after) != expected_identity
                or source_sha256 != expected_sha256
                or _sha256_fd(descriptor) != expected_sha256
            ):
                raise RuntimeError("prior release checker changed while reading its contract")
            return contract
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _special_handoff_targets_for_contract(
    contract: _SpecialHandoffModeContract,
) -> tuple[tuple[str, str, int], ...]:
    if contract is _SpecialHandoffModeContract.LEGACY_V2_TWO_TARGET:
        return _LEGACY_V2_SPECIAL_HANDOFF_MODE_TARGETS
    if contract is _SpecialHandoffModeContract.CURRENT_V2_THREE_TARGET:
        return _V2_THREE_TARGET_SPECIAL_HANDOFF_MODE_TARGETS
    if contract is _SpecialHandoffModeContract.CURRENT_V2_FOUR_TARGET:
        return _SPECIAL_HANDOFF_MODE_TARGETS
    raise ValueError("unsupported special handoff mode contract")


def _candidate_operator_snapshot(
    candidate: ReleaseLayout,
    candidate_fd: int,
) -> dict[str, object]:
    checker_snapshot = _release_file_snapshot(candidate_fd, _VERSIONED_CHECKER)
    receipt_snapshot = _release_file_snapshot(candidate_fd, _SYNC_RECEIPT)
    checker_identity = checker_snapshot["identity"]
    try:
        running_operator = Path(__file__).resolve(strict=True)
        expected_operator = (candidate.release_root / _VERSIONED_CHECKER).resolve(strict=True)
        running_metadata = Path(__file__).lstat()
    except OSError as error:
        raise PermissionError("candidate operator identity is unavailable") from error
    if (
        running_operator != expected_operator
        or not isinstance(checker_identity, tuple)
        or (running_metadata.st_dev, running_metadata.st_ino)
        != (checker_identity[0], checker_identity[1])
    ):
        raise PermissionError("candidate operator must run from its exact release root")
    receipt_identity = receipt_snapshot["identity"]
    if not isinstance(receipt_identity, tuple) or stat.S_IMODE(receipt_identity[2]) != 0o600:
        raise PermissionError("candidate receipt must be an owner-only 0600 file")
    return {
        "release_identity": _stat_identity(os.fstat(candidate_fd)),
        "checker": checker_snapshot,
        "receipt": receipt_snapshot,
    }


def _runtime_bytecode_cache_relative(relative: str) -> str:
    path = Path(relative)
    parts = path.parts
    cache_indexes = tuple(index for index, part in enumerate(parts) if part == "__pycache__")
    if (
        path.is_absolute()
        or ".." in parts
        or len(cache_indexes) != 1
        or cache_indexes[0] != len(parts) - 2
    ):
        raise ValueError("runtime bytecode inventory must contain direct __pycache__ files")
    cache_parts = parts[: cache_indexes[0] + 1]
    allowed = any(
        cache_parts[: len(Path(root).parts)] == Path(root).parts
        for root in _RUNTIME_BYTECODE_ALLOWED_ROOTS
    )
    if not allowed:
        raise ValueError("runtime bytecode inventory escapes the fixed allowed roots")
    if path.suffix != ".pyc":
        raise ValueError("runtime bytecode inventory contains a non-pyc entry")
    return Path(*cache_parts).as_posix()


def _discover_runtime_bytecode_cache_directories(previous_fd: int) -> tuple[str, ...]:
    pending = [Path(root).parts for root in _RUNTIME_BYTECODE_ALLOWED_ROOTS]
    discovered: set[str] = set()
    scanned = 0
    while pending:
        parts = pending.pop()
        try:
            directory_fd = _open_descendant_directory_fd(previous_fd, parts)
        except FileNotFoundError:
            continue
        try:
            scanned += 1
            if scanned > _RUNTIME_BYTECODE_MAX_SCAN_DIRECTORIES:
                raise ValueError("runtime bytecode source tree exceeds the scan limit")
            for name in sorted(os.listdir(directory_fd)):
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                child_parts = (*parts, name)
                if name == "__pycache__":
                    discovered.add(Path(*child_parts).as_posix())
                elif stat.S_ISDIR(metadata.st_mode):
                    child_fd = _open_directory_fd(name, dir_fd=directory_fd)
                    try:
                        if _stat_identity(os.fstat(child_fd)) != _stat_identity(metadata):
                            raise RuntimeError(
                                "runtime bytecode source directory changed while scanning"
                            )
                    finally:
                        os.close(child_fd)
                    pending.append(child_parts)
        finally:
            os.close(directory_fd)
    return tuple(sorted(discovered))


def _collect_runtime_bytecode_cache(
    previous: ReleaseLayout,
    *,
    previous_fd: int,
    relative_cache: str,
    tracked_paths: set[str],
) -> _RuntimeBytecodeCache:
    cache_directory = previous.release_root / relative_cache
    cache_parts = Path(relative_cache).parts
    source_parent_fd = _open_descendant_directory_fd(previous_fd, cache_parts[:-1])
    try:
        cache_fd = _open_directory_fd(cache_parts[-1], dir_fd=source_parent_fd)
    except BaseException:
        os.close(source_parent_fd)
        raise
    try:
        cache_metadata = os.fstat(cache_fd)
        cache_mode = stat.S_IMODE(cache_metadata.st_mode)
        if cache_metadata.st_uid != os.geteuid() or cache_mode & 0o7002:
            raise PermissionError(
                "runtime bytecode cache directory must be owner-owned "
                "without special or other-write mode"
            )
        try:
            children = sorted(os.listdir(cache_fd))
        except OSError as error:
            raise ValueError("runtime bytecode cache could not be enumerated") from error
        if not children:
            raise ValueError("runtime bytecode cache must not be empty")
        if len(children) > _RUNTIME_BYTECODE_MAX_FILES:
            raise ValueError("runtime bytecode inventory exceeds the file-count limit")
        entries: list[_RuntimeBytecodeFile] = []
        total_bytes = 0
        for name in children:
            relative_path = (Path(relative_cache) / name).as_posix()
            metadata = os.stat(name, dir_fd=cache_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
            ):
                raise PermissionError(
                    "runtime bytecode entries must be owner-owned single-link regular files"
                )
            mode = stat.S_IMODE(metadata.st_mode)
            if mode & 0o7113:
                raise PermissionError("runtime bytecode entries have an unsafe mode")
            if Path(name).suffix != ".pyc":
                raise ValueError("runtime bytecode inventory contains a non-pyc entry")
            total_bytes += metadata.st_size
            if total_bytes > _RUNTIME_BYTECODE_MAX_BYTES:
                raise ValueError("runtime bytecode inventory exceeds the byte limit")
            bytecode_fd = _open_regular_fd(name, dir_fd=cache_fd)
            try:
                bytecode_metadata = os.fstat(bytecode_fd)
                if _stat_identity(bytecode_metadata) != _stat_identity(metadata):
                    raise RuntimeError("runtime bytecode entry changed while opening")
                bytecode_sha256 = _sha256_fd(bytecode_fd)
            finally:
                os.close(bytecode_fd)
            bytecode_path = cache_directory / name
            try:
                source = Path(importlib.util.source_from_cache(str(bytecode_path)))
                source_relative = source.relative_to(previous.release_root).as_posix()
            except (ValueError, NotImplementedError):
                raise ValueError("runtime bytecode entry has no canonical source mapping") from None
            if source.parent != cache_directory.parent or source_relative not in tracked_paths:
                raise ValueError("runtime bytecode entry is not bound to a tracked sibling source")
            source_fd = _open_regular_fd(source.name, dir_fd=source_parent_fd)
            os.close(source_fd)
            entries.append(
                _RuntimeBytecodeFile(
                    name=name,
                    relative_path=relative_path,
                    source_relative_path=source_relative,
                    size=metadata.st_size,
                    mode=mode,
                    sha256=bytecode_sha256,
                    identity=_stat_identity(metadata),
                )
            )
    except BaseException:
        os.close(cache_fd)
        os.close(source_parent_fd)
        raise
    return _RuntimeBytecodeCache(
        cache_directory=cache_directory,
        relative_cache_directory=relative_cache,
        cache_identity=_stat_identity(cache_metadata),
        files=tuple(entries),
        total_bytes=total_bytes,
        source_parent_fd=source_parent_fd,
        cache_fd=cache_fd,
        cache_name=cache_parts[-1],
    )


def _collect_runtime_bytecode_inventory(
    previous: ReleaseLayout,
    *,
    previous_fd: int,
    ignored_paths: Sequence[str],
    discovered_cache_directories: Sequence[str],
) -> _RuntimeBytecodeInventory:
    ignored = tuple(sorted(ignored_paths))
    if not ignored:
        raise ValueError("active prior release has no runtime bytecode to quarantine")
    cache_directories = tuple(
        sorted({_runtime_bytecode_cache_relative(relative) for relative in ignored})
    )
    if tuple(sorted(discovered_cache_directories)) != cache_directories:
        raise ValueError(
            "runtime bytecode inventory does not exactly bind discovered __pycache__ directories"
        )
    tracked = _git_paths(
        _directory_fd_path(previous_fd),
        "ls-files",
        root_fd=previous_fd,
    )
    if tracked is None:
        raise ValueError("prior tracked source inventory could not be read")
    tracked_paths = set(tracked)
    caches: list[_RuntimeBytecodeCache] = []
    try:
        for relative_cache in cache_directories:
            caches.append(
                _collect_runtime_bytecode_cache(
                    previous,
                    previous_fd=previous_fd,
                    relative_cache=relative_cache,
                    tracked_paths=tracked_paths,
                )
            )
        files = tuple(entry for cache in caches for entry in cache.files)
        if len(files) > _RUNTIME_BYTECODE_MAX_FILES:
            raise ValueError("runtime bytecode inventory exceeds the file-count limit")
        total_bytes = sum(cache.total_bytes for cache in caches)
        if total_bytes > _RUNTIME_BYTECODE_MAX_BYTES:
            raise ValueError("runtime bytecode inventory exceeds the byte limit")
        if {entry.relative_path for entry in files} != set(ignored):
            raise ValueError("unexpected ignored inventory is not exact runtime bytecode")
    except BaseException:
        for cache in caches:
            os.close(cache.cache_fd)
            os.close(cache.source_parent_fd)
        raise
    payload = {
        "schema_version": _RUNTIME_BYTECODE_INVENTORY_SCHEMA,
        "prior_commit": previous.commit,
        "cache_directories": list(cache_directories),
        "files": [
            {
                "path": entry.relative_path,
                "source": entry.source_relative_path,
                "size": entry.size,
                "mode": entry.mode,
                "sha256": entry.sha256,
            }
            for entry in files
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _RuntimeBytecodeInventory(
        caches=tuple(caches),
        files=files,
        inventory_sha256=hashlib.sha256(canonical).hexdigest(),
        total_bytes=total_bytes,
    )


def _require_runtime_bytecode_prior_boundary(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    *,
    previous_fd: int,
) -> _RuntimeBytecodeInventory | None:
    _require_prepared_assets(candidate)
    _require_secure_directories(previous, release_fd=previous_fd)
    _release_file_snapshot(previous_fd, _VERSIONED_CHECKER)
    code = _git_identity_status(
        previous,
        required_regular_files=(_VERSIONED_CHECKER,),
        required_real_directories=(),
        release_fd=previous_fd,
        allow_plugin_pycache=False,
    )
    unexpected_ignored = code.get("unexpected_ignored")
    if not isinstance(unexpected_ignored, tuple):
        raise ValueError("prior ignored inventory could not be read")
    discovered_cache_directories = _discover_runtime_bytecode_cache_directories(previous_fd)
    if not unexpected_ignored and discovered_cache_directories:
        raise ValueError("runtime bytecode cache is not represented by the ignored inventory")
    inventory = (
        None
        if not unexpected_ignored
        else _collect_runtime_bytecode_inventory(
            previous,
            previous_fd=previous_fd,
            ignored_paths=unexpected_ignored,
            discovered_cache_directories=discovered_cache_directories,
        )
    )
    try:
        _require_code_identity(
            code if inventory is None else {**code, "unexpected_ignored": ()},
            previous,
        )
        receipt_fd = _open_regular_fd(_SYNC_RECEIPT, dir_fd=previous_fd)
        try:
            if stat.S_IMODE(os.fstat(receipt_fd).st_mode) != 0o600:
                raise PermissionError("prior release receipt must be an owner-only 0600 file")
        finally:
            os.close(receipt_fd)
        boundary = _release_boundary_status(
            previous,
            release_fd=previous_fd,
        )
        if not all(
            value
            for group_name in ("stable_assets", "secure_directories", "bindings")
            for value in boundary[group_name].values()
        ):
            raise RuntimeError("prior release violates the common release boundary")
        return inventory
    except BaseException:
        if inventory is not None:
            _close_runtime_bytecode_inventory(inventory)
        raise


def _close_runtime_bytecode_inventory(inventory: _RuntimeBytecodeInventory) -> None:
    for cache in inventory.caches:
        os.close(cache.cache_fd)
        os.close(cache.source_parent_fd)


def _require_runtime_bytecode_quarantine_receipt_compatible(
    previous: ReleaseLayout,
    *,
    previous_fd: int,
    expected_checker: Mapping[str, object],
) -> None:
    error_message = "prior frozen-sync receipt cannot be restored by source quarantine"
    try:
        receipt_context = _bound_sync_receipt(
            previous,
            release_fd=previous_fd,
        )
        with (
            receipt_context as receipt_binding,
            _bound_venv_directory_fd(previous_fd) as venv_fd,
        ):
            receipt = receipt_binding.payload
            has_publication_identity = "publication_identity" in receipt
            if has_publication_identity and receipt.get("publication_identity") != list(
                _receipt_publication_identity(os.fstat(receipt_binding.descriptor))
            ):
                raise ValueError(error_message)
            uv_lock_snapshot = _release_file_snapshot(previous_fd, "uv.lock")
            expected = {
                "commit": previous.commit,
                "uv_lock_sha256": uv_lock_snapshot["sha256"],
                "venv_sha256": _venv_digest_from_fd(venv_fd),
            }
            if any(receipt.get(key) != value for key, value in expected.items()):
                raise ValueError(error_message)
            try:
                _require_stored_python_identity_bound(
                    previous,
                    receipt.get("python_identity"),
                    release_fd=previous_fd,
                    venv_fd=venv_fd,
                )
            except RuntimeError:
                raise ValueError(error_message) from None
            runtime_imports = receipt.get("runtime_imports")
            if runtime_imports is not None and not (
                isinstance(runtime_imports, dict)
                and runtime_imports.get("ready") is True
                and isinstance(runtime_imports.get("module_count"), int)
                and isinstance(runtime_imports.get("modules_sha256"), str)
            ):
                raise ValueError(error_message)
            try:
                contract = _frozen_special_handoff_contract(
                    previous_fd,
                    expected_checker,
                )
            except (OSError, PermissionError, ValueError):
                raise ValueError(error_message) from None
            stored_handoff_binding_sha256 = receipt.get("handoff_binding_sha256")
            has_handoff_binding = "handoff_binding_sha256" in receipt
            if contract is _SpecialHandoffModeContract.PRE_HANDOFF:
                if has_publication_identity or has_handoff_binding:
                    raise ValueError(error_message)
            else:
                if (
                    not has_publication_identity
                    or not has_handoff_binding
                    or (
                        not isinstance(stored_handoff_binding_sha256, str)
                        or _FULL_SHA256.fullmatch(stored_handoff_binding_sha256) is None
                    )
                ):
                    raise ValueError(error_message)
                try:
                    handoff_binding_sha256 = _special_handoff_binding_sha256(
                        previous,
                        targets=_special_handoff_targets_for_contract(contract),
                        release_fd=previous_fd,
                    )
                except (OSError, PermissionError, ValueError):
                    handoff_binding_sha256 = None
                if stored_handoff_binding_sha256 != handoff_binding_sha256:
                    raise ValueError(error_message)
            if receipt.get("venv_sha256") != _venv_digest_from_fd(venv_fd):
                raise ValueError(error_message)
            if not _sync_receipt_binding_current(
                previous,
                receipt_binding,
                release_fd=previous_fd,
            ):
                raise ValueError(error_message)
    except (OSError, RuntimeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError(error_message) from None


def _require_post_quarantine_clean_prior(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    *,
    previous_fd: int,
    expected_checker: Mapping[str, object],
) -> None:
    inventory = _require_runtime_bytecode_prior_boundary(
        candidate,
        previous,
        previous_fd=previous_fd,
    )
    if inventory is None:
        _require_runtime_bytecode_quarantine_receipt_compatible(
            previous,
            previous_fd=previous_fd,
            expected_checker=expected_checker,
        )
        return
    _close_runtime_bytecode_inventory(inventory)
    raise RuntimeError("prior release acquired runtime bytecode after quarantine")


def _verify_runtime_bytecode_fd(
    directory_fd: int,
    cache: _RuntimeBytecodeCache,
) -> None:
    if _stat_identity(os.fstat(directory_fd)) != cache.cache_identity:
        raise RuntimeError("runtime bytecode directory identity drifted")
    try:
        children = sorted(os.listdir(directory_fd))
    except OSError as error:
        raise RuntimeError("runtime bytecode payload is unavailable") from error
    if tuple(children) != tuple(entry.name for entry in cache.files):
        raise RuntimeError("runtime bytecode payload inventory drifted")
    for name, entry in zip(children, cache.files, strict=True):
        descriptor = _open_regular_fd(name, dir_fd=directory_fd)
        try:
            if (
                _stat_identity(os.fstat(descriptor)) != entry.identity
                or _sha256_fd(descriptor) != entry.sha256
            ):
                raise RuntimeError("runtime bytecode payload identity drifted")
        finally:
            os.close(descriptor)


def _renameat2(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
    *,
    flags: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    _renameat2(
        source_parent_fd,
        source_name,
        destination_parent_fd,
        destination_name,
        flags=_RENAME_NOREPLACE,
    )


def _rename_exchange(
    first_parent_fd: int,
    first_name: str,
    second_parent_fd: int,
    second_name: str,
) -> None:
    _renameat2(
        first_parent_fd,
        first_name,
        second_parent_fd,
        second_name,
        flags=_RENAME_EXCHANGE,
    )


def _require_same_device(source_fd: int, destination_parent_fd: int) -> None:
    if os.fstat(source_fd).st_dev != os.fstat(destination_parent_fd).st_dev:
        raise OSError(errno.EXDEV, "runtime bytecode quarantine crosses filesystems")


def _runtime_bytecode_quarantine_basename(
    prior_commit: str,
    inventory_sha256: str,
    cache_index: int,
) -> str:
    index = f"{cache_index:04d}"
    name = f".runtime-bytecode-quarantine-{prior_commit}-{inventory_sha256}-{index}"
    if (
        len(os.fsencode(name)) > 255
        or _FULL_COMMIT.fullmatch(prior_commit) is None
        or re.fullmatch(r"[0-9a-f]{64}", inventory_sha256) is None
        or cache_index < 0
        or cache_index >= _RUNTIME_BYTECODE_MAX_FILES
        or re.fullmatch(
            r"\.runtime-bytecode-quarantine-[0-9a-f]{40}-[0-9a-f]{64}-[0-9]{4}",
            name,
        )
        is None
    ):
        raise ValueError("unsafe runtime bytecode quarantine basename")
    return name


def _open_bound_runtime_bytecode_directory(
    parent_fd: int,
    name: str,
    cache: _RuntimeBytecodeCache,
) -> int:
    descriptor = _open_directory_fd(name, dir_fd=parent_fd)
    if _stat_identity(os.fstat(descriptor)) != cache.cache_identity:
        os.close(descriptor)
        raise RuntimeError("runtime bytecode directory binding drifted")
    return descriptor


def _require_directory_entry_absent(parent_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise RuntimeError("runtime bytecode directory binding unexpectedly exists")


def _selected_current_release_fd_snapshot(
    selected: ReleaseLayout,
    *,
    data_fd: int,
    selected_fd: int,
) -> dict[str, object]:
    try:
        before = os.stat(
            "current",
            dir_fd=data_fd,
            follow_symlinks=False,
        )
        before_target = os.readlink("current", dir_fd=data_fd)
    except OSError as error:
        raise RuntimeError("current release pointer is unavailable") from error
    if not stat.S_ISLNK(before.st_mode) or before.st_uid != os.geteuid() or before.st_nlink != 1:
        raise PermissionError("current release pointer must be an owner-controlled symlink")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    current_target_fd = os.open("current", flags, dir_fd=data_fd)
    try:
        target_identity = _stat_identity(os.fstat(current_target_fd))
    finally:
        os.close(current_target_fd)
    try:
        after = os.stat(
            "current",
            dir_fd=data_fd,
            follow_symlinks=False,
        )
        after_target = os.readlink("current", dir_fd=data_fd)
    except OSError as error:
        raise RuntimeError("current release pointer changed while opening") from error
    if (
        _stat_identity(before) != _stat_identity(after)
        or before_target != after_target
        or target_identity != _stat_identity(os.fstat(selected_fd))
    ):
        raise RuntimeError(
            "current release pointer changed or does not select the expected release"
        )
    return {
        "identity": _stat_identity(after),
        "target": after_target,
        "target_identity": target_identity,
        "expected_commit": selected.commit,
    }


def _current_release_fd_snapshot(
    previous: ReleaseLayout,
    descriptors: _QuarantineReleaseDescriptors,
) -> dict[str, object]:
    return _selected_current_release_fd_snapshot(
        previous,
        data_fd=descriptors.data_fd,
        selected_fd=descriptors.previous_fd,
    )


def _quarantine_state_snapshot(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    descriptors: _QuarantineReleaseDescriptors,
) -> dict[str, object]:
    candidate_identity = _stat_identity(os.fstat(descriptors.candidate_fd))
    previous_identity = _stat_identity(os.fstat(descriptors.previous_fd))
    previous_generation = _directory_generation_stamp(os.fstat(descriptors.previous_fd))
    releases_generation = _directory_generation_stamp(os.fstat(descriptors.releases_fd))
    if (
        _path_identity(candidate.release_root) != candidate_identity
        or _path_identity(previous.release_root) != previous_identity
        or _directory_generation_stamp(previous.release_root.lstat()) != previous_generation
        or _directory_generation_stamp(previous.releases_root.lstat()) != releases_generation
    ):
        raise RuntimeError("release directory binding changed during quarantine")
    candidate_operator = _candidate_operator_snapshot(
        candidate,
        descriptors.candidate_fd,
    )
    candidate_status = _release_status(candidate)
    if candidate_status.get("ready") is not True:
        raise RuntimeError("candidate operator release is not fully ready")
    return {
        "current": _current_release_fd_snapshot(previous, descriptors),
        "candidate_operator": candidate_operator,
        "candidate_status": candidate_status,
        "candidate_identity": candidate_identity,
        "previous_identity": previous_identity,
        "previous_generation": previous_generation,
        "releases_generation": releases_generation,
        "previous_checker": _release_file_snapshot(
            descriptors.previous_fd,
            _VERSIONED_CHECKER,
        ),
        "previous_receipt": _release_file_snapshot(
            descriptors.previous_fd,
            _SYNC_RECEIPT,
        ),
    }


def _require_quarantine_state_unchanged(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    descriptors: _QuarantineReleaseDescriptors,
    expected: Mapping[str, object],
) -> None:
    observed = _quarantine_state_snapshot(candidate, previous, descriptors)
    if observed["current"] != expected["current"]:
        raise RuntimeError("current release pointer changed during runtime bytecode quarantine")
    if observed != expected:
        raise RuntimeError("release identity changed during runtime bytecode quarantine")


def _require_quarantine_data_root_bound(
    candidate: ReleaseLayout,
    descriptors: _QuarantineReleaseDescriptors,
) -> None:
    if _path_identity(candidate.data_root) != _stat_identity(os.fstat(descriptors.data_fd)):
        raise RuntimeError("quarantine data root binding changed")


def _runtime_bytecode_quarantine_destinations(
    previous: ReleaseLayout,
    inventory: _RuntimeBytecodeInventory,
) -> tuple[tuple[_RuntimeBytecodeCache, str], ...]:
    return tuple(
        (
            cache,
            _runtime_bytecode_quarantine_basename(
                previous.commit,
                inventory.inventory_sha256,
                index,
            ),
        )
        for index, cache in enumerate(inventory.caches)
    )


def _runtime_bytecode_quarantine_projection(
    candidate: ReleaseLayout,
    inventory: _RuntimeBytecodeInventory,
    destinations: Sequence[tuple[_RuntimeBytecodeCache, str]],
) -> dict[str, object]:
    return {
        "inventory_sha256": inventory.inventory_sha256,
        "cache_directories": [cache.relative_cache_directory for cache in inventory.caches],
        "file_count": len(inventory.files),
        "total_bytes": inventory.total_bytes,
        "artifacts": [
            {
                "cache_directory": cache.relative_cache_directory,
                "file_count": len(cache.files),
                "total_bytes": cache.total_bytes,
                "path": str(candidate.data_root / destination_name),
            }
            for cache, destination_name in destinations
        ],
    }


def _require_runtime_bytecode_source_bound(cache: _RuntimeBytecodeCache) -> None:
    source_fd = _open_bound_runtime_bytecode_directory(
        cache.source_parent_fd,
        cache.cache_name,
        cache,
    )
    try:
        _verify_runtime_bytecode_fd(source_fd, cache)
    finally:
        os.close(source_fd)


def _require_runtime_bytecode_published(
    descriptors: _QuarantineReleaseDescriptors,
    cache: _RuntimeBytecodeCache,
    destination_name: str,
    published_fd: int,
) -> None:
    _verify_runtime_bytecode_fd(published_fd, cache)
    _require_directory_entry_absent(
        cache.source_parent_fd,
        cache.cache_name,
    )
    target_fd = _open_bound_runtime_bytecode_directory(
        descriptors.data_fd,
        destination_name,
        cache,
    )
    try:
        _verify_runtime_bytecode_fd(target_fd, cache)
    finally:
        os.close(target_fd)


def _rollback_runtime_bytecode_caches(
    descriptors: _QuarantineReleaseDescriptors,
    moved: Sequence[tuple[_RuntimeBytecodeCache, str]],
) -> None:
    for cache, destination_name in reversed(moved):
        _verify_runtime_bytecode_fd(cache.cache_fd, cache)
        target_fd = _open_bound_runtime_bytecode_directory(
            descriptors.data_fd,
            destination_name,
            cache,
        )
        try:
            _verify_runtime_bytecode_fd(target_fd, cache)
        finally:
            os.close(target_fd)
        _rename_noreplace(
            descriptors.data_fd,
            destination_name,
            cache.source_parent_fd,
            cache.cache_name,
        )
        os.fsync(descriptors.data_fd)
        os.fsync(cache.source_parent_fd)
        _require_directory_entry_absent(
            descriptors.data_fd,
            destination_name,
        )
        _require_runtime_bytecode_source_bound(cache)


def _plan_runtime_bytecode_quarantine(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    descriptors: _QuarantineReleaseDescriptors,
) -> tuple[
    dict[str, object],
    _RuntimeBytecodeInventory | None,
    tuple[tuple[_RuntimeBytecodeCache, str], ...],
]:
    state_snapshot = _quarantine_state_snapshot(
        candidate,
        previous,
        descriptors,
    )
    inventory = _require_runtime_bytecode_prior_boundary(
        candidate,
        previous,
        previous_fd=descriptors.previous_fd,
    )
    try:
        previous_checker = state_snapshot.get("previous_checker")
        if not isinstance(previous_checker, Mapping):
            raise RuntimeError("prior release checker snapshot is invalid")
        _require_quarantine_state_unchanged(
            candidate,
            previous,
            descriptors,
            state_snapshot,
        )
        if inventory is None:
            _validate_versioned_previous_release(
                candidate,
                previous,
                previous_fd=descriptors.previous_fd,
                releases_fd=descriptors.releases_fd,
            )
            _require_post_quarantine_clean_prior(
                candidate,
                previous,
                previous_fd=descriptors.previous_fd,
                expected_checker=previous_checker,
            )
            _require_quarantine_state_unchanged(
                candidate,
                previous,
                descriptors,
                state_snapshot,
            )
            return state_snapshot, None, ()

        _require_runtime_bytecode_quarantine_receipt_compatible(
            previous,
            previous_fd=descriptors.previous_fd,
            expected_checker=previous_checker,
        )
        _require_quarantine_state_unchanged(
            candidate,
            previous,
            descriptors,
            state_snapshot,
        )
        repeated_inventory = _require_runtime_bytecode_prior_boundary(
            candidate,
            previous,
            previous_fd=descriptors.previous_fd,
        )
        try:
            if repeated_inventory != inventory:
                raise RuntimeError("runtime bytecode inventory changed during quarantine preflight")
        finally:
            if repeated_inventory is not None:
                _close_runtime_bytecode_inventory(repeated_inventory)
        _require_quarantine_state_unchanged(
            candidate,
            previous,
            descriptors,
            state_snapshot,
        )
        destinations = _runtime_bytecode_quarantine_destinations(
            previous,
            inventory,
        )
        for cache, destination_name in destinations:
            _require_same_device(cache.cache_fd, descriptors.data_fd)
            _require_directory_entry_absent(
                descriptors.data_fd,
                destination_name,
            )
            _require_runtime_bytecode_source_bound(cache)
        _require_quarantine_data_root_bound(
            candidate,
            descriptors,
        )
        _require_quarantine_state_unchanged(
            candidate,
            previous,
            descriptors,
            state_snapshot,
        )
        return state_snapshot, inventory, destinations
    except BaseException:
        if inventory is not None:
            _close_runtime_bytecode_inventory(inventory)
        raise


def preflight_active_release_runtime_bytecode_quarantine(
    candidate: ReleaseLayout,
    *,
    expected_current_commit: str,
) -> dict[str, Any]:
    """Validate an exact quarantine plan without moving runtime bytecode."""

    previous = ReleaseLayout(home=candidate.home, commit=expected_current_commit)
    if previous.commit == candidate.commit:
        raise ValueError(
            "runtime bytecode quarantine requires distinct candidate and prior commits"
        )
    with (
        _release_lock(candidate),
        _quarantine_release_descriptors(candidate, previous) as descriptors,
    ):
        _, inventory, destinations = _plan_runtime_bytecode_quarantine(
            candidate,
            previous,
            descriptors,
        )
        try:
            return {
                "schema_version": _RUNTIME_BYTECODE_QUARANTINE_PREFLIGHT_SCHEMA,
                "status": ("already-ready" if inventory is None else "ready-to-quarantine"),
                "ready": True,
                "would_change": inventory is not None,
                "current_unchanged": True,
                "frozen_sync_receipt_unchanged": True,
                "candidate_commit": candidate.commit,
                "prior_commit": previous.commit,
                "quarantine": (
                    None
                    if inventory is None
                    else _runtime_bytecode_quarantine_projection(
                        candidate,
                        inventory,
                        destinations,
                    )
                ),
            }
        finally:
            if inventory is not None:
                _close_runtime_bytecode_inventory(inventory)


def quarantine_active_release_runtime_bytecode(
    candidate: ReleaseLayout,
    *,
    expected_current_commit: str,
) -> dict[str, Any]:
    """Quarantine one bounded runtime-bytecode inventory from the active prior."""

    previous = ReleaseLayout(home=candidate.home, commit=expected_current_commit)
    if previous.commit == candidate.commit:
        raise ValueError(
            "runtime bytecode quarantine requires distinct candidate and prior commits"
        )
    with (
        _release_lock(candidate),
        _quarantine_release_descriptors(candidate, previous) as descriptors,
    ):
        state_snapshot, inventory, destinations = _plan_runtime_bytecode_quarantine(
            candidate,
            previous,
            descriptors,
        )
        published_fds: dict[str, int] = {}
        moved: list[tuple[_RuntimeBytecodeCache, str]] = []
        try:
            if inventory is None:
                return {
                    "schema_version": _RUNTIME_BYTECODE_QUARANTINE_SCHEMA,
                    "status": "already-ready",
                    "ready": True,
                    "changed": False,
                    "current_unchanged": True,
                    "frozen_sync_receipt_unchanged": True,
                    "candidate_commit": candidate.commit,
                    "prior_commit": previous.commit,
                    "quarantine": None,
                }

            repeated_inventory = _require_runtime_bytecode_prior_boundary(
                candidate,
                previous,
                previous_fd=descriptors.previous_fd,
            )
            try:
                if repeated_inventory != inventory:
                    raise RuntimeError("runtime bytecode inventory changed before atomic rename")
            finally:
                if repeated_inventory is not None:
                    _close_runtime_bytecode_inventory(repeated_inventory)
            _require_quarantine_state_unchanged(
                candidate,
                previous,
                descriptors,
                state_snapshot,
            )
            _require_quarantine_data_root_bound(
                candidate,
                descriptors,
            )
            for cache, _destination_name in destinations:
                _require_runtime_bytecode_source_bound(cache)
            for cache, destination_name in destinations:
                _rename_noreplace(
                    cache.source_parent_fd,
                    cache.cache_name,
                    descriptors.data_fd,
                    destination_name,
                )
                moved.append((cache, destination_name))
                os.fsync(cache.source_parent_fd)
                os.fsync(descriptors.data_fd)
                _require_directory_entry_absent(
                    cache.source_parent_fd,
                    cache.cache_name,
                )
                published_fd = _open_bound_runtime_bytecode_directory(
                    descriptors.data_fd,
                    destination_name,
                    cache,
                )
                _verify_runtime_bytecode_fd(published_fd, cache)
                published_fds[destination_name] = published_fd

            _validate_versioned_previous_release(
                candidate,
                previous,
                previous_fd=descriptors.previous_fd,
                releases_fd=descriptors.releases_fd,
            )
            previous_checker = state_snapshot.get("previous_checker")
            if not isinstance(previous_checker, Mapping):
                raise RuntimeError("prior release checker snapshot is invalid")
            _require_post_quarantine_clean_prior(
                candidate,
                previous,
                previous_fd=descriptors.previous_fd,
                expected_checker=previous_checker,
            )
            _require_quarantine_state_unchanged(
                candidate,
                previous,
                descriptors,
                state_snapshot,
            )
            for cache, destination_name in destinations:
                _require_runtime_bytecode_published(
                    descriptors,
                    cache,
                    destination_name,
                    published_fds[destination_name],
                )
            _require_quarantine_state_unchanged(
                candidate,
                previous,
                descriptors,
                state_snapshot,
            )
            _require_quarantine_data_root_bound(
                candidate,
                descriptors,
            )
            for cache, destination_name in destinations:
                _require_runtime_bytecode_published(
                    descriptors,
                    cache,
                    destination_name,
                    published_fds[destination_name],
                )
            return {
                "schema_version": _RUNTIME_BYTECODE_QUARANTINE_SCHEMA,
                "status": "quarantined",
                "ready": True,
                "changed": True,
                "current_unchanged": True,
                "frozen_sync_receipt_unchanged": True,
                "candidate_commit": candidate.commit,
                "prior_commit": previous.commit,
                "quarantine": _runtime_bytecode_quarantine_projection(
                    candidate,
                    inventory,
                    destinations,
                ),
            }
        except BaseException:
            if moved:
                try:
                    _rollback_runtime_bytecode_caches(
                        descriptors,
                        moved,
                    )
                    moved.clear()
                except BaseException as rollback_error:
                    raise RuntimeError(
                        "runtime bytecode quarantine failed and no-clobber rollback failed"
                    ) from rollback_error
            raise
        finally:
            for published_fd in published_fds.values():
                os.close(published_fd)
            if inventory is not None:
                _close_runtime_bytecode_inventory(inventory)


def _runtime_bytecode_binding_sha256(
    inventory: _RuntimeBytecodeInventory,
) -> str:
    return _canonical_json_sha256(
        [
            {
                "cache_directory": cache.relative_cache_directory,
                "cache_identity": list(cache.cache_identity),
                "files": [
                    {
                        "path": entry.relative_path,
                        "identity": list(entry.identity),
                    }
                    for entry in cache.files
                ],
            }
            for cache in inventory.caches
        ]
    )


def _actual_special_handoff_binding_sha256(
    layout: ReleaseLayout,
) -> str | None:
    try:
        return _special_handoff_binding_sha256(layout)
    except (OSError, PermissionError, ValueError):
        return None


def _degraded_current_cutover_plan(
    candidate: ReleaseLayout,
    previous: ReleaseLayout,
    descriptors: _QuarantineReleaseDescriptors,
    *,
    prior_current_pointer_sha256: str,
) -> tuple[dict[str, object], dict[str, object]]:
    _require_sha256(
        prior_current_pointer_sha256,
        label="prior current pointer digest",
    )
    candidate_identity = _stat_identity(os.fstat(descriptors.candidate_fd))
    previous_identity = _stat_identity(os.fstat(descriptors.previous_fd))
    if (
        _path_identity(candidate.release_root) != candidate_identity
        or _path_identity(previous.release_root) != previous_identity
    ):
        raise RuntimeError("degraded current release binding changed")
    candidate_operator = _candidate_operator_snapshot(
        candidate,
        descriptors.candidate_fd,
    )
    candidate_status = _release_status(candidate)
    if candidate_status.get("ready") is not True:
        raise RuntimeError("candidate operator release is not fully ready")
    inventory = _require_runtime_bytecode_prior_boundary(
        candidate,
        previous,
        previous_fd=descriptors.previous_fd,
    )
    try:
        current_target = f"releases/{previous.commit}"
        immutable = _immutable_previous_readiness_snapshot(
            candidate,
            previous,
            prior_current_target=current_target,
        )
        receipt_snapshot = _release_file_snapshot(
            descriptors.previous_fd,
            _SYNC_RECEIPT,
        )
        checker_snapshot = _release_file_snapshot(
            descriptors.previous_fd,
            _VERSIONED_CHECKER,
        )
        receipt_identity = receipt_snapshot.get("identity")
        checker_identity = checker_snapshot.get("identity")
        if (
            not isinstance(receipt_identity, tuple)
            or not isinstance(checker_identity, tuple)
            or list(receipt_identity) != immutable["prior_receipt_identity"]
            or receipt_snapshot["sha256"] != immutable["prior_receipt_sha256"]
            or list(checker_identity) != immutable["prior_checker_identity"]
            or checker_snapshot["sha256"] != immutable["prior_checker_sha256"]
        ):
            raise RuntimeError("degraded current immutable snapshot changed")
        actual_uv_lock_sha256 = _sha256_file(previous.release_root / "uv.lock")
        with _bound_venv_directory_fd(descriptors.previous_fd) as venv_fd:
            bound_python_identity = _require_stored_python_identity_bound(
                previous,
                immutable["prior_python_identity"],
                release_fd=descriptors.previous_fd,
                venv_fd=venv_fd,
            )
            actual_venv_sha256 = _venv_digest_from_fd(venv_fd)
        if (
            immutable["prior_receipt_commit"] != previous.commit
            or immutable["prior_uv_lock_sha256"] != actual_uv_lock_sha256
            or (
                immutable["prior_receipt_publication_identity"] is not None
                and _read_sync_receipt(
                    previous,
                    require_publication_identity=True,
                )
                is None
            )
        ):
            raise RuntimeError("degraded current immutable receipt baseline is invalid")
        actual_handoff_binding_sha256 = _actual_special_handoff_binding_sha256(previous)
        source_bytecode: dict[str, object] | None = (
            None
            if inventory is None
            else {
                "inventory_sha256": inventory.inventory_sha256,
                "binding_sha256": _runtime_bytecode_binding_sha256(inventory),
                "cache_directories": [cache.relative_cache_directory for cache in inventory.caches],
                "file_count": len(inventory.files),
                "total_bytes": inventory.total_bytes,
            }
        )
        if source_bytecode is None:
            raise ValueError("degraded current cutover requires bounded runtime bytecode drift")
        runtime_bytecode = source_bytecode
        prior_snapshot: dict[str, object] = {
            "immutable": immutable,
            "actual_venv_sha256": actual_venv_sha256,
            "actual_uv_lock_sha256": actual_uv_lock_sha256,
            "bound_python_identity": bound_python_identity,
            "actual_handoff_binding_sha256": actual_handoff_binding_sha256,
            "runtime_bytecode": runtime_bytecode,
        }
        candidate_snapshot: dict[str, object] = {
            "commit": candidate.commit,
            "release_identity": list(candidate_identity),
            "operator_sha256": _canonical_json_sha256(candidate_operator),
            "status_sha256": _canonical_json_sha256(candidate_status),
        }
        authority: dict[str, object] = {
            "schema_version": _DEGRADED_CURRENT_CUTOVER_AUTHORITY_SCHEMA,
            "candidate": candidate_snapshot,
            "prior_commit": previous.commit,
            "prior_current_pointer_sha256": prior_current_pointer_sha256,
            "prior": prior_snapshot,
        }
        cutover_sha256 = _canonical_json_sha256(authority)
        projection: dict[str, object] = {
            "cutover_sha256": cutover_sha256,
            "candidate_commit": candidate.commit,
            "prior_commit": previous.commit,
            "prior_current_pointer_sha256": prior_current_pointer_sha256,
            "degraded_current": {
                **runtime_bytecode,
                "receipt_sha256": immutable["prior_receipt_sha256"],
                "receipt_schema_version": immutable["prior_receipt_schema_version"],
                "stored_venv_sha256": immutable["prior_venv_sha256"],
                "actual_venv_sha256": actual_venv_sha256,
                "actual_uv_lock_sha256": actual_uv_lock_sha256,
                "python_identity_bound": True,
                "receipt_venv_matches_actual": (
                    immutable["prior_venv_sha256"] == actual_venv_sha256
                ),
                "stored_handoff_binding_sha256": immutable["prior_handoff_binding_sha256"],
                "actual_handoff_binding_sha256": (actual_handoff_binding_sha256),
                "receipt_handoff_matches_actual": (
                    None
                    if immutable["prior_handoff_binding_sha256"] is None
                    else immutable["prior_handoff_binding_sha256"] == actual_handoff_binding_sha256
                ),
                "git_identity_sha256": immutable["prior_git_identity_sha256"],
                "release_boundary_sha256": immutable["prior_release_boundary_sha256"],
            },
        }
        return authority, projection
    finally:
        if inventory is not None:
            _close_runtime_bytecode_inventory(inventory)


def _require_degraded_current_selection(
    selected: ReleaseLayout,
    *,
    data_fd: int,
    selected_fd: int,
) -> dict[str, object]:
    snapshot = _selected_current_release_fd_snapshot(
        selected,
        data_fd=data_fd,
        selected_fd=selected_fd,
    )
    if snapshot["target"] != f"releases/{selected.commit}":
        raise RuntimeError("degraded current pointer must use the canonical relative target")
    return snapshot


def _replace_current_release_pointer_fd(
    candidate: ReleaseLayout,
    descriptors: _QuarantineReleaseDescriptors,
    *,
    expected_current: ReleaseLayout,
    expected_current_fd: int,
    expected_snapshot: Mapping[str, object],
    target: ReleaseLayout,
    target_fd: int,
) -> dict[str, object]:
    if _path_identity(target.release_root) != _stat_identity(os.fstat(target_fd)):
        raise RuntimeError("cutover target release binding changed")
    _require_quarantine_data_root_bound(candidate, descriptors)
    temporary_name = f".current.tmp-{os.getpid()}"
    _require_directory_entry_absent(descriptors.data_fd, temporary_name)
    temporary_identity: tuple[int, int, int, int, int, int] | None = None
    displaced_identity: tuple[int, int, int, int, int, int] | None = None
    displaced_target: str | None = None
    exchanged = False
    try:
        os.symlink(
            f"releases/{target.commit}",
            temporary_name,
            dir_fd=descriptors.data_fd,
        )
        temporary_metadata = os.stat(
            temporary_name,
            dir_fd=descriptors.data_fd,
            follow_symlinks=False,
        )
        temporary_identity = _stat_identity(temporary_metadata)
        if (
            not stat.S_ISLNK(temporary_metadata.st_mode)
            or temporary_metadata.st_uid != os.geteuid()
            or temporary_metadata.st_nlink != 1
        ):
            raise PermissionError("temporary current pointer must be an owner-controlled symlink")
        observed = _require_degraded_current_selection(
            expected_current,
            data_fd=descriptors.data_fd,
            selected_fd=expected_current_fd,
        )
        if observed != dict(expected_snapshot):
            raise RuntimeError("current release pointer changed during cutover")
        _require_quarantine_data_root_bound(candidate, descriptors)
        _rename_exchange(
            descriptors.data_fd,
            temporary_name,
            descriptors.data_fd,
            "current",
        )
        exchanged = True
        displaced_metadata = os.stat(
            temporary_name,
            dir_fd=descriptors.data_fd,
            follow_symlinks=False,
        )
        displaced_identity = _stat_identity(displaced_metadata)
        displaced_target = os.readlink(
            temporary_name,
            dir_fd=descriptors.data_fd,
        )
        if displaced_identity != expected_snapshot.get(
            "identity"
        ) or displaced_target != expected_snapshot.get("target"):
            raise RuntimeError("current release pointer changed during cutover exchange")
        current_metadata = os.stat(
            "current",
            dir_fd=descriptors.data_fd,
            follow_symlinks=False,
        )
        if _stat_identity(current_metadata) != temporary_identity:
            raise RuntimeError("current release pointer changed after cutover exchange")
        target_snapshot = _require_degraded_current_selection(
            target,
            data_fd=descriptors.data_fd,
            selected_fd=target_fd,
        )
        if target_snapshot["identity"] != temporary_identity:
            raise RuntimeError("cutover target pointer identity changed")
        os.fsync(descriptors.data_fd)
        before_unlink = os.stat(
            temporary_name,
            dir_fd=descriptors.data_fd,
            follow_symlinks=False,
        )
        if (
            _stat_identity(before_unlink) != displaced_identity
            or os.readlink(temporary_name, dir_fd=descriptors.data_fd) != displaced_target
        ):
            raise RuntimeError("displaced current pointer changed before cleanup")
        os.unlink(temporary_name, dir_fd=descriptors.data_fd)
        exchanged = False
        os.fsync(descriptors.data_fd)
        return target_snapshot
    except BaseException:
        if exchanged:
            try:
                current_metadata = os.stat(
                    "current",
                    dir_fd=descriptors.data_fd,
                    follow_symlinks=False,
                )
                displaced_metadata = os.stat(
                    temporary_name,
                    dir_fd=descriptors.data_fd,
                    follow_symlinks=False,
                )
                if (
                    _stat_identity(current_metadata) != temporary_identity
                    or displaced_identity is None
                    or _stat_identity(displaced_metadata) != displaced_identity
                    or os.readlink(temporary_name, dir_fd=descriptors.data_fd) != displaced_target
                ):
                    raise RuntimeError("current pointer exchange rollback binding changed")
                _rename_exchange(
                    descriptors.data_fd,
                    temporary_name,
                    descriptors.data_fd,
                    "current",
                )
                exchanged = False
                os.fsync(descriptors.data_fd)
                if _require_degraded_current_selection(
                    expected_current,
                    data_fd=descriptors.data_fd,
                    selected_fd=expected_current_fd,
                ) != dict(expected_snapshot):
                    raise RuntimeError(
                        "current pointer exchange rollback did not restore selection"
                    )
            except BaseException as rollback_error:
                raise RuntimeError(
                    "current pointer exchange failed and atomic rollback failed"
                ) from rollback_error
        raise
    finally:
        if temporary_identity is not None and not exchanged:
            try:
                observed_temporary = os.stat(
                    temporary_name,
                    dir_fd=descriptors.data_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if _stat_identity(observed_temporary) != temporary_identity:
                    raise RuntimeError("temporary current pointer changed during cleanup")
                os.unlink(temporary_name, dir_fd=descriptors.data_fd)
                os.fsync(descriptors.data_fd)


def _require_sha256(value: str, *, label: str) -> None:
    if _FULL_SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase 64-character SHA256")


def preflight_degraded_current_cutover(
    candidate: ReleaseLayout,
    *,
    degraded_prior_commit: str,
) -> dict[str, Any]:
    """Freeze a read-only authority for leaving one known-running degraded prior."""

    previous = ReleaseLayout(home=candidate.home, commit=degraded_prior_commit)
    if previous.commit == candidate.commit:
        raise ValueError("degraded current cutover requires distinct candidate and prior commits")
    with (
        _release_lock(candidate),
        _quarantine_release_descriptors(candidate, previous) as descriptors,
    ):
        current_snapshot = _require_degraded_current_selection(
            previous,
            data_fd=descriptors.data_fd,
            selected_fd=descriptors.previous_fd,
        )
        prior_current_pointer_sha256 = _canonical_json_sha256(current_snapshot)
        authority, projection = _degraded_current_cutover_plan(
            candidate,
            previous,
            descriptors,
            prior_current_pointer_sha256=prior_current_pointer_sha256,
        )
        if (
            _require_degraded_current_selection(
                previous,
                data_fd=descriptors.data_fd,
                selected_fd=descriptors.previous_fd,
            )
            != current_snapshot
            or _degraded_current_cutover_plan(
                candidate,
                previous,
                descriptors,
                prior_current_pointer_sha256=prior_current_pointer_sha256,
            )[0]
            != authority
        ):
            raise RuntimeError("degraded current cutover plan changed")
        return {
            "schema_version": _DEGRADED_CURRENT_CUTOVER_PREFLIGHT_SCHEMA,
            "status": "ready-to-cutover",
            "ready": True,
            "would_change_current": True,
            "current_unchanged": True,
            **projection,
        }


def activate_degraded_current_cutover(
    candidate: ReleaseLayout,
    *,
    degraded_prior_commit: str,
    expected_cutover_sha256: str,
    expected_prior_current_pointer_sha256: str,
) -> dict[str, Any]:
    """CAS current from an exact degraded prior to one fully-ready candidate."""

    _require_sha256(expected_cutover_sha256, label="expected cutover digest")
    _require_sha256(
        expected_prior_current_pointer_sha256,
        label="expected prior current pointer digest",
    )
    previous = ReleaseLayout(home=candidate.home, commit=degraded_prior_commit)
    if previous.commit == candidate.commit:
        raise ValueError("degraded current cutover requires distinct candidate and prior commits")
    with (
        _release_lock(candidate),
        _quarantine_release_descriptors(candidate, previous) as descriptors,
    ):
        prior_current = _require_degraded_current_selection(
            previous,
            data_fd=descriptors.data_fd,
            selected_fd=descriptors.previous_fd,
        )
        if _canonical_json_sha256(prior_current) != expected_prior_current_pointer_sha256:
            raise RuntimeError("degraded current pointer changed after preflight")
        authority, projection = _degraded_current_cutover_plan(
            candidate,
            previous,
            descriptors,
            prior_current_pointer_sha256=expected_prior_current_pointer_sha256,
        )
        if projection["cutover_sha256"] != expected_cutover_sha256:
            raise RuntimeError("degraded current cutover plan changed")
        candidate_current: dict[str, object] | None = None
        try:
            candidate_current = _replace_current_release_pointer_fd(
                candidate,
                descriptors,
                expected_current=previous,
                expected_current_fd=descriptors.previous_fd,
                expected_snapshot=prior_current,
                target=candidate,
                target_fd=descriptors.candidate_fd,
            )
            after_authority, after_projection = _degraded_current_cutover_plan(
                candidate,
                previous,
                descriptors,
                prior_current_pointer_sha256=expected_prior_current_pointer_sha256,
            )
            if (
                after_authority != authority
                or after_projection != projection
                or _release_status(candidate).get("ready") is not True
            ):
                raise RuntimeError("degraded current cutover plan changed")
        except BaseException:
            if candidate_current is None:
                with suppress(OSError, PermissionError, RuntimeError):
                    candidate_current = _require_degraded_current_selection(
                        candidate,
                        data_fd=descriptors.data_fd,
                        selected_fd=descriptors.candidate_fd,
                    )
            if candidate_current is not None:
                try:
                    _replace_current_release_pointer_fd(
                        candidate,
                        descriptors,
                        expected_current=candidate,
                        expected_current_fd=descriptors.candidate_fd,
                        expected_snapshot=candidate_current,
                        target=previous,
                        target_fd=descriptors.previous_fd,
                    )
                except BaseException as rollback_error:
                    raise RuntimeError(
                        "degraded current cutover failed and pointer rollback failed"
                    ) from rollback_error
            raise
        return {
            "schema_version": _DEGRADED_CURRENT_CUTOVER_SCHEMA,
            "status": "activated",
            "active": True,
            "changed": True,
            "rollback_available": True,
            "prior_unchanged": True,
            "current": str(candidate.current_link),
            "release_root": str(candidate.release_root),
            **projection,
        }


def rollback_degraded_current_cutover(
    candidate: ReleaseLayout,
    *,
    degraded_prior_commit: str,
    expected_cutover_sha256: str,
    expected_prior_current_pointer_sha256: str,
) -> dict[str, Any]:
    """Restore current to the exact degraded prior bound by a cutover digest."""

    _require_sha256(expected_cutover_sha256, label="expected cutover digest")
    _require_sha256(
        expected_prior_current_pointer_sha256,
        label="expected prior current pointer digest",
    )
    previous = ReleaseLayout(home=candidate.home, commit=degraded_prior_commit)
    if previous.commit == candidate.commit:
        raise ValueError("degraded current rollback requires distinct candidate and prior commits")
    with (
        _release_lock(candidate),
        _quarantine_release_descriptors(candidate, previous) as descriptors,
    ):
        candidate_current = _require_degraded_current_selection(
            candidate,
            data_fd=descriptors.data_fd,
            selected_fd=descriptors.candidate_fd,
        )
        authority, projection = _degraded_current_cutover_plan(
            candidate,
            previous,
            descriptors,
            prior_current_pointer_sha256=expected_prior_current_pointer_sha256,
        )
        if projection["cutover_sha256"] != expected_cutover_sha256:
            raise RuntimeError("degraded current cutover plan changed")
        prior_current: dict[str, object] | None = None
        try:
            prior_current = _replace_current_release_pointer_fd(
                candidate,
                descriptors,
                expected_current=candidate,
                expected_current_fd=descriptors.candidate_fd,
                expected_snapshot=candidate_current,
                target=previous,
                target_fd=descriptors.previous_fd,
            )
            after_authority, after_projection = _degraded_current_cutover_plan(
                candidate,
                previous,
                descriptors,
                prior_current_pointer_sha256=expected_prior_current_pointer_sha256,
            )
            if (
                after_authority != authority
                or after_projection != projection
                or _require_degraded_current_selection(
                    previous,
                    data_fd=descriptors.data_fd,
                    selected_fd=descriptors.previous_fd,
                )
                != prior_current
            ):
                raise RuntimeError("degraded current cutover plan changed")
        except BaseException:
            if prior_current is None:
                with suppress(OSError, PermissionError, RuntimeError):
                    prior_current = _require_degraded_current_selection(
                        previous,
                        data_fd=descriptors.data_fd,
                        selected_fd=descriptors.previous_fd,
                    )
            if prior_current is not None:
                try:
                    _replace_current_release_pointer_fd(
                        candidate,
                        descriptors,
                        expected_current=previous,
                        expected_current_fd=descriptors.previous_fd,
                        expected_snapshot=prior_current,
                        target=candidate,
                        target_fd=descriptors.candidate_fd,
                    )
                except BaseException as rollback_error:
                    raise RuntimeError(
                        "degraded current rollback failed and pointer restore failed"
                    ) from rollback_error
            raise
        return {
            "schema_version": _DEGRADED_CURRENT_CUTOVER_ROLLBACK_SCHEMA,
            "status": "rolled-back",
            "active_prior": True,
            "changed": True,
            "candidate_unchanged": True,
            "prior_unchanged": True,
            "current": str(candidate.current_link),
            "release_root": str(previous.release_root),
            **projection,
        }


def _install_exact_symlink(link: Path, target: Path) -> bool:
    if os.path.lexists(link):
        if _link_matches(link, target):
            return False
        raise FileExistsError(f"refusing to replace unexpected release binding: {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.parent.is_symlink() or not link.parent.is_dir():
        raise PermissionError(f"release binding parent must be a real directory: {link.parent}")
    temporary = link.parent / f".{link.name}.tmp-{os.getpid()}"
    if os.path.lexists(temporary):
        raise FileExistsError(f"temporary release binding already exists: {temporary}")
    try:
        temporary.symlink_to(target, target_is_directory=target.is_dir())
        os.replace(temporary, link)
        _fsync_directory(link.parent)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()
    return True


def _release_bindings(layout: ReleaseLayout) -> tuple[tuple[Path, Path], ...]:
    return (
        (layout.release_root / ".env", layout.env_file),
        (layout.release_root / "knowledge-base/runtime", layout.runtime_root),
        (
            layout.release_root / "knowledge-base/market-cache",
            layout.market_cache_root,
        ),
    )


def _preflight_binding(link: Path, target: Path) -> None:
    if os.path.lexists(link) and not _link_matches(link, target):
        raise FileExistsError(f"refusing to replace unexpected release binding: {link}")
    existing_parent = link.parent
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    if existing_parent.is_symlink() or not existing_parent.is_dir():
        raise PermissionError(
            f"release binding parent must descend from a real directory: {link.parent}"
        )


def prepare_release_bindings(layout: ReleaseLayout) -> dict[str, Any]:
    """Bind one validated release to stable owner-only environment and data."""

    with _release_lock(layout):
        _require_prepared_assets(layout)
        bindings = _release_bindings(layout)
        for link, target in bindings:
            _preflight_binding(link, target)
        installed: list[tuple[Path, Path]] = []
        try:
            for link, target in bindings:
                if _install_exact_symlink(link, target):
                    installed.append((link, target))
            status = inspect_release(layout)
            if not status["ready"]:
                raise RuntimeError("prepared FIN release does not satisfy the release contract")
        except Exception:
            for link, target in reversed(installed):
                if _link_matches(link, target):
                    link.unlink()
                    _fsync_directory(link.parent)
            raise
        return {"changed": bool(installed), **status}


def _validate_existing_current(
    layout: ReleaseLayout,
    *,
    expected_current_commit: str | None,
) -> tuple[Path | None, dict[str, object] | None]:
    if (
        expected_current_commit is not None
        and _FULL_COMMIT.fullmatch(expected_current_commit) is None
    ):
        raise ValueError("expected current commit must be one lowercase 40-character Git SHA")
    current = layout.current_link
    if not os.path.lexists(current):
        if expected_current_commit is not None:
            raise RuntimeError("current release pointer does not match the expected commit")
        return None, None
    if not current.is_symlink():
        raise FileExistsError(f"current release pointer must be a symlink: {current}")
    try:
        target = current.resolve(strict=True)
    except OSError:
        raise FileNotFoundError("current release pointer is dangling") from None
    if target.parent != layout.releases_root.resolve(strict=True):
        raise PermissionError("current release pointer escapes the FIN releases directory")
    if _FULL_COMMIT.fullmatch(target.name) is None:
        raise ValueError("current release pointer target is not commit-bound")
    candidate_root = layout.release_root.resolve(strict=True)
    if target == candidate_root:
        if _release_status(layout).get("ready") is not True:
            raise RuntimeError("current release pointer target is not a fully ready release")
        versioned_snapshot = None
    else:
        if expected_current_commit is None:
            raise ValueError("expected current commit is required when replacing an active release")
        if target.name != expected_current_commit:
            raise RuntimeError("current release pointer does not match the expected commit")
        previous_layout = ReleaseLayout(home=layout.home, commit=target.name)
        try:
            _, versioned_snapshot = _validate_versioned_previous_release(
                layout,
                previous_layout,
            )
        except (OSError, RuntimeError, ValueError):
            raise RuntimeError(
                "current release pointer target is not a fully ready release"
            ) from None
    return target, versioned_snapshot


def _canonicalize_same_release_pointer(
    layout: ReleaseLayout,
    *,
    relative_target: Path,
    expected_snapshot: tuple[tuple[int, int, int, int, int, int], str],
) -> None:
    data_fd = _open_directory_fd(layout.data_root)
    try:
        if stat.S_IMODE(os.fstat(data_fd).st_mode) & 0o022:
            raise PermissionError("directory descriptor must be secure owner-controlled")
        temporary_name = f".current.tmp-{os.getpid()}"
        temporary_identity: tuple[int, int, int, int, int, int] | None = None
        displaced_snapshot: tuple[tuple[int, int, int, int, int, int], str] | None = None
        exchanged = False
        try:
            _require_directory_entry_absent(data_fd, temporary_name)
            os.symlink(str(relative_target), temporary_name, dir_fd=data_fd)
            temporary_metadata = os.stat(
                temporary_name,
                dir_fd=data_fd,
                follow_symlinks=False,
            )
            temporary_identity = _stat_identity(temporary_metadata)
            if (
                not stat.S_ISLNK(temporary_metadata.st_mode)
                or temporary_metadata.st_uid != os.geteuid()
                or temporary_metadata.st_nlink != 1
            ):
                raise PermissionError(
                    "temporary current pointer must be an owner-controlled symlink"
                )
            current_snapshot = (
                _stat_identity(os.stat("current", dir_fd=data_fd, follow_symlinks=False)),
                os.readlink("current", dir_fd=data_fd),
            )
            if current_snapshot != expected_snapshot:
                raise RuntimeError("current release pointer changed during activation")
            _rename_exchange(data_fd, temporary_name, data_fd, "current")
            exchanged = True
            displaced_snapshot = (
                _stat_identity(os.stat(temporary_name, dir_fd=data_fd, follow_symlinks=False)),
                os.readlink(temporary_name, dir_fd=data_fd),
            )
            if displaced_snapshot != expected_snapshot:
                raise RuntimeError("current release pointer changed during activation exchange")
            if (
                _stat_identity(os.stat("current", dir_fd=data_fd, follow_symlinks=False))
                != temporary_identity
            ):
                raise RuntimeError("current release pointer changed after activation exchange")
            os.fsync(data_fd)
            if (
                _stat_identity(os.stat(temporary_name, dir_fd=data_fd, follow_symlinks=False)),
                os.readlink(temporary_name, dir_fd=data_fd),
            ) != displaced_snapshot:
                raise RuntimeError("displaced current pointer changed before cleanup")
            os.unlink(temporary_name, dir_fd=data_fd)
            exchanged = False
            os.fsync(data_fd)
        except BaseException:
            if exchanged:
                try:
                    if (
                        temporary_identity is None
                        or displaced_snapshot is None
                        or _stat_identity(os.stat("current", dir_fd=data_fd, follow_symlinks=False))
                        != temporary_identity
                        or (
                            _stat_identity(
                                os.stat(
                                    temporary_name,
                                    dir_fd=data_fd,
                                    follow_symlinks=False,
                                )
                            ),
                            os.readlink(temporary_name, dir_fd=data_fd),
                        )
                        != displaced_snapshot
                    ):
                        raise RuntimeError("current pointer exchange rollback binding changed")
                    _rename_exchange(data_fd, temporary_name, data_fd, "current")
                    exchanged = False
                    os.fsync(data_fd)
                    if (
                        _stat_identity(os.stat("current", dir_fd=data_fd, follow_symlinks=False)),
                        os.readlink("current", dir_fd=data_fd),
                    ) != displaced_snapshot:
                        raise RuntimeError(
                            "current pointer exchange rollback did not restore selection"
                        )
                except BaseException as rollback_error:
                    raise RuntimeError(
                        "current pointer exchange failed and atomic rollback failed"
                    ) from rollback_error
            raise
        finally:
            if temporary_identity is not None and not exchanged:
                try:
                    observed_temporary = os.stat(
                        temporary_name,
                        dir_fd=data_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    if _stat_identity(observed_temporary) != temporary_identity:
                        raise RuntimeError("temporary current pointer changed during cleanup")
                    os.unlink(temporary_name, dir_fd=data_fd)
                    os.fsync(data_fd)
    finally:
        os.close(data_fd)


def activate_release(
    layout: ReleaseLayout,
    *,
    expected_current_commit: str | None = None,
) -> dict[str, Any]:
    """Atomically point ``current`` at one fully validated release."""

    with _release_lock(layout):
        candidate_identity = _path_identity(layout.release_root)
        status = _release_status(layout)
        if not status["ready"]:
            raise RuntimeError("release is not ready for activation")
        previous, versioned_snapshot = _validate_existing_current(
            layout,
            expected_current_commit=expected_current_commit,
        )
        if (
            _path_identity(layout.release_root) != candidate_identity
            or _release_status(layout).get("ready") is not True
        ):
            raise RuntimeError("candidate release changed during activation")
        relative_target = layout.release_root.relative_to(layout.current_link.parent)
        same_release_pointer_snapshot = None
        if previous == layout.release_root:
            try:
                pointer_metadata = layout.current_link.lstat()
                pointer_target = os.readlink(layout.current_link)
                resolved_pointer = layout.current_link.resolve(strict=True)
            except OSError as error:
                raise RuntimeError("current release pointer changed during activation") from error
            if not stat.S_ISLNK(pointer_metadata.st_mode) or resolved_pointer != previous:
                raise RuntimeError("current release pointer changed during activation")
            same_release_pointer_snapshot = (
                _stat_identity(pointer_metadata),
                pointer_target,
            )
        if same_release_pointer_snapshot is not None and same_release_pointer_snapshot[1] == str(
            relative_target
        ):
            changed = False
        elif same_release_pointer_snapshot is not None:
            _canonicalize_same_release_pointer(
                layout,
                relative_target=relative_target,
                expected_snapshot=same_release_pointer_snapshot,
            )
            changed = True
        else:
            temporary = layout.current_link.parent / f".current.tmp-{os.getpid()}"
            if os.path.lexists(temporary):
                raise FileExistsError(f"temporary current pointer already exists: {temporary}")
            try:
                temporary.symlink_to(relative_target, target_is_directory=True)
                if previous is None:
                    if os.path.lexists(layout.current_link):
                        raise RuntimeError("current release pointer changed during activation")
                elif versioned_snapshot is not None:
                    previous_layout = ReleaseLayout(
                        home=layout.home,
                        commit=previous.name,
                    )
                    try:
                        current_snapshot = _versioned_check_snapshot(
                            layout,
                            previous_layout,
                        )
                    except (OSError, RuntimeError, ValueError):
                        raise RuntimeError(
                            "current release pointer changed during activation"
                        ) from None
                    if current_snapshot != versioned_snapshot:
                        raise RuntimeError("current release pointer changed during activation")
                os.replace(temporary, layout.current_link)
                _fsync_directory(layout.current_link.parent)
            finally:
                if os.path.lexists(temporary):
                    temporary.unlink()
            changed = True
        if layout.current_link.resolve(strict=True) != layout.release_root or os.readlink(
            layout.current_link
        ) != str(relative_target):
            raise RuntimeError("atomic FIN release activation did not select the expected release")
        return {
            "active": True,
            "changed": changed,
            "commit": layout.commit,
            "current": str(layout.current_link),
            "release_root": str(layout.release_root),
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate, bind or activate one commit-bound FIN release.",
    )
    parser.add_argument(
        "action",
        choices=(
            "check",
            "record-sync",
            "prepare",
            "activate",
            "preflight-runtime-bytecode-quarantine",
            "quarantine-runtime-bytecode",
            "preflight-degraded-current-cutover",
            "activate-degraded-current-cutover",
            "rollback-degraded-current-cutover",
        ),
    )
    parser.add_argument("--commit", required=True, help="Exact lowercase 40-character SHA")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument(
        "--expected-current-commit",
        help="Expected active prior SHA for activate or runtime-bytecode quarantine actions",
    )
    parser.add_argument(
        "--degraded-prior-commit",
        help="Exact degraded prior SHA for degraded-current cutover actions",
    )
    parser.add_argument(
        "--expected-cutover-sha256",
        help="Exact preflight authority digest for degraded-current mutation actions",
    )
    parser.add_argument(
        "--expected-prior-current-pointer-sha256",
        help="Exact preflight current-pointer digest for degraded-current mutation actions",
    )
    arguments = parser.parse_args(argv)
    if not arguments.home.is_absolute():
        parser.error("home must be an absolute path")
    expected_current_actions = {
        "activate",
        "preflight-runtime-bytecode-quarantine",
        "quarantine-runtime-bytecode",
    }
    if (
        arguments.action not in expected_current_actions
        and arguments.expected_current_commit is not None
    ):
        parser.error(
            "expected current commit is only valid with activate or "
            "runtime-bytecode quarantine actions"
        )
    if (
        arguments.action in {"preflight-runtime-bytecode-quarantine", "quarantine-runtime-bytecode"}
        and arguments.expected_current_commit is None
    ):
        parser.error(f"{arguments.action} requires --expected-current-commit")
    degraded_actions = {
        "preflight-degraded-current-cutover",
        "activate-degraded-current-cutover",
        "rollback-degraded-current-cutover",
    }
    if arguments.action not in degraded_actions and (
        arguments.degraded_prior_commit is not None
        or arguments.expected_cutover_sha256 is not None
        or arguments.expected_prior_current_pointer_sha256 is not None
    ):
        parser.error(
            "degraded prior and cutover digests are only valid with "
            "degraded-current cutover actions"
        )
    if arguments.action in degraded_actions and arguments.degraded_prior_commit is None:
        parser.error(f"{arguments.action} requires --degraded-prior-commit")
    if arguments.action == "preflight-degraded-current-cutover" and (
        arguments.expected_cutover_sha256 is not None
        or arguments.expected_prior_current_pointer_sha256 is not None
    ):
        parser.error("preflight-degraded-current-cutover does not accept expected mutation digests")
    if arguments.action in {
        "activate-degraded-current-cutover",
        "rollback-degraded-current-cutover",
    } and (
        arguments.expected_cutover_sha256 is None
        or arguments.expected_prior_current_pointer_sha256 is None
    ):
        parser.error(
            f"{arguments.action} requires --expected-cutover-sha256 and "
            "--expected-prior-current-pointer-sha256"
        )
    layout = ReleaseLayout(home=arguments.home.expanduser(), commit=arguments.commit)

    if arguments.action == "check":
        result = inspect_release(layout)
        exit_code = 0 if result["ready"] else 1
    elif arguments.action == "record-sync":
        result = record_frozen_sync(layout)
        exit_code = 0
    elif arguments.action == "prepare":
        result = prepare_release_bindings(layout)
        exit_code = 0
    elif arguments.action == "activate":
        result = activate_release(
            layout,
            expected_current_commit=arguments.expected_current_commit,
        )
        exit_code = 0
    elif arguments.action == "preflight-runtime-bytecode-quarantine":
        assert arguments.expected_current_commit is not None
        result = preflight_active_release_runtime_bytecode_quarantine(
            layout,
            expected_current_commit=arguments.expected_current_commit,
        )
        exit_code = 0
    elif arguments.action == "quarantine-runtime-bytecode":
        assert arguments.expected_current_commit is not None
        result = quarantine_active_release_runtime_bytecode(
            layout,
            expected_current_commit=arguments.expected_current_commit,
        )
        exit_code = 0
    elif arguments.action == "preflight-degraded-current-cutover":
        assert arguments.degraded_prior_commit is not None
        result = preflight_degraded_current_cutover(
            layout,
            degraded_prior_commit=arguments.degraded_prior_commit,
        )
        exit_code = 0
    elif arguments.action == "activate-degraded-current-cutover":
        assert arguments.degraded_prior_commit is not None
        assert arguments.expected_cutover_sha256 is not None
        assert arguments.expected_prior_current_pointer_sha256 is not None
        result = activate_degraded_current_cutover(
            layout,
            degraded_prior_commit=arguments.degraded_prior_commit,
            expected_cutover_sha256=arguments.expected_cutover_sha256,
            expected_prior_current_pointer_sha256=(arguments.expected_prior_current_pointer_sha256),
        )
        exit_code = 0
    else:
        assert arguments.degraded_prior_commit is not None
        assert arguments.expected_cutover_sha256 is not None
        assert arguments.expected_prior_current_pointer_sha256 is not None
        result = rollback_degraded_current_cutover(
            layout,
            degraded_prior_commit=arguments.degraded_prior_commit,
            expected_cutover_sha256=arguments.expected_cutover_sha256,
            expected_prior_current_pointer_sha256=(arguments.expected_prior_current_pointer_sha256),
        )
        exit_code = 0
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
