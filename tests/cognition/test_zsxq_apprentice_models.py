from fin_analyse.cognition.models import (
    DynamicClock,
    EvidenceChain,
    InformationUnit,
    InvestmentResearchSuggestion,
    ThemeCluster,
    UsagePolicy,
    ZsxqApprenticeResult,
    ZsxqCognitionSource,
)


def test_information_unit_round_trip_keeps_usage_policy():
    unit = InformationUnit(
        unit_id="unit-1",
        source_id="src-1",
        teacher_id="guo",
        unit_type="strategic_thesis",
        title="半导体去日化",
        thesis="半导体国产替代从泛泛去美化进入具体去日化阶段。",
        original_evidence=["去日化是国产化2.0的核心路径"],
        apprentice_interpretation="这是认知学徒翻译，不是老师原话。",
        confidence=0.82,
        related_companies=["芯源微"],
        related_topics=["半导体", "去日化"],
        theme_cluster_ids=["cluster-semi-materials"],
        usage_policy=UsagePolicy(
            allowed_usage=["daily_key_interpretation", "research_tracking"],
            forbidden_usage=["direct_buy_signal", "automatic_position_change"],
        ),
        created_at="2026-06-24T00:00:00",
        metadata={"sample": True},
    )

    restored = InformationUnit.from_dict(unit.to_dict())

    assert restored == unit
    assert "direct_buy_signal" in restored.usage_policy.forbidden_usage


def test_result_round_trip_preserves_nested_records():
    source = ZsxqCognitionSource(
        source_id="src-1",
        article_path="knowledge-base/articles/demo.md",
        article_id="demo",
        topic_id="topic-1",
        published_at="2026-06-18 20:00",
        column="星大派特刊",
        title="星大派特刊：半导体AI卡脖子材料",
        content="钼前驱体最具性价比。",
        image_descriptions=["表格：钼前驱体评分最高"],
        image_ocr=["钼前驱体 14.5"],
        source_rank="t0_xingdapai",
        completeness="full",
        metadata={"score": None},
    )
    unit = InformationUnit(
        unit_id="unit-1",
        source_id="src-1",
        teacher_id="guo",
        unit_type="industry_map",
        title="AI卡脖子材料排序",
        thesis="钼前驱体、稀土氧化物、Niche前驱体、WF6 需要分层跟踪。",
        original_evidence=["钼前驱体总分14.5"],
        apprentice_interpretation="材料卡口是AI半导体主线的底层延伸。",
        confidence=0.8,
        related_companies=[],
        related_topics=["AI硬科技材料"],
        theme_cluster_ids=["cluster-semi-materials"],
        usage_policy=UsagePolicy.default_research_policy(),
        created_at="2026-06-24T00:00:00",
        metadata={},
    )
    chain = EvidenceChain(
        chain_id="chain-1",
        unit_id="unit-1",
        original_claims=["钼前驱体总分14.5"],
        original_source_refs=["knowledge-base/articles/demo.md"],
        apprentice_inferences=["这是低关注高壁垒方向"],
        inference_confidence=0.72,
        external_validations=[],
        counter_evidence=[],
        source_boundary_notes=["学徒推演不是老师原话"],
    )
    cluster = ThemeCluster(
        cluster_id="cluster-semi-materials",
        name="半导体底层卡口 / AI硬科技材料 / 去日化",
        description="6月星大派连续强化的半导体材料与去日化主题簇。",
        teacher_id="guo",
        unit_ids=["unit-1"],
        source_ids=["src-1"],
        core_theses=[unit.thesis],
        active_status="new",
        priority=0.8,
        last_reinforced_at="2026-06-24T00:00:00",
        tracking_indicators=["订单", "涨价", "认证"],
        risks=["股价透支"],
        metadata={},
    )
    clock = DynamicClock(
        unit_id="unit-1",
        state="fresh",
        observed_at="2026-06-24T00:00:00",
        base_half_life_days=60.0,
        effective_until=None,
        freshness_score=0.8,
        upgrade_triggers=["老师后续提及"],
        downgrade_triggers=["公司澄清"],
        reset_triggers=["订单验证"],
        last_evaluated_at="2026-06-24T00:00:00",
        reason="战略/产业图谱类信息仍新鲜",
    )
    suggestion = InvestmentResearchSuggestion(
        suggestion_id="sug-1",
        unit_id="unit-1",
        suggestion_level="research_candidate",
        summary="进入研究候选，不直接触发买入。",
        upgrade_conditions=["公告验证"],
        downgrade_conditions=["无真实产能"],
        tracking_indicators=["客户认证"],
        risk_boundaries=["不追高"],
        allowed_usage=["research_tracking"],
        forbidden_usage=["direct_buy_signal", "automatic_position_change"],
        confidence=0.74,
    )
    result = ZsxqApprenticeResult(
        source=source,
        units=[unit],
        evidence_chains=[chain],
        theme_clusters=[cluster],
        clocks=[clock],
        suggestions=[suggestion],
        warnings=[],
    )

    restored = ZsxqApprenticeResult.from_dict(result.to_dict())

    assert restored == result
    assert restored.source.source_rank == "t0_xingdapai"


def test_public_imports_are_available():
    from fin_analyse.cognition import InformationUnit, ZsxqCognitionApprentice

    assert InformationUnit.__name__ == "InformationUnit"
    assert ZsxqCognitionApprentice.__name__ == "ZsxqCognitionApprentice"
