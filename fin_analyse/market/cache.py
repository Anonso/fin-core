"""TTL-based cache for market data, with optional disk persistence."""

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from fin_analyse.utils.ids import stable_id


class MarketCache:
    """TTL-based cache with optional disk persistence.

    By default operates purely in-memory.  When *cache_dir* is provided,
    each ``set(... persist=True)`` also writes a JSON snapshot to disk so
    data survives process restarts.  Misses fall through to disk on next
    ``get()``.

    Key format: ``{provider}:{method}:{ticker}:{params_hash}``
    """

    def __init__(self, cache_dir: Path | None = None):
        self._store: dict[str, tuple[float, Any]] = {}
        self._cache_dir = cache_dir
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def make_key(self, provider: str, method: str, ticker: str, params: str = "") -> str:
        """Build a canonical cache key."""
        if params:
            return f"{provider}:{method}:{ticker}:{stable_id(params, digest_len=8)}"
        return f"{provider}:{method}:{ticker}"

    def _disk_path(self, key: str) -> Path | None:
        if not self._cache_dir:
            return None
        safe = key.replace("/", "_").replace("\\", "_")
        return self._cache_dir / f"{safe}.json"

    # ------------------------------------------------------------------
    # Get / Set / Invalidate
    # ------------------------------------------------------------------

    def get(self, key: str, *, allow_stale: bool = False) -> Any | None:
        """Get value if not expired, else return None.

        Checks memory first, then falls back to disk if available and not expired.
        When allow_stale=True, returns expired disk data as a fallback instead
        of None — useful for slow-changing data like northbound holdings.
        """
        entry = self._store.get(key)
        if entry is not None:
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]
            else:
                return value

        # Memory miss — try disk
        disk_path = self._disk_path(key)
        if disk_path and disk_path.exists():
            try:
                data = json.loads(disk_path.read_text(encoding="utf-8"))
                expires_at = data.get("_expires_at", 0)
                if expires_at > time.time():
                    value = data.get("_value")
                    self._store[key] = (expires_at - time.time() + time.monotonic(), value)
                    return value
                elif allow_stale:
                    value = data.get("_value")
                    if value is not None:
                        return value
                else:
                    with contextlib.suppress(OSError):
                        disk_path.unlink()
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def set(self, key: str, value: Any, ttl_seconds: int, persist: bool = False):
        """Store a value with TTL in seconds.

        If *persist* is True and *cache_dir* is configured, also write to disk.
        """
        self._store[key] = (time.monotonic() + ttl_seconds, value)

        if persist and self._cache_dir:
            disk_path = self._disk_path(key)
            if disk_path:
                try:
                    payload = {
                        "_value": value,
                        "_expires_at": time.time() + ttl_seconds,
                    }
                    disk_path.write_text(
                        json.dumps(payload, ensure_ascii=False, default=str),
                        encoding="utf-8",
                    )
                except OSError:
                    pass  # disk write failure is non-fatal

    def invalidate(self, pattern: str):
        """Remove all keys containing the pattern substring."""
        to_delete = [k for k in self._store if pattern in k]
        for k in to_delete:
            del self._store[k]


# Backward-compatible alias
TTLCache = MarketCache
