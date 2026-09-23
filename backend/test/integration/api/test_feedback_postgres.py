"""Feedback and review creation must commit together with PostgreSQL foreign keys enabled."""

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.product_chat_router import product_chat
from server.routers.dashboard_router import dashboard
from server.utils.auth_middleware import get_db
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Base, Department, User
from yuxi.storage.postgres.models_knowledge import (
    FeishuSource,
    FeishuSourceItem,
    FeishuMaterialVersion,
    FeishuKnowledgeUnit,
    FeishuReviewPackage,
    FeishuReviewItem,
    FeishuSourceChangeRequest,
    FeishuNotificationDelivery,
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeChunk,
)
from yuxi.storage.postgres.models_product import ProductConversation, ProductMessage, MessageCitation
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_other_feedback_creates_review_and_is_visible_in_dashboard(monkeypatch):
    schema = "test_feedback_" + uuid4().hex
    engine = create_async_engine(os.environ["POSTGRES_URL"], execution_options={"schema_translate_map": {None: schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Include the full schema: dashboard reads both legacy and product feedback.
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            # Flush fixture parents explicitly so a failure concerns the actual request.
            async def add(row):
                db.add(row)
                await db.flush()
                return row

            dept = await add(Department(name="反馈测试"))
            owner = await add(
                User(username="owner", uid="owner", role="user", password_hash="unused", department_id=dept.id)
            )
            admin = await add(
                User(username="admin", uid="admin", role="superadmin", password_hash="unused", department_id=dept.id)
            )
            await add(KnowledgeBase(kb_id="kb", name="test", kb_type="milvus"))
            await add(KnowledgeFile(file_id="file", kb_id="kb", filename="test.txt"))
            await add(
                FeishuSource(
                    source_id="source",
                    name="测试",
                    wiki_root_token="root",
                    target_kb_id="kb",
                    credential_env_name="unused",
                )
            )
            item = await add(
                FeishuSourceItem(
                    item_id="item",
                    source_id="source",
                    item_key="item",
                    item_type="docx",
                    title="语音机器人",
                    source_url="https://example.test/source",
                    source_validity="valid",
                    publication_status="ACTIVE",
                )
            )
            await add(
                FeishuMaterialVersion(
                    version_id="version",
                    item_id="item",
                    revision="1",
                    content_hash="hash",
                    processing_status="published",
                    review_status="approved",
                    yuxi_file_id="file",
                    published_at=datetime.now(UTC),
                )
            )
            item.active_version_id = "version"
            await add(
                FeishuKnowledgeUnit(
                    unit_id="unit",
                    unit_key="unit",
                    lineage_key="unit",
                    version_id="version",
                    item_id="item",
                    unit_index=0,
                    unit_type="SECTION",
                    title="语音机器人",
                    content="test",
                    content_hash="hash",
                    source_segment_ids=["segment"],
                    recommended_outcome="PUBLISH",
                    recommendation_reason="test",
                    publication_state="INCLUDED",
                    lifecycle_status="ACTIVE",
                    status="ACTIVE",
                )
            )
            await add(
                KnowledgeChunk(
                    chunk_id="chunk",
                    file_id="file",
                    kb_id="kb",
                    chunk_index=0,
                    content="test",
                    tags={"source_segment_ids": ["segment"]},
                )
            )
            conversation = await add(ProductConversation(owner_user_id=owner.id, title="介绍下语音机器人"))
            message = await add(
                ProductMessage(
                    conversation_id=conversation.conversation_id,
                    role="ASSISTANT",
                    content="机器人回答",
                    answer_status="SUPPORTED",
                    feedback_rating="LIKE",
                )
            )
            await add(
                MessageCitation(
                    message_id=message.message_id,
                    source_id="source",
                    item_id="item",
                    version_id="version",
                    yuxi_file_id="file",
                    chunk_id="chunk",
                    title="语音机器人",
                    source_url="https://example.test/source",
                    locator="第一段",
                    excerpt="test",
                )
            )
            await db.commit()

        @asynccontextmanager
        async def session_context():
            async with factory() as db:
                async with db.begin():
                    yield db

        async def get_session():
            async with session_context() as db:
                yield db

        monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)
        monkeypatch.setenv("YUXI_GOVERNANCE_AUTOMATION_MODE", "observe")
        app = FastAPI()
        app.include_router(product_chat, prefix="/api")
        app.include_router(dashboard, prefix="/api")
        app.dependency_overrides[get_db] = get_session
        token = AuthUtils.create_access_token({"sub": str(owner.id), "token_kind": "enterprise_assistant"})
        admin_token = AuthUtils.create_access_token({"sub": str(admin.id)})
        payload = {"rating": "DISLIKE", "reasonType": "OTHER", "reasonText": "我想查的是智能客服机器人"}
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            cookies={"enterprise_assistant_session": token},
        ) as client:
            for _ in range(2):
                response = await client.put(f"/api/chat/messages/{message.message_id}/feedback", json=payload)
                assert response.status_code == 200, response.text
                assert response.json()["feedbackReasonText"] == payload["reasonText"]
                assert response.json()["feedbackRating"] == "DISLIKE"
            response = await client.get(
                "/api/dashboard/feedbacks?rating=dislike", headers={"Authorization": f"Bearer {admin_token}"}
            )
            assert response.status_code == 200, response.text
            assert len(response.json()) == 1
            assert payload["reasonText"] in response.json()[0]["reason"]

        async with factory() as db:
            stored = await db.scalar(select(ProductMessage).where(ProductMessage.message_id == message.message_id))
            assert (stored.feedback_rating, stored.feedback_reason_type, stored.feedback_reason_text) == (
                "DISLIKE",
                "OTHER",
                payload["reasonText"],
            )
            for model in (FeishuReviewPackage, FeishuReviewItem, FeishuSourceChangeRequest, FeishuNotificationDelivery):
                assert await db.scalar(select(func.count()).select_from(model)) == 1
            request = await db.scalar(select(FeishuSourceChangeRequest))
            assert payload["reasonText"] in request.request_text
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()
