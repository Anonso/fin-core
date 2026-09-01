from __future__ import annotations

import pytest

from fin_analyse.operations.daily_workspace_generator import (
    DailyWorkspaceGenerationUnavailableError,
    L1DirectWorkspaceGenerator,
)


class _EmptyBackend:
    def __init__(self) -> None:
        self.budgets: list[tuple[float, float]] = []

    def complete_bounded(
        self,
        prompt: str,
        *,
        total_timeout_seconds: float,
        wire_timeout_seconds: float,
        before_attempt: object,
    ) -> str:
        del prompt, before_attempt
        self.budgets.append((total_timeout_seconds, wire_timeout_seconds))
        return "[]"


def test_backend_chain_shares_one_total_budget() -> None:
    first = _EmptyBackend()
    second = _EmptyBackend()
    generator = L1DirectWorkspaceGenerator(attempt_timeout_seconds=10)
    generator._backends = (("first", first), ("second", second))

    with pytest.raises(DailyWorkspaceGenerationUnavailableError):
        generator._complete("brief")

    assert first.budgets[0][0] == pytest.approx(5, abs=0.1)
    assert second.budgets[0][0] == pytest.approx(5, abs=0.1)
    assert sum(backend.budgets[0][0] for backend in (first, second)) <= 10.1
    assert all(total == wire for backend in (first, second) for total, wire in backend.budgets)


def test_one_backend_keeps_the_full_budget() -> None:
    backend = _EmptyBackend()
    generator = L1DirectWorkspaceGenerator(attempt_timeout_seconds=10)
    generator._backends = (("only", backend),)

    with pytest.raises(DailyWorkspaceGenerationUnavailableError):
        generator._complete("brief")

    assert backend.budgets[0][0] == pytest.approx(10, abs=0.1)
