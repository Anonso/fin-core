"""Gate 2A tests for the package-owned Windows Chrome CDP composition root.

These tests pin the production composition root that wires exactly one
:class:`ZsxqScraperModule` over one :class:`ScraperRuntimeRepository` and one
:class:`WindowsChromeCdpAdapter`. Construction is inert: it never touches Chrome,
and the factory refuses to accept an adapter/runner/fallback/window override so
the fixed rolling 3-day incremental policy stays owned by the adapter.

No real CDP is exercised here — a fake scraper is injected through the adapter's
internal seam so the trigger→method mapping and the ScrapeResult→ReconcileOutcome
conversion are deterministic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fin_analyse.scraper.cdp_runtime import (
    WindowsChromeCdpAdapter,
    build_production_cdp_module,
)
from fin_analyse.scraper.cdp_scraper import (
    INCREMENTAL_WINDOW_DAYS,
    CdpBridgeScraper,
    ScrapeResult,
)
from fin_analyse.scraper.contracts import ZsxqRunStatus
from fin_analyse.scraper.module import ZsxqScraperModule
from fin_analyse.scraper.runtime_repository import ScraperRuntimeRepository


class _FakeScraper:
    """A fake CDP scraper standing in for CdpBridgeScraper (no real Chrome).

    Records the surface method invoked and the ``deadline_at``/``checkpoint`` it
    was built with, and returns a scripted :class:`ScrapeResult`.
    """

    def __init__(self, *, deadline_at=None, checkpoint=None, result=None) -> None:
        self.deadline_at = deadline_at
        self.checkpoint = checkpoint
        self._result = result or ScrapeResult()
        self.calls: list[str] = []
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *args):
        self.exited += 1
        return False

    def run_incremental_with_result(self) -> ScrapeResult:
        self.calls.append("run_incremental_with_result")
        return self._result

    def run_priority_scan(self) -> ScrapeResult:
        self.calls.append("run_priority_scan")
        return self._result


def _fixed_deadline(seconds: float = 120.0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _make_adapter(result: ScrapeResult | None = None) -> tuple[WindowsChromeCdpAdapter, list]:
    built: list[_FakeScraper] = []

    def factory(*, deadline_at, checkpoint):
        scraper = _FakeScraper(deadline_at=deadline_at, checkpoint=checkpoint, result=result)
        built.append(scraper)
        return scraper

    return WindowsChromeCdpAdapter(scraper_factory=factory), built


def _timeline_evidence(*timestamps: str, schema_version: object = 1) -> str:
    return json.dumps(
        {
            "schema_version": schema_version,
            "items": [
                {
                    "topic_id": str(100000000 + index),
                    "header_lines": ["三线文案大锅饭", timestamp],
                    "timestamps": [timestamp],
                }
                for index, timestamp in enumerate(timestamps)
            ],
        },
        ensure_ascii=False,
    )


def _offline_timeline_text(now: datetime) -> str:
    current_date = now.strftime("%Y-%m-%d %H:%M")
    quoted_old_date = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M")
    return (
        f"三线文案大锅饭\n{current_date}\n窗口内股票研究\n能量评分 9.2 分\n"
        + "这是一篇讨论股票、产业链和供需关系的窗口内正文。" * 20
        + f"\n正文引用如下：\n三线文案大锅饭\n{quoted_old_date}\n"
        + "这只是正文中的历史引文，不是另一个时间线卡片。" * 20
    )


class _OfflineTimelineBrowser:
    def __init__(
        self,
        *,
        full_text: str,
        evidence: str | list[str],
        scroll_metrics: str | list[str],
    ) -> None:
        self._full_text = full_text
        self._evidence = evidence
        self._scroll_metrics = scroll_metrics
        self._evidence_calls = 0
        self._scroll_metric_calls = 0

    def navigate(self, url, wait=3.0):
        return None

    def scroll_by(self, px=4000, wait=1.0):
        return None

    def js(self, script):
        if "finTimelineTimestampEvidence" in script:
            if isinstance(self._evidence, list):
                index = min(self._evidence_calls, len(self._evidence) - 1)
                self._evidence_calls += 1
                return self._evidence[index]
            return self._evidence
        if "finTimelineLoaderState" in script:
            return '{"visible":false}'
        if "document.body.innerText" in script:
            return self._full_text
        if "scrollTop" in script and "scrollHeight" in script:
            if isinstance(self._scroll_metrics, list):
                index = self._scroll_metric_calls % len(self._scroll_metrics)
                self._scroll_metric_calls += 1
                return self._scroll_metrics[index]
            return self._scroll_metrics
        if "const datePattern" in script:
            return "[]"
        if "查看详情" in script:
            return "done"
        return ""


def _run_offline_sync(
    tmp_path,
    monkeypatch,
    *,
    evidence: str | list[str],
    scroll_metrics: str | list[str] = ('{"scrollTop":0,"clientHeight":800,"scrollHeight":10000}'),
):
    from fin_analyse.scraper import cdp_scraper as cdp_scraper_module

    now = datetime.now(timezone(timedelta(hours=8)))
    kb_root = tmp_path / "knowledge-base"
    browser = _OfflineTimelineBrowser(
        full_text=_offline_timeline_text(now),
        evidence=evidence,
        scroll_metrics=scroll_metrics,
    )

    class OfflineScraper(CdpBridgeScraper):
        def start(self) -> bool:
            self._client = browser
            return True

        def close(self) -> None:
            self._client = None

    def scraper_factory(*, deadline_at, checkpoint):
        return OfflineScraper(
            knowledge_base_root=kb_root,
            deadline_at=deadline_at,
            checkpoint=checkpoint,
        )

    monkeypatch.setattr(cdp_scraper_module.time, "sleep", lambda _seconds: None)
    repository = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    module = ZsxqScraperModule(
        repository=repository,
        adapter=WindowsChromeCdpAdapter(scraper_factory=scraper_factory),
    )
    try:
        result = module.run()
    finally:
        repository.close()
    return result, kb_root


def test_package_factory_constructs_one_cdp_module_with_windows_chrome_cdp_adapter(tmp_path):
    """The production factory wires exactly one module/repo/adapter, touching no Chrome."""
    db_path = str(tmp_path / "runtime.sqlite3")
    knowledge_base_root = tmp_path / "knowledge-base"

    module = build_production_cdp_module(
        runtime_db_path=db_path,
        knowledge_base_root=knowledge_base_root,
    )

    assert isinstance(module, ZsxqScraperModule)
    assert isinstance(module._adapter, WindowsChromeCdpAdapter)
    assert isinstance(module._repo, ScraperRuntimeRepository)
    # Construction is inert: the adapter has not built a scraper nor started Chrome.
    assert module._adapter.scraper_builds == 0
    module._repo.close()


def test_incremental_mode_drives_full_group_scan_and_converts_outcome():
    """``sync`` intent calls run_incremental_with_result; new_count→changed_count."""
    adapter, built = _make_adapter(
        ScrapeResult(
            new_count=4,
            scrape_completed=True,
            warnings=["boundary_status=unknown"],
        )
    )

    outcome = adapter.run_incremental(
        mode="sync", deadline_at=_fixed_deadline(), checkpoint=lambda: None
    )

    assert [s.calls for s in built] == [["run_incremental_with_result"]]
    assert built[0].entered == 1 and built[0].exited == 1
    assert outcome.changed_count == 4
    assert outcome.warnings == ["boundary_status=unknown"]


def test_priority_mode_drives_priority_scan_surface():
    """``watch`` intent calls run_priority_scan (not the full group scan)."""
    adapter, built = _make_adapter(ScrapeResult(new_count=2, scrape_completed=True))

    outcome = adapter.run_incremental(
        mode="watch", deadline_at=_fixed_deadline(), checkpoint=lambda: None
    )

    assert [s.calls for s in built] == [["run_priority_scan"]]
    assert outcome.changed_count == 2


def test_incomplete_scrape_fails_closed_instead_of_projecting_no_change(tmp_path):
    """An incomplete CDP result must terminalize as FAILED, never NO_CHANGE."""
    incomplete = ScrapeResult(
        scrape_completed=False,
        failure_kind="content_insufficient",
        warnings=["group_timeline_content_insufficient:chars=67:posts=0"],
    )
    direct_adapter, _ = _make_adapter(incomplete)

    with pytest.raises(RuntimeError, match="content_insufficient"):
        direct_adapter.run_incremental(
            mode="sync", deadline_at=_fixed_deadline(), checkpoint=lambda: None
        )

    adapter, built = _make_adapter(incomplete)
    repository = ScraperRuntimeRepository(tmp_path / "runtime.sqlite3")
    module = ZsxqScraperModule(repository=repository, adapter=adapter)

    result = module.run()
    stored = repository.get_run(result.run_id)
    repository.close()

    assert result.status == ZsxqRunStatus.FAILED.value
    assert stored is not None
    assert stored["status"] == ZsxqRunStatus.FAILED.value
    assert [s.calls for s in built] == [["run_incremental_with_result"]]
    assert built[0].entered == 1 and built[0].exited == 1


@pytest.mark.parametrize(
    "warning",
    [
        "priority_surface_failed:star_columns",
        "priority_events_failed:private-detail-must-not-cross",
        "deep_read_artifacts_failed:private-detail-must-not-cross",
        "g_working_set_support_repair_failed",
        "[DEEP-READ] article-private-ref: artifact generation failed",
    ],
)
def test_incomplete_publication_fails_closed_instead_of_reporting_success(warning):
    """A covered crawl is still failed when required G publications are incomplete."""
    adapter, _ = _make_adapter(
        ScrapeResult(
            scrape_completed=True,
            warnings=[warning],
        )
    )

    with pytest.raises(
        RuntimeError,
        match="CDP scrape completed with incomplete publications",
    ) as exc_info:
        adapter.run_incremental(
            mode="sync",
            deadline_at=_fixed_deadline(),
            checkpoint=lambda: None,
        )

    assert "private-detail" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("failed_surface", "expected_warning"),
    [
        ("star_columns", "priority_surface_failed:star_columns"),
        ("digests", "priority_surface_failed:digests"),
    ],
)
def test_priority_scan_surfaces_watch_failures_before_success_projection(
    tmp_path,
    monkeypatch,
    failed_surface,
    expected_warning,
):
    from fin_analyse.scraper import cdp_runtime

    scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
    monkeypatch.setattr(scraper, "_load_index", lambda: None)
    monkeypatch.setattr(
        scraper,
        "_scan_star_section",
        (
            (lambda *_args: (_ for _ in ()).throw(RuntimeError("private-star-error")))
            if failed_surface == "star_columns"
            else (lambda *_args: [])
        ),
    )
    monkeypatch.setattr(
        scraper,
        "_scan_digests",
        (
            (lambda *_args: (_ for _ in ()).throw(RuntimeError("private-digest-error")))
            if failed_surface == "digests"
            else (lambda *_args: [])
        ),
    )

    result = scraper.run_priority_scan()

    assert expected_warning in result.warnings
    with pytest.raises(RuntimeError, match="incomplete publications") as exc_info:
        cdp_runtime._to_reconcile_outcome(result)
    assert "private-" not in str(exc_info.value)


def test_sync_rejects_quoted_author_and_old_date_before_any_knowledge_base_write(
    tmp_path, monkeypatch
):
    """Max-scroll exhaustion plus quoted old body text cannot prove sync coverage."""
    now = datetime.now(timezone(timedelta(hours=8)))
    current_date = now.strftime("%Y-%m-%d %H:%M")
    result, kb_root = _run_offline_sync(
        tmp_path,
        monkeypatch,
        evidence=_timeline_evidence(current_date),
    )

    assert result.status == ZsxqRunStatus.FAILED.value
    assert not kb_root.exists()


@pytest.mark.parametrize(
    "evidence",
    [
        "not-json",
        _timeline_evidence("2026-07-01 10:00", schema_version=True),
        _timeline_evidence("2026-07-01 10:00", schema_version=1.0),
        (
            '{"schema_version":1,"items":[{"topic_id":"100000000",'
            '"header_lines":["三线文案大锅饭","2026-07-20 10:00"],"timestamps":[]}]}'
        ),
        (
            '{"schema_version":1,"items":[{"topic_id":"100000000",'
            '"header_lines":["三线文案大锅饭","2026-07-20 10:00"],'
            '"timestamps":["2026-07-20 10:00","2026-07-01 10:00"]}]}'
        ),
        (
            '{"schema_version":1,"items":[],"items":[{"topic_id":"100000000",'
            '"header_lines":["三线文案大锅饭","2026-07-01 10:00"],'
            '"timestamps":["2026-07-01 10:00"]}]}'
        ),
    ],
)
def test_sync_rejects_malformed_or_ambiguous_dom_coverage_before_kb_writes(
    tmp_path, monkeypatch, evidence
):
    result, kb_root = _run_offline_sync(tmp_path, monkeypatch, evidence=evidence)

    assert result.status == ZsxqRunStatus.FAILED.value
    assert not kb_root.exists()


@pytest.mark.parametrize(
    "scroll_metrics",
    [
        '{"scrollTop":1000000,"clientHeight":100,"scrollHeight":1000}',
        ('{"scrollTop":0,"scrollTop":900,"clientHeight":100,"scrollHeight":1000}'),
    ],
)
def test_sync_rejects_malformed_bottom_geometry_before_kb_writes(
    tmp_path, monkeypatch, scroll_metrics
):
    current_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    result, kb_root = _run_offline_sync(
        tmp_path,
        monkeypatch,
        evidence=_timeline_evidence(current_date),
        scroll_metrics=scroll_metrics,
    )

    assert result.status == ZsxqRunStatus.FAILED.value
    assert not kb_root.exists()


def test_sync_accepts_stable_consistent_page_end_with_timeline_evidence(tmp_path, monkeypatch):
    current_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    result, kb_root = _run_offline_sync(
        tmp_path,
        monkeypatch,
        evidence=_timeline_evidence(current_date),
        scroll_metrics='{"scrollTop":900,"clientHeight":100,"scrollHeight":1000}',
    )

    assert result.status == ZsxqRunStatus.SUCCEEDED.value
    assert len(list((kb_root / "articles").glob("*.md"))) == 1


def test_sync_rejects_page_end_when_timeline_evidence_disappears_before_writes(
    tmp_path, monkeypatch
):
    current_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    result, kb_root = _run_offline_sync(
        tmp_path,
        monkeypatch,
        evidence=[_timeline_evidence(current_date), "not-json"],
        scroll_metrics='{"scrollTop":900,"clientHeight":100,"scrollHeight":1000}',
    )

    assert result.status == ZsxqRunStatus.FAILED.value
    assert not kb_root.exists()


def test_sync_requires_consecutive_valid_bottom_observations(tmp_path, monkeypatch):
    current_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    result, kb_root = _run_offline_sync(
        tmp_path,
        monkeypatch,
        evidence=_timeline_evidence(current_date),
        scroll_metrics=[
            '{"scrollTop":900,"clientHeight":100,"scrollHeight":1000}',
            "not-json",
        ],
    )

    assert result.status == ZsxqRunStatus.FAILED.value
    assert not kb_root.exists()


def test_unknown_mode_is_rejected_visibly_without_touching_chrome():
    """An unknown mode (a legacy combined trigger) raises ValueError before building a scraper."""
    adapter, built = _make_adapter()

    with pytest.raises(ValueError, match="unknown scrape mode 'incremental'"):
        adapter.run_incremental(
            mode="incremental", deadline_at=_fixed_deadline(), checkpoint=lambda: None
        )

    assert adapter.scraper_builds == 0
    assert built == []


@pytest.mark.parametrize("forbidden", ["adapter", "runner", "window_days", "priority_only"])
def test_factory_refuses_adapter_runner_or_window_override(tmp_path, forbidden):
    """The production factory exposes no adapter/runner/fallback/window override."""
    db_path = str(tmp_path / "runtime.sqlite3")
    with pytest.raises(TypeError):
        build_production_cdp_module(
            runtime_db_path=db_path,
            knowledge_base_root=tmp_path / "knowledge-base",
            **{forbidden: object()},
        )


def test_production_factory_requires_explicit_knowledge_base_root(tmp_path):
    """Mutable production content must never default to the release checkout."""
    with pytest.raises(TypeError, match="knowledge_base_root"):
        build_production_cdp_module(runtime_db_path=tmp_path / "runtime.sqlite3")


def test_production_incremental_window_is_fixed_at_three_days():
    """The fixed rolling 3-day incremental policy is preserved and not overridable."""
    assert INCREMENTAL_WINDOW_DAYS == 3


def test_composition_root_has_zero_playwright_or_noop_fallback():
    """The production composition contains no Playwright/Noop import or fallback."""
    from fin_analyse.scraper import cdp_runtime

    source = Path(cdp_runtime.__file__).read_text().lower()
    assert "playwright" not in source
    assert "noop" not in source
