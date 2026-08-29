from fin_analyse.context.models import (
    ContextRequestScope,
    ExternalContextBundle,
    ExternalContextRecord,
)


def test_context_request_scope_defaults_are_platform_agnostic():
    scope = ContextRequestScope()

    assert scope.tenant_id == "default"
    assert scope.user_id == "default"
    assert scope.platform == "local"
    assert scope.conversation_id == ""
    assert scope.visibility == "private"


def test_context_request_scope_can_represent_feishu_group_user():
    scope = ContextRequestScope(
        tenant_id="team-a",
        user_id="u123",
        platform="feishu",
        conversation_id="chat456",
        visibility="shared",
    )

    assert scope.tenant_id == "team-a"
    assert scope.user_id == "u123"
    assert scope.platform == "feishu"
    assert scope.conversation_id == "chat456"
    assert scope.visibility == "shared"


def test_external_context_record_defaults_are_reference_only():
    record = ExternalContextRecord(
        record_id="dragon_tiger:600519:2026-06-23",
        source="eastmoney",
        category="event",
        ticker="600519",
        title="贵州茅台龙虎榜",
        summary="日涨幅偏离值达7%",
        occurred_at="2026-06-23",
    )

    assert record.is_decision_factor is False
    assert record.importance == 0.5
    assert record.url == ""
    assert record.metadata == {}
    assert record.raw == {}


def test_external_context_bundle_warns_reference_only():
    record = ExternalContextRecord(
        record_id="research:600519:r1",
        source="eastmoney_report",
        category="research",
        ticker="600519",
        title="贵州茅台深度报告",
        summary="维持买入评级",
        occurred_at="2026-06-23",
    )
    bundle = ExternalContextBundle(ticker="600519", records=[record], warnings=["sample warning"])

    assert bundle.ticker == "600519"
    assert bundle.records == [record]
    assert bundle.warnings == ["sample warning"]
    assert bundle.reference_only is True
