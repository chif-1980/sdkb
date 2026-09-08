"""Contracts and deterministic quality checks for Agentic solution drafts.

The runtime intentionally stores the model payload as JSON while validating it
at the boundary.  This keeps versions immutable and lets the product adapter
evolve its presentation without introducing a second orchestration model.
"""

from __future__ import annotations

import re

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


class ClarificationQuestionOption(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str | None = None


class ClarificationQuestion(BaseModel):
    """One safe, product-facing question used to refine a blocked draft."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    # Semantic metadata is produced by the Agent; the UI only renders it.
    # Keeping it optional preserves compatibility with older runs.
    intent: str = Field(default="OPEN_ENDED")
    domain: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    type: str = "TEXT"
    options: list[ClarificationQuestionOption] = Field(default_factory=list)
    required: bool = True
    allow_skip: bool = Field(default=True, alias="allowSkip")
    position: int = Field(default=1, ge=1)
    total: int = Field(default=1, ge=1)


def default_clarification_question(question: str) -> tuple[str, list[dict[str, str]]]:
    """Provide safe, product-facing choices when the model omits options."""
    text = question.strip()
    if "部署" in text or "私有化" in text or "公有云" in text or "混合" in text:
        return "SINGLE_CHOICE", [
            {"id": "private_deployment", "label": "私有化部署"},
            {"id": "hybrid_deployment", "label": "混合部署"},
            {"id": "public_cloud", "label": "公有云部署"},
            {"id": "other", "label": "其他（请说明）"},
        ]
    if "运营模式" in text or "经营模式" in text or "商城模式" in text:
        return "SINGLE_CHOICE", [
            {"id": "self_operated", "label": "自营"},
            {"id": "platform", "label": "平台入驻 / 多商户"},
            {"id": "distribution", "label": "分销"},
            {"id": "store_delivery", "label": "门店配送"},
            {"id": "other", "label": "其他（请说明）"},
        ]
    if any(token in text for token in ("销售哪些", "售卖哪些", "商品类型", "商品品类", "宠物用品")):
        return "MULTIPLE_CHOICE", [
            {"id": "food", "label": "宠物食品"},
            {"id": "supplies", "label": "宠物用品（牵引、清洁、窝垫等）"},
            {"id": "toys", "label": "宠物玩具"},
            {"id": "health", "label": "宠物保健与护理用品"},
            {"id": "clothing", "label": "宠物服饰"},
            {"id": "other", "label": "其他（请说明）"},
        ]
    if any(token in text for token in ("展示型", "完整线上销售", "购物车", "下单", "支付", "产品形态", "小程序")):
        return "SINGLE_CHOICE", [
            {"id": "showcase", "label": "展示型小程序（浏览、咨询为主）"},
            {"id": "full_commerce", "label": "完整交易小程序（购物车、下单和支付）"},
            {"id": "other", "label": "其他（请说明）"},
        ]
    if any(token in text for token in ("物流", "退款", "售后", "履约", "配送")) and any(token in text for token in ("需要", "支持", "是否", "包含")):
        return "MULTIPLE_CHOICE", [
            {"id": "logistics", "label": "物流与配送"},
            {"id": "refund", "label": "退款与售后"},
            {"id": "after_sales", "label": "客服与售后处理"},
            {"id": "none", "label": "首期暂不支持"},
            {"id": "other", "label": "其他（请说明）"},
        ]
    if ("首期" in text or "必须包含" in text) and any(token in text for token in ("范围", "功能", "模块", "端", "建设")):
        return "MULTIPLE_CHOICE", [
            {"id": "user_app", "label": "用户端"},
            {"id": "admin", "label": "运营管理端"},
            {"id": "catalog", "label": "商品管理与上下架"},
            {"id": "transaction", "label": "购物车、下单和支付"},
            {"id": "other", "label": "其他（请说明）"},
            {"id": "undecided", "label": "暂未确定首期范围"},
        ]
    if "SKU" in text or "商品数量" in text:
        return "SINGLE_CHOICE", [
            {"id": "small", "label": "少于 100 个 SKU"},
            {"id": "medium", "label": "100–1000 个 SKU"},
            {"id": "large", "label": "超过 1000 个 SKU"},
            {"id": "other", "label": "其他（请说明）"},
            {"id": "undecided", "label": "暂未确定"},
        ]
    if "ERP" in text or "库存" in text or "物流" in text or "客服" in text:
        return "MULTIPLE_CHOICE", [
            {"id": "erp", "label": "ERP / 业务系统"},
            {"id": "inventory", "label": "库存系统"},
            {"id": "logistics", "label": "物流 / 配送系统"},
            {"id": "service", "label": "客服 / 会员系统"},
            {"id": "none", "label": "暂无系统需要对接"},
        ]
    if "退款" in text or "优惠券" in text or "会员" in text or "营销" in text:
        return "MULTIPLE_CHOICE", [
            {"id": "refund", "label": "退款与售后"},
            {"id": "coupon", "label": "优惠券 / 促销"},
            {"id": "membership", "label": "会员 / 积分"},
            {"id": "group_buy", "label": "拼团 / 秒杀"},
            {"id": "undecided", "label": "暂未确定"},
        ]
    if "客户主体" in text or "运营主体" in text:
        return "SINGLE_CHOICE", [
            {
                "id": "confirmed",
                "label": "已确定",
                "description": "客户主体和运营主体都已明确，可以按此继续设计。",
            },
            {
                "id": "planning",
                "label": "已有候选，尚未最终确认",
                "description": "已有具体候选主体，但还没有完成最终决策。",
            },
            {
                "id": "undecided",
                "label": "尚未确定",
                "description": "目前还没有可供确认的候选主体。",
            },
            {
                "id": "other",
                "label": "其他情况",
                "description": "不属于以上三种情况，请补充说明。",
            },
        ]
    # Do not manufacture generic choices for an arbitrary open question.
    # “已确定/部分确定/其他” is tempting, but it is ambiguous for questions
    # such as预算、行业或交付边界。让产品 UI 显示可填写的文本框，或者由
    # Agent 在输出中明确提供 options，才能避免把模型猜测伪装成用户决策。
    return "TEXT", []


def _question_text(value: Any) -> str:
    """Read the common question aliases emitted by model/tool adapters."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("question", "prompt", "text", "description"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _question_options(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, dict) or not isinstance(value.get("options"), list):
        return []
    options: list[dict[str, str]] = []
    for index, raw in enumerate(value["options"], start=1):
        if isinstance(raw, str) and raw.strip():
            options.append({"id": raw.strip(), "label": raw.strip()})
            continue
        if not isinstance(raw, dict):
            continue
        label = raw.get("label") or raw.get("text") or raw.get("name") or raw.get("value")
        if not isinstance(label, str) or not label.strip():
            continue
        option_id = raw.get("id") or raw.get("value") or f"option-{index}"
        if not isinstance(option_id, str) or not option_id.strip():
            option_id = f"option-{index}"
        item = {"id": option_id.strip(), "label": label.strip()}
        description = raw.get("description")
        if isinstance(description, str) and description.strip():
            item["description"] = description.strip()
        options.append(item)
    return options


def clarification_question_from_open_question(value: Any, index: int) -> dict[str, Any] | None:
    """Convert one semantic ``open_questions`` item into a UI-safe question.

    ``open_questions`` is intentionally still kept as a concise descriptive
    list in the public Blueprint.  The richer question is derived only when
    the Agent did not emit ``clarification_questions`` itself.  Explicit
    options always win; otherwise we infer choices only for known dimensions
    (deployment, scope, SKU, integrations, etc.) and use TEXT for unknown
    dimensions.
    """
    question = _question_text(value)
    if not question:
        return None
    raw = value if isinstance(value, dict) else {}
    options = _question_options(raw)
    raw_type = raw.get("type") if isinstance(raw, dict) else None
    question_type = str(raw_type or "").strip().upper()
    if options:
        if question_type not in {"SINGLE_CHOICE", "MULTIPLE_CHOICE"}:
            question_type = "SINGLE_CHOICE"
    else:
        inferred_type, inferred_options = default_clarification_question(question)
        question_type = (
            question_type
            if question_type in {"SINGLE_CHOICE", "MULTIPLE_CHOICE", "TEXT"}
            else inferred_type
        )
        options = inferred_options
        if question_type in {"SINGLE_CHOICE", "MULTIPLE_CHOICE"} and not options:
            question_type = "TEXT"
    question_id = raw.get("id") or raw.get("questionId") or raw.get("question_id")
    if not isinstance(question_id, str) or not question_id.strip():
        question_id = f"OPEN_QUESTION_{index}"
    return {
        "id": question_id.strip(),
        "question": question,
        "type": question_type,
        "options": options,
        "required": raw.get("required") is not False,
        "allowSkip": raw.get("allowSkip", raw.get("allow_skip", True)) is not False,
    }


def clarification_questions_from_requirements(value: Any) -> list[dict[str, Any]]:
    """Recover safe questions from explicitly unresolved requirements.

    Older solution runs sometimes returned a complete requirement analysis but
    omitted both ``open_questions`` and ``clarification_questions``.  Only
    requirements carrying an explicit unresolved marker are eligible here;
    ordinary requirements must never be turned into speculative questions.
    """
    if not isinstance(value, list):
        return []
    pending: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            source = ""
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            source = str(item.get("source") or "").strip()
        else:
            continue
        combined = f"{source} {text}".strip()
        if text and re.search(
            r"待确认|待补充|需要确认|尚未(?:明确|确定)|未(?:明确|确定)|"
            r"建议纳入(?:首期)?范围|建议(?:纳入|考虑)|(?:产品|能力|范围)推断",
            combined,
        ):
            item_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
            pending.append({"id": item_id, "text": text, "source": source})
    if not pending:
        return []

    scope = [
        item for item in pending
        if re.search(
            r"首期|范围|功能模块|用户端|管理端|建议纳入",
            f"{item['source']} {item['text']}",
        )
    ]
    others = [item for item in pending if item not in scope]
    questions: list[dict[str, Any]] = []
    if scope:
        labels: list[str] = []
        for item in scope:
            cleaned = re.sub(
                r"^\s*(?:建议|推荐)?纳入首期范围\s*[:：]?\s*",
                "",
                item["text"],
                flags=re.I,
            )
            cleaned = re.sub(
                r"^\s*(?:首期范围|范围)\s*[:：]?\s*", "", cleaned, flags=re.I
            ).strip()
            if not cleaned:
                continue
            parts = [part.strip() for part in re.split(r"\n+|[；;]+", cleaned) if part.strip()]
            for part in parts or [cleaned]:
                if part not in labels:
                    labels.append(part)
        if len(labels) > 1:
            options = [{"id": f"SCOPE_{index}", "label": label} for index, label in enumerate(labels, start=1)]
        else:
            options = [
                {"id": "INCLUDE_SUGGESTED", "label": "纳入上述建议范围"},
                {"id": "ADJUST_SCOPE", "label": "需要调整首期范围"},
            ]
        options.extend([
            {"id": "UNDECIDED", "label": "暂未确定首期范围"},
            {"id": "OTHER", "label": "其他（请说明）"},
        ])
        questions.append({
            "id": "REQUIREMENT_SCOPE",
            "question": "以下建议内容是否纳入首期建设范围？",
            "type": "MULTIPLE_CHOICE",
            "options": options,
            "required": True,
            "allowSkip": True,
            "position": 1,
            "total": 1,
        })

    for index, item in enumerate(others, start=1):
        text = item["text"]
        inferred_type, inferred_options = default_clarification_question(text)
        if re.search(r"[？?]$", text):
            question = text
        elif re.match(r"^\s*(?:明确|确认|确定|请确认)", text):
            question = "请" + re.sub(r"^\s*(?:明确|确认|确定|请确认)\s*", "", text)
        else:
            question = f"请确认：{text}"
        requirement_id = str(item.get("id") or "").strip()
        questions.append({
            "id": requirement_id or f"REQUIREMENT_{index}",
            "question": question,
            "type": inferred_type,
            "options": inferred_options,
            "required": True,
            "allowSkip": True,
            "position": len(questions) + 1,
            "total": len(questions) + 1,
        })
    total = len(questions)
    for index, question in enumerate(questions, start=1):
        question["position"] = index
        question["total"] = total
    return questions


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
    clarification_questions: list[ClarificationQuestion] = Field(
        default_factory=list,
        alias="clarificationQuestions",
    )
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

    @model_validator(mode="before")
    @classmethod
    def normalize_open_questions(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        key = "openQuestions" if "openQuestions" in value else "open_questions"
        questions = value.get(key)
        data = dict(value)
        # Keep the display list compact while retaining structured questions
        # for the existing conversational clarification UI.
        display_questions: list[Any] = []
        derived_questions: list[dict[str, Any]] = []
        if isinstance(questions, list):
            for item in questions:
                if isinstance(item, dict):
                    # Older model responses used ``prompt``/``text`` for the same
                    # field.  Accept those aliases, but keep an invalid object
                    # intact when none is present so schema validation reports a
                    # useful malformed result instead of silently dropping it.
                    question = item.get("question") or item.get("prompt") or item.get("text")
                    display_questions.append(question if question is not None else item)
                else:
                    display_questions.append(item)
            data[key] = display_questions
        # Models frequently emit only ``open_questions`` even though the
        # product needs an actionable confirmation card.  Preserve explicit
        # clarification questions when present; otherwise derive a stable,
        # safe question batch from the semantic open questions.  This keeps
        # descriptive gaps visible while making them resumable through the
        # existing LangGraph/product UI path.
        clarification_key = "clarificationQuestions" if "clarificationQuestions" in data else "clarification_questions"
        explicit = data.get(clarification_key)
        if not isinstance(explicit, list) or not explicit:
            if isinstance(questions, list):
                for index, item in enumerate(questions, start=1):
                    derived = clarification_question_from_open_question(item, index)
                    if derived:
                        derived_questions.append(derived)
            # A legacy Agent may have emitted only a requirement analysis.  A
            # requirement is eligible only when it explicitly says that the
            # item is pending/derived; ordinary requirements stay untouched.
            if not derived_questions:
                derived_questions = clarification_questions_from_requirements(data.get("requirements"))
            if derived_questions:
                data["clarificationQuestions"] = derived_questions
                if not isinstance(questions, list):
                    data["open_questions"] = [item["question"] for item in derived_questions]
        return data

    @model_validator(mode="before")
    @classmethod
    def normalize_clarification_choices(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        key = "clarificationQuestions" if "clarificationQuestions" in value else "clarification_questions"
        questions = value.get(key)
        if not isinstance(questions, list):
            return value
        data = dict(value)
        normalized = []
        total = len(questions)
        for index, item in enumerate(questions, start=1):
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            question = str(item.get("question") or item.get("prompt") or "").strip()
            options = item.get("options")
            if question and (not isinstance(options, list) or not options):
                question_type, default_options = default_clarification_question(question)
                item = {**item, "type": question_type, "options": default_options}
            # Keep an explicitly emitted batch ordered even when the model
            # omits UI-only position metadata.  Existing values are retained
            # for backwards compatibility, but defaults are deterministic.
            item = {
                **item,
                "position": item.get("position") or index,
                "total": item.get("total") or total,
            }
            normalized.append(item)
        data[key] = normalized
        return data

    @field_validator("evidence_gaps", mode="before")
    @classmethod
    def normalize_evidence_gaps(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("description"), str):
                normalized.append(item)
                continue
            resolution = item.get("resolution")
            suffix = f"（待补充：{resolution.strip()}）" if isinstance(resolution, str) and resolution.strip() else ""
            normalized.append(f"{item['description'].strip()}{suffix}")
        return normalized

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


def blocked_solution_draft(reason: str, request: str = "") -> SolutionDraftPayload:
    """Return a useful, editable Blueprint even when evidence is insufficient.

    A blocker describes confidence and publishability. It must not erase the
    requirement analysis or the draft structure the user can continue to
    refine through LangGraph clarification runs.
    """
    requirement = request.strip() or "待补充客户需求与交付目标"
    section_content = {
        "执行摘要": "当前证据不足，先保留可编辑的方案骨架；完成待确认问题后再生成正式结论。",
        "需求与范围": f"客户需求：{requirement}\n\n待确认客户行业、目标、范围和交付物。",
        "方案设计": "待根据已确认需求匹配企业能力，并补充总体架构、功能模块与能力边界。",
        "实施计划": "待确认范围后，按需求确认、方案评审、实施验证和交付复盘分阶段推进。",
        "风险与待确认": "当前正式证据不足，所有能力、参数、版本和交付承诺均需继续核验。",
    }
    fallback_question_type, fallback_options = default_clarification_question(
        "请先补充客户行业、核心目标和本次方案的交付范围。"
    )
    return SolutionDraftPayload(
        title="方案草稿（待处理）",
        customerContext=requirement,
        executiveSummary="当前证据不足，已生成可继续确认和编辑的方案骨架。",
        requirements=[DraftRequirement(id="REQ-1", text=requirement, source="待确认")],
        sections=[
            DraftSection(
                id=f"SEC-{index}",
                title=title,
                contentMarkdown=content,
                requirementIds=["REQ-1"] if title == "需求与范围" else [],
                citationIds=[],
            )
            for index, (title, content) in enumerate(section_content.items(), start=1)
        ],
        assumptions=["客户场景、预算和交付边界尚未完整确认"],
        openQuestions=["请补充客户行业、核心目标、范围和部署方式"],
        clarificationQuestions=[ClarificationQuestion(
            id="SOLUTION_CONTEXT",
            question="请先补充客户行业、核心目标和本次方案的交付范围。",
            type=fallback_question_type,
            options=fallback_options,
            required=True,
            allowSkip=True,
            position=1,
            total=1,
        )],
        evidenceGaps=[reason],
        architecture={
            "summary": "待结合已确认需求和企业能力匹配结果完善总体架构",
            "layers": ["数据与集成层", "能力与服务层", "业务应用层", "交付与运营层"],
        },
        review=ReviewState(
            status="REQUIRED",
            pendingItems=["请确认企业能力覆盖范围和未证实的交付承诺"],
            requiredRoles=["方案负责人", "架构师"],
        ),
        quality=SolutionDraftQuality(status=SolutionDraftStatus.BLOCKED, notes=[reason]),
    )
