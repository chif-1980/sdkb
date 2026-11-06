"""Assignment, independent review and closure for both feedback stores."""

from fastapi import HTTPException
from sqlalchemy import select

from yuxi.services.role_permission_service import permission_scope
from yuxi.storage.postgres.models_business import FeedbackCase, MessageFeedback, User
from yuxi.storage.postgres.models_product import ProductConversation, ProductMessage
from yuxi.utils.datetime_utils import utc_now_naive, utc_isoformat


async def feedback_subject(db, key, user, *, lock=False):
    scope = await permission_scope(db, user, "feedback.view")
    if scope == "none":
        raise HTTPException(403, "没有反馈查看权限")
    if key.startswith("product:"):
        query = select(ProductMessage).where(
            ProductMessage.message_id == key.removeprefix("product:"), ProductMessage.feedback_rating.is_not(None)
        )
        message = await db.scalar(query.with_for_update() if lock else query)
        conversation = (
            await db.scalar(
                select(ProductConversation).where(ProductConversation.conversation_id == message.conversation_id)
            )
            if message
            else None
        )
        owner = await db.get(User, conversation.owner_user_id) if conversation else None
        snapshot = (
            {
                "rating": message.feedback_rating,
                "reason": message.feedback_reason_text,
                "reasonType": message.feedback_reason_type,
            }
            if message
            else None
        )
    else:
        if not key.isdecimal() or str(int(key)) != key:
            raise HTTPException(404, "反馈不存在")
        query = select(MessageFeedback).where(MessageFeedback.id == int(key))
        feedback = await db.scalar(query.with_for_update() if lock else query)
        owner = await db.scalar(select(User).where(User.uid == feedback.uid)) if feedback else None
        snapshot = {"rating": feedback.rating, "reason": feedback.reason} if feedback else None
    if (
        snapshot is None
        or (scope == "self" and (not owner or owner.id != user.id))
        or (
            scope == "department" and (not owner or not user.department_id or owner.department_id != user.department_id)
        )
    ):
        raise HTTPException(404, "反馈不存在或不在权限范围内")
    return snapshot


def case_view(case, snapshot=None):
    if not case:
        return {"status": "OPEN", "version": 0, "history": [], "sourceChanged": False}
    return {
        "status": case.status,
        "version": case.version,
        "assigneeId": case.assignee_id,
        "processorId": case.processor_id,
        "reviewerId": case.reviewer_id,
        "resolution": case.resolution,
        "history": case.history,
        "sourceChanged": snapshot is not None and case.snapshot != snapshot,
        "updatedAt": utc_isoformat(case.updated_at),
    }


async def transition_feedback(db, key, user, patch):
    if await permission_scope(db, user, "feedback.manage") != "all":
        raise HTTPException(403, "没有反馈处理权限")
    snapshot = await feedback_subject(db, key, user, lock=True)
    case = await db.get(FeedbackCase, key, populate_existing=True)
    if patch.version != (case.version if case else 0):
        raise HTTPException(409, "反馈已被其他人处理，请刷新后重试")
    if not patch.note.strip():
        raise HTTPException(422, "请填写处理或复核说明")
    if case and case.snapshot != snapshot and patch.action != "REOPEN":
        raise HTTPException(409, "用户已修改反馈，请重新打开工单后处理")
    if not case:
        case = FeedbackCase(feedback_key=key, status="OPEN", version=0, snapshot=snapshot, history=[])
        db.add(case)
    before = case.status
    action = patch.action
    if action == "ASSIGN":
        if before not in {"OPEN", "ASSIGNED", "PROCESSING"}:
            raise HTTPException(409, "当前状态不能重新指派")
        assignee = await db.get(User, patch.assigneeId) if patch.assigneeId else None
        if (
            not assignee
            or assignee.is_deleted
            or assignee.is_login_locked()
            or await permission_scope(db, assignee, "feedback.manage") != "all"
            or await permission_scope(db, assignee, "feedback.view") != "all"
        ):
            raise HTTPException(422, "请选择有反馈查看及处理权限的有效用户")
        case.assignee_id = assignee.id
        case.status = "ASSIGNED"
    elif action in {"START", "SUBMIT_REVIEW"}:
        if case.assignee_id != user.id:
            raise HTTPException(403, "只有被指派的负责人可以提交处理结果")
        if action == "START" and before == "ASSIGNED":
            case.status = "PROCESSING"
        elif action == "SUBMIT_REVIEW" and before == "PROCESSING":
            case.status = "IN_REVIEW"
            case.resolution = patch.note.strip()
            case.processor_id = user.id
            case.reviewer_id = None
        else:
            raise HTTPException(409, "请按当前处理阶段操作")
    elif action in {"APPROVE", "REJECT"}:
        if before != "IN_REVIEW":
            raise HTTPException(409, "只有待复核的反馈可以复核")
        if case.processor_id == user.id:
            raise HTTPException(403, "请由另一位有权限的人员复核")
        case.reviewer_id = user.id
        case.status = "RESOLVED" if action == "APPROVE" else "PROCESSING"
    elif action == "CLOSE":
        if before != "RESOLVED":
            raise HTTPException(409, "复核通过后才能关闭反馈")
        case.status = "CLOSED"
    elif action == "REOPEN":
        if before not in {"CLOSED", "RESOLVED"} and case.snapshot == snapshot:
            raise HTTPException(409, "该反馈仍在处理中")
        case.status = "OPEN"
        case.assignee_id = case.processor_id = case.reviewer_id = None
        case.resolution = None
        case.snapshot = snapshot
    case.version += 1
    case.updated_at = utc_now_naive()
    case.history = [
        *case.history,
        {
            "action": action,
            "from": before,
            "to": case.status,
            "actorId": user.id,
            "actorName": user.username,
            "assigneeId": case.assignee_id,
            "note": patch.note.strip(),
            "at": utc_isoformat(case.updated_at),
        },
    ]
    await db.flush()
    return case_view(case, snapshot)
