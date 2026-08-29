from fin_analyse.context.models import ContextRequestScope, ExternalContextRecord
from fin_analyse.context.service import ExternalContextService


class FakeEvents:
    def __init__(self):
        self.calls = []

    def get_dragon_tiger(self, ticker, days=30):
        self.calls.append(("dragon_tiger", ticker))
        return [
            ExternalContextRecord(
                "dragon_tiger:600519:2026-06-23",
                "eastmoney",
                "event",
                ticker,
                "龙虎榜",
                "龙虎榜参考",
                "2026-06-23",
            )
        ]

    def get_lockup_releases(self, ticker, days_forward=90):
        self.calls.append(("lockup", ticker))
        return [
            ExternalContextRecord(
                "lockup:600519:2026-07-01",
                "eastmoney",
                "event",
                ticker,
                "解禁",
                "解禁参考",
                "2026-07-01",
            )
        ]

    def get_block_trades(self, ticker, days=30):
        self.calls.append(("block_trade", ticker))
        return [
            ExternalContextRecord(
                "block_trade:600519:2026-06-20",
                "eastmoney",
                "event",
                ticker,
                "大宗交易",
                "大宗交易参考",
                "2026-06-20",
            )
        ]

    def get_shareholder_count(self, ticker):
        self.calls.append(("shareholder", ticker))
        return [
            ExternalContextRecord(
                "shareholder:600519:2026-03-31",
                "eastmoney",
                "shareholder",
                ticker,
                "股东户数",
                "股东户数参考",
                "2026-03-31",
            )
        ]

    def get_hot_themes(self, date=None):
        self.calls.append(("hot_theme", date))
        return []


class FakeReports:
    def __init__(self):
        self.calls = []

    def get_stock_reports(self, ticker, max_pages=2):
        self.calls.append(("research", ticker, max_pages))
        return [
            ExternalContextRecord(
                "report:600519:R1",
                "eastmoney_report",
                "research",
                ticker,
                "研报",
                "研报参考",
                "2026-06-22",
            )
        ]


class FakeAux:
    def __init__(self):
        self.calls = []

    def get_stock_news(self, ticker, limit=20):
        self.calls.append(("news", ticker, limit))
        return []

    def get_dividends(self, ticker):
        self.calls.append(("dividend", ticker))
        return [
            ExternalContextRecord(
                "dividend:600519:2026-06-23",
                "eastmoney",
                "dividend",
                ticker,
                "分红",
                "分红参考",
                "2026-06-23",
            )
        ]

    def get_fund_flow(self, ticker, period="daily"):
        self.calls.append(("fund_flow", ticker, period))
        return [
            ExternalContextRecord(
                "fund_flow:600519:2026-06-23",
                "eastmoney",
                "capital",
                ticker,
                "资金流",
                "资金流参考",
                "2026-06-23",
            )
        ]


class FakeAnnouncements:
    def __init__(self):
        self.calls = []

    def get_announcements(self, ticker, days=90, page_size=30):
        self.calls.append(("announcement", ticker, days, page_size))
        return [
            ExternalContextRecord(
                "announcement:600519:A1",
                "cninfo",
                "filing",
                ticker,
                "公告",
                "公告参考",
                "2026-06-21",
            )
        ]


def _service(events=None, reports=None, auxiliary=None, announcements=None):
    return ExternalContextService(
        events=events or FakeEvents(),
        reports=reports or FakeReports(),
        auxiliary=auxiliary or FakeAux(),
        announcements=announcements or FakeAnnouncements(),
    )


def test_collect_for_ticker_combines_default_sources():
    service = _service()

    bundle = service.collect_for_ticker("600519")

    assert bundle.ticker == "600519"
    assert {record.category for record in bundle.records} == {
        "event",
        "research",
        "filing",
        "dividend",
        "capital",
    }
    assert len(bundle.records) == 5
    assert bundle.reference_only is True
    assert bundle.warnings == []


def test_collect_for_ticker_keeps_platform_scope_metadata():
    service = _service()
    scope = ContextRequestScope(
        tenant_id="team-a",
        user_id="u123",
        platform="feishu",
        conversation_id="chat456",
        visibility="shared",
    )

    bundle = service.collect_for_ticker("600519", scope=scope)

    assert bundle.scope == scope
    assert bundle.scope.platform == "feishu"
    assert bundle.scope.user_id == "u123"


def test_collect_for_ticker_include_only_calls_requested_sources():
    events = FakeEvents()
    reports = FakeReports()
    auxiliary = FakeAux()
    announcements = FakeAnnouncements()
    service = _service(
        events=events, reports=reports, auxiliary=auxiliary, announcements=announcements
    )

    bundle = service.collect_for_ticker("600519", include=["announcement", "lockup"])

    assert [record.record_id for record in bundle.records] == [
        "lockup:600519:2026-07-01",
        "announcement:600519:A1",
    ]
    assert events.calls == [("lockup", "600519")]
    assert reports.calls == []
    assert auxiliary.calls == []
    assert announcements.calls == [("announcement", "600519", 90, 30)]


def test_collect_for_ticker_reports_provider_failures_as_warnings():
    class BrokenReports(FakeReports):
        def get_stock_reports(self, ticker, max_pages=2):
            raise RuntimeError("boom")

    service = _service(reports=BrokenReports())

    bundle = service.collect_for_ticker("600519", include=["research", "dividend"])

    assert [record.record_id for record in bundle.records] == ["dividend:600519:2026-06-23"]
    assert bundle.warnings == ["research: 数据暂时不可用"]


def test_collect_for_ticker_warns_for_parser_only_sources():
    service = _service()

    bundle = service.collect_for_ticker("600519", include=["news", "hot_theme"])

    assert bundle.records == []
    assert bundle.warnings == [
        "news: 数据源暂未启用，当前仅支持 parser 测试",
        "hot_theme: 数据源暂未启用，当前仅支持 parser 测试",
    ]


def test_to_prompt_context_contains_boundary_note():
    service = _service()
    bundle = service.collect_for_ticker("600519", include=["dragon_tiger"])

    text = service.to_prompt_context(bundle, max_items=1)

    assert "仅供参考" in text
    assert "不代表老师认知" in text
    assert "不构成交易建议" in text
    assert "龙虎榜参考" in text
