from fin_analyse.researcher.providers.eastmoney_reports import ResearchReportProvider


def test_parse_stock_report_to_reference_context():
    provider = ResearchReportProvider()
    row = {
        "code": "600519",
        "title": "贵州茅台深度报告",
        "publishDate": "2026-06-23 00:00:00",
        "orgSName": "中信证券",
        "infoCode": "ABC123",
        "emRatingName": "买入",
        "predictThisYearEps": "10.1",
        "predictNextYearEps": "11.2",
    }

    record = provider.parse_report(row, ticker="600519")

    assert record.record_id == "report:600519:ABC123"
    assert record.category == "research"
    assert record.ticker == "600519"
    assert "中信证券" in record.summary
    assert record.metadata["rating"] == "买入"
    assert record.metadata["eps_this_year"] == "10.1"
    assert record.is_decision_factor is False


def test_get_stock_reports_uses_em_get(monkeypatch):
    provider = ResearchReportProvider()

    class Resp:
        def json(self):
            return {
                "data": [
                    {
                        "code": "600519",
                        "title": "研报",
                        "publishDate": "2026-06-23",
                        "infoCode": "I1",
                    }
                ],
                "TotalPage": 1,
            }

    calls = []

    def fake_em_get(url, params=None, headers=None, timeout=30):
        calls.append((url, params, headers, timeout))
        return Resp()

    monkeypatch.setattr("fin_analyse.researcher.providers.eastmoney_reports.em_get", fake_em_get)

    records = provider.get_stock_reports("600519", max_pages=1)

    assert len(records) == 1
    assert calls[0][1]["qType"] == "0"
    assert calls[0][1]["code"] == "600519"


def test_get_industry_reports_uses_qtype_one(monkeypatch):
    provider = ResearchReportProvider()

    class Resp:
        def json(self):
            return {
                "data": [
                    {
                        "industryCode": "1238",
                        "industryName": "IT服务Ⅱ",
                        "title": "行业报告",
                        "publishDate": "2026-06-23",
                        "infoCode": "I2",
                    }
                ],
                "TotalPage": 1,
            }

    calls = []
    monkeypatch.setattr(
        "fin_analyse.researcher.providers.eastmoney_reports.em_get",
        lambda url, params=None, headers=None, timeout=30: calls.append((url, params)) or Resp(),
    )

    records = provider.get_industry_reports("1238", max_pages=1)

    assert len(records) == 1
    assert calls[0][1]["qType"] == "1"
    assert calls[0][1]["industryCode"] == "1238"
