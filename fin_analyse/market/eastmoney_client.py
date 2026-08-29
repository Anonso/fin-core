"""Shared EastMoney HTTP client with throttling and JSONP helpers.

Note: This module uses module-level mutable state (_EM_SESSION, _em_last_call) that is
not thread-safe. It is designed for single-threaded CLI usage. For multi-threaded
contexts, wrap access in a threading.Lock or use separate sessions.
"""

from __future__ import annotations

import random
import re
import time

import requests

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

_EM_SESSION = requests.Session()
_EM_SESSION.headers.update(_DEFAULT_HEADERS)
_em_last_call = [0.0]
EM_MIN_INTERVAL = 1.0
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def strip_jsonp(text: str) -> str:
    """Strip a JSONP callback wrapper if present."""
    match = re.search(r"\((.+)\)", text, re.DOTALL)
    return match.group(1) if match else text


def eastmoney_datacenter(
    report_name: str,
    columns: str = "ALL",
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict]:
    """Query EastMoney datacenter through the throttled em_get helper."""
    params = {
        "reportName": report_name,
        "columns": columns,
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    response = em_get(DATACENTER_URL, params=params, timeout=15)
    data = response.json()
    result = data.get("result") or {}
    rows = result.get("data") or []
    return rows if isinstance(rows, list) else []


def em_get(
    url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 15, **kwargs
):
    """EastMoney GET with serialized rate limiting and session reuse."""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return _EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = time.time()
