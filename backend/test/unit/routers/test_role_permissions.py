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
    Role,
    RoleCapability,
    RolePermission,
    UserRole,
    User,
)
from yuxi.storage.postgres.models_product import (
    FeishuDepartmentBinding,
    FeishuUserDepartmentMembership,
    ProductConversation,
    ProductMessage,
)

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
        users["other"] = other
        db.add_all([*users.values(), other])
        await db.flush()
        db.add_all(
            [
                Role(role_key="reviewer", name="审阅员", is_builtin=False),
                Role(role_key="dept_reviewer", name="部门审阅员", is_builtin=False),
            ]
        )
        feishu_department = FeishuDepartmentBinding(
            tenant_key="tenant", feishu_department_id="dept-a", department_id=departments[0].id, display_name="飞书A"
        )
        db.add(feishu_department)
        await db.flush()
        from yuxi.storage.postgres.models_business import FeishuDepartmentRole

        db.add_all(
            [
                RoleCapability(role_key="reviewer", permission="feedback.view", scope="self"),
                RoleCapability(role_key="dept_reviewer", permission="feedback.view", scope="department"),
                UserRole(user_id=users["user"].id, role_key="reviewer"),
                FeishuDepartmentRole(department_binding_id=feishu_department.id, role_key="dept_reviewer"),
                FeishuUserDepartmentMembership(
                    user_id=users["user"].id, department_binding_id=feishu_department.id, position=0
                ),
            ]
        )
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
    assert (await client.get("/api/role-permissions/me")).json()["feedback.view"] == "none"

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


async def test_user_receives_permissions_from_direct_and_feishu_department_roles(permission_api):
    client, actor, users, db = permission_api
    actor["user"] = users["user"]
    permissions = (await client.get("/api/role-permissions/me")).json()
    assert permissions["feedback.view"] == "department"
    response = await client.get("/api/dashboard/feedbacks")
    assert response.status_code == 200
    assert {item["uid"] for item in response.json()} == {"user"}


async def test_role_permissions_and_user_assignment_are_audited(permission_api):
    client, actor, users, db = permission_api
    response = await client.post("/api/role-permissions", json={"name": "知识审阅员"})
    assert response.status_code == 200, response.text
    role_key = response.json()["role_key"]
    response = await client.put(
        f"/api/role-permissions/{role_key}/permissions",
        json={"permissions": {"extensions.view": "all", "knowledge.view": "all", "knowledge.manage": "none"}},
    )
    assert response.status_code == 200, response.text
    response = await client.put(
        f"/api/role-permissions/users/{users['other'].id}/roles", json={"role_keys": [role_key]}
    )
    assert response.status_code == 200, response.text
    actor["user"] = users["other"]
    permissions = (await client.get("/api/role-permissions/me")).json()
    assert permissions["knowledge.view"] == "all"
    assert permissions["knowledge.manage"] == "none"
    actor["user"] = users["superadmin"]
    response = await client.delete(f"/api/role-permissions/{role_key}")
    assert response.status_code == 200, response.text
    logs = (await db.scalars(select(OperationLog))).all()
    assert {log.operation for log in logs} >= {"创建角色", "修改角色权限", "指派用户角色", "删除角色"}


async def test_user_without_management_permission_cannot_change_roles(permission_api):
    client, actor, users, db = permission_api
    actor["user"] = users["other"]
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


async def test_preview_matches_enforcement_and_names_all_sources(permission_api):
    client, actor, users, db = permission_api
    response = await client.get(f"/api/role-permissions/users/{users['user'].id}/preview")
    assert response.status_code == 200
    preview = response.json()
    assert {item["kind"] for item in preview["sources"]} == {"direct", "department"}
    department_source = next(item for item in preview["sources"] if item["kind"] == "department")
    assert department_source["department_name"] == "飞书A"
    assert preview["permissions"]["feedback.view"] == "department"
    actor["user"] = users["user"]
    assert (await client.get("/api/role-permissions/me")).json() == preview["permissions"]
    assert (await client.get(f"/api/role-permissions/users/{users['other'].id}/preview")).status_code == 403


async def test_permission_save_rejects_stale_snapshot_and_invalid_manage_scope(permission_api):
    client, actor, users, db = permission_api
    url = "/api/role-permissions/reviewer/permissions"
    response = await client.put(
        url, json={"permissions": {"feedback.view": "all"}, "expected_permissions": {"feedback.view": "none"}}
    )
    assert response.status_code == 409
    assert (await db.get(RoleCapability, ("reviewer", "feedback.view"))).scope == "self"
    for permissions in [
        {"meetings.manage": "all"},
        {"feedback.view": "department", "feedback.manage": "all"},
        {"meetings.view": "department"},
        {"knowledge.view": "all"},
        {"governance.view": "all"},
    ]:
        assert (await client.put(url, json={"permissions": permissions})).status_code == 422
    response = await client.put(
        url,
        json={
            "permissions": {"feedback.view": "all", "feedback.manage": "all"},
            "expected_permissions": {"feedback.view": "self", "meetings.view": "none"},
        },
    )
    assert response.status_code == 200, response.text


async def test_assignment_conflicts_and_department_revocation_update_preview(permission_api):
    client, actor, users, db = permission_api
    data = (await client.get("/api/role-permissions")).json()
    department = data["feishu_departments"][0]
    assert users["user"].id in department["user_ids"]
    url = f"/api/role-permissions/feishu-departments/{department['id']}/roles"
    assert (await client.put(url, json={"role_keys": [], "expected_role_keys": []})).status_code == 409
    response = await client.put(url, json={"role_keys": [], "expected_role_keys": ["dept_reviewer"]})
    assert response.status_code == 200
    preview = (await client.get(f"/api/role-permissions/users/{users['user'].id}/preview")).json()
    assert preview["permissions"]["feedback.view"] == "self"
    assert [source["kind"] for source in preview["sources"]] == ["direct"]
    user_url = f"/api/role-permissions/users/{users['user'].id}/roles"
    assert (await client.put(user_url, json={"role_keys": [], "expected_role_keys": []})).status_code == 409
    assert (await client.put(user_url, json={"role_keys": [], "expected_role_keys": ["reviewer"]})).status_code == 200
    preview = (await client.get(f"/api/role-permissions/users/{users['user'].id}/preview")).json()
    assert preview["permissions"]["feedback.view"] == "none"


async def test_superadmin_is_read_only_and_legacy_admin_fallback_is_explicit(permission_api):
    client, actor, users, db = permission_api
    db.add_all([Role(role_key=key, name=key, is_builtin=True) for key in ["superadmin", "admin", "user"]])
    await db.commit()
    roles = (await client.get("/api/role-permissions")).json()["roles"]
    assert next(role for role in roles if role["role_key"] == "superadmin")["permissions"]["admin.manage"] == "all"
    assert (
        await client.put("/api/role-permissions/superadmin/permissions", json={"permissions": {}})
    ).status_code == 409
    for key in ["superadmin", "admin", "user"]:
        assert (await client.delete(f"/api/role-permissions/{key}")).status_code == 409
    assert (
        await client.put(f"/api/role-permissions/users/{users['user'].id}/roles", json={"role_keys": ["admin"]})
    ).status_code == 422
    url = f"/api/role-permissions/users/{users['admin'].id}"
    preview = (await client.get(f"{url}/preview")).json()
    assert preview["legacy_fallback"] is True
    assert preview["permissions"]["meetings.manage"] == "all"
    assert (await client.put(f"{url}/roles", json={"role_keys": ["reviewer"]})).status_code == 200
    preview = (await client.get(f"{url}/preview")).json()
    assert preview["legacy_fallback"] is False
    assert preview["permissions"]["meetings.manage"] == "none"
    assert (await client.put(f"{url}/roles", json={"role_keys": []})).status_code == 200
    assert (await client.get(f"{url}/preview")).json()["legacy_fallback"] is True


async def test_delegated_manager_cannot_strip_protected_role_permissions(permission_api):
    client, actor, users, db = permission_api
    db.add(RoleCapability(role_key="reviewer", permission="admin.view", scope="all"))
    db.add(RoleCapability(role_key="reviewer", permission="admin.manage", scope="all"))
    await db.commit()
    actor["user"] = users["admin"]
    response = await client.put(
        "/api/role-permissions/reviewer/permissions", json={"permissions": {"feedback.view": "self"}}
    )
    assert response.status_code == 403
    assert (await db.get(RoleCapability, ("reviewer", "admin.manage"))).scope == "all"


async def test_superadmin_can_edit_builtin_templates_without_overriding_assigned_roles(permission_api):
    client, actor, users, db = permission_api
    db.add_all([Role(role_key=key, name=key, is_builtin=True) for key in ["admin", "user"]])
    await db.commit()
    roles = (await client.get("/api/role-permissions")).json()["roles"]
    before = next(role for role in roles if role["role_key"] == "admin")["permissions"]
    response = await client.put(
        "/api/role-permissions/admin/permissions",
        json={
            "permissions": {"meetings.view": "all"},
            "expected_permissions": before,
        },
    )
    assert response.status_code == 200, response.text
    preview = (await client.get(f"/api/role-permissions/users/{users['admin'].id}/preview")).json()
    assert preview["permissions"]["meetings.view"] == "all"
    assert preview["permissions"]["meetings.manage"] == "none"
    assert preview["permissions"]["admin.manage"] == "none"
    assert preview["legacy_fallback"] is False
    assert preview["sources"][0]["name"] == "管理员默认权限"
    assert (
        await client.put(
            "/api/role-permissions/admin/permissions",
            json={
                "permissions": {},
                "expected_permissions": before,
            },
        )
    ).status_code == 409
    response = await client.put(
        "/api/role-permissions/admin/permissions",
        json={
            "permissions": {},
            "expected_permissions": {"meetings.view": "all"},
        },
    )
    assert response.status_code == 200, response.text
    preview = (await client.get(f"/api/role-permissions/users/{users['admin'].id}/preview")).json()
    assert set(preview["permissions"].values()) == {"none"}
    assert preview["legacy_fallback"] is False
    assert (await client.put("/api/role-permissions/admin/feedback.view", json={"scope": "all"})).status_code == 409
    assert (
        await client.put(
            "/api/role-permissions/user/permissions",
            json={
                "permissions": {"meetings.view": "all"},
                "expected_permissions": {},
            },
        )
    ).status_code == 200
    # Unassigned ordinary accounts use the template; assigned accounts keep their prior policy.
    ordinary = (await client.get(f"/api/role-permissions/users/{users['other'].id}/preview")).json()
    assigned = (await client.get(f"/api/role-permissions/users/{users['user'].id}/preview")).json()
    assert ordinary["permissions"]["meetings.view"] == "all"
    assert assigned["permissions"]["meetings.view"] == "none"
    assert assigned["permissions"]["feedback.view"] == "department"
    rows = (await client.get("/api/role-permissions")).json()["roles"]
    assert next(role for role in rows if role["role_key"] == "user")["permissions"] == ordinary["permissions"]
    logs = (await db.scalars(select(OperationLog).where(OperationLog.operation == "修改角色权限"))).all()
    assert len(logs) == 3


async def test_delegated_manager_cannot_edit_builtin_templates(permission_api):
    client, actor, users, db = permission_api
    db.add_all([Role(role_key=key, name=key, is_builtin=True) for key in ["admin", "user"]])
    await db.commit()
    actor["user"] = users["admin"]
    for key in ["admin", "user"]:
        response = await client.put(
            f"/api/role-permissions/{key}/permissions", json={"permissions": {"meetings.view": "all"}}
        )
        assert response.status_code == 403
    assert await db.get(RoleCapability, ("user", "meetings.view")) is None
