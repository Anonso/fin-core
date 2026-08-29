"""Migration guard: gateway MarketService must not be a production snapshot seam."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_gateway_market_service_is_not_a_production_snapshot_seam() -> None:
    """Assert gateway services.py no longer contains class MarketService.

    The gateway MarketService composite snapshot seam has been replaced by
    MarketSnapshotService in fin_analyse.market.snapshot.  This guard
    prevents accidental reintroduction.
    """
    services = _read("fin_analyse/gateway/services.py")
    assert "class MarketService" not in services, (
        "gateway services.py still contains class MarketService — "
        "the old composite snapshot seam must be removed"
    )

    offenders: list[str] = []
    for path in sorted((ROOT / "fin_analyse").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        # MCP tool function name is allowed to keep the string "get_market_snapshot"
        source = path.read_text(encoding="utf-8")
        if (
            "from fin_analyse.gateway.services import MarketService" in source
            or "MarketService(" in source
        ):
            offenders.append(rel)

    assert offenders == [], (
        f"Production code still imports or instantiates gateway MarketService: {offenders}"
    )
