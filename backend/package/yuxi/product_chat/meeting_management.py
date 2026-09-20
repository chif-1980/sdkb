"""Meeting management read model and durable follow-ups shared by both products."""

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from hashlib import sha256

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import load_only

from yuxi.storage.postgres.models_product import (
    FeishuUserBinding,
    ManagedMeeting,
    ManagedMeetingEvent,
    ManagedMeetingItem,
    ManagedMeetingRun,
    MeetingRecord,
    ProductConversation,
)
from yuxi.utils.datetime_utils import UTC, utc_isoformat, utc_now_naive


def iso(value):
    return utc_isoformat(value.replace(tzinfo=UTC)) if value else None


def item_key(item):
    return sha256((str(item.get("title", "")) + "|" + "|".join(item.get("sourceRefs") or [])).encode()).hexdigest()


def merge_followups(old_items, incoming, *, run_id):
    """Preserve human decisions on reruns; sequence numbers are not identities."""
    merged = deepcopy(old_items)
    by_key = {item.get("extractionKey") or item_key(item): item for item in merged}
    used_ids = {item["id"] for item in merged}
    by_id = {str(item["id"]): item for item in merged}
    for item in deepcopy(incoming):
        target_id = item.pop("targetTaskId", None)
        if target_id:
            previous = by_id.get(str(target_id))
            if previous is None:
                raise ValueError("AI 修改的待办编号不存在，请重试。")
            previous["aiProposal"] = {
                key: item.get(key)
                for key in ("title", "content", "assigneeSuggestion", "dueDate", "dueDateSuggestion", "sourceRefs")
            }
            previous["aiProposal"]["sourceMeetingId"] = run_id
            continue
        key = item_key(item)
        if key in by_key:
            # A new model comparison may be refreshed, but a maintainer decision cannot.
            previous = by_key[key]
            for field in ("comparisonStatus", "comparison", "formalEvidenceIds", "formalEvidence"):
                if field in item:
                    previous[field] = item[field]
            continue
        if item["id"] in used_ids:
            item["id"] = f"{run_id[-12:]}-{item['id']}"
        item["extractionKey"] = key
        item["sourceMeetingId"] = run_id
        merged.append(item)
        used_ids.add(item["id"])
        by_key[key] = item
    return merged


class MeetingManagement:
    def __init__(self, db):
        self.db = db

    async def scope_for_user(self, user_id):
        """Resolve the management scope from the administrator's Feishu binding.

        Older installations may not have a binding for an administrator yet. In
        that case we keep the legacy self-owned scope so the workspace remains
        usable while the account is being connected.
        """
        binding = await self.db.scalar(
            select(FeishuUserBinding).where(
                FeishuUserBinding.user_id == user_id,
                FeishuUserBinding.authorization_status == "ACTIVE",
            )
        )
        if binding and binding.tenant_key:
            return {"mode": "TENANT", "tenantKey": binding.tenant_key, "userId": user_id}
        return {"mode": "OWNED", "tenantKey": None, "userId": user_id}

    async def attach(self, record):
        mapping = await self.db.get(ManagedMeetingRun, record.id)
        if mapping:
            managed = await self.db.get(ManagedMeeting, mapping.meeting_id)
            if managed and not managed.tenant_key:
                binding = await self.db.scalar(
                    select(FeishuUserBinding).where(
                        FeishuUserBinding.user_id == managed.owner_user_id,
                        FeishuUserBinding.authorization_status == "ACTIVE",
                    )
                )
                if binding:
                    managed.tenant_key = binding.tenant_key
                    await self.db.flush()
            return managed
        conversation = await self.db.scalar(
            select(ProductConversation).where(
                ProductConversation.conversation_id == record.conversation_id,
            )
        )
        parent_id = (record.input or {}).get("parentId")
        parent = await self.db.get(MeetingRecord, parent_id) if parent_id and parent_id != record.id else None
        # Explicit ancestry only, and never cross an ownership boundary.
        if parent and parent.conversation_id == record.conversation_id and parent.created_at <= record.created_at:
            managed = await self.attach(parent)
        else:
            binding = await self.db.scalar(
                select(FeishuUserBinding).where(
                    FeishuUserBinding.user_id == conversation.owner_user_id,
                    FeishuUserBinding.authorization_status == "ACTIVE",
                )
            )
            await self.db.execute(
                insert(ManagedMeeting)
                .values(
                    id=record.id,
                    owner_user_id=conversation.owner_user_id,
                    tenant_key=binding.tenant_key if binding else None,
                    latest_run_id=record.id,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    title=(record.result or {}).get("title") or conversation.title or "会议纪要",
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            managed = await self.db.get(ManagedMeeting, record.id)
        await self.db.execute(
            insert(ManagedMeetingRun)
            .values(
                run_id=record.id,
                meeting_id=managed.id,
            )
            .on_conflict_do_nothing(index_elements=["run_id"])
        )
        current = await self.db.get(MeetingRecord, managed.latest_run_id)
        if (record.created_at, record.id) >= (current.created_at, current.id):
            managed.latest_run_id = record.id
        await self.db.flush()
        return managed

    async def backfill_scope(self, user_id, tenant_key=None):
        """Import missing management rows for one user or one Feishu tenant."""
        if tenant_key:
            existing = list(
                await self.db.scalars(
                    select(ManagedMeeting)
                    .join(MeetingRecord, MeetingRecord.id == ManagedMeeting.latest_run_id)
                    .join(ProductConversation, ProductConversation.conversation_id == MeetingRecord.conversation_id)
                    .join(FeishuUserBinding, FeishuUserBinding.user_id == ProductConversation.owner_user_id)
                    .where(
                        ManagedMeeting.tenant_key.is_(None),
                        FeishuUserBinding.tenant_key == tenant_key,
                        FeishuUserBinding.authorization_status == "ACTIVE",
                    )
                )
            )
            for managed in existing:
                managed.tenant_key = tenant_key
            if existing:
                await self.db.flush()
        owner_conditions = [ProductConversation.owner_user_id == user_id]
        if tenant_key:
            owner_conditions = [
                FeishuUserBinding.tenant_key == tenant_key,
                FeishuUserBinding.authorization_status == "ACTIVE",
            ]
        records = list(
            await self.db.scalars(
                select(MeetingRecord)
                .join(ProductConversation, ProductConversation.conversation_id == MeetingRecord.conversation_id)
                .outerjoin(FeishuUserBinding, FeishuUserBinding.user_id == ProductConversation.owner_user_id)
                .outerjoin(ManagedMeetingRun, ManagedMeetingRun.run_id == MeetingRecord.id)
                .where(*owner_conditions, ManagedMeetingRun.run_id.is_(None))
                .order_by(MeetingRecord.created_at, MeetingRecord.id)
            )
        )
        for record in records:
            managed = await self.attach(record)
            if record.result:
                previous_id = managed.successful_run_id
                projected = await self.project(record, record.result, migration=True)
                if previous_id and previous_id != record.id and projected != record.result:
                    # The merged current version must agree across admin, assistant and export.
                    # Save a new revision rather than rewriting the historical snapshot.
                    from yuxi.product_chat.meeting_repository import MeetingRepository

                    await MeetingRepository(self.db).save_result(record, projected, editor=user_id)

    async def backfill_owned(self, user_id):
        """Backward-compatible alias for callers outside the management router."""
        return await self.backfill_scope(user_id)

    async def project(self, record, result, *, editor=None, migration=False):
        managed = await self.attach(record)
        await self.db.refresh(managed, with_for_update=True)
        previous_id = managed.successful_run_id
        if previous_id and previous_id != record.id:
            previous = await self.db.get(MeetingRecord, previous_id)
            if previous.created_at > record.created_at:
                return result  # An older snapshot is not the current management result.
        result = deepcopy(result)
        followup = result.get("followup") or {}
        for kind, field in (("TASK", "tasks"), ("KNOWLEDGE", "knowledgeSuggestions")):
            rows = list(
                await self.db.scalars(
                    select(ManagedMeetingItem).where(
                        ManagedMeetingItem.meeting_id == managed.id,
                        ManagedMeetingItem.kind == kind,
                    )
                )
            )
            by_id = {row.item_id: row for row in rows}
            incoming = followup.get(field) or []
            if kind == "KNOWLEDGE":
                evidence = {e["evidence_id"]: e for e in result.get("formalEvidence", [])}
                for item in incoming:
                    if "formalEvidence" not in item:
                        item["formalEvidence"] = [
                            evidence[eid] for eid in item.get("formalEvidenceIds", []) if eid in evidence
                        ]
            if previous_id and previous_id != record.id:
                incoming = merge_followups([r.payload for r in rows], incoming, run_id=record.id)
            else:
                # Full result edit must not silently delete already tracked work.
                supplied = {str(item["id"]) for item in incoming}
                incoming = incoming + [r.payload for r in rows if r.item_id not in supplied]
            for item in incoming:
                row = by_id.get(str(item["id"]))
                saved = row.payload if row else {}
                item.setdefault("extractionKey", saved.get("extractionKey") or item_key(item))
                item.setdefault("sourceMeetingId", saved.get("sourceMeetingId") or record.id)
                if kind == "TASK":
                    item.setdefault("reviewStatus", "PENDING")
                if row:
                    row.payload = deepcopy(item)
                    row.updated_at = utc_now_naive()
                else:
                    self.db.add(
                        ManagedMeetingItem(
                            meeting_id=managed.id,
                            kind=kind,
                            item_id=str(item["id"]),
                            payload=deepcopy(item),
                        )
                    )
            followup[field] = incoming
        result["followup"] = followup
        managed.successful_run_id = record.id
        managed.title = str(result.get("title") or "会议纪要")[:512]
        managed.meeting_type = str(result.get("meetingType") or "未提供")[:80]
        managed.meeting_date = str(result.get("meetingDate"))[:80] if result.get("meetingDate") else None
        managed.updated_at = record.updated_at if migration else utc_now_naive()
        managed.version += 1
        if not migration:
            self.event(managed.id, "EDIT" if editor else "ANALYSIS_COMPLETED", editor, {"runId": record.id})
        await self.db.flush()
        return result

    def event(self, meeting_id, action, actor=None, detail=None):
        self.db.add(
            ManagedMeetingEvent(
                meeting_id=meeting_id,
                action=action,
                actor_user_id=actor,
                detail=detail or {},
            )
        )

    async def require(self, meeting_id, user_id, *, tenant_key=None, lock=False):
        conditions = [ManagedMeeting.id == meeting_id]
        conditions.append(
            ManagedMeeting.tenant_key == tenant_key if tenant_key else ManagedMeeting.owner_user_id == user_id
        )
        query = select(ManagedMeeting).where(*conditions)
        if lock:
            query = query.with_for_update()
        return await self.db.scalar(query)

    async def listing(self, user_id, *, tenant_key=None, q="", archived=False, state=None, offset=0, limit=20):
        await self.backfill_scope(user_id, tenant_key)
        conditions = [ManagedMeeting.archived == int(archived)]
        conditions.append(
            ManagedMeeting.tenant_key == tenant_key if tenant_key else ManagedMeeting.owner_user_id == user_id
        )
        if q:
            conditions.append(
                or_(
                    ManagedMeeting.title.icontains(q, autoescape=True),
                    ManagedMeeting.id.in_(
                        select(ManagedMeetingRun.meeting_id)
                        .join(
                            MeetingRecord,
                            MeetingRecord.id == ManagedMeetingRun.run_id,
                        )
                        .where(MeetingRecord.result["body"].as_string().icontains(q, autoescape=True))
                    ),
                )
            )
        if state:
            conditions.append(MeetingRecord.state == state)
        query = (
            select(ManagedMeeting, MeetingRecord)
            .join(
                MeetingRecord,
                MeetingRecord.id == ManagedMeeting.latest_run_id,
            )
            .where(*conditions)
        )
        total = await self.db.scalar(select(func.count()).select_from(query.subquery()))
        page = (
            await self.db.execute(
                query.options(
                    load_only(
                        MeetingRecord.id,
                        MeetingRecord.state,
                        MeetingRecord.error,
                        MeetingRecord.progress,
                    )
                )
                .order_by(ManagedMeeting.updated_at.desc(), ManagedMeeting.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
        ids = [m.id for m, _ in page]
        items = list(await self.db.scalars(select(ManagedMeetingItem).where(ManagedMeetingItem.meeting_id.in_(ids))))
        return {
            "items": [self.summary(m, run, [i for i in items if i.meeting_id == m.id]) for m, run in page],
            "total": total,
            "scope": "TENANT" if tenant_key else "OWNED",
        }

    @staticmethod
    def summary(managed, run, items):
        tasks = [i.payload for i in items if i.kind == "TASK"]
        suggestions = [i.payload for i in items if i.kind == "KNOWLEDGE"]
        return {
            "id": managed.id,
            "title": managed.title,
            "meetingType": managed.meeting_type,
            "meetingDate": managed.meeting_date,
            "ownerUserId": managed.owner_user_id,
            "state": run.state,
            "error": run.error,
            "progress": run.progress,
            "latestRunId": run.id,
            "successfulRunId": managed.successful_run_id,
            "archived": bool(managed.archived),
            "version": managed.version,
            "createdAt": iso(managed.created_at),
            "updatedAt": iso(managed.updated_at),
            "pendingTasks": sum(t.get("reviewStatus", "PENDING") == "PENDING" for t in tasks),
            "completedTasks": sum(t.get("status") == "DONE" for t in tasks),
            "taskCount": len(tasks),
            "pendingKnowledge": sum(s.get("status") in {"PENDING_MAINTAINER", "NEW_SOURCE_DRAFT"} for s in suggestions),
        }

    async def queue(self, user_id, kind, *, tenant_key=None, status="", q="", offset=0, limit=20):
        await self.backfill_scope(user_id, tenant_key)
        conditions = [
            ManagedMeeting.archived == 0,
            ManagedMeetingItem.kind == kind,
        ]
        conditions.append(
            ManagedMeeting.tenant_key == tenant_key if tenant_key else ManagedMeeting.owner_user_id == user_id
        )
        p = ManagedMeetingItem.payload
        if q:
            conditions.append(
                or_(
                    p["title"].as_string().icontains(q, autoescape=True),
                    ManagedMeeting.title.icontains(q, autoescape=True),
                )
            )
        if status == "OVERDUE" and kind == "TASK":
            conditions += [
                p["dueDate"].as_string() < datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                p["status"].as_string() != "DONE",
                p["reviewStatus"].as_string() == "CONFIRMED",
            ]
        elif status in {"SYNC_FAILED", "DELIVERY_FAILED"} and kind == "TASK":
            conditions.append(
                or_(
                    p["delivery"]["syncStatus"].as_string() == "FAILED",
                    p["reviewStatus"].as_string() == "DELIVERY_FAILED",
                )
            )
        elif status:
            field = "reviewStatus" if kind == "TASK" else "status"
            conditions.append(p[field].as_string() == status)
        query = (
            select(ManagedMeetingItem, ManagedMeeting.title, ManagedMeeting.owner_user_id)
            .join(ManagedMeeting)
            .where(*conditions)
        )
        total = await self.db.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await self.db.execute(
                query.order_by(
                    ManagedMeetingItem.updated_at.desc(), ManagedMeetingItem.meeting_id, ManagedMeetingItem.item_id
                )
                .offset(offset)
                .limit(limit)
            )
        ).all()
        return {
            "items": [
                {
                    **i.payload,
                    "meetingId": i.meeting_id,
                    "meetingTitle": title,
                    "ownerUserId": owner_id,
                    "updatedAt": iso(i.updated_at),
                }
                for i, title, owner_id in rows
            ],
            "total": total,
        }
