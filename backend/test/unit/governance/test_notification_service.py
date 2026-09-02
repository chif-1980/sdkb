import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.governance.notification_service import NotificationService
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import FeishuNotificationDelivery


@pytest.mark.asyncio
async def test_notification_is_idempotent_and_read_is_scoped():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        service = NotificationService(session)
        first, created = await service.create(
            recipient_id="admin-a",
            channel="IN_APP",
            object_type="REVIEW_PACKAGE",
            object_id="package-1",
            idempotency_key="package-1:admin-a",
            title="新审核",
            body="请处理",
        )
        second, duplicate = await service.create(
            recipient_id="admin-a",
            channel="IN_APP",
            object_type="REVIEW_PACKAGE",
            object_id="package-1",
            idempotency_key="package-1:admin-a",
            title="新审核",
            body="请处理",
        )
        await session.commit()

        assert created is True
        assert duplicate is False
        assert first.notification_id == second.notification_id
        assert await session.scalar(
            select(FeishuNotificationDelivery).where(
                FeishuNotificationDelivery.notification_id == first.notification_id
            )
        )
        with pytest.raises(PermissionError):
            await service.mark_read(first.notification_id, recipient_id="admin-b")
        result = await service.mark_read(first.notification_id, recipient_id="admin-a")
        assert result["readAt"] is not None

        feishu, feishu_created = await service.create(
            recipient_id="admin-a",
            channel="FEISHU",
            object_type="REVIEW_PACKAGE",
            object_id="package-1",
            idempotency_key="package-1:admin-a:feishu",
            title="高风险审核",
            body="请处理",
        )
        assert feishu_created is True
        assert feishu.status == "SUPPRESSED"
        assert feishu.error_message == "飞书通知在观察模式下未发送"
    await engine.dispose()
