"""Trusted principal bindings for semantic decision guidance.

Public callers never construct these values.  Gateway infrastructure obtains a
binding from a configured provider and injects it into the domain service.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_NAMESPACE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PRINCIPAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_IDENTITY_PATTERN = re.compile(r"[0-9A-Fa-f]{64}\Z")


class PrincipalBindingError(RuntimeError):
    """Fail-closed authentication failure safe for public projection."""

    problem_code = "authentication_required"
    retryable = False

    def __init__(self) -> None:
        super().__init__("A trusted principal binding is required.")


@dataclass(frozen=True, slots=True)
class PrincipalBinding:
    """Immutable, server-authenticated principal identity."""

    namespace: str
    principal_id: str

    def __post_init__(self) -> None:
        if _NAMESPACE_PATTERN.fullmatch(self.namespace) is None:
            raise ValueError("principal namespace is invalid")
        if _PRINCIPAL_ID_PATTERN.fullmatch(self.principal_id) is None:
            raise ValueError("principal id is invalid")


class PrincipalBindingProvider(Protocol):
    """Gateway-owned seam that supplies an authenticated binding."""

    def require_binding(self) -> PrincipalBinding:
        """Return a trusted binding or fail closed."""
        ...


@dataclass(frozen=True, slots=True)
class FakePrincipalBindingProvider:
    """Explicit test provider; never derives identity from public input."""

    binding: PrincipalBinding | None

    @classmethod
    def from_ids(cls, *, namespace: str, principal_id: str) -> FakePrincipalBindingProvider:
        return cls(binding=PrincipalBinding(namespace=namespace, principal_id=principal_id))

    def require_binding(self) -> PrincipalBinding:
        if self.binding is None:
            raise PrincipalBindingError
        return self.binding


@dataclass(frozen=True, slots=True)
class LocalInstallationPrincipalProvider:
    """Read a provisioned owner-only local installation identity."""

    identity_path: Path
    installation_namespace: str

    def __post_init__(self) -> None:
        if _NAMESPACE_PATTERN.fullmatch(self.installation_namespace) is None:
            raise ValueError("installation namespace is invalid")

    def require_binding(self) -> PrincipalBinding:
        identity = self._read_identity()
        digest = hashlib.sha256(
            self.installation_namespace.encode("utf-8") + b"\0" + identity
        ).hexdigest()
        return PrincipalBinding(
            namespace=self.installation_namespace,
            principal_id=f"finp_{digest}",
        )

    def _read_identity(self) -> bytes:
        return _read_local_installation_identity(self.identity_path)


@dataclass(frozen=True, slots=True)
class LocalInstallationPrincipalReader:
    """Read-only installation identity reader.

    Mirrors the identity-verification performed by
    :class:`LocalInstallationPrincipalProvider` without constructing it.
    The MCP admission path for read-only tools (e.g. ``read_actual_portfolio``)
    uses this reader directly so that calling the tool from a fresh process
    does not trigger the production semantic composition (ResearchStateRepository
    warm-up writes SQLite/WAL/migration side effects).  Callers must pass the
    exact ``identity_path`` already provisioned for the canonical namespace so
    the reader never derives it from any public input.
    """

    identity_path: Path
    installation_namespace: str

    def __post_init__(self) -> None:
        if _NAMESPACE_PATTERN.fullmatch(self.installation_namespace) is None:
            raise ValueError("installation namespace is invalid")

    def read_binding(self) -> PrincipalBinding:
        identity = _read_local_installation_identity(self.identity_path)
        digest = hashlib.sha256(
            self.installation_namespace.encode("utf-8") + b"\0" + identity
        ).hexdigest()
        return PrincipalBinding(
            namespace=self.installation_namespace,
            principal_id=f"finp_{digest}",
        )


def _read_local_installation_identity(path: Path) -> bytes:
    """Shared identity-byte reader used by both provider and reader.

    Verifies the file is a regular, owner-only 0600 file under 4 KiB with a
    256-bit hex identity, raising :class:`PrincipalBindingError` (a stable
    non-disclosing failure code) on any deviation.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        owner_mode = stat.S_IRUSR | stat.S_IWUSR
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != owner_mode
        ):
            raise PrincipalBindingError
        raw = os.read(descriptor, 4_097)
        if len(raw) > 4_096:
            raise PrincipalBindingError
        text = raw.decode("ascii")
        identity_text = text.rstrip("\r\n")
        if _IDENTITY_PATTERN.fullmatch(identity_text) is None:
            raise PrincipalBindingError
        return bytes.fromhex(identity_text)
    except PrincipalBindingError:
        raise
    except (OSError, UnicodeDecodeError, ValueError):
        raise PrincipalBindingError from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
