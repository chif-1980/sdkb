from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from yuxi.governance.domain import ProblemTag, ReviewAction, ReviewDecision


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
