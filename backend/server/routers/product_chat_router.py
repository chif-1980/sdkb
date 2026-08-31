"""Conversation and message endpoints for the enterprise assistant."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from server.routers.product_api_route import ProductApiRoute
from server.utils.auth_middleware import get_product_user
from yuxi.governance.lifecycle_service import KnowledgeLifecycleService
from yuxi.product_chat.answer_service import AnswerDelta, AnswerProgress, AnswerService, GroundedAnswer
from yuxi.product_chat.repository import (
    ProductChatNotFoundError,
    ProductChatRepository,
    ProductMessageNotFoundError,
)
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
    SendMessageRequest,
)
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_product import (
    MessageCitation,
    ProductConversation,
    ProductMessage,
)
from yuxi.utils import logger
from yuxi.utils.datetime_utils import format_utc_datetime

product_chat = APIRouter(route_class=ProductApiRoute)


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
) -> MessageResponse:
    return MessageResponse(
        id=message.message_id,
        role=message.role,
        content=message.content,
        answer_status=message.answer_status,
        feedback_rating=message.feedback_rating,
        citations=[_citation_response(citation) for citation in citations],
        created_at=format_utc_datetime(message.created_at) or "",
    )


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
            return ConversationDetailResponse(
                conversation=_conversation_response(conversation, len(messages)),
                messages=[_message_response(message, citations) for message, citations in messages],
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
            request.content,
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
                assistant_message=_message_response(assistant_message, assistant_citations),
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
                request.content,
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
                )
                stored_conversation = await repository.require_conversation(
                    conversation_id,
                    current_user.id,
                )
                message_count = (await repository.get_message_counts([conversation_id])).get(conversation_id, 0)
                response = MessageExchangeResponse(
                    conversation=_conversation_response(stored_conversation, message_count),
                    user_message=_message_response(user_message, []),
                    assistant_message=_message_response(assistant_message, assistant_citations),
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


@product_chat.put(
    "/chat/messages/{message_id}/feedback",
    response_model=MessageFeedbackResponse,
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
                    reason=f"助手回答被用户标记为不满意（消息 {message.message_id}）",
                    operator_id=current_user.uid,
                )
            return MessageFeedbackResponse(
                message_id=message.message_id,
                feedback_rating=message.feedback_rating,
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
