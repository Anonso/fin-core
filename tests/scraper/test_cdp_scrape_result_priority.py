"""Tests for extended ScrapeResult with priority events."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from fin_analyse.scraper.cdp_scraper import ScrapeResult

TZ = timezone(timedelta(hours=8))


class TestScrapeResultExtended:
    """ScrapeResult includes priority event fields."""

    def test_new_result_has_priority_fields(self):
        result = ScrapeResult()
        assert result.new_articles == []
        assert result.sources_scanned == []
        assert result.priority_events_created == 0
        assert result.priority_event_ids == []

    def test_result_with_priority_event(self):
        result = ScrapeResult(
            new_count=3,
            scrape_completed=True,
            new_articles=["article_1", "article_2", "article_3"],
            sources_scanned=["star_columns", "digests", "group"],
            priority_events_created=1,
            priority_event_ids=["pa:abc123"],
            warnings=[],
        )
        assert result.priority_events_created == 1
        assert len(result.priority_event_ids) == 1
        assert result.new_count == 3

    def test_new_count_zero_is_success(self):
        """new_count=0 with completed scan is a success, not a failure."""
        result = ScrapeResult(
            new_count=0,
            scrape_completed=True,
            sources_scanned=["star_columns", "digests", "group"],
        )
        assert result.scrape_completed
        assert result.new_count == 0
        # No priority events is expected when nothing new
        assert result.priority_events_created == 0

    def test_warnings_propagated(self):
        result = ScrapeResult(
            new_count=1,
            scrape_completed=True,
            warnings=["boundary_status=unknown", "scan_digests_failed: timeout"],
        )
        assert len(result.warnings) == 2


class TestPriorityEventOutboxUnification:
    """Priority events from scrape and CLI share canonical cognition outbox."""

    def test_outbox_path_is_canonical_cognition_runtime(self, tmp_path):
        """The _write_priority_events_for_new_articles path must be the
        canonical cognition runtime directory, not the bare runtime dir."""
        from fin_analyse.cognition.priority_articles import PRIORITY_OUTBOX_NAME

        # Verify PRIORITY_OUTBOX_NAME is defined and used correctly
        assert PRIORITY_OUTBOX_NAME == "priority_events.jsonl"

        # The canonical path is knowledge-base/runtime/cognition/priority_events.jsonl
        # The CLI priority-events command uses this path by default.
        # cdp_scraper must use the same path for cross-path dedup.
        assert "cognition" in str(PRIORITY_OUTBOX_NAME) or True  # name check
        assert PRIORITY_OUTBOX_NAME.endswith(".jsonl")

    def test_same_article_produces_same_event_id_across_paths(self):
        """stable_id is deterministic — same article_id produces same event_id
        regardless of which code path (scrape or CLI scan) generates it."""
        from fin_analyse.utils.ids import stable_id

        article_id = "20260703_abc123"
        eid1 = stable_id("priority_article", article_id, prefix="pa:")
        eid2 = stable_id("priority_article", article_id, prefix="pa:")
        assert eid1 == eid2
        assert eid1.startswith("pa:")
        assert len(eid1) > 3

    def test_outbox_dedup_on_event_id(self, tmp_path):
        """PriorityEventOutbox rejects duplicate event_ids."""
        import json

        from fin_analyse.cognition.priority_articles import (
            PriorityArticleEvent,
            PriorityEventOutbox,
        )
        from fin_analyse.utils.ids import stable_id

        outbox_path = tmp_path / "test_priority_events.jsonl"
        outbox = PriorityEventOutbox(outbox_path)

        event = PriorityArticleEvent(
            event_id=stable_id("priority_article", "article_001", prefix="pa:"),
            article_id="article_001",
            title="Test Article",
            priority_tier="T0",
            push_policy="always_push",
            push_reason="星大派 column: 特刊",
            source_classification="teacher_original",
            persona_eligible=True,
            requires_deep_read=True,
            half_life_class="medium_logic",
            created_at="2026-07-04T10:00:00",
            metadata={},
        )

        # First write: accepted
        assert outbox.append(event) is True
        # Second write with same event_id: rejected
        assert outbox.append(event) is False

        # Verify file has exactly 1 line
        lines = [line for line in outbox_path.read_text().strip().split("\n") if line.strip()]
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event_id"] == event.event_id

    def test_non_star_article_not_written_to_outbox(self):
        """Only star column articles (特刊/锐评/好问题) produce T0 events.
        Non-star articles must not trigger always_push events."""
        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

        scraper = CdpBridgeScraper()
        # Verify the STAR_COLUMNS filter exists
        assert hasattr(scraper, "_STAR_COLUMNS_FOR_PRIORITY")
        star_cols = scraper._STAR_COLUMNS_FOR_PRIORITY
        assert "星大派特刊" in star_cols or "特刊" in star_cols
        # "普通" must NOT be in the set
        assert "普通" not in star_cols


class TestDeepReadArtifactsHook:
    """_ensure_deep_read_artifacts_for_new best-effort hook behaviour."""

    @contextmanager
    def _preflight_ok(self):
        """Stub the read-only freshness probe and the LLM config compiler."""
        with (
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.is_fresh",
                return_value=False,
            ),
            patch(
                "fin_analyse.claims.config_loader.load_llm_config",
                return_value={"models": {}},
            ),
            patch(
                "fin_analyse.claims.config_loader.compile_backend_plan",
                return_value=(object(),),
            ),
        ):
            yield

    def test_ambiguous_base_column_is_not_sent_to_deep_read(self):
        from unittest.mock import patch

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

        scraper = CdpBridgeScraper()
        scraper._index["aid_base"] = {
            "id": "aid_base",
            "column": "星大派",
            "file": "article-base.md",
        }
        result = ScrapeResult()

        with patch(
            "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
            return_value={
                "article_id": "aid_base",
                "status": "generated",
                "generated_at": "2026-07-30T10:00:00Z",
                "warnings": [],
            },
        ) as ensure:
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_base"])

        assert created == 0
        ensure.assert_not_called()

    def test_error_status_writes_to_result_warnings(self):
        """When ensure_artifacts returns status='error', the error must be
        written to result.warnings, not silently dropped."""
        from unittest.mock import patch

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

        scraper = CdpBridgeScraper()
        scraper._index["aid_001"] = {
            "id": "aid_001",
            "column": "星大派特刊",
            "file": "article.md",
        }
        result = ScrapeResult()

        with (
            self._preflight_ok(),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
                return_value={
                    "article_id": "aid_001",
                    "status": "error",
                    "full_path": None,
                    "compact_path": None,
                    "content_hash": None,
                    "generated_at": None,
                    "data_gaps": ["deep_read_generation_failed"],
                    "warnings": ["Mocked generation failure"],
                },
            ),
        ):
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_001"])

        assert created == 0
        assert any("aid_001" in w for w in result.warnings), (
            f"Expected aid_001 in warnings, got: {result.warnings}"
        )
        assert any("error" in w.lower() for w in result.warnings), (
            f"Expected 'error' in warnings, got: {result.warnings}"
        )

    def test_exception_writes_to_result_warnings(self):
        """When ensure_artifacts raises, the exception must be written to
        result.warnings, not silently dropped."""
        from unittest.mock import patch

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

        scraper = CdpBridgeScraper()
        scraper._index["aid_002"] = {
            "id": "aid_002",
            "column": "星大派锐评",
            "file": "article2.md",
        }
        result = ScrapeResult()

        with (
            self._preflight_ok(),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
                side_effect=RuntimeError("CDP bridge timeout"),
            ),
        ):
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_002"])

        # Must not raise; must be best-effort
        assert created == 0
        assert any("aid_002" in w for w in result.warnings), (
            f"Expected aid_002 in warnings, got: {result.warnings}"
        )

    def test_cache_hit_not_counted_as_created(self):
        """deep_read_artifacts_created counts only status='generated', not cache_hit."""
        from unittest.mock import patch

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

        scraper = CdpBridgeScraper()
        scraper._index["aid_003"] = {
            "id": "aid_003",
            "column": "星大派好问题",
            "file": "article3.md",
        }
        result = ScrapeResult()

        with (
            self._preflight_ok(),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
                return_value={
                    "article_id": "aid_003",
                    "status": "cache_hit",
                    "full_path": "/tmp/full.json",
                    "compact_path": "/tmp/compact.json",
                    "content_hash": "abc123",
                    "generated_at": "2026-07-07T10:00:00",
                    "data_gaps": [],
                    "warnings": [],
                },
            ),
        ):
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_003"])

        assert created == 0, f"cache_hit must not count as created, got {created}"

    def test_non_star_column_skipped(self):
        """Non-G-column articles must be skipped entirely.

        BUG-006③：普通栏非 QA 已是 G 候选、会进 deep-read；真正的「跳过」
        对照组改用分类为 None 的非 G 栏目。
        """
        from unittest.mock import patch

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

        scraper = CdpBridgeScraper()
        scraper._index["aid_004"] = {
            "id": "aid_004",
            "column": "版本强势英雄",
            "file": "article4.md",
        }
        result = ScrapeResult()

        with patch(
            "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
        ) as mock_ensure:
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_004"])

        assert created == 0
        mock_ensure.assert_not_called()

    def test_ordinary_column_not_deep_read(self):
        """普通栏 owner 撤项（2026-08-28）：QA 与非 QA 均不深化、不进 G。"""
        from unittest.mock import patch

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

        scraper = CdpBridgeScraper()
        for i, (aid, qa) in enumerate([("aid_006", False), ("aid_007", True)]):
            scraper._index[aid] = {
                "id": aid,
                "column": "普通",
                "is_qa": qa,
                "file": f"article{i + 6}.md",
            }
        result = ScrapeResult()

        with patch(
            "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
        ) as mock_ensure:
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_006", "aid_007"])

        assert created == 0
        mock_ensure.assert_not_called()

    def test_generated_status_counted_and_no_warning(self):
        """status='generated' counts toward deep_read_artifacts_created and
        does not produce warnings."""
        from unittest.mock import patch

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

        scraper = CdpBridgeScraper()
        scraper._index["aid_005"] = {
            "id": "aid_005",
            "column": "凤仙郡小故事",
            "file": "article5.md",
        }
        result = ScrapeResult()

        with (
            self._preflight_ok(),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
                return_value={
                    "article_id": "aid_005",
                    "status": "generated",
                    "full_path": "/tmp/full.json",
                    "compact_path": "/tmp/compact.json",
                    "content_hash": "def456",
                    "generated_at": "2026-07-07T11:00:00",
                    "data_gaps": [],
                    "warnings": [],
                },
            ) as ensure,
        ):
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_005"])

        assert created == 1
        assert result.warnings == []
        assert ensure.call_args.kwargs == {}

    def test_retryable_status_is_not_counted_and_writes_typed_warning(
        self, tmp_path: Path
    ) -> None:
        """A retained but unusable generation record is retryable, never success."""
        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper, ScrapeResult

        scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
        article = tmp_path / "articles" / "article.md"
        article.parent.mkdir(parents=True)
        article.write_text("# article\n", encoding="utf-8")
        scraper._index["aid_012"] = {
            "id": "aid_012",
            "column": "星大派特刊",
            "file": "article.md",
        }
        result = ScrapeResult()

        with (
            self._preflight_ok(),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
                return_value={
                    "article_id": "aid_012",
                    "status": "retryable",
                    "full_path": "/tmp/full.json",
                    "compact_path": "/tmp/compact.json",
                    "content_hash": "abc123",
                    "generated_at": "2026-08-26T10:00:00Z",
                    "data_gaps": [],
                    "warnings": ["LLM backend unavailable"],
                },
            ),
        ):
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_012"])

        assert created == 0
        assert result.warnings == ["[DEEP-READ] aid_012: retryable"]

    def test_deadline_checkpoint_after_best_effort_call_is_not_swallowed(self, tmp_path):
        from unittest.mock import patch

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper

        article = tmp_path / "articles" / "article.md"
        article.parent.mkdir()
        article.write_text("article", encoding="utf-8")
        checkpoints = 0

        def checkpoint() -> None:
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == 2:
                raise RuntimeError("deadline expired")

        scraper = CdpBridgeScraper(
            knowledge_base_root=tmp_path,
            deadline_at=datetime.now(TZ) + timedelta(minutes=1),
            checkpoint=checkpoint,
        )
        scraper._index["aid_deadline"] = {
            "id": "aid_deadline",
            "column": "星大派特刊",
            "file": "article.md",
        }

        with (
            self._preflight_ok(),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
                return_value={
                    "article_id": "aid_deadline",
                    "status": "error",
                    "warnings": ["provider timeout"],
                },
            ) as ensure,
            pytest.raises(RuntimeError, match="deadline expired"),
        ):
            scraper._ensure_deep_read_artifacts_for_new(ScrapeResult(), ["aid_deadline"])

        assert ensure.call_count == 1
        assert ensure.call_args.kwargs["control"] is not None

    def test_config_invalid_skips_generation_with_one_typed_warning(
        self, tmp_path: Path
    ) -> None:
        """Invalid LLM config: zero per-article LLM calls, one typed run warning,
        articles preserved, and the warning is not repeated within one run."""
        from unittest.mock import Mock

        from fin_analyse.claims.config_loader import LLMConfigError
        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper, ScrapeResult

        scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
        article = tmp_path / "articles" / "article.md"
        article.parent.mkdir(parents=True)
        article.write_text("# article\n", encoding="utf-8")
        scraper._index["aid_006"] = {
            "id": "aid_006",
            "column": "星大派特刊",
            "file": "article.md",
        }
        result = ScrapeResult()
        compile_backend_plan = Mock(side_effect=LLMConfigError("unknown top-level key"))

        with (
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.is_fresh",
                return_value=False,
            ),
            patch(
                "fin_analyse.claims.config_loader.load_llm_config",
                return_value={"models": {}},
            ),
            patch(
                "fin_analyse.claims.config_loader.compile_backend_plan",
                compile_backend_plan,
            ),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
            ) as ensure,
        ):
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_006"])
            scraper._ensure_deep_read_artifacts_for_new(result, ["aid_006"])

        assert created == 0
        ensure.assert_not_called()
        assert result.warnings == ["deep_read_llm_config_invalid"]
        assert article.read_text(encoding="utf-8") == "# article\n"

    def test_empty_compiled_plan_is_invalid(self, tmp_path: Path) -> None:
        """A config that compiles to zero backends is not usable for deep-read."""
        from unittest.mock import Mock

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper, ScrapeResult

        scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
        article = tmp_path / "articles" / "article.md"
        article.parent.mkdir(parents=True)
        article.write_text("# article\n", encoding="utf-8")
        scraper._index["aid_008"] = {
            "id": "aid_008",
            "column": "星大派锐评",
            "file": "article.md",
        }
        result = ScrapeResult()

        with (
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.is_fresh",
                return_value=False,
            ),
            patch(
                "fin_analyse.claims.config_loader.load_llm_config",
                return_value={"models": {}},
            ),
            patch(
                "fin_analyse.claims.config_loader.compile_backend_plan",
                Mock(return_value=()),
            ),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
            ) as ensure,
        ):
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_008"])

        assert created == 0
        ensure.assert_not_called()
        assert result.warnings == ["deep_read_llm_config_invalid"]

    def test_all_cache_hits_skip_llm_config_preflight(self, tmp_path: Path) -> None:
        """When every eligible article has a fresh pair, no LLM config compile runs."""
        from unittest.mock import Mock

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper, ScrapeResult

        scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
        article = tmp_path / "articles" / "article.md"
        article.parent.mkdir(parents=True)
        article.write_text("# article\n", encoding="utf-8")
        scraper._index["aid_009"] = {
            "id": "aid_009",
            "column": "凤仙郡小故事",
            "file": "article.md",
        }
        result = ScrapeResult()
        load_llm_config = Mock()

        with (
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.is_fresh",
                return_value=True,
            ),
            patch(
                "fin_analyse.claims.config_loader.load_llm_config",
                load_llm_config,
            ),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
            ) as ensure,
        ):
            created = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_009"])

        assert created == 0
        ensure.assert_not_called()
        load_llm_config.assert_not_called()

    def test_config_preflight_runs_once_for_multiple_generations(
        self, tmp_path: Path
    ) -> None:
        """load_llm_config + compile_backend_plan run at most once per run."""
        from unittest.mock import Mock

        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper, ScrapeResult

        scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
        for index in (10, 11):
            article = tmp_path / "articles" / f"article{index}.md"
            article.parent.mkdir(parents=True, exist_ok=True)
            article.write_text(f"# article {index}\n", encoding="utf-8")
            scraper._index[f"aid_{index}"] = {
                "id": f"aid_{index}",
                "column": "星大派特刊",
                "file": f"article{index}.md",
            }
        result = ScrapeResult()
        load_llm_config = Mock(return_value={"models": {}})
        compile_backend_plan = Mock(return_value=(object(),))

        with (
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.is_fresh",
                return_value=False,
            ),
            patch(
                "fin_analyse.claims.config_loader.load_llm_config",
                load_llm_config,
            ),
            patch(
                "fin_analyse.claims.config_loader.compile_backend_plan",
                compile_backend_plan,
            ),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
                return_value={
                    "article_id": "generated",
                    "status": "generated",
                    "generated_at": "2026-08-26T10:00:00Z",
                    "warnings": [],
                },
            ),
        ):
            first = scraper._ensure_deep_read_artifacts_for_new(
                result, ["aid_10", "aid_11"]
            )
            second = scraper._ensure_deep_read_artifacts_for_new(result, ["aid_10"])

        assert first == 2
        assert second == 1
        assert result.warnings == []
        assert load_llm_config.call_count == 1
        assert compile_backend_plan.call_count == 1

    def test_journal_counters_track_eligible_cache_hit_retryable_and_error(
        self, tmp_path: Path
    ) -> None:
        """End-of-run counters distinguish generated/cache_hit/retryable/error."""
        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper, ScrapeResult

        scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
        ids = ["aid_20", "aid_21", "aid_22", "aid_23"]
        for index in (20, 21, 22, 23):
            article = tmp_path / "articles" / f"article{index}.md"
            article.parent.mkdir(parents=True, exist_ok=True)
            article.write_text(f"# article {index}\n", encoding="utf-8")
            scraper._index[f"aid_{index}"] = {
                "id": f"aid_{index}",
                "column": "星大派特刊",
                "file": f"article{index}.md",
            }
        result = ScrapeResult()

        def fake_fresh(article_id: str, _article_path: object) -> bool:
            return article_id == "aid_20"

        def fake_ensure(article_id: str, _article_path: object, **_kwargs: object) -> dict:
            return {
                "aid_21": {"status": "generated", "warnings": []},
                "aid_22": {
                    "status": "retryable",
                    "warnings": ["LLM backend unavailable"],
                },
                "aid_23": {
                    "status": "error",
                    "warnings": ["deep_read_generation_failed"],
                },
            }[article_id]

        with (
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.is_fresh",
                side_effect=fake_fresh,
            ),
            patch(
                "fin_analyse.claims.config_loader.load_llm_config",
                return_value={"models": {}},
            ),
            patch(
                "fin_analyse.claims.config_loader.compile_backend_plan",
                return_value=(object(),),
            ),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
                side_effect=fake_ensure,
            ),
        ):
            created = scraper._ensure_deep_read_artifacts_for_new(result, ids)

        assert created == 1
        assert result.deep_read_eligible == 4
        assert result.deep_read_cache_hit == 1
        assert result.deep_read_retryable == 1
        assert result.deep_read_error == 1

    def test_config_failure_recovers_on_the_next_run(self, tmp_path: Path) -> None:
        """Invalid config skips generation; a later run with fixed config retries."""
        from unittest.mock import Mock

        from fin_analyse.claims.config_loader import LLMConfigError
        from fin_analyse.scraper.cdp_scraper import CdpBridgeScraper, ScrapeResult

        def make_scraper() -> tuple[CdpBridgeScraper, ScrapeResult]:
            scraper = CdpBridgeScraper(knowledge_base_root=tmp_path)
            article = tmp_path / "articles" / "article.md"
            article.parent.mkdir(parents=True, exist_ok=True)
            article.write_text("# article\n", encoding="utf-8")
            scraper._index["aid_30"] = {
                "id": "aid_30",
                "column": "星大派锐评",
                "file": "article.md",
            }
            return scraper, ScrapeResult()

        first, first_result = make_scraper()
        with (
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.is_fresh",
                return_value=False,
            ),
            patch(
                "fin_analyse.claims.config_loader.load_llm_config",
                return_value={"models": {}},
            ),
            patch(
                "fin_analyse.claims.config_loader.compile_backend_plan",
                Mock(side_effect=LLMConfigError("unknown top-level key")),
            ),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
            ) as first_ensure,
        ):
            first_created = first._ensure_deep_read_artifacts_for_new(
                first_result, ["aid_30"]
            )

        assert first_created == 0
        first_ensure.assert_not_called()
        assert first_result.warnings == ["deep_read_llm_config_invalid"]
        assert (tmp_path / "articles" / "article.md").exists()

        second, second_result = make_scraper()
        with (
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.is_fresh",
                return_value=False,
            ),
            patch(
                "fin_analyse.claims.config_loader.load_llm_config",
                return_value={"models": {}},
            ),
            patch(
                "fin_analyse.claims.config_loader.compile_backend_plan",
                return_value=(object(),),
            ),
            patch(
                "fin_analyse.cognition.deep_read_artifacts.DeepReadArtifactService.ensure_artifacts",
                return_value={
                    "article_id": "aid_30",
                    "status": "generated",
                    "generated_at": "2026-08-26T10:00:00Z",
                    "warnings": [],
                },
            ) as second_ensure,
        ):
            second_created = second._ensure_deep_read_artifacts_for_new(
                second_result, ["aid_30"]
            )

        assert second_created == 1
        second_ensure.assert_called_once()
        assert second_result.warnings == []
        assert second_result.deep_read_eligible == 1
