from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers import router
from server.utils.auth_middleware import get_db
from yuxi.knowledge.runtime import knowledge_base
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Base, Department, User
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
)
from yuxi.storage.postgres.models_product import (
    AnswerStatus,
    CitationKind,
    ConversationStatus,
    MessageCitation,
    MessageRole,
    ProductConversation,
    ProductMessage,
)
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def product_citation_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-that-is-long-and-stable")
    monkeypatch.setenv("YUXI_INSTANCE_ID", "product-citation-api-test")
    monkeypatch.delenv("LITE_MODE", raising=False)


def _product_token(user_id: int) -> str:
    return AuthUtils.create_access_token({"sub": str(user_id), "token_kind": "enterprise_assistant"})


@pytest_asyncio.fixture()
async def citation_api_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'product-citation-api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    persisted_source_url = "https://quickdone.feishu.cn/wiki/persisted-item"
    source_version_at = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
    async with session_factory() as session:
        department = Department(name="Engineering")
        owner = User(
            username="Citation Owner",
            uid="citation-owner",
            password_hash="not-used-by-product-chat",
            role="superadmin",
            department=department,
        )
        other_user = User(
            username="Other Citation User",
            uid="other-citation-user",
            password_hash="not-used-by-product-chat",
            role="user",
            department=department,
        )
        session.add_all([owner, other_user])
        await session.flush()
        conversation = ProductConversation(
            owner_user_id=owner.id,
            title="引用验证",
            status=ConversationStatus.ACTIVE,
        )
        session.add(conversation)
        await session.flush()
        message = ProductMessage(
            conversation_id=conversation.conversation_id,
            role=MessageRole.ASSISTANT,
            content="正式回答",
            answer_status=AnswerStatus.SUPPORTED,
            model_version="model-1",
            prompt_version="enterprise-grounded-v1",
        )
        source = FeishuSource(
            source_id="source-1",
            name="产品知识库",
            wiki_root_token="wiki-root",
            target_kb_id="kb-product",
            credential_env_name="FEISHU_TOKEN",
            enabled=True,
        )
        item = FeishuSourceItem(
            item_id="item-1",
            source_id=source.source_id,
            item_key="wiki:item-1",
            item_type="page",
            title="当前标题",
            path_text="当前 / 路径",
            source_url="https://quickdone.feishu.cn/wiki/current-item",
            source_validity="valid",
            active_version_id="version-1",
        )
        version = FeishuMaterialVersion(
            version_id="version-1",
            item_id=item.item_id,
            revision="1",
            content_hash="hash-1",
            processing_status="published",
            review_status="approved",
            yuxi_file_id="file-1",
            published_at=datetime(2026, 8, 16, 7, 0, tzinfo=UTC),
        )
        session.add_all([message, source, item, version])
        await session.flush()
        citation = MessageCitation(
            message_id=message.message_id,
            kind=CitationKind.ENTERPRISE_EVIDENCE,
            source_id=source.source_id,
            item_id=item.item_id,
            version_id=version.version_id,
            yuxi_file_id="file-1",
            title="回答时标题",
            source_url=persisted_source_url,
            path_text="产品 / 手册",
            locator="第1段",
            excerpt="支持私有部署。",
            source_version_at=source_version_at,
        )
        session.add(citation)
        await session.commit()
        await session.refresh(citation)
        owner_id = owner.id
        other_user_id = other_user.id
        citation_id = citation.citation_id

    async def override_db():
        async with session_factory() as session:
            yield session

    @asynccontextmanager
    async def short_session_context():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    policy_calls: list[tuple[dict, str]] = []

    async def allow_policy(user: dict, kb_id: str) -> bool:
        policy_calls.append((user, kb_id))
        return True

    monkeypatch.setattr(pg_manager, "get_async_session_context", short_session_context)
    monkeypatch.setattr(knowledge_base, "check_policy_accessible", allow_policy)

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = override_db

    async def mutate_material(**changes):
        async with session_factory() as session:
            target_name = changes.pop("target")
            model = {
                "source": FeishuSource,
                "item": FeishuSourceItem,
                "version": FeishuMaterialVersion,
                "citation": MessageCitation,
            }[target_name]
            identity = {
                "source": "source-1",
                "item": "item-1",
                "version": "version-1",
                "citation": citation_id,
            }[target_name]
            identity_column = {
                "source": FeishuSource.source_id,
                "item": FeishuSourceItem.item_id,
                "version": FeishuMaterialVersion.version_id,
                "citation": MessageCitation.citation_id,
            }[target_name]
            record = await session.scalar(select(model).where(identity_column == identity))
            for field, value in changes.items():
                setattr(record, field, value)
            await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        yield SimpleNamespace(
            client=client,
            factory=session_factory,
            owner_id=owner_id,
            owner_headers={"Cookie": f"enterprise_assistant_session={_product_token(owner_id)}"},
            other_headers={"Cookie": (f"enterprise_assistant_session={_product_token(other_user_id)}")},
            citation_id=citation_id,
            persisted_source_url=persisted_source_url,
            source_version_at=source_version_at,
            policy_calls=policy_calls,
            mutate_material=mutate_material,
        )

    await engine.dispose()


@pytest.mark.parametrize("suffix", ["", "/open"])
async def test_citation_endpoints_require_product_session(citation_api_context, suffix: str):
    context = citation_api_context

    response = await context.client.get(f"/api/citations/{context.citation_id}{suffix}")

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "LOGIN_REQUIRED",
            "message": "请使用飞书登录",
        }
    }


async def test_get_citation_returns_persisted_camel_case_snapshot_after_current_policy_check(
    citation_api_context,
):
    context = citation_api_context

    response = await context.client.get(
        f"/api/citations/{context.citation_id}",
        headers=context.owner_headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": context.citation_id,
        "kind": "ENTERPRISE_EVIDENCE",
        "title": "回答时标题",
        "path": "产品 / 手册",
        "locator": "第1段",
        "excerpt": "支持私有部署。",
        "versionAt": response.json()["versionAt"],
    }
    assert response.json()["versionAt"].endswith("Z")
    assert len(context.policy_calls) == 1
    policy_user, kb_id = context.policy_calls[0]
    assert policy_user["id"] == context.owner_id
    assert policy_user["uid"] == "citation-owner"
    assert policy_user["role"] == "superadmin"
    assert kb_id == "kb-product"


async def test_open_citation_redirects_to_persisted_validated_feishu_url(
    citation_api_context,
):
    context = citation_api_context

    response = await context.client.get(
        f"/api/citations/{context.citation_id}/open",
        headers=context.owner_headers,
    )

    assert response.status_code == 307
    assert response.headers["location"] == context.persisted_source_url
    assert response.headers["location"] != "https://quickdone.feishu.cn/wiki/current-item"


@pytest.mark.parametrize("citation_id", ["missing-citation", None])
async def test_nonexistent_and_cross_user_citations_are_hidden_as_not_found(
    citation_api_context,
    citation_id: str | None,
):
    context = citation_api_context
    requested_id = citation_id or context.citation_id
    headers = context.owner_headers if citation_id else context.other_headers

    response = await context.client.get(
        f"/api/citations/{requested_id}",
        headers=headers,
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "CITATION_NOT_FOUND",
            "message": "引用不存在",
        }
    }
    assert context.policy_calls == []


@pytest.mark.parametrize("suffix", ["", "/open"])
@pytest.mark.parametrize("policy_outcome", ["denied", "unconfirmable"])
async def test_current_knowledge_policy_denial_or_failure_returns_403_even_for_superadmin(
    citation_api_context,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    policy_outcome: str,
):
    context = citation_api_context

    async def policy_result(user: dict, kb_id: str) -> bool:
        assert user["role"] == "superadmin"
        assert kb_id == "kb-product"
        if policy_outcome == "unconfirmable":
            raise RuntimeError("policy backend unavailable")
        return False

    monkeypatch.setattr(knowledge_base, "check_policy_accessible", policy_result)

    response = await context.client.get(
        f"/api/citations/{context.citation_id}{suffix}",
        headers=context.owner_headers,
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "CITATION_ACCESS_DENIED",
            "message": "当前无权访问该引用",
        }
    }


@pytest.mark.parametrize("suffix", ["", "/open"])
@pytest.mark.parametrize(
    ("target", "changes"),
    [
        ("source", {"enabled": False}),
        ("item", {"source_validity": "invalid"}),
        ("item", {"active_version_id": "version-replaced"}),
        ("version", {"processing_status": "parsed"}),
        ("version", {"review_status": "pending"}),
        ("version", {"published_at": None}),
        ("version", {"yuxi_file_id": "file-replaced"}),
    ],
)
async def test_withdrawn_source_version_or_material_returns_410(
    citation_api_context,
    suffix: str,
    target: str,
    changes: dict,
):
    context = citation_api_context
    await context.mutate_material(target=target, **changes)

    response = await context.client.get(
        f"/api/citations/{context.citation_id}{suffix}",
        headers=context.owner_headers,
    )

    assert response.status_code == 410
    assert response.json() == {
        "error": {
            "code": "CITATION_GONE",
            "message": "引用资料已失效",
        }
    }


async def test_citation_is_gone_when_version_no_longer_belongs_to_original_item(
    citation_api_context,
):
    context = citation_api_context
    async with context.factory() as session:
        other_item = FeishuSourceItem(
            item_id="item-2",
            source_id="source-1",
            item_key="wiki:item-2",
            item_type="page",
            title="其他条目",
            source_url="https://quickdone.feishu.cn/wiki/item-2",
            source_validity="valid",
            active_version_id=None,
        )
        session.add(other_item)
        await session.flush()
        version = await session.scalar(
            select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-1")
        )
        version.item_id = other_item.item_id
        await session.commit()

    response = await context.client.get(
        f"/api/citations/{context.citation_id}",
        headers=context.owner_headers,
    )

    assert response.status_code == 410
