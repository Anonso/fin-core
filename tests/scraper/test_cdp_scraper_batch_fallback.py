"""Tests for CdpBridgeScraper batch-first fallback behavior."""

from datetime import datetime, timedelta, timezone

import pytest

from fin_analyse.scraper.cdp_diagnostics import CdpBatchResult, CdpBatchStepResult
from fin_analyse.scraper.cdp_scraper import (
    CdpBridgeScraper,
    _extract_datetimes_from_text,
)

pytestmark = pytest.mark.integration

TZ = timezone(timedelta(hours=8))


def _timeline_evidence(*timestamps: str) -> str:
    items = ",".join(
        (
            f'{{"topic_id":"{100000000 + index}",'
            f'"header_lines":["三线文案大锅饭","{timestamp}"],'
            f'"timestamps":["{timestamp}"]}}'
        )
        for index, timestamp in enumerate(timestamps)
    )
    return f'{{"schema_version":1,"items":[{items}]}}'


class FakeBatchClient:
    def __init__(self, batch_result):
        self.batch_result = batch_result
        self.nav_calls = []
        self.scroll_calls = []
        self.full_text_calls = 0

    def batch_execute(self, steps):
        self.last_steps = steps
        return self.batch_result

    def navigate(self, url, wait=3.0):
        self.nav_calls.append((url, wait))

    def scroll_by(self, px=4000, wait=1.0):
        self.scroll_calls.append((px, wait))

    def js(self, script):
        if "finTimelineTimestampEvidence" in script:
            return _timeline_evidence("2026-06-30 08:00")
        if "document.body.innerText" in script:
            self.full_text_calls += 1
            return "三线文案大锅饭\n2026-07-04 10:00\n测试标题\n测试正文"
        if "scrollTop" in script and "scrollHeight" in script:
            return '{"scrollTop":900,"clientHeight":100,"scrollHeight":1000}'
        if "查看详情" in script:
            return "done"
        return ""


def _cutoff():
    return datetime(2026, 7, 1, 0, 0, tzinfo=TZ)


class TestCdpScraperBatchFallback:
    def test_batch_success_returns_batch_text_without_single_step_nav(self):
        batch = CdpBatchResult(
            status="ok",
            steps=[
                CdpBatchStepResult(
                    index=0,
                    action="js",
                    name="timeline_dates",
                    status="ok",
                    result=_timeline_evidence("2026-06-30 08:00", "2026-07-04 10:00"),
                ),
                CdpBatchStepResult(
                    index=1,
                    action="full_text",
                    name="body",
                    status="ok",
                    result="三线文案大锅饭\n2026-06-30 08:00\n批量标题\n批量正文\n2026-07-04 10:00\n最新标题",
                ),
            ],
            cdp_trace={"attempts": 1},
        )
        client = FakeBatchClient(batch)
        scraper = CdpBridgeScraper()
        scraper._client = client

        loaded = scraper._load_group_timeline_batch_first(_cutoff())

        assert loaded.used_batch is True
        assert "批量标题" in loaded.full_text
        assert loaded.warnings == []
        assert client.nav_calls == []
        assert client.full_text_calls == 0

    def test_batch_failure_falls_back_to_single_step_path(self):
        batch = CdpBatchResult(
            status="failed",
            steps=[
                CdpBatchStepResult(
                    index=0,
                    action="full_text",
                    name="body",
                    status="failed",
                    error="Connection closed",
                    failure_kind="extension_disconnected",
                )
            ],
            failed_step={
                "index": 0,
                "action": "full_text",
                "name": "body",
                "status": "failed",
                "result": None,
                "error": "Connection closed",
                "failure_kind": "extension_disconnected",
                "duration_ms": 1,
            },
            cdp_trace={"attempts": 2},
        )
        client = FakeBatchClient(batch)
        scraper = CdpBridgeScraper()
        scraper._client = client

        loaded = scraper._load_group_timeline_batch_first(_cutoff())

        assert loaded.used_batch is False
        assert "测试标题" in loaded.full_text
        assert len(client.nav_calls) == 1
        assert client.nav_calls[0][0].startswith(
            "https://wx.zsxq.com/group/15522441811252?_fin_ts="
        )
        assert client.nav_calls[0][1] == 5.0
        assert loaded.warnings == ["batch_group_timeline_failed:extension_disconnected:step=body"]

    def test_batch_success_with_insufficient_window_falls_back_to_single_step_path(self):
        """Batch returned non-empty body but with insufficient content (no dates) → fallback."""
        batch = CdpBatchResult(
            status="ok",
            steps=[
                CdpBatchStepResult(
                    index=0,
                    action="full_text",
                    name="body",
                    status="ok",
                    result="页面加载中...\n请稍候\n查看详情",
                )
            ],
            cdp_trace={"attempts": 1},
        )
        client = FakeBatchClient(batch)
        scraper = CdpBridgeScraper()
        scraper._client = client

        loaded = scraper._load_group_timeline_batch_first(_cutoff())

        assert loaded.used_batch is False, "insufficient batch text must trigger fallback"
        assert "测试标题" in loaded.full_text  # from single-step fallback
        assert len(client.nav_calls) == 1
        assert client.nav_calls[0][0].startswith(
            "https://wx.zsxq.com/group/15522441811252?_fin_ts="
        )
        assert client.nav_calls[0][1] == 5.0
        assert any("batch_group_timeline_insufficient" in w for w in loaded.warnings), (
            f"expected batch_group_timeline_insufficient warning, got {loaded.warnings}"
        )

    def test_batch_tab_reselection_reloads_group_before_accepting_body(self):
        """A recovered scroll may land on digests; its body is not group content."""
        batch = CdpBatchResult(
            status="ok",
            steps=[
                CdpBatchStepResult(
                    index=0,
                    action="full_text",
                    name="body",
                    status="ok",
                    result=(
                        "2026-06-30 08:00\n精华归档\n2026-07-04 10:00\n日期存在但没有群主页作者块"
                    ),
                )
            ],
            cdp_trace={
                "attempts": 2,
                "restart_count": 1,
                "initial_tab_id": "735044465",
                "final_tab_id": "735044462",
            },
        )
        client = FakeBatchClient(batch)
        scraper = CdpBridgeScraper()
        scraper._client = client

        loaded = scraper._load_group_timeline_batch_first(_cutoff())

        assert loaded.used_batch is False
        assert "测试标题" in loaded.full_text
        assert len(client.nav_calls) == 1
        assert client.nav_calls[0][0].startswith(
            "https://wx.zsxq.com/group/15522441811252?_fin_ts="
        )
        assert loaded.warnings == [
            "batch_group_timeline_tab_changed:735044465->735044462:reload_required"
        ]

    def test_batch_wrong_surface_on_same_tab_reloads_group(self):
        """A same-tab recovery still falls back when the group author is absent."""
        batch = CdpBatchResult(
            status="ok",
            steps=[
                CdpBatchStepResult(
                    index=0,
                    action="full_text",
                    name="body",
                    status="ok",
                    result="2026-06-30 08:00\n精华归档\n2026-07-04 10:00\n日期充足",
                )
            ],
            cdp_trace={
                "attempts": 2,
                "restart_count": 1,
                "initial_tab_id": "735044465",
                "final_tab_id": "735044465",
            },
        )
        client = FakeBatchClient(batch)
        scraper = CdpBridgeScraper()
        scraper._client = client

        loaded = scraper._load_group_timeline_batch_first(_cutoff())

        assert loaded.used_batch is False
        assert "测试标题" in loaded.full_text
        assert len(client.nav_calls) == 1
        assert loaded.warnings == [
            "batch_group_timeline_insufficient:missing_group_author:step=body"
        ]

    def test_batch_relative_structured_date_does_not_cross_window_boundary(self):
        now = datetime(2026, 7, 9, 13, 0, tzinfo=TZ)
        cutoff = now - timedelta(days=3)
        text = "三线文案大锅饭\n昨天 10:30\n测试标题\n今天 12:00\n新标题"
        timeline_dates = _extract_datetimes_from_text("昨天 10:30", now=now)

        sufficient, reason = CdpBridgeScraper._is_batch_text_sufficient(
            text, cutoff, timeline_dates=timeline_dates
        )

        assert sufficient is False
        assert reason != "no_structured_timeline_dates"
        assert reason.startswith("oldest_date_20260708")

    def test_batch_body_date_does_not_satisfy_window_boundary(self):
        now = datetime(2026, 7, 9, 13, 0, tzinfo=TZ)
        cutoff = now - timedelta(days=3)
        text = (
            "三线文案大锅饭\n昨天 10:30\n窗口内文章\n"
            "正文引用 2026-06-30 08:00 的历史材料，但这不是文章发布时间。"
        )
        timeline_dates = _extract_datetimes_from_text("昨天 10:30", now=now)

        sufficient, reason = CdpBridgeScraper._is_batch_text_sufficient(
            text, cutoff, timeline_dates=timeline_dates
        )

        assert sufficient is False
        assert reason.startswith("oldest_date_20260708")

    def test_batch_month_day_date_can_satisfy_window_boundary(self):
        now = datetime(2026, 7, 9, 13, 0, tzinfo=TZ)
        cutoff = now - timedelta(days=3)
        text = "三线文案大锅饭\n07-05 10:30\n旧文章\n今天 12:00\n新标题"
        timeline_dates = _extract_datetimes_from_text("07-05 10:30", now=now)

        sufficient, reason = CdpBridgeScraper._is_batch_text_sufficient(
            text, cutoff, timeline_dates=timeline_dates
        )

        assert sufficient is True
        assert reason == ""

    def test_client_without_batch_execute_uses_single_step_path(self):
        class SingleStepOnlyClient:
            def __init__(self):
                self.nav_calls = []

            def navigate(self, url, wait=3.0):
                self.nav_calls.append((url, wait))

            def scroll_by(self, px=4000, wait=1.0):
                pass

            def js(self, script):
                if "finTimelineTimestampEvidence" in script:
                    return _timeline_evidence("2026-06-30 08:00")
                if "scrollTop" in script and "scrollHeight" in script:
                    return '{"scrollTop":900,"clientHeight":100,"scrollHeight":1000}'
                if "查看详情" in script:
                    return "done"
                return "三线文案大锅饭\n2026-07-04 10:00\n旧路径标题\n正文"

        client = SingleStepOnlyClient()
        scraper = CdpBridgeScraper()
        scraper._client = client

        loaded = scraper._load_group_timeline_batch_first(_cutoff())

        assert loaded.used_batch is False
        assert "旧路径标题" in loaded.full_text
        assert loaded.warnings == []

    def test_visible_relative_dates_extract_oldest_boundary(self):
        now = datetime(2026, 7, 9, 13, 0, tzinfo=TZ)
        dates = _extract_datetimes_from_text(
            "今天 12:00\n昨天 10:30\n前天 08:15\n2小时前",
            now=now,
        )

        assert min(dates) == datetime(2026, 7, 7, 8, 15, tzinfo=TZ)

    def test_parse_post_normalizes_relative_date(self, monkeypatch):
        import fin_analyse.scraper.cdp_scraper as cdp_scraper

        now = datetime(2026, 7, 9, 13, 0, tzinfo=TZ)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz else now.replace(tzinfo=None)

        monkeypatch.setattr(cdp_scraper, "datetime", FrozenDateTime)
        text = (
            "昨天 10:30\n"
            "星大派锐评：测试标题\n"
            "这是一段足够长的测试正文，用于确保普通文章不会因为长度太短被过滤。"
            "这里继续补充一些金融相关内容和产业链观察。"
        )

        scraper = CdpBridgeScraper()
        post = scraper._parse_post(text)

        assert post is not None
        assert post["date"] == "2026-07-08 10:30"
        assert post["title"] == "星大派锐评：测试标题"


class TestRunIncrementalBatchWarnings:
    def test_run_incremental_propagates_batch_warning(self, monkeypatch, tmp_path):
        batch = CdpBatchResult(
            status="failed",
            steps=[],
            failed_step={
                "index": 0,
                "action": "full_text",
                "name": "body",
                "status": "failed",
                "result": None,
                "error": "Connection closed",
                "failure_kind": "extension_disconnected",
                "duration_ms": 1,
            },
        )
        client = FakeBatchClient(batch)
        scraper = CdpBridgeScraper()
        scraper._client = client

        monkeypatch.setattr(scraper, "_load_index", lambda: None)
        monkeypatch.setattr(scraper, "_images_by_date_from_page", lambda: {})
        monkeypatch.setattr(scraper, "_split_by_author", lambda text: [])
        monkeypatch.setattr(
            scraper, "_write_priority_events_for_new_articles", lambda result, ids: 0
        )

        result = scraper.run_incremental_with_result()

        assert result.scrape_completed is False
        assert result.failure_kind == "content_insufficient"
        assert "batch_group_timeline_failed:extension_disconnected:step=body" in result.warnings
