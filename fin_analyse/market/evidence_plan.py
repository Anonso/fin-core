"""Strict production topology for on-demand A-share market evidence.

The manifest can only order, enable, and bound drivers installed in FIN's
static catalog.  It cannot name imports, endpoints, credentials, or arbitrary
providers, and therefore is not a second market-data control plane.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, NamedTuple


class MarketEvidencePlanError(ValueError):
    """The market evidence manifest violates the closed topology contract."""


class MarketEvidenceDriver(NamedTuple):
    driver_id: str
    timeout_seconds: float


class MarketEvidencePlan(NamedTuple):
    schema_version: str
    manifest_sha256: str
    quote: tuple[MarketEvidenceDriver, ...]
    daily: tuple[MarketEvidenceDriver, ...]
    intraday: tuple[MarketEvidenceDriver, ...]


_MANIFEST_PATH = Path(__file__).with_name("market_evidence_plan.v1.json")
_TOP_LEVEL_KEYS = {"schema_version", "lanes"}
_LANE_KEYS = {"quote", "daily", "intraday"}
_DRIVER_KEYS = {"driver_id", "enabled", "timeout_seconds"}
_STATIC_LANE_CATALOG: Final = {
    "quote": frozenset({"eastmoney_quote", "tencent_quote"}),
    "daily": frozenset({"eastmoney_daily", "tencent_daily"}),
    "intraday": frozenset({"eastmoney_intraday", "tencent_intraday"}),
}


def compile_market_evidence_plan(raw: Mapping[str, object]) -> MarketEvidencePlan:
    payload: dict[str, Any] = dict(raw)
    if set(payload) != _TOP_LEVEL_KEYS:
        raise MarketEvidencePlanError("market evidence manifest schema is invalid")
    if payload["schema_version"] != "fin.market-evidence-plan/v1":
        raise MarketEvidencePlanError("market evidence manifest version is invalid")
    raw_lanes = payload["lanes"]
    if not isinstance(raw_lanes, dict) or set(raw_lanes) != _LANE_KEYS:
        raise MarketEvidencePlanError("market evidence lanes are invalid")

    compiled = {
        lane: _compile_lane(lane, raw_lanes[lane])
        for lane in ("quote", "daily", "intraday")
    }
    if len(compiled["quote"]) != 2:
        raise MarketEvidencePlanError("quote lane requires two independent sources")
    if not compiled["daily"] or not compiled["intraday"]:
        raise MarketEvidencePlanError("bar lanes require at least one source")

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return MarketEvidencePlan(
        schema_version="fin.market-evidence-plan/v1",
        manifest_sha256=hashlib.sha256(canonical).hexdigest(),
        quote=compiled["quote"],
        daily=compiled["daily"],
        intraday=compiled["intraday"],
    )


def _compile_lane(lane: str, raw_drivers: object) -> tuple[MarketEvidenceDriver, ...]:
    if not isinstance(raw_drivers, list):
        raise MarketEvidencePlanError(f"market evidence {lane} lane must be a list")
    seen: set[str] = set()
    compiled: list[MarketEvidenceDriver] = []
    for raw_driver in raw_drivers:
        if not isinstance(raw_driver, dict) or set(raw_driver) != _DRIVER_KEYS:
            raise MarketEvidencePlanError(f"market evidence {lane} driver is invalid")
        driver_id = raw_driver["driver_id"]
        enabled = raw_driver["enabled"]
        timeout = raw_driver["timeout_seconds"]
        if (
            not isinstance(driver_id, str)
            or driver_id not in _STATIC_LANE_CATALOG[lane]
            or driver_id in seen
        ):
            raise MarketEvidencePlanError(f"market evidence {lane} driver is not installed")
        if not isinstance(enabled, bool):
            raise MarketEvidencePlanError(f"market evidence {lane} enable flag is invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise MarketEvidencePlanError(f"market evidence {lane} timeout is invalid")
        normalized_timeout = float(timeout)
        if not math.isfinite(normalized_timeout) or not 0 < normalized_timeout <= 300:
            raise MarketEvidencePlanError(f"market evidence {lane} timeout is invalid")
        seen.add(driver_id)
        if enabled:
            compiled.append(MarketEvidenceDriver(driver_id, normalized_timeout))
    return tuple(compiled)


def load_market_evidence_plan(path: Path = _MANIFEST_PATH) -> MarketEvidencePlan:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MarketEvidencePlanError("market evidence manifest is unreadable") from error
    if not isinstance(raw, dict):
        raise MarketEvidencePlanError("market evidence manifest must be an object")
    return compile_market_evidence_plan(raw)


DEFAULT_MARKET_EVIDENCE_PLAN: Final = load_market_evidence_plan()
