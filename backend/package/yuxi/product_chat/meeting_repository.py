"""Owned meeting jobs and optimistic, immutable result revisions."""

from uuid import uuid4

from sqlalchemy import select

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

    async def list_owned(self, user_id):
        rows = await self.db.scalars(
            select(MeetingRecord)
            .join(
                ProductConversation,
                MeetingRecord.conversation_id == ProductConversation.conversation_id,
            )
            .where(ProductConversation.owner_user_id == user_id, MeetingRecord.result.is_not(None))
            .order_by(MeetingRecord.created_at.desc())
            .limit(100)
        )
        return list(rows)

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
        return record

    async def save_result(self, record, result, *, editor=None):
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
        "result": record.result,
        "error": record.error,
        "version": record.version,
        "updatedAt": utc_isoformat(record.updated_at.replace(tzinfo=UTC)),
        "createdAt": utc_isoformat(record.created_at.replace(tzinfo=UTC)),
        "sources": record.sources if include_sources else [],
        "inputContent": record.input.get("content", ""),
    }
