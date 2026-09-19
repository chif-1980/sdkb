"""Admin meeting workspace; management never grants access to private chat history."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from server.utils.auth_middleware import get_admin_user
from server.routers import product_meeting_router as product
from yuxi.product_chat.meeting_management import MeetingManagement, iso
from yuxi.product_chat.meeting_repository import serialize_meeting, MeetingRepository
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_product import (
    FeishuUserBinding,
    ManagedMeeting,
    ManagedMeetingEvent,
    ManagedMeetingItem,
    ManagedMeetingRun,
    MeetingRecord,
    MeetingRevision,
)

meeting_management = APIRouter(prefix="/meeting-management", tags=["meeting-management"])


async def require(db, meeting_id, user, *, lock=False, writable=False):
    service = MeetingManagement(db)
    scope = await service.scope_for_user(user.id)
    await service.backfill_scope(user.id, scope["tenantKey"])
    managed = await service.require(meeting_id, user.id, tenant_key=scope["tenantKey"], lock=lock)
    if managed is None:
        raise HTTPException(404, "会议不存在或不在你的管理范围内")
    if writable and managed.archived:
        raise HTTPException(409, "请先恢复会议再修改")
    return managed


@meeting_management.get("")
async def listing(
    q: str = Query(default="", max_length=200),
    archived: bool = False,
    state: Literal["pending", "running", "completed", "failed", "cancelled"] | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    user=Depends(get_admin_user),
):
    async with pg_manager.get_async_session_context() as db:
        service = MeetingManagement(db)
        scope = await service.scope_for_user(user.id)
        result = await service.listing(
            user.id,
            tenant_key=scope["tenantKey"],
            q=q.strip(),
            archived=archived,
            state=state,
            offset=offset,
            limit=limit,
        )
        owner_ids = {item["ownerUserId"] for item in result["items"]}
        name_rows = (
            (
                await db.execute(
                    select(FeishuUserBinding.user_id, FeishuUserBinding.display_name).where(
                        FeishuUserBinding.user_id.in_(owner_ids)
                    )
                )
            ).all()
            if owner_ids
            else []
        )
        names = {row.user_id: row.display_name for row in name_rows}
        for item in result["items"]:
            item["ownerDisplayName"] = names.get(item["ownerUserId"])
        return result


@meeting_management.get("/queue/{kind}")
async def queue(
    kind: Literal["TASK", "KNOWLEDGE"],
    status: str = Query(default="", max_length=40),
    q: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    user=Depends(get_admin_user),
):
    async with pg_manager.get_async_session_context() as db:
        service = MeetingManagement(db)
        scope = await service.scope_for_user(user.id)
        result = await service.queue(
            user.id, kind, tenant_key=scope["tenantKey"], status=status, q=q.strip(), offset=offset, limit=limit
        )
        owner_ids = {item["ownerUserId"] for item in result["items"]}
        name_rows = (
            (
                await db.execute(
                    select(FeishuUserBinding.user_id, FeishuUserBinding.display_name).where(
                        FeishuUserBinding.user_id.in_(owner_ids)
                    )
                )
            ).all()
            if owner_ids
            else []
        )
        names = {row.user_id: row.display_name for row in name_rows}
        for item in result["items"]:
            item["ownerDisplayName"] = names.get(item["ownerUserId"])
        return result


@meeting_management.get("/metrics")
async def metrics(user=Depends(get_admin_user)):
    async with pg_manager.get_async_session_context() as db:
        service = MeetingManagement(db)
        scope = await service.scope_for_user(user.id)
        tenant_key = scope["tenantKey"]
        pending = await service.queue(user.id, "TASK", tenant_key=tenant_key, status="PENDING", limit=1)
        overdue = await service.queue(user.id, "TASK", tenant_key=tenant_key, status="OVERDUE", limit=1)
        knowledge = await service.queue(
            user.id, "KNOWLEDGE", tenant_key=tenant_key, status="PENDING_MAINTAINER", limit=1
        )
        errors = await db.scalar(
            select(func.count())
            .select_from(ManagedMeeting)
            .join(
                MeetingRecord,
                MeetingRecord.id == ManagedMeeting.latest_run_id,
            )
            .where(
                (ManagedMeeting.tenant_key == tenant_key if tenant_key else ManagedMeeting.owner_user_id == user.id),
                ManagedMeeting.archived == 0,
                MeetingRecord.state == "failed",
            )
        )
        return {
            "pending": pending["total"],
            "overdue": overdue["total"],
            "knowledge": knowledge["total"],
            "errors": errors,
            "scope": "TENANT" if tenant_key else "OWNED",
        }


@meeting_management.get("/formal-knowledge")
async def formal_knowledge(q: str = Query(default="", max_length=200), user=Depends(get_admin_user)):
    from yuxi.storage.postgres.models_knowledge import FeishuKnowledgeUnit, FeishuMaterialVersion, FeishuSourceItem

    async with pg_manager.get_async_session_context() as db:
        query = (
            select(FeishuKnowledgeUnit)
            .join(
                FeishuMaterialVersion,
                FeishuMaterialVersion.version_id == FeishuKnowledgeUnit.version_id,
            )
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuKnowledgeUnit.item_id)
            .where(
                FeishuKnowledgeUnit.status == "ACTIVE",
                FeishuKnowledgeUnit.lifecycle_status == "ACTIVE",
                FeishuKnowledgeUnit.publication_state == "INCLUDED",
                FeishuSourceItem.active_version_id == FeishuKnowledgeUnit.version_id,
                FeishuMaterialVersion.review_status == "approved",
                FeishuMaterialVersion.published_at.is_not(None),
            )
        )
        if q.strip():
            query = query.where(FeishuKnowledgeUnit.title.icontains(q.strip(), autoescape=True))
        rows = list(await db.scalars(query.order_by(FeishuKnowledgeUnit.updated_at.desc()).limit(30)))
        return {
            "items": [
                {"id": r.unit_id, "title": r.title, "versionId": r.version_id, "excerpt": r.content[:500]} for r in rows
            ]
        }


@meeting_management.get("/{meeting_id}")
async def detail(meeting_id: str, user=Depends(get_admin_user)):
    async with pg_manager.get_async_session_context() as db:
        managed = await require(db, meeting_id, user)
        run = await db.get(MeetingRecord, managed.latest_run_id)
        success = await db.get(MeetingRecord, managed.successful_run_id) if managed.successful_run_id else None
        items = list(await db.scalars(select(ManagedMeetingItem).where(ManagedMeetingItem.meeting_id == managed.id)))
        runs = list(
            await db.scalars(
                select(MeetingRecord)
                .join(
                    ManagedMeetingRun,
                    ManagedMeetingRun.run_id == MeetingRecord.id,
                )
                .where(ManagedMeetingRun.meeting_id == managed.id)
                .order_by(MeetingRecord.created_at.desc())
            )
        )
        events = list(
            await db.scalars(
                select(ManagedMeetingEvent)
                .where(ManagedMeetingEvent.meeting_id == managed.id)
                .order_by(ManagedMeetingEvent.created_at.desc())
                .limit(100)
            )
        )
        result = serialize_meeting(success) if success else None
        owner_display_name = await db.scalar(
            select(FeishuUserBinding.display_name).where(FeishuUserBinding.user_id == managed.owner_user_id)
        )
        if result:
            # Do not expose embedded historical meetings through the management endpoint.
            result["result"] = {k: v for k, v in result["result"].items() if k != "selectedHistory"}
            result["result"]["followup"] = {
                **(result["result"].get("followup") or {}),
                "tasks": [i.payload for i in items if i.kind == "TASK"],
                "knowledgeSuggestions": [i.payload for i in items if i.kind == "KNOWLEDGE"],
            }
        return {
            "meeting": MeetingManagement.summary(managed, run, items),
            "current": result,
            "runs": [
                {"id": r.id, "state": r.state, "version": r.version, "createdAt": iso(r.created_at), "error": r.error}
                for r in runs
            ],
            "events": [{"action": e.action, "detail": e.detail, "createdAt": iso(e.created_at)} for e in events],
            "scope": "TENANT" if managed.tenant_key else "OWNED",
            "ownerDisplayName": owner_display_name,
        }


@meeting_management.get("/{meeting_id}/runs/{run_id}")
async def run_detail(meeting_id: str, run_id: str, version: int | None = None, user=Depends(get_admin_user)):
    async with pg_manager.get_async_session_context() as db:
        await require(db, meeting_id, user)
        mapping = await db.get(ManagedMeetingRun, run_id)
        if not mapping or mapping.meeting_id != meeting_id:
            raise HTTPException(404, "会议版本不存在")
        run = await db.get(MeetingRecord, run_id)
        data = serialize_meeting(run)
        if version is not None:
            revision = await db.scalar(
                select(MeetingRevision).where(
                    MeetingRevision.meeting_id == run_id,
                    MeetingRevision.version == version,
                )
            )
            if not revision:
                raise HTTPException(404, "纪要修订不存在")
            data["result"], data["version"] = revision.result, revision.version
        if data["result"]:
            data["result"] = {k: v for k, v in data["result"].items() if k != "selectedHistory"}
        revisions = list(
            await db.scalars(
                select(MeetingRevision.version)
                .where(MeetingRevision.meeting_id == run_id)
                .order_by(MeetingRevision.version.desc())
            )
        )
        return {"meeting": data, "versions": revisions}


class ArchiveEdit(BaseModel):
    version: int = Field(ge=1)
    archived: bool


@meeting_management.patch("/{meeting_id}/archive")
async def archive(meeting_id: str, patch: ArchiveEdit, user=Depends(get_admin_user)):
    async with pg_manager.get_async_session_context() as db:
        managed = await require(db, meeting_id, user, lock=True)
        if managed.version != patch.version:
            raise HTTPException(409, "会议已更新，请刷新后重试")
        managed.archived = int(patch.archived)
        managed.version += 1
        MeetingManagement(db).event(managed.id, "ARCHIVE" if patch.archived else "RESTORE", user.id)
        return {"version": managed.version, "archived": bool(managed.archived)}


async def current_run(meeting_id, user, *, writable=True, latest=False):
    async with pg_manager.get_async_session_context() as db:
        managed = await require(db, meeting_id, user, writable=writable)
        run_id = managed.latest_run_id if latest else managed.successful_run_id
        if not run_id:
            raise HTTPException(409, "会议尚无成功结果")
        return run_id, managed.owner_user_id


@meeting_management.get("/{meeting_id}/directory")
async def directory(meeting_id: str, user=Depends(get_admin_user)):
    run_id, owner_id = await current_run(meeting_id, user)
    return await product.meeting_followup_directory(run_id, user, owner_id=owner_id)


@meeting_management.patch("/{meeting_id}/followup")
async def followup(meeting_id: str, patch: product.MeetingFollowupEdit, user=Depends(get_admin_user)):
    run_id, owner_id = await current_run(meeting_id, user)
    return await product.update_meeting_followup(
        run_id, patch, user, owner_id=owner_id, require_active_conversation=False
    )


@meeting_management.patch("/{meeting_id}/minutes")
async def minutes(meeting_id: str, patch: product.MeetingEdit, user=Depends(get_admin_user)):
    run_id, owner_id = await current_run(meeting_id, user)
    return await product.update_meeting_minutes(
        run_id, patch, user, owner_id=owner_id, require_active_conversation=False
    )


@meeting_management.post("/{meeting_id}/retry")
async def retry(meeting_id: str, user=Depends(get_admin_user)):
    run_id, owner_id = await current_run(meeting_id, user, latest=True)
    return await product.retry_meeting(run_id, user, owner_id=owner_id)


@meeting_management.post("/{meeting_id}/cancel")
async def cancel(meeting_id: str, user=Depends(get_admin_user)):
    run_id, owner_id = await current_run(meeting_id, user, latest=True)
    return await product.cancel_meeting(run_id, user, owner_id=owner_id)


@meeting_management.get("/{meeting_id}/export")
async def export(meeting_id: str, version: int, user=Depends(get_admin_user)):
    run_id, owner_id = await current_run(meeting_id, user, writable=False)
    return await product.export_meeting(run_id, version, user, owner_id=owner_id)


class KnowledgeDecision(BaseModel):
    version: int = Field(ge=1)
    action: Literal[
        "COVERED",
        "DEFERRED",
        "REJECTED",
        "PENDING_MAINTAINER",
        "PROCESSING",
        "REQUEST_SOURCE_CHANGE",
        "NEW_SOURCE_DRAFT",
    ]
    reason: str = Field(min_length=1, max_length=2000)
    knowledgeUnitId: str | None = Field(default=None, max_length=128)
    draftContent: str | None = Field(default=None, max_length=20000)


@meeting_management.patch("/{meeting_id}/knowledge/{item_id}")
async def knowledge_decision(meeting_id: str, item_id: str, patch: KnowledgeDecision, user=Depends(get_admin_user)):
    from copy import deepcopy
    from yuxi.storage.postgres.models_knowledge import FeishuKnowledgeUnit

    async with pg_manager.get_async_session_context() as db:
        managed = await require(db, meeting_id, user, writable=True)
        record = await db.scalar(
            select(MeetingRecord).where(MeetingRecord.id == managed.successful_run_id).with_for_update()
        )
        if not record or record.version != patch.version:
            raise HTTPException(409, "会议版本已变化，请刷新后重试")
        if not patch.reason.strip():
            raise HTTPException(422, "请填写处理说明")
        result = deepcopy(record.result)
        target = next(
            (s for s in result.get("followup", {}).get("knowledgeSuggestions", []) if s["id"] == item_id), None
        )
        if not target:
            raise HTTPException(404, "知识建议不存在")
        if patch.action == "COVERED":
            if not patch.knowledgeUnitId:
                raise HTTPException(422, "请关联已覆盖的正式知识")
            unit = await db.scalar(
                select(FeishuKnowledgeUnit).where(
                    FeishuKnowledgeUnit.unit_id == patch.knowledgeUnitId,
                )
            )
            if not unit or unit.lifecycle_status != "ACTIVE":
                raise HTTPException(422, "请选择有效的正式知识")
            from yuxi.governance.lifecycle_service import KnowledgeLifecycleService

            try:
                await KnowledgeLifecycleService(db)._ensure_formal_unit(unit)
            except (ValueError, LookupError):
                raise HTTPException(422, "该知识尚未发布，或已不属于当前正式版本") from None
            target["knowledgeUnitId"] = unit.unit_id
            target["knowledgeVersionId"] = unit.version_id
        if patch.action == "PROCESSING":
            if not patch.draftContent or not patch.draftContent.strip():
                raise HTTPException(422, "请填写建议草稿正文")
            target["draftContent"] = patch.draftContent.strip()
        if patch.action == "NEW_SOURCE_DRAFT":
            if not patch.draftContent or not patch.draftContent.strip():
                raise HTTPException(422, "请填写待维护人员补回的原文或建议草稿")
            target["draftContent"] = patch.draftContent.strip()
            target["governance"] = {
                "nextStep": "请维护人员补回飞书原文后启动扫描审核",
                "formalPublication": False,
            }
        if patch.action == "REQUEST_SOURCE_CHANGE":
            if not patch.knowledgeUnitId:
                raise HTTPException(422, "请选择需要修改的正式知识")
            if not patch.draftContent or not patch.draftContent.strip():
                raise HTTPException(422, "请填写修改建议，供维护人员更新飞书原文")
            from yuxi.governance.domain import ReviewTriggerType
            from yuxi.governance.lifecycle_service import KnowledgeLifecycleService

            try:
                revision = await KnowledgeLifecycleService(db).create_revision_request(
                    patch.knowledgeUnitId,
                    trigger_type=ReviewTriggerType.FEEDBACK,
                    reason=patch.reason.strip(),
                    operator_id=str(user.uid),
                )
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            from yuxi.storage.postgres.models_knowledge import FeishuReviewItem, FeishuReviewPackage

            package = await db.get(FeishuReviewPackage, revision["package_id"])
            review_item = await db.get(FeishuReviewItem, revision["review_item_id"])
            if package is not None:
                package.draft_json = {
                    **dict(package.draft_json or {}),
                    "meetingSuggestion": {
                        "meetingId": managed.id,
                        "meetingItemId": item_id,
                        "content": patch.draftContent.strip(),
                    },
                }
            if review_item is not None:
                review_item.decision_payload = {
                    **dict(review_item.decision_payload or {}),
                    "meeting_id": managed.id,
                    "meeting_item_id": item_id,
                    "draft_content": patch.draftContent.strip(),
                }
            target["draftContent"] = patch.draftContent.strip()
            target["knowledgeUnitId"] = patch.knowledgeUnitId
            target["governance"] = {
                "reviewPackageId": revision["package_id"],
                "reviewItemId": revision["review_item_id"],
                "changeRequestId": revision["change_request_id"],
                "workflowStatus": revision["workflow_status"],
                "formalPublication": False,
                "nextStep": "维护人员修改飞书原文后进入现有审核/发布流程",
            }
            patch_action = "REVIEW_REQUESTED"
        else:
            patch_action = patch.action
        target.update(status=patch_action, decisionReason=patch.reason.strip(), decidedBy=user.id)
        await MeetingRepository(db).save_result(record, result, editor=user.id)
        MeetingManagement(db).event(
            managed.id,
            "KNOWLEDGE_DECISION",
            user.id,
            {"itemId": item_id, "status": patch_action, "reason": patch.reason.strip()},
        )
        return {"meeting": serialize_meeting(record)}


@meeting_management.post("/{meeting_id}/sync-tasks")
async def refresh_tasks(meeting_id: str, user=Depends(get_admin_user)):
    from yuxi.product_chat.meeting_task_sync import sync_tasks

    async with pg_manager.get_async_session_context() as db:
        managed = await require(db, meeting_id, user, writable=True)
        record = await db.scalar(
            select(MeetingRecord).where(MeetingRecord.id == managed.successful_run_id).with_for_update()
        )
        if not record:
            raise HTTPException(409, "尚无会议结果")
        try:
            return await sync_tasks(db, record, user.id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
