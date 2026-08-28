from typing import Literal

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Enum, Integer, String, Text

from yuxi.product_chat.schemas import (
    CitationResponse,
    ConversationListResponse,
    ConversationResponse,
    ConversationDetailResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    MessageExchangeResponse,
    MessageFeedbackRequest,
    MessageFeedbackResponse,
    MessageResponse,
    ProductUserResponse,
    SendMessageRequest,
    SessionResponse,
)
from yuxi.storage.postgres.models_business import Base as BusinessBase
from yuxi.storage.postgres.models_product import (
    AnswerStatus,
    AuthorizationStatus,
    CitationKind,
    ConversationStatus,
    FeishuDepartmentBinding,
    FeishuUserBinding,
    FeishuUserDepartmentMembership,
    MessageCitation,
    MessageRole,
    ProductConversation,
    ProductMessage,
)


def _unique_column_names(table) -> set[str]:
    unique_columns = {column.name for column in table.columns if column.unique}
    unique_columns.update(
        next(iter(constraint.columns)).name
        for constraint in table.constraints
        if getattr(constraint, "unique", False) and len(constraint.columns) == 1
    )
    return unique_columns


def _foreign_key_target(table, column_name: str) -> str:
    return next(iter(table.c[column_name].foreign_keys)).target_fullname


def test_product_models_share_business_metadata_and_define_required_tables():
    models = (
        FeishuUserBinding,
        FeishuDepartmentBinding,
        FeishuUserDepartmentMembership,
        ProductConversation,
        ProductMessage,
        MessageCitation,
    )

    assert all(model.metadata is BusinessBase.metadata for model in models)
    assert {model.__tablename__ for model in models} == {
        "feishu_user_bindings",
        "feishu_department_bindings",
        "feishu_user_department_memberships",
        "product_conversations",
        "product_messages",
        "message_citations",
    }


def test_product_models_define_ids_uniqueness_foreign_keys_and_indexes():
    binding = FeishuUserBinding.__table__
    department_binding = FeishuDepartmentBinding.__table__
    membership = FeishuUserDepartmentMembership.__table__
    conversation = ProductConversation.__table__
    message = ProductMessage.__table__
    citation = MessageCitation.__table__

    assert set(binding.columns.keys()) == {
        "id",
        "user_id",
        "feishu_open_id",
        "feishu_user_id",
        "feishu_union_id",
        "tenant_key",
        "display_name",
        "avatar_url",
        "authorization_status",
        "last_login_at",
        "created_at",
        "updated_at",
    }
    assert set(conversation.columns.keys()) == {
        "id",
        "conversation_id",
        "owner_user_id",
        "title",
        "status",
        "created_at",
        "updated_at",
    }
    assert set(department_binding.columns.keys()) == {
        "id",
        "tenant_key",
        "feishu_department_id",
        "department_id",
        "display_name",
        "created_at",
        "updated_at",
    }
    assert set(membership.columns.keys()) == {
        "id",
        "user_id",
        "department_binding_id",
        "position",
        "created_at",
        "updated_at",
    }
    assert set(message.columns.keys()) == {
        "id",
        "message_id",
        "conversation_id",
        "role",
        "content",
        "answer_status",
        "model_version",
        "prompt_version",
        "feedback_rating",
        "created_at",
    }
    assert set(citation.columns.keys()) == {
        "id",
        "citation_id",
        "message_id",
        "kind",
        "source_id",
        "item_id",
        "version_id",
        "yuxi_file_id",
        "title",
        "source_url",
        "path_text",
        "locator",
        "excerpt",
        "source_version_at",
        "created_at",
    }

    assert isinstance(binding.c.id.type, Integer)
    assert isinstance(conversation.c.id.type, Integer)
    assert isinstance(message.c.id.type, Integer)
    assert isinstance(citation.c.id.type, Integer)

    for column in (conversation.c.conversation_id, message.c.message_id, citation.c.citation_id):
        assert isinstance(column.type, String)
        assert column.type.length == 26
        assert column.default is not None and callable(column.default.arg)
        generated_id = column.default.arg(None)
        assert len(generated_id) == 26
        assert set(generated_id) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")

    assert {"user_id", "feishu_open_id", "feishu_user_id"} <= _unique_column_names(binding)
    assert "conversation_id" in _unique_column_names(conversation)
    assert "message_id" in _unique_column_names(message)
    assert "citation_id" in _unique_column_names(citation)

    assert _foreign_key_target(binding, "user_id") == "users.id"
    assert _foreign_key_target(department_binding, "department_id") == "departments.id"
    assert _foreign_key_target(membership, "user_id") == "users.id"
    assert _foreign_key_target(membership, "department_binding_id") == "feishu_department_bindings.id"
    assert _foreign_key_target(conversation, "owner_user_id") == "users.id"
    assert _foreign_key_target(message, "conversation_id") == "product_conversations.conversation_id"
    assert _foreign_key_target(citation, "message_id") == "product_messages.message_id"

    assert {index.name for index in conversation.indexes} == {"ix_product_conversations_owner_status_updated"}
    assert {index.name for index in message.indexes} == {"ix_product_messages_conversation_created"}
    assert {index.name for index in citation.indexes} == {
        "ix_message_citations_message_id",
        "ix_message_citations_version_id",
    }

    timestamp_columns = (
        binding.c.created_at,
        binding.c.updated_at,
        conversation.c.created_at,
        conversation.c.updated_at,
        message.c.created_at,
        citation.c.created_at,
    )
    assert all(column.default.arg.__name__ == "utc_now_naive" for column in timestamp_columns)


def test_message_citation_locator_is_stored_as_text():
    assert isinstance(MessageCitation.__table__.c.locator.type, Text)


def test_product_models_define_stable_enums_and_message_constraints():
    assert [status.value for status in AuthorizationStatus] == ["ACTIVE", "REVOKED"]
    assert [status.value for status in ConversationStatus] == ["ACTIVE", "ARCHIVED"]
    assert [role.value for role in MessageRole] == ["USER", "ASSISTANT"]
    assert [status.value for status in AnswerStatus] == ["SUPPORTED", "INSUFFICIENT", "CONFLICTING"]
    assert [kind.value for kind in CitationKind] == ["ENTERPRISE_EVIDENCE"]

    answer_type = ProductMessage.__table__.c.answer_status.type
    assert isinstance(answer_type, Enum)
    assert answer_type.enums == ["SUPPORTED", "INSUFFICIENT", "CONFLICTING"]

    constraints = "\n".join(
        str(constraint.sqltext)
        for constraint in ProductMessage.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "role != 'USER'" in constraints
    assert "answer_status IS NULL" in constraints
    assert "model_version IS NULL" in constraints
    assert "prompt_version IS NULL" in constraints
    assert "role != 'ASSISTANT'" in constraints
    assert "answer_status IS NOT NULL" in constraints
    assert "feedback_rating IS NULL" in constraints
    assert "LIKE" in constraints
    assert "DISLIKE" in constraints


def test_product_request_schemas_reject_extra_fields_and_enforce_message_length():
    with pytest.raises(ValidationError):
        SendMessageRequest(content="hello", unexpected=True)

    with pytest.raises(ValidationError):
        SendMessageRequest(content="")

    with pytest.raises(ValidationError):
        SendMessageRequest(content="x" * 20_001)

    assert CreateConversationRequest(title=None).title is None
    with pytest.raises(ValidationError):
        CreateConversationRequest(title="x" * 81)

    assert MessageFeedbackRequest(rating="LIKE").rating == "LIKE"
    assert MessageFeedbackRequest(rating=None).rating is None
    with pytest.raises(ValidationError):
        MessageFeedbackRequest(rating="OTHER")


def test_product_response_contract_uses_literals_and_string_timestamps():
    assert ConversationSummaryResponse.model_fields["status"].annotation == Literal["ACTIVE", "ARCHIVED"]
    assert CitationResponse.model_fields["kind"].annotation == Literal["ENTERPRISE_EVIDENCE"]
    assert MessageResponse.model_fields["role"].annotation == Literal["USER", "ASSISTANT"]
    assert MessageResponse.model_fields["answer_status"].annotation == (
        Literal["SUPPORTED", "INSUFFICIENT", "CONFLICTING"] | None
    )
    assert MessageResponse.model_fields["feedback_rating"].annotation == (Literal["LIKE", "DISLIKE"] | None)
    assert MessageFeedbackResponse.model_fields["feedback_rating"].annotation == (Literal["LIKE", "DISLIKE"] | None)
    assert ConversationSummaryResponse.model_fields["created_at"].annotation is str
    assert ConversationSummaryResponse.model_fields["updated_at"].annotation is str
    assert MessageResponse.model_fields["created_at"].annotation is str
    assert CitationResponse.model_fields["version_at"].annotation == (str | None)


def test_product_user_response_allows_omitting_avatar_url():
    user = ProductUserResponse(id="user-1", name="Yuxi")

    assert user.avatar_url is None


def test_product_response_schemas_accept_snake_case_and_serialize_camel_case():
    created_at = "2026-08-16T09:30:00Z"
    conversation = ConversationSummaryResponse(
        id="01K2V7RM06QJ1H1W6EJ8JECMKT",
        title="Enterprise answer",
        status="ACTIVE",
        message_count=2,
        created_at=created_at,
        updated_at=created_at,
    )
    citation = CitationResponse(
        id="01K2V7RM06QJ1H1W6EJ8JECMKV",
        kind="ENTERPRISE_EVIDENCE",
        title="Deployment guide",
        path=None,
        locator="page=3",
        excerpt="Use the enterprise deployment profile.",
        version_at="2026-08-15T12:00:00Z",
    )
    user_message = MessageResponse(
        id="01K2V7RM06QJ1H1W6EJ8JECMKW",
        role="USER",
        content="How do I deploy?",
        answer_status=None,
        citations=[],
        created_at=created_at,
    )
    assistant_message = MessageResponse(
        id="01K2V7RM06QJ1H1W6EJ8JECMKX",
        role="ASSISTANT",
        content="Use the enterprise deployment profile.",
        answer_status="SUPPORTED",
        citations=[citation],
        created_at=created_at,
    )

    session = SessionResponse(user=ProductUserResponse(id="user-1", name="Yuxi"))
    listing = ConversationListResponse(conversations=[conversation])
    wrapped = ConversationResponse(conversation=conversation)
    detail = ConversationDetailResponse(conversation=conversation, messages=[user_message, assistant_message])
    exchange = MessageExchangeResponse(
        conversation=conversation,
        user_message=user_message,
        assistant_message=assistant_message,
    )

    assert session.model_dump() == {"user": {"id": "user-1", "name": "Yuxi", "avatarUrl": None}}
    assert listing.model_dump()["conversations"][0]["messageCount"] == 2
    assert wrapped.model_dump()["conversation"]["status"] == "ACTIVE"
    assert wrapped.model_dump()["conversation"]["createdAt"] == created_at
    assert detail.model_dump()["conversation"]["messageCount"] == 2
    assert exchange.model_dump()["userMessage"]["answerStatus"] is None
    serialized_citation = exchange.model_dump()["assistantMessage"]["citations"][0]
    assert serialized_citation["locator"] == "page=3"
    assert serialized_citation["versionAt"] == "2026-08-15T12:00:00Z"
