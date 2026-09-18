"""Live configuration API checks; keep current assignments and remove the disposable identity."""

import os
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def model_clients():
    if os.getenv("TEST_MODEL_ASSIGNMENTS_LIVE") != "1":
        pytest.skip("Requires explicit live configuration validation")
    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        admin = await db.scalar(select(User).where(User.role == "superadmin", User.is_deleted == 0))
        assert admin is not None, "Initialize an administrator before testing"
        name = f"model-config-test-{uuid4().hex[:10]}"
        ordinary = User(username=name, uid=name, password_hash="not-a-login", department_id=admin.department_id)
        db.add(ordinary)
        await db.flush()
        admin_id, ordinary_id = admin.id, ordinary.id
    clients = [httpx.AsyncClient(
        base_url=os.getenv("TEST_BASE_URL", "http://localhost:5050"),
        headers={"Authorization": "Bearer " + AuthUtils.create_access_token(
            {"sub": str(uid)}, expires_delta=timedelta(minutes=5))}, timeout=30,
    ) for uid in (admin_id, ordinary_id)]
    try:
        yield clients
    finally:
        for client in clients:
            await client.aclose()
        async with pg_manager.get_async_session_context() as db:
            await db.execute(delete(User).where(User.id == ordinary_id))


async def test_config_roundtrip_rejects_invalid_assignment_without_partial_write(model_clients):
    admin, _ = model_clients
    response = await admin.get("/api/system/config")
    assert response.status_code == 200
    before = response.json()
    saved = await admin.post("/api/system/config/update", json={"default_model": before["default_model"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["default_model"] == before["default_model"]
    invalid = await admin.post("/api/system/config/update", json={
        "default_model": before["default_model"], "reranker": "missing:model-config-test",
    })
    assert invalid.status_code == 400
    after = await admin.get("/api/system/config")
    assert after.json() == before


async def test_ordinary_user_cannot_change_model_assignments(model_clients):
    admin, ordinary = model_clients
    before = (await admin.get("/api/system/config")).json()
    for path, payload in [
        ("/api/system/config/update", {"default_model": "missing:model"}),
        ("/api/system/config", {"key": "default_model", "value": "missing:model"}),
    ]:
        response = await ordinary.post(path, json=payload)
        assert response.status_code == 403
    assert (await admin.get("/api/system/config")).json() == before


async def test_title_call_ignores_legacy_model_override(model_clients):
    admin, _ = model_clients
    response = await admin.post("/api/chat/call", json={
        "query": "这是模型配置连通性检查，请只回复：配置检查通过。",
        "meta": {"model_spec": "missing:legacy-model"},
    }, timeout=90)
    assert response.status_code == 200, response.text
    assert response.json()["response"].strip()
