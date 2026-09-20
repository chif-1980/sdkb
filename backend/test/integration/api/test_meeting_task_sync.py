"""Real PostgreSQL projection/HTTP checks in a disposable schema; Feishu is read-only fake."""

import asyncio
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.meeting_management_router import meeting_management
from server.routers.product_meeting_router import product_meeting
from server.utils.auth_middleware import get_admin_user, get_product_user
from yuxi.integrations.feishu.client import FeishuClientError
from yuxi.product_chat import meeting_task_sync as sync
from yuxi.product_chat.meeting_repository import MeetingRepository
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Department, User, Base
from yuxi.storage.postgres.models_product import (
    FeishuUserBinding,
    ProductConversation,
    ProductMessage,
    MeetingRecord,
    MeetingRevision,
    ManagedMeeting,
    ManagedMeetingItem,
    ManagedMeetingRun,
    ManagedMeetingEvent,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def workspace(monkeypatch):
    # Every row and table is isolated from the running application's database schema.
    schema = "test_meeting_sync_" + uuid4().hex
    engine = create_async_engine(os.environ["POSTGRES_URL"], execution_options={"schema_translate_map": {None: schema}})
    classes = [
        Department,
        User,
        FeishuUserBinding,
        ProductConversation,
        ProductMessage,
        MeetingRecord,
        MeetingRevision,
        ManagedMeeting,
        ManagedMeetingItem,
        ManagedMeetingRun,
        ManagedMeetingEvent,
    ]
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[cls.__table__ for cls in classes]))
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def context():
        async with sessions.begin() as db:
            yield db

    monkeypatch.setattr(pg_manager, "get_async_session_context", context)
    client = SimpleNamespace(
        get_employee=AsyncMock(return_value={"open_id": "open-test"}),
        get_task=AsyncMock(return_value={"completed_at": "0"}),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(sync, "FeishuClient", lambda: client)
    try:
        async with context() as db:
            dept = Department(name="测试部门")
            db.add(dept)
            await db.flush()
            user = User(
                username="同步测试", uid="sync-test", role="admin", password_hash="unused", department_id=dept.id
            )
            db.add(user)
            await db.flush()
            db.add(
                FeishuUserBinding(
                    user_id=user.id,
                    feishu_user_id="user-test",
                    feishu_open_id="open-test",
                    tenant_key="test-tenant",
                    display_name="同步测试",
                    authorization_status="ACTIVE",
                )
            )

        async def create(tasks=None, archived=False):
            mid = "MT-" + uuid4().hex
            async with context() as db:
                conversation = ProductConversation(owner_user_id=user.id, title="测试会议", status="ACTIVE")
                db.add(conversation)
                await db.flush()
                question = ProductMessage(conversation_id=conversation.conversation_id, role="USER", content="测试")
                answer = ProductMessage(
                    conversation_id=conversation.conversation_id,
                    role="ASSISTANT",
                    content="测试",
                    answer_status="SUPPORTED",
                )
                db.add_all([question, answer])
                await db.flush()
                record = MeetingRecord(
                    id=mid,
                    conversation_id=conversation.conversation_id,
                    message_id=answer.message_id,
                    user_message_id=question.message_id,
                    request_id=mid,
                    state="completed",
                    version=0,
                    input={},
                    sources=[],
                )
                db.add(record)
                await db.flush()
                await MeetingRepository(db).save_result(
                    record,
                    {
                        "title": "测试会议",
                        "body": "保留原文",
                        "followup": {
                            "tasks": deepcopy(tasks if tasks is not None else [task()]),
                            "knowledgeSuggestions": [],
                        },
                    },
                )
                managed = await db.get(ManagedMeeting, mid)
                managed.archived = int(archived)
            return mid

        app = FastAPI()
        app.include_router(meeting_management, prefix="/api")
        app.include_router(product_meeting, prefix="/api")
        app.dependency_overrides[get_admin_user] = lambda: user
        app.dependency_overrides[get_product_user] = lambda: user
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield SimpleNamespace(db=context, client=client, create=create, http=http)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await engine.dispose()


def task(id="task-1", **extra):
    return {
        "id": id,
        "title": "待办",
        "status": "IN_PROGRESS",
        "reviewStatus": "CONFIRMED",
        "dueDate": "2000-01-01",
        "assignee": {"displayName": "实际负责人"},
        "delivery": {"feishuTaskId": id, "syncStatus": "SYNCED"},
        **extra,
    }


async def snapshot(w, mid):
    assistant = (await w.http.get(f"/api/chat/meetings/{mid}")).json()["meeting"]
    admin = (await w.http.get(f"/api/meeting-management/{mid}")).json()
    assert {t["id"]: t for t in assistant["result"]["followup"]["tasks"]} == {
        t["id"]: t for t in admin["current"]["result"]["followup"]["tasks"]
    }
    return assistant, admin


async def test_completion_reopen_repeated_poll_and_metrics(workspace):
    w = workspace
    mid = await w.create()
    assert (await w.http.get("/api/meeting-management/metrics")).json()["overdue"] == 1
    w.client.get_task.return_value = {"completed_at": "1750000000000"}
    await sync.sync_task_batch()
    first, admin = await snapshot(w, mid)
    assert first["result"]["followup"]["tasks"][0]["status"] == "DONE"
    assert admin["meeting"]["completedTasks"] == 1
    assert (await w.http.get("/api/meeting-management/metrics")).json()["overdue"] == 0
    await sync.sync_task_batch()
    repeated, _ = await snapshot(w, mid)
    assert repeated["version"] == first["version"]
    assert (
        repeated["result"]["followup"]["tasks"][0]["delivery"]["lastSyncedAt"]
        >= first["result"]["followup"]["tasks"][0]["delivery"]["lastSyncedAt"]
    )
    w.client.get_task.return_value = {"completed_at": "0"}
    await sync.sync_task_batch()
    reopened, admin = await snapshot(w, mid)
    assert reopened["result"]["followup"]["tasks"][0]["status"] == "OPEN"
    assert admin["meeting"]["completedTasks"] == 0
    assert (await w.http.get("/api/meeting-management/metrics")).json()["overdue"] == 1


@pytest.mark.parametrize("failure", ["identity", "unbound", "timeout", "remote", "invalid"])
async def test_failure_visible_preserves_state_and_recovers(workspace, failure, monkeypatch):
    w = workspace
    mid = await w.create()
    await sync.sync_task_batch()
    before, _ = await snapshot(w, mid)
    if failure == "identity":
        w.client.get_employee.return_value = {"open_id": "other-tenant"}
    elif failure == "unbound":
        async with w.db() as db:
            binding = await db.scalar(select(FeishuUserBinding))
            binding.authorization_status = "REVOKED"
    elif failure == "timeout":
        timeout = asyncio.timeout
        monkeypatch.setattr(sync.asyncio, "timeout", lambda _: timeout(0.05))

        async def slow_employee(*args):
            await asyncio.sleep(1)

        w.client.get_employee.side_effect = slow_employee
    elif failure == "remote":
        w.client.get_task.side_effect = FeishuClientError("无读取权限")
    else:
        w.client.get_task.return_value = {"completed_at": None}
    await sync.sync_task_batch()
    failed, _ = await snapshot(w, mid)
    t = failed["result"]["followup"]["tasks"][0]
    assert t["status"] == "IN_PROGRESS"
    assert t["delivery"]["syncStatus"] == "FAILED" and t["delivery"]["syncError"]
    assert t["delivery"]["lastSyncedAt"] == before["result"]["followup"]["tasks"][0]["delivery"]["lastSyncedAt"]
    assert (await w.http.get("/api/meeting-management/queue/TASK", params={"status": "SYNC_FAILED"})).json()[
        "total"
    ] == 1
    await sync.sync_task_batch()
    assert (await snapshot(w, mid))[0]["version"] == failed["version"]
    w.client.get_employee.side_effect = None
    w.client.get_employee.return_value = {"open_id": "open-test"}
    w.client.get_task.side_effect = None
    w.client.get_task.return_value = {"completed_at": "0"}
    if failure == "unbound":
        async with w.db() as db:
            binding = await db.scalar(select(FeishuUserBinding))
            binding.authorization_status = "ACTIVE"
    response = await w.http.post(f"/api/meeting-management/{mid}/sync-tasks")
    assert response.status_code == 200
    assert response.json() == {"checked": 1, "failed": 0}
    recovered, _ = await snapshot(w, mid)
    assert recovered["result"]["followup"]["tasks"][0]["delivery"]["syncStatus"] == "SYNCED"
    assert recovered["result"]["followup"]["tasks"][0]["delivery"]["syncError"] is None


async def test_skips_drafts_archives_and_concurrent_replica(workspace):
    w = workspace
    draft = task("draft", delivery={"feishuTaskId": "draft", "pendingUpdate": True}, status="IN_PROGRESS")
    mid = await w.create([task("ready"), draft, task("unsent", delivery={})])
    await w.create(archived=True)
    async with w.db() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(7130291)"))
        await sync.sync_task_batch()
        w.client.get_task.assert_not_awaited()
    w.client.get_task.return_value = {"completed_at": "123"}
    await sync.sync_task_batch()
    w.client.get_task.assert_awaited_once_with("ready")
    saved, _ = await snapshot(w, mid)
    assert saved["result"]["followup"]["tasks"][1] == {
        **draft,
        **{
            k: v
            for k, v in saved["result"]["followup"]["tasks"][1].items()
            if k in ["extractionKey", "sourceMeetingId"]
        },
    }
    assert saved["result"]["followup"]["tasks"][2]["delivery"] == {}


async def test_batch_limit_and_failure_do_not_starve_other_meetings(workspace):
    w = workspace
    for n in range(21):
        await w.create([task(f"task-{n}")])

    async def response(task_id):
        if task_id == "task-0":
            raise FeishuClientError("测试失败")
        return {"completed_at": "0"}

    w.client.get_task.side_effect = response
    await sync.sync_task_batch()
    assert w.client.get_task.await_count == 20
    assert "task-20" not in {call.args[0] for call in w.client.get_task.await_args_list}
    await sync.sync_task_batch()
    assert "task-20" in {call.args[0] for call in w.client.get_task.await_args_list}
