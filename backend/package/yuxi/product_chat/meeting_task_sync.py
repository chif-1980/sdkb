"""Read Feishu task completion without overwriting unsent local changes."""

from copy import deepcopy

from sqlalchemy import select, update

from yuxi.integrations.feishu.client import FeishuClient, FeishuClientError
from yuxi.product_chat.meeting_management import MeetingManagement
from yuxi.product_chat.meeting_repository import MeetingRepository
from yuxi.storage.postgres.models_product import FeishuUserBinding, ManagedMeetingItem
from yuxi.utils.datetime_utils import utc_now_naive, utc_isoformat, UTC


async def sync_tasks(db, record, user_id):
    binding = await db.scalar(
        select(FeishuUserBinding).where(
            FeishuUserBinding.user_id == user_id,
            FeishuUserBinding.authorization_status == "ACTIVE",
        )
    )
    if binding is None or not binding.feishu_user_id:
        raise ValueError("请先绑定有效的飞书企业身份，再读取任务状态")
    client = FeishuClient()
    result = deepcopy(record.result)
    changed, failures = 0, 0
    state_changed = False
    try:
        employee = await client.get_employee(binding.feishu_user_id)
        if employee.get("open_id") != binding.feishu_open_id:
            raise ValueError("当前飞书应用与会议所属企业身份不一致")
        for task in result.get("followup", {}).get("tasks", []):
            delivery = task.get("delivery") or {}
            task_id = delivery.get("feishuTaskId")
            if not task_id or delivery.get("pendingUpdate"):
                continue
            before = (task.get("status"), delivery.get("syncStatus"), delivery.get("syncError"))
            now = utc_isoformat(utc_now_naive().replace(tzinfo=UTC))
            try:
                remote = await client.get_task(task_id)
                completed = str(remote.get("completed_at") or "0") not in {"", "0"}
                if completed:
                    task["status"] = "DONE"
                elif task.get("status") == "DONE":
                    task["status"] = "OPEN"
                delivery.update(syncStatus="SYNCED", lastSyncedAt=now, syncError=None)
            except (FeishuClientError, ValueError) as exc:
                failures += 1
                delivery.update(syncStatus="FAILED", lastSyncAttemptAt=now, syncError=str(exc)[:300])
            task["delivery"] = delivery
            changed += 1
            state_changed |= before != (task.get("status"), delivery.get("syncStatus"), delivery.get("syncError"))
    finally:
        await client.aclose()
    if changed:
        managed = await MeetingManagement(db).attach(record)
        if state_changed:
            await MeetingRepository(db).save_result(record, result, editor=user_id)
            MeetingManagement(db).event(managed.id, "TASK_SYNC", user_id, {"checked": changed, "failed": failures})
        else:
            # Successful polling with no state change only refreshes sync metadata.
            # It must not create a revision or invalidate a user's edit version.
            record.result = result
            for task in result.get("followup", {}).get("tasks", []):
                if not (task.get("delivery") or {}).get("feishuTaskId") or task["delivery"].get("pendingUpdate"):
                    continue
                await db.execute(
                    update(ManagedMeetingItem)
                    .where(
                        ManagedMeetingItem.meeting_id == managed.id,
                        ManagedMeetingItem.kind == "TASK",
                        ManagedMeetingItem.item_id == task["id"],
                    )
                    .values(payload=task, updated_at=utc_now_naive())
                )
    return {"checked": changed, "failed": failures}


async def poll_task_statuses():
    """Bounded five-minute polling, single replica via transaction advisory lock."""
    import asyncio
    from sqlalchemy import func, false
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_product import ManagedMeeting, ManagedMeetingItem, MeetingRecord
    from yuxi.utils import logger

    while True:
        await asyncio.sleep(300)
        try:
            async with pg_manager.get_async_session_context() as db:
                if not await db.scalar(select(func.pg_try_advisory_xact_lock(7130291))):
                    continue
                payload = ManagedMeetingItem.payload
                ids = list(
                    await db.scalars(
                        select(ManagedMeetingItem.meeting_id)
                        .join(ManagedMeeting)
                        .where(
                            ManagedMeeting.archived == 0,
                            func.coalesce(payload["delivery"]["pendingUpdate"].as_boolean(), false()).is_(False),
                            ManagedMeetingItem.kind == "TASK",
                            payload["delivery"]["feishuTaskId"].as_string().is_not(None),
                        )
                        .group_by(ManagedMeetingItem.meeting_id)
                        .order_by(func.min(ManagedMeetingItem.updated_at))
                        .limit(20)
                    )
                )
                for mid in ids:
                    # Savepoint isolates a revoked identity or unavailable remote task.
                    try:
                        async with db.begin_nested():
                            managed = await db.get(ManagedMeeting, mid)
                            record = await db.scalar(
                                select(MeetingRecord)
                                .where(
                                    MeetingRecord.id == managed.successful_run_id,
                                )
                                .with_for_update(skip_locked=True)
                            )
                            if record:
                                await asyncio.wait_for(sync_tasks(db, record, managed.owner_user_id), timeout=45)
                    except Exception as exc:
                        # Failed identity/timeout must not starve later meetings in the next batch.
                        await db.execute(
                            update(ManagedMeetingItem)
                            .where(
                                ManagedMeetingItem.meeting_id == mid,
                                ManagedMeetingItem.kind == "TASK",
                            )
                            .values(updated_at=utc_now_naive())
                        )
                        logger.warning("Meeting task readback deferred: {}", type(exc).__name__)
        except Exception as exc:
            logger.warning("Meeting task polling unavailable: {}", type(exc).__name__)
