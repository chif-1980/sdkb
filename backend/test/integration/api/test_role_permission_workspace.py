"""Exercise the role workspace against the running local API with disposable identities."""

import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.storage.postgres.models_business import Department, OperationLog, Role, User
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_live_role_save_assignment_preview_and_revoke():
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    prefix = "rbac-qa-" + uuid4().hex[:12]
    ids, role_key, department_id = [], None, None
    try:
        async with factory() as db:
            department = Department(name=prefix)
            db.add(department)
            await db.flush()
            department_id = department.id
            identities = [
                User(
                    username=f"{prefix}-{role}",
                    uid=f"{prefix}-{role}",
                    role=role,
                    password_hash="unused",
                    department_id=department_id,
                )
                for role in ("superadmin", "user")
            ]
            db.add_all(identities)
            await db.commit()
            ids = [user.id for user in identities]
        headers = [
            {"Authorization": "Bearer " + AuthUtils.create_access_token({"sub": str(user_id)})} for user_id in ids
        ]
        async with httpx.AsyncClient(
            base_url=os.getenv("TEST_BASE_URL", "http://localhost:5050"), timeout=30
        ) as client:
            response = await client.get("/api/role-permissions", headers=headers[1])
            assert response.status_code == 403, response.text
            created = await client.post("/api/role-permissions", headers=headers[0], json={"name": prefix})
            assert created.status_code == 200, created.text
            role_key = created.json()["role_key"]
            url = f"/api/role-permissions/{role_key}/permissions"
            grant = {"feedback.view": "self"}
            saved = await client.put(url, headers=headers[0], json={"permissions": grant, "expected_permissions": {}})
            assert saved.status_code == 200, saved.text
            conflict = await client.put(url, headers=headers[0], json={"permissions": {}, "expected_permissions": {}})
            assert conflict.status_code == 409, conflict.text
            assignment_url = f"/api/role-permissions/users/{ids[1]}/roles"
            assigned = await client.put(
                assignment_url, headers=headers[0], json={"role_keys": [role_key], "expected_role_keys": []}
            )
            assert assigned.status_code == 200, assigned.text
            mine = await client.get("/api/role-permissions/me", headers=headers[1])
            preview = await client.get(f"/api/role-permissions/users/{ids[1]}/preview", headers=headers[0])
            assert preview.status_code == 200, preview.text
            assert preview.json()["permissions"] == mine.json()
            assert mine.json()["feedback.view"] == "self"
            assert preview.json()["sources"][0]["kind"] == "direct"
            assert (await client.get("/api/dashboard/feedbacks", headers=headers[1])).status_code == 200
            response = await client.get("/api/role-permissions", headers=headers[1])
            assert response.status_code == 403, response.text
            revoked = await client.put(
                assignment_url, headers=headers[0], json={"role_keys": [], "expected_role_keys": [role_key]}
            )
            assert revoked.status_code == 200, revoked.text
            assert (await client.get("/api/dashboard/feedbacks", headers=headers[1])).status_code == 403
            assert (await client.delete(f"/api/role-permissions/{role_key}", headers=headers[0])).status_code == 200
    finally:
        async with factory() as db:
            if role_key:
                await db.execute(delete(Role).where(Role.role_key == role_key))
            if ids:
                await db.execute(delete(OperationLog).where(OperationLog.user_id.in_(ids)))
                await db.execute(delete(User).where(User.id.in_(ids)))
            if department_id:
                await db.execute(delete(Department).where(Department.id == department_id))
            await db.commit()
        await engine.dispose()
