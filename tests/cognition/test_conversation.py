from fin_analyse.cognition.conversation import ConversationRequest, ConversationResponse
from fin_analyse.context.models import ContextRequestScope


def test_conversation_request_reuses_context_scope_metadata():
    scope = ContextRequestScope(
        tenant_id="team-a",
        user_id="u123",
        platform="qq",
        conversation_id="group456",
        visibility="shared",
    )
    request = ConversationRequest(
        scope=scope,
        message_id="msg789",
        text="帮我看一下贵州茅台",
        teacher_id="guo",
        company="贵州茅台",
        ticker="600519",
    )

    metadata = request.to_metadata(context_type="conversation", request_id="req-1")

    assert metadata == {
        "context_type": "conversation",
        "platform": "qq",
        "tenant_id": "team-a",
        "user_id": "u123",
        "conversation_id": "group456",
        "visibility": "shared",
        "message_id": "msg789",
        "request_id": "req-1",
        "company": "贵州茅台",
        "ticker": "600519",
        "teacher_id": "guo",
    }


def test_conversation_request_defaults_to_platform_agnostic_scope():
    request = ConversationRequest(message_id="m1", text="怎么看这个票？")

    assert request.scope.platform == "local"
    assert request.scope.tenant_id == "default"
    assert request.scope.user_id == "default"
    assert request.teacher_id == "guo"


def test_conversation_response_carries_analysis_and_warnings():
    response = ConversationResponse(
        text="结论：关注但不追高",
        analysis_id="pa-1",
        confidence=0.62,
        warnings=["缺少近期外部上下文"],
        needs_human_review=True,
    )

    assert response.analysis_id == "pa-1"
    assert response.confidence == 0.62
    assert response.warnings == ["缺少近期外部上下文"]
    assert response.needs_human_review is True
