import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.dashboard_router import dashboard
from server.routers.role_permission_router import role_permissions
from server.utils.auth_middleware import get_current_user, get_db
from yuxi.storage.postgres.models_business import (
    Base,
    Conversation,
    Department,
    Message,
    MessageFeedback,
    OperationLog,
    RolePermission,
    User,
)
from yuxi.storage.postgres.models_product import ProductConversation, ProductMessage

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture
async def permission_api():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        departments = [Department(name="A"), Department(name="B")]
        db.add_all(departments)
        await db.flush()
        users = {
            role: User(username=role, uid=role, role=role, password_hash="unused", department_id=departments[0].id)
            for role in ["superadmin", "admin", "user"]
        }
        other = User(
            username="other", uid="other", role="user", password_hash="unused", department_id=departments[1].id
        )
        db.add_all([*users.values(), other])
        await db.flush()
        for index, owner in enumerate([users["user"], other]):
            legacy = Conversation(thread_id=f"t{index}", uid=owner.uid, agent_id="test", title=owner.uid)
            product = ProductConversation(owner_user_id=owner.id, title=owner.uid)
            db.add_all([legacy, product])
            await db.flush()
            message = Message(conversation_id=legacy.id, role="assistant", content="legacy answer")
            db.add(message)
            await db.flush()
            db.add(MessageFeedback(message_id=message.id, uid=owner.uid, rating="like"))
            db.add(
                ProductMessage(
                    conversation_id=product.conversation_id,
                    role="ASSISTANT",
                    content="product answer",
                    answer_status="SUPPORTED",
                    feedback_rating="DISLIKE",
                )
            )
        await db.commit()
        app = FastAPI()
        app.include_router(role_permissions, prefix="/api")
        app.include_router(dashboard, prefix="/api")
        actor = {"user": users["superadmin"]}

        async def current_user():
            return actor["user"]

        async def session():
            yield db

        app.dependency_overrides[get_current_user] = current_user
        app.dependency_overrides[get_db] = session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, actor, users, db
    await engine.dispose()


async def test_grant_department_expand_and_revoke_with_audit(permission_api):
    client, actor, users, db = permission_api
    actor["user"] = users["admin"]
    assert (await client.get("/api/dashboard/feedbacks")).status_code == 403
    assert (await client.get("/api/role-permissions/me")).json() == {"feedback.view": "none"}

    for scope, expected in [("department", {"user"}), ("all", {"user", "other"}), ("none", set())]:
        actor["user"] = users["superadmin"]
        response = await client.put("/api/role-permissions/admin/feedback.view", json={"scope": scope})
        assert response.status_code == 200, response.text
        actor["user"] = users["admin"]
        assert (await client.get("/api/role-permissions/me")).json()["feedback.view"] == scope
        response = await client.get("/api/dashboard/feedbacks")
        if scope == "none":
            assert response.status_code == 403
        else:
            assert response.status_code == 200, response.text
            assert {item["uid"] for item in response.json()} == expected
            assert len(response.json()) == len(expected) * 2
            disliked = await client.get("/api/dashboard/feedbacks?rating=dislike")
            assert len(disliked.json()) == len(expected)
        assert (await client.get("/api/dashboard/stats")).status_code == 403
    logs = (await db.scalars(select(OperationLog).where(OperationLog.operation == "修改角色权限"))).all()
    assert len(logs) == 3
    assert all(log.user_id == users["superadmin"].id for log in logs)
    import json

    assert [json.loads(log.details)["before"] for log in logs] == ["none", "department", "all"]


@pytest.mark.parametrize("role", ["admin", "user"])
async def test_unauthorized_users_cannot_change_role_permissions(permission_api, role):
    client, actor, users, db = permission_api
    actor["user"] = users[role]
    assert (await client.get("/api/role-permissions")).status_code == 403
    response = await client.put("/api/role-permissions/admin/feedback.view", json={"scope": "all"})
    assert response.status_code == 403
    assert await db.get(RolePermission, ("admin", "feedback.view")) is None
    assert (await client.get("/api/dashboard/feedbacks")).status_code == 403


async def test_invalid_scope_and_anonymous_requests_rejected(permission_api):
    client, actor, users, db = permission_api
    response = await client.put("/api/role-permissions/admin/feedback.view", json={"scope": "invalid"})
    assert response.status_code == 422
    assert await db.get(RolePermission, ("admin", "feedback.view")) is None
    actor["user"] = None
    assert (await client.get("/api/role-permissions/me")).status_code == 401
    assert (await client.get("/api/dashboard/feedbacks")).status_code == 401
