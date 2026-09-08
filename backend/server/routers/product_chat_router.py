"""Conversation and message endpoints for the enterprise assistant."""

from __future__ import annotations

import asyncio
import io
import json
import re
from collections.abc import AsyncIterator
from traceback import format_tb
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from server.routers.product_api_route import ProductApiRoute
from server.utils.auth_middleware import get_product_user
from yuxi.governance.lifecycle_service import KnowledgeLifecycleService
from yuxi.product_chat.answer_service import AnswerDelta, AnswerProgress, AnswerService, GroundedAnswer
from yuxi.product_chat.citation_service import CitationResolutionError
from yuxi.product_chat.material_service import ProductMaterialService
from yuxi.product_chat.progress import progress_stage_from_chunk
from yuxi.product_chat.repository import (
    ProductChatNotFoundError,
    ProductChatRepository,
    ProductMessageNotFoundError,
)
from yuxi.product_chat.solution_draft_repository import (
    SolutionDraftNotFoundError,
    SolutionDraftRepository,
    serialize_solution_draft,
)
from yuxi.product_chat.solution_draft_service import (
    SolutionExtractionStatus,
    extract_solution_payload,
    extract_solution_result,
    render_solution_draft,
)
from yuxi.product_chat.solution_draft import clarification_question_from_open_question
from yuxi.repositories.agent_repository import AgentRepository, SOLUTION_DRAFT_AGENT_SLUG
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.agent_run_service import (
    await_agent_run_result,
    cancel_agent_run_view,
    get_agent_run_result,
    create_agent_run_view,
    get_agent_run_view,
    load_agent_run_result,
    stream_agent_run_events,
)
from yuxi.services.conversation_service import upload_thread_attachment_view
from yuxi.services.input_message_service import build_chat_input_message
from yuxi.product_chat.schemas import (
    CitationResponse,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    MessageExchangeResponse,
    MessageFeedbackRequest,
    MessageFeedbackResponse,
    MessageResponse,
    MaterialDistributionRequest,
    MaterialDistributionResponse,
    MaterialDistributionTaskResponse,
    ProductMaterialResponse,
    ResumeRunRequest,
    SendMessageRequest,
    SolutionDraftEditRequest,
)
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_product import (
    MessageCitation,
    ProductConversation,
    ProductMessage,
)
from yuxi.knowledge.runtime import knowledge_base
from yuxi.utils import logger
from yuxi.utils.datetime_utils import format_utc_datetime, utc_isoformat

product_chat = APIRouter(route_class=ProductApiRoute)

_SKILL_MENTION_PATTERN = re.compile(r"(^|\s)@(查资料|做方案|分析会议)(?=\s|$)")
_MATERIAL_INTENT_PATTERN = re.compile(r"查资料|产品说明|宣传手册|宣传册|解决方案|下载|分发|飞书原文")


def _blocked_draft_needs_payload_repair(draft: object, payload: object) -> bool:
    """Return whether a legacy blocked projection is missing safe data.

    Older solution runs were projected before ``clarificationQuestions`` was
    derived from ``openQuestions``.  Their quality is still legitimately
    ``BLOCKED`` (for example, there may be no citations), so using a quality
    transition as the sole repair signal leaves those drafts permanently
    without an interactive question card.  Only backfill missing questions
    (or a genuinely improved non-blocked payload), and never resurrect a
    batch explicitly marked resolved by a user.
    """
    if str(getattr(draft, "status", "") or "").upper() != "BLOCKED":
        return False
    candidate_questions = getattr(payload, "clarification_questions", None)
    if not candidate_questions:
        return bool(
            getattr(getattr(payload, "quality", None), "status", None)
            and str(getattr(payload.quality.status, "value", payload.quality.status)).upper() != "BLOCKED"
        )
    persisted = getattr(draft, "payload", None)
    persisted = persisted if isinstance(persisted, dict) else {}
    if persisted.get("clarificationQuestionsResolved") is True or persisted.get("clarification_questions_resolved") is True:
        return False
    existing_questions = persisted.get("clarificationQuestions")
    if not isinstance(existing_questions, list) or not existing_questions:
        existing_questions = persisted.get("clarification_questions")
    if not isinstance(existing_questions, list) or not existing_questions:
        return True
    quality = getattr(payload, "quality", None)
    quality_status = getattr(getattr(quality, "status", None), "value", getattr(quality, "status", ""))
    return str(quality_status or "").upper() != "BLOCKED"

# Solution runs are backed by LangGraph events.  The product stream records
# safe actions in arrival order because an agent may legitimately retrieve,
# verify, and retrieve again.  Partial Blueprint JSON is filtered to
# user-facing text before it is emitted.

_SOLUTION_STREAM_FIELDS = {
    "title",
    "customer_context",
    "executive_summary",
    "content_markdown",
    "text",
    "claim",
    "applicability",
    "description",
}

_SOLUTION_STREAM_CHUNK_MAX_CHARS = 64
_SOLUTION_STREAM_CHUNK_MIN_CHARS = 18
_SOLUTION_STREAM_BOUNDARIES = set("\n。！？；：,.!?;:")


def _knowledge_question(content: str) -> str:
    """Keep the visible command in history but omit it from retrieval/model input."""
    cleaned = _SKILL_MENTION_PATTERN.sub(lambda match: match.group(1), content).strip()
    return cleaned or content


def _uses_material_search(content: str, skill_id: str | None = None) -> bool:
    return skill_id == "MATERIAL_SEARCH" or "@查资料" in content or bool(_MATERIAL_INTENT_PATTERN.search(content))


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "CONVERSATION_NOT_FOUND", "message": "会话不存在"},
    )


def _message_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "MESSAGE_NOT_FOUND", "message": "消息不存在"},
    )


def _knowledge_unavailable(conversation_id: str, error: Exception) -> JSONResponse:
    logger.error(
        "product_chat_answer_unavailable conversation_id={} error_type={}",
        conversation_id,
        type(error).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "error": {
                "code": "KNOWLEDGE_SERVICE_UNAVAILABLE",
                "message": "知识服务暂时不可用，请稍后重试",
            }
        },
    )


def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _conversation_response(
    conversation: ProductConversation,
    message_count: int,
) -> ConversationSummaryResponse:
    return ConversationSummaryResponse(
        id=conversation.conversation_id,
        title=conversation.title or "",
        status=conversation.status,
        message_count=message_count,
        created_at=format_utc_datetime(conversation.created_at) or "",
        updated_at=format_utc_datetime(conversation.updated_at) or "",
    )


def _citation_response(citation: MessageCitation) -> CitationResponse:
    return CitationResponse(
        id=citation.citation_id,
        kind=citation.kind,
        title=citation.title,
        path=citation.path_text,
        locator=citation.locator,
        excerpt=citation.excerpt,
        version_at=format_utc_datetime(citation.source_version_at),
        media_type=citation.media_type,
        image_url=citation.image_url,
        preview_url=citation.preview_url,
        image_alt=citation.image_alt,
    )


def _message_response(
    message: ProductMessage,
    citations: list[MessageCitation],
    materials: list[ProductMaterialResponse] | None = None,
    solution_draft: dict | None = None,
) -> MessageResponse:
    return MessageResponse(
        id=message.message_id,
        role=message.role,
        content=message.content,
        skill_id=message.skill_id,
        answer_status=message.answer_status,
        feedback_rating=message.feedback_rating,
        feedback_reason_type=message.feedback_reason_type,
        feedback_reason_text=message.feedback_reason_text,
        citations=[_citation_response(citation) for citation in citations],
        materials=materials or [],
        solution_draft=solution_draft,
        created_at=format_utc_datetime(message.created_at) or "",
    )


def _solution_answer_status(payload) -> str:
    """Map Blueprint quality to the product's concise answer status."""
    unresolved = any(
        str(conflict.status or "").upper() == "UNRESOLVED"
        for conflict in payload.conflicts
    )
    if unresolved:
        return "CONFLICTING"
    if payload.quality and payload.quality.status.value == "BLOCKED":
        return "INSUFFICIENT"
    return "SUPPORTED"


async def _ensure_solution_thread(db, *, conversation_id: str, current_user: User) -> str:
    """Bind one product conversation to one stable Yuxi LangGraph thread."""
    thread_id = f"product-{conversation_id}"
    await AgentRepository(db).ensure_solution_draft_agent()
    repository = ConversationRepository(db)
    existing = await repository.get_conversation_by_thread_id(thread_id)
    if existing:
        if existing.uid != str(current_user.uid) or existing.agent_id != SOLUTION_DRAFT_AGENT_SLUG:
            raise HTTPException(status_code=409, detail="方案线程绑定冲突")
        return thread_id
    await repository.create_conversation(
        uid=str(current_user.uid),
        agent_id=SOLUTION_DRAFT_AGENT_SLUG,
        title="方案草稿",
        thread_id=thread_id,
        metadata={"source": "product_chat", "product_conversation_id": conversation_id},
    )
    return thread_id


async def _create_solution_run(
    *,
    conversation_id: str,
    request: SendMessageRequest,
    current_user: User,
) -> dict:
    request_id = request.request_id or str(uuid4())
    async with pg_manager.get_async_session_context() as db:
        await ProductChatRepository(db).require_conversation(conversation_id, current_user.id)
        thread_id = await _ensure_solution_thread(db, conversation_id=conversation_id, current_user=current_user)
        return await create_agent_run_view(
            input_message=build_chat_input_message(_knowledge_question(request.content), None),
            agent_slug=SOLUTION_DRAFT_AGENT_SLUG,
            thread_id=thread_id,
            meta={
                "request_id": request_id,
                "source": "product_chat_solution_draft",
                "attachment_file_ids": request.attachment_ids,
                "agent_invocation_meta": {
                    "product_conversation_id": conversation_id,
                    "skill_id": "SOLUTION_DRAFT",
                    "request_source": "enterprise_assistant",
                    # Keep the product attachment identifiers in the durable
                    # invocation context.  A resumed LangGraph run only
                    # carries its answer, so the product adapter needs this
                    # metadata to project the final draft back into the same
                    # conversation.
                    "product_attachment_ids": list(request.attachment_ids),
                },
            },
            current_uid=str(current_user.uid),
            db=db,
        )


def _resume_answer_text(raw_content: object, question: dict | None = None) -> str | None:
    """Normalize a persisted resume input into safe, human-readable context.

    Resume input is deliberately stored in its runtime form (option ids are
    required by LangGraph), but old runs may not have the separate
    ``resume_display_answer`` metadata.  Always pass those values through the
    same display normalizer used by the product transcript so replaying an
    old chain cannot surface ids such as ``confirmed`` to the user.
    """
    if not isinstance(raw_content, str) or not raw_content.strip():
        return None
    try:
        value = json.loads(raw_content)
    except (TypeError, ValueError):
        value = raw_content
    if isinstance(value, dict):
        # Keep question ids/keys in the model context so a multi-question
        # continuation remains unambiguous, while translating only the
        # option values.  The keys never get written to the product
        # transcript; this representation is for Agent context replay.
        rendered_items = []
        for key, item in value.items():
            if key in {"action", "questionId", "question_id"}:
                continue
            rendered = _resume_display_value(item, question)
            if rendered:
                rendered_items.append(f"{key}：{rendered}")
        normalized = "\n".join(rendered_items)
    else:
        normalized = _resume_display_value(value, question)
    return normalized.strip() or None


def _resume_request_answer(request: ResumeRunRequest) -> str:
    """Normalize a product clarification answer before adding it to context.

    The browser may submit a string, a selected option list, or a structured
    value.  ``skip`` is represented explicitly so the agent can distinguish a
    user who is unsure from a missing answer.
    """
    if request.action == "skip":
        return "（用户暂不确定）"
    if isinstance(request.answer, str):
        answer = request.answer.strip()
    elif isinstance(request.answer, list) and all(isinstance(item, str) for item in request.answer):
        answer = "、".join(item.strip() for item in request.answer if item.strip())
    else:
        try:
            answer = json.dumps(request.answer, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="answer 格式不受支持") from exc
        answer = answer.strip()
    if not answer:
        raise HTTPException(status_code=422, detail="answer 不能为空")
    return answer


_SKIPPED_RESUME_VALUES = {"（用户暂不确定）", "(用户暂不确定)", "暂不确定", "跳过"}

_LEGACY_RESUME_LABELS = {
    "confirmed": "已确定",
    "planning": "已有候选，尚未最终确认",
    "undecided": "尚未确定",
    "other": "其他情况",
    "self_operated": "自营",
    "platform": "平台入驻 / 多商户",
    "distribution": "分销",
    "store_delivery": "门店配送",
    "user_app": "用户端",
    "admin": "运营管理端",
    "catalog": "商品管理与上下架",
    "transaction": "购物车、下单和支付",
    "small": "少于 100 个 SKU",
    "medium": "100–1000 个 SKU",
    "large": "超过 1000 个 SKU",
    "erp": "ERP / 业务系统",
    "inventory": "库存系统",
    "logistics": "物流 / 配送系统",
    "service": "客服 / 会员系统",
    "none": "暂无系统需要对接",
    "refund": "退款与售后",
    "coupon": "优惠券 / 促销",
    "membership": "会员 / 积分",
    "group_buy": "拼团 / 秒杀",
    "private_deployment": "私有化部署",
    "hybrid_deployment": "混合部署",
    "public_cloud": "公有云部署",
    "showcase": "展示型小程序（浏览、咨询为主）",
    "full_commerce": "完整交易小程序（购物车、下单和支付）",
}


def _is_skipped_resume_value(value: object) -> bool:
    return isinstance(value, str) and value.strip() in _SKIPPED_RESUME_VALUES


def _resume_label_lookup(text: str, labels: dict[str, str]) -> str | None:
    """Look up an option id without making ordinary prose substitutions.

    Option ids are stable machine values and are commonly emitted in a
    different case by older clients.  Matching only the complete scalar (or a
    complete comma-separated part) keeps text such as ``please use admin for
    this area`` untouched while still rendering ``CONFIRMED`` as its label.
    """
    if text in labels:
        return labels[text]
    folded = text.casefold()
    for option_id, label in labels.items():
        if option_id.casefold() == folded:
            return label
    for option_id, label in _LEGACY_RESUME_LABELS.items():
        if option_id.casefold() == folded:
            return label
    return None


def _resume_answer_entries(request: ResumeRunRequest) -> list[dict]:
    """Normalize scalar, question-id and batch resume payloads."""
    raw = request.answer
    if isinstance(raw, dict) and isinstance(raw.get("questionId"), str) and (
        "answer" in raw or "value" in raw
    ):
        value = raw.get("value", raw.get("answer"))
        return [{
            "question_id": raw["questionId"],
            "value": value,
            "action": "skip" if raw.get("action") == "skip" or _is_skipped_resume_value(value) else request.action,
        }]
    if isinstance(raw, dict) and not any(key in raw for key in ("value", "answer", "questionId")):
        return [
            {
                "question_id": question_id,
                "value": value.get("value", value.get("answer")) if isinstance(value, dict) else value,
                "action": (
                    "skip"
                    if isinstance(value, dict) and value.get("action") == "skip"
                    or _is_skipped_resume_value(
                        value.get("value", value.get("answer")) if isinstance(value, dict) else value
                    )
                    else request.action
                ),
            }
            for question_id, value in raw.items()
        ]
    return [{
        "question_id": request.question_id,
        "value": raw,
        "action": "skip" if request.action == "skip" or _is_skipped_resume_value(raw) else request.action,
    }]


def _resume_display_value(value: object, question: dict | None = None) -> str:
    """Render a resume value for the product transcript.

    LangGraph receives option ids so the graph can make a deterministic
    decision.  Those ids are implementation details, though, and must not be
    written back to the user-facing conversation.  Keep free-form text and
    ``其他：…`` answers intact while translating known option ids to labels.
    """
    if _is_skipped_resume_value(value):
        return "暂不确定"
    options = question.get("options") if isinstance(question, dict) else None
    labels: dict[str, str] = {}
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            option_id = str(
                option.get("id")
                or option.get("value")
                or option.get("key")
                or option.get("text")
                or ""
            ).strip()
            label = str(option.get("label") or option.get("text") or option.get("value") or option_id).strip()
            if option_id and label:
                labels[option_id] = label
    if isinstance(value, list):
        return "、".join(
            rendered
            for item in value
            if (rendered := _resume_display_value(item, question))
        )
    if isinstance(value, dict):
        # Batch answers and older adapters may wrap a value as
        # ``{value, action}``.  Do not render the transport-only ``action``
        # token as if it were a user answer.
        if "value" in value or "answer" in value:
            if value.get("action") == "skip":
                return "暂不确定"
            return _resume_display_value(value.get("value", value.get("answer")), question)
        return "、".join(
            rendered
            for key, item in value.items()
            if key not in {"action", "questionId", "question_id"}
            and (rendered := _resume_display_value(item, question))
        )
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith("其他："):
        return text
    # Multi-select answers occasionally arrive as a comma-separated scalar.
    parts = [part.strip() for part in re.split(r"[、,，;；]", text) if part.strip()]
    if len(parts) > 1:
        translated = [_resume_label_lookup(part, labels) for part in parts]
        if all(translated):
            return "、".join(translated)  # type: ignore[arg-type]
    return _resume_label_lookup(text, labels) or text


def _resume_display_answer(request: ResumeRunRequest, questions: list[dict]) -> str:
    """Return a human-readable transcript value without changing runtime ids."""
    entries = _resume_answer_entries(request)
    by_id = {
        str(question.get("questionId") or question.get("question_id") or question.get("id") or "").strip(): question
        for question in questions
        if isinstance(question, dict)
    }
    rendered: list[str] = []
    for entry in entries:
        question_id = str(entry.get("question_id") or "").strip()
        value = "（用户暂不确定）" if entry.get("action") == "skip" else entry.get("value")
        question = by_id.get(question_id)
        # A scalar answer from a one-question legacy interrupt has no
        # question id in the request body.  Use its sole question's options
        # before falling back to the legacy id map.
        if question is None and not question_id and len(entries) == 1 and len(questions) == 1:
            question = questions[0]
        display = _resume_display_value(value, question)
        if not display:
            continue
        question_text = str(question.get("question") or "").strip() if isinstance(question, dict) else ""
        rendered.append(f"{question_text}：{display}" if question_text and len(entries) > 1 else display)
    if rendered:
        return "\n".join(rendered)
    # Never fall back to the runtime serializer here: it would put option ids
    # directly into the product transcript when question metadata is absent.
    raw_value = "（用户暂不确定）" if request.action == "skip" else request.answer
    return _resume_display_value(raw_value) or ""


def _resume_value_present(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_resume_value_present(item) for item in value)
    if isinstance(value, dict):
        return bool(value)
    return value is not None


def _validated_resume_answer(
    request: ResumeRunRequest,
    questions: list[dict],
    *,
    run_id: str | None = None,
) -> object:
    """Require one explicit answer/skip decision for every interrupt item."""
    if not questions:
        return _resume_request_answer(request) if request.action == "skip" else request.answer

    normalized_questions = []
    seen: set[str] = set()
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        question_id = str(
            question.get("questionId")
            or question.get("question_id")
            or question.get("id")
            or f"q-{index + 1}"
        ).strip()
        if not question_id or question_id in seen:
            continue
        seen.add(question_id)
        normalized_questions.append({
            "id": question_id,
            "allow_skip": question.get(
                "allowSkip",
                question.get("allow_skip", question.get("skippable", True)),
            )
            is not False,
        })
    if not normalized_questions:
        return _resume_request_answer(request) if request.action == "skip" else request.answer

    by_id = {question["id"]: question for question in normalized_questions}
    entries = _resume_answer_entries(request)
    resolved: dict[str, dict] = {}
    invalid: list[str] = []
    not_skippable: list[str] = []
    first_id = normalized_questions[0]["id"]
    for entry in entries:
        question_id = str(entry.get("question_id") or (first_id if len(entries) == 1 else "")).strip()
        if question_id not in by_id:
            invalid.append(question_id)
            continue
        action = "skip" if entry.get("action") == "skip" or _is_skipped_resume_value(entry.get("value")) else "answer"
        if action == "skip" and by_id[question_id]["allow_skip"] is False:
            not_skippable.append(question_id)
        if action == "answer" and not _resume_value_present(entry.get("value")):
            continue
        resolved[question_id] = {"value": entry.get("value"), "action": action}

    if invalid:
        detail = {
            "code": "QUESTION_NOT_CURRENT",
            "message": "待确认问题已变化，请刷新后重试",
            "questionIds": invalid,
        }
        if run_id:
            detail["runId"] = run_id
        raise HTTPException(
            status_code=400,
            detail=detail,
        )
    if not_skippable:
        detail = {
            "code": "QUESTION_NOT_SKIPPABLE",
            "message": "当前问题不允许跳过",
            "questionIds": not_skippable,
        }
        if run_id:
            detail["runId"] = run_id
        raise HTTPException(
            status_code=400,
            detail=detail,
        )
    missing = [question["id"] for question in normalized_questions if question["id"] not in resolved]
    if missing:
        detail = {
            "code": "QUESTIONS_INCOMPLETE",
            "message": "请先完成全部待确认问题，再继续生成方案",
            "missingQuestionIds": missing,
        }
        if run_id:
            detail["runId"] = run_id
        raise HTTPException(
            status_code=400,
            detail=detail,
        )

    values = {
        question_id: "（用户暂不确定）" if entry["action"] == "skip" else entry["value"]
        for question_id, entry in resolved.items()
    }
    if len(values) == 1:
        return next(iter(values.values()))
    return values


def _normalize_interrupt_question_batch(value: object) -> list[dict]:
    """Extract a deterministic, de-duplicated question batch from an interrupt.

    Older adapters persisted a single question at the top level while newer
    LangGraph events carry ``questions``.  Keeping the normalization here
    means resume validation does not accidentally treat the first item as the
    whole batch.  The helper deliberately keeps only safe question metadata;
    raw tool payloads must never reach the product API.
    """
    if not isinstance(value, dict):
        return []
    raw_questions = value.get("questions")
    if not isinstance(raw_questions, list):
        raw_questions = [value] if any(key in value for key in ("question", "prompt", "text")) else []

    normalized: list[dict] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            continue
        question = str(raw.get("question") or raw.get("prompt") or raw.get("text") or "").strip()
        if not question:
            continue
        question_id = str(
            raw.get("questionId")
            or raw.get("question_id")
            or raw.get("id")
            or f"q-{index + 1}"
        ).strip()
        if not question_id:
            question_id = f"q-{index + 1}"
        text_key = re.sub(r"\s+", " ", question).strip().casefold()
        id_key = question_id.casefold()
        if id_key in seen_ids or text_key in seen_texts:
            continue
        seen_ids.add(id_key)
        seen_texts.add(text_key)
        # Preserve the runtime's safe presentation fields, while normalizing
        # the aliases consumed by _validated_resume_answer().
        item = dict(raw)
        item["question"] = question
        item["questionId"] = question_id
        item.setdefault("id", question_id)
        normalized.append(item)

    total = len(normalized)
    for index, item in enumerate(normalized, start=1):
        # Runtime-provided positions are not trusted for validation/UI order;
        # the event's order is the only stable order across legacy payloads.
        item["position"] = index
        item["total"] = total
    return normalized


def _draft_questions_for_resume(draft: object) -> list[dict]:
    """Read the persisted clarification batch used by a legacy draft run.

    Before durable LangGraph interrupts were exposed, a completed run could
    still be projected as a ``BLOCKED`` solution draft containing the
    questions that the user needed to answer.  The compatibility resume path
    has no checkpoint interrupt to inspect, so the draft payload is the
    authoritative snapshot.  Keep this conversion deliberately narrow and
    feed it through the same normalizer as runtime interrupt events.
    """
    payload = draft.get("payload") if isinstance(draft, dict) else getattr(draft, "payload", None)
    if not isinstance(payload, dict):
        return []
    raw_questions = payload.get("clarificationQuestions")
    if not isinstance(raw_questions, list):
        raw_questions = payload.get("clarification_questions")
    # Legacy projections persisted only the descriptive openQuestions list.
    # Derive the same safe question metadata used by the Blueprint parser so
    # completed-run resume still validates a full batch instead of accepting a
    # scalar answer and silently dropping the remaining questions.
    if not isinstance(raw_questions, list) or not raw_questions:
        raw_open_questions = payload.get("openQuestions")
        if not isinstance(raw_open_questions, list):
            raw_open_questions = payload.get("open_questions")
        if isinstance(raw_open_questions, list):
            raw_questions = [
                question
                for index, item in enumerate(raw_open_questions, start=1)
                if (question := clarification_question_from_open_question(item, index)) is not None
            ]
    if not isinstance(raw_questions, list) or not raw_questions:
        return []
    return _normalize_interrupt_question_batch({"questions": raw_questions})


def _resume_context_answer(request: ResumeRunRequest, questions: list[dict]) -> str:
    """Render answers with question text for a fresh compatibility run.

    A completed legacy run is continued by starting a new chat run rather
    than resuming a LangGraph checkpoint.  Including the question text in the
    model input makes a batch answer unambiguous and tells the Agent which
    conditions have already been settled, reducing repeated clarification
    questions.  This is model context only; option ids are translated to
    their user-facing labels and never exposed here.
    """
    entries = _resume_answer_entries(request)
    by_id = {
        str(
            question.get("questionId")
            or question.get("question_id")
            or question.get("id")
            or ""
        )
        .strip()
        .casefold(): question
        for question in questions
        if isinstance(question, dict)
    }
    rendered: list[str] = []
    for entry in entries:
        question_id = str(entry.get("question_id") or "").strip()
        question = by_id.get(question_id.casefold()) if question_id else None
        if question is None and len(entries) == 1 and len(questions) == 1:
            question = questions[0]
        value = "（用户暂不确定）" if entry.get("action") == "skip" else entry.get("value")
        display = _resume_display_value(value, question)
        if not display:
            continue
        question_text = str(question.get("question") or "").strip() if isinstance(question, dict) else ""
        rendered.append(f"{question_text}：{display}" if question_text else display)
    return "\n".join(rendered).strip()


async def _interrupt_questions_for_run(run_id: str, current_user: User, parent_data: dict) -> list[dict]:
    """Replay persisted events and recover the latest complete question batch.

    A run can contain a direct interrupt snapshot plus one or more replayed
    interrupt events (for example after a worker retry).  Returning on the
    first item made a stale single-question snapshot hide the newer batch and
    caused already answered questions to reappear.  Prefer the most recent
    event batch, but retain the largest batch when a retry emitted a truncated
    duplicate.  Exact duplicate ids/text are removed by the normalizer.
    """
    direct = parent_data.get("interrupt") if isinstance(parent_data, dict) else None
    direct_batch = _normalize_interrupt_question_batch(direct)
    best_batch = direct_batch
    best_source = 0  # direct snapshot is a fallback; event replay wins ties.
    best_order = 0

    event_order = 0
    async for raw_event in stream_agent_run_events(
        run_id=run_id,
        after_seq="0-0",
        current_uid=str(current_user.uid),
        verbose=False,
    ):
        event_order += 1
        progress = _agent_progress(raw_event)
        interrupt = progress.get("interrupt") if isinstance(progress, dict) else None
        candidate = _normalize_interrupt_question_batch(interrupt)
        if not candidate:
            continue
        # Events are authoritative over a stale DTO snapshot.  Within the
        # replay, prefer the latest batch; if a retry emitted a shorter
        # truncated copy, keep the larger complete batch instead.
        if (
            not best_batch
            or len(candidate) > len(best_batch)
            or (len(candidate) == len(best_batch) and (best_source == 0 or event_order >= best_order))
        ):
            best_batch = candidate
            best_source = 1
            best_order = event_order

    return best_batch


async def _solution_context_for_run(
    *,
    run_id: str,
    current_user: User,
) -> tuple[str, SendMessageRequest]:
    """Recover the original product request across any resume chain.

    ``resume`` runs intentionally store only the user's answer.  The product
    projection still needs the original conversation and attachment context,
    so walk ``created_by_run_id`` back to the initial chat run.  This keeps
    reconnects and multi-step ``ask_user_question`` flows idempotent without
    introducing a second run state machine.
    """
    runs: list[dict] = []
    current_id = run_id
    visited: set[str] = set()
    async with pg_manager.get_async_session_context() as db:
        while current_id and current_id not in visited:
            visited.add(current_id)
            view = await get_agent_run_view(run_id=current_id, current_uid=str(current_user.uid), db=db)
            run = view.get("run") or {}
            if not isinstance(run, dict):
                break
            runs.append(run)
            parent_id = run.get("created_by_run_id")
            if str(run.get("run_type") or "") != "resume" or not parent_id:
                break
            current_id = str(parent_id)

    if not runs:
        raise HTTPException(status_code=404, detail="方案运行上下文不存在")

    root = runs[-1]
    root_input = str(root.get("input_content") or "").strip()
    if not root_input:
        raise HTTPException(status_code=409, detail="方案运行缺少原始请求")

    root_metadata = root.get("input_metadata")
    metadata = root_metadata if isinstance(root_metadata, dict) else {}
    invocation = metadata.get("agent_invocation_meta")
    invocation_meta = invocation if isinstance(invocation, dict) else {}
    thread_id = str(root.get("conversation_thread_id") or "").strip()
    conversation_id = str(
        invocation_meta.get("product_conversation_id") or thread_id.removeprefix("product-")
    ).strip()
    if not conversation_id:
        raise HTTPException(status_code=409, detail="方案运行缺少产品会话上下文")

    # Runs are collected newest -> oldest.  Present answers in their original
    # order so a second clarification cannot appear before the first one.
    answers: list[str] = []
    for run in reversed(runs[:-1]):
        input_metadata = run.get("input_metadata")
        input_metadata = input_metadata if isinstance(input_metadata, dict) else {}
        display_answer = input_metadata.get("resume_display_answer")
        if isinstance(display_answer, str) and display_answer.strip():
            answers.append(display_answer.strip())
            continue
        if answer := _resume_answer_text(run.get("input_content")):
            answers.append(answer)
    content = root_input
    if answers:
        content = f"{content}\n\n补充信息：\n" + "\n".join(answers)

    # ``attachment_file_ids`` is the legacy product adapter field.  Fall back
    # to it for runs created before ``product_attachment_ids`` was added so a
    # pending clarification does not silently lose its uploaded files.
    raw_attachment_ids = invocation_meta.get("product_attachment_ids")
    if not isinstance(raw_attachment_ids, list):
        raw_attachment_ids = metadata.get("attachment_file_ids")
    attachment_ids = (
        [item for item in raw_attachment_ids if isinstance(item, str) and item.strip()]
        if isinstance(raw_attachment_ids, list)
        else []
    )
    # ``SendMessageRequest`` deliberately accepts camelCase aliases at the
    # API boundary.  Use the same validation path here instead of constructing
    # it with snake_case keyword arguments (which ``extra=forbid`` rejects).
    return conversation_id, SendMessageRequest.model_validate(
        {
            "content": content,
            "skillId": "SOLUTION_DRAFT",
            "attachmentIds": attachment_ids,
            "requestId": str(root.get("request_id") or "") or None,
        }
    )


async def _project_solution_run(
    *,
    run_id: str,
    conversation_id: str,
    request: SendMessageRequest,
    current_user: User,
) -> MessageExchangeResponse:
    result = await load_agent_run_result(run_id=run_id, current_uid=str(current_user.uid))
    # The durable run result is usually assistant text, but structured Agent
    # backends may already return a decoded mapping.  Preserve that mapping so
    # the schema validator can consume it without lossy ``str(dict)`` parsing.
    # Pass the complete result envelope so the bridge can handle both the
    # durable plain-text output and structured/LangChain message wrappers.
    extraction = extract_solution_result(result)
    if extraction.status is SolutionExtractionStatus.EMPTY:
        # Do not create a misleading empty BLOCKED card.  The caller can
        # safely retry the same request/run and the run id makes diagnosis
        # possible without exposing internal model output.
        raise HTTPException(
            status_code=502,
            detail={
                "code": "AGENT_EMPTY_RESULT",
                "message": "Agent 未返回有效方案结果，请重试",
                "runId": run_id,
                "retryable": True,
            },
        )
    payload = extraction.payload
    if payload is None:
        # A malformed Blueprint is safe to persist only when a deterministic
        # diagnostic can be shown to the user.
        from yuxi.product_chat.solution_draft import blocked_solution_draft

        payload = blocked_solution_draft(
            extraction.reason or "方案结构化结果无法校验",
            request.content,
        )
    payload.execution_trace = result.get("execution_trace") if isinstance(result.get("execution_trace"), dict) else {}
    run_status = str(result.get("status") or "").strip().lower()
    if run_status not in {"completed", "succeeded", "success"}:
        from yuxi.product_chat.solution_draft import blocked_solution_draft

        payload = blocked_solution_draft(
            f"方案运行未完成：{result.get('status') or 'failed'}",
            request.content,
        )
        payload.execution_trace = (
            result.get("execution_trace")
            if isinstance(result.get("execution_trace"), dict)
            else {}
        )
        payload.evidence_gaps = [f"方案运行未完成：{result.get('status') or 'failed'}"]
    answer_status = _solution_answer_status(payload)
    answer = GroundedAnswer(
        status=answer_status,
        content=render_solution_draft(payload),
        citations=(),
        model_version="yuxi-solution-draft-agent",
        prompt_version="solution-draft-v1",
    )
    async with pg_manager.get_async_session_context() as db:
        chat_repository = ProductChatRepository(db)
        conversation = await chat_repository.require_conversation(conversation_id, current_user.id)
        draft_repository = SolutionDraftRepository(db)
        draft = await draft_repository.create_from_run(
            conversation_id=conversation_id,
            source_run_id=run_id,
            payload=payload,
        )
        existing_exchange = await chat_repository.get_exchange_for_solution_draft(
            draft.id,
            current_user.id,
        )
        if existing_exchange is not None:
            user_message, assistant_message, assistant_citations = existing_exchange
            # A retry/reconnect may revisit a draft projected before the
            # structured-output compatibility fix.  Keep the same exchange
            # and run id, but append a corrected immutable draft version.
            if _blocked_draft_needs_payload_repair(draft, payload):
                draft = await draft_repository.refresh_blocked_from_payload(
                    draft_id=draft.id,
                    user_id=current_user.id,
                    payload=payload,
                )
                assistant_message.content = render_solution_draft(payload)
                assistant_message.answer_status = answer_status
                await db.flush()
            await db.commit()
            stored_conversation = await chat_repository.require_conversation(conversation_id, current_user.id)
            message_count = (await chat_repository.get_message_counts([conversation_id])).get(conversation_id, 0)
            return MessageExchangeResponse(
                conversation=_conversation_response(stored_conversation, message_count),
                user_message=_message_response(user_message, []),
                assistant_message=_message_response(
                    assistant_message,
                    assistant_citations,
                    solution_draft=serialize_solution_draft(
                        draft,
                        await draft_repository.list_versions(draft.id),
                    ),
                ),
            )
        result_metadata = (
            result.get("input_metadata") if isinstance(result.get("input_metadata"), dict) else {}
        )
        display_content = (
            result_metadata.get("resume_display_answer")
            if isinstance(result_metadata.get("resume_display_answer"), str)
            else None
        )
        user_message, assistant_message, assistant_citations = await chat_repository.append_exchange(
            conversation,
            current_user.id,
            display_content or request.content,
            answer,
            solution_draft_id=draft.id,
            # ``_create_solution_run`` generates a request id for legacy
            # clients that do not send one.  Reuse the durable run id here so
            # the exchange remains idempotent across a retry/projection.
            request_id=request.request_id or str(result.get("request_id") or f"run:{run_id}"),
            skill_id="SOLUTION_DRAFT",
        )
        await db.commit()
        stored_conversation = await chat_repository.require_conversation(conversation_id, current_user.id)
        message_count = (await chat_repository.get_message_counts([conversation_id])).get(conversation_id, 0)
        draft_data = serialize_solution_draft(draft, await draft_repository.list_versions(draft.id))
        return MessageExchangeResponse(
            conversation=_conversation_response(stored_conversation, message_count),
            user_message=_message_response(user_message, []),
            assistant_message=_message_response(
                assistant_message,
                assistant_citations,
                solution_draft=draft_data,
            ),
        )


def _agent_progress(raw_event: str) -> dict | None:
    """Translate Yuxi runtime events without exposing chain-of-thought or tool args."""
    event_name = ""
    data: dict = {}
    for line in raw_event.splitlines():
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            try:
                data = json.loads(line.split(":", 1)[1].strip())
            except json.JSONDecodeError:
                data = {}
    if event_name == "error":
        return {"error": data.get("message") or "方案运行暂时不可用"}
    if event_name == "interrupt":
        envelope = data if isinstance(data, dict) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else envelope
        chunk = payload.get("chunk") if isinstance(payload.get("chunk"), dict) else payload
        # A few older workers emitted a single question directly on the
        # interrupt chunk, while current workers always use ``questions``.
        # Normalize both shapes here so a legacy run still exposes its
        # options and question id instead of falling back to a generic prompt.
        questions = chunk.get("questions") if isinstance(chunk, dict) else []
        if not isinstance(questions, list) and isinstance(chunk, dict):
            raw_single = questions if isinstance(questions, dict) else chunk
            questions = [raw_single] if any(
                key in raw_single for key in ("question", "prompt", "text")
            ) else []
        normalized_questions = []
        if isinstance(questions, list):
            for index, item in enumerate(questions):
                if not isinstance(item, dict):
                    continue
                question = str(item.get("question") or item.get("prompt") or item.get("text") or "").strip()
                if not question:
                    continue
                question_id = str(
                    item.get("question_id") or item.get("questionId") or item.get("id") or f"q-{index + 1}"
                ).strip()
                options = item.get("options") or item.get("choices")
                normalized_options = []
                if isinstance(options, list):
                    for option in options:
                        if isinstance(option, dict):
                            label = str(
                                option.get("label")
                                or option.get("text")
                                or option.get("name")
                                or option.get("value")
                                or option.get("id")
                                or ""
                            ).strip()
                            value = str(
                                option.get("id")
                                or option.get("value")
                                or option.get("key")
                                or option.get("label")
                                or option.get("text")
                                or ""
                            ).strip()
                        else:
                            label = str(option).strip()
                            value = label
                        if label and value:
                            normalized_options.append({"id": value, "label": label})
                normalized_questions.append({
                    "id": question_id,
                    "questionId": question_id,
                    "question": question,
                    "type": (
                        "MULTIPLE_CHOICE"
                        if item.get("multi_select", item.get("multiSelect", False))
                        else ("SINGLE_CHOICE" if normalized_options else "TEXT")
                    ),
                    **({"options": normalized_options} if normalized_options else {}),
                    "required": item.get("required", True) is not False,
                    # ``allow_other`` controls whether the user may enter a
                    # free-form "other" value; it must not accidentally
                    # disable the independent skip affordance.  Older
                    # payloads used ``allow_skip``/``skippable`` while newer
                    # ones may use camelCase, so accept all three spellings.
                    "allowSkip": item.get(
                        "allow_skip",
                        item.get("allowSkip", item.get("skippable", True)),
                    ) is not False,
                    "allowOther": item.get(
                        "allow_other",
                        item.get("allowOther", True),
                    ) is not False,
                    "position": index + 1,
                    "total": len(questions),
                })
        first = normalized_questions[0] if normalized_questions else None
        interrupt = {
            "question": (
                first["question"]
                if first
                else (chunk.get("message") if isinstance(chunk, dict) else None) or "请补充所需信息"
            ),
            **(
                {
                    "questionId": first["questionId"],
                    "type": first["type"],
                    "options": first.get("options", []),
                    "required": first["required"],
                    "allowSkip": first["allowSkip"],
                    "position": first["position"],
                    "total": first["total"],
                }
                if first
                else {}
            ),
            **({"questions": normalized_questions} if normalized_questions else {}),
        }
        return {
            "interrupt": interrupt,
        }
    if event_name == "end":
        envelope = data if isinstance(data, dict) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else envelope
        terminal_status = str(payload.get("status") or "")
        if terminal_status and terminal_status != "completed":
            return {"terminalStatus": terminal_status}
        return {"stage": "COMPOSING", "message": "正在校验并保存方案草稿", "terminalStatus": "completed"}
    if event_name not in {"messages", "custom"}:
        return None
    payload = data.get("payload") if isinstance(data, dict) else None
    delta = ""
    chunks: list[dict] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("chunk"), dict):
            chunks.append(payload["chunk"])
        if isinstance(payload.get("items"), list):
            chunks.extend(item for item in payload["items"] if isinstance(item, dict))
        for chunk in chunks:
            stream_event = chunk.get("stream_event")
            if isinstance(stream_event, dict) and stream_event.get("type") == "message_delta":
                # The runtime separates normal content from reasoning content;
                # only the former is eligible for a product-facing preview.
                value = stream_event.get("content")
                if isinstance(value, str):
                    delta += value
    for chunk in chunks:
        stage = progress_stage_from_chunk(chunk)
        if stage:
            return {"stage": stage[0], "message": stage[1], "delta": delta}
    return {"delta": delta} if delta else None


def _solution_safe_stream_delta(content: str, state: dict[str, object]) -> str:
    """Extract only human-readable Blueprint field text from partial JSON.

    Structured output is deliberately persisted and rendered only after full
    validation.  During generation we can still provide useful streaming
    feedback by exposing text already inside known display fields, while never
    forwarding JSON punctuation, tool arguments, or reasoning content.
    """
    if not content:
        return ""
    raw = str(state.get("raw_output") or "") + content
    state["raw_output"] = raw
    emitted = state.setdefault("field_emitted", {})
    if not isinstance(emitted, dict):
        emitted = {}
        state["field_emitted"] = emitted
    chunks: list[tuple[int, str]] = []
    field_pattern = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"')
    for match in field_pattern.finditer(raw):
        field = match.group(1)
        if field not in _SOLUTION_STREAM_FIELDS:
            continue
        start = match.end()
        index = start
        escaped = False
        while index < len(raw):
            char = raw[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                break
            index += 1
        fragment = raw[start:index]
        decoded = ""
        # A partial escape at the end of a token is not decodable yet.  Drop
        # only that unfinished suffix and retry; the next token will complete it.
        for end in range(len(fragment), max(-1, len(fragment) - 3), -1):
            try:
                decoded = json.loads(f'"{fragment[:end]}"')
                break
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        previous = int(emitted.get(str(start), 0) or 0)
        if len(decoded) > previous:
            chunks.append((start, decoded[previous:]))
            emitted[str(start)] = len(decoded)
    chunks.sort(key=lambda item: item[0])
    return "".join(value for _, value in chunks)


def _solution_stream_chunks(content: str) -> list[str]:
    """Split one aggregated model delta into independently paintable pieces."""
    if not content:
        return []
    chunks: list[str] = []
    remaining = content
    while len(remaining) > _SOLUTION_STREAM_CHUNK_MAX_CHARS:
        cut = _SOLUTION_STREAM_CHUNK_MAX_CHARS
        for index in range(
            _SOLUTION_STREAM_CHUNK_MAX_CHARS,
            min(_SOLUTION_STREAM_CHUNK_MIN_CHARS, _SOLUTION_STREAM_CHUNK_MAX_CHARS) - 1,
            -1,
        ):
            if remaining[index - 1] in _SOLUTION_STREAM_BOUNDARIES:
                cut = index
                break
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _solution_progress_events(progress: dict, state: dict[str, object]) -> list[tuple[str, dict]]:
    """Return one event-ordered, product-safe progress event.

    Progress belongs in the collapsible execution process. It must never be
    repeated as a synthetic answer delta because that mixes runtime state into
    the draft body. Only adjacent duplicates are collapsed; a later return to
    an earlier action remains visible as part of the real Agent trajectory.
    """
    stage = str(progress.get("stage") or "").strip()
    message = str(progress.get("message") or "").strip()
    if not stage or not message:
        return []

    current_stage = str(state.get("stage") or "")
    current_message = str(state.get("message") or "")
    if current_stage == stage and current_message == message:
        return []

    state.update(stage=stage, message=message)
    return [("progress", {"stage": stage, "message": message})]


def _material_http_error(error: CitationResolutionError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


def _feedback_governance_reason(message: ProductMessage) -> str:
    labels = {
        "CONTENT_ERROR": "内容错误",
        "OUTDATED": "内容过时",
        "MISSING_SOURCE": "资料缺失",
        "CITATION_ERROR": "引用错误",
        "OTHER": "其他",
    }
    label = labels.get(message.feedback_reason_type, "未分类")
    detail = f"；补充说明：{message.feedback_reason_text}" if message.feedback_reason_text else ""
    return f"助手回答被用户标记为不满意（{label}，消息 {message.message_id}）{detail}"


@product_chat.get("/chat/conversations", response_model=ConversationListResponse)
async def list_conversations(
    current_user: User = Depends(get_product_user),
) -> ConversationListResponse:
    async with pg_manager.get_async_session_context() as db:
        repository = ProductChatRepository(db)
        conversations = await repository.list_conversations(current_user.id)
        counts = await repository.get_message_counts([conversation.conversation_id for conversation in conversations])
        return ConversationListResponse(
            conversations=[
                _conversation_response(
                    conversation,
                    counts.get(conversation.conversation_id, 0),
                )
                for conversation in conversations
            ]
        )


@product_chat.post(
    "/chat/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    request: CreateConversationRequest,
    current_user: User = Depends(get_product_user),
) -> ConversationResponse:
    async with pg_manager.get_async_session_context() as db:
        conversation = await ProductChatRepository(db).create_conversation(
            current_user.id,
            request.title or "",
        )
        return ConversationResponse(conversation=_conversation_response(conversation, 0))


@product_chat.post(
    "/chat/conversations/{conversation_id}/attachments",
    status_code=status.HTTP_201_CREATED,
)
async def upload_solution_attachment(
    conversation_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_product_user),
) -> dict:
    """Upload a session attachment and bind it to the stable solution thread."""
    try:
        async with pg_manager.get_async_session_context() as db:
            await ProductChatRepository(db).require_conversation(conversation_id, current_user.id)
            thread_id = await _ensure_solution_thread(
                db,
                conversation_id=conversation_id,
                current_user=current_user,
            )
            uploaded = await upload_thread_attachment_view(
                thread_id=thread_id,
                file=file,
                db=db,
                current_uid=str(current_user.uid),
            )
            return {
                "attachment": {
                    "id": uploaded["file_id"],
                    "name": uploaded["file_name"],
                    "mimeType": uploaded.get("file_type") or "application/octet-stream",
                    "size": uploaded.get("file_size", 0),
                    "status": "READY" if uploaded.get("status") in {"uploaded", "parsed"} else "FAILED",
                }
            }
    except ProductChatNotFoundError:
        raise _not_found() from None


@product_chat.get(
    "/chat/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
)
async def get_conversation(
    conversation_id: str,
    current_user: User = Depends(get_product_user),
) -> ConversationDetailResponse:
    try:
        async with pg_manager.get_async_session_context() as db:
            repository = ProductChatRepository(db)
            conversation = await repository.require_viewable_conversation(
                conversation_id,
                current_user.id,
            )
            messages = await repository.list_messages_with_citations(conversation_id)
            message_responses: list[MessageResponse] = []
            preceding_user_content = ""
            material_service = ProductMaterialService(db)
            for message, citations in messages:
                materials: list[ProductMaterialResponse] = []
                if message.role == "USER":
                    preceding_user_content = message.content
                elif citations and _uses_material_search(preceding_user_content):
                    materials = await material_service.list_from_citations(citations, current_user)
                draft_data = None
                if message.solution_draft_id:
                    draft_repository = SolutionDraftRepository(db)
                    draft = await draft_repository.get_for_user(message.solution_draft_id, current_user.id)
                    if draft:
                        # Repair historical projections lazily on read.  This
                        # is safe because only completed runs can be promoted
                        # and the repository appends a new immutable version.
                        if str(draft.status or "").upper() == "BLOCKED" and draft.source_run_id:
                            # Historical drafts may outlive their AgentRun (or
                            # the run service may be temporarily unavailable).
                            # A best-effort repair must never turn a normal
                            # conversation load into a 5xx response.  Keep the
                            # persisted draft and its actionable requirements
                            # when the source run cannot be read.
                            try:
                                run_result = await get_agent_run_result(
                                    run_id=draft.source_run_id,
                                    current_uid=str(current_user.uid),
                                    db=db,
                                )
                            except Exception as exc:  # pragma: no cover - backend/service dependent
                                logger.warning(
                                    "跳过历史方案草稿修复：source_run_id=%s error=%s",
                                    draft.source_run_id,
                                    exc,
                                )
                                run_result = None
                            if isinstance(run_result, dict) and str(run_result.get("status") or "").strip().lower() in {
                                "completed",
                                "succeeded",
                                "success",
                            }:
                                extraction = extract_solution_result(run_result)
                                candidate = extraction.payload
                                if (
                                    extraction.status is SolutionExtractionStatus.VALID
                                    and candidate is not None
                                    and _blocked_draft_needs_payload_repair(draft, candidate)
                                ):
                                    draft = await draft_repository.refresh_blocked_from_payload(
                                        draft_id=draft.id,
                                        user_id=current_user.id,
                                        payload=candidate,
                                    )
                                    message.content = render_solution_draft(candidate)
                                    message.answer_status = _solution_answer_status(candidate)
                                    await db.flush()
                        draft_data = serialize_solution_draft(draft, await draft_repository.list_versions(draft.id))
                message_responses.append(_message_response(message, citations, materials, draft_data))
            return ConversationDetailResponse(
                conversation=_conversation_response(conversation, len(messages)),
                messages=message_responses,
            )
    except ProductChatNotFoundError:
        raise _not_found() from None


@product_chat.post(
    "/chat/conversations/{conversation_id}/messages",
    response_model=MessageExchangeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    conversation_id: str,
    request: SendMessageRequest,
    current_user: User = Depends(get_product_user),
) -> MessageExchangeResponse | JSONResponse:
    if request.skill_id == "SOLUTION_DRAFT":
        try:
            run = await _create_solution_run(
                conversation_id=conversation_id,
                request=request,
                current_user=current_user,
            )
            await await_agent_run_result(run_id=run["run_id"], current_uid=str(current_user.uid))
            return await _project_solution_run(
                run_id=run["run_id"],
                conversation_id=conversation_id,
                request=request,
                current_user=current_user,
            )
        except ProductChatNotFoundError:
            raise _not_found() from None
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "product_solution_draft_failed conversation_id={} error_type={}",
                conversation_id,
                type(exc).__name__,
            )
            return _knowledge_unavailable(conversation_id, exc)

    answer_service: AnswerService | None = None
    initialization_error: Exception | None = None
    try:
        async with pg_manager.get_async_session_context() as read_db:
            conversation = await ProductChatRepository(read_db).require_conversation(
                conversation_id,
                current_user.id,
            )
            try:
                answer_service = AnswerService(
                    db=read_db,
                    read_session_factory=pg_manager.AsyncSession,
                )
            except Exception as exc:
                initialization_error = exc
    except ProductChatNotFoundError:
        raise _not_found() from None
    except Exception as exc:
        return _knowledge_unavailable(conversation_id, exc)

    if initialization_error is not None:
        return _knowledge_unavailable(conversation_id, initialization_error)

    assert answer_service is not None
    try:
        answer = await answer_service.answer(
            _knowledge_question(request.content),
            current_user,
            conversation_id,
            mode=request.mode,
        )
    except Exception as exc:
        return _knowledge_unavailable(conversation_id, exc)

    try:
        async with pg_manager.get_async_session_context() as write_db:
            repository = ProductChatRepository(write_db)
            user_message, assistant_message, assistant_citations = await repository.append_exchange(
                conversation,
                current_user.id,
                request.content,
                answer,
                request_id=request.request_id,
                skill_id=request.skill_id,
            )
            assistant_materials = (
                await ProductMaterialService(write_db).list_from_citations(assistant_citations, current_user)
                if _uses_material_search(request.content, request.skill_id)
                else []
            )
            stored_conversation = await repository.require_conversation(
                conversation_id,
                current_user.id,
            )
            message_count = (await repository.get_message_counts([conversation_id])).get(conversation_id, 0)
            return MessageExchangeResponse(
                conversation=_conversation_response(
                    stored_conversation,
                    message_count,
                ),
                user_message=_message_response(user_message, []),
                assistant_message=_message_response(assistant_message, assistant_citations, assistant_materials),
            )
    except ProductChatNotFoundError:
        raise _not_found() from None
    except Exception as exc:
        return _knowledge_unavailable(conversation_id, exc)


@product_chat.post(
    "/chat/conversations/{conversation_id}/messages/stream",
    response_model=None,
)
async def stream_message(
    conversation_id: str,
    request: SendMessageRequest,
    current_user: User = Depends(get_product_user),
) -> StreamingResponse | JSONResponse:
    if request.skill_id == "SOLUTION_DRAFT":
        try:
            run = await _create_solution_run(
                conversation_id=conversation_id,
                request=request,
                current_user=current_user,
            )
        except ProductChatNotFoundError:
            raise _not_found() from None
        except HTTPException:
            raise
        except Exception as exc:
            return _knowledge_unavailable(conversation_id, exc)

        async def solution_event_stream() -> AsyncIterator[str]:
            yield _sse_event("run_started", {"runId": run["run_id"], "status": run.get("status")})
            progress_state: dict[str, object] = {}
            for event_name, payload in _solution_progress_events(
                {"stage": "UNDERSTANDING", "message": "正在分析需求并规划方案"},
                progress_state,
            ):
                yield _sse_event(event_name, payload)
            try:
                interrupted_question: dict | None = None
                terminal_status = ""
                stream_state: dict[str, object] = {}
                async for raw_event in stream_agent_run_events(
                    run_id=run["run_id"],
                    after_seq="0-0",
                    current_uid=str(current_user.uid),
                    verbose=False,
                ):
                    progress = _agent_progress(raw_event)
                    if not progress:
                        continue
                    if progress.get("error"):
                        yield _sse_event("error", {"code": "AGENT_RUN_FAILED", "message": progress["error"]})
                        return
                    if progress.get("interrupt"):
                        value = progress["interrupt"]
                        interrupted_question = value if isinstance(value, dict) else {"question": str(value)}
                    if progress.get("terminalStatus"):
                        terminal_status = str(progress["terminalStatus"])
                    raw_delta = progress.get("delta")
                    if isinstance(raw_delta, str):
                        safe_delta = _solution_safe_stream_delta(raw_delta, stream_state)
                        if safe_delta:
                            chunks = _solution_stream_chunks(safe_delta)
                            for index, chunk in enumerate(chunks):
                                yield _sse_event("delta", {"content": chunk})
                                if index < len(chunks) - 1:
                                    await asyncio.sleep(0.012)
                    for event_name, payload in _solution_progress_events(progress, progress_state):
                        yield _sse_event(event_name, payload)
                if terminal_status == "interrupted" or interrupted_question:
                    yield _sse_event(
                        "interrupt",
                        {
                            "runId": run["run_id"],
                            **(interrupted_question or {"question": "请补充所需信息"}),
                            "status": "INTERRUPTED",
                        },
                    )
                    return
                if terminal_status and terminal_status != "completed":
                    yield _sse_event("error", {"code": "AGENT_RUN_FAILED", "message": "方案运行未完成，请重试"})
                    return
                response = await _project_solution_run(
                    run_id=run["run_id"],
                    conversation_id=conversation_id,
                    request=request,
                    current_user=current_user,
                )
                yield _sse_event("draft", response.assistant_message.solution_draft or {})
                yield _sse_event("complete", response.model_dump(mode="json", by_alias=True))
            except Exception as exc:
                logger.error(
                    "product_solution_draft_stream_failed conversation_id={} error_type={}",
                    conversation_id,
                    type(exc).__name__,
                )
                if isinstance(exc, HTTPException):
                    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
                    yield _sse_event("error", detail)
                else:
                    yield _sse_event(
                        "error",
                        {
                            "code": "AGENT_RUN_FAILED",
                            "message": "方案草稿生成失败，请重试",
                            "retryable": True,
                        },
                    )

        return StreamingResponse(
            solution_event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    try:
        async with pg_manager.get_async_session_context() as read_db:
            conversation = await ProductChatRepository(read_db).require_conversation(
                conversation_id,
                current_user.id,
            )
            answer_service = AnswerService(
                db=read_db,
                read_session_factory=pg_manager.AsyncSession,
            )
    except ProductChatNotFoundError:
        raise _not_found() from None
    except Exception as exc:
        return _knowledge_unavailable(conversation_id, exc)

    async def event_stream() -> AsyncIterator[str]:
        answer: GroundedAnswer | None = None
        try:
            async for event in answer_service.answer_events(
                _knowledge_question(request.content),
                current_user,
                conversation_id,
                mode=request.mode,
            ):
                if isinstance(event, AnswerProgress):
                    yield _sse_event(
                        "progress",
                        {"stage": event.stage, "message": event.message},
                    )
                elif isinstance(event, AnswerDelta):
                    yield _sse_event("delta", {"content": event.content})
                else:
                    answer = event

            if answer is None:
                raise RuntimeError("Answer orchestration completed without a result")

            async with pg_manager.get_async_session_context() as write_db:
                repository = ProductChatRepository(write_db)
                user_message, assistant_message, assistant_citations = await repository.append_exchange(
                    conversation,
                    current_user.id,
                    request.content,
                    answer,
                    request_id=request.request_id,
                    skill_id=request.skill_id,
                )
                assistant_materials = (
                    await ProductMaterialService(write_db).list_from_citations(assistant_citations, current_user)
                    if _uses_material_search(request.content, request.skill_id)
                    else []
                )
                stored_conversation = await repository.require_conversation(
                    conversation_id,
                    current_user.id,
                )
                message_count = (await repository.get_message_counts([conversation_id])).get(conversation_id, 0)
                response = MessageExchangeResponse(
                    conversation=_conversation_response(stored_conversation, message_count),
                    user_message=_message_response(user_message, []),
                    assistant_message=_message_response(assistant_message, assistant_citations, assistant_materials),
                )
            yield _sse_event("complete", response.model_dump(mode="json", by_alias=True))
        except ProductChatNotFoundError:
            yield _sse_event(
                "error",
                {"code": "CONVERSATION_NOT_FOUND", "message": "会话不存在"},
            )
        except Exception as exc:
            logger.error(
                "product_chat_stream_failed conversation_id={} error_type={}",
                conversation_id,
                type(exc).__name__,
            )
            yield _sse_event(
                "error",
                {
                    "code": "KNOWLEDGE_SERVICE_UNAVAILABLE",
                    "message": "知识服务暂时不可用，请稍后重试",
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@product_chat.get("/chat/conversations/{conversation_id}/active-run")
async def get_active_product_chat_run(
    conversation_id: str,
    current_user: User = Depends(get_product_user),
) -> dict:
    async with pg_manager.get_async_session_context() as db:
        try:
            await ProductChatRepository(db).require_conversation(conversation_id, current_user.id)
        except ProductChatNotFoundError:
            raise _not_found() from None
        run = await AgentRunRepository(db).get_latest_run_by_thread_for_user(
            f"product-{conversation_id}", str(current_user.uid),
        )
        if (
            not run or run.agent_slug != SOLUTION_DRAFT_AGENT_SLUG
            or run.status not in {"pending", "running", "cancel_requested", "interrupted"}
        ):
            return {"run": None}
        run_id = run.id
    return await get_product_chat_run(run_id, current_user)


@product_chat.get("/chat/runs/{run_id}")
async def get_product_chat_run(
    run_id: str,
    current_user: User = Depends(get_product_user),
) -> dict:
    try:
        async with pg_manager.get_async_session_context() as db:
            result = await get_agent_run_view(run_id=run_id, current_uid=str(current_user.uid), db=db)
            run = result.get("run") or {}
            return {
                "run": {
                    "runId": run.get("run_id") or run.get("id"),
                    "threadId": run.get("thread_id") or run.get("conversation_thread_id"),
                    "status": run.get("status"),
                    "requestId": run.get("request_id"),
                    "inputContent": run.get("input_content") or "",
                    "executionTrace": run.get("execution_trace") or {},
                    "interrupt": run.get("interrupt"),
                    "streamUrl": f"/api/chat/runs/{run_id}/events",
                }
            }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="运行任务不存在") from None


@product_chat.post("/chat/runs/{run_id}/resume")
async def resume_product_chat_run(
    run_id: str,
    request: ResumeRunRequest,
    current_user: User = Depends(get_product_user),
) -> dict:
    """Resume an interrupted LangGraph run with the user's answer."""
    if request.answer is None:
        raise HTTPException(status_code=422, detail="answer 不能为空")
    try:
        encoded_answer = json.dumps(request.answer, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="answer 格式不受支持") from exc
    if len(encoded_answer) > 20_000:
        raise HTTPException(status_code=422, detail="answer 过长")

    async with pg_manager.get_async_session_context() as db:
        parent = await AgentRunRepository(db).get_run_for_user(run_id, str(current_user.uid))
        if not parent:
            raise HTTPException(status_code=404, detail="运行任务不存在")
        parent_status = str(parent.status or "").strip().lower()
        if parent_status != "interrupted":
            # Older solution runs could finish with a BLOCKED draft and still
            # expose a deterministic clarification question.  They cannot be
            # resumed through LangGraph's checkpoint API, so continue the
            # original request as a new chat run on the same stable thread.
            # Keep this compatibility path narrow: arbitrary completed runs
            # and non-solution agents must retain the normal 409 contract.
            if parent_status not in {"completed", "succeeded", "success"}:
                raise HTTPException(status_code=409, detail="只有 interrupted run 可以恢复")
            parent_view = await get_agent_run_view(
                run_id=run_id,
                current_uid=str(current_user.uid),
                db=db,
            )
            parent_data = parent_view.get("run") or {}
            metadata = parent_data.get("input_metadata") if isinstance(parent_data, dict) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            invocation = metadata.get("agent_invocation_meta")
            invocation_meta = invocation if isinstance(invocation, dict) else {}
            agent_slug = str(parent_data.get("agent_slug") or parent.agent_slug or "")
            skill_id = str(invocation_meta.get("skill_id") or "")
            if agent_slug != SOLUTION_DRAFT_AGENT_SLUG and skill_id != "SOLUTION_DRAFT":
                raise HTTPException(status_code=409, detail="只有方案草稿运行可以继续确认")

            # Completed compatibility runs do not have a LangGraph checkpoint
            # interrupt to replay. Recover the complete question batch from
            # the immutable draft projection before accepting an answer;
            # otherwise a scalar answer could silently advance a multi-question
            # batch and the Agent would ask the first question again.
            completed_draft = await SolutionDraftRepository(db).get_by_source_run(run_id)
            completed_questions = _draft_questions_for_resume(completed_draft)
            if not completed_questions and isinstance(parent_data.get("interrupt"), dict):
                completed_questions = _normalize_interrupt_question_batch(parent_data["interrupt"])
            if completed_questions:
                # Keep the completed-run fallback consistent with a true
                # interrupted LangGraph resume: every current question needs
                # an explicit answer or skip decision before we continue.
                _validated_resume_answer(request, completed_questions, run_id=run_id)

            # Recover the root request plus any answers already collected in a
            # previous resume chain, then append this answer in order.  The
            # new request id prevents the old completed run's idempotency key
            # from being reused.
            conversation_id, solution_request = await _solution_context_for_run(
                run_id=run_id,
                current_user=current_user,
            )
            # This compatibility branch continues an already-completed run
            # as a fresh chat run (there is no LangGraph interrupt to resume).
            # The runtime answer still needs the submitted option id when a
            # true resume is possible, but this new chat input is also used as
            # the user-facing history message.  Render it through the label
            # normalizer first so legacy values such as ``confirmed`` never
            # leak into the transcript.
            answer = _resume_context_answer(request, completed_questions)
            if not answer:
                answer = _resume_display_answer(request, completed_questions)
            if not answer:
                answer = _resume_request_answer(request)
            # Keep the canonical ``补充信息：`` marker so the product
            # projection can filter questions already answered in this
            # compatibility continuation.  The extra note is model context,
            # not a second transcript protocol.
            supplement_note = "以下信息已确认，请勿重复询问：\n" if completed_questions else ""
            content = f"{solution_request.content}\n\n补充信息：\n{supplement_note}{answer}"
            continued_request = SendMessageRequest.model_validate(
                {
                    "content": content,
                    "skillId": "SOLUTION_DRAFT",
                    "attachmentIds": solution_request.attachment_ids,
                    "requestId": request.request_id or str(uuid4()),
                }
            )
            run = await _create_solution_run(
                conversation_id=conversation_id,
                request=continued_request,
                current_user=current_user,
            )
            return {
                "run": {
                    "runId": run["run_id"],
                    "threadId": run["thread_id"],
                    "status": run["status"],
                    "requestId": run["request_id"],
                    "streamUrl": f"/api/chat/runs/{run['run_id']}/events",
                    "resumedFromRunId": run_id,
                }
            }
        parent_view = await get_agent_run_view(
            run_id=run_id,
            current_uid=str(current_user.uid),
            db=db,
        )
        parent_data = parent_view.get("run") or {}
        parent_metadata = parent_data.get("input_metadata") if isinstance(parent_data, dict) else {}
        parent_metadata = parent_metadata if isinstance(parent_metadata, dict) else {}
        invocation = parent_metadata.get("agent_invocation_meta")
        invocation_meta = invocation if isinstance(invocation, dict) else {}
        # Validate the complete interrupt batch before creating a resume run.
        # This keeps a partial answer from advancing LangGraph and makes the
        # product contract deterministic across browser refreshes/retries.
        interrupt_questions = await _interrupt_questions_for_run(
            run_id,
            current_user,
            parent_data,
        )
        resume_answer = _validated_resume_answer(request, interrupt_questions, run_id=run_id)
        resume_display_answer = _resume_display_answer(request, interrupt_questions)
        run = await create_agent_run_view(
            input_message=None,
            agent_slug=parent.agent_slug,
            thread_id=parent.conversation_thread_id,
            meta={
                "source": "product_chat_solution_draft_resume",
                "attachment_file_ids": parent_metadata.get("attachment_file_ids") or [],
                "agent_invocation_meta": invocation_meta,
                **({"resume_display_answer": resume_display_answer} if resume_display_answer else {}),
                **({"request_id": request.request_id} if request.request_id else {}),
            },
            current_uid=str(current_user.uid),
            db=db,
            resume=resume_answer,
            created_by_run_id=run_id,
        )
    return {
        "run": {
            "runId": run["run_id"],
            "threadId": run["thread_id"],
            "status": run["status"],
            "requestId": run["request_id"],
            "streamUrl": f"/api/chat/runs/{run['run_id']}/events",
            "resumedFromRunId": run_id,
        }
    }


@product_chat.get("/chat/runs/{run_id}/events")
async def stream_product_chat_run_events(
    run_id: str,
    current_user: User = Depends(get_product_user),
):
    async def events() -> AsyncIterator[str]:
        # Keep the product-facing stream contract identical for initial and
        # resumed runs.  The browser can immediately bind its cancel action to
        # the durable run id while the event backlog is being replayed.
        yield _sse_event("run_started", {"runId": run_id, "status": "running"})
        progress_state: dict[str, object] = {}
        for event_name, payload in _solution_progress_events(
            {"stage": "UNDERSTANDING", "message": "正在分析需求并规划方案"},
            progress_state,
        ):
            yield _sse_event(event_name, payload)
        interrupted_question: dict | None = None
        terminal_status = ""
        # The Redis stream is a live transport, not the durable source of
        # truth.  Seed the terminal state and clarification batch from the run
        # snapshot so reconnecting after stream expiry still renders the same
        # questions and validates the complete batch on resume.
        async with pg_manager.get_async_session_context() as db:
            initial_view = await get_agent_run_view(
                run_id=run_id,
                current_uid=str(current_user.uid),
                db=db,
            )
        initial_run = initial_view.get("run") or {}
        initial_status = str(initial_run.get("status") or "").strip().lower()
        persisted_interrupt = initial_run.get("interrupt")
        if initial_status == "interrupted":
            terminal_status = "interrupted"
        if isinstance(persisted_interrupt, dict):
            interrupted_question = persisted_interrupt
        stream_state: dict[str, object] = {}
        async for raw_event in stream_agent_run_events(
            run_id=run_id,
            after_seq="0-0",
            current_uid=str(current_user.uid),
            verbose=False,
        ):
            progress = _agent_progress(raw_event)
            if progress and not progress.get("error"):
                if progress.get("interrupt"):
                    value = progress["interrupt"]
                    interrupted_question = value if isinstance(value, dict) else {"question": str(value)}
                if progress.get("terminalStatus"):
                    terminal_status = str(progress["terminalStatus"])
                raw_delta = progress.get("delta")
                if isinstance(raw_delta, str):
                    safe_delta = _solution_safe_stream_delta(raw_delta, stream_state)
                    if safe_delta:
                        chunks = _solution_stream_chunks(safe_delta)
                        for index, chunk in enumerate(chunks):
                            yield _sse_event("delta", {"content": chunk})
                            if index < len(chunks) - 1:
                                await asyncio.sleep(0.012)
                for event_name, payload in _solution_progress_events(progress, progress_state):
                    yield _sse_event(event_name, payload)
            elif progress and progress.get("error"):
                yield _sse_event("error", {"code": "AGENT_RUN_FAILED", "message": progress["error"]})
                return
        if terminal_status == "interrupted" or interrupted_question:
            yield _sse_event(
                "interrupt",
                {
                    "runId": run_id,
                    **(interrupted_question or {"question": "请补充所需信息"}),
                    "status": "INTERRUPTED",
                },
            )
            return
        if terminal_status and terminal_status != "completed":
            yield _sse_event(
                "error",
                {
                    "code": "AGENT_RUN_FAILED",
                    "message": "方案运行未完成，请重试",
                },
            )
            return

        # A resumed run has no browser request body to project with.  Recover
        # the original product conversation, attachments and clarification
        # answers from the durable run chain before emitting the same terminal
        # events as the initial stream.
        try:
            conversation_id, request = await _solution_context_for_run(
                run_id=run_id,
                current_user=current_user,
            )
            response = await _project_solution_run(
                run_id=run_id,
                conversation_id=conversation_id,
                request=request,
                current_user=current_user,
            )
            yield _sse_event("draft", response.assistant_message.solution_draft or {})
            yield _sse_event("complete", response.model_dump(mode="json", by_alias=True))
        except Exception as exc:
            logger.error(
                "product_solution_draft_run_projection_failed run_id={} error_type={} traceback={}",
                run_id,
                type(exc).__name__,
                "".join(format_tb(exc.__traceback__)),
            )
            yield _sse_event(
                "error",
                {
                    "code": "AGENT_RUN_PROJECTION_FAILED",
                    "message": "方案草稿保存失败，请重试",
                    "runId": run_id,
                    "retryable": True,
                },
            )
    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@product_chat.post("/chat/runs/{run_id}/cancel")
async def cancel_product_chat_run(
    run_id: str,
    current_user: User = Depends(get_product_user),
) -> dict:
    async with pg_manager.get_async_session_context() as db:
        return await cancel_agent_run_view(run_id=run_id, current_uid=str(current_user.uid), db=db)


@product_chat.get("/chat/solution-drafts/{draft_id}")
async def get_solution_draft(draft_id: str, current_user: User = Depends(get_product_user)) -> dict:
    async with pg_manager.get_async_session_context() as db:
        repository = SolutionDraftRepository(db)
        draft = await repository.get_for_user(draft_id, current_user.id)
        if not draft:
            raise HTTPException(status_code=404, detail="方案草稿不存在")
        return {"draft": serialize_solution_draft(draft, await repository.list_versions(draft.id))}


@product_chat.patch("/chat/solution-drafts/{draft_id}")
async def patch_solution_draft(
    draft_id: str,
    request: SolutionDraftEditRequest,
    current_user: User = Depends(get_product_user),
) -> dict:
    async with pg_manager.get_async_session_context() as db:
        repository = SolutionDraftRepository(db)
        try:
            draft = await repository.update_for_user(
                draft_id=draft_id,
                user_id=current_user.id,
                patch=request.model_dump(exclude_none=True, by_alias=True),
            )
        except SolutionDraftNotFoundError:
            raise HTTPException(status_code=404, detail="方案草稿不存在") from None
        await db.commit()
        return {"draft": serialize_solution_draft(draft, await repository.list_versions(draft.id))}


@product_chat.get("/chat/materials/{material_id}/download")
async def download_material(
    material_id: str,
    current_user: User = Depends(get_product_user),
) -> StreamingResponse:
    """Download a currently governed material through the existing KB service."""

    try:
        async with pg_manager.get_async_session_context() as db:
            material = await ProductMaterialService(db).resolve(material_id, current_user)
            data = await knowledge_base.get_file_download(
                kb_id=material.source.target_kb_id,
                file_id=material.version.yuxi_file_id,
                variant="original",
            )
    except CitationResolutionError as exc:
        raise _material_http_error(exc) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "MATERIAL_GONE", "message": "资料暂不可下载"},
        ) from exc
    except Exception as exc:
        logger.error("product_material_download_failed material_id={} error_type={}", material_id, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "MATERIAL_DOWNLOAD_UNAVAILABLE", "message": "资料下载服务暂不可用"},
        ) from exc

    filename = data.get("filename") or material.response.file_name
    return StreamingResponse(
        io.BytesIO(data.get("content") or b""),
        media_type=data.get("media_type") or material.response.mime_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@product_chat.post(
    "/chat/materials/{material_id}/distributions",
    response_model=MaterialDistributionResponse,
)
async def prepare_material_distribution(
    material_id: str,
    request: MaterialDistributionRequest,
    current_user: User = Depends(get_product_user),
) -> MaterialDistributionResponse:
    """Create a safe, user-confirmed device-share preparation task.

    The API deliberately does not send to a contact or create a public link;
    the browser/device share sheet performs the final user-confirmed action.
    """

    if request.channel == "DINGTALK":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "CHANNEL_NOT_AVAILABLE", "message": "钉钉暂未接入，请选择微信或飞书"},
        )
    try:
        async with pg_manager.get_async_session_context() as db:
            material = await ProductMaterialService(db).resolve(material_id, current_user)
    except CitationResolutionError as exc:
        raise _material_http_error(exc) from None

    now = utc_isoformat()
    task_id = f"DST-{uuid4().hex[:24].upper()}"
    share_text = (
        f"{material.response.title}\n\n{material.response.summary or '企业正式资料'}\n\n"
        f"来源：{material.response.citation.path or '飞书知识库'}"
    )
    return MaterialDistributionResponse(
        distribution=MaterialDistributionTaskResponse(
            id=task_id,
            material_id=material_id,
            requester_id=str(current_user.id),
            channel=request.channel,
            mode="DEVICE_SHARE",
            status="READY",
            created_at=now,
        ),
        title=material.response.title,
        text=share_text,
        download_url=f"/api/chat/materials/{quote(material_id, safe='')}/download",
        requires_user_confirmation=True,
    )


@product_chat.put(
    "/chat/messages/{message_id}/feedback",
    response_model=MessageFeedbackResponse,
    response_model_exclude_none=True,
)
async def set_message_feedback(
    message_id: str,
    request: MessageFeedbackRequest,
    current_user: User = Depends(get_product_user),
) -> MessageFeedbackResponse:
    try:
        async with pg_manager.get_async_session_context() as db:
            message = await ProductChatRepository(db).set_message_feedback(
                message_id,
                current_user.id,
                request.rating,
                reason_type=request.reason_type,
                reason_text=request.reason_text,
            )
            if request.rating == "DISLIKE":
                citations = list(
                    await db.scalars(select(MessageCitation).where(MessageCitation.message_id == message.message_id))
                )
                chunk_ids = [citation.chunk_id for citation in citations if citation.chunk_id]
                governance = await ProductChatRepository(db).get_chunk_governance(chunk_ids)
                segment_ids = {
                    str(segment_id)
                    for details in governance.values()
                    for segment_id in details.get("source_segment_ids", ())
                    if segment_id
                }
                await KnowledgeLifecycleService(db).create_feedback_requests_for_segments(
                    segment_ids,
                    reason=_feedback_governance_reason(message),
                    operator_id=current_user.uid,
                )
            return MessageFeedbackResponse(
                message_id=message.message_id,
                feedback_rating=message.feedback_rating,
                feedback_reason_type=message.feedback_reason_type,
                feedback_reason_text=message.feedback_reason_text,
            )
    except ProductMessageNotFoundError:
        raise _message_not_found() from None


@product_chat.post(
    "/chat/conversations/{conversation_id}/archive",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def archive_conversation(
    conversation_id: str,
    current_user: User = Depends(get_product_user),
) -> Response:
    try:
        async with pg_manager.get_async_session_context() as db:
            await ProductChatRepository(db).archive_conversation(
                conversation_id,
                current_user.id,
            )
    except ProductChatNotFoundError:
        raise _not_found() from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@product_chat.post(
    "/chat/conversations/{conversation_id}/restore",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def restore_conversation(
    conversation_id: str,
    current_user: User = Depends(get_product_user),
) -> Response:
    try:
        async with pg_manager.get_async_session_context() as db:
            await ProductChatRepository(db).restore_conversation(
                conversation_id,
                current_user.id,
            )
    except ProductChatNotFoundError:
        raise _not_found() from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
