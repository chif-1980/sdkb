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
from server.utils.auth_middleware import get_db
from yuxi.product_chat.answer_service import AnswerService, GroundedAnswer, GroundedCitation
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Base, Department, User
from yuxi.storage.postgres.models_product import (
    ConversationStatus,
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
        ("POST", "/api/chat/conversations/missing/archive", None),
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


async def test_create_and_list_conversations_use_camel_case_owned_active_summaries(
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
    assert list_response.json() == {"conversations": [created]}


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

    async def answer_question(self, question: str, user: User, conversation_id: str):
        assert question == "企业版如何部署？"
        assert user.id == context.owner_id
        assert conversation_id == conversation.conversation_id
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
        }

    assert detail_response.status_code == 200
    assert detail_response.json() == {
        "conversation": exchange["conversation"],
        "messages": [exchange["userMessage"], exchange["assistantMessage"]],
    }


@pytest.mark.parametrize("action", ["detail", "send", "archive"])
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

    second_archive = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/archive",
        headers=context.owner_headers,
    )
    send_response = await context.client.post(
        f"/api/chat/conversations/{conversation.conversation_id}/messages",
        headers=context.owner_headers,
        json={"content": "归档后问题"},
    )

    async with context.factory() as session:
        archived_twice = await session.scalar(
            select(ProductConversation).where(ProductConversation.conversation_id == conversation.conversation_id)
        )
        message_count = await session.scalar(select(func.count()).select_from(ProductMessage))

    assert first_archive.status_code == 204
    assert second_archive.status_code == 404
    assert send_response.status_code == 404
    assert archived_twice.status == ConversationStatus.ARCHIVED
    assert archived_twice.updated_at == first_updated_at
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

    async def answer_question(self, question: str, user: User, conversation_id: str):
        assert question == "事务边界问题"
        assert conversation_id == conversation.conversation_id
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
