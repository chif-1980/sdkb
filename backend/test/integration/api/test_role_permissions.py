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
from yuxi.storage.postgres.models_business import Base, Department, OperationLog, RolePermission, User
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_policy_persists_and_applies_to_existing_tokens():
    schema = "test_role_permissions_" + uuid4().hex
    engine = create_async_engine(os.environ["POSTGRES_URL"], execution_options={"schema_translate_map": {None: schema}})
    tables = [model.__table__ for model in (Department, User, RolePermission, OperationLog)]
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
            assert (await client.get("/api/role-permissions/me", headers=headers[1])).json() == {
                "feedback.view": "none"
            }
            for scope in ("department", "all", "none"):
                response = await client.put(
                    "/api/role-permissions/admin/feedback.view", headers=headers[0], json={"scope": scope}
                )
                assert response.status_code == 200, response.text
                assert (await client.get("/api/role-permissions/me", headers=headers[1])).json() == {
                    "feedback.view": scope
                }
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
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()
