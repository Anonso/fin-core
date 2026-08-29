from dataclasses import FrozenInstanceError

import pytest

from fin_analyse.guo_teacher_research.principal_binding import (
    FakePrincipalBindingProvider,
    LocalInstallationPrincipalProvider,
    PrincipalBinding,
    PrincipalBindingError,
)


def test_fake_provider_injects_an_immutable_trusted_binding() -> None:
    first = FakePrincipalBindingProvider.from_ids(
        namespace="installation-a", principal_id="principal-a"
    )
    second = FakePrincipalBindingProvider.from_ids(
        namespace="installation-b", principal_id="principal-b"
    )

    assert first.require_binding() == PrincipalBinding(
        namespace="installation-a", principal_id="principal-a"
    )
    assert second.require_binding() != first.require_binding()
    with pytest.raises(FrozenInstanceError):
        first.require_binding().principal_id = "caller-override"  # type: ignore[misc]

    with pytest.raises(PrincipalBindingError) as error:
        FakePrincipalBindingProvider(binding=None).require_binding()
    assert error.value.problem_code == "authentication_required"


def test_local_provider_fails_closed_for_missing_corrupt_or_insecure_identity(tmp_path) -> None:
    identity_path = tmp_path / "installation.identity"
    provider = LocalInstallationPrincipalProvider(
        identity_path=identity_path,
        installation_namespace="local-installation",
    )

    with pytest.raises(PrincipalBindingError) as missing:
        provider.require_binding()
    assert missing.value.problem_code == "authentication_required"
    assert str(identity_path) not in str(missing.value)

    identity_path.write_text("not-a-256-bit-identity\n", encoding="utf-8")
    identity_path.chmod(0o600)
    with pytest.raises(PrincipalBindingError):
        provider.require_binding()

    raw_identity = "ab" * 32
    identity_path.write_text(raw_identity + "\n", encoding="utf-8")
    identity_path.chmod(0o644)
    with pytest.raises(PrincipalBindingError):
        provider.require_binding()

    identity_path.chmod(0o600)
    first = provider.require_binding()
    second = provider.require_binding()
    assert first == second
    assert first.namespace == "local-installation"
    assert first.principal_id.startswith("finp_")
    assert raw_identity not in first.principal_id
    assert raw_identity not in repr(provider)
