"""Fail-closed recovery for a ZSXQ timeline whose DOM loader is stuck."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fin_analyse.scraper import cdp_scraper as subject

TZ = timezone(timedelta(hours=8))


def _raw_page(*topics: dict[str, object]) -> str:
    return json.dumps(
        {
            "schema_version": 4,
            "http_status": 200,
            "api_succeeded": True,
            "api_code": None,
            "topics": list(topics),
        },
        ensure_ascii=False,
    )


def _topic(
    topic_id: str,
    created_at: datetime,
    *,
    legacy_topic_id: str | None = None,
    title: str = "半导体产业链跟踪",
    topic_type: str = "talk",
    source_class: str = "teacher",
    answer_state: str | None = None,
    content_text: str | None = None,
) -> dict:
    return {
        "topic_id": topic_id,
        "legacy_topic_id": topic_id if legacy_topic_id is None else legacy_topic_id,
        "create_time": created_at.isoformat(timespec="milliseconds"),
        "title": "" if topic_type == "q&a" else title,
        "topic_type": topic_type,
        "source_class": source_class,
        "answer_state": answer_state
        or ("not_applicable" if topic_type == "talk" else "answered"),
        "content_text": content_text
        if content_text is not None
        else (
            "能量评分 9.1 分\n" + "半导体设备与材料供需继续改善。" * 10
            if source_class == "teacher"
            else ""
        ),
    }


def test_topic_cursor_page_decoder_accepts_only_typed_native_topics():
    now = datetime.now(TZ).replace(microsecond=123000)

    page = subject._decode_topic_cursor_page(_raw_page(_topic("123456789", now)))

    assert page is not None
    assert page.http_status == 200
    assert page.api_succeeded is True
    assert len(page.topics) == 1
    assert page.topics[0].topic_id == "123456789"
    assert page.topics[0].legacy_topic_id == "123456789"
    assert page.topics[0].created_at == now
    assert page.topics[0].is_teacher_source is True


def test_topic_cursor_page_decoder_rejects_ambiguous_or_malformed_payloads():
    duplicate_key = (
        '{"schema_version":1,"http_status":200,"api_succeeded":true,'
        '"api_code":null,"topics":[],"topics":[]}'
    )
    malformed_topic = _raw_page(
        {
            "topic_id": "../escape",
            "legacy_topic_id": "123456789",
            "create_time": "not-a-time",
            "title": "bad",
            "topic_type": "talk",
            "content_text": "bad",
            "source_class": "teacher",
            "answer_state": "not_applicable",
        }
    )

    assert subject._decode_topic_cursor_page(duplicate_key) is None
    assert subject._decode_topic_cursor_page(malformed_topic) is None


def test_topic_cursor_page_decoder_separates_teacher_answer_from_member_context():
    now = datetime.now(TZ).replace(microsecond=123000)
    teacher_answer = _topic(
        "123456790",
        now,
        topic_type="q&a",
        content_text=(
            "能量评分 9.1 分\n"
            + "只保留老师回答，不把群友问题写成老师认知。" * 10
        ),
    )
    member_talk = _topic(
        "123456791",
        now - timedelta(minutes=1),
        title="",
        source_class="coverage_only",
    )
    unanswered = _topic(
        "123456792",
        now - timedelta(minutes=2),
        topic_type="q&a",
        title="",
        source_class="coverage_only",
        answer_state="unanswered",
        content_text="",
    )

    page = subject._decode_topic_cursor_page(
        _raw_page(teacher_answer, member_talk, unanswered)
    )

    assert page is not None
    assert [topic.is_teacher_source for topic in page.topics] == [True, False, False]
    post = subject.CdpBridgeScraper()._post_from_topic_cursor_item(page.topics[0])
    assert post is not None
    assert "只保留老师回答" in post["content"]
    assert "问：" not in post["content"]
    assert subject.CdpBridgeScraper()._post_from_topic_cursor_item(page.topics[1]) is None
    assert subject.CdpBridgeScraper()._post_from_topic_cursor_item(page.topics[2]) is None


def test_topic_cursor_page_decoder_rejects_ambiguous_answer_identity():
    now = datetime.now(TZ).replace(microsecond=123000)
    invalid_answer = _topic(
        "123456793",
        now,
        topic_type="q&a",
        title="",
        source_class="invalid",
        answer_state="invalid",
        content_text="",
    )

    assert subject._decode_topic_cursor_page(_raw_page(invalid_answer)) is None


def test_topic_cursor_fetch_is_fixed_signed_in_surface_and_awaits_response():
    now = datetime.now(TZ).replace(microsecond=0)
    scripts: list[str] = []
    scraper = subject.CdpBridgeScraper()
    scraper._client = SimpleNamespace(
        js=lambda script: scripts.append(script)
        or _raw_page(_topic("123456789", now))
    )

    page = scraper._fetch_topic_cursor_page("2026-07-25T16:56:00.000+08:00")

    assert page is not None
    assert len(scripts) == 1
    script = scripts[0]
    assert "return await (async function finTopicCursorPage()" in script
    assert "https://api.zsxq.com/v2/groups/15522441811252/topics?" in script
    assert "scope=all&count=30&end_time=2026-07-25T16%3A56%3A00.000%2B08%3A00" in script
    assert "document.cookie" not in script
    assert "topic.question" not in script
    assert "owner_name" not in script
    assert "owner_user_id" not in script
    assert "source_class" in script
    assert "owner.name ===" in script
    assert "content_text: isTeacher ?" in script
    assert "typeof topic.topic_uid === 'string'" in script
    assert "? topic.topic_uid" in script
    assert "String(topic && topic.topic_uid" not in script
    assert "topic_id: String(topic && topic.topic_id || '')" not in script
    assert "legacy_topic_id:" in script
    assert "typeof topic.topic_id === 'number'" in script


def test_topic_cursor_coverage_paginates_by_oldest_native_time_until_cutoff(monkeypatch):
    now = datetime.now(TZ).replace(microsecond=0)
    cutoff = now - timedelta(days=3)
    page_one_items = [
        _topic(str(1000 + index), now - timedelta(hours=index))
        for index in range(30)
    ]
    first_oldest_raw = page_one_items[-1]["create_time"]
    page_two_items = [
        _topic("2001", now - timedelta(hours=40)),
        _topic("2002", now - timedelta(days=4)),
    ]
    pages = [
        subject._decode_topic_cursor_page(_raw_page(*page_one_items)),
        subject._decode_topic_cursor_page(_raw_page(*page_two_items)),
    ]
    assert all(page is not None for page in pages)
    cursors: list[str] = []
    scraper = subject.CdpBridgeScraper()

    def fetch(cursor: str):
        cursors.append(cursor)
        return pages[len(cursors) - 1]

    monkeypatch.setattr(scraper, "_fetch_topic_cursor_page", fetch)

    result = scraper._collect_topic_cursor_coverage(cutoff, max_pages=4)

    assert result.covered is True
    assert result.boundary_kind == "cutoff"
    assert result.oldest_observed_at < cutoff
    assert len(result.topics) == 31
    assert {topic.topic_id for topic in result.topics}.isdisjoint({"2002"})
    assert cursors == ["", first_oldest_raw]


def test_topic_cursor_coverage_accepts_single_inclusive_page_boundary_overlap(monkeypatch):
    """ZSXQ repeats the previous page's last topic as the next page's first item."""
    now = datetime.now(TZ).replace(microsecond=0)
    cutoff = now - timedelta(days=3)
    page_one_items = [
        _topic(str(2100 + index), now - timedelta(hours=index))
        for index in range(30)
    ]
    boundary = page_one_items[-1]
    page_two_items = [
        boundary,
        *[
            _topic(str(2200 + index), now - timedelta(hours=30 + index))
            for index in range(28)
        ],
        _topic("2299", now - timedelta(days=4)),
    ]
    pages = [
        subject._decode_topic_cursor_page(_raw_page(*page_one_items)),
        subject._decode_topic_cursor_page(_raw_page(*page_two_items)),
    ]
    assert all(page is not None for page in pages)
    cursors: list[str] = []
    scraper = subject.CdpBridgeScraper()

    def fetch(cursor: str):
        cursors.append(cursor)
        return pages[len(cursors) - 1]

    monkeypatch.setattr(scraper, "_fetch_topic_cursor_page", fetch)

    result = scraper._collect_topic_cursor_coverage(cutoff, max_pages=3)

    assert result.covered is True
    assert result.boundary_kind == "cutoff"
    assert result.failure_code == ""
    assert len(result.topics) == 58
    assert len({topic.topic_id for topic in result.topics}) == 58
    assert cursors == ["", boundary["create_time"]]


def test_full_page_with_inclusive_overlap_does_not_hide_next_page_api_rejection(
    monkeypatch,
):
    now = datetime.now(TZ).replace(microsecond=0)
    cutoff = now - timedelta(days=3)
    page_one_items = [
        _topic(str(2300 + index), now - timedelta(hours=index))
        for index in range(30)
    ]
    page_two_items = [
        page_one_items[-1],
        *[
            _topic(str(2400 + index), now - timedelta(hours=30 + index))
            for index in range(29)
        ],
    ]
    rejected = json.dumps(
        {
            "schema_version": 4,
            "http_status": 200,
            "api_succeeded": False,
            "api_code": 1060,
            "topics": [],
        }
    )
    pages = [
        subject._decode_topic_cursor_page(_raw_page(*page_one_items)),
        subject._decode_topic_cursor_page(_raw_page(*page_two_items)),
        subject._decode_topic_cursor_page(rejected),
    ]
    assert all(page is not None for page in pages)
    cursors: list[str] = []
    scraper = subject.CdpBridgeScraper()

    def fetch(cursor: str):
        cursors.append(cursor)
        return pages[len(cursors) - 1]

    monkeypatch.setattr(scraper, "_fetch_topic_cursor_page", fetch)

    result = scraper._collect_topic_cursor_coverage(cutoff, max_pages=4)

    assert result.covered is False
    assert result.failure_code == "api_rejected"
    assert len(cursors) == 3


def test_topic_cursor_rate_limit_is_retried_once_after_bounded_pacing(monkeypatch):
    now = datetime.now(TZ).replace(microsecond=0)
    cutoff = now - timedelta(days=3)
    first_items = [
        _topic(str(2500 + index), now - timedelta(hours=index))
        for index in range(30)
    ]
    second_items = [
        first_items[-1],
        _topic("2599", now - timedelta(days=4)),
    ]
    rate_limited = subject._decode_topic_cursor_page(
        json.dumps(
            {
                "schema_version": 4,
                "http_status": 200,
                "api_succeeded": False,
                "api_code": 1059,
                "topics": [],
            }
        )
    )
    pages = [
        subject._decode_topic_cursor_page(_raw_page(*first_items)),
        rate_limited,
        subject._decode_topic_cursor_page(_raw_page(*second_items)),
    ]
    assert all(page is not None for page in pages)
    waits: list[float] = []
    cursors: list[str] = []
    scraper = subject.CdpBridgeScraper()
    scraper._client = SimpleNamespace()
    monkeypatch.setattr(subject.time, "sleep", waits.append)

    def fetch(cursor: str):
        cursors.append(cursor)
        return pages[len(cursors) - 1]

    monkeypatch.setattr(scraper, "_fetch_topic_cursor_page", fetch)

    result = scraper._collect_topic_cursor_coverage(cutoff, max_pages=3)

    assert result.covered is True
    assert result.boundary_kind == "cutoff"
    assert result.failure_code == ""
    assert cursors == ["", first_items[-1]["create_time"], first_items[-1]["create_time"]]
    assert waits == [8.0, 8.0]


def test_topic_cursor_repeated_rate_limit_still_fails_closed(monkeypatch):
    now = datetime.now(TZ).replace(microsecond=0)
    first_items = [
        _topic(str(2600 + index), now - timedelta(hours=index))
        for index in range(30)
    ]
    rate_limited = subject._decode_topic_cursor_page(
        json.dumps(
            {
                "schema_version": 4,
                "http_status": 200,
                "api_succeeded": False,
                "api_code": 1059,
                "topics": [],
            }
        )
    )
    first = subject._decode_topic_cursor_page(_raw_page(*first_items))
    assert first is not None and rate_limited is not None
    pages = [first, rate_limited, rate_limited]
    scraper = subject.CdpBridgeScraper()
    scraper._client = SimpleNamespace()
    monkeypatch.setattr(subject.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        scraper,
        "_fetch_topic_cursor_page",
        lambda _cursor: pages.pop(0),
    )

    result = scraper._collect_topic_cursor_coverage(
        now - timedelta(days=3),
        max_pages=3,
    )

    assert result.covered is False
    assert result.failure_code == "api_rejected"
    assert len(pages) == 0


def test_topic_cursor_pacing_checkpoints_before_and_after_sleep(monkeypatch):
    events: list[object] = []
    scraper = subject.CdpBridgeScraper(
        checkpoint=lambda: events.append("checkpoint"),
    )
    scraper._client = SimpleNamespace()
    monkeypatch.setattr(
        subject.time,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    scraper._wait_between_topic_cursor_requests()

    assert events == ["checkpoint", ("sleep", 8.0), "checkpoint"]


def test_rate_limit_retry_stops_when_pacing_reaches_deadline(monkeypatch):
    class DeadlineReachedError(RuntimeError):
        pass

    rate_limited = subject._decode_topic_cursor_page(
        json.dumps(
            {
                "schema_version": 4,
                "http_status": 200,
                "api_succeeded": False,
                "api_code": 1059,
                "topics": [],
            }
        )
    )
    assert rate_limited is not None
    checkpoints = 0
    waits: list[float] = []
    fetch_cursors: list[str] = []

    def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 3:
            raise DeadlineReachedError

    scraper = subject.CdpBridgeScraper(
        deadline_at=datetime.now(TZ) + timedelta(seconds=5),
        checkpoint=checkpoint,
    )
    scraper._client = SimpleNamespace()
    monkeypatch.setattr(subject.time, "sleep", waits.append)

    def fetch(cursor: str):
        fetch_cursors.append(cursor)
        return rate_limited

    monkeypatch.setattr(scraper, "_fetch_topic_cursor_page", fetch)

    with pytest.raises(DeadlineReachedError):
        scraper._collect_topic_cursor_coverage(
            datetime.now(TZ) - timedelta(days=3),
        )

    assert fetch_cursors == [""]
    assert len(waits) == 1
    assert 0 < waits[0] < 8.0
    assert checkpoints == 3


def test_topic_cursor_coverage_fails_closed_when_page_order_or_cursor_stalls(monkeypatch):
    now = datetime.now(TZ).replace(microsecond=0)
    cutoff = now - timedelta(days=3)
    repeated = [
        _topic(str(3000 + index), now - timedelta(hours=index))
        for index in range(30)
    ]
    page = subject._decode_topic_cursor_page(_raw_page(*repeated))
    assert page is not None
    scraper = subject.CdpBridgeScraper()
    monkeypatch.setattr(scraper, "_fetch_topic_cursor_page", lambda _cursor: page)

    result = scraper._collect_topic_cursor_coverage(cutoff, max_pages=3)

    assert result.covered is False
    assert result.failure_code == "cursor_not_advancing"


def test_full_coverage_only_page_followed_by_empty_page_proves_page_end(monkeypatch):
    now = datetime.now(TZ).replace(microsecond=0)
    coverage_only = [
        _topic(
            str(4000 + index),
            now - timedelta(hours=index),
            title="",
            source_class="coverage_only",
        )
        for index in range(30)
    ]
    first = subject._decode_topic_cursor_page(_raw_page(*coverage_only))
    empty = subject._decode_topic_cursor_page(_raw_page())
    assert first is not None and empty is not None
    pages = [first, empty]
    scraper = subject.CdpBridgeScraper()
    monkeypatch.setattr(
        scraper,
        "_fetch_topic_cursor_page",
        lambda _cursor: pages.pop(0),
    )

    result = scraper._collect_topic_cursor_coverage(
        now - timedelta(days=3),
        max_pages=3,
    )

    assert result.covered is True
    assert result.boundary_kind == "page_end"
    assert result.topics == ()
    assert result.oldest_observed_at == first.topics[-1].created_at


def test_cross_page_duplicate_legacy_topic_id_cannot_prove_cutoff(monkeypatch):
    now = datetime.now(TZ).replace(microsecond=0)
    first_items = [
        _topic(str(5000 + index), now - timedelta(hours=index))
        for index in range(30)
    ]
    duplicate_before_cutoff = _topic(
        "999999",
        now - timedelta(days=4),
        legacy_topic_id="5000",
    )
    first = subject._decode_topic_cursor_page(_raw_page(*first_items))
    second = subject._decode_topic_cursor_page(_raw_page(duplicate_before_cutoff))
    assert first is not None and second is not None
    pages = [first, second]
    scraper = subject.CdpBridgeScraper()
    monkeypatch.setattr(
        scraper,
        "_fetch_topic_cursor_page",
        lambda _cursor: pages.pop(0),
    )

    result = scraper._collect_topic_cursor_coverage(
        now - timedelta(days=3),
        max_pages=3,
    )

    assert result.covered is False
    assert result.failure_code == "duplicate_legacy_topic_id"


def test_inclusive_boundary_with_changed_projection_is_rejected(monkeypatch):
    now = datetime.now(TZ).replace(microsecond=0)
    first_items = [
        _topic(str(5100 + index), now - timedelta(hours=index))
        for index in range(30)
    ]
    changed_boundary = {
        **first_items[-1],
        "content_text": "能量评分 9.1 分\n" + "边界项内容发生变化。" * 20,
    }
    first = subject._decode_topic_cursor_page(_raw_page(*first_items))
    second = subject._decode_topic_cursor_page(_raw_page(changed_boundary))
    assert first is not None and second is not None
    pages = [first, second]
    scraper = subject.CdpBridgeScraper()
    monkeypatch.setattr(
        scraper,
        "_fetch_topic_cursor_page",
        lambda _cursor: pages.pop(0),
    )

    result = scraper._collect_topic_cursor_coverage(
        now - timedelta(days=3),
        max_pages=3,
    )

    assert result.covered is False
    assert result.failure_code == "duplicate_topic_id"


def test_topic_cursor_item_becomes_teacher_source_post_with_native_identity():
    now = datetime.now(TZ).replace(microsecond=0)
    page = subject._decode_topic_cursor_page(_raw_page(_topic("99887766", now)))
    assert page is not None
    scraper = subject.CdpBridgeScraper()

    post = scraper._post_from_topic_cursor_item(page.topics[0])

    assert post is not None
    assert post["topic_id"] == "99887766"
    assert post["content_source"] == "zsxq_topic_cursor"
    assert post["date"] == now.strftime("%Y-%m-%d %H:%M")
    assert "半导体" in post["content"]


def test_topic_cursor_coverage_uses_other_authors_for_boundary_but_never_writes_them(
    monkeypatch,
):
    now = datetime.now(TZ).replace(microsecond=0)
    cutoff = now - timedelta(days=3)
    teacher = _topic("7000", now - timedelta(hours=3))
    other_before_cutoff = _topic(
        "7001",
        now - timedelta(days=4),
        title="",
        source_class="coverage_only",
    )
    page = subject._decode_topic_cursor_page(
        _raw_page(teacher, other_before_cutoff)
    )
    assert page is not None
    scraper = subject.CdpBridgeScraper()
    monkeypatch.setattr(scraper, "_fetch_topic_cursor_page", lambda _cursor: page)

    result = scraper._collect_topic_cursor_coverage(cutoff)

    assert result.covered is True
    assert result.boundary_kind == "cutoff"
    assert [topic.topic_id for topic in result.topics] == ["7000"]


def test_incremental_recovers_stuck_dom_window_with_cursor_before_writing(tmp_path, monkeypatch):
    now = datetime.now(TZ).replace(microsecond=0)
    legacy_one = "55522445558181460"
    legacy_two = "82255442552848850"
    inside_one = _topic(
        legacy_one,
        now - timedelta(hours=6),
        title="半导体产业链跟踪",
    )
    inside_two = _topic(
        legacy_two,
        now - timedelta(hours=6),
        title="半导体产业链跟踪",
    )
    page = subject._decode_topic_cursor_page(_raw_page(inside_one, inside_two))
    assert page is not None
    coverage = subject._TopicCursorCoverage(
        topics=page.topics,
        covered=True,
        boundary_kind="cutoff",
        oldest_observed_at=now - timedelta(days=4),
    )
    coverage_holder = [coverage]
    dom_date = page.topics[0].created_at.strftime("%Y-%m-%d %H:%M")
    dom_text = (
        f"三线文案大锅饭\n{dom_date}\n半导体产业链跟踪\n"
        "能量评分 9.1 分\n"
        + "半导体设备与材料供需继续改善。" * 20
    )
    scraper = subject.CdpBridgeScraper(knowledge_base_root=tmp_path)
    scraper._client = object()  # type: ignore[assignment]
    monkeypatch.setattr(
        scraper,
        "_load_group_timeline_batch_first",
        lambda _cutoff: subject.GroupTimelineLoadResult(
            full_text=dom_text,
            timeline_dates=[page.topics[0].created_at],
            reached_page_end=False,
        ),
    )
    monkeypatch.setattr(
        scraper,
        "_collect_topic_cursor_coverage",
        lambda _cutoff: coverage_holder[0],
    )
    monkeypatch.setattr(scraper, "_images_by_date_from_page", lambda: {})
    monkeypatch.setattr(scraper, "_write_priority_events_for_new_articles", lambda *_args: 0)
    monkeypatch.setattr(scraper, "_ensure_deep_read_artifacts_for_new", lambda *_args: 0)
    monkeypatch.setattr(scraper, "_repair_active_g_support_artifacts", lambda _result: None)

    result = scraper.run_incremental_with_result()

    assert result.scrape_completed is True
    assert result.failure_kind is None
    assert result.new_count == 2
    assert result.stopped_by_window_boundary is True
    assert "group_topic_cursor" in result.sources_scanned
    assert "group_timeline_cursor_recovered:cutoff" in result.warnings
    index = json.loads((tmp_path / "index.json").read_text())
    assert {article["topic_id"] for article in index["articles"]} == {
        legacy_one,
        legacy_two,
    }
    assert {article["id"] for article in index["articles"]} == {
        f"zsxq-{legacy_one}",
        f"zsxq-{legacy_two}",
    }
    assert {article["content_source"] for article in index["articles"]} == {
        "zsxq_topic_cursor"
    }
    assert all("legacy_topic_id" not in article for article in index["articles"])

    edited_page = subject._decode_topic_cursor_page(
        _raw_page(
            _topic(
                "55522445558181454",
                now - timedelta(hours=6),
                legacy_topic_id=legacy_one,
                title="半导体产业链跟踪（修订）",
            ),
            _topic(
                "82255442552848842",
                now - timedelta(hours=6),
                legacy_topic_id=legacy_two,
                title="半导体产业链跟踪（修订）",
            ),
        )
    )
    assert edited_page is not None
    coverage_holder[0] = subject._TopicCursorCoverage(
        topics=edited_page.topics,
        covered=True,
        boundary_kind="cutoff",
        oldest_observed_at=now - timedelta(days=4),
    )

    second = scraper.run_incremental_with_result()

    assert second.scrape_completed is True
    assert second.new_count == 0
    second_index = json.loads((tmp_path / "index.json").read_text())
    assert len(second_index["articles"]) == 2
    assert {article["topic_id"] for article in second_index["articles"]} == {
        legacy_one,
        legacy_two,
    }


def test_incremental_accepts_coverage_only_cursor_window_without_persisting_member_data(
    tmp_path,
    monkeypatch,
):
    now = datetime.now(TZ).replace(microsecond=0)
    member_page = subject._decode_topic_cursor_page(
        _raw_page(
            _topic(
                "8001",
                now - timedelta(days=4),
                title="",
                source_class="coverage_only",
                content_text="",
            )
        )
    )
    assert member_page is not None
    coverage = subject._TopicCursorCoverage(
        topics=(),
        covered=True,
        boundary_kind="cutoff",
        oldest_observed_at=member_page.topics[0].created_at,
    )
    scraper = subject.CdpBridgeScraper(knowledge_base_root=tmp_path)
    scraper._client = object()  # type: ignore[assignment]
    monkeypatch.setattr(
        scraper,
        "_load_group_timeline_batch_first",
        lambda _cutoff: subject.GroupTimelineLoadResult(),
    )
    monkeypatch.setattr(
        scraper,
        "_collect_topic_cursor_coverage",
        lambda _cutoff: coverage,
    )
    monkeypatch.setattr(scraper, "_images_by_date_from_page", lambda: {})
    monkeypatch.setattr(scraper, "_repair_active_g_support_artifacts", lambda _result: None)

    result = scraper.run_incremental_with_result()

    assert result.scrape_completed is True
    assert result.failure_kind is None
    assert result.new_count == 0
    assert result.posts_seen == 0
    assert "group_topic_cursor" in result.sources_scanned
    assert not (tmp_path / "index.json").exists()


def test_incremental_cursor_failure_keeps_knowledge_base_untouched(tmp_path, monkeypatch):
    now = datetime.now(TZ).replace(microsecond=0)
    dom_text = (
        f"三线文案大锅饭\n{now.strftime('%Y-%m-%d %H:%M')}\n半导体产业链跟踪\n"
        "能量评分 9.1 分\n"
        + "半导体设备与材料供需继续改善。" * 20
    )
    scraper = subject.CdpBridgeScraper(knowledge_base_root=tmp_path)
    scraper._client = object()  # type: ignore[assignment]
    monkeypatch.setattr(
        scraper,
        "_load_group_timeline_batch_first",
        lambda _cutoff: subject.GroupTimelineLoadResult(
            full_text=dom_text,
            timeline_dates=[now],
            reached_page_end=False,
        ),
    )
    monkeypatch.setattr(
        scraper,
        "_collect_topic_cursor_coverage",
        lambda _cutoff: subject._TopicCursorCoverage(failure_code="api_rejected"),
    )
    writes: list[dict] = []
    monkeypatch.setattr(scraper, "_save_article", lambda post, **_kwargs: writes.append(post))

    result = scraper.run_incremental_with_result()

    assert result.scrape_completed is False
    assert result.failure_kind == "window_coverage_incomplete"
    assert "group_timeline_cursor_failed:api_rejected" in result.warnings
    assert writes == []
    assert not (tmp_path / "index.json").exists()
