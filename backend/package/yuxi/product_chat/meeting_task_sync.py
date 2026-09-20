"""Read Feishu task completion without overwriting unsent local changes."""

import asyncio
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from yuxi.integrations.feishu.client import FeishuClient, FeishuClientError
from yuxi.product_chat.meeting_management import MeetingManagement
from yuxi.product_chat.meeting_repository import MeetingRepository
from yuxi.storage.postgres.models_product import FeishuUserBinding, ManagedMeetingItem
from yuxi.utils.datetime_utils import utc_now_naive, utc_isoformat, UTC


async def sync_tasks(db, record, user_id):
    result = deepcopy(record.result)
    tasks = result.get("followup", {}).get("tasks", [])
    eligible = [
        t for t in tasks if (t.get("delivery") or {}).get("feishuTaskId") and not t["delivery"].get("pendingUpdate")
    ]
    if not eligible:
        return {"checked": 0, "failed": 0}
    changed, failures = len(eligible), 0
    processed = set()
    client = None
    try:
        # Bound the whole meeting, including identity checks and client retries.
        async with asyncio.timeout(45):
            binding = await db.scalar(
                select(FeishuUserBinding).where(
                    FeishuUserBinding.user_id == user_id,
                    FeishuUserBinding.authorization_status == "ACTIVE",
                )
            )
            if binding is None or not binding.feishu_user_id:
                raise ValueError("请先绑定有效的飞书企业身份，再读取任务状态")
            client = FeishuClient()
            employee = await client.get_employee(binding.feishu_user_id)
            if employee.get("open_id") != binding.feishu_open_id:
                raise ValueError("当前飞书应用与会议所属企业身份不一致")
            for task in eligible:
                delivery = task["delivery"]
                now = utc_isoformat(utc_now_naive().replace(tzinfo=UTC))
                delivery["lastSyncAttemptAt"] = now
                try:
                    remote = await client.get_task(delivery["feishuTaskId"])
                    completed_at = str(remote.get("completed_at", ""))
                    if not completed_at.isdecimal():
                        raise ValueError("飞书未返回有效的任务完成状态")
                    if int(completed_at) > 0:
                        task["status"] = "DONE"
                    elif task.get("status") == "DONE":
                        task["status"] = "OPEN"
                    delivery.update(syncStatus="SYNCED", lastSyncedAt=now, syncError=None)
                    delivery["scheduleComparison"] = compare_task_schedule(task, remote)
                    delivery["scheduleCheckedAt"] = now
                except (FeishuClientError, ValueError) as exc:
                    failures += 1
                    delivery.update(syncStatus="FAILED", syncError=str(exc)[:300])
                processed.add(task["id"])
    except (FeishuClientError, ValueError, TimeoutError) as exc:
        # An identity failure or meeting timeout must be visible in both products.
        # Preserve last known business state and last successful read time.
        error = "飞书状态读取超时，将在后续同步中重试" if isinstance(exc, TimeoutError) else str(exc)[:300]
        for task in eligible:
            if task["id"] in processed:
                continue
            failures += 1
            task["delivery"].update(
                syncStatus="FAILED",
                syncError=error,
                lastSyncAttemptAt=utc_isoformat(utc_now_naive().replace(tzinfo=UTC)),
            )
    finally:
        if client:
            await client.aclose()
    previous = {t["id"]: t for t in record.result["followup"]["tasks"]}
    state_changed = any(
        (task.get("status"), task["delivery"].get("syncStatus"), task["delivery"].get("syncError"),
         task["delivery"].get("scheduleComparison"))
        != (
            previous[task["id"]].get("status"),
            previous[task["id"]]["delivery"].get("syncStatus"),
            previous[task["id"]]["delivery"].get("syncError"),
            previous[task["id"]]["delivery"].get("scheduleComparison"),
        )
        for task in eligible
    )
    if changed:
        managed = await MeetingManagement(db).attach(record)
        if state_changed:
            await MeetingRepository(db).save_result(record, result, editor=user_id)
            MeetingManagement(db).event(managed.id, "TASK_SYNC", user_id, {"checked": changed, "failed": failures})
        else:
            # Polling with no state change only refreshes sync metadata.
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


def compare_task_schedule(task, remote):
    """Compare read-only snapshots; an incomplete response is never a match."""
    try:
        members = remote.get("members")
        if not isinstance(members, list) or any(
            not isinstance(member, dict) or not isinstance(member.get("id"), str)
            or not member["id"] or not isinstance(member.get("role"), str) for member in members
        ):
            raise ValueError("飞书未返回完整的负责人信息")
        local = task.get("assignee") or {}
        local_id = local.get("feishuUserId")
        if local and not local_id:
            raise ValueError("本地负责人缺少飞书用户编号，请核对绑定")
        remote_ids = sorted({m["id"] for m in members if m["role"] == "assignee"})
        assignees = [{"id": uid, "name": next(
            (m.get("name") for m in members if m["id"] == uid and m.get("name")),
            local.get("displayName") if uid == local_id else None,
        ) or uid} for uid in remote_ids]
        due = remote.get("due")
        due = {} if due is None else due
        if not isinstance(due, dict) or (due and "timestamp" not in due):
            raise ValueError("飞书未返回有效的任务期限")
        timestamp = str(due.get("timestamp", "0"))
        if not timestamp.isdecimal() or (int(timestamp) > 0 and not isinstance(due.get("is_all_day"), bool)):
            raise ValueError("飞书未返回有效的任务期限")
        due_date = None
        if int(timestamp):
            # Feishu all-day timestamps encode the calendar date in UTC, not local time.
            value = datetime.fromtimestamp(int(timestamp) / 1000, UTC)
            due_date = (value.date().isoformat() if due["is_all_day"] else
                        value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M（北京时间）"))
        differences = []
        if remote_ids != ([local_id] if local_id else []):
            differences.append("assignee")
        if due_date != (task.get("dueDate") or None):
            differences.append("dueDate")
        return {
            "status": "DIFFERENT" if differences else "MATCH", "differences": differences,
            "localAssignees": [{"id": local_id, "name": local.get("displayName") or local_id}] if local_id else [],
            "remoteAssignees": assignees, "localDueDate": task.get("dueDate") or None, "remoteDueDate": due_date,
        }
    except (ValueError, OverflowError, OSError) as exc:
        error = str(exc) if isinstance(exc, ValueError) else "飞书任务期限超出有效范围"
        return {"status": "UNVERIFIED", "error": error}


async def sync_task_batch():
    """Read one bounded batch; PostgreSQL coordinates concurrent API replicas."""
    from sqlalchemy import func, false
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_product import ManagedMeeting, MeetingRecord
    from yuxi.utils import logger

    async with pg_manager.get_async_session_context() as db:
        if not await db.scalar(select(func.pg_try_advisory_xact_lock(7130291))):
            return
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
            # Savepoint isolates unexpected errors in one meeting.
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
                        await sync_tasks(db, record, managed.owner_user_id)
            except Exception as exc:
                # A failed meeting must not starve later meetings in the next batch.
                await db.execute(
                    update(ManagedMeetingItem)
                    .where(
                        ManagedMeetingItem.meeting_id == mid,
                        ManagedMeetingItem.kind == "TASK",
                    )
                    .values(updated_at=utc_now_naive())
                )
                logger.warning("Meeting task readback deferred: {}", type(exc).__name__)


async def poll_task_statuses():
    """Poll every five minutes after the previous batch finishes."""
    from yuxi.utils import logger

    while True:
        await asyncio.sleep(300)
        try:
            await sync_task_batch()
        except Exception as exc:
            logger.warning("Meeting task polling unavailable: {}", type(exc).__name__)
