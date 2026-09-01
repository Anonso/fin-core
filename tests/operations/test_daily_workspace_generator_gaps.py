"""L1 direct generator material-gap accounting (design daily-gap-ledger v2).

Two rules from the B1 blind-eval L3 attribution: missing vs unrenderable
materials get distinct gap codes, and a checkpoint whose two core data faces
are both broken must not generate a normal-shaped briefing at all — it raises
the same typed unavailable error the backend-failure path uses, so the
existing handlers emit the deterministic degraded notice with these codes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest

from fin_analyse.operations.daily_workspace_generator import (
    DailyWorkspaceGenerationUnavailableError,
    L1DirectWorkspaceGenerator,
    _material_gaps,
    _record_market_overview_failure_diagnostic,
)

_OVERVIEW_REPR = "<AshareMarketOverviewService object at 0x7f0000000000>"


class _SealedBackend:
    def __init__(self) -> None:
        self.calls = 0

    def complete_bounded(
        self,
        prompt: str,
        *,
        total_timeout_seconds: float,
        wire_timeout_seconds: float,
        before_attempt: Any,
    ) -> str:
        self.calls += 1
        return "正常简报正文。"


def _config(tmp_path: Path) -> str:
    path = tmp_path / "llm.yaml"
    path.write_text(
        "models:\n"
        "  sealed_t0:\n"
        "    provider: openai_compatible\n"
        "    model: sealed-model\n"
        "    api_key: sk-sealed\n"
        "    base_url: http://127.0.0.1:9\n"
        "    enabled: true\n"
        "priorities:\n"
        "  t0: [sealed_t0]\n",
        encoding="utf-8",
    )
    return str(path)


def _generator(
    materials: dict[str, str | None],
    *,
    tmp_path: Path,
    backend: _SealedBackend,
) -> L1DirectWorkspaceGenerator:
    return L1DirectWorkspaceGenerator(
        config_path=_config(tmp_path),
        backend_factory=lambda name: backend,
        material_provider=lambda question: materials,
    )


def test_material_gaps_splits_missing_from_unrenderable() -> None:
    gaps = _material_gaps(
        {
            "portfolio": None,
            "market_overview": _OVERVIEW_REPR,
            "g_context": "G 认知参考正文",
            "g_reference": "参考材料正文",
        }
    )
    assert gaps == (
        "l1_material_portfolio_unavailable",
        "l1_material_market_overview_unrenderable",
    )


def test_both_core_faces_broken_raises_typed_unavailable(tmp_path: Path) -> None:
    backend = _SealedBackend()
    generator = _generator(
        {
            "portfolio": None,
            "market_overview": _OVERVIEW_REPR,
            "g_context": None,
            "g_reference": None,
        },
        tmp_path=tmp_path,
        backend=backend,
    )
    with pytest.raises(DailyWorkspaceGenerationUnavailableError) as raised:
        generator.generate(
            snapshot={"checkpoint": "close", "trading_day_id": "2026-08-30"},
            principal=object(),
        )
    assert raised.value.data_gaps == (
        "l1_material_portfolio_unavailable",
        "l1_material_market_overview_unrenderable",
        "l1_material_g_context_unavailable",
        "l1_material_g_reference_unavailable",
    )
    assert backend.calls == 0


def test_single_broken_face_still_generates_with_gap_codes(tmp_path: Path) -> None:
    backend = _SealedBackend()
    generator = _generator(
        {
            "portfolio": "持仓快照正文",
            "market_overview": None,
            "g_context": "G 认知参考正文",
            "g_reference": "参考材料正文",
        },
        tmp_path=tmp_path,
        backend=backend,
    )
    product = generator.generate(
        snapshot={"checkpoint": "close", "trading_day_id": "2026-08-30"},
        principal=object(),
    )
    assert product["data_gaps"] == ["l1_material_market_overview_unavailable"]
    assert product["consultation_product"]["answer_text"] == "正常简报正文。"
    assert "degraded" not in product


def test_no_material_provider_keeps_existing_behaviour(tmp_path: Path) -> None:
    backend = _SealedBackend()
    generator = L1DirectWorkspaceGenerator(
        config_path=_config(tmp_path),
        backend_factory=lambda name: backend,
    )
    product = generator.generate(
        snapshot={"checkpoint": "premarket", "trading_day_id": "2026-08-30"},
        principal=object(),
    )
    assert product["data_gaps"] == []


def test_disabled_t0_entry_is_skipped_on_l1_chain(tmp_path: Path) -> None:
    config_path = tmp_path / "llm.yaml"
    config_path.write_text(
        "models:\n"
        "  disabled_t0:\n"
        "    provider: openai_compatible\n"
        "    model: sealed-model\n"
        "    api_key: sk-sealed\n"
        "    base_url: http://127.0.0.1:9\n"
        "    enabled: false\n"
        "priorities:\n"
        "  t0: [disabled_t0]\n",
        encoding="utf-8",
    )
    generator = L1DirectWorkspaceGenerator(config_path=str(config_path))

    assert generator._resolve_backends() == ()


def test_disabled_priority_entry_does_not_consume_l1_chain_slot(tmp_path: Path) -> None:
    config_path = tmp_path / "llm.yaml"
    config_path.write_text(
        "models:\n"
        "  disabled_first:\n"
        "    provider: openai_compatible\n"
        "    model: sealed-model\n"
        "    api_key: sk-sealed\n"
        "    base_url: http://127.0.0.1:9\n"
        "    enabled: false\n"
        "  second:\n"
        "    provider: openai_compatible\n"
        "    model: sealed-model\n"
        "    api_key: sk-sealed\n"
        "    base_url: http://127.0.0.1:9\n"
        "    enabled: true\n"
        "  third:\n"
        "    provider: openai_compatible\n"
        "    model: sealed-model\n"
        "    api_key: sk-sealed\n"
        "    base_url: http://127.0.0.1:9\n"
        "    enabled: true\n"
        "priorities:\n"
        "  t0: [disabled_first, second, third]\n",
        encoding="utf-8",
    )
    generator = L1DirectWorkspaceGenerator(config_path=str(config_path))

    assert [name for name, _backend in generator._resolve_backends()] == ["second", "third"]


def test_overview_failure_diagnostic_written_for_unknown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    result = SimpleNamespace(
        status="UNKNOWN",
        data_gaps=("MARKET_OVERVIEW_INDEX_TRADE_DATE_MISMATCH", "MARKET_OVERVIEW_UNAVAILABLE"),
        session_phase="PRE_OPEN",
        effective_trade_date="2026-08-31",
        observation_mode="LATEST_COMPLETED_SESSION",
        provider_updated_at=None,
        provider_observation_age_seconds=None,
        queried_at=None,
    )

    _record_market_overview_failure_diagnostic(result)

    lines = (
        tmp_path / "fin-analyse" / "daily-workspace-overview-failures.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["status"] == "UNKNOWN"
    assert "MARKET_OVERVIEW_INDEX_TRADE_DATE_MISMATCH" in record["data_gaps"]
    assert record["observation_mode"] == "LATEST_COMPLETED_SESSION"


def test_overview_failure_diagnostic_skipped_for_partial(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _record_market_overview_failure_diagnostic(SimpleNamespace(status="PARTIAL"))

    target = tmp_path / "fin-analyse" / "daily-workspace-overview-failures.jsonl"
    assert not target.exists()
