"""Read-only per-user real holdings store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UserPosition:
    ticker: str
    company: str
    shares: float | int | None = None
    avg_cost: float | None = None
    market: str | None = None
    added: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company": self.company,
            "shares": self.shares,
            "avg_cost": self.avg_cost,
            "market": self.market,
            "added": self.added,
        }


@dataclass(frozen=True)
class UserPortfolio:
    user_id: str
    positions: list[UserPosition]
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "updated_at": self.updated_at,
            "positions": [position.to_dict() for position in self.positions],
        }
