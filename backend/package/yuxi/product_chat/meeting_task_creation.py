"""Persist task creation intent and receipt outside the meeting transaction."""

import asyncio
from datetime import UTC, date, datetime, time
from hashlib import sha256

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from yuxi.integrations.feishu.client import FeishuClient, FeishuClientError
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_product import FeishuUserBinding, ManagedMeetingItem, MeetingTaskCreation
from yuxi.utils.datetime_utils import utc_now_naive


class TaskCreationUncertain(FeishuClientError):
    pass


def creation_key(source_meeting_id, task_id):
    return "meeting-" + sha256(f"{source_meeting_id}\0{task_id}".encode()).hexdigest()


def task_creation_request(task, meeting_title):
    refs = task.get("sourceRefs") or []
    return {
        "user_id": (task.get("assignee") or {}).get("feishuUserId"),
        "summary": task["title"],
        "description": (
            f"{task.get('content') + chr(10) if task.get('content') else ''}"
            f"来源：{meeting_title or '会议纪要'}"
            f"{chr(10) + '依据：' + '、'.join(refs) if refs else ''}"
        ),
        "due_timestamp": (
            int(datetime.combine(date.fromisoformat(task["dueDate"]), time.min, tzinfo=UTC).timestamp())
            if task.get("dueDate") else None
        ),
    }


async def create_task_once(client, *, source_meeting_id, task_id, request, previously_attempted=False):
    """Return (GUID, original request, recovered). Never resend an uncertain intent.

    The client may retry the identical HTTP request within this bounded attempt.
    A later API call uses the durable receipt or requests reconciliation, without
    depending on Feishu's expiring, caller-specific client_token guarantee.
    """
    key = creation_key(source_meeting_id, task_id)
    async with pg_manager.get_async_session_context() as db:
        receipt = await db.get(MeetingTaskCreation, key)
        if receipt is None and previously_attempted:
            raise TaskCreationUncertain("历史发送结果待核对。请由管理员核对飞书中的原任务，系统已暂停重复创建。")
        inserted = False
        if receipt is None:
            inserted = bool(await db.scalar(
                insert(MeetingTaskCreation).values(
                    request_key=key, app_id=client.application_id, request=request,
                ).on_conflict_do_nothing(index_elements=["request_key"]).returning(MeetingTaskCreation.request_key)
            ))
            receipt = await db.get(MeetingTaskCreation, key)
        if not inserted:
            if receipt.app_id != client.application_id:
                raise TaskCreationUncertain("飞书应用配置已变化，请先核对原任务，系统已暂停重复创建。")
            if receipt.feishu_task_id:
                return receipt.feishu_task_id, receipt.request, True
            raise TaskCreationUncertain("飞书待办创建结果待核对，请由管理员核对原任务，系统已暂停重复创建。")

    # Intent is committed before any external write. Crash/timeout/rollback
    # therefore cannot erase the fact that creation may have reached Feishu.
    try:
        async with asyncio.timeout(180):
            created = await client.create_task(**request, client_token=key, extra=key)
        remote = (created.get("data") or {}).get("task") or {}
        guid = remote.get("guid") or remote.get("id")
        if not isinstance(guid, str) or not guid.strip():
            raise FeishuClientError("飞书未返回任务编号")
    except (FeishuClientError, ValueError, TimeoutError) as exc:
        raise TaskCreationUncertain(
            "飞书待办创建结果待核对，请由管理员核对原任务，系统已暂停重复创建。"
        ) from exc

    async with pg_manager.get_async_session_context() as db:
        receipt = await db.get(MeetingTaskCreation, key)
        receipt.feishu_task_id = guid
    return guid, request, False


async def inspect_task_reconciliation(db, record, task, guid, owner_id):
    """Read and verify provenance; caller commits receipt and meeting together.

    No external writes. Both preview and confirmation repeat these checks.
    Legacy unknown requests without a marker cannot be proven by title alone.
    """
    if (task.get("delivery") or {}).get("feishuTaskId"):
        raise ValueError("该待办已有飞书关联，请刷新后使用原任务")
    key = creation_key(task.get("sourceMeetingId") or record.id, task["id"])
    receipt = await db.scalar(
        select(MeetingTaskCreation).where(MeetingTaskCreation.request_key == key).with_for_update()
    )
    if receipt is None:
        raise ValueError("缺少原始发送记录，无法可靠确认任务归属；请人工核对，系统不会重新创建")
    if not receipt.feishu_task_id and (utc_now_naive() - receipt.created_at).total_seconds() < 180:
        raise ValueError("原发送请求可能仍在处理中，请三分钟后再核对")
    if receipt.feishu_task_id and receipt.feishu_task_id != guid:
        raise ValueError("任务编号与已保存的飞书成功回执不一致")
    binding = await db.scalar(select(FeishuUserBinding).where(
        FeishuUserBinding.user_id == owner_id,
        FeishuUserBinding.authorization_status == "ACTIVE",
    ))
    if binding is None or not binding.feishu_user_id:
        raise ValueError("会议上传者没有有效的飞书企业身份，请先恢复授权")
    client = FeishuClient()
    try:
        async with asyncio.timeout(45):
            if receipt.app_id != client.application_id:
                raise ValueError("当前飞书应用与原发送应用不一致，不能恢复关联")
            employee = await client.get_employee(binding.feishu_user_id)
            if employee.get("open_id") != binding.feishu_open_id:
                raise ValueError("当前飞书应用与会议所属企业身份不一致")
            remote = await client.get_task(guid)
    finally:
        await client.aclose()
    if remote.get("guid") != guid:
        raise ValueError("飞书返回的任务编号不一致")
    if remote.get("extra") != key:
        if remote.get("extra") or not receipt.feishu_task_id:
            raise ValueError("任务来源标记不匹配或缺失，无法确认属于本条待办；请人工核对原任务")
    completed_at = str(remote.get("completed_at", ""))
    if not completed_at.isdecimal():
        raise ValueError("飞书未返回有效的任务完成状态")
    # Serialize competing recoveries of one remote task across different meetings.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {"key": f"meeting-reconcile:{receipt.app_id}:{guid}"})
    duplicate = await db.scalar(select(MeetingTaskCreation.request_key).where(
        MeetingTaskCreation.app_id == receipt.app_id,
        MeetingTaskCreation.feishu_task_id == guid,
        MeetingTaskCreation.request_key != key,
    ).limit(1))
    if duplicate:
        raise ValueError("该飞书任务已关联其他待办，不能重复关联")
    # Include mappings created before durable receipts were introduced.
    existing = await db.scalars(select(ManagedMeetingItem).where(
        ManagedMeetingItem.kind == "TASK",
        ManagedMeetingItem.payload["delivery"]["feishuTaskId"].as_string() == guid,
    ))
    for item in existing:
        original_id = item.payload.get("sourceMeetingId") or item.meeting_id
        if creation_key(original_id, item.item_id) != key:
            raise ValueError("该飞书任务已关联其他待办，不能重复关联")
    request = task_creation_request(task, record.result.get("title"))
    assignees = sorted(m["id"] for m in remote.get("members", []) if m.get("role") == "assignee")
    due = remote.get("due") or {}
    due_ms = str(due.get("timestamp") or "0")
    if not due_ms.isdecimal():
        raise ValueError("飞书未返回有效的任务期限")
    pending = (
        remote.get("summary") != request["summary"]
        or remote.get("description", "") not in (request["description"], task.get("content") or "")
        or assignees != [request["user_id"]]
        or int(due_ms) != (request["due_timestamp"] or 0) * 1000
        or (int(due_ms) > 0 and due.get("is_all_day") is not True)
        or bool((task.get("delivery") or {}).get("pendingStatusUpdate"))
    )
    return receipt, {
        "remote": {
            "id": guid, "title": remote.get("summary", ""), "content": remote.get("description", ""),
            "assigneeIds": assignees,
            "dueDate": datetime.fromtimestamp(int(due_ms) / 1000, UTC).date().isoformat() if int(due_ms) else None,
            "status": "DONE" if int(completed_at) > 0 else "OPEN",
        },
        "pendingUpdate": pending,
        "matchBasis": "来源标记" if remote.get("extra") == key else "已保存的成功回执",
    }
