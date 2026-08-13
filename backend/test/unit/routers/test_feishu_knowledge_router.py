"""Unit contracts for the Feishu knowledge admin API (Task 4)."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.routers import feishu_knowledge_router as router_module
from server.routers.feishu_knowledge_router import FeishuReviewService, feishu_knowledge
from server.utils.auth_middleware import get_admin_user, get_current_user, get_db
from yuxi.integrations.feishu.client import FeishuPermissionError
from yuxi.integrations.feishu.schemas import FeishuNode, FeishuPageContent
from yuxi.knowledge.utils.kb_utils import prepare_item_metadata
from yuxi.repositories.feishu_knowledge_repository import ConcurrentSyncRunError, FeishuKnowledgeRepository
from yuxi.storage.postgres.models_business import Base, User
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuSource,
    FeishuSourceItem,
    FeishuSyncRun,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def review_fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        source = FeishuSource(
            source_id="source-1",
            name="Source",
            wiki_root_token="root",
            target_kb_id="kb-1",
            credential_env_name="FEISHU_ACCESS_TOKEN",
        )
        item = FeishuSourceItem(
            item_id="item-1",
            source_id="source-1",
            item_key="page:space:node",
            item_type="page",
            title="Page",
            source_validity="valid",
            active_version_id="version-old",
        )
        old = FeishuMaterialVersion(
            version_id="version-old",
            item_id="item-1",
            revision="1",
            content_hash="old-hash",
            processing_status="published",
            review_status="approved",
            yuxi_file_id="file-old",
        )
        current = FeishuMaterialVersion(
            version_id="version-new",
            item_id="item-1",
            revision="2",
            content_hash="new-hash",
            processing_status="parsed",
            review_status="pending",
        )
        session.add_all([source, item, old, current])
        await session.commit()
        yield session
    await engine.dispose()


async def _client(*, user=None):
    app = FastAPI()
    app.include_router(feishu_knowledge, prefix="/api")

    async def admin_override():
        if user is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    async def db_override():
        yield SimpleNamespace()

    app.dependency_overrides[get_admin_user] = admin_override
    app.dependency_overrides[get_db] = db_override
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _database_client(session, *, user=None):
    app = FastAPI()
    app.include_router(feishu_knowledge, prefix="/api")

    async def admin_override():
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    async def db_override():
        yield session

    app.dependency_overrides[get_admin_user] = admin_override
    app.dependency_overrides[get_db] = db_override
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _FailingAfterArchiveFeishuClient:
    async def get_node(self, node_token):
        assert node_token == "root"
        return FeishuNode(
            space_id="space-1",
            node_token="root",
            obj_token="obj-root",
            obj_type="docx",
            title="Root",
            has_child=True,
            source_updated_at="2026-08-13T00:00:00Z",
        )

    async def get_wiki_document(self, node):
        assert node.node_token == "root"
        return FeishuPageContent(content=b"# Root", revision="1")

    async def list_children(self, parent_node_token):
        assert parent_node_token == "root"
        raise FeishuPermissionError("permission denied")

    async def aclose(self):
        pass


class _StableArchiveAdapter:
    async def archive(self, **kwargs):
        return (
            f"minio://knowledgebases/feishu/{kwargs['source_id']}/{kwargs['item_id']}/{kwargs['version_id']}/source.md"
        )


@asynccontextmanager
async def _production_session_context(session_factory):
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _seed_feishu_source(session_factory):
    async with session_factory() as session:
        await router_module.FeishuKnowledgeRepository(session).get_or_create_source(
            source_id="source-1",
            name="Source",
            wiki_root_token="root",
            wiki_root_url="https://example.feishu.cn/wiki/root",
            target_kb_id="kb-1",
            credential_env_name="FEISHU_ACCESS_TOKEN",
        )
        await session.commit()


async def test_router_module_exposes_all_admin_endpoints():
    paths = {route.path for route in feishu_knowledge.routes}
    assert "/feishu-knowledge/sources" in paths
    assert "/feishu-knowledge/sources/{source_id}/scan" in paths
    assert "/feishu-knowledge/materials/{version_id}/approve" in paths
    assert "/feishu-knowledge/materials/batch-action" in paths


async def test_batch_action_rejects_empty_or_more_than_100_items():
    async with await _client(user=SimpleNamespace(uid="admin", role="admin")) as client:
        empty = await client.post(
            "/api/feishu-knowledge/materials/batch-action", json={"action": "approve", "version_ids": []}
        )
        too_many = await client.post(
            "/api/feishu-knowledge/materials/batch-action",
            json={"action": "approve", "version_ids": [str(i) for i in range(101)]},
        )
    assert empty.status_code == 422
    assert too_many.status_code == 422


async def test_reject_requires_non_blank_reason():
    async with await _client(user=SimpleNamespace(uid="admin", role="admin")) as client:
        response = await client.post(
            "/api/feishu-knowledge/materials/version-1/reject",
            json={"reason": "   "},
        )
    assert response.status_code == 422


async def test_scan_only_accepts_full_or_incremental():
    async with await _client(user=SimpleNamespace(uid="admin", role="admin")) as client:
        response = await client.post(
            "/api/feishu-knowledge/sources/source-1/scan",
            json={"mode": "delta"},
        )
    assert response.status_code == 422


async def test_approve_queues_publish_without_replacing_active(review_fixture):
    service = FeishuReviewService(review_fixture)

    material = await service.approve("version-new", operator_id="admin")

    item = await review_fixture.get(FeishuSourceItem, 1)
    assert material.review_status == "approved"
    assert material.processing_status == "publish_queued"
    assert item.active_version_id == "version-old"


async def test_publish_success_switches_active_and_replaces_old_version(review_fixture):
    service = FeishuReviewService(review_fixture)
    await service.approve("version-new", operator_id="admin")

    switch = await service.mark_publish_succeeded("version-new", yuxi_file_id="file-new")

    item = await review_fixture.get(FeishuSourceItem, 1)
    old = await review_fixture.get(FeishuMaterialVersion, 1)
    new = await review_fixture.get(FeishuMaterialVersion, 2)
    assert item.active_version_id == "version-new"
    assert old.processing_status == "replaced"
    assert old.replaced_at is not None
    assert new.processing_status == "published"
    assert new.yuxi_file_id == "file-new"
    assert switch.material == new
    assert switch.replaced_file_id == "file-old"


async def test_publish_failure_keeps_old_active_and_records_event(review_fixture):
    service = FeishuReviewService(review_fixture)
    await service.approve("version-new", operator_id="admin")

    await service.mark_publish_failed("version-new", message="index failed")

    item = await review_fixture.get(FeishuSourceItem, 1)
    new = await review_fixture.get(FeishuMaterialVersion, 2)
    assert item.active_version_id == "version-old"
    assert new.processing_status == "publish_failed"
    assert new.error_message == "index failed"
    events = (await review_fixture.execute(FeishuProcessingEvent.__table__.select())).all()
    assert events[-1].event_type == "publish_failed"


async def test_retry_only_accepts_failed_status_and_increments_counter(review_fixture):
    service = FeishuReviewService(review_fixture)
    with pytest.raises(ValueError, match="failed"):
        await service.retry("version-new", operator_id="admin")

    current = await review_fixture.get(FeishuMaterialVersion, 2)
    current.processing_status = "parse_failed"
    await review_fixture.commit()
    retried = await service.retry("version-new", operator_id="admin")
    assert retried.processing_status == "processing_queued"
    assert retried.retry_count == 1


async def test_retry_rejects_removal_failed_without_changing_material(review_fixture):
    service = FeishuReviewService(review_fixture)
    version = await review_fixture.get(FeishuMaterialVersion, 1)
    version.processing_status = "removal_failed"
    version.error_message = "delete failed"
    await review_fixture.commit()

    with pytest.raises(ValueError, match="retried"):
        await service.retry("version-old", operator_id="admin")

    await review_fixture.refresh(version)
    events = list(
        (
            await review_fixture.execute(
                select(FeishuProcessingEvent).where(FeishuProcessingEvent.version_id == "version-old")
            )
        ).scalars()
    )
    assert version.processing_status == "removal_failed"
    assert version.retry_count == 0
    assert version.error_message == "delete failed"
    assert events == []


async def test_confirm_removal_requires_invalid_source_and_real_adapter(review_fixture):
    removed = []

    class RemovalAdapter:
        async def remove(self, *, kb_id: str, file_id: str) -> None:
            removed.append((kb_id, file_id))

    service = FeishuReviewService(review_fixture, removal_adapter=RemovalAdapter())
    with pytest.raises(ValueError, match="invalid"):
        await service.confirm_removal("version-old", operator_id="admin")

    item = await review_fixture.get(FeishuSourceItem, 1)
    item.source_validity = "invalid"
    await review_fixture.commit()
    removed_version = await service.confirm_removal("version-old", operator_id="admin")
    assert removed == [("kb-1", "file-old")]
    assert removed_version.processing_status == "removed"
    assert item.active_version_id is None


async def test_scan_uses_unique_task_by_source_and_returns_task_and_run_ids(monkeypatch):
    captured = {}

    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_source(self, source_id):
            assert source_id == "source-1"
            return SimpleNamespace(source_id=source_id, name="Source")

        async def queue_sync_run(self, **kwargs):
            captured["run"] = kwargs
            return SimpleNamespace(run_id="run-1")

    async def fake_enqueue_unique_by_payload(**kwargs):
        captured["task"] = kwargs
        return SimpleNamespace(id="task-1"), True

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", fake_enqueue_unique_by_payload)

    class FakeDb:
        async def commit(self):
            pass

    result = await router_module.scan_source(
        "source-1",
        router_module.ScanRequest(mode="full"),
        db=FakeDb(),
        current_user=SimpleNamespace(uid="admin"),
    )

    assert result == {"task_id": "task-1", "run_id": "run-1", "status": "queued", "created": True}
    assert captured["task"]["payload_match"] == {"source_id": "source-1"}
    assert captured["task"]["statuses"] == {"pending", "running"}


async def test_scan_commits_before_task_enqueue(monkeypatch):
    calls = []

    class FakeDb:
        async def commit(self):
            calls.append("commit")

    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_source(self, source_id):
            return SimpleNamespace(source_id=source_id, name="Source")

        async def queue_sync_run(self, **kwargs):
            return SimpleNamespace(run_id="run-1")

    async def fake_enqueue_unique_by_payload(**kwargs):
        calls.append("enqueue")
        return SimpleNamespace(id="task-1", payload=kwargs["payload"]), True

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", fake_enqueue_unique_by_payload)

    await router_module.scan_source(
        "source-1",
        router_module.ScanRequest(mode="full"),
        db=FakeDb(),
        current_user=SimpleNamespace(uid="admin"),
    )

    assert calls == ["commit", "enqueue"]


async def test_successful_scan_queues_archived_material_processing(monkeypatch):
    captured = {}
    calls = []

    class FakeDb:
        async def commit(self):
            calls.append("request_commit")

    class FakeRepository:
        def __init__(self, _session, *, queued_run_id=None):
            self.queued_run_id = queued_run_id

        async def get_source(self, source_id):
            return SimpleNamespace(
                source_id=source_id,
                name="Source",
                credential_env_name="FEISHU_TOKEN",
            )

        async def queue_sync_run(self, **kwargs):
            return SimpleNamespace(run_id="run-1")

        async def queue_archived_versions_for_processing(self, *, source_id, operator_id):
            calls.append(("claim", source_id, operator_id))
            return ["version-1"]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def aclose(self):
            pass

    class FakeScanService:
        def __init__(self, **kwargs):
            pass

        async def scan(self, **kwargs):
            return SimpleNamespace(run_id="run-1", status="succeeded")

    class WorkerDb:
        async def commit(self):
            calls.append("worker_commit")

    @asynccontextmanager
    async def fake_session_context():
        yield WorkerDb()

    async def capture_scan(**kwargs):
        captured["coroutine"] = kwargs["coroutine"]
        return SimpleNamespace(id="task-scan", payload=kwargs["payload"]), True

    async def enqueue_processing(version_id, *, operator_id):
        calls.append(("processing", version_id, operator_id))
        return SimpleNamespace(id="task-process")

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router_module, "FeishuClient", FakeClient)
    monkeypatch.setattr(router_module, "FeishuScanService", FakeScanService)
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", capture_scan)
    monkeypatch.setattr(router_module, "_enqueue_processing", enqueue_processing)

    await router_module.scan_source(
        "source-1",
        router_module.ScanRequest(mode="full"),
        db=FakeDb(),
        current_user=SimpleNamespace(uid="admin"),
    )
    context = SimpleNamespace(set_result=lambda value: None)

    async def set_result(value):
        captured["result"] = value

    context.set_result = set_result
    await captured["coroutine"](context)

    assert calls == [
        "request_commit",
        "worker_commit",
        ("claim", "source-1", "admin"),
        "worker_commit",
        ("processing", "version-1", "admin"),
    ]


async def test_failed_scan_commits_domain_state_before_task_failure(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'failed-scan.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    captured = {}
    processing_calls = []

    async def capture_scan(**kwargs):
        captured["coroutine"] = kwargs["coroutine"]
        return SimpleNamespace(id="task-scan", payload=kwargs["payload"]), True

    async def unexpected_processing(version_id, *, operator_id):
        processing_calls.append((version_id, operator_id))

    monkeypatch.setattr(router_module, "FeishuClient", lambda **kwargs: _FailingAfterArchiveFeishuClient())
    monkeypatch.setattr(router_module, "MinioFeishuArchiveAdapter", lambda: _StableArchiveAdapter())
    monkeypatch.setattr(
        router_module.pg_manager,
        "get_async_session_context",
        lambda: _production_session_context(session_factory),
    )
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", capture_scan)
    monkeypatch.setattr(router_module, "_enqueue_processing", unexpected_processing)

    await _seed_feishu_source(session_factory)
    async with session_factory() as request_session:
        response = await router_module.scan_source(
            "source-1",
            router_module.ScanRequest(mode="full"),
            db=request_session,
            current_user=SimpleNamespace(uid="admin"),
        )

    result_values = []

    class Context:
        async def set_result(self, value):
            result_values.append(value)

    with pytest.raises(RuntimeError, match="FeishuPermissionError: permission denied"):
        await captured["coroutine"](Context())

    async with session_factory() as verification_session:
        run = await verification_session.scalar(select(FeishuSyncRun).where(FeishuSyncRun.run_id == response["run_id"]))
        events = list(
            (
                await verification_session.execute(
                    select(FeishuProcessingEvent).where(FeishuProcessingEvent.event_type == "scan_failed")
                )
            ).scalars()
        )
        version = await verification_session.scalar(select(FeishuMaterialVersion))

        assert run.status == "failed"
        assert "FeishuPermissionError: permission denied" in run.error_summary
        assert len(events) == 1
        assert events[0].payload_json == {"run_id": run.run_id}
        assert version.source_object_path
        assert version.processing_params["object_path"] == version.source_object_path
        assert version.processing_status == "discovered"

        next_run = await router_module.FeishuKnowledgeRepository(verification_session).queue_sync_run(
            source_id="source-1",
            run_type="full",
            operator_id="admin",
        )
        await verification_session.commit()
        claimed = await router_module.FeishuKnowledgeRepository(
            verification_session,
            queued_run_id=next_run.run_id,
        ).start_sync_run(source_id="source-1", run_type="full", operator_id="admin")
        assert claimed.status == "running"

    assert result_values == [{"run_id": response["run_id"], "status": "failed"}]
    assert processing_calls == []
    await engine.dispose()


async def test_scan_domain_commit_failure_uses_fresh_session_to_fail_run(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'commit-failure.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    request_session_factory = async_sessionmaker(engine, expire_on_commit=False)
    commit_calls = 0

    class FailFirstCommitSession(AsyncSession):
        async def commit(self):
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 1:
                raise RuntimeError("forced domain commit failure")
            await super().commit()

    worker_session_factory = async_sessionmaker(
        engine,
        class_=FailFirstCommitSession,
        expire_on_commit=False,
    )
    captured = {}

    async def capture_scan(**kwargs):
        captured["coroutine"] = kwargs["coroutine"]
        return SimpleNamespace(id="task-scan", payload=kwargs["payload"]), True

    monkeypatch.setattr(router_module, "FeishuClient", lambda **kwargs: _FailingAfterArchiveFeishuClient())
    monkeypatch.setattr(router_module, "MinioFeishuArchiveAdapter", lambda: _StableArchiveAdapter())
    monkeypatch.setattr(
        router_module.pg_manager,
        "get_async_session_context",
        lambda: _production_session_context(worker_session_factory),
    )
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", capture_scan)

    await _seed_feishu_source(request_session_factory)
    async with request_session_factory() as request_session:
        response = await router_module.scan_source(
            "source-1",
            router_module.ScanRequest(mode="full"),
            db=request_session,
            current_user=SimpleNamespace(uid="admin"),
        )

    class Context:
        async def set_result(self, value):
            pass

    with pytest.raises(RuntimeError, match="forced domain commit failure"):
        await captured["coroutine"](Context())

    async with request_session_factory() as verification_session:
        run = await verification_session.scalar(select(FeishuSyncRun).where(FeishuSyncRun.run_id == response["run_id"]))
        events = list(
            (
                await verification_session.execute(
                    select(FeishuProcessingEvent).where(FeishuProcessingEvent.event_type == "scan_failed")
                )
            ).scalars()
        )

    assert commit_calls >= 2
    assert run.status == "failed"
    assert run.error_summary == "FeishuPermissionError: permission denied"
    assert len(events) == 1
    assert events[0].payload_json == {"run_id": run.run_id}
    await engine.dispose()


@pytest.mark.parametrize("failure_stage", ["source_lookup", "client_constructor"])
async def test_scan_worker_initialization_failure_marks_run_failed(monkeypatch, failure_stage):
    captured = {}
    calls = []

    class FakeDb:
        async def commit(self):
            calls.append("request_commit")

    class FakeRepository:
        def __init__(self, _session, *, queued_run_id=None):
            self.queued_run_id = queued_run_id

        async def get_source(self, source_id):
            if self.queued_run_id and failure_stage == "source_lookup":
                return None
            return SimpleNamespace(
                source_id=source_id,
                name="Source",
                credential_env_name="FEISHU_TOKEN",
            )

        async def queue_sync_run(self, **kwargs):
            return SimpleNamespace(run_id="run-1")

        async def fail_sync_run(self, *, run_id, source_id, error_summary, operator_id):
            calls.append(("failed", run_id, source_id, error_summary, operator_id))
            return True

    class FailingClient:
        def __init__(self, **kwargs):
            raise RuntimeError("credential unavailable")

    class WorkerDb:
        async def commit(self):
            calls.append("worker_commit")

    @asynccontextmanager
    async def fake_session_context():
        yield WorkerDb()

    async def capture_scan(**kwargs):
        captured["coroutine"] = kwargs["coroutine"]
        return SimpleNamespace(id="task-scan", payload=kwargs["payload"]), True

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router_module, "FeishuClient", FailingClient)
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", capture_scan)

    await router_module.scan_source(
        "source-1",
        router_module.ScanRequest(mode="full"),
        db=FakeDb(),
        current_user=SimpleNamespace(uid="admin"),
    )

    with pytest.raises((LookupError, RuntimeError)):
        await captured["coroutine"](SimpleNamespace())

    expected_message = (
        "LookupError: Feishu source not found: source-1"
        if failure_stage == "source_lookup"
        else "RuntimeError: credential unavailable"
    )
    assert calls == [
        "request_commit",
        ("failed", "run-1", "source-1", expected_message, "admin"),
        "worker_commit",
    ]


async def test_scan_worker_concurrent_claim_failure_marks_queued_run_failed(monkeypatch):
    captured = {}
    calls = []

    class FakeDb:
        async def commit(self):
            calls.append("request_commit")

    class FakeRepository:
        def __init__(self, _session, *, queued_run_id=None):
            self.queued_run_id = queued_run_id

        async def get_source(self, source_id):
            return SimpleNamespace(
                source_id=source_id,
                name="Source",
                credential_env_name="FEISHU_TOKEN",
            )

        async def queue_sync_run(self, **kwargs):
            return SimpleNamespace(run_id="run-2")

        async def fail_sync_run(self, *, run_id, source_id, error_summary, operator_id):
            calls.append(("failed", run_id, source_id, error_summary, operator_id))
            return True

    class FakeClient:
        async def aclose(self):
            calls.append("client_close")

    class FakeScanService:
        def __init__(self, **kwargs):
            pass

        async def scan(self, **kwargs):
            raise ConcurrentSyncRunError("A Feishu sync run is already active for source: source-1")

    class WorkerDb:
        async def commit(self):
            calls.append("worker_commit")

    @asynccontextmanager
    async def fake_session_context():
        yield WorkerDb()

    async def capture_scan(**kwargs):
        captured["coroutine"] = kwargs["coroutine"]
        return SimpleNamespace(id="task-scan", payload=kwargs["payload"]), True

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router_module, "FeishuClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(router_module, "FeishuScanService", FakeScanService)
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", capture_scan)

    await router_module.scan_source(
        "source-1",
        router_module.ScanRequest(mode="full"),
        db=FakeDb(),
        current_user=SimpleNamespace(uid="admin"),
    )

    with pytest.raises(ConcurrentSyncRunError):
        await captured["coroutine"](SimpleNamespace())

    assert calls == [
        "request_commit",
        (
            "failed",
            "run-2",
            "source-1",
            "ConcurrentSyncRunError: A Feishu sync run is already active for source: source-1",
            "admin",
        ),
        "worker_commit",
        "client_close",
    ]


async def test_router_repository_queued_run_claim_honors_same_source_mutex(review_fixture):
    runs = [
        FeishuSyncRun(run_id="run-first", source_id="source-1", run_type="full", status="queued"),
        FeishuSyncRun(run_id="run-second", source_id="source-1", run_type="full", status="queued"),
    ]
    review_fixture.add_all(runs)
    await review_fixture.commit()

    first = await router_module.FeishuKnowledgeRepository(
        review_fixture,
        queued_run_id="run-first",
    ).start_sync_run(source_id="source-1", run_type="full", operator_id="admin-1")

    with pytest.raises(ConcurrentSyncRunError):
        await router_module.FeishuKnowledgeRepository(
            review_fixture,
            queued_run_id="run-second",
        ).start_sync_run(source_id="source-1", run_type="full", operator_id="admin-2")

    assert first.status == "running"
    assert runs[1].status == "queued"


async def test_rejected_queued_scan_worker_persists_failed_run_and_event(monkeypatch, review_fixture):
    running = FeishuSyncRun(
        run_id="run-active",
        source_id="source-1",
        run_type="full",
        status="running",
    )
    review_fixture.add(running)
    await review_fixture.commit()
    captured = {}

    class FakeClient:
        async def aclose(self):
            pass

    @asynccontextmanager
    async def fake_session_context():
        yield review_fixture

    async def capture_scan(**kwargs):
        captured["coroutine"] = kwargs["coroutine"]
        return SimpleNamespace(id="task-scan", payload=kwargs["payload"]), True

    monkeypatch.setattr(router_module, "FeishuClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", capture_scan)

    response = await router_module.scan_source(
        "source-1",
        router_module.ScanRequest(mode="full"),
        db=review_fixture,
        current_user=SimpleNamespace(uid="admin"),
    )

    with pytest.raises(ConcurrentSyncRunError):
        await captured["coroutine"](SimpleNamespace())

    rejected = (
        await review_fixture.execute(select(FeishuSyncRun).where(FeishuSyncRun.run_id == response["run_id"]))
    ).scalar_one()
    event = (
        await review_fixture.execute(
            select(FeishuProcessingEvent).where(
                FeishuProcessingEvent.event_type == "scan_failed",
                FeishuProcessingEvent.payload_json == {"run_id": rejected.run_id},
            )
        )
    ).scalar_one()
    assert rejected.status == "failed"
    assert rejected.finished_at is not None
    assert rejected.error_summary == (
        "ConcurrentSyncRunError: A Feishu sync run is already active for source: source-1"
    )
    assert (event.from_status, event.to_status, event.operator_id) == ("queued", "failed", "admin")


async def test_duplicate_scan_returns_existing_task_and_rolls_back_unused_run(monkeypatch):
    captured = {"cancelled": [], "commits": 0}

    class FakeDb:
        async def commit(self):
            captured["commits"] += 1

    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_source(self, source_id):
            return SimpleNamespace(source_id=source_id, name="Source")

        async def queue_sync_run(self, **kwargs):
            return SimpleNamespace(run_id="run-unused")

        async def cancel_queued_run(self, run_id):
            captured["cancelled"].append(run_id)

        async def get_sync_run_status(self, run_id):
            assert run_id == "run-existing"
            return "running"

    async def fake_enqueue_unique_by_payload(**kwargs):
        return SimpleNamespace(id="task-existing", payload={"run_id": "run-existing"}), False

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", fake_enqueue_unique_by_payload)

    result = await router_module.scan_source(
        "source-1",
        router_module.ScanRequest(mode="incremental"),
        db=FakeDb(),
        current_user=SimpleNamespace(uid="admin"),
    )

    assert result["task_id"] == "task-existing"
    assert result["run_id"] == "run-existing"
    assert result["created"] is False
    assert captured["cancelled"] == ["run-unused"]
    assert captured["commits"] == 2


async def test_scan_does_not_reuse_running_task_for_terminal_run(monkeypatch):
    captured = {"cancelled": [], "enqueue_statuses": []}

    class FakeDb:
        async def commit(self):
            pass

    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_source(self, source_id):
            return SimpleNamespace(source_id=source_id, name="Source")

        async def queue_sync_run(self, **kwargs):
            return SimpleNamespace(run_id="run-new")

        async def get_sync_run_status(self, run_id):
            assert run_id == "run-old"
            return "failed"

        async def cancel_queued_run(self, run_id):
            captured["cancelled"].append(run_id)

    async def fake_enqueue_unique_by_payload(**kwargs):
        captured["enqueue_statuses"].append(kwargs["statuses"])
        if "running" in kwargs["statuses"]:
            return SimpleNamespace(id="task-old", payload={"run_id": "run-old"}), False
        return SimpleNamespace(id="task-new", payload=kwargs["payload"]), True

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", fake_enqueue_unique_by_payload)

    result = await router_module.scan_source(
        "source-1",
        router_module.ScanRequest(mode="incremental"),
        db=FakeDb(),
        current_user=SimpleNamespace(uid="admin"),
    )

    assert result == {
        "task_id": "task-new",
        "run_id": "run-new",
        "status": "queued",
        "created": True,
    }
    assert captured["enqueue_statuses"] == [{"pending", "running"}, {"pending"}]
    assert captured["cancelled"] == []


async def test_publish_adapter_passes_feishu_citation_metadata(monkeypatch):
    calls = []

    class FakeKnowledgeBase:
        async def add_file_record(self, kb_id, object_path, *, params, operator_id):
            metadata = await prepare_item_metadata(object_path, params["content_type"], kb_id, params=params)
            calls.append(("add", kb_id, object_path, params, operator_id, metadata))
            return {"file_id": "file-new", "status": "uploaded"}

        async def parse_file(self, kb_id, file_id, *, operator_id):
            calls.append(("parse", kb_id, file_id, operator_id))
            return {"status": "parsed"}

        async def index_file(self, kb_id, file_id, *, operator_id, params):
            calls.append(("index", kb_id, file_id, operator_id, params))
            return {"status": "indexed", "chunk_count": 3}

    monkeypatch.setattr(router_module, "knowledge_base", FakeKnowledgeBase())
    adapter = router_module.KnowledgePublishAdapter()
    result = await adapter.publish(
        kb_id="kb-1",
        object_path="minio://knowledgebases/feishu/source/version/page.md",
        source_url="https://feishu.example/wiki/node",
        wiki_path="Root / Page",
        version_id="version-new",
        page_info={"item_type": "page", "title": "Page"},
        operator_id="admin",
        content_hash="sha256-value",
    )

    assert result.file_id == "file-new"
    params = calls[0][3]["feishu"]
    assert params == {
        "source_url": "https://feishu.example/wiki/node",
        "wiki_path": "Root / Page",
        "material_version": "version-new",
        "page_info": {"item_type": "page", "title": "Page"},
    }
    assert [call[0] for call in calls] == ["add", "parse", "index"]
    assert calls[0][3]["source_path"] == "Root/Page.md"
    assert calls[0][3]["content_hashes"] == {"minio://knowledgebases/feishu/source/version/page.md": "sha256-value"}
    assert calls[0][5]["filename"] == "Root/Page.md"
    assert calls[0][5]["path"] == "minio://knowledgebases/feishu/source/version/page.md"


async def test_minio_archive_adapter_uses_stable_ids_and_safe_extension(monkeypatch):
    calls = []

    class FakeMinio:
        async def aupload_file(self, bucket_name, object_name, data, content_type=None):
            calls.append((bucket_name, object_name, data, content_type))

    monkeypatch.setattr(router_module, "get_minio_client", lambda: FakeMinio())
    adapter = router_module.MinioFeishuArchiveAdapter()

    object_path = await adapter.archive(
        source_id="source-1",
        item_id="item-1",
        version_id="version-1",
        item_type="attachment",
        title="../../Quarterly Report.PDF",
        content=b"%PDF",
        content_type="application/pdf",
    )

    assert object_path == "minio://knowledgebases/feishu/source-1/item-1/version-1/source.pdf"
    assert calls == [
        (
            "knowledgebases",
            "feishu/source-1/item-1/version-1/source.pdf",
            b"%PDF",
            "application/pdf",
        )
    ]


async def test_minio_archive_adapter_ignores_unsafe_extension(monkeypatch):
    calls = []

    class FakeMinio:
        async def aupload_file(self, bucket_name, object_name, data, content_type=None):
            calls.append(object_name)

    monkeypatch.setattr(router_module, "get_minio_client", lambda: FakeMinio())
    object_path = await router_module.MinioFeishuArchiveAdapter().archive(
        source_id="source-1",
        item_id="item-1",
        version_id="version-1",
        item_type="attachment",
        title="payload.exe",
        content=b"data",
        content_type="application/octet-stream",
    )

    assert object_path.endswith("/source.bin")
    assert calls == ["feishu/source-1/item-1/version-1/source.bin"]


async def test_router_requires_login_and_admin_role():
    app = FastAPI()
    app.include_router(feishu_knowledge, prefix="/api")

    async def fake_db():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = fake_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.get("/api/feishu-knowledge/sources")

    async def standard_user():
        return SimpleNamespace(role="user", department_id=1)

    app.dependency_overrides[get_current_user] = standard_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        forbidden = await client.get("/api/feishu-knowledge/sources")

    assert anonymous.status_code == 401
    assert forbidden.status_code == 403


async def test_create_source_persists_only_credential_environment_name(monkeypatch):
    captured = {}

    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_or_create_source(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(**kwargs)

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    result = await router_module.create_source(
        router_module.SourceCreate(
            name="Docs",
            wiki_root_token="root",
            target_kb_id="kb-1",
            credential_env_name="FEISHU_DOCS_TOKEN",
        ),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(uid="admin"),
    )
    assert result["credential_env_name"] == "FEISHU_DOCS_TOKEN"
    assert "credential" not in captured
    assert captured["created_by"] == "admin"


async def test_create_source_rejects_blank_identifiers_before_repository_call(monkeypatch):
    called = False

    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_or_create_source(self, **kwargs):
            nonlocal called
            called = True
            return SimpleNamespace(**kwargs)

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    async with await _client(user=SimpleNamespace(uid="admin", role="admin")) as client:
        response = await client.post(
            "/api/feishu-knowledge/sources",
            json={
                "name": "   ",
                "wiki_root_token": "root",
                "target_kb_id": "kb-1",
                "credential_env_name": "FEISHU_DOCS_TOKEN",
            },
        )

    assert response.status_code == 422
    assert called is False


async def test_check_source_uses_read_only_feishu_client(monkeypatch):
    calls = []

    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_source(self, source_id):
            return SimpleNamespace(
                source_id=source_id,
                wiki_root_token="root",
                credential_env_name="FEISHU_DOCS_TOKEN",
            )

    class FakeClient:
        def __init__(self, *, credential_env_name):
            calls.append(("init", credential_env_name))

        async def get_node(self, node_token):
            calls.append(("get", node_token))
            return SimpleNamespace(node_token=node_token, title="Root")

        async def aclose(self):
            calls.append(("close",))

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router_module, "FeishuClient", FakeClient)
    result = await router_module.check_source("source-1", db=SimpleNamespace())
    assert result == {"status": "ok", "source_id": "source-1", "root_title": "Root"}
    assert calls == [("init", "FEISHU_DOCS_TOKEN"), ("get", "root"), ("close",)]


async def test_check_source_maps_client_initialization_error_to_422(monkeypatch):
    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_source(self, source_id):
            return SimpleNamespace(
                source_id=source_id,
                wiki_root_token="root",
                credential_env_name="MISSING_FEISHU_TOKEN",
            )

    class FailingClient:
        def __init__(self, *, credential_env_name):
            raise router_module.FeishuClientError(f"Missing credential: {credential_env_name}")

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router_module, "FeishuClient", FailingClient)

    with pytest.raises(HTTPException) as exc_info:
        await router_module.check_source("source-1", db=SimpleNamespace())

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Missing credential: MISSING_FEISHU_TOKEN"


async def test_check_source_maps_read_failure_to_422_and_closes_client(monkeypatch):
    closed = False

    class FakeRepository:
        def __init__(self, _session):
            pass

        async def get_source(self, source_id):
            return SimpleNamespace(
                source_id=source_id,
                wiki_root_token="root",
                credential_env_name="FEISHU_DOCS_TOKEN",
            )

    class FailingClient:
        def __init__(self, *, credential_env_name):
            assert credential_env_name == "FEISHU_DOCS_TOKEN"

        async def get_node(self, node_token):
            assert node_token == "root"
            raise router_module.FeishuClientError("Feishu root is not readable")

        async def aclose(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(router_module, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router_module, "FeishuClient", FailingClient)

    with pytest.raises(HTTPException) as exc_info:
        await router_module.check_source("source-1", db=SimpleNamespace())

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Feishu root is not readable"
    assert closed is True


async def test_approve_endpoint_queues_real_publish_task(monkeypatch):
    calls = []

    class FakeReviewService:
        def __init__(self, _session):
            pass

        async def approve(self, version_id, *, operator_id):
            calls.append(("approve", version_id, operator_id))
            return SimpleNamespace(version_id=version_id, processing_status="publish_queued")

    class FakeDb:
        async def commit(self):
            calls.append(("commit",))

    async def fake_enqueue(**kwargs):
        calls.append(("enqueue", kwargs))
        return SimpleNamespace(id="task-publish"), True

    monkeypatch.setattr(router_module, "FeishuReviewService", FakeReviewService)
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", fake_enqueue)
    result = await router_module.approve_material(
        "version-new",
        db=FakeDb(),
        current_user=SimpleNamespace(uid="admin"),
    )
    assert result == {"version_id": "version-new", "status": "publish_queued", "task_id": "task-publish"}
    assert calls[1] == ("commit",)
    assert calls[2][1]["task_type"] == "feishu_publish"
    assert calls[2][1]["payload"] == {"version_id": "version-new"}
    assert calls[2][1]["payload_match"] == {"version_id": "version-new"}
    assert calls[2][1]["statuses"] == {"pending"}


async def test_batch_reject_requires_reason_even_when_field_is_omitted():
    async with await _client(user=SimpleNamespace(uid="admin", role="admin")) as client:
        response = await client.post(
            "/api/feishu-knowledge/materials/batch-action",
            json={"action": "reject", "version_ids": ["version-1"]},
        )
    assert response.status_code == 422


async def test_query_endpoints_return_sources_runs_materials_and_events(review_fixture):
    run_started_at = datetime(2026, 8, 13, 3, tzinfo=UTC)
    review_fixture.add(
        FeishuSyncRun(
            run_id="run-1",
            source_id="source-1",
            run_type="full",
            status="succeeded",
            started_at=run_started_at,
            operator_id="admin-1",
        )
    )
    review_fixture.add(
        FeishuProcessingEvent(
            source_id="source-1",
            item_id="item-1",
            version_id="version-new",
            event_type="parsed",
        )
    )
    await review_fixture.commit()

    sources = await router_module.list_sources(db=review_fixture)
    runs = await router_module.list_source_runs("source-1", db=review_fixture)
    run = await router_module.get_run("run-1", db=review_fixture)
    materials = await router_module.list_materials("source-1", db=review_fixture)
    material = await router_module.get_material("version-new", db=review_fixture)
    events = await router_module.list_material_events("version-new", db=review_fixture)

    assert sources["items"][0] == {
        "source_id": "source-1",
        "name": "Source",
        "wiki_root_token": "root",
        "wiki_root_url": None,
        "target_kb_id": "kb-1",
        "credential_env_name": "FEISHU_ACCESS_TOKEN",
        "enabled": True,
        "created_at": sources["items"][0]["created_at"],
        "updated_at": sources["items"][0]["updated_at"],
        "last_full_sync_at": "2026-08-13T03:00:00",
        "last_incremental_sync_at": None,
        "total_count": 1,
        "awaiting_review_count": 0,
        "failed_count": 0,
        "source_invalid_count": 0,
    }
    assert runs["items"][0]["run_id"] == run["run_id"] == "run-1"
    assert runs["items"][0]["operator_id"] == run["operator_id"] == "admin-1"
    assert {entry["version_id"] for entry in materials["items"]} == {"version-old", "version-new"}
    assert material["version_id"] == "version-new"
    assert material["source_url"] is None
    assert events["items"][0]["event_type"] == "parsed"


async def test_source_list_returns_null_times_and_zero_counts_for_empty_source(review_fixture):
    review_fixture.add(
        FeishuSource(
            source_id="source-empty",
            name="Empty Source",
            wiki_root_token="empty-root",
            target_kb_id="kb-empty",
            credential_env_name="FEISHU_ACCESS_TOKEN",
        )
    )
    await review_fixture.commit()

    response = await router_module.list_sources(db=review_fixture)
    source = next(item for item in response["items"] if item["source_id"] == "source-empty")

    assert {
        "last_full_sync_at": source["last_full_sync_at"],
        "last_incremental_sync_at": source["last_incremental_sync_at"],
        "total_count": source["total_count"],
        "awaiting_review_count": source["awaiting_review_count"],
        "failed_count": source["failed_count"],
        "source_invalid_count": source["source_invalid_count"],
    } == {
        "last_full_sync_at": None,
        "last_incremental_sync_at": None,
        "total_count": 0,
        "awaiting_review_count": 0,
        "failed_count": 0,
        "source_invalid_count": 0,
    }


async def test_material_list_supports_item_type_filter_and_serializes_api_response(review_fixture):
    attachment = FeishuSourceItem(
        item_id="item-attachment",
        source_id="source-1",
        item_key="attachment:space:file",
        item_type="attachment",
        title="Guide.pdf",
        source_validity="valid",
    )
    attachment_version = FeishuMaterialVersion(
        version_id="version-attachment",
        item_id="item-attachment",
        revision="1",
        content_hash="attachment-hash",
        processing_status="parsed",
        review_status="pending",
    )
    review_fixture.add_all([attachment, attachment_version])
    await review_fixture.commit()

    async with await _database_client(
        review_fixture,
        user=SimpleNamespace(uid="admin", role="admin"),
    ) as client:
        response = await client.get(
            "/api/feishu-knowledge/sources/source-1/materials",
            params={"item_type": "attachment", "processing_status": "parsed"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "version_id": "version-attachment",
                "item_id": "item-attachment",
                "source_id": "source-1",
                "title": "Guide.pdf",
                "item_type": "attachment",
                "source_validity": "valid",
                "active": False,
                "source_url": None,
                "wiki_path": None,
                "target_kb_id": "kb-1",
                "revision": "1",
                "content_hash": "attachment-hash",
                "sync_run_id": None,
                "source_updated_at": None,
                "source_object_path": None,
                "parsed_object_path": None,
                "processing_status": "parsed",
                "processing_params": {},
                "error_code": None,
                "error_message": None,
                "review_status": "pending",
                "reviewer_id": None,
                "reviewed_at": None,
                "review_comment": None,
                "retry_count": 0,
                "yuxi_file_id": None,
                "chunk_count": 0,
                "token_count": 0,
                "published_at": None,
                "replaced_at": None,
                "created_at": attachment_version.created_at.isoformat(),
                "updated_at": attachment_version.updated_at.isoformat(),
            }
        ]
    }


async def test_material_list_filters_directory_source_update_range_and_run(review_fixture):
    run = FeishuSyncRun(run_id="run-filter", source_id="source-1", run_type="incremental", status="succeeded")
    items = [
        FeishuSourceItem(
            item_id="item-in-range",
            source_id="source-1",
            item_key="attachment:in-range",
            item_type="attachment",
            title="In range.pdf",
            path_text="Root / Team / In range.pdf",
            source_updated_at=datetime(2026, 8, 13, 4, tzinfo=UTC),
        ),
        FeishuSourceItem(
            item_id="item-other-directory",
            source_id="source-1",
            item_key="attachment:other-directory",
            item_type="attachment",
            title="Other.pdf",
            path_text="Root / Teamwork / Other.pdf",
            source_updated_at=datetime(2026, 8, 13, 4, tzinfo=UTC),
        ),
        FeishuSourceItem(
            item_id="item-other-run",
            source_id="source-1",
            item_key="attachment:other-run",
            item_type="attachment",
            title="Old.pdf",
            path_text="Root / Team / Old.pdf",
            source_updated_at=datetime(2026, 8, 13, 4, tzinfo=UTC),
        ),
    ]
    versions = [
        FeishuMaterialVersion(
            version_id="version-in-range",
            item_id="item-in-range",
            sync_run_id="run-filter",
            revision="1",
            content_hash="hash-in-range",
        ),
        FeishuMaterialVersion(
            version_id="version-other-directory",
            item_id="item-other-directory",
            sync_run_id="run-filter",
            revision="1",
            content_hash="hash-other-directory",
        ),
        FeishuMaterialVersion(
            version_id="version-other-run",
            item_id="item-other-run",
            sync_run_id=None,
            revision="1",
            content_hash="hash-other-run",
        ),
    ]
    review_fixture.add_all([run, *items, *versions])
    await review_fixture.commit()

    async with await _database_client(
        review_fixture,
        user=SimpleNamespace(uid="admin", role="admin"),
    ) as client:
        response = await client.get(
            "/api/feishu-knowledge/sources/source-1/materials",
            params={
                "directory": "Root / Team",
                "updated_from": "2026-08-13T03:00:00Z",
                "updated_to": "2026-08-13T05:00:00Z",
                "run_id": "run-filter",
            },
        )
        invalid_range = await client.get(
            "/api/feishu-knowledge/sources/source-1/materials",
            params={
                "updated_from": "2026-08-14T00:00:00Z",
                "updated_to": "2026-08-13T00:00:00Z",
            },
        )

    assert response.status_code == 200
    assert [item["version_id"] for item in response.json()["items"]] == ["version-in-range"]
    assert response.json()["items"][0]["sync_run_id"] == "run-filter"
    assert response.json()["items"][0]["source_updated_at"] == "2026-08-13T04:00:00+00:00"
    assert invalid_range.status_code == 422


async def test_material_queries_return_404_for_missing_parent_or_material(review_fixture):
    async with await _database_client(
        review_fixture,
        user=SimpleNamespace(uid="admin", role="admin"),
    ) as client:
        missing_source = await client.get("/api/feishu-knowledge/sources/missing/materials")
        missing_material = await client.get("/api/feishu-knowledge/materials/missing")
        missing_events = await client.get("/api/feishu-knowledge/materials/missing/events")

    assert missing_source.status_code == 404
    assert missing_material.status_code == 404
    assert missing_events.status_code == 404


async def test_reject_endpoint_maps_missing_and_state_conflict(review_fixture):
    async with await _database_client(
        review_fixture,
        user=SimpleNamespace(uid="admin", role="admin"),
    ) as client:
        missing = await client.post(
            "/api/feishu-knowledge/materials/missing/reject",
            json={"reason": "not relevant"},
        )
        first = await client.post(
            "/api/feishu-knowledge/materials/version-new/reject",
            json={"reason": "not relevant"},
        )
        conflict = await client.post(
            "/api/feishu-knowledge/materials/version-new/reject",
            json={"reason": "still not relevant"},
        )

    assert missing.status_code == 404
    assert first.status_code == 200
    assert first.json() == {"version_id": "version-new", "status": "rejected"}
    assert conflict.status_code == 409


async def test_reject_endpoint_persists_reason_and_does_not_publish(review_fixture):
    result = await router_module.reject_material(
        "version-new",
        router_module.RejectRequest(reason="Not approved for production"),
        db=review_fixture,
        current_user=SimpleNamespace(uid="admin"),
    )
    material = await review_fixture.get(FeishuMaterialVersion, 2)
    item = await review_fixture.get(FeishuSourceItem, 1)
    assert result == {"version_id": "version-new", "status": "rejected"}
    assert material.review_comment == "Not approved for production"
    assert item.active_version_id == "version-old"


async def test_retry_endpoint_increments_and_queues_processing_task(monkeypatch):
    calls = []

    class FakeReviewService:
        def __init__(self, _session):
            pass

        async def retry(self, version_id, *, operator_id):
            calls.append(("retry", version_id, operator_id))
            return SimpleNamespace(version_id=version_id, processing_status="processing_queued")

    class FakeDb:
        async def commit(self):
            calls.append(("commit",))

    async def fake_enqueue(**kwargs):
        calls.append(("enqueue", kwargs))
        return SimpleNamespace(id="task-process"), True

    monkeypatch.setattr(router_module, "FeishuReviewService", FakeReviewService)
    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", fake_enqueue)
    result = await router_module.retry_material(
        "version-new",
        db=FakeDb(),
        current_user=SimpleNamespace(uid="admin"),
    )
    assert result == {"version_id": "version-new", "status": "processing_queued", "task_id": "task-process"}
    assert calls[1] == ("commit",)
    assert calls[2][1]["task_type"] == "feishu_process"
    assert calls[2][1]["payload_match"] == {"version_id": "version-new"}
    assert calls[2][1]["statuses"] == {"pending"}


async def test_retry_endpoint_rejects_removal_failed_without_enqueue(monkeypatch, review_fixture):
    enqueue_calls = []
    version = await review_fixture.get(FeishuMaterialVersion, 1)
    version.processing_status = "removal_failed"
    version.error_message = "delete failed"
    await review_fixture.commit()

    async def record_enqueue(*args, **kwargs):
        enqueue_calls.append((args, kwargs))
        return SimpleNamespace(id="unexpected-task")

    monkeypatch.setattr(router_module, "_enqueue_processing", record_enqueue)
    monkeypatch.setattr(router_module, "_enqueue_publish", record_enqueue)

    async with await _database_client(
        review_fixture,
        user=SimpleNamespace(uid="admin", role="admin"),
    ) as client:
        response = await client.post("/api/feishu-knowledge/materials/version-old/retry")

    await review_fixture.refresh(version)
    assert response.status_code == 409
    assert version.processing_status == "removal_failed"
    assert version.retry_count == 0
    assert version.error_message == "delete failed"
    assert enqueue_calls == []


@pytest.mark.parametrize(
    ("initial_status", "expected_task_type"),
    [
        ("parse_failed", "feishu_process"),
        ("publish_failed", "feishu_publish"),
    ],
)
async def test_retry_does_not_reuse_running_task_that_is_finishing(
    monkeypatch,
    review_fixture,
    initial_status,
    expected_task_type,
):
    version = await review_fixture.get(FeishuMaterialVersion, 2)
    version.processing_status = initial_status
    await review_fixture.commit()
    enqueued = []

    async def fake_enqueue_unique_by_payload(**kwargs):
        if "running" in kwargs["statuses"]:
            return SimpleNamespace(id="task-old", status="running"), False
        enqueued.append(kwargs)
        return SimpleNamespace(id="task-new", status="pending"), True

    monkeypatch.setattr(router_module.tasker, "enqueue_unique_by_payload", fake_enqueue_unique_by_payload)

    result = await router_module.retry_material(
        "version-new",
        db=review_fixture,
        current_user=SimpleNamespace(uid="admin"),
    )

    assert result["task_id"] == "task-new"
    assert enqueued[0]["task_type"] == expected_task_type
    assert enqueued[0]["statuses"] == {"pending"}


@pytest.mark.parametrize(
    ("action", "initial_status", "expected_status"),
    [
        ("approve", "parsed", "publish_failed"),
        ("retry", "parse_failed", "parse_failed"),
    ],
)
async def test_action_enqueue_failure_is_committed_as_retryable_failure(
    monkeypatch,
    review_fixture,
    action,
    initial_status,
    expected_status,
):
    version = await review_fixture.get(FeishuMaterialVersion, 2)
    version.processing_status = initial_status
    await review_fixture.commit()

    async def fail_enqueue(*args, **kwargs):
        raise RuntimeError("task queue unavailable")

    monkeypatch.setattr(
        router_module,
        "_enqueue_publish" if action == "approve" else "_enqueue_processing",
        fail_enqueue,
    )

    with pytest.raises(HTTPException) as exc_info:
        if action == "approve":
            await router_module.approve_material(
                "version-new",
                db=review_fixture,
                current_user=SimpleNamespace(uid="admin"),
            )
        else:
            await router_module.retry_material(
                "version-new",
                db=review_fixture,
                current_user=SimpleNamespace(uid="admin"),
            )

    await review_fixture.refresh(version)
    assert exc_info.value.status_code == 500
    assert version.processing_status == expected_status
    assert version.error_message == "task queue unavailable"


async def test_processing_worker_passes_archived_source_and_content_hash(monkeypatch, review_fixture):
    calls = []

    class FakeKnowledgeBase:
        async def add_file_record(self, kb_id, object_path, *, params, operator_id):
            metadata = await prepare_item_metadata(object_path, params["content_type"], kb_id, params=params)
            calls.append(("add", kb_id, object_path, params, operator_id, metadata))
            return {"file_id": "file-new"}

        async def parse_file(self, kb_id, file_id, *, operator_id):
            calls.append(("parse", kb_id, file_id, operator_id))
            return {"status": "parsed"}

    @asynccontextmanager
    async def fake_session_context():
        yield review_fixture

    current = await review_fixture.get(FeishuMaterialVersion, 2)
    current.processing_status = "processing_queued"
    current.source_object_path = "minio://knowledgebases/feishu/source-1/item-1/version-new/source.pdf"
    item = await review_fixture.get(FeishuSourceItem, 1)
    item.item_type = "attachment"
    item.title = "Quarterly Report.PDF"
    item.path_text = "Root / Finance / Quarterly Report.PDF"
    item.source_url = "https://feishu.example/wiki/finance"
    await review_fixture.commit()
    monkeypatch.setattr(router_module, "knowledge_base", FakeKnowledgeBase())
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)

    result = await router_module._run_processing_worker("version-new", operator_id="admin")

    assert result == {"version_id": "version-new", "status": "awaiting_review", "file_id": "file-new"}
    params = calls[0][3]
    assert params["source_path"] == "Root/Finance/Quarterly Report.PDF"
    assert params["content_hashes"] == {current.source_object_path: "new-hash"}
    assert params["feishu"] == {
        "source_url": "https://feishu.example/wiki/finance",
        "wiki_path": "Root / Finance / Quarterly Report.PDF",
        "material_version": "version-new",
        "page_info": {"item_type": "attachment", "title": "Quarterly Report.PDF"},
    }
    assert calls[0][5]["filename"] == "Root/Finance/Quarterly Report.PDF"
    assert calls[0][5]["path"] == current.source_object_path


async def test_processing_retry_reuses_file_record_created_before_parse_failure(monkeypatch, review_fixture):
    calls = []

    class FakeKnowledgeBase:
        async def add_file_record(self, kb_id, object_path, *, params, operator_id):
            calls.append(("add", kb_id, object_path, operator_id))
            return {"file_id": "file-one"}

        async def parse_file(self, kb_id, file_id, *, operator_id):
            calls.append(("parse", kb_id, file_id, operator_id))
            if len([call for call in calls if call[0] == "parse"]) == 1:
                raise RuntimeError("parser unavailable")
            return {"status": "parsed"}

    @asynccontextmanager
    async def fake_session_context():
        yield review_fixture

    current = await review_fixture.get(FeishuMaterialVersion, 2)
    current.processing_status = "processing_queued"
    current.source_object_path = "minio://knowledgebases/feishu/source-1/item-1/version-new/source.md"
    await review_fixture.commit()
    monkeypatch.setattr(router_module, "knowledge_base", FakeKnowledgeBase())
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)

    with pytest.raises(RuntimeError, match="parser unavailable"):
        await router_module._run_processing_worker("version-new", operator_id="admin")

    await review_fixture.refresh(current)
    assert current.processing_status == "parse_failed"
    assert current.yuxi_file_id == "file-one"

    await FeishuReviewService(review_fixture).retry("version-new", operator_id="admin")
    result = await router_module._run_processing_worker("version-new", operator_id="admin")

    await review_fixture.refresh(current)
    events = list(
        (
            await review_fixture.execute(
                select(FeishuProcessingEvent)
                .where(FeishuProcessingEvent.version_id == "version-new")
                .order_by(FeishuProcessingEvent.id)
            )
        ).scalars()
    )
    assert result == {"version_id": "version-new", "status": "awaiting_review", "file_id": "file-one"}
    assert current.processing_status == "awaiting_review"
    assert current.yuxi_file_id == "file-one"
    assert [call[0] for call in calls] == ["add", "parse", "parse"]
    assert [call[2] for call in calls if call[0] == "parse"] == ["file-one", "file-one"]
    assert [(event.event_type, event.from_status, event.to_status) for event in events] == [
        ("parse_failed", "processing", "parse_failed"),
        ("retry_queued", "parse_failed", "processing_queued"),
        ("parsed", "processing", "awaiting_review"),
    ]


async def test_publish_worker_archives_then_publishes_and_switches_active(monkeypatch, review_fixture):
    calls = []

    class FakePublishAdapter:
        async def publish(self, **kwargs):
            calls.append(("publish", kwargs))
            return router_module.PublishResult(file_id="file-new", chunk_count=4)

    @asynccontextmanager
    async def fake_session_context():
        yield review_fixture

    current = await review_fixture.get(FeishuMaterialVersion, 2)
    current.source_object_path = "minio://knowledgebases/feishu/source-1/item-1/version-new/source.md"
    await review_fixture.commit()
    await FeishuReviewService(review_fixture).approve("version-new", operator_id="admin")
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)
    result = await router_module._run_publish_worker(
        "version-new",
        operator_id="admin",
        publish_adapter=FakePublishAdapter(),
    )

    item = await review_fixture.get(FeishuSourceItem, 1)
    version = await review_fixture.get(FeishuMaterialVersion, 2)
    assert result == {"version_id": "version-new", "status": "published", "file_id": "file-new"}
    assert [call[0] for call in calls] == ["publish"]
    assert calls[0][1]["source_url"] is None
    assert calls[0][1]["wiki_path"] is None
    assert calls[0][1]["page_info"] == {"item_type": "page", "title": "Page"}
    assert version.source_object_path.endswith("/source.md")
    assert version.chunk_count == 4
    assert item.active_version_id == "version-new"


async def test_publish_worker_commits_active_switch_before_deleting_replaced_file(monkeypatch, review_fixture):
    deletions = []

    class FakePublishAdapter:
        async def publish(self, **kwargs):
            return router_module.PublishResult(file_id="file-new", chunk_count=4)

    class FakeKnowledgeBase:
        async def delete_file(self, kb_id, file_id):
            deletions.append((kb_id, file_id, review_fixture.in_transaction()))

    @asynccontextmanager
    async def fake_session_context():
        yield review_fixture

    current = await review_fixture.get(FeishuMaterialVersion, 2)
    current.source_object_path = "minio://knowledgebases/feishu/source-1/item-1/version-new/source.md"
    await review_fixture.commit()
    await FeishuReviewService(review_fixture).approve("version-new", operator_id="admin")
    monkeypatch.setattr(router_module, "knowledge_base", FakeKnowledgeBase())
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)

    await router_module._run_publish_worker(
        "version-new",
        operator_id="admin",
        publish_adapter=FakePublishAdapter(),
    )

    assert deletions == [("kb-1", "file-old", False)]


async def test_publish_worker_does_not_delete_when_replacement_reuses_file_id(monkeypatch, review_fixture):
    class FakePublishAdapter:
        async def publish(self, **kwargs):
            return router_module.PublishResult(file_id="file-old")

    class FakeKnowledgeBase:
        async def delete_file(self, kb_id, file_id):
            raise AssertionError("shared knowledge file must not be deleted")

    @asynccontextmanager
    async def fake_session_context():
        yield review_fixture

    current = await review_fixture.get(FeishuMaterialVersion, 2)
    current.source_object_path = "minio://knowledgebases/feishu/source-1/item-1/version-new/source.md"
    await review_fixture.commit()
    await FeishuReviewService(review_fixture).approve("version-new", operator_id="admin")
    monkeypatch.setattr(router_module, "knowledge_base", FakeKnowledgeBase())
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)

    result = await router_module._run_publish_worker(
        "version-new",
        operator_id="admin",
        publish_adapter=FakePublishAdapter(),
    )

    assert result["status"] == "published"


async def test_replacement_cleanup_failure_keeps_new_active_and_records_event(monkeypatch, review_fixture):
    class FakePublishAdapter:
        async def publish(self, **kwargs):
            return router_module.PublishResult(file_id="file-new")

    class FailingKnowledgeBase:
        async def delete_file(self, kb_id, file_id):
            raise RuntimeError("vector cleanup failed")

    @asynccontextmanager
    async def fake_session_context():
        yield review_fixture

    current = await review_fixture.get(FeishuMaterialVersion, 2)
    current.source_object_path = "minio://knowledgebases/feishu/source-1/item-1/version-new/source.md"
    await review_fixture.commit()
    await FeishuReviewService(review_fixture).approve("version-new", operator_id="admin")
    monkeypatch.setattr(router_module, "knowledge_base", FailingKnowledgeBase())
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)

    result = await router_module._run_publish_worker(
        "version-new",
        operator_id="admin",
        publish_adapter=FakePublishAdapter(),
    )

    item = await review_fixture.get(FeishuSourceItem, 1)
    version = await review_fixture.get(FeishuMaterialVersion, 2)
    events = list((await review_fixture.execute(FeishuProcessingEvent.__table__.select())).all())
    assert result["status"] == "published"
    assert item.active_version_id == "version-new"
    assert version.processing_status == "published"
    assert version.error_message == "vector cleanup failed"
    assert events[-1].event_type == "replacement_cleanup_failed"


async def test_publish_worker_failure_keeps_old_active_and_records_failure(monkeypatch, review_fixture):
    class FailingPublishAdapter:
        async def publish(self, **kwargs):
            raise RuntimeError("index failed")

    @asynccontextmanager
    async def fake_session_context():
        yield review_fixture

    current = await review_fixture.get(FeishuMaterialVersion, 2)
    current.source_object_path = "minio://knowledgebases/feishu/source-1/item-1/version-new/source.md"
    await review_fixture.commit()
    await FeishuReviewService(review_fixture).approve("version-new", operator_id="admin")
    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)
    with pytest.raises(RuntimeError, match="index failed"):
        await router_module._run_publish_worker(
            "version-new",
            operator_id="admin",
            publish_adapter=FailingPublishAdapter(),
        )

    item = await review_fixture.get(FeishuSourceItem, 1)
    version = await review_fixture.get(FeishuMaterialVersion, 2)
    assert item.active_version_id == "version-old"
    assert version.processing_status == "publish_failed"
    assert version.error_message == "index failed"


async def test_publish_worker_missing_archive_fails_and_keeps_old_active(monkeypatch, review_fixture):
    class UnexpectedPublishAdapter:
        async def publish(self, **kwargs):
            raise AssertionError("publish must not run without an archive")

    @asynccontextmanager
    async def fake_session_context():
        yield review_fixture

    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", fake_session_context)
    await FeishuReviewService(review_fixture).approve("version-new", operator_id="admin")
    with pytest.raises(RuntimeError, match="archived source object"):
        await router_module._run_publish_worker(
            "version-new",
            operator_id="admin",
            publish_adapter=UnexpectedPublishAdapter(),
        )

    item = await review_fixture.get(FeishuSourceItem, 1)
    version = await review_fixture.get(FeishuMaterialVersion, 2)
    assert item.active_version_id == "version-old"
    assert version.processing_status == "publish_failed"


async def test_removal_adapter_failure_keeps_active_version(review_fixture):
    class FailingRemovalAdapter:
        async def remove(self, *, kb_id: str, file_id: str) -> None:
            raise RuntimeError("Milvus deletion failed")

    item = await review_fixture.get(FeishuSourceItem, 1)
    item.source_validity = "invalid"
    await review_fixture.commit()
    service = FeishuReviewService(review_fixture, removal_adapter=FailingRemovalAdapter())
    with pytest.raises(RuntimeError, match="Milvus deletion failed"):
        await service.confirm_removal("version-old", operator_id="admin")
    item = await review_fixture.get(FeishuSourceItem, 1)
    assert item.active_version_id == "version-old"
    old = await review_fixture.get(FeishuMaterialVersion, 1)
    assert old.processing_status == "removal_failed"
    assert old.yuxi_file_id == "file-old"
    events = list(
        (
            await review_fixture.execute(
                select(FeishuProcessingEvent).where(FeishuProcessingEvent.version_id == "version-old")
            )
        ).scalars()
    )
    assert [(event.event_type, event.from_status, event.to_status) for event in events] == [
        ("removal_started", "published", "removal_pending"),
        ("removal_failed", "removal_pending", "removal_failed"),
    ]


async def test_restart_reconciles_removal_for_idempotent_admin_retry(review_fixture):
    calls = []

    class AlreadyRemovedAdapter:
        async def remove(self, *, kb_id: str, file_id: str) -> None:
            calls.append((kb_id, file_id))
            raise FileNotFoundError(file_id)

    item = await review_fixture.get(FeishuSourceItem, 1)
    old = await review_fixture.get(FeishuMaterialVersion, 1)
    item.source_validity = "invalid"
    old.processing_status = "removal_pending"
    await review_fixture.commit()

    reconciled = await FeishuKnowledgeRepository(review_fixture).reconcile_interrupted_work()
    removed = await FeishuReviewService(
        review_fixture,
        removal_adapter=AlreadyRemovedAdapter(),
    ).confirm_removal("version-old", operator_id="admin")

    assert reconciled == {"sync_runs": 0, "material_versions": 1}
    assert calls == [("kb-1", "file-old")]
    assert removed.processing_status == "removed"
    assert removed.yuxi_file_id is None
    assert item.active_version_id is None
    events = list(
        (
            await review_fixture.execute(
                select(FeishuProcessingEvent)
                .where(FeishuProcessingEvent.version_id == "version-old")
                .order_by(FeishuProcessingEvent.id)
            )
        ).scalars()
    )
    assert [(event.event_type, event.from_status, event.to_status) for event in events] == [
        ("startup_reconciled", "removal_pending", "removal_failed"),
        ("removal_started", "removal_failed", "removal_pending"),
        ("removal_confirmed", "removal_pending", "removed"),
    ]
    assert events[-1].payload_json == {"external_file_already_missing": True}


async def test_confirm_removal_calls_external_adapter_outside_transaction(review_fixture):
    calls = []

    class RemovalAdapter:
        async def remove(self, *, kb_id: str, file_id: str) -> None:
            calls.append((kb_id, file_id, review_fixture.in_transaction()))

    item = await review_fixture.get(FeishuSourceItem, 1)
    item.source_validity = "invalid"
    await review_fixture.commit()

    service = FeishuReviewService(review_fixture, removal_adapter=RemovalAdapter())
    removed = await service.confirm_removal("version-old", operator_id="admin")

    assert calls == [("kb-1", "file-old", False)]
    assert removed.processing_status == "removed"
    assert item.active_version_id is None


async def test_confirm_removal_commits_claim_before_external_delete_when_auth_opened_transaction(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'removal-claim.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    class RequestSession(AsyncSession):
        fail_finalization_commit = False

        async def commit(self):
            if self.fail_finalization_commit:
                raise RuntimeError("forced request finalization commit failure")
            await super().commit()

    session_factory = async_sessionmaker(
        engine,
        class_=RequestSession,
        expire_on_commit=False,
    )

    async with session_factory() as seed_session:
        seed_session.add_all(
            [
                User(
                    id=1,
                    username="Admin",
                    uid="admin",
                    password_hash="unused",
                    role="admin",
                ),
                FeishuSource(
                    source_id="source-1",
                    name="Source",
                    wiki_root_token="root",
                    target_kb_id="kb-1",
                    credential_env_name="FEISHU_ACCESS_TOKEN",
                ),
                FeishuSourceItem(
                    item_id="item-1",
                    source_id="source-1",
                    item_key="page:space:node",
                    item_type="page",
                    title="Page",
                    source_validity="invalid",
                    active_version_id="version-old",
                ),
                FeishuMaterialVersion(
                    version_id="version-old",
                    item_id="item-1",
                    revision="1",
                    content_hash="old-hash",
                    processing_status="published",
                    review_status="approved",
                    yuxi_file_id="file-old",
                ),
            ]
        )
        await seed_session.commit()

    adapter_transaction_states = []

    @asynccontextmanager
    async def failing_request_context():
        async with session_factory() as request_session:
            try:
                yield request_session
                request_session.fail_finalization_commit = True
                await request_session.commit()
            except Exception:
                await request_session.rollback()
                raise

    with pytest.raises(RuntimeError, match="forced request finalization commit failure"):
        async with failing_request_context() as request_session:
            await request_session.scalar(select(User).where(User.id == 1))

            class RemovalAdapter:
                async def remove(self, *, kb_id: str, file_id: str) -> None:
                    adapter_transaction_states.append(request_session.in_transaction())

            await FeishuReviewService(
                request_session,
                removal_adapter=RemovalAdapter(),
            ).confirm_removal("version-old", operator_id="admin")

    async with session_factory() as verification_session:
        item = await verification_session.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == "item-1"))
        version = await verification_session.scalar(
            select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-old")
        )

    assert adapter_transaction_states == [False]
    assert version.processing_status == "removed"
    assert version.yuxi_file_id is None
    assert item.active_version_id is None
    await engine.dispose()


async def test_confirm_removal_claim_commit_failure_rolls_back_without_external_delete(monkeypatch, review_fixture):
    calls = []
    rollback_calls = 0

    class UnexpectedRemovalAdapter:
        async def remove(self, *, kb_id: str, file_id: str) -> None:
            calls.append((kb_id, file_id))

    item = await review_fixture.get(FeishuSourceItem, 1)
    item.source_validity = "invalid"
    await review_fixture.commit()
    await review_fixture.scalar(select(User).limit(1))
    original_rollback = review_fixture.rollback

    async def fail_commit():
        raise RuntimeError("forced claim commit failure")

    async def track_rollback():
        nonlocal rollback_calls
        rollback_calls += 1
        await original_rollback()

    monkeypatch.setattr(review_fixture, "commit", fail_commit)
    monkeypatch.setattr(review_fixture, "rollback", track_rollback)

    with pytest.raises(RuntimeError, match="forced claim commit failure"):
        await FeishuReviewService(
            review_fixture,
            removal_adapter=UnexpectedRemovalAdapter(),
        ).confirm_removal("version-old", operator_id="admin")

    assert calls == []
    assert rollback_calls == 1


async def test_removal_claim_rejects_second_worker_after_first_claim_is_committed(review_fixture):
    item = await review_fixture.get(FeishuSourceItem, 1)
    item.source_validity = "invalid"
    await review_fixture.commit()
    service = FeishuReviewService(review_fixture)

    await service._claim_removal("version-old", operator_id="admin-1")
    await review_fixture.commit()

    with pytest.raises(ValueError, match="active published version"):
        await service._claim_removal("version-old", operator_id="admin-2")

    status = await review_fixture.scalar(
        select(FeishuMaterialVersion.processing_status).where(FeishuMaterialVersion.version_id == "version-old")
    )
    assert status == "removal_pending"
    events = list(
        (
            await review_fixture.execute(
                select(FeishuProcessingEvent).where(
                    FeishuProcessingEvent.version_id == "version-old",
                    FeishuProcessingEvent.event_type == "removal_started",
                )
            )
        ).scalars()
    )
    assert len(events) == 1


async def test_batch_action_reports_each_version_and_supports_reject(monkeypatch):
    calls = []

    async def fake_apply(version_id, action, *, db, operator_id, reason):
        calls.append((version_id, action, operator_id, reason))
        return {"version_id": version_id, "status": "rejected"}

    monkeypatch.setattr(router_module, "_apply_action", fake_apply)
    result = await router_module.batch_action(
        router_module.BatchActionRequest(
            action="reject",
            version_ids=["version-1", "version-2"],
            reason="duplicate",
        ),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(uid="admin"),
    )
    assert result["succeeded"] == 2
    assert result["failed"] == 0
    assert calls == [
        ("version-1", "reject", "admin", "duplicate"),
        ("version-2", "reject", "admin", "duplicate"),
    ]


async def test_batch_action_returns_partial_results(monkeypatch):
    async def fake_apply(version_id, action, *, db, operator_id, reason):
        if version_id == "missing":
            raise HTTPException(status_code=404, detail="material not found")
        if version_id == "conflict":
            raise HTTPException(status_code=409, detail="invalid state")
        return {"version_id": version_id, "status": "publish_queued", "task_id": "task-1"}

    monkeypatch.setattr(router_module, "_apply_action", fake_apply)
    result = await router_module.batch_action(
        router_module.BatchActionRequest(
            action="approve",
            version_ids=["ok", "missing", "conflict"],
        ),
        db=SimpleNamespace(),
        current_user=SimpleNamespace(uid="admin"),
    )

    assert result["succeeded"] == 1
    assert result["failed"] == 2
    assert result["items"] == [
        {"version_id": "ok", "status": "publish_queued", "task_id": "task-1", "ok": True},
        {"version_id": "missing", "ok": False, "status_code": 404, "error": "material not found"},
        {"version_id": "conflict", "ok": False, "status_code": 409, "error": "invalid state"},
    ]
