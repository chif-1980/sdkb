"""Real PostgreSQL projection/HTTP checks in a disposable schema; Feishu is read-only fake."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, MockTransport, ReadTimeout, Response
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
    MeetingTaskCreation,
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
        MeetingTaskCreation,
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
            yield SimpleNamespace(db=context, client=client, create=create, http=http, app=app, user=user)
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


async def test_timeout_keeps_tasks_already_read_successfully(workspace, monkeypatch):
    w = workspace
    mid = await w.create([task("fast"), task("slow")])
    timeout = asyncio.timeout
    monkeypatch.setattr(sync.asyncio, "timeout", lambda _: timeout(0.1))

    async def response(task_id):
        if task_id == "slow":
            await asyncio.sleep(1)
        return {"completed_at": "1750000000000"}

    w.client.get_task.side_effect = response
    await sync.sync_task_batch()
    saved, _ = await snapshot(w, mid)
    fast, slow = saved["result"]["followup"]["tasks"]
    assert fast["status"] == "DONE"
    assert fast["delivery"]["syncStatus"] == "SYNCED"
    assert slow["status"] == "IN_PROGRESS"
    assert slow["delivery"]["syncStatus"] == "FAILED"


@pytest.fixture
async def dispatch_workspace(workspace, monkeypatch):
    from server.routers import product_meeting_router as router

    w = workspace
    w.client.create_task = AsyncMock(return_value={"data": {"task": {"guid": "remote-task"}}})
    w.client.application_id = "test-app"
    w.client.edit_task = AsyncMock()
    w.client.update_task = AsyncMock()
    w.client.send_text_message = AsyncMock()
    factory = Mock(return_value=w.client)
    factory.update_task = w.client.update_task
    monkeypatch.setattr(router, "FeishuClient", factory)
    monkeypatch.setattr(router, "load_meeting_directory", AsyncMock(return_value={"users": [
        {"userId": None, "feishuUserId": "member", "displayName": "实际负责人"},
    ]}))
    w.mid = await w.create([task(
        delivery={}, reviewStatus="PENDING", status="OPEN", dueDate="2026-09-21",
        assignee={"userId": None, "feishuUserId": "member", "displayName": "实际负责人"},
    )])
    w.patch = {
        "version": 1, "action": "CONFIRM", "taskId": "task-1",
        "tasks": [{"id": "task-1", "title": "待办", "assigneeFeishuUserId": "member", "dueDate": "2026-09-21"}],
    }
    w.url = f"/api/chat/meetings/{w.mid}/followup"
    return w


async def test_concurrent_confirmation_creates_only_one_task(dispatch_workspace):
    w = dispatch_workspace
    responses = await asyncio.gather(*(w.http.patch(w.url, json=w.patch) for _ in range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    w.client.create_task.assert_awaited_once()
    w.client.send_text_message.assert_not_awaited()
    from datetime import UTC, datetime
    assert datetime.fromtimestamp(w.client.create_task.await_args.kwargs["due_timestamp"], UTC).isoformat() == (
        "2026-09-21T00:00:00+00:00"
    )
    w.patch["version"] = 2
    assert (await w.http.patch(w.url, json=w.patch)).status_code == 200
    w.client.create_task.assert_awaited_once()
    w.client.edit_task.assert_not_awaited()
    w.client.update_task.assert_not_awaited()


async def test_uncertain_creation_never_resends_after_window_or_edits(dispatch_workspace):
    from datetime import timedelta

    w = dispatch_workspace
    w.client.create_task.side_effect = FeishuClientError("Response lost after create")
    first = await w.http.patch(w.url, json=w.patch)
    assert first.status_code == 200 and first.json()["deliveryError"]
    async with w.db() as db:
        receipt = await db.scalar(select(MeetingTaskCreation))
        assert receipt is not None
        receipt.created_at -= timedelta(days=1)
    w.client.create_task.side_effect = None
    w.patch.update(version=first.json()["meeting"]["version"], action="RESEND")
    w.patch["tasks"][0]["title"] = "修改后的标题"
    retried = await w.http.patch(w.url, json=w.patch)
    assert retried.status_code == 200
    assert "待核对" in retried.json()["deliveryError"]
    w.client.create_task.assert_awaited_once()
    _, admin = await snapshot(w, w.mid)
    event = next(e for e in admin["events"] if e["action"] == "TASK_RESEND")
    assert event["detail"]["outcome"] == "FAILED"


@pytest.fixture
async def reconciliation_workspace(dispatch_workspace, monkeypatch):
    from datetime import timedelta
    from yuxi.product_chat import meeting_task_creation as creation

    w = dispatch_workspace
    monkeypatch.setattr(creation, "FeishuClient", lambda: w.client)
    w.client.create_task.side_effect = FeishuClientError("Response lost")
    response = await w.http.patch(w.url, json=w.patch)
    assert response.status_code == 200
    async with w.db() as db:
        receipt = await db.scalar(select(MeetingTaskCreation))
        receipt.created_at -= timedelta(minutes=10)
        w.key = receipt.request_key
        original = receipt.request
    assert w.client.create_task.await_args.kwargs["extra"] == w.key
    w.client.get_task.return_value = {
        "guid": "remote-task", "extra": w.key, "summary": original["summary"],
        "description": original["description"], "completed_at": "0",
        "members": [{"id": "member", "type": "user", "role": "assignee"}],
        "due": {"timestamp": str(original["due_timestamp"] * 1000), "is_all_day": True},
    }
    w.reconcile_url = f"/api/meeting-management/{w.mid}/tasks/task-1/reconcile"
    w.reconcile_patch = {"version": response.json()["meeting"]["version"], "feishuTaskId": "remote-task"}
    return w


async def test_reconciliation_previews_and_recovers_without_external_writes(reconciliation_workspace):
    w = reconciliation_workspace
    w.client.get_task.return_value["completed_at"] = "1750000000000"
    preview = await w.http.post(w.reconcile_url + "/preview", json=w.reconcile_patch)
    assert preview.status_code == 200, preview.text
    assert preview.json()["remote"]["status"] == "DONE"
    assert preview.json()["pendingUpdate"] is False
    before, _ = await snapshot(w, w.mid)
    assert not before["result"]["followup"]["tasks"][0]["delivery"].get("feishuTaskId")
    response = await w.http.post(w.reconcile_url, json={**w.reconcile_patch, "reason": "已核对原任务"})
    assert response.status_code == 200, response.text
    after, admin = await snapshot(w, w.mid)
    task = after["result"]["followup"]["tasks"][0]
    assert task["delivery"]["feishuTaskId"] == "remote-task"
    assert task["delivery"]["syncStatus"] == "SYNCED" and task["delivery"]["error"] is None
    assert task["status"] == "DONE" and task["reviewStatus"] == "CONFIRMED"
    event = next(e for e in admin["events"] if e["action"] == "TASK_RECONCILE")
    assert event["detail"]["reason"] == "已核对原任务"
    assert event["detail"]["feishuTaskId"] == "remote-task"
    async with w.db() as db:
        assert (await db.get(MeetingTaskCreation, w.key)).feishu_task_id == "remote-task"
    assert (await w.http.post(w.reconcile_url, json={**w.reconcile_patch, "reason": "重复"})).status_code == 409
    w.client.create_task.assert_awaited_once()  # Only the original failed attempt.
    w.client.edit_task.assert_not_awaited()
    w.client.send_text_message.assert_not_awaited()


@pytest.mark.parametrize("problem", [
    "wrong_marker", "missing_marker", "application", "identity", "inflight", "duplicate", "historical_duplicate",
    "no_receipt", "archived", "stale", "invalid_status",
])
async def test_reconciliation_rejects_unproven_or_conflicting_task(reconciliation_workspace, problem):
    from yuxi.utils.datetime_utils import utc_now_naive

    w = reconciliation_workspace
    async with w.db() as db:
        receipt = await db.get(MeetingTaskCreation, w.key)
        if problem == "wrong_marker":
            w.client.get_task.return_value["extra"] = "another-meeting"
        elif problem == "missing_marker":
            w.client.get_task.return_value.pop("extra")
        elif problem == "application":
            receipt.app_id = "another-app"
        elif problem == "identity":
            w.client.get_employee.return_value = {"open_id": "another-tenant"}
        elif problem == "inflight":
            receipt.created_at = utc_now_naive()
        elif problem == "duplicate":
            db.add(MeetingTaskCreation(
                request_key="other-key", app_id="test-app", request={}, feishu_task_id="remote-task"
            ))
        elif problem == "historical_duplicate":
            db.add(ManagedMeetingItem(
                meeting_id=w.mid, kind="TASK", item_id="other-task",
                payload={"id": "other-task", "delivery": {"feishuTaskId": "remote-task"}},
            ))
        elif problem == "no_receipt":
            await db.delete(receipt)
        elif problem == "archived":
            (await db.get(ManagedMeeting, w.mid)).archived = 1
        elif problem == "stale":
            w.reconcile_patch["version"] = 1
        elif problem == "invalid_status":
            w.client.get_task.return_value["completed_at"] = "invalid"
    for endpoint in [w.reconcile_url + "/preview", w.reconcile_url]:
        response = await w.http.post(endpoint, json={**w.reconcile_patch, "reason": "核对"})
        assert response.status_code in (409, 422), response.text
    if problem == "historical_duplicate":
        async with w.db() as db:
            await db.delete(await db.get(ManagedMeetingItem, (w.mid, "TASK", "other-task")))
    after, admin = await snapshot(w, w.mid)
    assert not after["result"]["followup"]["tasks"][0]["delivery"].get("feishuTaskId")
    assert not any(e["action"] == "TASK_RECONCILE" for e in admin["events"])
    w.client.edit_task.assert_not_awaited()


async def test_reconciliation_atomic_on_save_failure_and_concurrent_confirmation(reconciliation_workspace, monkeypatch):
    w = reconciliation_workspace
    original = MeetingRepository.save_result
    monkeypatch.setattr(MeetingRepository, "save_result", AsyncMock(side_effect=RuntimeError("save failed")))
    with pytest.raises(RuntimeError, match="save failed"):
        await w.http.post(w.reconcile_url, json={**w.reconcile_patch, "reason": "核对"})
    async with w.db() as db:
        assert (await db.get(MeetingTaskCreation, w.key)).feishu_task_id is None
    monkeypatch.setattr(MeetingRepository, "save_result", original)
    responses = await asyncio.gather(*(
        w.http.post(w.reconcile_url, json={**w.reconcile_patch, "reason": "核对"}) for _ in range(2)
    ))
    assert sorted(r.status_code for r in responses) == [200, 409]
    _, admin = await snapshot(w, w.mid)
    assert sum(e["action"] == "TASK_RECONCILE" for e in admin["events"]) == 1


async def test_reconciliation_legacy_success_receipt_and_task_links(reconciliation_workspace):
    w = reconciliation_workspace
    async with w.db() as db:
        (await db.get(MeetingTaskCreation, w.key)).feishu_task_id = "remote-task"
    w.client.get_task.return_value.pop("extra")
    response = await w.http.post(w.reconcile_url + "/preview", json={
        **w.reconcile_patch, "feishuTaskId": "https://applink.feishu.cn/client/todo/detail?guid=remote-task",
    })
    assert response.status_code == 200 and response.json()["matchBasis"] == "已保存的成功回执"
    w.client.get_task.reset_mock()
    for link in ["https://127.0.0.1/private?guid=remote-task", "../remote-task",
                 "https://applink.feishu.cn/client/todo/detail?guid=one&guid=two"]:
        response = await w.http.post(w.reconcile_url + "/preview", json={**w.reconcile_patch, "feishuTaskId": link})
        assert response.status_code == 422
    w.client.get_task.assert_not_awaited()
    w.client.send_text_message.assert_not_awaited()


async def test_reconciliation_revalidates_remote_and_preserves_local_edits(reconciliation_workspace):
    w = reconciliation_workspace
    assert (await w.http.post(w.reconcile_url + "/preview", json=w.reconcile_patch)).status_code == 200
    w.client.get_task.return_value["extra"] = "wrong"
    response = await w.http.post(w.reconcile_url, json={**w.reconcile_patch, "reason": "核对"})
    assert response.status_code == 422
    w.client.get_task.return_value.update(extra=w.key, summary="飞书端修改的标题")
    response = await w.http.post(w.reconcile_url, json={**w.reconcile_patch, "reason": "保留本地安排待同步"})
    assert response.status_code == 200, response.text
    saved, _ = await snapshot(w, w.mid)
    task = saved["result"]["followup"]["tasks"][0]
    assert task["title"] == "待办" and task["dueDate"] == "2026-09-21"
    assert task["delivery"]["pendingUpdate"] is True and task["delivery"]["syncStatus"] == "PENDING"
    w.client.edit_task.assert_not_awaited()


async def test_creation_receipt_reused_across_reruns_and_app_change_rejected(dispatch_workspace):
    from yuxi.product_chat.meeting_task_creation import create_task_once, TaskCreationUncertain

    w = dispatch_workspace
    request = {"user_id": "member", "summary": "测试", "description": "", "due_timestamp": None}
    args = {"source_meeting_id": w.mid, "task_id": "task-1", "request": request}
    first, _, recovered = await create_task_once(w.client, **args)
    assert not recovered
    # A rerun carries the source meeting/task identity. Updated payload must
    # update the recovered GUID, never acquire a new creation identity.
    second, original, recovered = await create_task_once(
        w.client, **{**args, "request": {**request, "summary": "新标题"}},
    )
    assert first == second and recovered and original == request
    w.client.application_id = "different-app"
    with pytest.raises(TaskCreationUncertain, match="应用配置已变化"):
        await create_task_once(w.client, **args)
    w.client.create_task.assert_awaited_once()


async def test_failed_intent_commit_never_calls_feishu(dispatch_workspace, monkeypatch):
    w = dispatch_workspace
    calls = 0

    @asynccontextmanager
    async def fail_intent():
        nonlocal calls
        calls += 1
        async with w.db() as db:
            yield db
            if calls == 2:
                raise RuntimeError("Intent commit failed")

    monkeypatch.setattr(pg_manager, "get_async_session_context", fail_intent)
    with pytest.raises(RuntimeError, match="Intent commit failed"):
        await w.http.patch(w.url, json=w.patch)
    w.client.create_task.assert_not_awaited()
    async with w.db() as db:
        assert await db.scalar(select(MeetingTaskCreation)) is None


async def test_empty_title_rejected_before_creation_can_be_corrected(dispatch_workspace):
    w = dispatch_workspace
    w.patch["tasks"][0]["title"] = " "
    response = await w.http.patch(w.url, json=w.patch)
    assert response.status_code == 422
    w.client.create_task.assert_not_awaited()
    async with w.db() as db:
        assert await db.scalar(select(MeetingTaskCreation)) is None
    w.patch["tasks"][0]["title"] = "填写后的标题"
    response = await w.http.patch(w.url, json=w.patch)
    assert response.status_code == 200 and not response.json()["deliveryError"]
    w.client.create_task.assert_awaited_once()


async def test_legacy_unknown_delivery_is_not_recreated(dispatch_workspace):
    w = dispatch_workspace
    async with w.db() as db:
        record = await db.get(MeetingRecord, w.mid)
        result = deepcopy(record.result)
        result["followup"]["tasks"][0].update(reviewStatus="CONFIRMED", delivery={"notification": "FAILED"})
        record.result = result
    response = await w.http.patch(w.url, json=w.patch)
    assert response.status_code == 200
    assert "历史发送结果待核对" in response.json()["deliveryError"]
    w.client.create_task.assert_not_awaited()


@pytest.mark.parametrize("failure", ["response_lost", "transaction_failed", "transaction_failed_then_edited"])
async def test_durable_creation_survives_lost_response_or_local_rollback(dispatch_workspace, monkeypatch, failure):
    from server.routers import product_meeting_router as router
    from yuxi.integrations.feishu.client import FeishuClient

    w = dispatch_workspace
    remote_tasks, requests = {}, []

    async def feishu(request):
        if request.method == "GET":
            return Response(200, json={"code": 0, "data": {"task": {
                "guid": "remote-1", "completed_at": "0",
                "members": [{"id": "member", "type": "user", "role": "assignee"}],
            }}})
        if request.method == "PATCH":
            assert request.url.path.endswith("/remote-1")
            payload = json.loads(request.content)
            assert payload["task"]["summary"] == "重试前修改标题"
            assert "completed_at" not in payload["update_fields"]
            return Response(200, json={"code": 0})
        assert request.method == "POST" and request.url.path == "/open-apis/task/v2/tasks"
        payload = json.loads(request.content)
        requests.append(payload)
        # This fake models only identical retries inside the documented five
        # minutes, not indefinite deduplication or real Feishu acceptance.
        key = payload.get("client_token") or f"unprotected-{len(requests)}"
        remote_tasks.setdefault(key, {"guid": f"remote-{len(remote_tasks) + 1}"})
        if failure == "response_lost" and len(requests) == 1:
            raise ReadTimeout("Response lost after remote success", request=request)
        return Response(200, json={"code": 0, "data": {"task": remote_tasks[key]}})

    async def token_provider(force_refresh):
        return "test-token"

    monkeypatch.setattr(router, "FeishuClient", lambda: FeishuClient(
        client=AsyncClient(transport=MockTransport(feishu)),
        token_provider=token_provider, environ={"FEISHU_APP_ID": "test-app"}, max_retries=0,
    ))

    class CommitFailure(RuntimeError):
        pass

    if failure.startswith("transaction_failed"):
        fail_commit = True

        save = MeetingRepository.save_result

        async def failing_save(self, *args, **kwargs):
            nonlocal fail_commit
            await save(self, *args, **kwargs)
            if fail_commit:
                fail_commit = False
                raise CommitFailure("Simulated failure before transaction commit")

        monkeypatch.setattr(MeetingRepository, "save_result", failing_save)
        with pytest.raises(CommitFailure):
            await w.http.patch(w.url, json=w.patch)
        async with w.db() as db:
            record = await db.get(MeetingRecord, w.mid)
            assert record.version == 1
            assert not record.result["followup"]["tasks"][0]["delivery"].get("feishuTaskId")
            receipt = await db.scalar(select(MeetingTaskCreation))
            from datetime import timedelta
            receipt.created_at -= timedelta(days=1)
        if failure == "transaction_failed_then_edited":
            w.patch["tasks"][0]["title"] = "重试前修改标题"
    else:
        first = await w.http.patch(w.url, json=w.patch)
        assert first.status_code == 200 and first.json()["deliveryError"]
        w.patch.update(version=first.json()["meeting"]["version"], action="RESEND")

    assert len(remote_tasks) == 1
    retried = await w.http.patch(w.url, json=w.patch)
    assert retried.status_code == 200
    assert len(requests) == 1
    assert len(remote_tasks) == 1
    saved, admin = await snapshot(w, w.mid)
    saved_task = saved["result"]["followup"]["tasks"][0]
    if failure == "response_lost":
        assert "待核对" in retried.json()["deliveryError"]
        assert not saved_task["delivery"]["feishuTaskId"]
        assert saved_task["delivery"]["syncStatus"] == "FAILED"
    else:
        assert not retried.json()["deliveryError"]
        assert saved_task["delivery"]["feishuTaskId"] == "remote-1"
        assert saved_task["delivery"]["syncStatus"] == "SYNCED"
    event = next(e for e in admin["events"] if e["action"] == f"TASK_{w.patch['action']}")
    assert event["detail"]["outcome"] == ("FAILED" if failure == "response_lost" else "SUCCESS")
    w.client.send_text_message.assert_not_awaited()


async def test_failed_read_retry_must_contact_feishu(dispatch_workspace):
    w = dispatch_workspace
    assert (await w.http.patch(w.url, json=w.patch)).status_code == 200
    w.client.get_task.side_effect = FeishuClientError("无读取权限")
    await sync.sync_task_batch()
    saved, _ = await snapshot(w, w.mid)
    w.patch.update(version=saved["version"], action="RESEND")
    retried = await w.http.patch(w.url, json=w.patch)
    assert retried.status_code == 200
    assert retried.json()["deliveryError"]
    delivery = retried.json()["meeting"]["result"]["followup"]["tasks"][0]["delivery"]
    assert delivery["syncStatus"] == "FAILED"
    assert delivery["feishuTaskId"] == "remote-task" and delivery["notification"] == "SENT"
    _, admin = await snapshot(w, w.mid)
    event = next(e for e in admin["events"] if e["action"] == "TASK_RESEND")
    assert event["actorUserId"] == w.user.id
    assert event["detail"]["outcome"] == "FAILED"
    assert event["detail"]["taskId"] == "task-1"
    assert event["detail"]["actorName"] == w.user.username
    w.client.get_task.side_effect = None
    w.client.get_task.return_value = {"completed_at": "1750000000000"}
    w.patch["version"] = retried.json()["meeting"]["version"]
    recovered = (await w.http.patch(w.url, json=w.patch)).json()
    assert not recovered["deliveryError"]
    t = recovered["meeting"]["result"]["followup"]["tasks"][0]
    assert t["status"] == "DONE" and t["delivery"]["syncStatus"] == "SYNCED"
    assert t["delivery"]["syncError"] is None
    w.client.create_task.assert_awaited_once()
    w.client.send_text_message.assert_not_awaited()


async def test_management_permission_rechecked_and_tenants_isolated(workspace):
    from server.utils.auth_middleware import get_db
    from yuxi.utils.auth_utils import AuthUtils

    w = workspace
    mid = await w.create()
    async with w.db() as db:
        member = User(username="普通成员", uid="member", role="user", password_hash="unused",
                      department_id=w.user.department_id)
        foreign = User(username="其他企业管理员", uid="foreign", role="admin", password_hash="unused",
                       department_id=w.user.department_id)
        db.add_all([member, foreign])
        await db.flush()
        for u, tenant in [(member, "test-tenant"), (foreign, "another-tenant")]:
            db.add(FeishuUserBinding(user_id=u.id, feishu_user_id=u.uid, feishu_open_id=u.uid,
                                    tenant_key=tenant, display_name=u.username, authorization_status="ACTIVE"))

    async def db_dependency():
        async with w.db() as db:
            yield db

    w.app.dependency_overrides.clear()
    w.app.dependency_overrides[get_db] = db_dependency

    def headers(uid, **claims):
        return {"Authorization": "Bearer " + AuthUtils.create_access_token({"sub": str(uid), **claims})}

    path = f"/api/meeting-management/{mid}"
    assert (await w.http.get(path)).status_code == 401
    assert (await w.http.get(path, headers=headers(member.id))).status_code == 403
    assert (await w.http.get(path, headers=headers(foreign.id))).status_code == 404
    assert (await w.http.get(path, headers=headers(w.user.id, token_kind="enterprise_assistant"))).status_code == 401
    admin_headers = headers(w.user.id)
    assert (await w.http.get(path, headers=admin_headers)).status_code == 200
    for suffix in ["/preview", ""]:
        for auth, status in [({}, 401), (headers(member.id), 403), (headers(foreign.id), 404),
                             (headers(w.user.id, token_kind="enterprise_assistant"), 401)]:
            response = await w.http.post(path + "/tasks/task-1/reconcile" + suffix, headers=auth,
                                         json={"version": 1, "feishuTaskId": "remote-task", "reason": "核对"})
            assert response.status_code == status
    # A previously issued token loses management access immediately on role revocation.
    async with w.db() as db:
        user = await db.get(User, w.user.id)
        user.role = "user"
    assert (await w.http.get(path, headers=admin_headers)).status_code == 403
    w.http.cookies.set("enterprise_assistant_session", AuthUtils.create_access_token(
        {"sub": str(member.id), "token_kind": "enterprise_assistant"}
    ))
    assert (await w.http.get(f"/api/chat/meetings/{mid}")).status_code == 404


async def test_explicit_status_change_survives_save_and_failed_update(dispatch_workspace):
    w = dispatch_workspace
    assert (await w.http.patch(w.url, json=w.patch)).status_code == 200
    w.patch.update(version=2, action="SAVE")
    w.patch["tasks"][0]["status"] = "DONE"
    saved = (await w.http.patch(w.url, json=w.patch)).json()["meeting"]
    assert saved["result"]["followup"]["tasks"][0]["delivery"]["pendingStatusUpdate"]
    w.client.edit_task.assert_not_awaited()
    w.patch.update(version=saved["version"], action="CONFIRM")
    w.client.edit_task.side_effect = FeishuClientError("临时失败")
    failed = (await w.http.patch(w.url, json=w.patch)).json()["meeting"]
    assert failed["result"]["followup"]["tasks"][0]["delivery"]["pendingStatusUpdate"]
    w.client.edit_task.side_effect = None
    w.patch["version"] = failed["version"]
    updated = (await w.http.patch(w.url, json=w.patch)).json()["meeting"]
    assert w.client.edit_task.await_args.kwargs["completed"] is True
    delivery = updated["result"]["followup"]["tasks"][0]["delivery"]
    assert not delivery["pendingStatusUpdate"] and not delivery["pendingUpdate"]
    w.client.create_task.assert_awaited_once()
    w.client.send_text_message.assert_not_awaited()

async def test_schedule_drift_is_visible_in_both_products_without_overwriting_or_revision_churn(workspace):
    w = workspace
    mid = await w.create([task(
        assignee={"feishuUserId": "local-user", "displayName": "本地负责人"}, dueDate="2026-09-21"
    )])
    remote = {"completed_at": "1750000000000",
              "members": [{"id": "remote-user", "role": "assignee", "name": "飞书负责人"}],
              "due": {"timestamp": "1790035200000", "is_all_day": True}}
    w.client.get_task.return_value = remote  # 2026-09-22 UTC, all-day dates must not shift with timezone.
    await sync.sync_task_batch()
    first, _ = await snapshot(w, mid)
    saved = first["result"]["followup"]["tasks"][0]
    assert saved["status"] == "DONE"
    assert saved["assignee"]["feishuUserId"] == "local-user"
    assert saved["dueDate"] == "2026-09-21"
    comparison = saved["delivery"]["scheduleComparison"]
    assert comparison["status"] == "DIFFERENT"
    assert comparison["differences"] == ["assignee", "dueDate"]
    assert comparison["remoteDueDate"] == "2026-09-22"
    assert comparison["remoteAssignees"] == [{"id": "remote-user", "name": "飞书负责人"}]
    assert not saved["delivery"].get("pendingUpdate")
    await sync.sync_task_batch()
    repeated, _ = await snapshot(w, mid)
    assert repeated["version"] == first["version"]
    w.client.get_task.side_effect = FeishuClientError("暂时不可读")
    await sync.sync_task_batch()
    failed, _ = await snapshot(w, mid)
    delivery = failed["result"]["followup"]["tasks"][0]["delivery"]
    assert delivery["syncStatus"] == "FAILED"
    assert delivery["scheduleComparison"] == comparison
    assert delivery["scheduleCheckedAt"] == repeated["result"]["followup"]["tasks"][0]["delivery"]["scheduleCheckedAt"]
    w.client.get_task.side_effect = None
    w.client.get_task.return_value = {**remote, "members": [{"id": "local-user", "role": "assignee"}],
                                    "due": {"timestamp": "1789948800000", "is_all_day": True}}
    await sync.sync_task_batch()
    matched, _ = await snapshot(w, mid)
    assert matched["result"]["followup"]["tasks"][0]["delivery"]["scheduleComparison"]["status"] == "MATCH"


@pytest.mark.parametrize("remote, expected, date_label", [
    ({"members": [], "due": None}, "MATCH", None),
    ({"members": []}, "MATCH", None),
    ({"due": None}, "UNVERIFIED", None),
    ({"members": [{"id": 1, "role": "assignee"}]}, "UNVERIFIED", None),
    ({"members": [], "due": []}, "UNVERIFIED", None),
    ({"members": [], "due": {"is_all_day": True}}, "UNVERIFIED", None),
    ({"members": [], "due": {"timestamp": "bad"}}, "UNVERIFIED", None),
    ({"members": [], "due": {"timestamp": "999999999999999999999", "is_all_day": True}}, "UNVERIFIED", None),
    ({"members": [], "due": {"timestamp": "1789916400000", "is_all_day": False}},
     "DIFFERENT", "2026-09-20 23:00（北京时间）"),
])
async def test_schedule_missing_malformed_and_timed_values_do_not_block_completion(
    workspace, remote, expected, date_label
):
    w = workspace
    mid = await w.create([task(assignee=None, dueDate=None)])
    w.client.get_task.return_value = {**remote, "completed_at": "1750000000000"}
    await sync.sync_task_batch()
    saved = (await snapshot(w, mid))[0]["result"]["followup"]["tasks"][0]
    assert saved["status"] == "DONE"
    assert saved["delivery"]["syncStatus"] == "SYNCED"
    comparison = saved["delivery"]["scheduleComparison"]
    assert comparison["status"] == expected
    if expected != "UNVERIFIED":
        assert comparison["remoteDueDate"] == date_label
    else:
        assert comparison["error"]


async def test_explicit_local_edit_keeps_old_snapshot_until_send_then_requires_new_read(dispatch_workspace):
    w = dispatch_workspace
    assert (await w.http.patch(w.url, json=w.patch)).status_code == 200
    w.client.get_task.return_value = {
        "completed_at": "0", "members": [{"id": "member", "role": "assignee"}],
        "due": {"timestamp": "1790035200000", "is_all_day": True},
    }
    await sync.sync_task_batch()
    first, _ = await snapshot(w, w.mid)
    assert first["result"]["followup"]["tasks"][0]["delivery"]["scheduleComparison"]["status"] == "DIFFERENT"
    w.patch.update(version=first["version"], action="SAVE")
    w.patch["tasks"][0]["dueDate"] = "2026-09-22"
    saved = (await w.http.patch(w.url, json=w.patch)).json()["meeting"]
    assert saved["result"]["followup"]["tasks"][0]["delivery"]["pendingUpdate"] is True
    reads = w.client.get_task.await_count
    await sync.sync_task_batch()
    assert w.client.get_task.await_count == reads
    w.client.edit_task.assert_not_awaited()
    w.patch.update(version=saved["version"], action="CONFIRM")
    sent = (await w.http.patch(w.url, json=w.patch)).json()["meeting"]
    assert "scheduleComparison" not in sent["result"]["followup"]["tasks"][0]["delivery"]
    await sync.sync_task_batch()
    matched, _ = await snapshot(w, w.mid)
    assert matched["result"]["followup"]["tasks"][0]["delivery"]["scheduleComparison"]["status"] == "MATCH"
