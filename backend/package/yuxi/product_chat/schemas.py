"""Pydantic contracts for enterprise assistant product chat APIs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class CreateConversationRequest(StrictRequest):
    title: str | None = Field(default=None, max_length=80)


ProductSkillId = Literal["MATERIAL_SEARCH", "SOLUTION_DRAFT", "MEETING_ANALYSIS"]


class SendMessageRequest(StrictRequest):
    content: str = Field(min_length=1, max_length=20_000)
    mode: Literal["CONCISE", "DETAILED"] = "CONCISE"
    skill_id: ProductSkillId | None = Field(default=None, alias="skillId")


class MessageFeedbackRequest(StrictRequest):
    rating: Literal["LIKE", "DISLIKE"] | None
    reason_type: Literal[
        "CONTENT_ERROR",
        "OUTDATED",
        "MISSING_SOURCE",
        "CITATION_ERROR",
        "OTHER",
    ] | None = Field(default=None, alias="reasonType")
    reason_text: str | None = Field(default=None, max_length=500, alias="reasonText")


class ProductUserResponse(ProductResponse):
    id: str
    name: str
    avatar_url: str | None = None


class SessionResponse(ProductResponse):
    user: ProductUserResponse


class FeishuQrLoginConfigResponse(ProductResponse):
    goto: str
    expires_in: int


class ConversationSummaryResponse(ProductResponse):
    id: str
    title: str
    status: Literal["ACTIVE", "ARCHIVED"]
    message_count: int
    created_at: str
    updated_at: str


class CitationResponse(ProductResponse):
    id: str
    kind: Literal["ENTERPRISE_EVIDENCE"]
    title: str
    path: str | None
    locator: str
    excerpt: str
    version_at: str | None
    media_type: Literal["IMAGE"] | None = None
    image_url: str | None = None
    preview_url: str | None = None
    image_alt: str | None = None


class ProductMaterialResponse(ProductResponse):
    """A complete, currently published Feishu material returned by chat."""

    id: str
    title: str
    type: str
    file_name: str
    mime_type: str
    size_bytes: int
    updated_at: str
    summary: str
    status: Literal["APPROVED", "PUBLISHED"]
    approval_status: Literal["APPROVED"]
    publication_status: Literal["PUBLISHED"]
    citation: CitationResponse


class MessageResponse(ProductResponse):
    id: str
    role: Literal["USER", "ASSISTANT"]
    content: str
    answer_status: Literal["SUPPORTED", "INSUFFICIENT", "CONFLICTING"] | None
    feedback_rating: Literal["LIKE", "DISLIKE"] | None = None
    feedback_reason_type: str | None = None
    feedback_reason_text: str | None = None
    citations: list[CitationResponse]
    materials: list[ProductMaterialResponse] = Field(default_factory=list)
    created_at: str


class MessageFeedbackResponse(ProductResponse):
    message_id: str
    feedback_rating: Literal["LIKE", "DISLIKE"] | None
    feedback_reason_type: str | None = None
    feedback_reason_text: str | None = None


class ConversationListResponse(ProductResponse):
    conversations: list[ConversationSummaryResponse]


class ConversationResponse(ProductResponse):
    conversation: ConversationSummaryResponse


class ConversationDetailResponse(ConversationResponse):
    messages: list[MessageResponse]


class MessageExchangeResponse(ConversationResponse):
    user_message: MessageResponse
    assistant_message: MessageResponse


MaterialShareChannel = Literal["WECHAT", "FEISHU", "DINGTALK"]
MaterialDistributionStatus = Literal["READY", "DISPATCHED", "FAILED", "CANCELLED"]


class MaterialDistributionRequest(StrictRequest):
    channel: MaterialShareChannel


class MaterialDistributionTaskResponse(ProductResponse):
    id: str
    material_id: str
    requester_id: str
    channel: MaterialShareChannel
    mode: Literal["DEVICE_SHARE"]
    status: MaterialDistributionStatus
    created_at: str


class MaterialDistributionResponse(ProductResponse):
    distribution: MaterialDistributionTaskResponse
    title: str
    text: str
    download_url: str
    requires_user_confirmation: Literal[True]
