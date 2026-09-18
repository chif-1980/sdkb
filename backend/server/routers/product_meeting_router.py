"""Meeting API; delegates ownership, revisions and execution to product services."""

import asyncio
import json
from datetime import date
import re
from uuid import uuid4
from urllib.parse import quote
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from server.routers.product_api_route import ProductApiRoute
from server.utils.auth_middleware import get_product_user
from yuxi.product_chat.meeting_repository import MeetingRepository, serialize_meeting
from yuxi.product_chat.meeting_service import enqueue_meeting
from yuxi.product_chat.meeting_directory import load_meeting_directory
from yuxi.product_chat.repository import ProductChatNotFoundError
from yuxi.product_chat.schemas import SendMessageRequest
from yuxi.services.run_queue_service import publish_cancel_signal
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_product import (
    ProductConversation,
    MeetingRecord,
    MeetingRevision,
    ProductMessage,
)
from yuxi.utils.datetime_utils import utc_now_naive

product_meeting = APIRouter(route_class=ProductApiRoute)


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def require_meeting(db, meeting_id, user, **kwargs):
    try:
        return await MeetingRepository(db).require(meeting_id, user.id, **kwargs)
    except ProductChatNotFoundError:
        raise HTTPException(404, "会议不存在或无权访问") from None


class MeetingEdit(BaseModel):
    version: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=200000)
    title: str = Field(min_length=1, max_length=512)
    meetingType: str = Field(max_length=80)


class FollowupTaskEdit(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=1000)
    assigneeUserId: str | None = Field(default=None, max_length=32)
    assigneeFeishuUserId: str | None = Field(default=None, max_length=128)
    dueDate: date | None = None
    status: Literal["OPEN", "IN_PROGRESS", "DONE"] = "OPEN"


class MeetingFollowupEdit(BaseModel):
    version: int = Field(ge=1)
    tasks: list[FollowupTaskEdit] = Field(default_factory=list, max_length=100)


async def start_meeting(conversation_id, request, user):
    try:
        async with pg_manager.get_async_session_context() as db:
            record = await MeetingRepository(db).create(conversation_id, user, request)
            record_id = record.id
        try:
            await enqueue_meeting(record_id)
        except Exception:
            # A committed placeholder must never stay pending when Redis is unavailable.
            async with pg_manager.get_async_session_context() as db:
                stored = await db.get(MeetingRecord, record_id)
                if stored.state == "pending":
                    stored.state = "failed"
                    stored.error = {"code": "QUEUE_UNAVAILABLE", "message": "后台队列暂时不可用，请重试。"}
                    stored.progress = {**stored.progress, "status": "FAILED", "message": stored.error["message"]}
            raise HTTPException(503, "后台队列暂时不可用，请重试。") from None
        return record_id
    except ProductChatNotFoundError:
        raise HTTPException(404, "会话或会议不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


async def meeting_exchange(meeting_id, user):
    from server.routers.product_chat_router import get_conversation

    async with pg_manager.get_async_session_context() as db:
        record = await require_meeting(db, meeting_id, user)
        conversation_id, question_id, answer_id = record.conversation_id, record.user_message_id, record.message_id
    detail = await get_conversation(conversation_id, user)
    messages = {m.id: m for m in detail.messages}
    return {
        "conversation": detail.conversation.model_dump(by_alias=True),
        "userMessage": messages[question_id].model_dump(by_alias=True),
        "assistantMessage": messages[answer_id].model_dump(by_alias=True),
    }


async def meeting_stream(meeting_id, user):
    async with pg_manager.get_async_session_context() as db:
        await require_meeting(db, meeting_id, user)

    async def events():
        yield sse("run_started", {"runId": meeting_id, "skillId": "MEETING_ANALYSIS"})
        previous = None
        while True:
            async with pg_manager.get_async_session_context() as db:
                record = await require_meeting(db, meeting_id, user)
                data = serialize_meeting(record, include_sources=False)
            if data["progress"] != previous:
                previous = data["progress"]
                yield sse("progress", previous)
            if data["state"] in {"completed", "failed", "cancelled"}:
                yield sse("complete", await meeting_exchange(meeting_id, user))
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@product_meeting.get("/chat/meetings")
async def list_meetings(user: User = Depends(get_product_user)):
    async with pg_manager.get_async_session_context() as db:
        rows = await MeetingRepository(db).list_owned(user.id)
        return {
            "meetings": [
                {
                    "id": r.id,
                    "conversationId": r.conversation_id,
                    "title": (r.result or {}).get("title", "会议纪要"),
                    "createdAt": serialize_meeting(r, include_sources=False)["createdAt"],
                }
                for r in rows
            ]
        }


@product_meeting.get("/chat/meeting-activity")
async def meeting_activity(user: User = Depends(get_product_user)):
    async with pg_manager.get_async_session_context() as db:
        rows = await db.execute(
            select(MeetingRecord, ProductConversation.title)
            .join(
                ProductConversation,
                ProductConversation.conversation_id == MeetingRecord.conversation_id,
            )
            .where(ProductConversation.owner_user_id == user.id)
            .order_by(MeetingRecord.updated_at.desc())
            .limit(100)
        )
        return {
            "tasks": [
                {
                    "id": r.id,
                    "conversationId": r.conversation_id,
                    "title": (r.result or {}).get("title") or title or "会议纪要",
                    "state": r.state,
                    "progress": r.progress,
                    "updatedAt": serialize_meeting(r, include_sources=False)["updatedAt"],
                }
                for r, title in rows
            ]
        }


@product_meeting.get("/chat/meetings/{meeting_id}")
async def get_meeting(meeting_id: str, user: User = Depends(get_product_user)):
    async with pg_manager.get_async_session_context() as db:
        record = await require_meeting(db, meeting_id, user)
        revisions = await db.scalars(
            select(MeetingRevision)
            .where(MeetingRevision.meeting_id == meeting_id)
            .order_by(MeetingRevision.version.desc())
        )
        return {
            "meeting": serialize_meeting(record),
            "revisions": [{"version": row.version, "result": row.result} for row in revisions],
        }


@product_meeting.get("/chat/meetings/{meeting_id}/followup-directory")
async def meeting_followup_directory(meeting_id: str, user: User = Depends(get_product_user)):
    async with pg_manager.get_async_session_context() as db:
        await require_meeting(db, meeting_id, user)
        return await load_meeting_directory(db, user.id)


@product_meeting.patch("/chat/meetings/{meeting_id}/followup")
async def edit_meeting_followup(
    meeting_id: str,
    patch: MeetingFollowupEdit,
    user: User = Depends(get_product_user),
):
    async with pg_manager.get_async_session_context() as db:
        record = await require_meeting(db, meeting_id, user, lock=True, writable=True)
        if record.state != "completed" or record.version != patch.version or not record.result:
            raise HTTPException(409, "会议跟进版本已变化，请重新加载后再修改。")
        directory = (
            await load_meeting_directory(db, user.id)
            if any(t.assigneeUserId or t.assigneeFeishuUserId for t in patch.tasks)
            else {"users": []}
        )
        directory_by_id = {item["userId"]: item for item in directory["users"] if item["userId"]}
        directory_by_feishu = {item["feishuUserId"]: item for item in directory["users"]}
        existing = record.result.get("followup") or {}
        existing_tasks = {str(item.get("id")): item for item in existing.get("tasks", []) if isinstance(item, dict)}
        updated_tasks = []
        for task in patch.tasks:
            previous = existing_tasks.get(task.id)
            if previous is None:
                raise HTTPException(422, f"未知的会议任务：{task.id}")
            assignee = None
            if task.assigneeUserId or task.assigneeFeishuUserId:
                assignee = (
                    directory_by_feishu.get(task.assigneeFeishuUserId)
                    if task.assigneeFeishuUserId
                    else directory_by_id.get(task.assigneeUserId)
                )
                if assignee is None:
                    raise HTTPException(422, "任务负责人必须来自当前飞书企业目录。")
                assignee = {
                    "userId": assignee["userId"],
                    "feishuUserId": assignee["feishuUserId"],
                    "displayName": assignee["displayName"],
                }
            updated_tasks.append(
                {
                    "id": task.id,
                    "title": task.title.strip(),
                    "assignee": assignee,
                    "assigneeSuggestion": previous.get("assigneeSuggestion"),
                    "dueDate": task.dueDate.isoformat() if task.dueDate else None,
                    "dueDateSuggestion": previous.get("dueDateSuggestion")
                    or (
                        previous.get("dueDate")
                        if previous.get("dueDate") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", previous["dueDate"])
                        else None
                    ),
                    "status": task.status,
                    "sourceRefs": list(previous.get("sourceRefs") or []),
                }
            )
        followup = {
            "coordinator": existing.get("coordinator") or {"userId": str(user.id), "displayName": user.username},
            "tasks": updated_tasks,
            "knowledgeSuggestions": list(existing.get("knowledgeSuggestions") or []),
        }
        await MeetingRepository(db).save_result(record, {**record.result, "followup": followup}, editor=user.id)
        return {"meeting": serialize_meeting(record)}


@product_meeting.patch("/chat/meetings/{meeting_id}")
async def edit_meeting(meeting_id: str, patch: MeetingEdit, user: User = Depends(get_product_user)):
    async with pg_manager.get_async_session_context() as db:
        record = await require_meeting(db, meeting_id, user, lock=True, writable=True)
        if record.state != "completed" or record.version != patch.version:
            raise HTTPException(409, "纪要版本已变化，请重新加载后再修改。")
        await MeetingRepository(db).save_result(
            record,
            {
                **record.result,
                "body": patch.body,
                "title": patch.title,
                "meetingType": patch.meetingType,
            },
            editor=user.id,
        )
        return {"meeting": serialize_meeting(record)}


@product_meeting.post("/chat/meetings/{meeting_id}/retry")
async def retry_meeting(meeting_id: str, user: User = Depends(get_product_user)):
    async with pg_manager.get_async_session_context() as db:
        record = await require_meeting(db, meeting_id, user, writable=True)
        request = SendMessageRequest(
            content=record.input["content"],
            requestId=uuid4().hex,
            skillId="MEETING_ANALYSIS",
            meetingId=record.id,
            attachmentIds=record.input.get("attachmentIds", []),
            historyMeetingIds=record.input.get("historyMeetingIds", []),
        )
        conversation_id = record.conversation_id
    new_id = await start_meeting(conversation_id, request, user)
    return {"runId": new_id, "conversationId": conversation_id}


async def cancel_meeting(meeting_id, user):
    async with pg_manager.get_async_session_context() as db:
        record = await require_meeting(db, meeting_id, user, lock=True, writable=True)
        if record.state in {"pending", "running"}:
            record.state = "cancelled"
            record.updated_at = utc_now_naive()
            record.progress = {**record.progress, "message": "已取消", "status": "INTERRUPTED"}
            message = await db.scalar(select(ProductMessage).where(ProductMessage.message_id == record.message_id))
            message.content = "会议任务已取消，可重试。"
    await publish_cancel_signal(meeting_id)
    return {"run": {"runId": meeting_id, "status": record.state}}


@product_meeting.get("/chat/meetings/{meeting_id}/export")
async def export_meeting(meeting_id: str, version: int, user: User = Depends(get_product_user)):
    from yuxi.product_chat.meeting_export import export_docx

    async with pg_manager.get_async_session_context() as db:
        record = await require_meeting(db, meeting_id, user)
        if not record.result or record.version != version:
            raise HTTPException(409, "导出版本已变化，请等待保存完成后重试。")
        data = await asyncio.to_thread(export_docx, record.result, record.sources, record.version)
    filename = quote(f"{record.result['title'][:80]}-v{version}.docx", safe="")
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
