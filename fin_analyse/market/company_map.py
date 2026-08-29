"""Company name → ticker mapping with offline A-share fallback.

A-share name→ticker is STATIC data — we load a pre-built JSON map
(5,528 stocks) instead of downloading the full list from network on
every ``search_stock`` call.  Rebuild the JSON with::

    .venv/bin/python scripts/rebuild_name_map.py

Manual entries for non-A-share (US / HK / unlisted) remain inline.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Inline manual mapping (non-A-share: US, HK, unlisted) ─────────────

COMPANY_TO_TICKER: dict[str, dict] = {
    # US stocks (via yfinance)
    "英伟达": {"ticker": "NVDA", "market": "美股", "name": "NVIDIA"},
    "NVIDIA": {"ticker": "NVDA", "market": "美股", "name": "NVIDIA"},
    "特斯拉": {"ticker": "TSLA", "market": "美股", "name": "Tesla"},
    "苹果": {"ticker": "AAPL", "market": "美股", "name": "Apple"},
    "微软": {"ticker": "MSFT", "market": "美股", "name": "Microsoft"},
    "谷歌": {"ticker": "GOOGL", "market": "美股", "name": "Alphabet"},
    "亚马逊": {"ticker": "AMZN", "market": "美股", "name": "Amazon"},
    "Meta": {"ticker": "META", "market": "美股", "name": "Meta"},
    "AMD": {"ticker": "AMD", "market": "美股", "name": "AMD"},
    "英特尔": {"ticker": "INTC", "market": "美股", "name": "Intel"},
    "高通": {"ticker": "QCOM", "market": "美股", "name": "Qualcomm"},
    "台积电": {"ticker": "TSM", "market": "美股", "name": "TSMC"},
    "美光": {"ticker": "MU", "market": "美股", "name": "Micron"},
    "ASML": {"ticker": "ASML", "market": "美股", "name": "ASML"},
    "博通": {"ticker": "AVGO", "market": "美股", "name": "Broadcom"},
    # HK stocks
    "阿里巴巴": {"ticker": "9988.HK", "market": "港股", "name": "阿里巴巴"},
    "腾讯": {"ticker": "0700.HK", "market": "港股", "name": "腾讯控股"},
    "美团": {"ticker": "3690.HK", "market": "港股", "name": "美团"},
    "小米": {"ticker": "1810.HK", "market": "港股", "name": "小米集团"},
    "快手": {"ticker": "1024.HK", "market": "港股", "name": "快手"},
    "京东": {"ticker": "9618.HK", "market": "港股", "name": "京东"},
    "百度": {"ticker": "9888.HK", "market": "港股", "name": "百度"},
    "比亚迪": {"ticker": "1211.HK", "market": "港股", "name": "比亚迪"},
    "网易": {"ticker": "9999.HK", "market": "港股", "name": "网易"},
    "极兔速递": {"ticker": "1519.HK", "market": "港股", "name": "极兔速递"},
    "顺丰同城": {"ticker": "9699.HK", "market": "港股", "name": "顺丰同城"},
    "中通快递": {"ticker": "2057.HK", "market": "港股", "name": "中通快递"},
    "圆通速递": {"ticker": "6123.HK", "market": "港股", "name": "圆通速递"},
    # Unlisted / private companies
    "华为": {"ticker": None, "market": "未上市", "name": "华为"},
    "字节跳动": {"ticker": None, "market": "未上市", "name": "字节跳动"},
    "大疆": {"ticker": None, "market": "未上市", "name": "大疆"},
    "壁仞": {"ticker": None, "market": "未上市", "name": "壁仞科技"},
    "平头哥": {"ticker": None, "market": "未上市", "name": "平头哥"},
    "国星宇航": {"ticker": None, "market": "未上市", "name": "国星宇航"},
    "中科宇航": {"ticker": None, "market": "未上市", "name": "中科宇航"},
    # Other foreign
    "康宁": {"ticker": "GLW", "market": "美股", "name": "Corning"},
    "藤仓": {"ticker": "5803.T", "market": "日股", "name": "Fujikura"},
    "九方智投": {"ticker": "9636.HK", "market": "港股", "name": "九方智投"},
}

# ── Offline A-share map (5,528 stocks, lazy-loaded) ──────────────────

_A_SHARE_MAP: dict[str, dict] | None = None
"""Lazy-loaded from knowledge-base/runtime/a_share_name_map.json."""

_A_SHARE_MAP_PATH_DEFAULT = "runtime/a_share_name_map.json"


def _get_a_share_map_path() -> Path | None:
    """Resolve the A-share map file via the production knowledge-root seam.

    Returns None (treated as "map unavailable") when the seam fails closed.
    """
    try:
        from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

        return default_knowledge_base_root() / _A_SHARE_MAP_PATH_DEFAULT
    except RuntimeError as exc:
        logger.warning("Knowledge root unavailable — A-share map disabled: %s", exc)
        return None


def _load_a_share_map(force: bool = False) -> dict[str, dict]:
    """Load the A-share name→ticker JSON map (lazy, cached)."""
    global _A_SHARE_MAP
    if _A_SHARE_MAP is not None and not force:
        return _A_SHARE_MAP
    path = _get_a_share_map_path()
    if path is None or not path.exists():
        logger.warning("A-share map not found — run scripts/rebuild_name_map.py")
        _A_SHARE_MAP = {}
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        entries: dict[str, dict] = data.get("entries", {})
        _A_SHARE_MAP = entries
        logger.debug("Loaded %d A-share entries from %s", len(entries), path)
        return entries
    except Exception as exc:
        logger.warning("Failed to load A-share map: %s", exc)
        _A_SHARE_MAP = {}
        return {}


def search_a_share(name_or_ticker: str) -> dict | None:
    """Look up an A-share by name or ticker in the offline map (O(1)).

    Returns {ticker, market, name} or None.
    """
    a_map = _load_a_share_map()
    # Exact match
    if name_or_ticker in a_map:
        return a_map[name_or_ticker]
    # Case-insensitive
    name_lower = name_or_ticker.lower()
    for key, val in a_map.items():
        if key.lower() == name_lower:
            return val
    return None


def fuzzy_search_a_share(query: str) -> dict | None:
    """Partial/substring match against the offline A-share snapshot (no network).

    Mirrors the legacy live-path behaviour (``df["name"].str.contains`` then
    ``df["code"].str.contains``): match on company name first, then on
    ticker/code. First match in snapshot order wins (deterministic).

    Returns {ticker, market, name} or None. Use only after an exact
    :func:`search_a_share` / :func:`lookup_company` miss.
    """
    query = query.strip()
    if not query:
        return None
    a_map = _load_a_share_map()
    if not a_map:
        return None
    # Name substring match (mirrors df["name"].str.contains)
    for val in a_map.values():
        if query in str(val.get("name", "")):
            return val
    # Ticker/code substring match (mirrors df["code"].str.contains)
    for val in a_map.values():
        if query in str(val.get("ticker", "")):
            return val
    return None


def lookup_company(name: str) -> dict | None:
    """Look up a company by name in manual + A-share maps.

    1. Exact match in manual map (US/HK/unlisted)
    2. Case-insensitive in manual map
    3. A-share offline map (5,528 stocks, O(1))
    4. Return None if not found
    """
    # Exact match
    if name in COMPANY_TO_TICKER:
        return COMPANY_TO_TICKER[name]
    # Case-insensitive
    name_lower = name.lower()
    for key, val in COMPANY_TO_TICKER.items():
        if key.lower() == name_lower:
            return val
    # A-share offline map
    result = search_a_share(name)
    if result:
        return result
    return None
