"""Real HTTP boundary tests; no model calls or external Feishu writes."""

import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, update

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Department, User
from yuxi.storage.postgres.models_product import (
    FeishuUserBinding,
    MeetingRecord,
    MeetingRevision,
    ProductConversation,
    ProductMessage,
)
from yuxi.utils.auth_utils import AuthUtils

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_owned_management_pagination_archive_revisions_and_failed_rerun():
    if os.getenv("MEETING_ISOLATED_TEST") != "1":
        pytest.skip("Requires isolated meeting test API")
    pg_manager.initialize()
    prefix = f"management-{uuid4().hex[:8]}"
    ids, mids, conversations = [], [], []
    try:
        async with pg_manager.get_async_session_context() as db:
            dept = Department(name=prefix)
            db.add(dept)
            await db.flush()
            dept_id = dept.id
            for index in range(3):
                user = User(
                    username=f"{prefix}-{index}",
                    uid=f"{prefix}-{index}",
                    password_hash="unused",
                    role="admin" if index < 2 else "user",
                    department_id=dept.id,
                )
                db.add(user)
                await db.flush()
                ids.append(user.id)
                conversation = ProductConversation(owner_user_id=user.id, title="同名会议", status="ACTIVE")
                db.add(conversation)
                await db.flush()
                conversations.append(conversation.conversation_id)
                for n in range(3 if index == 0 else 1):
                    question = ProductMessage(conversation_id=conversation.conversation_id, role="USER", content="会议")
                    answer = ProductMessage(
                        conversation_id=conversation.conversation_id,
                        role="ASSISTANT",
                        content="纪要",
                        answer_status="INSUFFICIENT",
                    )
                    db.add_all([question, answer])
                    await db.flush()
                    mid = f"{prefix}-{index}-{n}"
                    mids.append(mid)
                    result = {
                        "title": "同名会议",
                        "body": "测试纪要 正式记录",
                        "followup": {
                            "tasks": [
                                {
                                    "id": "task-1",
                                    "title": "跟进",
                                    "status": "OPEN",
                                    "reviewStatus": "CONFIRMED",
                                    "delivery": {"feishuTaskId": "never-send-this"},
                                }
                            ],
                            "knowledgeSuggestions": [
                                {"id": "knowledge-1", "title": "新增资料", "status": "PENDING_MAINTAINER"}
                            ],
                        },
                    }
                    db.add(
                        MeetingRecord(
                            id=mid,
                            conversation_id=conversation.conversation_id,
                            message_id=answer.message_id,
                            user_message_id=question.message_id,
                            request_id=mid,
                            state="failed" if n == 1 else "completed",
                            version=0 if n == 1 else 1,
                            input={"parentId": f"{prefix}-0-0"} if n == 1 else {},
                            result=None if n == 1 else result,
                            error={"message": "测试重跑失败"} if n == 1 else None,
                            sources=[{"title": "资料", "url": "https://example.com/meeting", "paragraphs": []}],
                        )
                    )
                    await db.flush()
                    if n != 1:
                        db.add(MeetingRevision(meeting_id=mid, version=1, result=result))

        def token(uid):
            return AuthUtils.create_access_token({"sub": str(uid)})

        async with httpx.AsyncClient(
            base_url=os.getenv("TEST_BASE_URL", "http://localhost:5051"),
            timeout=60,
            headers={"Authorization": f"Bearer {token(ids[0])}"},
        ) as client:
            response = await client.get("/api/meeting-management", params={"limit": 1})
            assert response.status_code == 200, response.text
            assert response.json()["total"] == 2  # Only explicit parent links combine versions.
            assert len(response.json()["items"]) == 1
            response = await client.get(f"/api/meeting-management/{mids[0]}")
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["meeting"]["state"] == "failed"
            assert data["current"]["result"]["body"] == "测试纪要 正式记录"
            assert len(data["runs"]) == 2
            assert data["current"]["result"]["followup"]["tasks"][0]["delivery"]["feishuTaskId"] == "never-send-this"
            assert (await client.get(f"/api/meeting-management/{mids[-1]}")).status_code == 404
            assert (await client.get(f"/api/meeting-management/{mids[0]}/runs/{mids[-1]}")).status_code == 404
            archive = {"version": data["meeting"]["version"], "archived": True}
            assert (await client.patch(f"/api/meeting-management/{mids[0]}/archive", json=archive)).status_code == 200
            assert (await client.patch(f"/api/meeting-management/{mids[0]}/archive", json=archive)).status_code == 409
            assert (await client.get("/api/meeting-management")).json()["total"] == 1
            assert (await client.get("/api/meeting-management", params={"archived": True})).json()["total"] == 1
            assert (await client.post(f"/api/meeting-management/{mids[0]}/retry")).status_code == 409
            own = mids[2]
            async with pg_manager.get_async_session_context() as db:
                await db.execute(
                    update(ProductConversation)
                    .where(
                        ProductConversation.conversation_id == conversations[0],
                    )
                    .values(status="ARCHIVED")
                )
            edit = await client.patch(
                f"/api/meeting-management/{own}/minutes",
                json={
                    "version": 1,
                    "title": "已修改",
                    "body": "人工修订内容",
                    "meetingType": "内部会议",
                },
            )
            assert edit.status_code == 200, edit.text
            assert (
                await client.patch(
                    f"/api/meeting-management/{own}/minutes",
                    json={
                        "version": 1,
                        "title": "旧版本",
                        "body": "不能覆盖",
                        "meetingType": "内部会议",
                    },
                )
            ).status_code == 409
            decision = await client.patch(
                f"/api/meeting-management/{own}/knowledge/knowledge-1",
                json={
                    "version": 2,
                    "action": "PROCESSING",
                    "reason": "等待维护人员核对",
                    "draftContent": "知识草稿正文",
                },
            )
            assert decision.status_code == 200, decision.text
            assert decision.json()["meeting"]["result"]["followup"]["knowledgeSuggestions"][0]["status"] == "PROCESSING"
            snapshot = await client.get(f"/api/meeting-management/{own}/runs/{own}", params={"version": 1})
            assert snapshot.json()["meeting"]["result"]["body"] == "测试纪要 正式记录"
            pending = await client.get("/api/meeting-management/queue/KNOWLEDGE", params={"status": "PROCESSING"})
            assert pending.json()["total"] == 1
            assert pending.json()["items"][0]["draftContent"] == "知识草稿正文"
            # Import a historical successful rerun: durable work remains editable and exportable.
            async with pg_manager.get_async_session_context() as db:
                previous = await db.get(MeetingRecord, own)
                question = ProductMessage(conversation_id=conversations[0], role="USER", content="重新分析")
                answer = ProductMessage(
                    conversation_id=conversations[0], role="ASSISTANT", content="新纪要", answer_status="INSUFFICIENT"
                )
                db.add_all([question, answer])
                await db.flush()
                new_id = f"{prefix}-rerun"
                mids.append(new_id)
                new_result = {
                    "title": "重新分析后的会议",
                    "body": "新的纪要正文",
                    "followup": {
                        "tasks": [],
                        "knowledgeSuggestions": [],
                    },
                }
                db.add(
                    MeetingRecord(
                        id=new_id,
                        conversation_id=conversations[0],
                        message_id=answer.message_id,
                        user_message_id=question.message_id,
                        request_id=new_id,
                        state="completed",
                        version=1,
                        input={"parentId": own},
                        result=new_result,
                        sources=previous.sources,
                    )
                )
                await db.flush()
                db.add(MeetingRevision(meeting_id=new_id, version=1, result=new_result))
            imported = await client.get(f"/api/meeting-management/{own}")
            assert imported.status_code == 200, imported.text
            current = imported.json()["current"]
            assert current["id"] == new_id
            assert current["version"] == 2  # New migration revision, not overwritten history.
            assert current["result"]["followup"]["tasks"][0]["delivery"]["feishuTaskId"] == "never-send-this"
            saved = await client.patch(
                f"/api/meeting-management/{own}/knowledge/knowledge-1",
                json={
                    "version": 2,
                    "action": "DEFERRED",
                    "reason": "补充资料后处理",
                },
            )
            assert saved.status_code == 200, saved.text
            historical = await client.get(f"/api/meeting-management/{own}/runs/{new_id}", params={"version": 1})
            assert historical.json()["meeting"]["result"]["followup"]["tasks"] == []
            client.headers["Authorization"] = f"Bearer {token(ids[1])}"
            assert (await client.get(f"/api/meeting-management/{mids[0]}")).status_code == 404
            assert (await client.get("/api/meeting-management")).json()["total"] == 1
            client.headers["Authorization"] = f"Bearer {token(ids[2])}"
            assert (await client.get("/api/meeting-management")).status_code == 403
        async with pg_manager.get_async_session_context() as db:
            assert (
                await db.scalar(
                    select(ProductConversation.status).where(ProductConversation.conversation_id == conversations[0])
                )
            ) == "ARCHIVED"
    finally:
        async with pg_manager.get_async_session_context() as db:
            await db.execute(delete(MeetingRevision).where(MeetingRevision.meeting_id.in_(mids)))
            await db.execute(delete(MeetingRecord).where(MeetingRecord.id.in_(mids)))
            await db.execute(delete(ProductMessage).where(ProductMessage.conversation_id.in_(conversations)))
            await db.execute(delete(ProductConversation).where(ProductConversation.conversation_id.in_(conversations)))
            await db.execute(delete(User).where(User.id.in_(ids)))
            if "dept_id" in locals():
                await db.execute(delete(Department).where(Department.id == dept_id))
        await pg_manager.close()


@pytest.mark.asyncio
async def test_management_scope_includes_same_feishu_tenant_only():
    if os.getenv("MEETING_ISOLATED_TEST") != "1":
        pytest.skip("Requires isolated meeting test API")
    pg_manager.initialize()
    prefix = f"tenant-scope-{uuid4().hex[:8]}"
    ids, conversations, meetings = [], [], []
    try:
        async with pg_manager.get_async_session_context() as db:
            dept = Department(name=prefix)
            db.add(dept)
            await db.flush()
            users = []
            for index, tenant in enumerate(("tenant-a", "tenant-a", "tenant-b")):
                user = User(
                    username=f"{prefix}-{index}",
                    uid=f"{prefix}-{index}",
                    password_hash="unused",
                    role="admin",
                    department_id=dept.id,
                )
                db.add(user)
                await db.flush()
                ids.append(user.id)
                users.append(user)
                db.add(
                    FeishuUserBinding(
                        user_id=user.id,
                        feishu_open_id=f"{prefix}-open-{index}",
                        feishu_user_id=f"{prefix}-user-{index}",
                        tenant_key=tenant,
                        display_name=f"成员 {index}",
                        authorization_status="ACTIVE",
                    )
                )
                conversation = ProductConversation(owner_user_id=user.id, title=f"会议 {index}", status="ACTIVE")
                db.add(conversation)
                await db.flush()
                conversations.append(conversation.conversation_id)
                question = ProductMessage(conversation_id=conversation.conversation_id, role="USER", content="会议")
                answer = ProductMessage(
                    conversation_id=conversation.conversation_id,
                    role="ASSISTANT",
                    content="纪要",
                    answer_status="SUPPORTED",
                )
                db.add_all([question, answer])
                await db.flush()
                meeting_id = f"{prefix}-{index}"
                result = {
                    "title": f"会议 {index}",
                    "body": "正文",
                    "followup": {"tasks": [], "knowledgeSuggestions": []},
                }
                db.add(
                    MeetingRecord(
                        id=meeting_id,
                        conversation_id=conversation.conversation_id,
                        message_id=answer.message_id,
                        user_message_id=question.message_id,
                        request_id=meeting_id,
                        state="completed",
                        version=1,
                        input={},
                        result=result,
                        sources=[],
                    )
                )
                db.add(MeetingRevision(meeting_id=meeting_id, version=1, result=result))
                meetings.append(meeting_id)

        def token(uid):
            return AuthUtils.create_access_token({"sub": str(uid)})

        async with httpx.AsyncClient(
            base_url=os.getenv("TEST_BASE_URL", "http://localhost:5051"),
            timeout=60,
            headers={"Authorization": f"Bearer {token(ids[0])}"},
        ) as client:
            response = await client.get("/api/meeting-management")
            assert response.status_code == 200, response.text
            assert response.json()["scope"] == "TENANT"
            assert response.json()["total"] == 2
            assert {row["ownerDisplayName"] for row in response.json()["items"]} == {"成员 0", "成员 1"}
            assert (await client.get(f"/api/meeting-management/{meetings[1]}")).status_code == 200
            assert (await client.get(f"/api/meeting-management/{meetings[2]}")).status_code == 404
    finally:
        async with pg_manager.get_async_session_context() as db:
            await db.execute(delete(MeetingRevision).where(MeetingRevision.meeting_id.in_(meetings)))
            await db.execute(delete(MeetingRecord).where(MeetingRecord.id.in_(meetings)))
            await db.execute(delete(ProductMessage).where(ProductMessage.conversation_id.in_(conversations)))
            await db.execute(delete(ProductConversation).where(ProductConversation.conversation_id.in_(conversations)))
            await db.execute(delete(FeishuUserBinding).where(FeishuUserBinding.user_id.in_(ids)))
            await db.execute(delete(User).where(User.id.in_(ids)))
            if "dept" in locals():
                await db.execute(delete(Department).where(Department.id == dept.id))
        await pg_manager.close()
