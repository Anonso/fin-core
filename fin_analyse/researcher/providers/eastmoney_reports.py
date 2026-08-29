"""EastMoney research report context provider."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fin_analyse.context.models import ExternalContextRecord
from fin_analyse.market.eastmoney_client import em_get

REPORT_API = "https://reportapi.eastmoney.com/report/list"
PDF_TPL = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
_REFERER = {"Referer": "https://data.eastmoney.com/"}


def _date10(value: Any) -> str:
    return str(value or "")[:10]


class ResearchReportProvider:
    """Research reports as reference-only apprentice context."""

    def get_stock_reports(self, ticker: str, max_pages: int = 2) -> list[ExternalContextRecord]:
        records: list[ExternalContextRecord] = []
        for page in range(1, max_pages + 1):
            params = {
                "industryCode": "*",
                "pageSize": "100",
                "industry": "*",
                "rating": "*",
                "ratingChange": "*",
                "beginTime": "2000-01-01",
                "endTime": "2030-01-01",
                "pageNo": str(page),
                "fields": "",
                "qType": "0",
                "orgCode": "",
                "code": ticker,
                "rcode": "",
                "p": str(page),
                "pageNum": str(page),
                "pageNumber": str(page),
            }
            data = em_get(REPORT_API, params=params, headers=_REFERER, timeout=30).json()
            rows = data.get("data") or []
            records.extend(self.parse_report(row, ticker=ticker) for row in rows)
            if page >= (data.get("TotalPage", 1) or 1):
                break
        return records

    def get_industry_reports(
        self, industry_code: str = "*", max_pages: int = 1
    ) -> list[ExternalContextRecord]:
        records: list[ExternalContextRecord] = []
        for page in range(1, max_pages + 1):
            params = {
                "industryCode": industry_code,
                "pageSize": "100",
                "industry": "*",
                "rating": "*",
                "ratingChange": "*",
                "beginTime": "2024-01-01",
                "endTime": "2030-01-01",
                "pageNo": str(page),
                "fields": "",
                "qType": "1",
            }
            data = em_get(REPORT_API, params=params, headers=_REFERER, timeout=30).json()
            rows = data.get("data") or []
            records.extend(
                self.parse_report(
                    row, ticker=str(row.get("industryCode") or industry_code), category="research"
                )
                for row in rows
            )
            if page >= (data.get("TotalPage", 1) or 1):
                break
        return records

    def download_pdf(self, info_code: str, target_dir: Path) -> Path | None:
        url = PDF_TPL.format(info_code=info_code)
        response = em_get(url, headers=_REFERER, timeout=60)
        if response.status_code != 200 or len(response.content) < 1024:
            return None
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', info_code)}.pdf"
        target.write_bytes(response.content)
        return target

    def parse_report(
        self, row: dict[str, Any], ticker: str, category: str = "research"
    ) -> ExternalContextRecord:
        info_code = str(row.get("infoCode") or row.get("INFO_CODE") or "")
        title = row.get("title") or row.get("TITLE") or "研报"
        date = _date10(row.get("publishDate") or row.get("PUBLISH_DATE"))
        org = row.get("orgSName") or row.get("ORG_S_NAME") or "未知机构"
        rating = row.get("emRatingName") or row.get("EM_RATING_NAME") or ""
        eps_this = row.get("predictThisYearEps")
        eps_next = row.get("predictNextYearEps")
        return ExternalContextRecord(
            record_id=f"report:{ticker}:{info_code or title}",
            source="eastmoney_report",
            category=category,
            ticker=ticker,
            title=str(title),
            summary=f"{date} {org}研报：{title}，评级={rating}，EPS预测={eps_this}/{eps_next}。券商研报为外部参考，不写入老师认知。",
            occurred_at=date,
            url=PDF_TPL.format(info_code=info_code) if info_code else "",
            importance=0.6,
            metadata={
                "org": org,
                "rating": rating,
                "eps_this_year": eps_this,
                "eps_next_year": eps_next,
                "info_code": info_code,
            },
            raw=row,
        )
