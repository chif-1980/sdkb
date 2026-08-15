"""Pydantic contracts for enterprise assistant product chat APIs."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from yuxi.storage.postgres.models_product import AnswerStatus, CitationKind, ConversationStatus, MessageRole


def to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class CreateConversationRequest(StrictRequest):
    title: str | None = Field(default=None, max_length=80)


class SendMessageRequest(StrictRequest):
    content: str = Field(min_length=1, max_length=20_000)


class ProductUserResponse(ProductResponse):
    id: str
    name: str
    avatar_url: str | None


class SessionResponse(ProductResponse):
    user: ProductUserResponse


class ConversationSummaryResponse(ProductResponse):
    id: str
    title: str
    status: ConversationStatus
    message_count: int
    created_at: datetime
    updated_at: datetime


class CitationResponse(ProductResponse):
    id: str
    kind: CitationKind
    title: str
    path: str | None
    locator: dict[str, object]
    excerpt: str
    version_at: datetime | None


class MessageResponse(ProductResponse):
    id: str
    role: MessageRole
    content: str
    answer_status: AnswerStatus | None
    citations: list[CitationResponse]
    created_at: datetime


class ConversationListResponse(ProductResponse):
    conversations: list[ConversationSummaryResponse]


class ConversationResponse(ProductResponse):
    conversation: ConversationSummaryResponse


class ConversationDetailResponse(ConversationResponse):
    messages: list[MessageResponse]


class MessageExchangeResponse(ConversationResponse):
    user_message: MessageResponse
    assistant_message: MessageResponse
