import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.governance_router import governance
from server.utils.auth_middleware import get_admin_user, get_db
from yuxi.storage.postgres.models_business import Base, User
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def governance_api_fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        admin_a = User(username="管理员甲", uid="admin-a", password_hash="x", role="admin")
        admin_b = User(username="管理员乙", uid="admin-b", password_hash="x", role="admin")
        source = FeishuSource(
            source_id="source-1",
            name="SD 知识库",
            wiki_root_token="root",
            target_kb_id="kb-1",
            credential_env_name="FEISHU_USER_OAUTH",
        )
        item = FeishuSourceItem(
            item_id="item-1",
            source_id="source-1",
            item_key="page:item-1",
            item_type="docx",
            title="部署指南",
            source_validity="valid",
        )
        version = FeishuMaterialVersion(
            version_id="version-1",
            item_id="item-1",
            revision="1",
            content_hash="hash-1",
            processing_status="awaiting_review",
            review_status="pending",
            yuxi_file_id="file-1",
        )
        session.add_all([admin_a, admin_b, source, item, version])
        await session.commit()

        current_user = {"value": admin_a}
        app = FastAPI()
        app.include_router(governance, prefix="/api")

        async def admin_override():
            return current_user["value"]

        async def db_override():
            yield session

        app.dependency_overrides[get_admin_user] = admin_override
        app.dependency_overrides[get_db] = db_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, session, current_user, admin_b
    await engine.dispose()


async def test_governance_review_transfer_blocks_old_reviewer_and_allows_new_assignee(governance_api_fixture):
    client, _, current_user, admin_b = governance_api_fixture

    listed = await client.get("/api/governance/reviews", params={"source_id": "source-1"})
    review_id = listed.json()["items"][0]["review_id"]
    transferred = await client.post(
        f"/api/governance/reviews/{review_id}/resolve",
        json={
            "decision": "TRANSFER",
            "action": "KEEP_CURRENT",
            "problem_tags": [],
            "decision_comment": "请产品负责人确认",
            "applicability_scope": {},
            "assignee_id": "admin-b",
        },
    )

    assert listed.status_code == 200
    assert transferred.status_code == 200
    assert transferred.json()["assignee_id"] == "admin-b"
    forbidden = await client.post(
        f"/api/governance/reviews/{transferred.json()['review_id']}/resolve",
        json={
            "decision": "REQUEST_CHANGES",
            "action": "MARK_INSUFFICIENT",
            "problem_tags": ["MISSING_SCOPE"],
            "decision_comment": "请补充版本",
            "applicability_scope": {},
        },
    )
    assert forbidden.status_code == 403

    current_user["value"] = admin_b
    resolved = await client.post(
        f"/api/governance/reviews/{transferred.json()['review_id']}/resolve",
        json={
            "decision": "REQUEST_CHANGES",
            "action": "MARK_INSUFFICIENT",
            "problem_tags": ["MISSING_SCOPE"],
            "decision_comment": "请补充版本",
            "applicability_scope": {"product": "Q900"},
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "changes_requested"


async def test_governance_reviewer_list_uses_active_admin_users(governance_api_fixture):
    client, _, _, _ = governance_api_fixture

    response = await client.get("/api/governance/reviewers")

    assert response.status_code == 200
    assert sorted(response.json()["items"], key=lambda item: item["user_id"]) == [
        {"user_id": "admin-a", "name": "管理员甲", "role": "admin"},
        {"user_id": "admin-b", "name": "管理员乙", "role": "admin"},
    ]


async def test_governance_blocks_publish_when_conflict_is_unresolved(governance_api_fixture):
    client, session, _, _ = governance_api_fixture
    session.add(
        FeishuCrossDocumentRelation(
            relation_id="relation-conflict",
            comparison_key="version-1:version-1",
            source_version_id="version-1",
            target_version_id="version-1",
            relation_type="CONFLICT",
            similarity=0.8,
            confidence=0.9,
            same_content=[],
            different_content=[{"field": "部署模式", "current": "A", "candidate": "B"}],
            scope_difference={},
            reasoning="相同条件下结论不同",
            status="open",
        )
    )
    await session.commit()

    listed = await client.get("/api/governance/reviews", params={"source_id": "source-1"})
    review_id = listed.json()["items"][0]["review_id"]
    response = await client.post(
        f"/api/governance/reviews/{review_id}/resolve",
        json={
            "decision": "PUBLISH",
            "action": "CREATE",
            "problem_tags": ["CONFLICT"],
            "decision_comment": "直接创建新知识",
            "applicability_scope": {},
        },
    )

    assert response.status_code == 409
    assert "未解决" in response.json()["detail"]
