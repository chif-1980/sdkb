"""Close linked meeting suggestions only after their source version is published."""

from copy import deepcopy

from sqlalchemy import select

from yuxi.governance.domain import SourceChangeRequestStatus
from yuxi.product_chat.meeting_repository import MeetingRepository
from yuxi.storage.postgres.models_knowledge import FeishuMaterialVersion, FeishuSourceChangeRequest, FeishuSourceItem
from yuxi.storage.postgres.models_product import ManagedMeeting, ManagedMeetingItem, MeetingRecord
from yuxi.utils.datetime_utils import utc_isoformat


async def complete_meeting_suggestions(db, version_id: str) -> int:
    version = await db.scalar(select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == version_id))
    if not version or version.processing_status != "published" or not version.published_at:
        return 0
    source = await db.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == version.item_id))
    # 扫描已把来源标记失效（飞书原文被删除或移出范围）时，即使本次发布成功也不算有效发布，
    # 不能关闭会议建议，否则会对外宣称一份已经不存在的原文已经闭环。
    if (
        not source
        or source.source_validity != "valid"
        or source.active_version_id != version_id
        or source.publication_status != "ACTIVE"
    ):
        return 0
    request_ids = list(
        await db.scalars(
            select(FeishuSourceChangeRequest.change_request_id).where(
                FeishuSourceChangeRequest.received_version_id == version_id,
                FeishuSourceChangeRequest.status == SourceChangeRequestStatus.FULFILLED,
            )
        )
    )
    if not request_ids:
        return 0
    meeting_ids = list(
        await db.scalars(
            select(ManagedMeetingItem.meeting_id)
            .where(
                ManagedMeetingItem.kind == "KNOWLEDGE",
                ManagedMeetingItem.payload["governance"]["changeRequestId"].as_string().in_(request_ids),
            )
            .distinct()
        )
    )
    updated = 0
    for meeting_id in sorted(meeting_ids):
        managed = await db.get(ManagedMeeting, meeting_id)
        record = await db.scalar(
            select(MeetingRecord).where(MeetingRecord.id == managed.successful_run_id).with_for_update()
        )
        if not record or not record.result:
            continue
        result = deepcopy(record.result)
        changed = []
        for suggestion in result.get("followup", {}).get("knowledgeSuggestions", []):
            governance = suggestion.get("governance") or {}
            if (
                suggestion.get("status") != "REVIEW_REQUESTED"
                or governance.get("changeRequestId") not in request_ids
                or governance.get("formalPublication")
            ):
                continue
            suggestion["status"] = "PUBLISHED"
            suggestion["knowledgeVersionId"] = version_id
            suggestion["governance"] = {
                **governance,
                "formalPublication": True,
                "publishedVersionId": version_id,
                "publishedAt": utc_isoformat(version.published_at),
                "workflowStatus": "PUBLISHED",
                "nextStep": "原文修改已正式发布，会议建议已自动完成",
            }
            changed.append(suggestion["id"])
        if changed:
            await MeetingRepository(db).save_result(
                record,
                result,
                audit={
                    "action": "KNOWLEDGE_PUBLISHED",
                    "detail": {
                        "itemIds": changed,
                        "versionId": version_id,
                        "actorName": "系统发布回写",
                    },
                },
            )
            updated += len(changed)
    return updated
