"""Main-app integration contracts for the Feishu knowledge admin API."""

from __future__ import annotations

import importlib
import inspect
import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import server.routers as routers_module
from server.routers import feishu_knowledge_router as feishu_module
from server.utils.auth_middleware import get_admin_user, get_current_user, get_db
from server.utils.lifespan import lifespan

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

EXPECTED_ROUTES = {
    ("GET", "/feishu-knowledge/sources"),
    ("POST", "/feishu-knowledge/sources"),
    ("POST", "/feishu-knowledge/sources/{source_id}/check"),
    ("POST", "/feishu-knowledge/sources/{source_id}/scan"),
    ("GET", "/feishu-knowledge/sources/{source_id}/runs"),
    ("GET", "/feishu-knowledge/runs/{run_id}"),
    ("GET", "/feishu-knowledge/sources/{source_id}/materials"),
    ("GET", "/feishu-knowledge/materials/{version_id}"),
    ("GET", "/feishu-knowledge/materials/{version_id}/events"),
    ("POST", "/feishu-knowledge/materials/{version_id}/approve"),
    ("POST", "/feishu-knowledge/materials/{version_id}/reject"),
    ("POST", "/feishu-knowledge/materials/{version_id}/retry"),
    ("POST", "/feishu-knowledge/materials/batch-action"),
    ("POST", "/feishu-knowledge/materials/{version_id}/confirm-removal"),
}


@pytest.fixture(scope="session")
def ensure_live_api_schema():
    """This module uses an in-process app and must not initialize live services."""


@pytest.fixture(scope="session")
def cleanup_test_knowledge_databases():
    """This module creates no live knowledge databases."""


@pytest.fixture(scope="session")
def cleanup_test_sandboxes():
    """This module creates no sandbox containers."""


class FakeResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self.values


class FakeDatabase:
    def __init__(self, source):
        self.source = source

    async def execute(self, _statement):
        return FakeResult([self.source])


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(routers_module.router, prefix="/api")
    return app


def _registered_feishu_routes() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in routers_module.router.routes
        if route.path.startswith("/feishu-knowledge")
        for method in route.methods or set()
    }


async def test_main_router_registers_every_feishu_admin_endpoint():
    assert _registered_feishu_routes() == EXPECTED_ROUTES


async def test_lite_mode_does_not_register_feishu_knowledge_routes():
    original = os.environ.get("LITE_MODE")
    try:
        os.environ["LITE_MODE"] = "true"
        lite_module = importlib.reload(routers_module)
        assert not any(route.path.startswith("/feishu-knowledge") for route in lite_module.router.routes)
    finally:
        if original is None:
            os.environ.pop("LITE_MODE", None)
        else:
            os.environ["LITE_MODE"] = original
        importlib.reload(routers_module)


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("GET", "/api/feishu-knowledge/sources", None),
        (
            "POST",
            "/api/feishu-knowledge/sources",
            {"name": "Docs", "wiki_root_token": "root", "target_kb_id": "kb-1"},
        ),
        ("POST", "/api/feishu-knowledge/sources/source-1/check", None),
        ("POST", "/api/feishu-knowledge/sources/source-1/scan", {"mode": "incremental"}),
        ("GET", "/api/feishu-knowledge/sources/source-1/runs", None),
        ("GET", "/api/feishu-knowledge/runs/run-1", None),
        ("GET", "/api/feishu-knowledge/sources/source-1/materials", None),
        ("GET", "/api/feishu-knowledge/materials/version-1", None),
        ("GET", "/api/feishu-knowledge/materials/version-1/events", None),
        ("POST", "/api/feishu-knowledge/materials/version-1/approve", None),
        ("POST", "/api/feishu-knowledge/materials/version-1/reject", {"reason": "obsolete"}),
        ("POST", "/api/feishu-knowledge/materials/version-1/retry", None),
        (
            "POST",
            "/api/feishu-knowledge/materials/batch-action",
            {"action": "approve", "version_ids": ["version-1"]},
        ),
        ("POST", "/api/feishu-knowledge/materials/version-1/confirm-removal", None),
    ],
)
async def test_every_feishu_endpoint_rejects_anonymous_requests(method, path, json):
    app = _app()

    async def fake_db():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = fake_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json=json)

    assert response.status_code == 401


async def test_feishu_endpoints_reject_non_admin_users():
    app = _app()

    async def standard_user():
        return SimpleNamespace(uid="user-1", role="user", department_id=1)

    async def fake_db():
        yield SimpleNamespace()

    app.dependency_overrides[get_current_user] = standard_user
    app.dependency_overrides[get_db] = fake_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/feishu-knowledge/sources")

    assert response.status_code == 403


async def test_admin_create_check_scan_query_reject_and_approve_contracts(monkeypatch):
    source = SimpleNamespace(
        source_id="source-1",
        name="Docs",
        wiki_root_token="root",
        wiki_root_url=None,
        target_kb_id="kb-1",
        credential_env_name="FEISHU_ACCESS_TOKEN",
        enabled=True,
        created_at=None,
        updated_at=None,
    )
    database = FakeDatabase(source)
    calls = []

    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_or_create_source(self, **kwargs):
            calls.append(("create", kwargs))
            return SimpleNamespace(**kwargs, created_at=None, updated_at=None)

        async def get_source(self, source_id):
            assert source_id == "source-1"
            return source

        async def queue_sync_run(self, **kwargs):
            calls.append(("queue_scan", kwargs))
            return SimpleNamespace(run_id="run-1")

    class FakeFeishuClient:
        def __init__(self, *, credential_env_name):
            calls.append(("client", credential_env_name))

        async def get_node(self, token):
            calls.append(("get_node", token))
            return SimpleNamespace(title="Root")

        async def aclose(self):
            calls.append(("close",))

    class FakeReviewService:
        def __init__(self, _session):
            pass

        async def reject(self, version_id, *, operator_id, reason):
            calls.append(("reject", version_id, operator_id, reason))
            return SimpleNamespace(version_id=version_id, review_status="rejected")

        async def approve(self, version_id, *, operator_id):
            calls.append(("approve", version_id, operator_id))
            return SimpleNamespace(version_id=version_id, processing_status="publish_queued")

    async def fake_enqueue_unique_by_payload(**kwargs):
        calls.append(("enqueue_scan", kwargs["payload"]))
        return SimpleNamespace(id="task-scan", payload=kwargs["payload"]), True

    async def fake_enqueue_publish(version_id, *, operator_id):
        calls.append(("enqueue_publish", version_id, operator_id))
        return SimpleNamespace(id="task-publish")

    monkeypatch.setattr(feishu_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(feishu_module, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(feishu_module, "FeishuReviewService", FakeReviewService)
    monkeypatch.setattr(feishu_module.tasker, "enqueue_unique_by_payload", fake_enqueue_unique_by_payload)
    monkeypatch.setattr(feishu_module, "_enqueue_publish", fake_enqueue_publish)

    app = _app()

    async def admin_user():
        return SimpleNamespace(uid="admin-1", role="admin", department_id=1)

    async def fake_db():
        yield database

    app.dependency_overrides[get_admin_user] = admin_user
    app.dependency_overrides[get_db] = fake_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/feishu-knowledge/sources",
            json={"name": "Docs", "wiki_root_token": "root", "target_kb_id": "kb-1"},
        )
        checked = await client.post("/api/feishu-knowledge/sources/source-1/check")
        scanned = await client.post(
            "/api/feishu-knowledge/sources/source-1/scan",
            json={"mode": "incremental"},
        )
        queried = await client.get("/api/feishu-knowledge/sources")
        rejected = await client.post(
            "/api/feishu-knowledge/materials/version-1/reject",
            json={"reason": "obsolete"},
        )
        approved = await client.post("/api/feishu-knowledge/materials/version-1/approve")

    assert created.status_code == 201
    assert created.json()["credential_env_name"] == "FEISHU_ACCESS_TOKEN"
    assert checked.status_code == 200
    assert checked.json() == {"status": "ok", "source_id": "source-1", "root_title": "Root"}
    assert scanned.status_code == 202
    assert scanned.json() == {"task_id": "task-scan", "run_id": "run-1", "status": "queued", "created": True}
    assert queried.status_code == 200
    assert queried.json()["items"][0]["source_id"] == "source-1"
    assert rejected.status_code == 200
    assert rejected.json() == {"version_id": "version-1", "status": "rejected"}
    assert approved.status_code == 202
    assert approved.json() == {"version_id": "version-1", "status": "publish_queued", "task_id": "task-publish"}
    assert ("get_node", "root") in calls
    assert ("reject", "version-1", "admin-1", "obsolete") in calls
    assert ("approve", "version-1", "admin-1") in calls


async def test_lifespan_does_not_scan_or_schedule_feishu_work():
    source = inspect.getsource(lifespan)

    assert "ensure_knowledge_schema" in source
    assert "await tasker.start()" in source
    assert "feishu" not in source.lower()


@pytest.mark.skipif(not os.getenv("FEISHU_ACCESS_TOKEN"), reason="Real Feishu credentials are not configured")
async def test_real_feishu_network_scan_is_deferred_to_task_6_e2e():
    pytest.skip("Task 6 owns the opt-in real Feishu network scan")
