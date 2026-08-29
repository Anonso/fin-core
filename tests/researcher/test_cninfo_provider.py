from fin_analyse.researcher.providers.cninfo import AnnouncementProvider


def test_parse_announcement_to_reference_context():
    provider = AnnouncementProvider()
    row = {
        "secCode": "600519",
        "secName": "贵州茅台",
        "announcementTitle": "2026年半年度报告",
        "announcementTime": 1782662400000,
        "announcementId": "ann1",
        "adjunctUrl": "finalpage/ann1.PDF",
    }

    record = provider.parse_announcement(row, ticker="600519")

    assert record.record_id == "announcement:600519:ann1"
    assert record.source == "cninfo"
    assert record.category == "filing"
    assert record.ticker == "600519"
    assert "半年度报告" in record.summary
    assert record.url.endswith("finalpage/ann1.PDF")
    assert record.is_decision_factor is False


def test_get_announcements_posts_cninfo_query(monkeypatch):
    provider = AnnouncementProvider()
    calls = []

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "announcements": [
                    {
                        "secCode": "600519",
                        "secName": "贵州茅台",
                        "announcementTitle": "2026年年度报告",
                        "announcementTime": 1782662400000,
                        "announcementId": "ann2",
                        "adjunctUrl": "finalpage/ann2.PDF",
                    }
                ]
            }

    def fake_post(url, data=None, headers=None, timeout=15):
        calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return Resp()

    monkeypatch.setattr("fin_analyse.researcher.providers.cninfo.requests.post", fake_post)

    records = provider.get_announcements("600519", days=30, page_size=20)

    assert len(records) == 1
    assert records[0].record_id == "announcement:600519:ann2"
    assert records[0].category == "filing"
    assert calls[0]["url"].endswith("/new/hisAnnouncement/query")
    assert calls[0]["data"]["stock"] == "600519"
    assert calls[0]["data"]["searchkey"] == "600519"
    assert calls[0]["data"]["pageSize"] == "20"
    assert calls[0]["data"]["column"] == "sse"
    assert "~" in calls[0]["data"]["seDate"]
    assert (
        calls[0]["headers"]["Referer"]
        == "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"
    )
    assert calls[0]["timeout"] == 15


def test_get_announcements_flattens_classified_announcements(monkeypatch):
    provider = AnnouncementProvider()

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "classifiedAnnouncements": [
                    [
                        {
                            "secCode": "000001",
                            "announcementTitle": "董事会公告",
                            "announcementTime": 1782662400000,
                            "announcementId": "ann3",
                        }
                    ]
                ]
            }

    monkeypatch.setattr(
        "fin_analyse.researcher.providers.cninfo.requests.post",
        lambda *args, **kwargs: Resp(),
    )

    records = provider.get_announcements("000001", days=7, page_size=10)

    assert len(records) == 1
    assert records[0].record_id == "announcement:000001:ann3"
    assert records[0].title == "董事会公告"
