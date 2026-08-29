"""Cninfo announcement context provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from fin_analyse.context.models import ExternalContextRecord

_CNINFO_BASE = "https://static.cninfo.com.cn/"
_CNINFO_QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
_CNINFO_REFERER = "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"
_CNINFO_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": "https://www.cninfo.com.cn",
    "Referer": _CNINFO_REFERER,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}


def _millis_to_date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(value or "")[:10]


def _column_for(ticker: str) -> str:
    return "sse" if ticker.startswith(("6", "9")) else "szse"


def _extract_announcement_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("announcements") or []
    if rows:
        return [row for row in rows if isinstance(row, dict)]

    classified = data.get("classifiedAnnouncements") or []
    flattened: list[dict[str, Any]] = []
    for group in classified:
        if isinstance(group, list):
            flattened.extend(row for row in group if isinstance(row, dict))
        elif isinstance(group, dict):
            flattened.append(group)
    return flattened


class AnnouncementProvider:
    """Company announcements as reference-only apprentice context."""

    def get_announcements(
        self,
        ticker: str,
        days: int = 90,
        page_size: int = 30,
    ) -> list[ExternalContextRecord]:
        end = datetime.now(UTC).date()
        start = end - timedelta(days=days)
        payload = {
            "pageNum": "1",
            "pageSize": str(page_size),
            "column": _column_for(ticker),
            "tabName": "fulltext",
            "plate": "",
            "stock": ticker,
            "searchkey": ticker,
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{start:%Y-%m-%d}~{end:%Y-%m-%d}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        response = requests.post(
            _CNINFO_QUERY,
            data=payload,
            headers=_CNINFO_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        rows = _extract_announcement_rows(response.json())
        return [self.parse_announcement(row, ticker=ticker) for row in rows]

    def parse_announcement(self, row: dict[str, Any], ticker: str) -> ExternalContextRecord:
        ann_id = str(
            row.get("announcementId") or row.get("id") or row.get("announcementTitle") or ""
        )
        title = str(row.get("announcementTitle") or "公告")
        date = _millis_to_date(row.get("announcementTime"))
        adjunct_url = str(row.get("adjunctUrl") or "")
        url = (
            _CNINFO_BASE + adjunct_url
            if adjunct_url and not adjunct_url.startswith("http")
            else adjunct_url
        )
        return ExternalContextRecord(
            record_id=f"announcement:{ticker}:{ann_id}",
            source="cninfo",
            category="filing",
            ticker=ticker,
            title=title,
            summary=f"{date} {ticker} 公告：{title}。公告为外部参考，不写入老师认知。",
            occurred_at=date,
            url=url,
            importance=0.6,
            metadata={"announcement_id": ann_id},
            raw=row,
        )
