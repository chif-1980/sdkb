from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from yuxi.product_chat import meeting_directory as directory
from yuxi.product_chat.meeting_service import build_followup


@pytest.mark.asyncio
async def test_directory_includes_non_login_members_and_deduplicates_departments(monkeypatch):
    client = SimpleNamespace(
        get_employee=AsyncMock(return_value={"open_id": "current-open"}),
        list_contact_pages=AsyncMock(
            side_effect=[
                [{"open_department_id": "sales", "parent_department_id": "0", "name": "销售部"}],
                [{"user_id": "member", "name": "张三", "en_name": "Sam", "status": {}}],
                [
                    {"user_id": "member", "name": "张三", "status": {}},
                    {"user_id": "left", "name": "离职员工", "status": {"is_resigned": True}},
                ],
            ]
        ),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(directory, "FeishuClient", lambda: client)
    db = SimpleNamespace(
        scalar=AsyncMock(
            return_value=SimpleNamespace(
                feishu_user_id="current",
                feishu_open_id="current-open",
                tenant_key="tenant-a",
            )
        ),
        scalars=AsyncMock(return_value=[]),
    )
    result = await directory.load_meeting_directory(db, 1)
    assert result["departments"][1]["parentId"] == "0"
    assert result["users"] == [
        {
            "userId": None,
            "feishuUserId": "member",
            "displayName": "张三",
            "englishName": "Sam",
            "departmentIds": ["0", "sales"],
        }
    ]
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_directory_rejects_application_from_another_tenant(monkeypatch):
    client = SimpleNamespace(
        get_employee=AsyncMock(return_value={"open_id": "another-tenant"}),
        list_contact_pages=AsyncMock(),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(directory, "FeishuClient", lambda: client)
    db = SimpleNamespace(
        scalar=AsyncMock(
            return_value=SimpleNamespace(
                feishu_user_id="current",
                feishu_open_id="current-open",
                tenant_key="tenant-a",
            )
        )
    )
    with pytest.raises(HTTPException) as exc:
        await directory.load_meeting_directory(db, 1)
    assert exc.value.status_code == 403
    client.list_contact_pages.assert_not_called()
    client.aclose.assert_awaited_once()


def test_ambiguous_deadline_is_preserved_without_inventing_date():
    result = build_followup(
        {"actionItems": [{"title": "测试", "dueDate": "下周"}]}, coordinator_id=1, coordinator_name="上传者"
    )
    assert result["tasks"][0]["dueDate"] is None
    assert result["tasks"][0]["dueDateSuggestion"] == "下周"


@pytest.mark.asyncio
@pytest.mark.parametrize("feishu_id,allowed", [("member", True), ("foreign-member", False)])
async def test_assignment_checks_directory_and_allows_employee_without_local_account(monkeypatch, feishu_id, allowed):
    from contextlib import asynccontextmanager

    from server.routers import product_meeting_router as router

    record = SimpleNamespace(
        state="completed",
        version=1,
        result={
            "followup": {
                "tasks": [{"id": "t1", "title": "核对资料", "sourceRefs": ["S1-P1"]}],
            }
        },
    )
    db = object()

    @asynccontextmanager
    async def session():
        yield db

    save = AsyncMock()
    monkeypatch.setattr(router.pg_manager, "get_async_session_context", session)
    monkeypatch.setattr(router, "require_meeting", AsyncMock(return_value=record))
    monkeypatch.setattr(
        router,
        "load_meeting_directory",
        AsyncMock(
            return_value={
                "users": [
                    {"userId": None, "feishuUserId": "member", "displayName": "张三"},
                ]
            }
        ),
    )
    monkeypatch.setattr(router, "MeetingRepository", lambda _: SimpleNamespace(save_result=save))
    monkeypatch.setattr(router, "serialize_meeting", lambda _: {})
    patch = router.MeetingFollowupEdit(
        version=1,
        tasks=[
            router.FollowupTaskEdit(
                id="t1",
                title="核对资料",
                assigneeFeishuUserId=feishu_id,
            )
        ],
    )
    if allowed:
        await router.edit_meeting_followup("MT-test", patch, SimpleNamespace(id=1, username="上传者"))
        task = save.await_args.args[1]["followup"]["tasks"][0]
        assert task["assignee"] == {"userId": None, "feishuUserId": "member", "displayName": "张三"}
        assert task["sourceRefs"] == ["S1-P1"]
    else:
        with pytest.raises(HTTPException) as exc:
            await router.edit_meeting_followup("MT-test", patch, SimpleNamespace(id=1, username="上传者"))
        assert exc.value.status_code == 422
        save.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_sends_task_content_and_source_refs(monkeypatch):
    from contextlib import asynccontextmanager

    from server.routers import product_meeting_router as router

    record = SimpleNamespace(
        id="MT-test",
        state="completed",
        version=1,
        result={
            "title": "产品交流",
            "followup": {
                "coordinator": {"userId": "1", "displayName": "上传者"},
                "tasks": [{"id": "t1", "title": "核对资料", "content": "补充报价范围", "assignee": None,
                            "dueDate": None, "status": "OPEN", "sourceRefs": ["S1-P3"]}],
                "knowledgeSuggestions": [],
            },
        },
    )
    db = object()

    @asynccontextmanager
    async def session():
        yield db

    class FakeFeishuClient:
        def __init__(self):
            self.message = None
            self.task = None

        async def send_text_message(self, **kwargs):
            self.message = kwargs
            return {"data": {"message_id": "om_1"}}

        async def create_task(self, **kwargs):
            self.task = kwargs
            return {"data": {"task": {"guid": "task_1"}}}

        async def aclose(self):
            return None

    client = FakeFeishuClient()
    save = AsyncMock()
    monkeypatch.setattr(router.pg_manager, "get_async_session_context", session)
    monkeypatch.setattr(router, "require_meeting", AsyncMock(return_value=record))
    monkeypatch.setattr(router, "load_meeting_directory", AsyncMock(return_value={"users": [
        {"userId": None, "feishuUserId": "ou_1", "feishuOpenId": "ou_open_1", "displayName": "张三"},
    ]}))
    monkeypatch.setattr(router, "MeetingRepository", lambda _: SimpleNamespace(save_result=save))
    monkeypatch.setattr(router, "serialize_meeting", lambda _: {})
    monkeypatch.setattr(router, "FeishuClient", lambda: client)

    patch = router.MeetingFollowupEdit(
        version=1,
        action="CONFIRM",
        taskId="t1",
        tasks=[router.FollowupTaskEdit(
            id="t1", title="核对资料", content="补充报价范围", assigneeFeishuUserId="ou_1",
        )],
    )
    await router.edit_meeting_followup("MT-test", patch, SimpleNamespace(id=1, username="上传者"))

    assert "内容：补充报价范围" in client.message["text"]
    assert "依据：S1-P3" in client.message["text"]
    assert "补充报价范围" in client.task["description"]
    assert "依据：S1-P3" in client.task["description"]
    task = save.await_args.args[1]["followup"]["tasks"][0]
    assert task["reviewStatus"] == "CONFIRMED"
    assert task["delivery"] == {
        "notification": "SENT", "feishuTaskId": "task_1", "messageId": "om_1",
        "chatId": None, "error": None, "pendingUpdate": False, "syncStatus": "SYNCED",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("notification_fails", [False, True])
async def test_resend_sends_a_new_message_without_creating_a_second_task(monkeypatch, notification_fails):
    from contextlib import asynccontextmanager

    from server.routers import product_meeting_router as router

    record = SimpleNamespace(
        id="MT-test",
        state="completed",
        version=2,
        result={
            "title": "产品交流",
            "followup": {
                "coordinator": {"userId": "1", "displayName": "上传者"},
                "tasks": [{
                    "id": "t1", "title": "核对资料", "content": "补充报价范围",
                    "assignee": {"userId": None, "feishuUserId": "ou_1", "feishuOpenId": "ou_open_1", "displayName": "张三"},
                    "dueDate": None, "status": "OPEN", "sourceRefs": ["S1-P3"],
                    "reviewStatus": "CONFIRMED",
                    "delivery": {"notification": "SENT", "feishuTaskId": "task_1", "messageId": "om_old", "chatId": None, "error": None},
                }],
                "knowledgeSuggestions": [],
            },
        },
    )

    @asynccontextmanager
    async def session():
        yield object()

    class FakeFeishuClient:
        def __init__(self):
            self.message = None
            self.task = None

        async def send_text_message(self, **kwargs):
            self.message = kwargs
            if notification_fails:
                raise router.FeishuClientError("notification unavailable")
            return {"data": {"message_id": "om_new", "chat_id": "oc_new"}}

        async def create_task(self, **kwargs):
            self.task = kwargs
            return {"data": {"task": {"guid": "task_1"}}}

        async def aclose(self):
            return None

    client = FakeFeishuClient()
    save = AsyncMock()
    monkeypatch.setattr(router.pg_manager, "get_async_session_context", session)
    monkeypatch.setattr(router, "require_meeting", AsyncMock(return_value=record))
    monkeypatch.setattr(router, "load_meeting_directory", AsyncMock(return_value={"users": [
        {"userId": None, "feishuUserId": "ou_1", "feishuOpenId": "ou_open_1", "displayName": "张三"},
    ]}))
    monkeypatch.setattr(router, "MeetingRepository", lambda _: SimpleNamespace(save_result=save))
    monkeypatch.setattr(router, "serialize_meeting", lambda _: {})
    monkeypatch.setattr(router, "FeishuClient", lambda: client)

    patch = router.MeetingFollowupEdit(
        version=2,
        action="RESEND",
        taskId="t1",
        tasks=[router.FollowupTaskEdit(
            id="t1", title="核对资料", content="补充报价范围", assigneeFeishuUserId="ou_1",
        )],
    )
    await router.edit_meeting_followup("MT-test", patch, SimpleNamespace(id=1, username="上传者"))

    assert client.message["user_id"] == "ou_1"
    assert client.message["text"].startswith("会议待办：核对资料")
    assert client.task is None
    task = save.await_args.args[1]["followup"]["tasks"][0]
    assert task["delivery"]["feishuTaskId"] == "task_1"
    assert task["delivery"]["notification"] == ("FAILED" if notification_fails else "SENT")
    assert task["delivery"]["messageId"] == (None if notification_fails else "om_new")
    assert task["delivery"]["chatId"] == (None if notification_fails else "oc_new")


@pytest.mark.asyncio
async def test_edit_confirmed_task_preserves_guid_and_updates_instead_of_recreating(monkeypatch):
    from contextlib import asynccontextmanager
    from server.routers import product_meeting_router as router

    old = {"id": "t1", "title": "原任务", "content": "", "status": "OPEN", "dueDate": None,
           "assignee": {"userId": None, "feishuUserId": "u1", "displayName": "员工"},
           "sourceRefs": [], "reviewStatus": "CONFIRMED",
           "delivery": {"feishuTaskId": "existing-task", "messageId": "existing-message", "notification": "SENT"}}
    record = SimpleNamespace(id="MT-test", state="completed", version=1,
                             result={"title": "会议", "followup": {"tasks": [old]}})

    @asynccontextmanager
    async def session():
        yield object()

    save = AsyncMock()
    client = SimpleNamespace(edit_task=AsyncMock(), create_task=AsyncMock(), send_text_message=AsyncMock(),
                             aclose=AsyncMock())
    monkeypatch.setattr(router.pg_manager, "get_async_session_context", session)
    monkeypatch.setattr(router, "require_meeting", AsyncMock(return_value=record))
    monkeypatch.setattr(router, "load_meeting_directory", AsyncMock(return_value={"users": [
        {"userId": None, "feishuUserId": "u1", "displayName": "员工"},
    ]}))
    monkeypatch.setattr(router, "MeetingRepository", lambda _: SimpleNamespace(save_result=save))
    monkeypatch.setattr(router, "serialize_meeting", lambda _: {})
    monkeypatch.setattr(router, "FeishuClient", lambda: client)
    patch = router.MeetingFollowupEdit(version=1, action="CONFIRM", taskId="t1", tasks=[
        router.FollowupTaskEdit(id="t1", title="修改后的任务", assigneeFeishuUserId="u1")])
    await router.edit_meeting_followup("MT-test", patch, SimpleNamespace(id=1, username="用户"))
    client.edit_task.assert_awaited_once()
    assert client.edit_task.await_args.kwargs["task_id"] == "existing-task"
    client.create_task.assert_not_called()
    client.send_text_message.assert_not_called()
    saved = save.await_args.args[1]["followup"]["tasks"][0]
    assert saved["delivery"]["feishuTaskId"] == "existing-task"
    assert saved["reviewStatus"] == "CONFIRMED"
