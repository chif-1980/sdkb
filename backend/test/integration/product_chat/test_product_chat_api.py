from __future__ import annotations

import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers import router
from server.routers import product_chat_router
from server.utils.auth_middleware import get_db
from yuxi.product_chat.answer_service import (
    AnswerDelta,
    AnswerProgress,
    AnswerService,
    GroundedAnswer,
    GroundedCitation,
)
from yuxi.product_chat.repository import ProductChatRepository
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Base, Department, User
from yuxi.storage.postgres.models_knowledge import (
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSource,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
    KnowledgeChunk,
)
from yuxi.storage.postgres.models_product import (
    ConversationStatus,
    MessageCitation,
    ProductConversation,
    ProductMessage,
)
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def product_chat_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-that-is-long-and-stable")
    monkeypatch.setenv("YUXI_INSTANCE_ID", "product-chat-api-test")
    monkeypatch.delenv("LITE_MODE", raising=False)


def _product_token(user_id: int) -> str:
    return AuthUtils.create_access_token({"sub": str(user_id), "token_kind": "enterprise_assistant"})


def _citation(index: int = 1) -> GroundedCitation:
    return GroundedCitation(
        evidence_id=f"E{index}",
        source_id="source-1",
        item_id=f"item-{index}",
        version_id=f"version-{index}",
        yuxi_file_id=f"file-{index}",
        title=f"产品手册 {index}",
        source_url=f"https://quickdone.feishu.cn/wiki/item-{index}",
        path_text="产品 / 手册",
        locator=f"第{index}段",
        excerpt=f"正式资料 {index}",
        source_version_at=datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
        chunk_id=f"chunk-{index}",
    )


def _answer(status: str) -> GroundedAnswer:
    if status == "SUPPORTED":
        citations = (_citation(),)
        content = "该产品支持私有部署。"
    elif status == "CONFLICTING":
        citations = (_citation(), _citation(2))
        content = "两份正式资料对部署范围的描述存在冲突。"
    else:
        citations = ()
        content = "暂无足够可靠资料"
    return GroundedAnswer(
        status=status,
        content=content,
        citations=citations,
        model_version="model-1",
    )


@pytest_asyncio.fixture()
async def chat_api_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'product-chat-api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as seed_session:
        department = Department(name="Engineering")
        owner = User(
            username="Product Chat Owner",
            uid="product-chat-owner",
            password_hash="not-used-by-product-chat",
            role="user",
            department=department,
        )
        other_user = User(
            username="Other Product User",
            uid="other-product-user",
            password_hash="not-used-by-product-chat",
            role="user",
            department=department,
        )
        seed_session.add_all([owner, other_user])
        await seed_session.commit()
        await seed_session.refresh(owner)
        await seed_session.refresh(other_user)
        owner_id = owner.id
        other_user_id = other_user.id

    auth_sessions = []
    short_sessions = []
    session_events: list[str] = []

    async def override_db():
        async with session_factory() as session:
            auth_sessions.append(session)
            yield session

    @asynccontextmanager
    async def short_session_context():
        session = session_factory()
        short_sessions.append(session)
        session_number = len(short_sessions)
        session_events.append(f"enter:{session_number}")
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
            session_events.append(f"exit:{session_number}")

    monkeypatch.setattr(pg_manager, "AsyncSession", session_factory)
    monkeypatch.setattr(pg_manager, "get_async_session_context", short_session_context)
    monkeypatch.setattr(AnswerService, "__init__", lambda self, **kwargs: None)

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = override_db

    @app.get("/api/test/non-product-error")
    async def non_product_error():
        raise HTTPException(status_code=418, detail="非产品接口错误")

    async def create_conversation(
        *,
        owner_id_override: int | None = None,
        title: str = "",
        status: ConversationStatus = ConversationStatus.ACTIVE,
    ) -> ProductConversation:
        async with session_factory() as session:
            conversation = ProductConversation(
                owner_user_id=owner_id_override or owner_id,
                title=title,
                status=status,
            )
            session.add(conversation)
            await session.commit()
            await session.refresh(conversation)
            return conversation

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            factory=session_factory,
            owner_id=owner_id,
            other_user_id=other_user_id,
            owner_headers={"Cookie": f"enterprise_assistant_session={_product_token(owner_id)}"},
            other_headers={"Cookie": (f"enterprise_assistant_session={_product_token(other_user_id)}")},
            auth_sessions=auth_sessions,
            short_sessions=short_sessions,
            session_events=session_events,
            create_conversation=create_conversation,
        )

    await engine.dispose()


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/api/chat/conversations", None),
        ("POST", "/api/chat/conversations", {}),
        ("GET", "/api/chat/conversations/missing", None),
        ("POST", "/api/chat/conversations/missing/messages", {"content": "问题"}),
        ("POST", "/api/chat/conversations/missing/messages/stream", {"content": "问题"}),
        ("PUT", "/api/chat/messages/missing/feedback", {"rating": "LIKE"}),
        ("POST", "/api/chat/conversations/missing/archive", None),
        ("POST", "/api/chat/conversations/missing/restore", None),
    ],
)
async def test_chat_endpoints_require_product_session(
    chat_api_context,
    method: str,
    path: str,
    json_body: dict | None,
):
    response = await chat_api_context.client.request(method, path, json=json_body)

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "LOGIN_REQUIRED",
            "message": "请使用飞书登录",
        }
    }


async def test_non_product_route_keeps_fastapi_default_error_shape(chat_api_context):
    response = await chat_api_context.client.get("/api/test/non-product-error")

    assert response.status_code == 418
    assert response.json() == {"detail": "非产品接口错误"}


async def test_create_and_list_conversations_use_camel_case_owned_summaries(
    chat_api_context,
):
    context = chat_api_context
    await context.create_conversation(owner_id_override=context.other_user_id, title="Other")
    await context.create_conversation(title="Archived", status=ConversationStatus.ARCHIVED)

    created_response = await context.client.post(
        "/api/chat/conversations",
        headers=context.owner_headers,
        json={"title": "  产品部署  "},
    )
    list_response = await context.client.get(
        "/api/chat/conversations",
        headers=context.owner_headers,
    )

    assert created_response.status_code == 201
    created = created_response.json()["conversation"]
    assert created == {
        "id": created["id"],
        "title": "产品部署",
        "status": "ACTIVE",
        "messageCount": 0,
        "createdAt": created["createdAt"],
        "updatedAt": created["updatedAt"],
    }
    assert len(created["id"]) == 26
    assert created["createdAt"].endswith("Z")
    assert created["updatedAt"].endswith("Z")
    assert list_response.status_code == 200
    listed = list_response.json()["conversations"]
    assert len(listed) == 2
    assert listed[0] == created
    assert listed[1]["title"] == "Archived"
    assert listed[1]["status"] == "ARCHIVED"
    assert listed[1]["messageCount"] == 0


@pytest.mark.parametrize(
    ("status", "expected_citation_count"),
    [("SUPPORTED", 1), ("INSUFFICIENT", 0), ("CONFLICTING", 2)],
)
async def test_send_and_detail_return_persisted_exchange_for_each_answer_status(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_citation_count: int,
):
    context = chat_api_context
    conversation = await context.create_conversation()
    answer = _answer(status)

    async def answer_question(self, question: str, user: User, conversation_id: str, *, mode: str):
        assert question == "企业版如何部署？"
        assert user.id == context.owner_id
        assert conversation_id == conversation.conversation_id
        assert mode == "CONCISE"
        return answer

    monkeypatch.setattr(AnswerService, "answer", answer_question)

    send_response = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/messages",
        headers=context.owner_headers,
        json={"content": "企业版如何部署？"},
    )
    detail_response = await context.client.get(
        f"/api/chat/conversations/{conversation.conversation_id}",
        headers=context.owner_headers,
    )

    assert send_response.status_code == 201
    exchange = send_response.json()
    assert exchange["conversation"]["title"] == "企业版如何部署？"
    assert exchange["conversation"]["messageCount"] == 2
    assert exchange["userMessage"]["role"] == "USER"
    assert exchange["userMessage"]["content"] == "企业版如何部署？"
    assert exchange["userMessage"]["answerStatus"] is None
    assert exchange["userMessage"]["citations"] == []
    assert exchange["assistantMessage"]["role"] == "ASSISTANT"
    assert exchange["assistantMessage"]["content"] == answer.content
    assert exchange["assistantMessage"]["answerStatus"] == status
    citations = exchange["assistantMessage"]["citations"]
    assert len(citations) == expected_citation_count
    if citations:
        assert citations[0]["kind"] == "ENTERPRISE_EVIDENCE"
        assert citations[0]["path"] == "产品 / 手册"
        assert citations[0]["locator"] == "第1段"
        assert citations[0]["excerpt"] == "正式资料 1"
        assert citations[0]["versionAt"].endswith("Z")
        assert set(citations[0]) == {
            "id",
            "kind",
            "title",
            "path",
            "locator",
            "excerpt",
            "versionAt",
            "mediaType",
            "imageUrl",
            "previewUrl",
            "imageAlt",
        }

    assert detail_response.status_code == 200
    assert detail_response.json() == {
        "conversation": exchange["conversation"],
        "messages": [exchange["userMessage"], exchange["assistantMessage"]],
    }


async def test_stream_message_emits_real_progress_and_persists_the_exchange(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
):
    context = chat_api_context
    conversation = await context.create_conversation()

    async def answer_events(self, question: str, user: User, conversation_id: str, *, mode: str):
        assert question == "部署前需要准备什么？"
        assert user.id == context.owner_id
        assert conversation_id == conversation.conversation_id
        assert mode == "DETAILED"
        yield AnswerProgress("UNDERSTANDING", "正在结合当前对话理解问题")
        yield AnswerProgress("RETRIEVING", "正在检索已审核发布的资料")
        yield AnswerProgress("VERIFYING", "正在核对原文与适用条件")
        yield AnswerProgress("COMPOSING", "正在整理结论和可核验来源")
        yield AnswerDelta("该产品支持")
        yield AnswerDelta("私有部署。")
        yield _answer("SUPPORTED")

    monkeypatch.setattr(AnswerService, "answer_events", answer_events)

    response = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/messages/stream",
        headers=context.owner_headers,
        json={"content": "部署前需要准备什么？", "mode": "DETAILED"},
    )
    detail_response = await context.client.get(
        f"/api/chat/conversations/{conversation.conversation_id}",
        headers=context.owner_headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text.count("event: progress") == 4
    assert '"stage": "VERIFYING"' in response.text
    assert response.text.count("event: delta") == 2
    assert response.text.index("event: delta") < response.text.index("event: complete")
    assert "event: complete" in response.text
    assert '"messageCount": 2' in response.text
    assert detail_response.json()["messages"][0]["content"] == "部署前需要准备什么？"
    assert detail_response.json()["messages"][1]["content"] == "该产品支持私有部署。"


async def test_stream_failure_emits_stable_error_without_persisting_half_exchange(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
):
    context = chat_api_context
    conversation = await context.create_conversation()
    secret_error = "provider-secret-stream-detail"

    async def failing_answer_events(*args, **kwargs):
        yield AnswerProgress("UNDERSTANDING", "正在结合当前对话理解问题")
        yield AnswerDelta("尚未完成的回答")
        raise RuntimeError(secret_error)

    monkeypatch.setattr(AnswerService, "answer_events", failing_answer_events)

    response = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/messages/stream",
        headers=context.owner_headers,
        json={"content": "触发流式回答失败"},
    )

    async with context.factory() as session:
        message_count = await session.scalar(select(func.count()).select_from(ProductMessage))
    assert response.status_code == 200
    assert "event: progress" in response.text
    assert "event: delta" in response.text
    assert "event: error" in response.text
    assert "event: complete" not in response.text
    assert "KNOWLEDGE_SERVICE_UNAVAILABLE" in response.text
    assert secret_error not in response.text
    assert message_count == 0


async def test_assistant_feedback_can_be_set_switched_cleared_and_reloaded(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
):
    context = chat_api_context
    conversation = await context.create_conversation()

    async def answer_question(*args, **kwargs):
        return _answer("SUPPORTED")

    monkeypatch.setattr(AnswerService, "answer", answer_question)
    exchange_response = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/messages",
        headers=context.owner_headers,
        json={"content": "这条回答有帮助吗？"},
    )
    exchange = exchange_response.json()
    assistant_id = exchange["assistantMessage"]["id"]
    assert exchange["assistantMessage"]["feedbackRating"] is None

    liked = await context.client.put(
        f"/api/chat/messages/{assistant_id}/feedback",
        headers=context.owner_headers,
        json={"rating": "LIKE"},
    )
    detail = await context.client.get(
        f"/api/chat/conversations/{conversation.conversation_id}",
        headers=context.owner_headers,
    )
    disliked = await context.client.put(
        f"/api/chat/messages/{assistant_id}/feedback",
        headers=context.owner_headers,
        json={"rating": "DISLIKE"},
    )
    cleared = await context.client.put(
        f"/api/chat/messages/{assistant_id}/feedback",
        headers=context.owner_headers,
        json={"rating": None},
    )

    assert liked.status_code == 200
    assert liked.json() == {"messageId": assistant_id, "feedbackRating": "LIKE"}
    assert detail.json()["messages"][1]["feedbackRating"] == "LIKE"
    assert disliked.json() == {"messageId": assistant_id, "feedbackRating": "DISLIKE"}
    assert cleared.json() == {"messageId": assistant_id, "feedbackRating": None}
    async with context.factory() as session:
        stored = await session.scalar(select(ProductMessage).where(ProductMessage.message_id == assistant_id))
    assert stored.feedback_rating is None


async def test_dislike_creates_one_unassigned_source_correction_task_from_cited_chunk(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
):
    context = chat_api_context
    conversation = await context.create_conversation()
    async with context.factory() as session:
        session.add_all(
            [
                FeishuSource(
                    source_id="source-1",
                    name="善达知识库",
                    wiki_root_token="root",
                    target_kb_id="kb-1",
                    credential_env_name="FEISHU_USER_OAUTH",
                ),
                FeishuSourceItem(
                    item_id="item-1",
                    source_id="source-1",
                    item_key="page:item-1",
                    item_type="docx",
                    title="产品手册 1",
                    source_url="https://quickdone.feishu.cn/wiki/item-1",
                    source_validity="valid",
                    active_version_id="version-1",
                    publication_status="ACTIVE",
                ),
                FeishuMaterialVersion(
                    version_id="version-1",
                    item_id="item-1",
                    revision="1",
                    content_hash="version-hash",
                    processing_status="published",
                    review_status="approved",
                    yuxi_file_id="file-1",
                    published_at=datetime.now(UTC),
                ),
                FeishuKnowledgeUnit(
                    unit_id="unit-1",
                    unit_key="section:deployment",
                    lineage_key="section:deployment",
                    version_id="version-1",
                    item_id="item-1",
                    unit_index=0,
                    unit_type="SECTION",
                    title="部署要求",
                    content="该产品支持私有部署。",
                    content_hash="unit-hash",
                    source_segment_ids=["segment-1"],
                    recommended_outcome="PUBLISH",
                    recommendation_reason="内容完整。",
                    publication_state="INCLUDED",
                    lifecycle_status="ACTIVE",
                    status="ACTIVE",
                ),
                KnowledgeChunk(
                    chunk_id="chunk-1",
                    file_id="file-1",
                    kb_id="kb-1",
                    chunk_index=0,
                    content="该产品支持私有部署。",
                    tags={"source_segment_ids": ["segment-1"]},
                ),
            ]
        )
        await session.commit()

    async def answer_question(*args, **kwargs):
        return _answer("SUPPORTED")

    monkeypatch.setattr(AnswerService, "answer", answer_question)
    exchange = (
        await context.client.post(
            f"/api/chat/conversations/{conversation.conversation_id}/messages",
            headers=context.owner_headers,
            json={"content": "该产品支持私有部署吗？"},
        )
    ).json()
    assistant_id = exchange["assistantMessage"]["id"]

    first = await context.client.put(
        f"/api/chat/messages/{assistant_id}/feedback",
        headers=context.owner_headers,
        json={"rating": "DISLIKE"},
    )
    repeated = await context.client.put(
        f"/api/chat/messages/{assistant_id}/feedback",
        headers=context.owner_headers,
        json={"rating": "DISLIKE"},
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    async with context.factory() as session:
        citation = await session.scalar(select(MessageCitation).where(MessageCitation.message_id == assistant_id))
        packages = list(await session.scalars(select(FeishuReviewPackage)))
        review_items = list(await session.scalars(select(FeishuReviewItem)))
        change_requests = list(await session.scalars(select(FeishuSourceChangeRequest)))

    assert citation.chunk_id == "chunk-1"
    assert len(packages) == len(review_items) == len(change_requests) == 1
    assert packages[0].trigger_type == "FEEDBACK"
    assert packages[0].workflow_status == "WAITING_SOURCE_CHANGE"
    assert packages[0].assignee_id is None
    assert review_items[0].subject_id == "unit-1"
    assert change_requests[0].source_url == "https://quickdone.feishu.cn/wiki/item-1"


async def test_feedback_rejects_user_messages_cross_user_access_and_invalid_ratings(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
):
    context = chat_api_context
    conversation = await context.create_conversation()

    async def answer_question(*args, **kwargs):
        return _answer("SUPPORTED")

    monkeypatch.setattr(AnswerService, "answer", answer_question)
    exchange = (
        await context.client.post(
            f"/api/chat/conversations/{conversation.conversation_id}/messages",
            headers=context.owner_headers,
            json={"content": "测试反馈权限"},
        )
    ).json()
    user_id = exchange["userMessage"]["id"]
    assistant_id = exchange["assistantMessage"]["id"]

    user_message_response = await context.client.put(
        f"/api/chat/messages/{user_id}/feedback",
        headers=context.owner_headers,
        json={"rating": "LIKE"},
    )
    cross_user_response = await context.client.put(
        f"/api/chat/messages/{assistant_id}/feedback",
        headers=context.other_headers,
        json={"rating": "DISLIKE"},
    )
    invalid_response = await context.client.put(
        f"/api/chat/messages/{assistant_id}/feedback",
        headers=context.owner_headers,
        json={"rating": "OTHER"},
    )

    for response in (user_message_response, cross_user_response):
        assert response.status_code == 404
        assert response.json() == {
            "error": {
                "code": "MESSAGE_NOT_FOUND",
                "message": "消息不存在",
            }
        }
    assert invalid_response.status_code == 422
    assert invalid_response.json() == {
        "error": {
            "code": "REQUEST_VALIDATION_ERROR",
            "message": "请求参数不合法",
        }
    }


@pytest.mark.parametrize("action", ["detail", "send", "archive", "restore"])
async def test_cross_user_conversation_access_is_hidden_as_not_found(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
):
    context = chat_api_context
    conversation = await context.create_conversation()
    answer_calls = 0

    async def answer_question(*args, **kwargs):
        nonlocal answer_calls
        answer_calls += 1
        return _answer("SUPPORTED")

    monkeypatch.setattr(AnswerService, "answer", answer_question)
    paths = {
        "detail": ("GET", f"/api/chat/conversations/{conversation.conversation_id}", None),
        "send": (
            "POST",
            f"/api/chat/conversations/{conversation.conversation_id}/messages",
            {"content": "问题"},
        ),
        "archive": (
            "POST",
            f"/api/chat/conversations/{conversation.conversation_id}/archive",
            None,
        ),
        "restore": (
            "POST",
            f"/api/chat/conversations/{conversation.conversation_id}/restore",
            None,
        ),
    }
    method, path, body = paths[action]

    response = await context.client.request(
        method,
        path,
        headers=context.other_headers,
        json=body,
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "CONVERSATION_NOT_FOUND",
            "message": "会话不存在",
        }
    }
    assert answer_calls == 0


async def test_archived_conversation_cannot_send_and_repeated_archive_has_no_side_effect(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
):
    context = chat_api_context
    conversation = await context.create_conversation(title="Archive once")
    answer_calls = 0

    async def answer_question(*args, **kwargs):
        nonlocal answer_calls
        answer_calls += 1
        return _answer("SUPPORTED")

    monkeypatch.setattr(AnswerService, "answer", answer_question)
    first_archive = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/archive",
        headers=context.owner_headers,
    )
    async with context.factory() as session:
        archived_once = await session.scalar(
            select(ProductConversation).where(ProductConversation.conversation_id == conversation.conversation_id)
        )
        first_updated_at = archived_once.updated_at

    listed_after_archive = await context.client.get(
        "/api/chat/conversations",
        headers=context.owner_headers,
    )
    detail_after_archive = await context.client.get(
        f"/api/chat/conversations/{conversation.conversation_id}",
        headers=context.owner_headers,
    )

    second_archive = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/archive",
        headers=context.owner_headers,
    )
    send_response = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/messages",
        headers=context.owner_headers,
        json={"content": "归档后问题"},
    )

    restore_response = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/restore",
        headers=context.owner_headers,
    )
    second_restore = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/restore",
        headers=context.owner_headers,
    )

    async with context.factory() as session:
        restored = await session.scalar(
            select(ProductConversation).where(ProductConversation.conversation_id == conversation.conversation_id)
        )
        message_count = await session.scalar(select(func.count()).select_from(ProductMessage))

    assert first_archive.status_code == 204
    assert listed_after_archive.status_code == 200
    assert listed_after_archive.json()["conversations"] == [
        {
            "id": conversation.conversation_id,
            "title": "Archive once",
            "status": "ARCHIVED",
            "messageCount": 0,
            "createdAt": listed_after_archive.json()["conversations"][0]["createdAt"],
            "updatedAt": listed_after_archive.json()["conversations"][0]["updatedAt"],
        }
    ]
    assert detail_after_archive.status_code == 200
    assert detail_after_archive.json()["conversation"]["status"] == "ARCHIVED"
    assert second_archive.status_code == 404
    assert send_response.status_code == 404
    assert restore_response.status_code == 204
    assert second_restore.status_code == 404
    assert restored.status == ConversationStatus.ACTIVE
    assert restored.updated_at != first_updated_at
    assert message_count == 0
    assert answer_calls == 0


@pytest.mark.parametrize("technical_field", ["kb_id", "model", "agent", "top_k", "prompt"])
@pytest.mark.parametrize("endpoint", ["create", "send"])
async def test_technical_request_fields_are_rejected_without_side_effects(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
    technical_field: str,
    endpoint: str,
):
    context = chat_api_context
    conversation = await context.create_conversation()
    answer_calls = 0

    async def answer_question(*args, **kwargs):
        nonlocal answer_calls
        answer_calls += 1
        return _answer("SUPPORTED")

    monkeypatch.setattr(AnswerService, "answer", answer_question)
    if endpoint == "create":
        path = "/api/chat/conversations"
        payload = {"title": "会话", technical_field: "internal-value"}
    else:
        path = f"/api/chat/conversations/{conversation.conversation_id}/messages"
        payload = {"content": "问题", technical_field: "internal-value"}

    response = await context.client.post(
        path,
        headers=context.owner_headers,
        json=payload,
    )

    async with context.factory() as session:
        message_count = await session.scalar(select(func.count()).select_from(ProductMessage))
        conversation_count = await session.scalar(select(func.count()).select_from(ProductConversation))
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "REQUEST_VALIDATION_ERROR",
            "message": "请求参数不合法",
        }
    }
    assert message_count == 0
    assert conversation_count == 1
    assert answer_calls == 0


async def test_send_releases_auth_and_read_transactions_before_answer_and_uses_new_write_session(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
):
    context = chat_api_context
    conversation = await context.create_conversation()

    async def answer_question(self, question: str, user: User, conversation_id: str, *, mode: str):
        assert question == "事务边界问题"
        assert conversation_id == conversation.conversation_id
        assert mode == "CONCISE"
        assert len(context.auth_sessions) == 1
        assert not context.auth_sessions[0].in_transaction()
        assert inspect(user).detached
        assert context.session_events == ["enter:1", "exit:1"]
        assert not context.short_sessions[0].in_transaction()
        return _answer("INSUFFICIENT")

    monkeypatch.setattr(AnswerService, "answer", answer_question)

    response = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/messages",
        headers=context.owner_headers,
        json={"content": "事务边界问题"},
    )

    assert response.status_code == 201
    assert context.session_events == ["enter:1", "exit:1", "enter:2", "exit:2"]
    assert len(context.short_sessions) == 2
    assert context.short_sessions[0] is not context.short_sessions[1]
    assert all(not session.in_transaction() for session in context.short_sessions)


@pytest.mark.parametrize("failure_origin", ["constructor", "answer"])
async def test_knowledge_failure_returns_stable_503_without_half_write_or_error_leakage(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
    failure_origin: str,
):
    context = chat_api_context
    conversation = await context.create_conversation()
    secret_error = "provider-secret-stack-detail"

    def failing_constructor(*args, **kwargs):
        raise RuntimeError(secret_error)

    async def failing_answer(*args, **kwargs):
        raise RuntimeError(secret_error)

    if failure_origin == "constructor":
        monkeypatch.setattr(AnswerService, "__init__", failing_constructor)
    else:
        monkeypatch.setattr(AnswerService, "answer", failing_answer)

    response = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/messages",
        headers=context.owner_headers,
        json={"content": "触发知识服务失败"},
    )

    async with context.factory() as session:
        message_count = await session.scalar(select(func.count()).select_from(ProductMessage))
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "KNOWLEDGE_SERVICE_UNAVAILABLE",
            "message": "知识服务暂时不可用，请稍后重试",
        }
    }
    assert secret_error not in response.text
    assert message_count == 0
    assert context.session_events == ["enter:1", "exit:1"]


async def test_response_construction_failure_rolls_back_exchange_and_returns_stable_503(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
):
    context = chat_api_context
    conversation = await context.create_conversation()

    async def answer_question(*args, **kwargs):
        return _answer("SUPPORTED")

    def fail_response_construction(*args, **kwargs):
        raise RuntimeError("response-construction-secret")

    monkeypatch.setattr(AnswerService, "answer", answer_question)
    monkeypatch.setattr(product_chat_router, "_conversation_response", fail_response_construction)

    async with AsyncClient(
        transport=ASGITransport(app=context.app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/chat/conversations/{conversation.conversation_id}/messages",
            headers=context.owner_headers,
            json={"content": "响应构造失败不应留下半写入"},
        )

    async with context.factory() as session:
        message_count = await session.scalar(select(func.count()).select_from(ProductMessage))
        citation_count = await session.scalar(select(func.count()).select_from(MessageCitation))

    assert (response.status_code, message_count, citation_count) == (503, 0, 0)
    assert response.json() == {
        "error": {
            "code": "KNOWLEDGE_SERVICE_UNAVAILABLE",
            "message": "知识服务暂时不可用，请稍后重试",
        }
    }
    assert "response-construction-secret" not in response.text


async def test_send_counts_messages_without_loading_full_history(
    chat_api_context,
    monkeypatch: pytest.MonkeyPatch,
):
    context = chat_api_context
    conversation = await context.create_conversation(title="已有会话")
    async with context.factory() as session:
        await ProductChatRepository(session).append_exchange(
            conversation,
            context.owner_id,
            "历史问题",
            _answer("SUPPORTED"),
        )
        await session.commit()

    current_answer = GroundedAnswer(
        status="SUPPORTED",
        content="这是本次回答。",
        citations=(_citation(2),),
        model_version="model-2",
    )

    async def answer_question(*args, **kwargs):
        return current_answer

    async def fail_full_history_load(*args, **kwargs):
        raise AssertionError("send_message must not load full message history")

    monkeypatch.setattr(AnswerService, "answer", answer_question)
    monkeypatch.setattr(ProductChatRepository, "list_messages_with_citations", fail_full_history_load)

    async with AsyncClient(
        transport=ASGITransport(app=context.app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/chat/conversations/{conversation.conversation_id}/messages",
            headers=context.owner_headers,
            json={"content": "本次问题"},
        )

    assert response.status_code == 201
    exchange = response.json()
    assert exchange["conversation"]["messageCount"] == 4
    assert exchange["assistantMessage"]["content"] == current_answer.content
    assert [citation["title"] for citation in exchange["assistantMessage"]["citations"]] == ["产品手册 2"]


async def test_product_chat_routes_are_not_registered_in_lite_mode():
    backend_root = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment["LITE_MODE"] = "true"
    python_path = [str(backend_root), str(backend_root / "package")]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    script = (
        "from server.routers import router; "
        "paths={route.path for route in router.routes}; "
        "assert '/chat/conversations' not in paths; "
        "assert not any(path.startswith('/citations/') for path in paths)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
