"""Live HTTP contract checks using disposable data; no model jobs or external messages."""

import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User, Department
from yuxi.storage.postgres.models_product import MeetingRecord, MeetingRevision, ProductConversation, ProductMessage
from yuxi.utils.auth_utils import AuthUtils

pytestmark = pytest.mark.integration


@pytest.fixture
async def followup_clients():
    pg_manager.initialize()
    prefix = f"followup-test-{uuid4().hex[:10]}"
    async with pg_manager.get_async_session_context() as db:
        department = Department(name=prefix)
        db.add(department)
        await db.flush()
        users = [
            User(
                username=f"{prefix}-{i}", uid=f"{prefix}-{i}", password_hash="not-a-login", department_id=department.id
            )
            for i in range(2)
        ]
        db.add_all(users)
        await db.flush()
        ids, department_id = [u.id for u in users], department.id
        conversation = ProductConversation(owner_user_id=ids[0], title=prefix)
        db.add(conversation)
        await db.flush()
        cid = conversation.conversation_id
        question = ProductMessage(conversation_id=cid, role="USER", content="测试会议")
        answer = ProductMessage(conversation_id=cid, role="ASSISTANT", content="测试结果", answer_status="SUPPORTED")
        db.add_all([question, answer])
        await db.flush()
        mid = f"MT-{uuid4().hex}"
        db.add(
            MeetingRecord(
                id=mid,
                conversation_id=cid,
                message_id=answer.message_id,
                user_message_id=question.message_id,
                request_id=prefix,
                state="completed",
                version=1,
                progress={"message": "完成"},
                sources=[],
                input={},
                result={
                    "title": prefix,
                    "body": "测试结果",
                    "followup": {
                        "coordinator": {"userId": str(ids[0]), "displayName": prefix},
                        "tasks": [
                            {
                                "id": "task-1",
                                "title": "核对资料",
                                "dueDate": None,
                                "assignee": None,
                                "status": "OPEN",
                                "sourceRefs": [],
                            }
                        ],
                        "knowledgeSuggestions": [],
                    },
                },
            )
        )
    clients = [
        httpx.AsyncClient(
            base_url=os.getenv("TEST_BASE_URL", "http://localhost:5050"),
            timeout=30,
            cookies={
                "enterprise_assistant_session": AuthUtils.create_access_token(
                    {"sub": str(uid), "token_kind": "enterprise_assistant"}
                )
            },
        )
        for uid in ids
    ]
    try:
        yield clients, mid
    finally:
        for client in clients:
            await client.aclose()
        async with pg_manager.get_async_session_context() as db:
            await db.execute(delete(MeetingRevision).where(MeetingRevision.meeting_id == mid))
            await db.execute(delete(MeetingRecord).where(MeetingRecord.id == mid))
            await db.execute(delete(ProductMessage).where(ProductMessage.conversation_id == cid))
            await db.execute(delete(ProductConversation).where(ProductConversation.conversation_id == cid))
            await db.execute(delete(User).where(User.id.in_(ids)))
            await db.execute(delete(Department).where(Department.id == department_id))
        await pg_manager.close()


async def test_activity_is_owned_dates_validated_and_revision_saved(followup_clients):
    (owner, stranger), mid = followup_clients
    activity = await owner.get("/api/chat/meeting-activity")
    assert activity.status_code == 200, activity.text
    assert [task["id"] for task in activity.json()["tasks"]] == [mid]
    assert (await stranger.get("/api/chat/meeting-activity")).json()["tasks"] == []
    assert (await stranger.get(f"/api/chat/meetings/{mid}/followup-directory")).status_code == 404
    assert (await owner.get(f"/api/chat/meetings/{mid}/followup-directory")).status_code == 403
    patch = {"version": 1, "tasks": [{"id": "task-1", "title": "核对资料", "dueDate": "2026-02-30"}]}
    assert (await owner.patch(f"/api/chat/meetings/{mid}/followup", json=patch)).status_code == 422
    patch["tasks"][0]["dueDate"] = "2026-09-30"
    assert (await stranger.patch(f"/api/chat/meetings/{mid}/followup", json=patch)).status_code == 404
    saved = await owner.patch(f"/api/chat/meetings/{mid}/followup", json=patch)
    assert saved.status_code == 200, saved.text
    assert saved.json()["meeting"]["version"] == 2
    assert saved.json()["meeting"]["result"]["followup"]["tasks"][0]["dueDate"] == "2026-09-30"
    assert (await owner.patch(f"/api/chat/meetings/{mid}/followup", json=patch)).status_code == 409
    patch["version"] = 2
    patch["tasks"][0]["dueDate"] = None
    cleared = await owner.patch(f"/api/chat/meetings/{mid}/followup", json=patch)
    assert cleared.json()["meeting"]["result"]["followup"]["tasks"][0]["dueDate"] is None
