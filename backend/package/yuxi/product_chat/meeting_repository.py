"""Owned meeting jobs and optimistic, immutable result revisions."""

from copy import deepcopy
import re
from uuid import uuid4
from urllib.parse import urldefrag

from sqlalchemy import cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH

from sqlalchemy import select

from yuxi.product_chat.meeting_content import meeting_body
from yuxi.product_chat.repository import ProductChatNotFoundError, ProductChatRepository
from yuxi.storage.postgres.models_product import MeetingRecord, MeetingRevision, ProductConversation, ProductMessage
from yuxi.utils.datetime_utils import UTC, utc_isoformat, utc_now_naive


class MeetingRepository:
    def __init__(self, db):
        self.db = db

    async def require(self, meeting_id, user_id, *, lock=False, writable=False):
        query = (
            select(MeetingRecord)
            .join(
                ProductConversation,
                MeetingRecord.conversation_id == ProductConversation.conversation_id,
            )
            .where(MeetingRecord.id == meeting_id, ProductConversation.owner_user_id == user_id)
        )
        if writable:
            query = query.where(ProductConversation.status == "ACTIVE")
        if lock:
            query = query.with_for_update(of=MeetingRecord)
        record = await self.db.scalar(query)
        if record is None:
            raise ProductChatNotFoundError()
        return record

    async def export_materials(self, record, user_id):
        """Resolve retained tasks against their own run before numbering export sources."""
        result, sources = deepcopy(record.result), deepcopy(record.sources or [])
        offsets = {record.id: 0}
        for task in (result.get("followup") or {}).get("tasks", []):
            source_id = task.get("sourceMeetingId") or record.id
            if source_id not in offsets:
                original = await self.require(source_id, user_id)
                if original.conversation_id != record.conversation_id:
                    raise ProductChatNotFoundError()
                if original.sources == record.sources:
                    offsets[source_id] = 0
                else:
                    offsets[source_id] = len(sources)
                    sources.extend(deepcopy(original.sources or []))
            offset = offsets[source_id]
            task["sourceRefs"] = [
                re.sub(r"^S(\d+)-", lambda match: f"S{int(match[1]) + offset}-", ref)
                for ref in task.get("sourceRefs", [])
            ]
        return result, sources

    async def list_history(self, user_id, *, query="", offset=0, limit=20, group_id=None):
        # Only project metadata: never load the long transcripts into the picker.
        title = MeetingRecord.result["title"].as_string()
        body = MeetingRecord.result["body"].as_string()
        rows = (
            (
                await self.db.execute(
                    select(
                        MeetingRecord.id,
                        MeetingRecord.conversation_id,
                        MeetingRecord.state,
                        MeetingRecord.created_at,
                        MeetingRecord.input["parentId"].as_string().label("parent_id"),
                        title.label("title"),
                        MeetingRecord.result.is_not(None).label("has_result"),
                        func.json_array_length(MeetingRecord.sources).label("source_count"),
                        func.jsonb_path_query_array(
                            cast(MeetingRecord.sources, JSONB), cast("$[*].url", JSONPATH)
                        ).label("urls"),
                        func.jsonb_path_query_array(
                            cast(MeetingRecord.sources, JSONB), cast("$[*].platform", JSONPATH)
                        ).label("platforms"),
                        or_(
                            func.lower(title).contains(query.lower(), autoescape=True),
                            func.lower(body).contains(query.lower(), autoescape=True),
                        ).label("matches"),
                    )
                    .join(ProductConversation, MeetingRecord.conversation_id == ProductConversation.conversation_id)
                    .where(ProductConversation.owner_user_id == user_id)
                    .order_by(MeetingRecord.created_at.desc(), MeetingRecord.id.desc())
                )
            )
            .mappings()
            .all()
        )
        groups = group_history_rows(rows)
        if group_id:
            groups = [group for group in groups if any(row["id"] == group_id for row in group)]
            selected = [(row, groups[0]) for row in groups[0]] if groups else []
        else:
            selected = [(group[0], group) for group in groups if not query or any(row["matches"] for row in group)]
        total = len(selected)
        page = selected[offset : offset + limit]
        previews = {}
        if page:
            previews = {
                row.id: row
                for row in (
                    await self.db.execute(
                        select(
                            MeetingRecord.id,
                            func.substr(body, 1, 1400).label("preview"),
                            MeetingRecord.result["meetingDate"].as_string().label("meeting_date"),
                        ).where(MeetingRecord.id.in_([row["id"] for row, _ in page]))
                    )
                ).all()
            }
        return {
            "meetings": [
                {
                    "id": row["id"],
                    "conversationId": row["conversation_id"],
                    "groupId": group[-1]["id"],
                    "title": row["title"] or "会议纪要",
                    "createdAt": utc_isoformat(row["created_at"].replace(tzinfo=UTC)),
                    "meetingDate": previews[row["id"]].meeting_date,
                    "platforms": list(dict.fromkeys(p for p in row["platforms"] if p)),
                    "sourceUrls": list(dict.fromkeys(u for u in row["urls"] if u)),
                    "preview": previews[row["id"]].preview or "",
                    "versionCount": len(group),
                }
                for row, group in page
            ],
            "total": total,
            "nextOffset": offset + len(page) if offset + len(page) < total else None,
        }

    async def active(self, conversation_id):
        return await self.db.scalar(
            select(MeetingRecord)
            .where(
                MeetingRecord.conversation_id == conversation_id,
                MeetingRecord.state.in_(["pending", "running"]),
            )
            .order_by(MeetingRecord.created_at.desc())
            .limit(1)
        )

    async def create(self, conversation_id, user, request):
        conversation = await ProductChatRepository(self.db).require_conversation(conversation_id, user.id)
        if conversation.status != "ACTIVE":
            raise ValueError("已归档会话不能发起会议任务，请先恢复会话。")
        # Serialize concurrent retries within a conversation before checking the idempotency key.
        await self.db.execute(
            select(ProductConversation).where(ProductConversation.id == conversation.id).with_for_update()
        )
        request_id = request.request_id or uuid4().hex
        existing = await self.db.scalar(
            select(MeetingRecord).where(
                MeetingRecord.conversation_id == conversation_id, MeetingRecord.request_id == request_id
            )
        )
        if existing:
            return existing
        if await self.active(conversation_id):
            raise ValueError("当前会话已有会议任务，请等待完成或先取消。")
        parent = None
        if request.meeting_id:
            parent = await self.require(request.meeting_id, user.id)
            if parent.conversation_id != conversation_id:
                raise ProductChatNotFoundError()
        resume_notes = []
        if (
            parent
            and parent.state in {"failed", "cancelled"}
            and parent.sources
            and request.content == parent.input["content"]
            and request.attachment_ids == parent.input.get("attachmentIds", [])
            and request.history_meeting_ids == parent.input.get("historyMeetingIds", [])
        ):
            resume_notes = parent.input.get("chunkNotes", [])
        for history_id in request.history_meeting_ids:
            history = await self.require(history_id, user.id)
            if not history.result:
                raise ValueError("选中的历史会议尚未完成。")
        question = ProductMessage(
            conversation_id=conversation_id,
            role="USER",
            content=request.content,
            request_id=request_id,
            skill_id="MEETING_ANALYSIS",
        )
        answer = ProductMessage(
            conversation_id=conversation_id,
            role="ASSISTANT",
            content="正在读取会议资料",
            answer_status="INSUFFICIENT",
            request_id=request_id,
            skill_id="MEETING_ANALYSIS",
        )
        self.db.add_all([question, answer])
        await self.db.flush()
        record = MeetingRecord(
            id=f"MT-{uuid4().hex}",
            conversation_id=conversation_id,
            message_id=answer.message_id,
            user_message_id=question.message_id,
            request_id=request_id,
            state="pending",
            input={
                "content": request.content,
                "attachmentIds": request.attachment_ids,
                "historyMeetingIds": request.history_meeting_ids,
                "parentId": parent.id if parent else None,
                "previousResult": parent.result if parent else None,
                "chunkNotes": resume_notes,
            },
            sources=parent.sources if parent else [],
            progress={
                "stage": "UNDERSTANDING",
                "message": "等待读取会议资料",
                "completed": 0,
            },
        )
        self.db.add(record)
        conversation.updated_at = utc_now_naive()
        if not conversation.title:
            conversation.title = request.content[:70]
        await self.db.flush()
        from yuxi.product_chat.meeting_management import MeetingManagement

        await MeetingManagement(self.db).attach(record)
        return record

    async def save_result(self, record, result, *, editor=None, audit=None):
        from yuxi.product_chat.meeting_management import MeetingManagement

        result = await MeetingManagement(self.db).project(record, result, editor=editor, audit=audit)
        result = {**result, "body": meeting_body(result)}
        record.version += 1
        record.result = result
        record.updated_at = utc_now_naive()
        self.db.add(MeetingRevision(meeting_id=record.id, version=record.version, result=result, editor_user_id=editor))
        message = await self.db.scalar(select(ProductMessage).where(ProductMessage.message_id == record.message_id))
        message.content = result["body"]
        message.answer_status = "SUPPORTED"
        await self.db.flush()


def serialize_meeting(record, *, include_sources=True):
    return {
        "id": record.id,
        "conversationId": record.conversation_id,
        "state": record.state,
        "progress": record.progress,
        "result": {**record.result, "body": meeting_body(record.result)} if record.result else None,
        "error": record.error,
        "version": record.version,
        "updatedAt": utc_isoformat(record.updated_at.replace(tzinfo=UTC)),
        "createdAt": utc_isoformat(record.created_at.replace(tzinfo=UTC)),
        "sources": record.sources if include_sources else [],
        "inputContent": record.input.get("content", ""),
    }


def group_history_rows(rows):
    """Group owned runs by explicit ancestry or identical source links, never by title."""
    parents = {row["id"]: row["id"] for row in rows}

    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    sources = {}
    for row in rows:
        parent = row["parent_id"]
        if parent in parents:
            parents[root(row["id"])] = root(parent)
        urls = tuple(sorted({urldefrag(url)[0] for url in row["urls"] if url}))
        if urls and len(row["urls"]) == row["source_count"] and all(row["urls"]):
            if urls in sources:
                parents[root(row["id"])] = root(sources[urls])
            else:
                sources[urls] = row["id"]
    groups = {}
    for row in rows:
        if row["state"] == "completed" and row["has_result"]:
            groups.setdefault(root(row["id"]), []).append(row)
    # Rows arrive newest first, including when the latest attempted rerun failed.
    return list(groups.values())
