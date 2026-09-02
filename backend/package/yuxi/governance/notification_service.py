from __future__ import annotations

import os
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_knowledge import FeishuNotificationDelivery
from yuxi.utils.datetime_utils import utc_now_naive


def governance_automation_mode() -> str:
    return os.getenv("YUXI_GOVERNANCE_AUTOMATION_MODE", "observe").strip().lower()


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        recipient_id: str,
        channel: str,
        object_type: str,
        object_id: str,
        idempotency_key: str,
        title: str,
        body: str,
    ) -> tuple[FeishuNotificationDelivery, bool]:
        existing = await self.session.scalar(
            select(FeishuNotificationDelivery).where(FeishuNotificationDelivery.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing, False

        now = utc_now_naive()
        normalized_channel = channel.upper()
        in_observe_mode = governance_automation_mode() == "observe"
        notification = FeishuNotificationDelivery(
            notification_id=f"notification_{uuid.uuid4().hex}",
            recipient_id=recipient_id,
            channel=normalized_channel,
            object_type=object_type,
            object_id=object_id,
            idempotency_key=idempotency_key,
            title=title,
            body=body,
            status=("DELIVERED" if normalized_channel == "IN_APP" else "SUPPRESSED" if in_observe_mode else "PENDING"),
            delivered_at=now if normalized_channel == "IN_APP" else None,
            error_message=(
                "飞书通知在观察模式下未发送" if normalized_channel == "FEISHU" and in_observe_mode else None
            ),
        )
        try:
            async with self.session.begin_nested():
                self.session.add(notification)
                await self.session.flush()
            return notification, True
        except IntegrityError:
            existing = await self.session.scalar(
                select(FeishuNotificationDelivery).where(FeishuNotificationDelivery.idempotency_key == idempotency_key)
            )
            if existing is None:
                raise
            return existing, False

    async def notify_admins(
        self,
        *,
        object_type: str,
        object_id: str,
        title: str,
        body: str,
        assignee_id: str | None = None,
        event_key: str | None = None,
        feishu: bool = False,
    ) -> int:
        """Create idempotent in-app notices for the assignee or admin pool."""
        recipients = (
            [assignee_id]
            if assignee_id
            else list(
                await self.session.scalars(
                    select(User.uid).where(User.role.in_({"admin", "superadmin"}), User.is_deleted == 0)
                )
            )
        )
        created = 0
        for recipient_id in dict.fromkeys(item for item in recipients if item):
            key = event_key or object_type.lower()
            _notification, was_created = await self.create(
                recipient_id=str(recipient_id),
                channel="IN_APP",
                object_type=object_type,
                object_id=object_id,
                idempotency_key=f"{key}:{object_id}:{recipient_id}",
                title=title,
                body=body,
            )
            created += int(was_created)
            if feishu:
                await self.create(
                    recipient_id=str(recipient_id),
                    channel="FEISHU",
                    object_type=object_type,
                    object_id=object_id,
                    idempotency_key=f"{key}:{object_id}:{recipient_id}:feishu",
                    title=title,
                    body=body,
                )
        return created

    async def list_for_recipient(
        self,
        recipient_id: str,
        *,
        unread_only: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        statement = select(FeishuNotificationDelivery).where(
            FeishuNotificationDelivery.recipient_id == recipient_id,
            FeishuNotificationDelivery.channel == "IN_APP",
        )
        if unread_only:
            statement = statement.where(FeishuNotificationDelivery.read_at.is_(None))
        notifications = list(
            await self.session.scalars(statement.order_by(FeishuNotificationDelivery.created_at.desc()))
        )
        total = len(notifications)
        offset = (page - 1) * page_size
        return {
            "items": [self._notification_dict(item) for item in notifications[offset : offset + page_size]],
            "total": total,
            "unread": sum(item.read_at is None for item in notifications),
            "page": page,
            "pageSize": page_size,
        }

    async def mark_read(self, notification_id: str, *, recipient_id: str) -> dict:
        notification = await self.session.scalar(
            select(FeishuNotificationDelivery)
            .where(FeishuNotificationDelivery.notification_id == notification_id)
            .with_for_update()
        )
        if notification is None:
            raise LookupError(f"Notification not found: {notification_id}")
        if notification.recipient_id != recipient_id:
            raise PermissionError("Notification does not belong to current user")
        if notification.read_at is None:
            notification.read_at = utc_now_naive()
            await self.session.flush()
        return self._notification_dict(notification)

    @staticmethod
    def _notification_dict(notification: FeishuNotificationDelivery) -> dict:
        return {
            "id": notification.notification_id,
            "channel": notification.channel,
            "objectType": notification.object_type,
            "objectId": notification.object_id,
            "title": notification.title,
            "body": notification.body,
            "status": notification.status,
            "retryCount": notification.retry_count,
            "error": notification.error_message,
            "readAt": notification.read_at.isoformat() if notification.read_at else None,
            "deliveredAt": notification.delivered_at.isoformat() if notification.delivered_at else None,
            "createdAt": notification.created_at.isoformat(),
        }
