from __future__ import annotations

import pytest

from fin_analyse.market.evidence_plan import (
    MarketEvidencePlanError,
    compile_market_evidence_plan,
    load_market_evidence_plan,
)


def test_packaged_market_plan_freezes_current_production_topology() -> None:
    plan = load_market_evidence_plan()

    assert tuple(driver.driver_id for driver in plan.quote) == (
        "eastmoney_quote",
        "tencent_quote",
    )
    assert tuple(driver.driver_id for driver in plan.daily) == (
        "eastmoney_daily",
        "tencent_daily",
    )
    assert tuple(driver.driver_id for driver in plan.intraday) == (
        "tencent_intraday",
        "eastmoney_intraday",
    )
    assert plan.manifest_sha256


def test_compiler_consumes_enable_and_timeout_without_dynamic_adapters() -> None:
    plan = compile_market_evidence_plan(
        {
            "schema_version": "fin.market-evidence-plan/v1",
            "lanes": {
                "quote": [
                    {"driver_id": "tencent_quote", "enabled": True, "timeout_seconds": 4.0},
                    {"driver_id": "eastmoney_quote", "enabled": True, "timeout_seconds": 10.0},
                ],
                "daily": [
                    {"driver_id": "tencent_daily", "enabled": True, "timeout_seconds": 7.0},
                    {"driver_id": "eastmoney_daily", "enabled": False, "timeout_seconds": 12.0},
                ],
                "intraday": [
                    {
                        "driver_id": "tencent_intraday",
                        "enabled": True,
                        "timeout_seconds": 6.0,
                    },
                    {
                        "driver_id": "eastmoney_intraday",
                        "enabled": False,
                        "timeout_seconds": 12.0,
                    },
                ],
            },
        }
    )

    assert tuple(driver.driver_id for driver in plan.daily) == ("tencent_daily",)
    assert plan.daily[0].timeout_seconds == 7.0
    assert tuple(driver.driver_id for driver in plan.quote) == (
        "tencent_quote",
        "eastmoney_quote",
    )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda raw: raw["lanes"]["quote"][0].update(driver_id="arbitrary_import"),
        lambda raw: raw["lanes"]["daily"][0].update(timeout_seconds=301),
        lambda raw: raw["lanes"]["intraday"][0].update(python="pkg.module:factory"),
        lambda raw: raw["lanes"]["quote"][1].update(enabled=False),
    ),
)
def test_market_plan_rejects_unknown_authority_and_unsafe_topology(mutation) -> None:
    raw = {
        "schema_version": "fin.market-evidence-plan/v1",
        "lanes": {
            "quote": [
                {"driver_id": "eastmoney_quote", "enabled": True, "timeout_seconds": 15.0},
                {"driver_id": "tencent_quote", "enabled": True, "timeout_seconds": 5.0},
            ],
            "daily": [
                {"driver_id": "eastmoney_daily", "enabled": True, "timeout_seconds": 15.0},
                {"driver_id": "tencent_daily", "enabled": True, "timeout_seconds": 8.0},
            ],
            "intraday": [
                {
                    "driver_id": "tencent_intraday",
                    "enabled": True,
                    "timeout_seconds": 8.0,
                },
                {
                    "driver_id": "eastmoney_intraday",
                    "enabled": True,
                    "timeout_seconds": 15.0,
                },
            ],
        },
    }
    mutation(raw)

    with pytest.raises(MarketEvidencePlanError):
        compile_market_evidence_plan(raw)
