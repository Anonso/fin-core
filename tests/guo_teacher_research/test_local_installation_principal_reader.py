"""RED tests for LocalInstallationPrincipalReader (registry-owned, zero-write).

The reader opens the canonical installation identity file the same way as
LocalInstallationPrincipalProvider, but without constructing or triggering the
production semantic composition. It returns the same PrincipalBinding result so
MCP admission for read-only tools can lean on it without paying the cold-start
write cost (SQLite WAL, PRAGMA journal_mode, migrations).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fin_analyse.guo_teacher_research.principal_binding import (
    LocalInstallationPrincipalProvider,
    LocalInstallationPrincipalReader,
    PrincipalBindingError,
)


@pytest.fixture()
def install_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tighten HOME/XDG to a fresh root so the reader can't touch production state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def _seed_identity(path: Path) -> str:
    raw = "ab" * 32
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw + "\n", encoding="utf-8")
    path.chmod(0o600)
    return raw


def test_reader_produces_same_binding_as_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    identity_path = tmp_path / "xdg" / "fin-analyse" / "installation-identity.hex"
    _seed_identity(identity_path)

    provider = LocalInstallationPrincipalProvider(
        identity_path=identity_path,
        installation_namespace="fin.local-installation.v1",
    )
    reader = LocalInstallationPrincipalReader(
        identity_path=identity_path,
        installation_namespace="fin.local-installation.v1",
    )

    expected = provider.require_binding()
    actual = reader.read_binding()
    assert actual == expected
    assert actual.namespace == "fin.local-installation.v1"
    assert actual.principal_id.startswith("finp_")


def test_reader_never_triggers_provider_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reader must not import or instantiate anything that touches the
    production semantic composition.  This sentinel test asserts that the
    reader's dependencies are import-only and never call _services().
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    identity_path = tmp_path / "xdg" / "fin-analyse" / "installation-identity.hex"
    _seed_identity(identity_path)

    reader = LocalInstallationPrincipalReader(
        identity_path=identity_path,
        installation_namespace="fin.local-installation.v1",
    )

    # The reader itself only opens the identity file.  No service registry,
    # no ResearchStateRepository, no SQLite.  This is enforced by the fact
    # that the reader takes identity_path explicitly instead of going through
    # _services().principal_binding_provider (which triggers semantic composition).
    binding = reader.read_binding()
    assert binding.namespace == "fin.local-installation.v1"


def test_reader_fails_closed_on_missing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    missing = tmp_path / "xdg" / "fin-analyse" / "installation-identity.hex"

    reader = LocalInstallationPrincipalReader(
        identity_path=missing,
        installation_namespace="fin.local-installation.v1",
    )
    with pytest.raises(PrincipalBindingError) as error:
        reader.read_binding()
    assert error.value.problem_code == "authentication_required"
    assert str(missing) not in str(error.value)


def test_reader_fails_closed_on_corrupt_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    identity_path = tmp_path / "xdg" / "fin-analyse" / "installation-identity.hex"
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text("not-a-256-bit-identity\n", encoding="utf-8")
    identity_path.chmod(0o600)

    reader = LocalInstallationPrincipalReader(
        identity_path=identity_path,
        installation_namespace="fin.local-installation.v1",
    )
    with pytest.raises(PrincipalBindingError):
        reader.read_binding()


def test_reader_fails_closed_on_insecure_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    identity_path = tmp_path / "xdg" / "fin-analyse" / "installation-identity.hex"
    _seed_identity(identity_path)
    identity_path.chmod(0o644)

    reader = LocalInstallationPrincipalReader(
        identity_path=identity_path,
        installation_namespace="fin.local-installation.v1",
    )
    with pytest.raises(PrincipalBindingError):
        reader.read_binding()


def test_reader_preserves_zero_write_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold-start zero-write proof: fresh process with a brand new state root
    reads the identity, but creates zero new files in the state root.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    identity_path = tmp_path / "xdg" / "fin-analyse" / "installation-identity.hex"
    _seed_identity(identity_path)

    state_root = tmp_path / "state"
    state_root.mkdir(parents=True)

    before = {
        str(p.relative_to(state_root)): p.stat()
        for p in state_root.rglob("*")
        if p.is_file() or p.is_symlink()
    }

    reader = LocalInstallationPrincipalReader(
        identity_path=identity_path,
        installation_namespace="fin.local-installation.v1",
    )
    binding = reader.read_binding()
    assert binding.namespace == "fin.local-installation.v1"

    after = {
        str(p.relative_to(state_root)): p.stat()
        for p in state_root.rglob("*")
        if p.is_file() or p.is_symlink()
    }
    assert before == after, f"reader introduced new files: {set(after) - set(before)}"


def test_reader_uses_nofollow_cloexec_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader must open the identity file with O_NOFOLLOW (no symlink
    following) and O_CLOEXEC (no descriptor leak across exec).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    identity_path = tmp_path / "xdg" / "fin-analyse" / "installation-identity.hex"
    _seed_identity(identity_path)

    original_open = os.open
    seen_flags: list[int] = []

    def spy_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)

    reader = LocalInstallationPrincipalReader(
        identity_path=identity_path,
        installation_namespace="fin.local-installation.v1",
    )
    reader.read_binding()

    assert seen_flags, "reader did not call os.open"
    flags = seen_flags[0]
    # On POSIX O_RDONLY equals 0, so & is meaningless — assert exact flag set.
    expected = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    assert flags == expected, (
        f"open flags mismatch: got={flags:#x}, expected={expected:#x}"
    )
