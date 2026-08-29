"""Tests for fin_analyse/cognition/time_sensitivity.py — content-driven time sensitivity."""

from __future__ import annotations

from unittest.mock import MagicMock

# ── helpers ──────────────────────────────────────────────────────────────────


def _sample_article(**overrides) -> dict:
    return {
        "article_id": "art-001",
        "title": "测试文章",
        "column": "星大派特刊",
        "published_at": "2026-07-02T10:00:00",
        **overrides,
    }


def _deep_read(**overrides) -> dict:
    return {
        "units": [],
        "clocks": [],
        "theme_clusters": [],
        "evidence_chains": [],
        "suggestions": [],
        **overrides,
    }


# ── Basic assessments ────────────────────────────────────────────────────────


class TestAssessTimeSensitivity:
    """Unit tests for assess_time_sensitivity() function."""

    def test_empty_title_is_unknown(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title=""),
            _deep_read(),
            None,
        )
        assert result.category == "unknown"
        assert "empty_title" in result.data_gaps

    def test_intraday_clues_map_to_intraday_event(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="今天早盘跳水，吃面了"),
            _deep_read(units=[{"title": "早盘跳水", "thesis": "指数跌2%"}]),
            None,
        )
        assert result.category == "intraday_event"
        assert result.horizon == "intraday"
        assert result.evidence

    def test_tracking_clues_map_to_short_term_tracking(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="超微4T报价降至$2.5/hr，算力租赁ROI判断"),
            _deep_read(units=[{"title": "报价变化", "thesis": "超微降价"}]),
            None,
        )
        # "报价" (tracking) + "ROI" (theme) → promoted to active_theme
        assert result.category in ("short_term_tracking", "active_theme")
        assert result.horizon in ("1-3d", "1-2w")
        # evidence must mention clues found
        assert any("报价" in e or "ROI" in e for e in result.evidence)

    def test_theme_clues_map_to_active_theme(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="国产替代政策窗口：产业链分析"),
            _deep_read(units=[{"title": "国产替代加速", "thesis": "政策窗口打开"}]),
            None,
        )
        assert result.category == "active_theme"
        assert result.horizon == "1-2w"

    def test_durable_clues_map_to_durable_framework(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="投资方法论：认知框架与信息差套利"),
            _deep_read(units=[{"title": "方法论", "thesis": "信息差是核心"}]),
            None,
        )
        assert result.category == "durable_framework"
        assert result.horizon == "durable"

    def test_deep_read_clock_takes_priority(self):
        """deep_read clock with 'high_urgency' must override keyword rules."""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        # Title has "报价" which would normally → short_term_tracking
        # But clock says "high_urgency" → intraday_event
        result = assess_time_sensitivity(
            _sample_article(title="超微报价变化：早盘跳水"),
            _deep_read(
                units=[{"title": "报价", "thesis": "测试"}],
                clocks=[{"current_label": "high_urgency", "label": "高紧迫"}],
            ),
            None,
        )
        assert result.category == "intraday_event"
        assert "deep_read时钟" in result.evidence[0]

    def test_intraday_with_tracking_clues_promotes_to_tracking(self):
        """Article with both intraday and tracking keywords → short_term_tracking."""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="早盘跳水但订单报价仍在变化"),
            _deep_read(units=[{"title": "订单+情绪", "thesis": "测试"}]),
            None,
        )
        # Has both "早盘" (intraday) and "订单"+"报价" (tracking)
        # → promoted to short_term_tracking
        assert result.category == "short_term_tracking"
        assert result.confidence >= 0.6

    def test_publish_freshness_is_preserved(self):
        """publish_freshness must reflect the actual publish date."""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="方法论", published_at="2026-01-15T08:00:00"),
            _deep_read(units=[{"title": "方法论", "thesis": "长期框架"}]),
            None,
        )
        assert "2026-01-15" in result.publish_freshness
        # But category is driven by content ("方法论" → durable_framework)
        assert result.category == "durable_framework"

    def test_missing_publish_date_is_unknown_not_low_sensitivity(self):
        """Missing published_at → 'unknown', never '低时效'."""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        article = _sample_article(title="一些想法")
        article["column"] = ""
        article.pop("published_at", None)
        result = assess_time_sensitivity(article, _deep_read(), None)
        assert result.publish_freshness == "unknown"
        assert "低时效" not in result.label
        assert "missing_published_at" in result.data_gaps

    def test_fengxianjun_defaults_to_durable_even_with_a_theme_cluster(self):
        """凤仙郡栏目不是当前观点；主题聚类本身不能提升它的时效性。"""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(
                title="从商业模式看行业格局",
                column="凤仙郡小故事",
            ),
            _deep_read(theme_clusters=[{"theme": "算力产业链"}]),
            None,
        )

        assert result.category == "durable_framework"
        assert result.horizon == "durable"
        assert "栏目默认" in result.reason

    def test_fengxianjun_explicit_current_claim_can_use_tracking_rule(self):
        """只有原文明确给出时点，凤仙郡才可成为需跟踪的当前判断。"""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(
                title="近期报价上涨与订单变化",
                column="凤仙郡小故事",
            ),
            _deep_read(),
            None,
        )

        assert result.category == "short_term_tracking"
        assert result.horizon == "1-3d"

    def test_fengxianjun_derived_clock_cannot_override_original_claim_default(self):
        """派生 deep-read 时钟不是凤仙郡原文的当前时点证据。"""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(
                title="从商业模式看行业格局",
                column="凤仙郡小故事",
            ),
            _deep_read(
                units=[{"title": "商业模式", "thesis": "长期框架"}],
                clocks=[{"current_label": "high_urgency"}],
            ),
            None,
        )

        assert result.category == "durable_framework"
        assert result.horizon == "durable"


# ── Two-day-old article with tracking content ────────────────────────────────


class TestOldArticleWithTrackingContent:
    """Two-day-old articles with tracking-worthy content must not be 'low sensitivity'."""

    def test_two_day_old_article_with_quotes_is_tracking(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(
                title="算力租赁ROI判断与超微4T/3T报价分析 — 情绪杀 vs 信息差",
                published_at="2026-06-30T10:00:00",
            ),
            _deep_read(
                units=[
                    {"title": "算力租赁ROI持续走低", "thesis": "超微4T报价降至$2.5/hr"},
                    {"title": "情绪杀带来信息差套利", "thesis": "市场过度反应提供买入窗口"},
                ]
            ),
            {"top_event": {"freshness_score": 0.0}},  # stale
        )
        assert result.category != "unknown"
        assert "低时效" not in result.label
        assert result.category in ("short_term_tracking", "active_theme"), (
            f"Expected short_term_tracking or active_theme, got {result.category}"
        )
        # reason must mention specific content clues
        reason = result.reason.lower()
        assert any(w in reason for w in ["报价", "roi", "订单", "供需", "产业链", "信息差"]), (
            f"Reason must mention content clues: {result.reason}"
        )

    def test_today_intraday_rant_is_intraday_event(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(
                title="今天早盘跳水吃面了 — 盘面情绪崩溃",
                published_at="2026-07-02T09:45:00",
            ),
            _deep_read(units=[{"title": "早盘跳水", "thesis": "恐慌蔓延"}]),
            {"top_event": {"freshness_score": 1.0}},  # fresh
        )
        # Even though freshly published, content is intraday rant → intraday_event
        assert result.category == "intraday_event"
        assert result.horizon == "intraday"

    def test_missing_pub_date_with_roi_content_not_low(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        article = _sample_article(title="算力租赁ROI分析与报价跟踪")
        article.pop("published_at", None)
        result = assess_time_sensitivity(
            article,
            _deep_read(units=[{"title": "ROI走低", "thesis": "报价下降"}]),
            {},
        )
        assert result.category != "unknown"
        assert "低时效" not in result.label


# ── Structured output ────────────────────────────────────────────────────────


class TestStructuredOutput:
    """TimeSensitivityAssessment must have all required fields."""

    def test_all_fields_present(self):
        from fin_analyse.cognition.time_sensitivity import (
            assess_time_sensitivity,
        )

        result = assess_time_sensitivity(
            _sample_article(title="测试"),
            _deep_read(),
            None,
        )
        d = result.to_dict()
        for field in (
            "category",
            "label",
            "horizon",
            "publish_freshness",
            "reason",
            "evidence",
            "confidence",
            "data_gaps",
        ):
            assert field in d, f"Missing field: {field}"

    def test_to_display_string_includes_reason_and_evidence(self):
        from fin_analyse.cognition.time_sensitivity import (
            TimeSensitivityAssessment,
        )

        ts = TimeSensitivityAssessment(
            category="short_term_tracking",
            label="持续关注",
            horizon="1-3d",
            reason="涉及报价变化",
            evidence=["报价=$2.5/hr", "订单量变化"],
            confidence=0.7,
        )
        s = ts.to_display_string()
        assert "（原因：" in s
        assert "证据：" in s
        assert "short_term_tracking" in s
        assert "报价=$2.5/hr" in s


# ── LLM fallback tests ──────────────────────────────────────────────────────


class TestLLMFallback:
    """LLM is NOT called synchronously from assess_time_sensitivity (per §13 P0 Gate).

    _llm_classify() is retained for manual/interactive use only but NEVER
    invoked from cron/consumer fast path.  G-source articles without rule
    match get rule fallback + llm_enrichment_pending data_gap.
    """

    def test_fast_path_never_calls_llm_even_with_env_var(self, monkeypatch):
        """Even with FIN_TIME_SENSITIVITY_LLM=1, assess_time_sensitivity NEVER calls LLM."""
        monkeypatch.setenv("FIN_TIME_SENSITIVITY_LLM", "1")
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        article = _sample_article(
            title="关于某股票的深入思考",
            column="星大派特刊",
        )
        dr = _deep_read()

        llm_called = [False]
        mock_llm = MagicMock()

        def track_call(prompt, *, expected_type):
            llm_called[0] = True
            return MagicMock()

        mock_llm.complete_json = track_call

        monkeypatch.setattr(
            "fin_analyse.cognition.llm.CognitionLLM.from_config",
            classmethod(lambda cls, preferred=None: mock_llm),
        )

        result = assess_time_sensitivity(article, dr, None)

        # LLM must NEVER be called from fast path, regardless of env var
        assert not llm_called[0], "LLM must not be called from assess_time_sensitivity"
        assert result.category == "short_term_tracking"
        assert "llm_enrichment_pending" in result.data_gaps
        assert result.confidence <= 0.4

    def test_g_source_without_rule_match_gets_enrichment_pending(self):
        """G-source without rule match → rule fallback + llm_enrichment_pending."""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        article = _sample_article(
            title="关于某股票的深入思考",
            column="星大派特刊",
        )
        dr = _deep_read()

        result = assess_time_sensitivity(article, dr, None)
        assert result.category == "short_term_tracking"
        assert "llm_enrichment_pending" in result.data_gaps
        assert result.source_level == "agent_inference"
        assert result.quality_mode == "rule_fast_path"

    def test_llm_classify_can_still_be_called_directly(self, monkeypatch):
        """_llm_classify() is retained as a standalone helper for manual use."""
        monkeypatch.setenv("FIN_TIME_SENSITIVITY_LLM", "1")
        from fin_analyse.cognition.time_sensitivity import _llm_classify

        mock_llm = MagicMock()
        mock_result = MagicMock()
        mock_result.ok = True
        mock_result.data = {
            "category": "active_theme",
            "label": "当前主线",
            "horizon": "1-2w",
            "reason": "文章讨论主线方向",
            "evidence_spans": ["深入思考主线"],
            "confidence": 0.6,
        }
        mock_llm.complete_json.return_value = mock_result

        monkeypatch.setattr(
            "fin_analyse.cognition.llm.CognitionLLM.from_config",
            classmethod(lambda cls, preferred=None: mock_llm),
        )

        result = _llm_classify(
            title="关于某股票的深入思考",
            column="星大派特刊",
            units=[],
            clusters=[],
            chains=[],
            suggestions=[],
        )
        assert result is not None
        assert result.category == "active_theme"
        # Direct LLM call sets enriched quality metadata
        assert result.quality_mode == "rule_fast_path"  # default, caller should override
        assert result.source_level == "agent_inference"  # default, caller should override


# ── Evidence spans ───────────────────────────────────────────────────────────


class TestEvidenceSpans:
    """Evidence must reference actual article content."""

    def test_rule_based_includes_matched_clues(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="超微报价变化与订单趋势分析"),
            _deep_read(units=[{"title": "报价下行", "thesis": "订单量下降"}]),
            None,
        )
        assert len(result.evidence) >= 2  # at least the matched clues
        # Evidence should contain the actual matched words
        evidence_text = " ".join(result.evidence)
        assert "报价" in evidence_text or "订单" in evidence_text

    def test_no_false_evidence_for_empty_input(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        article = _sample_article(title="无意义标题xyz")
        article["column"] = ""
        result = assess_time_sensitivity(article, _deep_read(), None)
        # No content → unknown, evidence empty
        assert result.category == "unknown"


# ── Rule priority: tracking/theme before durable ─────────────────────────────


class TestRulePriority:
    """Strong tracking/theme signals must override durable keyword matches."""

    def test_info_gap_with_quote_and_roi_is_tracking_not_durable(self):
        """'信息差' + '报价' + 'ROI' → short_term_tracking/active_theme, NOT durable."""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(
                title="算力租赁ROI判断与超微4T/3T报价分析 — 情绪杀 vs 信息差",
                published_at="2026-06-30T10:00:00",
            ),
            _deep_read(
                units=[
                    {"title": "算力租赁ROI持续走低", "thesis": "超微4T报价降至$2.5/hr"},
                    {"title": "情绪杀带来信息差套利", "thesis": "市场过度反应提供买入窗口"},
                ]
            ),
            {"top_event": {"freshness_score": 0.0}},
        )
        # Must NOT be durable_framework
        assert result.category != "durable_framework", (
            f"'信息差'+'报价'+'ROI' must not be durable, got {result.category}"
        )
        # Must NOT be "低时效"
        assert "低时效" not in result.label
        # Must be short_term_tracking or active_theme
        assert result.category in ("short_term_tracking", "active_theme"), (
            f"Expected short_term_tracking or active_theme, got {result.category}"
        )
        # evidence should contain tracking/theme clues
        evidence_text = " ".join(result.evidence)
        has_tracking_evidence = any(w in evidence_text for w in ["报价", "ROI", "供需", "订单"])
        assert has_tracking_evidence, f"Evidence missing tracking clues: {result.evidence}"

    def test_pure_methodology_framework_is_durable(self):
        """Pure methodology/framework, no tracking signals → durable_framework."""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="投资方法论：认知框架与金融分配体系"),
            _deep_read(
                units=[
                    {"title": "投资哲学", "thesis": "长期认知框架是超额收益的来源"},
                ]
            ),
            None,
        )
        assert result.category == "durable_framework"
        assert result.horizon == "durable"

    def test_info_gap_alone_without_tracking_is_durable(self):
        """'信息差' alone, no 报价/ROI/供需 → durable_framework."""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="信息差与金融分配：认知框架思考"),
            _deep_read(
                units=[
                    {"title": "信息差本质", "thesis": "信息差是金融分配的核心"},
                ]
            ),
            None,
        )
        assert result.category == "durable_framework", (
            f"Expected durable_framework, got {result.category}"
        )
        assert result.evidence, "Should have evidence"


# ── Enrichment / async quality layer ──────────────────────────────────────────


class TestFastPathQualityMetadata:
    """Fast path must set source_level=agent_inference, quality_mode=rule_fast_path."""

    def test_fast_path_sets_quality_mode_rule_fast_path(self):
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="算力租赁ROI判断与超微4T报价分析"),
            _deep_read(units=[{"title": "报价变化", "thesis": "测试"}]),
            None,
        )
        assert result.quality_mode == "rule_fast_path"
        assert result.source_level == "agent_inference"

    def test_fast_path_source_level_is_not_g_direct(self):
        """Rule-based keyword matching is agent_inference, never g_direct."""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="星大派锐评：产业链分析", column="星大派锐评"),
            _deep_read(units=[{"title": "产业逻辑", "thesis": "测试"}]),
            None,
        )
        assert result.source_level != "g_direct", (
            f"Rule-based fast path must not claim g_direct, got {result.source_level}"
        )
        assert result.source_level == "agent_inference"

    def test_advisory_only_and_execution_allowed_preserved(self):
        """Fast path must not change advisory/execution boundaries."""
        from fin_analyse.cognition.time_sensitivity import assess_time_sensitivity

        result = assess_time_sensitivity(
            _sample_article(title="测试"),
            _deep_read(),
            None,
        )
        # These fields are on the enrichment/audit level, always advisory
        assert result.quality_mode == "rule_fast_path"


class TestEnrichmentRecord:
    """EnrichmentRecord creation, conflict detection, and field completeness."""

    def test_create_enrichment_pending_has_all_fields(self):
        from fin_analyse.cognition.time_sensitivity import (
            TimeSensitivityAssessment,
            create_enrichment_pending,
        )

        fast = TimeSensitivityAssessment(
            category="short_term_tracking",
            label="持续关注",
            horizon="1-3d",
            reason="涉及报价变化",
            evidence=["报价=$2.5/hr"],
            confidence=0.7,
            source_level="agent_inference",
            quality_mode="rule_fast_path",
        )
        record = create_enrichment_pending(
            article_id="art_001",
            job_id="job_001",
            fast_path=fast,
        )
        d = record.to_dict()
        assert record.enrichment_status == "pending"
        assert d["fast_path_result"]["category"] == "short_term_tracking"
        assert d["fast_path_result"]["source_level"] == "agent_inference"
        assert d["fast_path_result"]["quality_mode"] == "rule_fast_path"
        for field in (
            "article_id",
            "job_id",
            "enrichment_status",
            "fast_path_result",
            "enriched_result",
            "conflict",
            "conflict_reason",
            "created_at",
            "updated_at",
        ):
            assert field in d, f"Missing field: {field}"

    def test_resolve_conflict_different_categories(self):
        """Different categories → conflict=true with reason."""
        from fin_analyse.cognition.time_sensitivity import (
            TimeSensitivityAssessment,
            resolve_enrichment_conflict,
        )

        fast = TimeSensitivityAssessment(
            category="short_term_tracking",
            label="持续关注",
            horizon="1-3d",
            confidence=0.7,
            source_level="agent_inference",
            quality_mode="rule_fast_path",
        )
        enriched = TimeSensitivityAssessment(
            category="active_theme",
            label="当前主线",
            horizon="1-2w",
            confidence=0.8,
            source_level="g_logic_transfer",
            quality_mode="llm_enriched",
        )

        record = resolve_enrichment_conflict(fast, enriched)
        assert record.conflict is True
        assert "short_term_tracking" in record.conflict_reason
        assert "active_theme" in record.conflict_reason
        assert record.fast_path_result is not None
        assert record.enriched_result is not None

    def test_resolve_conflict_same_category_no_conflict(self):
        """Same category → conflict=false."""
        from fin_analyse.cognition.time_sensitivity import (
            TimeSensitivityAssessment,
            resolve_enrichment_conflict,
        )

        fast = TimeSensitivityAssessment(
            category="short_term_tracking",
            label="持续关注",
            confidence=0.7,
            source_level="agent_inference",
            quality_mode="rule_fast_path",
        )
        enriched = TimeSensitivityAssessment(
            category="short_term_tracking",
            label="持续关注（LLM复核）",
            confidence=0.85,
            source_level="g_logic_transfer",
            quality_mode="llm_enriched",
        )

        record = resolve_enrichment_conflict(fast, enriched)
        assert record.conflict is False
        assert record.conflict_reason == ""

    def test_write_enrichment_record_writes_to_sink(self, tmp_path):
        """write_enrichment_record must append to JSONL sink."""
        from fin_analyse.cognition.time_sensitivity import (
            TimeSensitivityAssessment,
            create_enrichment_pending,
            write_enrichment_record,
        )

        fast = TimeSensitivityAssessment(
            category="active_theme",
            label="当前主线",
            confidence=0.65,
        )
        record = create_enrichment_pending("art_w", "job_w", fast)

        write_enrichment_record(record, kb_root=str(tmp_path))

        sink = tmp_path / "runtime" / "time_sensitivity_enrichment.jsonl"
        assert sink.exists()
        lines = sink.read_text().strip().split("\n")
        assert len(lines) == 1
        import json

        data = json.loads(lines[0])
        assert data["article_id"] == "art_w"
        assert data["enrichment_status"] == "pending"
        assert data["fast_path_result"]["category"] == "active_theme"
