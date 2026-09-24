"""Permission HTTP contracts with real PostgreSQL and bearer authentication in an isolated schema."""

import os
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.role_permission_router import role_permissions
from server.utils.auth_middleware import get_db
from yuxi.storage.postgres.models_business import (
    Base,
    Department,
    FeishuDepartmentRole,
    OperationLog,
    Role,
    RoleCapability,
    RolePermission,
    User,
    UserRole,
)
from yuxi.storage.postgres.models_product import (
    FeishuDepartmentBinding,
    FeishuUserDepartmentMembership,
)
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_policy_persists_and_applies_to_existing_tokens():
    schema = "test_role_permissions_" + uuid4().hex
    engine = create_async_engine(os.environ["POSTGRES_URL"], execution_options={"schema_translate_map": {None: schema}})
    tables = [
        model.__table__
        for model in (
            Department,
            User,
            RolePermission,
            OperationLog,
            Role,
            RoleCapability,
            UserRole,
            FeishuDepartmentBinding,
            FeishuDepartmentRole,
            FeishuUserDepartmentMembership,
        )
    ]
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            department = Department(name="permission test")
            db.add(department)
            await db.flush()
            users = [
                User(username=role, uid=role, role=role, password_hash="unused", department_id=department.id)
                for role in ("superadmin", "admin")
            ]
            db.add_all(users)
            feishu_department = FeishuDepartmentBinding(
                tenant_key="tenant-test",
                feishu_department_id="department-test",
                department_id=department.id,
                display_name="飞书测试部门",
            )
            db.add(feishu_department)
            await db.commit()
            await db.refresh(feishu_department)
            await db.refresh(users[1])
            department_id = feishu_department.id
            admin_id = users[1].id
            db.add(FeishuUserDepartmentMembership(user_id=admin_id, department_binding_id=department_id, position=0))
            await db.commit()
        headers = [
            {"Authorization": "Bearer " + AuthUtils.create_access_token({"sub": str(user.id)})} for user in users
        ]
        app = FastAPI()
        app.include_router(role_permissions, prefix="/api")

        async def session():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/role-permissions/me", headers=headers[1])).json()["feedback.view"] == "none"
            for scope in ("department", "all", "none"):
                response = await client.put(
                    "/api/role-permissions/admin/feedback.view", headers=headers[0], json={"scope": scope}
                )
                assert response.status_code == 200, response.text
                assert (await client.get("/api/role-permissions/me", headers=headers[1])).json()[
                    "feedback.view"
                ] == scope
            response = await client.put(
                "/api/role-permissions/admin/feedback.view", headers=headers[1], json={"scope": "all"}
            )
            assert response.status_code == 403
            # An assistant-only token must not grant access to management permissions.
            product_token = AuthUtils.create_access_token(
                {"sub": str(users[0].id), "token_kind": "enterprise_assistant"}
            )
            response = await client.get(
                "/api/role-permissions/me", headers={"Authorization": "Bearer " + product_token}
            )
            assert response.status_code == 401
        async with factory() as db:
            assert (await db.get(RolePermission, ("admin", "feedback.view"))).scope == "none"
            assert len((await db.scalars(select(OperationLog))).all()) == 3

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/role-permissions", headers=headers[0], json={"name": "部门审阅员"})
            assert response.status_code == 200, response.text
            role_key = response.json()["role_key"]
            response = await client.put(
                f"/api/role-permissions/{role_key}/permissions",
                headers=headers[0],
                json={"permissions": {"feedback.view": "department"}},
            )
            assert response.status_code == 200, response.text
            response = await client.put(
                f"/api/role-permissions/feishu-departments/{department_id}/roles",
                headers=headers[0],
                json={"role_keys": [role_key]},
            )
            assert response.status_code == 200, response.text
            assert (await client.get("/api/role-permissions/me", headers=headers[1])).json()[
                "feedback.view"
            ] == "department"
            preview = await client.get(f"/api/role-permissions/users/{admin_id}/preview", headers=headers[0])
            assert preview.status_code == 200, preview.text
            assert (
                preview.json()["permissions"]
                == (await client.get("/api/role-permissions/me", headers=headers[1])).json()
            )
            assert preview.json()["sources"][0]["department_name"] == "飞书测试部门"
            assert (await client.get("/api/role-permissions", headers=headers[1])).status_code == 403
            # Optimistic writes must preserve another manager's saved grant.
            conflict = await client.put(
                f"/api/role-permissions/{role_key}/permissions",
                headers=headers[0],
                json={"permissions": {}, "expected_permissions": {}},
            )
            assert conflict.status_code == 409
            assert (await client.get("/api/role-permissions/me", headers=headers[1])).json()[
                "feedback.view"
            ] == "department"
            revoked = await client.put(
                f"/api/role-permissions/feishu-departments/{department_id}/roles",
                headers=headers[0],
                json={"role_keys": [], "expected_role_keys": [role_key]},
            )
            assert revoked.status_code == 200, revoked.text
            final = (await client.get(f"/api/role-permissions/users/{admin_id}/preview", headers=headers[0])).json()
            assert final["legacy_fallback"] is True
            assert final["permissions"]["feedback.view"] == "none"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()
