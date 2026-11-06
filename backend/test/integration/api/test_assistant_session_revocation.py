"""Re-check of effective grants on existing sessions; revoked access must stop immediately."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.utils.auth_middleware import get_admin_user, get_db, get_product_user
from yuxi.storage.postgres.models_business import (
    Base,
    Department,
    Role,
    RoleCapability,
    User,
    UserRole,
)
from yuxi.storage.postgres.models_product import FeishuUserBinding
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def session_app(monkeypatch):
    """One assistant account with a custom role, one binding, and both auth dependencies wired."""
    monkeypatch.setenv("JWT_SECRET_KEY", "session-revocation-test-secret-long-enough")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    department = Department(name="撤销测试部门")
    session.add(department)
    await session.flush()
    user = User(username="助手用户", uid="assistant-user", role="user", password_hash="x", department_id=department.id)
    session.add(user)
    await session.flush()
    role = Role(role_key="assistant-role", name="助手角色")
    session.add(role)
    await session.flush()
    session.add(UserRole(user_id=user.id, role_key=role.role_key))
    session.add(RoleCapability(role_key=role.role_key, permission="assistant.view", scope="all"))
    session.add(
        FeishuUserBinding(
            user_id=user.id,
            feishu_open_id="ou_test",
            tenant_key="tenant-test",
            display_name="助手用户",
        )
    )
    await session.commit()

    def cookie():
        token = AuthUtils.create_access_token(
            {"sub": str(user.id), "token_kind": "enterprise_assistant"}, expires_delta=timedelta(hours=8)
        )
        return {"enterprise_assistant_session": token}

    def bearer():
        return {"Authorization": f"Bearer {AuthUtils.create_access_token({'sub': str(user.id)})}"}

    app = FastAPI()

    @app.get("/api/session")
    async def session_route(actor=Depends(get_product_user)):
        return {"id": actor.id}

    # The management dependency derives the module from the URL path, so these paths
    # exercise the real mapping without pulling the full routers into the fixture.
    @app.get("/api/meeting-management/probe")
    async def meeting_probe(actor=Depends(get_admin_user)):
        return {"id": actor.id}

    @app.get("/api/dashboard/feedbacks/probe")
    async def feedback_probe(actor=Depends(get_admin_user)):
        return {"id": actor.id}

    async def db_override():
        yield session

    app.dependency_overrides[get_db] = db_override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield {
                "client": client,
                "session": session,
                "user": user,
                "role": role,
                "department": department,
                "cookie": cookie,
                "bearer": bearer,
                "factory": factory,
            }
    finally:
        await session.close()
        await engine.dispose()


async def test_revoking_feishu_binding_stops_an_existing_assistant_session(session_app):
    """解绑或撤销授权后，已经签发的助手会话必须立即失效。"""
    from sqlalchemy import select

    client = session_app["client"]
    binding = await session_app["session"].scalar(
        select(FeishuUserBinding).where(FeishuUserBinding.user_id == session_app["user"].id)
    )
    assert (await client.get("/api/session", cookies=session_app["cookie"]())).status_code == 200

    binding.authorization_status = "REVOKED"
    await session_app["session"].commit()

    revoked = await client.get("/api/session", cookies=session_app["cookie"]())
    assert revoked.status_code == 403
    assert revoked.json()["detail"]["code"] == "ACCESS_REVOKED"

    binding.authorization_status = "ACTIVE"
    await session_app["session"].commit()
    assert (await client.get("/api/session", cookies=session_app["cookie"]())).status_code == 200


async def test_removing_assistant_grant_stops_an_existing_assistant_session(session_app):
    """仅撤销企业助手入口权限，也必须让已打开的会话立即失去访问能力。"""
    client = session_app["client"]
    grant = await session_app["session"].get(RoleCapability, ("assistant-role", "assistant.view"))
    grant.scope = "none"
    await session_app["session"].commit()

    denied = await client.get("/api/session", cookies=session_app["cookie"]())
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "ASSISTANT_ACCESS_REQUIRED"

    grant.scope = "all"
    await session_app["session"].commit()
    assert (await client.get("/api/session", cookies=session_app["cookie"]())).status_code == 200


async def test_deleted_or_remapped_account_cannot_keep_using_the_assistant(session_app):
    """账号删除或移出部门后，旧会话必须按不同原因拒绝，而不是继续可用。"""
    client = session_app["client"]
    user = await session_app["session"].get(User, session_app["user"].id)

    user.department_id = None
    await session_app["session"].commit()
    unmapped = await client.get("/api/session", cookies=session_app["cookie"]())
    assert unmapped.status_code == 403
    assert unmapped.json()["detail"]["code"] == "IDENTITY_MAPPING_REQUIRED"

    user.department_id = session_app["department"].id
    await session_app["session"].commit()
    assert (await client.get("/api/session", cookies=session_app["cookie"]())).status_code == 200

    # 会话校验会 rollback 并 expunge，因此重新取一次对象再软删除。
    user = await session_app["session"].get(User, session_app["user"].id)
    user.is_deleted = 1
    await session_app["session"].commit()
    deleted = await client.get("/api/session", cookies=session_app["cookie"]())
    assert deleted.status_code == 401
    assert deleted.json()["detail"]["code"] == "SESSION_INVALID"


async def test_locked_account_is_refused_with_the_assistant_session(session_app):
    """登录锁定期间，已签发的助手会话同样不能继续调用接口。"""
    client = session_app["client"]
    user = await session_app["session"].get(User, session_app["user"].id)
    user.login_locked_until = utc_now_naive() + timedelta(minutes=10)
    await session_app["session"].commit()

    locked = await client.get("/api/session", cookies=session_app["cookie"]())
    assert locked.status_code == 403
    assert locked.json()["detail"]["code"] == "ACCESS_REVOKED"


async def test_assistant_only_grant_never_opens_management_endpoints(session_app):
    """只管企业助手权限的账号，用管理端令牌也不能访问会议或反馈管理接口。"""
    client = session_app["client"]
    assert (await client.get("/api/meeting-management/probe", headers=session_app["bearer"]())).status_code == 403
    assert (await client.get("/api/dashboard/feedbacks/probe", headers=session_app["bearer"]())).status_code == 403

    # 授予会议查看后只放开对应模块，反馈管理仍应拒绝。
    session_app["session"].add(
        RoleCapability(role_key="assistant-role", permission="meetings.view", scope="all")
    )
    await session_app["session"].commit()
    assert (await client.get("/api/meeting-management/probe", headers=session_app["bearer"]())).status_code == 200
    assert (await client.get("/api/dashboard/feedbacks/probe", headers=session_app["bearer"]())).status_code == 403

    # 助手令牌不能充当管理端令牌。
    assert (await client.get("/api/meeting-management/probe", cookies=session_app["cookie"]())).status_code == 401
