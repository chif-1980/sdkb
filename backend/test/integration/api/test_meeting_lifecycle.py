"""Real HTTP tests with disposable identities; run against the isolated meeting test stack."""

import asyncio
import io
import os
from uuid import uuid4

import httpx
import pytest
from docx import Document
from sqlalchemy import delete, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Conversation, Department, User
from yuxi.storage.postgres.models_product import MeetingRecord, MeetingRevision, ProductConversation, ProductMessage
from yuxi.utils.auth_utils import AuthUtils

pytestmark = pytest.mark.integration


@pytest.fixture
async def meeting_clients():
    if os.getenv("MEETING_ISOLATED_TEST") != "1":
        pytest.skip("Requires an isolated API/worker test queue (MEETING_ISOLATED_TEST=1)")
    pg_manager.initialize()
    prefix = f"meeting-test-{uuid4().hex[:10]}"
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
    clients = [
        httpx.AsyncClient(
            base_url=os.getenv("TEST_BASE_URL", "http://localhost:5050"),
            timeout=60,
            cookies={
                "enterprise_assistant_session": AuthUtils.create_access_token(
                    {"sub": str(uid), "token_kind": "enterprise_assistant"}
                )
            },
        )
        for uid in ids
    ]
    try:
        yield clients
    finally:
        async with pg_manager.get_async_session_context() as db:
            active = list(
                await db.scalars(
                    select(MeetingRecord.id).where(
                        MeetingRecord.conversation_id.in_(
                            select(ProductConversation.conversation_id).where(
                                ProductConversation.owner_user_id.in_(ids)
                            )
                        ),
                        MeetingRecord.state.in_(["pending", "running"]),
                    )
                )
            )
        for mid in active:
            for client in clients:
                await client.post(f"/api/chat/runs/{mid}/cancel")
        if active:
            await asyncio.sleep(2)  # Worker observes cancellation once per second before fixture deletion.
        for client in clients:
            await client.aclose()
        async with pg_manager.get_async_session_context() as db:
            conversations = select(ProductConversation.conversation_id).where(
                ProductConversation.owner_user_id.in_(ids)
            )
            records = select(MeetingRecord.id).where(MeetingRecord.conversation_id.in_(conversations))
            await db.execute(delete(MeetingRevision).where(MeetingRevision.meeting_id.in_(records)))
            await db.execute(delete(MeetingRecord).where(MeetingRecord.conversation_id.in_(conversations)))
            await db.execute(delete(ProductMessage).where(ProductMessage.conversation_id.in_(conversations)))
            await db.execute(delete(ProductConversation).where(ProductConversation.owner_user_id.in_(ids)))
            for thread in await db.scalars(
                select(Conversation).where(Conversation.uid.in_(select(User.uid).where(User.id.in_(ids))))
            ):
                await db.delete(thread)
            await db.execute(delete(User).where(User.id.in_(ids)))
            await db.execute(delete(Department).where(Department.id == department_id))
        await pg_manager.close()


async def submit(client, text):
    response = await client.post("/api/chat/conversations", json={"title": "会议技能验收"})
    assert response.status_code == 201, response.text
    cid = response.json()["conversation"]["id"]
    response = await client.post(
        f"/api/chat/conversations/{cid}/messages",
        json={"content": text, "skillId": "MEETING_ANALYSIS", "requestId": uuid4().hex},
    )
    assert response.status_code == 202, response.text
    return cid, response.json()["run"]["runId"]


async def wait_terminal(client, mid, seconds=900):
    for _ in range(seconds):
        response = await client.get(f"/api/chat/meetings/{mid}")
        assert response.status_code == 200, response.text
        record = response.json()["meeting"]
        if record["state"] in {"failed", "completed", "cancelled"}:
            return record
        await asyncio.sleep(1)
    pytest.fail(f"Meeting did not finish; last progress: {record['progress']}")


async def test_failed_source_visible_retry_and_cross_user_isolation(meeting_clients):
    owner, stranger = meeting_clients
    cid, mid = await submit(owner, "@会议纪要 http://127.0.0.1/private")
    record = await wait_terminal(owner, mid)
    assert record["state"] == "failed" and record["error"]["code"] == "UNSAFE_ADDRESS"
    for path in [f"/api/chat/meetings/{mid}", f"/api/chat/runs/{mid}", f"/api/chat/meetings/{mid}/export?version=1"]:
        assert (await stranger.get(path)).status_code == 404
    assert (await stranger.post(f"/api/chat/meetings/{mid}/retry")).status_code == 404
    retry = await owner.post(f"/api/chat/meetings/{mid}/retry")
    assert retry.status_code == 200
    retry_id = retry.json()["runId"]
    assert retry_id != mid
    await owner.post(f"/api/chat/runs/{retry_id}/cancel")
    assert (await wait_terminal(owner, retry_id))["state"] in {"cancelled", "failed"}
    messages = (await owner.get(f"/api/chat/conversations/{cid}")).json()["messages"]
    assert len([m for m in messages if m.get("meeting")]) == 2


async def test_saved_revision_export_and_retry_preserve_previous_result(meeting_clients):
    owner, stranger = meeting_clients
    text = (
        "@会议纪要\n张经理：今天只是讨论备选方向，还没有决定采购。\n"
        "李工程师：建议下周测试，但负责人和具体期限尚未确定。\n张经理：同意继续收集需求，采购需另行审批。"
    )
    cid, mid = await submit(owner, text)
    record = await wait_terminal(owner, mid)
    assert record["state"] == "completed", record.get("error")
    assert record["result"]["coverage"]["processed"] == record["result"]["coverage"]["total"]
    assert "待确认" in record["result"]["body"]
    patch = {
        "version": record["version"],
        "title": "人工修订版本",
        "meetingType": "内部管理",
        "body": "负责人待确认；采购尚未决定。[S1-P1]",
    }
    assert (await stranger.patch(f"/api/chat/meetings/{mid}", json=patch)).status_code == 404
    edited = await owner.patch(f"/api/chat/meetings/{mid}", json=patch)
    assert edited.status_code == 200, edited.text
    version = edited.json()["meeting"]["version"]
    assert version == record["version"] + 1
    assert (await owner.patch(f"/api/chat/meetings/{mid}", json=patch)).status_code == 409
    exported = await owner.get(f"/api/chat/meetings/{mid}/export?version={version}")
    assert exported.status_code == 200
    doc = Document(io.BytesIO(exported.content))
    assert patch["body"] in "\n".join(p.text for p in doc.paragraphs)
    retry = await owner.post(f"/api/chat/meetings/{mid}/retry")
    assert retry.status_code == 200
    await owner.post(f"/api/chat/runs/{retry.json()['runId']}/cancel")
    preserved = (await owner.get(f"/api/chat/meetings/{mid}")).json()
    assert preserved["meeting"]["result"]["body"] == patch["body"]
    assert len(preserved["revisions"]) == 2


async def test_attachment_input_and_explicit_history_are_owned_and_saved(meeting_clients):
    owner, stranger = meeting_clients
    response = await owner.post("/api/chat/conversations", json={"title": "附件与历史会议验收"})
    cid = response.json()["conversation"]["id"]
    transcript = "王经理：本次会议决定继续收集需求，采购尚未审批。\n李工程师：测试只是建议，具体负责人和日期还未确定。"
    upload = await owner.post(
        f"/api/chat/conversations/{cid}/attachments", files={"file": ("会议.txt", transcript.encode(), "text/plain")}
    )
    assert upload.status_code == 201, upload.text
    attachment_id = upload.json()["attachment"]["id"]
    response = await owner.post(
        f"/api/chat/conversations/{cid}/messages",
        json={
            "content": "@会议纪要 整理上传的会议文字",
            "skillId": "MEETING_ANALYSIS",
            "attachmentIds": [attachment_id],
        },
    )
    assert response.status_code == 202, response.text
    mid = response.json()["run"]["runId"]
    first = await wait_terminal(owner, mid)
    assert first["state"] == "completed", first.get("error")
    assert "王经理" in first["sources"][0]["paragraphs"][0]["text"]
    other = (await stranger.post("/api/chat/conversations", json={"title": "权限检查"})).json()["conversation"]["id"]
    forbidden = await stranger.post(
        f"/api/chat/conversations/{other}/messages",
        json={
            "content": "@会议纪要 整理会议",
            "skillId": "MEETING_ANALYSIS",
            "historyMeetingIds": [mid],
        },
    )
    assert forbidden.status_code == 404
    following = await owner.post(
        f"/api/chat/conversations/{cid}/messages",
        json={
            "content": (
                "@会议纪要 张经理：本次会议仍未决定采购，继续讨论需求。"
                "李工程师：建议先收集使用场景，再讨论预算，负责人和时间仍待确认。"
            ),
            "skillId": "MEETING_ANALYSIS",
            "historyMeetingIds": [mid],
        },
    )
    assert following.status_code == 202, following.text
    second = await wait_terminal(owner, following.json()["run"]["runId"])
    assert second["state"] == "completed", second.get("error")
    assert second["result"]["selectedHistory"][0]["body"] == first["result"]["body"]
    assert second["result"]["selectedHistory"][0]["label"] == "H1"


async def test_history_picker_groups_searches_paginates_and_isolates_owners(meeting_clients):
    owner, stranger = meeting_clients
    response = await owner.post("/api/chat/conversations", json={"title": "历史选择验收"})
    assert response.status_code == 201
    cid = response.json()["conversation"]["id"]
    ids = []
    async with pg_manager.get_async_session_context() as db:
        for index in range(105):
            question = ProductMessage(conversation_id=cid, role="USER", content="test")
            answer = ProductMessage(conversation_id=cid, role="ASSISTANT", content="test", answer_status="INSUFFICIENT")
            db.add_all([question, answer])
            await db.flush()
            mid = f"MT-{uuid4().hex}"
            ids.append(mid)
            db.add(
                MeetingRecord(
                    id=mid,
                    conversation_id=cid,
                    message_id=answer.message_id,
                    user_message_id=question.message_id,
                    request_id=uuid4().hex,
                    state="completed",
                    input={"parentId": ids[0]} if index == 104 else {},
                    sources=[{"platform": "文字资料", "paragraphs": [{"text": "private"}]}],
                    result={"title": "同名会议", "body": "独有验收关键词" if index == 0 else f"会议正文 {index}"},
                )
            )
    response = await owner.get("/api/chat/meetings", params={"limit": 20})
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["total"] == 104
    assert len(first["meetings"]) == 20 and first["nextOffset"] == 20
    assert first["meetings"][0]["id"] == ids[-1]
    assert first["meetings"][0]["versionCount"] == 2
    assert "paragraphs" not in first["meetings"][0]
    tail = (await owner.get("/api/chat/meetings", params={"offset": 100, "limit": 20})).json()
    assert len(tail["meetings"]) == 4 and tail["nextOffset"] is None
    search = (await owner.get("/api/chat/meetings", params={"q": "独有验收关键词"})).json()
    assert search["total"] == 1 and search["meetings"][0]["id"] == ids[-1]
    versions = (await owner.get("/api/chat/meetings", params={"groupId": ids[-1], "limit": 1})).json()
    assert versions["total"] == 2 and versions["nextOffset"] == 1
    older = (await owner.get("/api/chat/meetings", params={"groupId": ids[-1], "offset": 1})).json()
    assert older["meetings"][0]["id"] == ids[0]
    assert older["meetings"][0]["preview"] == "独有验收关键词"
    assert (await stranger.get("/api/chat/meetings", params={"groupId": ids[-1]})).json()["meetings"] == []
    assert (await stranger.get("/api/chat/meetings", params={"q": "独有验收关键词"})).json()["meetings"] == []
    assert (await owner.get("/api/chat/meetings", params={"offset": -1})).status_code == 422
