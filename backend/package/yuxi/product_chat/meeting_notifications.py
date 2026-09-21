"""Durable meeting completion notices, independent of governance/task notices."""

import json
import os
from datetime import UTC, timedelta
from urllib.parse import urlencode, urlsplit
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import or_, select

from yuxi.integrations.feishu.client import FeishuClient
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_knowledge import FeishuNotificationDelivery
from yuxi.storage.postgres.models_product import (
    FeishuUserBinding, ManagedMeeting, ManagedMeetingRun, MeetingRecord, ProductConversation,
)
from yuxi.utils.datetime_utils import utc_now_naive

OBJECT_TYPE = "ASSISTANT_MEETING"


def notifications_enabled():
    return os.getenv("FEISHU_MEETING_COMPLETION_NOTIFICATIONS", "false").lower() == "true"


async def queue_completion(db, record):
    """Called under the meeting row lock in the result transaction; never sends."""
    if not notifications_enabled():
        return
    key = f"assistant-meeting:{record.id}:completed"
    if await db.scalar(select(FeishuNotificationDelivery.id).where(FeishuNotificationDelivery.idempotency_key == key)):
        return
    meeting = await db.scalar(select(ManagedMeeting).join(
        ManagedMeetingRun, ManagedMeetingRun.meeting_id == ManagedMeeting.id
    ).where(ManagedMeetingRun.run_id == record.id))
    binding = await db.scalar(select(FeishuUserBinding).where(
        FeishuUserBinding.user_id == meeting.owner_user_id
    )) if meeting else None
    user = await db.get(User, meeting.owner_user_id) if meeting else None
    redirect = urlsplit(os.getenv("FEISHU_PRODUCT_REDIRECT_URI", ""))
    valid = (binding and user and not user.is_deleted and binding.authorization_status == "ACTIVE"
             and binding.tenant_key == meeting.tenant_key and redirect.scheme == "https" and redirect.netloc)
    payload = {
        "userId": user.id if user else None,
        "openId": binding.feishu_open_id if binding else None,
        "tenantKey": meeting.tenant_key if meeting else None,
        "url": (
            f"{redirect.scheme}://{redirect.netloc}/chat?"
            f"{urlencode({'conversationId': record.conversation_id, 'meetingId': record.id})}"
        ),
    }
    db.add(FeishuNotificationDelivery(
        notification_id=str(uuid5(NAMESPACE_URL, key)), idempotency_key=key,
        recipient_id=user.uid if user else "unavailable", channel="FEISHU", object_type=OBJECT_TYPE,
        object_id=record.id, title=str((record.result or {}).get("title") or "会议纪要")[:512],
        body=json.dumps(payload, ensure_ascii=False), status="PENDING" if valid else "BLOCKED",
        error_message=None if valid else "上传者身份或助手 HTTPS 地址不可用，未发送通知",
    ))
    await db.flush()


async def deliver_pending(db, client, *, now=None, limit=20):
    now = now or utc_now_naive()
    if now.tzinfo:
        now = now.astimezone(UTC).replace(tzinfo=None)
    rows = list(await db.scalars(select(FeishuNotificationDelivery).where(
        FeishuNotificationDelivery.object_type == OBJECT_TYPE,
        FeishuNotificationDelivery.status.in_(["PENDING", "RETRY"]),
        or_(FeishuNotificationDelivery.next_retry_at.is_(None), FeishuNotificationDelivery.next_retry_at <= now),
    ).order_by(FeishuNotificationDelivery.created_at).limit(limit).with_for_update(skip_locked=True)))
    for row in rows:
        # Never retry outside Feishu's one-hour UUID deduplication window, even after a worker crash.
        created = (
            row.created_at.replace(tzinfo=None)
            if not row.created_at.tzinfo
            else row.created_at.astimezone(UTC).replace(tzinfo=None)
        )
        if now - created >= timedelta(minutes=50):
            row.status, row.error_message = "EXPIRED", "通知重试期限已过，请从助手后台任务查看结果"
            continue
        payload = json.loads(row.body)
        binding = await db.scalar(select(FeishuUserBinding).join(User, User.id == FeishuUserBinding.user_id).where(
            User.id == payload["userId"], User.uid == row.recipient_id, User.is_deleted == 0,
            FeishuUserBinding.feishu_open_id == payload["openId"],
            FeishuUserBinding.tenant_key == payload["tenantKey"], FeishuUserBinding.authorization_status == "ACTIVE",
        ))
        record = await db.get(MeetingRecord, row.object_id)
        conversation = await db.scalar(select(ProductConversation).where(
            ProductConversation.conversation_id == record.conversation_id,
            ProductConversation.owner_user_id == payload["userId"],
        )) if record else None
        if not binding or not conversation or record.state != "completed":
            row.status, row.error_message = "BLOCKED", "会议或上传者访问权限已变化，未发送通知"
            continue
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "会议纪要已完成"}, "template": "green"},
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": row.title}},
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "请核对纪要及待办；知识更新建议由维护人员审核。"}],
                },
                {
                    "tag": "action",
                    "actions": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看会议纪要"},
                        "type": "primary",
                        "url": payload["url"],
                    }],
                },
            ],
        }
        try:
            await client.send_card_message(open_id=binding.feishu_open_id, card=card, uuid=row.notification_id)
        except Exception as exc:
            row.retry_count += 1
            row.status = "RETRY" if row.retry_count < 4 else "FAILED"
            row.next_retry_at = now + timedelta(minutes=(1, 5, 15)[min(row.retry_count - 1, 2)])
            row.error_message = f"飞书通知发送失败（{type(exc).__name__}），纪要已保存"
        else:
            row.status, row.delivered_at = "DELIVERED", now
            row.error_message, row.next_retry_at = None, None
    await db.flush()
    return len(rows)


async def deliver_meeting_notifications(ctx):
    if not notifications_enabled():
        return
    client = FeishuClient()
    try:
        async with pg_manager.get_async_session_context() as db:
            await deliver_pending(db, client)
    finally:
        await client.aclose()
