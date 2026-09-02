import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.governance.notification_service import (
    NotificationService,
    auto_close_enabled,
    feishu_notifications_enabled,
    governance_automation_mode,
    in_app_notifications_enabled,
)
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import FeishuNotificationDelivery
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_product import FeishuUserBinding


@pytest.mark.asyncio
async def test_notification_is_idempotent_and_read_is_scoped(monkeypatch):
    monkeypatch.setenv("YUXI_GOVERNANCE_AUTOMATION_MODE", "in_app")
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
        assert feishu.error_message == "飞书通知在当前自动化阶段未发送"
    await engine.dispose()


@pytest.mark.asyncio
async def test_observe_mode_records_but_does_not_expose_in_app_notification(monkeypatch):
    monkeypatch.setenv("YUXI_GOVERNANCE_AUTOMATION_MODE", "observe")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        notification, created = await NotificationService(session).create(
            recipient_id="admin-a",
            channel="IN_APP",
            object_type="REVIEW_PACKAGE",
            object_id="package-observe",
            idempotency_key="package-observe:admin-a",
            title="观察模式待办",
            body="只记录，不提醒",
        )

        assert created is True
        assert notification.status == "SUPPRESSED"
        assert (await NotificationService(session).list_for_recipient("admin-a"))["items"] == []
    await engine.dispose()


def test_automation_stages_are_safe_and_sequential(monkeypatch):
    expectations = {
        "observe": (False, False, False),
        "in_app": (True, False, False),
        "feishu": (True, True, False),
        "active": (True, True, True),
    }
    for mode, expected in expectations.items():
        monkeypatch.setenv("YUXI_GOVERNANCE_AUTOMATION_MODE", mode)
        assert (in_app_notifications_enabled(), feishu_notifications_enabled(), auto_close_enabled()) == expected

    monkeypatch.setenv("YUXI_GOVERNANCE_AUTOMATION_MODE", "typo")
    assert governance_automation_mode() == "observe"


@pytest.mark.asyncio
async def test_active_feishu_notification_is_delivered_and_failure_is_recorded(monkeypatch):
    monkeypatch.setenv("YUXI_GOVERNANCE_AUTOMATION_MODE", "active")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class FakeClient:
        def __init__(self, *, fail=False):
            self.fail = fail
            self.sent = []

        async def send_text_message(self, *, open_id, text):
            self.sent.append((open_id, text))
            if self.fail:
                raise RuntimeError("feishu unavailable")

        async def aclose(self):
            return None

    async with factory() as session:
        user = User(
            username="Admin",
            uid="admin-active",
            password_hash="hash",
            role="admin",
            is_deleted=0,
        )
        session.add(user)
        await session.flush()
        session.add(
            FeishuUserBinding(
                user_id=user.id,
                feishu_open_id="ou_admin",
                tenant_key="tenant",
                display_name="Admin",
            )
        )
        client = FakeClient()
        delivered, _ = await NotificationService(session, feishu_client=client).create(
            recipient_id="admin-active",
            channel="FEISHU",
            object_type="REVIEW_PACKAGE",
            object_id="package-active",
            idempotency_key="package-active:admin-active",
            title="高风险审核",
            body="请处理",
        )
        assert delivered.status == "DELIVERED"
        assert client.sent == [("ou_admin", "高风险审核\n请处理")]

        failed, _ = await NotificationService(session, feishu_client=FakeClient(fail=True)).create(
            recipient_id="admin-active",
            channel="FEISHU",
            object_type="REVIEW_PACKAGE",
            object_id="package-failed",
            idempotency_key="package-failed:admin-active",
            title="高风险审核",
            body="请处理",
        )
        assert failed.status == "FAILED"
        assert failed.retry_count == 1
        assert failed.error_message == "feishu unavailable"

        unbound = User(
            username="Unbound",
            uid="admin-unbound",
            password_hash="hash",
            role="admin",
            is_deleted=0,
        )
        session.add(unbound)
        await session.flush()
        missing_binding, _ = await NotificationService(session, feishu_client=client).create(
            recipient_id="admin-unbound",
            channel="FEISHU",
            object_type="REVIEW_PACKAGE",
            object_id="package-unbound",
            idempotency_key="package-unbound:admin-unbound",
            title="高风险审核",
            body="请处理",
        )
        assert missing_binding.status == "FAILED"
        assert missing_binding.retry_count == 1
        assert "No active Feishu binding" in (missing_binding.error_message or "")
    await engine.dispose()


@pytest.mark.asyncio
async def test_suppressed_notifications_activate_when_stage_advances(monkeypatch):
    monkeypatch.setenv("YUXI_GOVERNANCE_AUTOMATION_MODE", "observe")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        notification, _ = await NotificationService(session).create(
            recipient_id="admin-a",
            channel="IN_APP",
            object_type="REVIEW_PACKAGE",
            object_id="package-activate",
            idempotency_key="package-activate:admin-a",
            title="待激活提醒",
            body="阶段开启后可见",
        )
        monkeypatch.setenv("YUXI_GOVERNANCE_AUTOMATION_MODE", "in_app")

        activated = await NotificationService(session).activate_suppressed()

        assert activated == 1
        assert notification.status == "DELIVERED"
        assert len((await NotificationService(session).list_for_recipient("admin-a"))["items"]) == 1
    await engine.dispose()
