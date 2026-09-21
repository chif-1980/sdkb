from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.product_chat.meeting_notifications import deliver_pending, queue_completion
from yuxi.storage.postgres.models_business import Base, User
from yuxi.storage.postgres.models_knowledge import FeishuNotificationDelivery
from yuxi.storage.postgres.models_product import (
    FeishuUserBinding, ManagedMeeting, ManagedMeetingRun, MeetingRecord, ProductConversation,
)
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def context(monkeypatch):
    monkeypatch.setenv("FEISHU_MEETING_COMPLETION_NOTIFICATIONS", "true")
    monkeypatch.setenv("FEISHU_PRODUCT_REDIRECT_URI", "https://assistant.example/api/auth/feishu/callback")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        user = User(username="Uploader", uid="employee", password_hash="unused", role="user")
        db.add(user)
        await db.flush()
        binding = FeishuUserBinding(user_id=user.id, feishu_open_id="ou_uploader", feishu_user_id="employee",
                                    tenant_key="tenant-a", display_name="Uploader")
        record = MeetingRecord(id="MT-1", conversation_id="C1", message_id="M1", user_message_id="M2",
                               request_id="R1", state="completed", result={"title": "会议 <测试>"})
        db.add_all([binding, record, ProductConversation(conversation_id="C1", owner_user_id=user.id, title="Meeting"),
                    ManagedMeeting(id="MT-1", owner_user_id=user.id, tenant_key="tenant-a", latest_run_id="MT-1"),
                    ManagedMeetingRun(run_id="MT-1", meeting_id="MT-1")])
        await db.commit()
        yield db, record, binding
    await engine.dispose()


async def test_completion_is_queued_once_and_card_opens_exact_meeting(context):
    db, record, _ = context
    client = AsyncMock()
    await queue_completion(db, record)
    await queue_completion(db, record)
    rows = list(await db.scalars(select(FeishuNotificationDelivery)))
    assert len(rows) == 1
    assert rows[0].status == "PENDING"
    client.send_card_message.assert_not_called()
    await db.commit()
    assert await deliver_pending(db, client) == 1
    await db.commit()
    assert await deliver_pending(db, client) == 0
    args = client.send_card_message.call_args.kwargs
    assert args["open_id"] == "ou_uploader"
    assert args["card"]["elements"][-1]["actions"][0]["url"] == "https://assistant.example/chat?conversationId=C1&meetingId=MT-1"
    assert rows[0].status == "DELIVERED"
    assert rows[0].delivered_at


async def test_ambiguous_delivery_retries_same_uuid_then_stops_before_dedup_expires(context):
    db, record, _ = context
    await queue_completion(db, record)
    row = await db.scalar(select(FeishuNotificationDelivery))
    now = utc_now_naive()
    client = AsyncMock()
    client.send_card_message.side_effect = TimeoutError("possible remote success")
    await deliver_pending(db, client, now=now)
    assert row.status == "RETRY"
    await deliver_pending(db, client, now=now + timedelta(minutes=2))
    assert client.send_card_message.call_count == 2
    assert (
        client.send_card_message.call_args_list[0].kwargs["uuid"]
        == client.send_card_message.call_args_list[1].kwargs["uuid"]
    )
    await deliver_pending(db, client, now=now + timedelta(minutes=51))
    assert row.status == "EXPIRED"
    assert client.send_card_message.call_count == 2
    assert record.state == "completed"


@pytest.mark.parametrize("change", ["revoked", "tenant", "deleted", "owner"])
async def test_permissions_are_rechecked_before_notification(context, change):
    db, record, binding = context
    await queue_completion(db, record)
    if change == "revoked":
        binding.authorization_status = "REVOKED"
    elif change == "tenant":
        binding.tenant_key = "tenant-b"
    elif change == "deleted":
        (await db.get(User, binding.user_id)).is_deleted = 1
    else:
        (await db.scalar(select(ProductConversation))).owner_user_id = 999
    await db.flush()
    client = AsyncMock()
    await deliver_pending(db, client)
    client.send_card_message.assert_not_called()
    assert (await db.scalar(select(FeishuNotificationDelivery))).status == "BLOCKED"


async def test_disabled_feature_does_not_backfill_notifications(context, monkeypatch):
    db, record, _ = context
    monkeypatch.setenv("FEISHU_MEETING_COMPLETION_NOTIFICATIONS", "false")
    await queue_completion(db, record)
    assert not list(await db.scalars(select(FeishuNotificationDelivery)))
    monkeypatch.setenv("FEISHU_MEETING_COMPLETION_NOTIFICATIONS", "true")
    assert await deliver_pending(db, AsyncMock()) == 0
