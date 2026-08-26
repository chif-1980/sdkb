from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from yuxi.governance.domain import (
    DuplicateResolutionStrategy,
    ProblemTag,
    ReviewAction,
    ReviewDecision,
    ReviewOutcome,
)


class ApplicabilityScope(BaseModel):
    industry: str | None = Field(default=None, max_length=255)
    product: str | None = Field(default=None, max_length=255)
    product_version: str | None = Field(default=None, max_length=255)
    deployment_mode: str | None = Field(default=None, max_length=255)
    customer_type: str | None = Field(default=None, max_length=255)
    region_language: str | None = Field(default=None, max_length=255)
    effective_time: datetime | None = None

    @field_validator(
        "industry",
        "product",
        "product_version",
        "deployment_mode",
        "customer_type",
        "region_language",
    )
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ReviewResolveRequest(BaseModel):
    decision: ReviewDecision
    action: ReviewAction
    problem_tags: list[ProblemTag] = Field(default_factory=list, max_length=20)
    decision_comment: str | None = Field(default=None, max_length=4000)
    applicability_scope: ApplicabilityScope = Field(default_factory=ApplicabilityScope)
    assignee_id: str | None = Field(default=None, max_length=64)

    @field_validator("decision_comment", "assignee_id")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_decision_requirements(self):
        if self.decision == ReviewDecision.TRANSFER and not self.assignee_id:
            raise ValueError("assignee_id is required for transfer")
        if self.decision in {ReviewDecision.REQUEST_CHANGES, ReviewDecision.REJECT} and not self.decision_comment:
            raise ValueError("decision_comment is required")
        if self.decision == ReviewDecision.PUBLISH and self.action not in {
            ReviewAction.CREATE,
            ReviewAction.UPDATE,
            ReviewAction.SPLIT_BY_SCOPE,
        }:
            raise ValueError("publish requires CREATE, UPDATE or SPLIT_BY_SCOPE action")
        return self


class ReviewPackageDraftRequest(BaseModel):
    lock_version: int = Field(ge=1)
    draft: dict = Field(default_factory=dict)


class ReviewLayoutEditRequest(BaseModel):
    lock_version: int = Field(ge=1)
    block_id: str = Field(min_length=1, max_length=128)
    page_number: int = Field(ge=1)
    content: str = Field(max_length=20000)
    source_segment_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("block_id", "content")
    @classmethod
    def normalize_layout_text(cls, value: str) -> str:
        return value.strip()


class ReviewItemDecisionRequest(BaseModel):
    review_item_id: str = Field(min_length=1, max_length=64)
    outcome: ReviewOutcome
    problem_tags: list[ProblemTag] = Field(default_factory=list, max_length=20)
    decision_comment: str | None = Field(default=None, max_length=4000)
    applicability_scope: ApplicabilityScope = Field(default_factory=ApplicabilityScope)
    responsible_user_id: str | None = Field(default=None, max_length=128)
    responsible_user_name: str | None = Field(default=None, max_length=255)

    @field_validator("review_item_id")
    @classmethod
    def normalize_review_item_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("review_item_id is required")
        return normalized

    @field_validator("decision_comment", "responsible_user_id", "responsible_user_name")
    @classmethod
    def normalize_decision_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ReviewPackageResolveRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    lock_version: int = Field(ge=1)
    decisions: list[ReviewItemDecisionRequest] = Field(min_length=1, max_length=500)

    @field_validator("request_id")
    @classmethod
    def normalize_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("request_id is required")
        return normalized

    @model_validator(mode="after")
    def decisions_must_be_unique(self):
        item_ids = [decision.review_item_id for decision in self.decisions]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("review_item_id must be unique within one request")
        return self


class ReviewPackageTransferRequest(BaseModel):
    lock_version: int = Field(ge=1)
    assignee_id: str = Field(min_length=1, max_length=64)
    comment: str = Field(min_length=1, max_length=4000)

    @field_validator("assignee_id", "comment")
    @classmethod
    def normalize_transfer_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class SourceChangeRequestCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason is required")
        return normalized


class DuplicateRelationResolutionRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    strategy: DuplicateResolutionStrategy
    comment: str | None = Field(default=None, max_length=4000)

    @field_validator("request_id")
    @classmethod
    def normalize_duplicate_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("request_id is required")
        return normalized

    @field_validator("comment")
    @classmethod
    def normalize_duplicate_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None
