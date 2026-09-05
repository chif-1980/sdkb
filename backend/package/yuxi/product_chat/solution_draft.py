"""Contracts and deterministic quality checks for Agentic solution drafts.

The runtime intentionally stores the model payload as JSON while validating it
at the boundary.  This keeps versions immutable and lets the product adapter
evolve its presentation without introducing a second orchestration model.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SolutionDraftStatus(StrEnum):
    GENERATING = "GENERATING"
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"
    SUPERSEDED = "SUPERSEDED"


class DraftCitation(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str = Field(min_length=1)
    title: str = ""
    locator: str = ""
    excerpt: str = ""
    source_url: str | None = Field(default=None, alias="sourceUrl")


class DraftRequirement(BaseModel):
    """A requirement tracked through sections and evidence."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source: str | None = None


class DraftSection(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content_markdown: str = Field(default="", alias="contentMarkdown")
    requirement_ids: list[str] = Field(default_factory=list, alias="requirementIds")
    citation_ids: list[str] = Field(default_factory=list, alias="citationIds")


class CapabilityMatch(BaseModel):
    """A requirement-to-enterprise-capability match.

    Capability ids are resolved from the governed catalog (or explicitly
    marked UNKNOWN); the model is not allowed to invent an enterprise
    capability and present it as productized.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    requirement_id: str = Field(default="", alias="requirementId")
    capability_id: str = Field(default="", alias="capabilityId")
    capability_name: str = Field(default="", alias="capabilityName")
    delivery_status: str = Field(default="UNKNOWN", alias="deliveryStatus")
    match_type: str = Field(default="UNKNOWN", alias="matchType")
    match_score: float = Field(default=0, ge=0, le=1, alias="matchScore")
    confidence: float = Field(default=0, ge=0, le=1)
    citation_ids: list[str] = Field(default_factory=list, alias="citationIds")
    limitations: list[str] = Field(default_factory=list)
    review_required: bool = Field(default=True, alias="reviewRequired")


class EvidenceItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str = Field(min_length=1)
    source_type: str = Field(default="ENTERPRISE_FORMAL", alias="sourceType")
    title: str = ""
    locator: str = ""
    excerpt: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    citation_id: str | None = Field(default=None, alias="citationId")


class ConfidenceSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    enterprise_coverage: float = Field(default=0, ge=0, le=1, alias="enterpriseCoverage")
    evidence_coverage: float = Field(default=0, ge=0, le=1, alias="evidenceCoverage")
    industry_reference_ratio: float = Field(default=0, ge=0, le=1, alias="industryReferenceRatio")
    innovation_ratio: float = Field(default=0, ge=0, le=1, alias="innovationRatio")
    notes: list[str] = Field(default_factory=list)


class ReviewState(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status: str = "NOT_REQUIRED"
    pending_items: list[str] = Field(default_factory=list, alias="pendingItems")
    required_roles: list[str] = Field(default_factory=list, alias="requiredRoles")
    decisions: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("decisions", mode="before")
    @classmethod
    def normalize_decisions(cls, value: Any) -> Any:
        """Accept concise string decisions emitted by older/model clients.

        The public contract keeps decisions structured, but models may emit a
        plain list of review notes. Wrapping those notes preserves the text
        while allowing the result to pass schema validation and be reviewed.
        """
        if not isinstance(value, list):
            return value
        return [
            {"decision": item}
            if isinstance(item, str)
            else item
            for item in value
        ]


class ConflictAlternative(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    statement: str = ""
    applicability: dict[str, str] = Field(default_factory=dict)
    citation_ids: list[str] = Field(default_factory=list, alias="citationIds")


class ConflictItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    claim: str = ""
    alternatives: list[ConflictAlternative] = Field(default_factory=list)
    applicability: str = ""
    citation_ids: list[str] = Field(default_factory=list, alias="citationIds")
    status: str = "UNRESOLVED"


class SolutionDraftQuality(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status: SolutionDraftStatus
    evidence_coverage: float = Field(default=0, ge=0, le=1, alias="evidenceCoverage")
    missing_sections: list[str] = Field(default_factory=list, alias="missingSections")
    invalid_citations: list[str] = Field(default_factory=list, alias="invalidCitations")
    notes: list[str] = Field(default_factory=list)

    @field_validator("evidence_coverage", mode="before")
    @classmethod
    def normalize_evidence_coverage(cls, value: Any) -> Any:
        """Accept both the API ratio (0..1) and model-friendly percentage (0..100)."""
        if isinstance(value, (int, float)) and 1 < value <= 100:
            return value / 100
        return value


class SolutionDraftPayload(BaseModel):
    """Stable structured output emitted by the solution-draft skill."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title: str = "方案草稿"
    customer: str = ""
    customer_context: str = Field(default="", alias="customerContext")
    executive_summary: str = Field(default="", alias="executiveSummary")
    requirements: list[DraftRequirement] = Field(default_factory=list)
    sections: list[DraftSection] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list, alias="openQuestions")
    risks: list[str] = Field(default_factory=list)
    conflicts: list[ConflictItem] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list, alias="evidenceGaps")
    citations: list[DraftCitation] = Field(default_factory=list)
    capability_matches: list[CapabilityMatch] = Field(default_factory=list, alias="capabilityMatches")
    architecture: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    confidence_summary: ConfidenceSummary = Field(default_factory=ConfidenceSummary, alias="confidenceSummary")
    review: ReviewState = Field(default_factory=ReviewState)
    execution_trace: dict[str, Any] = Field(default_factory=dict, alias="executionTrace")
    quality: SolutionDraftQuality | None = None

    @field_validator("requirements", mode="before")
    @classmethod
    def normalize_requirements(cls, value: Any) -> Any:
        """Keep drafts produced by older clients compatible with the richer contract."""
        if not isinstance(value, list):
            return value
        return [
            {"id": f"REQ-{index}", "text": item}
            if isinstance(item, str)
            else item
            for index, item in enumerate(value, start=1)
        ]

    @field_validator("risks", mode="before")
    @classmethod
    def normalize_risks(cls, value: Any) -> Any:
        """Accept both the compact string contract and richer risk objects.

        Agents sometimes include a mitigation alongside a risk description.
        The public draft contract intentionally keeps risks as displayable
        strings, so normalize that richer shape without rejecting the whole
        Blueprint (or silently dropping the mitigation).
        """
        if not isinstance(value, list):
            return value
        normalized: list[str] = []
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    normalized.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            description = next(
                (
                    item.get(key)
                    for key in ("description", "risk", "claim", "title", "text")
                    if isinstance(item.get(key), str) and item.get(key, "").strip()
                ),
                None,
            )
            mitigation = item.get("mitigation")
            if isinstance(description, str):
                suffix = (
                    f"（缓解措施：{mitigation.strip()}）"
                    if isinstance(mitigation, str) and mitigation.strip()
                    else ""
                )
                normalized.append(f"{description.strip()}{suffix}")
        return normalized

    @model_validator(mode="after")
    def ensure_quality(self) -> Self:
        """Apply deterministic checks; model scores never override blockers."""
        # A missing capability lookup is a reviewable gap, not permission to
        # invent an enterprise capability.  Keep the stage visible in the
        # product card by materializing explicit UNKNOWN matches.
        if self.requirements and not self.capability_matches:
            self.capability_matches = [
                CapabilityMatch(
                    requirement_id=requirement.id,
                    capability_name="企业能力目录未返回匹配结果",
                    match_type="UNKNOWN",
                    delivery_status="UNKNOWN",
                    review_required=True,
                )
                for requirement in self.requirements
            ]
        # Older/partial model responses may put the architecture in a named
        # section while omitting the top-level field.  Preserve that evidence
        # for the structured card without creating any new claim.
        if not self.architecture:
            architecture_section = next(
                (
                    section
                    for section in self.sections
                    if "架构" in section.title
                ),
                None,
            )
            if architecture_section is not None:
                self.architecture = {
                    "overview": architecture_section.content_markdown,
                    "layers": [],
                    "sourceSectionId": architecture_section.id,
                }
        citation_ids = {item.id for item in self.citations}
        invalid = sorted(
            {item for section in self.sections for item in section.citation_ids if item not in citation_ids}
        )
        invalid.extend(
            item
            for conflict in self.conflicts
            for item in conflict.citation_ids
            if item not in citation_ids
        )
        invalid.extend(
            item
            for conflict in self.conflicts
            for alternative in conflict.alternatives
            for item in alternative.citation_ids
            if item not in citation_ids
        )
        invalid.extend(
            citation.id
            for citation in self.citations
            if not citation.locator.strip() or not citation.excerpt.strip()
        )
        invalid = sorted(set(invalid))
        required_titles = {"执行摘要", "需求与范围", "方案设计", "实施计划", "风险与待确认"}
        present_titles = {section.title.strip() for section in self.sections}
        missing = sorted(required_titles - present_titles)
        unresolved_high_risk = [item for item in self.conflicts if item.status.upper() == "UNRESOLVED"]
        has_evidence = bool(self.citations) and bool(self.sections)
        has_empty_content = not self.executive_summary.strip() or any(
            not section.content_markdown.strip() for section in self.sections
        )
        coverage = 0.0
        if has_evidence:
            linked = sum(1 for section in self.sections if section.citation_ids)
            coverage = linked / max(len(self.sections), 1)
        capability_needs_review = bool(self.requirements) and (
            not self.capability_matches
            or any(
                item.review_required or item.match_type.upper() in {"UNKNOWN", "R_AND_D", "CUSTOM"}
                for item in self.capability_matches
            )
        )
        explicit_review_required = self.review.status.upper() in {"REQUIRED", "NEEDS_REVIEW"}
        if invalid or not has_evidence or has_empty_content or unresolved_high_risk:
            status = SolutionDraftStatus.BLOCKED
        elif (
            missing
            or self.assumptions
            or self.open_questions
            or self.evidence_gaps
            or self.risks
            or capability_needs_review
            or explicit_review_required
        ):
            status = SolutionDraftStatus.NEEDS_REVIEW
        else:
            status = SolutionDraftStatus.READY
        enterprise_matches = [
            item for item in self.capability_matches
            if item.match_type.upper() == "EXISTING"
            and item.delivery_status.upper() in {"PRODUCTIZED", "DELIVERED"}
        ]
        enterprise_coverage = len(enterprise_matches) / max(len(self.capability_matches), 1)
        evidence_types = [item.source_type.upper() for item in self.evidence]
        evidence_count = max(len(evidence_types), 1)
        industry_ratio = evidence_types.count("INDUSTRY_REFERENCE") / evidence_count
        innovation_ratio = evidence_types.count("INNOVATION_HYPOTHESIS") / evidence_count
        self.confidence_summary = ConfidenceSummary(
            enterpriseCoverage=enterprise_coverage,
            evidenceCoverage=coverage,
            industryReferenceRatio=industry_ratio,
            innovationRatio=innovation_ratio,
            notes=(
                ["能力目录为空或未匹配到已登记能力"]
                if self.requirements and not enterprise_matches else []
            ),
        )
        if capability_needs_review:
            self.review.status = "REQUIRED"
            self.review.pending_items = sorted(set(self.review.pending_items + [
                "请售前或架构师确认企业能力覆盖范围",
            ]))
        self.quality = SolutionDraftQuality(
            status=status,
            evidenceCoverage=coverage,
            missingSections=missing,
            invalidCitations=invalid,
            notes=(
                ["存在未解决冲突，不能输出确定结论"] if unresolved_high_risk else []
            ) + (["正文或章节内容为空"] if has_empty_content else [])
            + (["部分章节缺少引用"] if has_evidence and coverage < 1 else [])
            + self.evidence_gaps[:3],
        )
        return self

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def parse_solution_draft(value: Any) -> SolutionDraftPayload:
    """Parse model output and turn malformed payloads into a blocked draft."""
    if isinstance(value, SolutionDraftPayload):
        return value
    return SolutionDraftPayload.model_validate(value)


def blocked_solution_draft(reason: str) -> SolutionDraftPayload:
    return SolutionDraftPayload(
        title="方案草稿（待处理）",
        evidenceGaps=[reason],
        quality=SolutionDraftQuality(status=SolutionDraftStatus.BLOCKED, notes=[reason]),
    )
