"""Robust mootdx client creation with explicit server fallback."""

from __future__ import annotations

import socket
from collections.abc import Iterable

from mootdx.quotes import Quotes

_TDX_SERVERS: list[tuple[str, int]] = [
    ("119.97.185.59", 7709),
    ("124.70.133.119", 7709),
    ("116.205.183.150", 7709),
    ("123.60.73.44", 7709),
    ("116.205.163.254", 7709),
    ("121.36.225.169", 7709),
    ("123.60.70.228", 7709),
    ("124.71.9.153", 7709),
    ("110.41.147.114", 7709),
    ("124.71.187.122", 7709),
]


def _probe(ip: str, port: int, timeout: float = 2.0) -> bool:
    """Return True when a TCP server accepts a connection."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def tdx_client(market: str = "std", servers: Iterable[tuple[str, int]] | None = None):
    """Create a mootdx client while avoiding empty BESTIP config failures."""
    candidates = list(servers or _TDX_SERVERS)
    for ip, port in candidates:
        if _probe(ip, port):
            return Quotes.factory(market=market, server=(ip, port))

    try:
        return Quotes.factory(market=market, bestip=True)
    except Exception:
        pass

    try:
        return Quotes.factory(market=market)
    except Exception as exc:
        raise RuntimeError(
            "mootdx client unavailable: all configured TDX servers failed, "
            "bestip fallback failed, and bare Quotes.factory() failed"
        ) from exc
