from __future__ import annotations

from collections import Counter
from datetime import UTC, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.governance.domain import ReviewPackageStatus, ReviewTriggerType, SourceChangeRequestStatus
from yuxi.governance.knowledge_unit_service import KnowledgeUnitService
from yuxi.governance.quality_gate_service import QualityGateService
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
)
from yuxi.utils.datetime_utils import utc_now_naive


TERMINAL_PACKAGE_STATUSES = {ReviewPackageStatus.COMPLETED, ReviewPackageStatus.INVALIDATED}
OPEN_CHANGE_STATUSES = {
    SourceChangeRequestStatus.OPEN,
    SourceChangeRequestStatus.NEW_VERSION_RECEIVED,
}
FAILED_PROCESSING_STATUSES = {"parse_failed", "publish_failed"}
RISK_SLA_DAYS = {"HIGH": 1, "MEDIUM": 3, "LOW": 7}


def _db_utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class WorkItemService:
    """Aggregate existing governance objects without creating a second task state."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_work_items(
        self,
        *,
        operator_id: str,
        source_id: str | None = None,
        assignee: str | None = None,
        work_type: str | None = None,
        risk: str | None = None,
        status: str | None = None,
        overdue: bool | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        items = await self._aggregate(source_id=source_id)
        filtered = [
            item
            for item in items
            if self._matches(
                item,
                operator_id=operator_id,
                assignee=assignee,
                work_type=work_type,
                risk=risk,
                status=status,
                overdue=overdue,
            )
        ]
        filtered.sort(key=lambda item: (not item["overdue"], self._risk_rank(item["risk"]), item["createdAt"]))
        total = len(filtered)
        offset = (page - 1) * page_size
        return {
            "items": filtered[offset : offset + page_size],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }

    async def summary(
        self,
        *,
        operator_id: str,
        source_id: str | None = None,
        assignee: str | None = None,
    ) -> dict:
        result = await self.list_work_items(
            operator_id=operator_id,
            source_id=source_id,
            assignee=assignee,
            page=1,
            page_size=10000,
        )
        items = result["items"]
        return {
            "total": len(items),
            "overdue": sum(item["overdue"] for item in items),
            "unassigned": sum(item["assigneeId"] is None for item in items),
            "byType": dict(Counter(item["type"] for item in items)),
            "byRisk": dict(Counter(item["risk"] for item in items)),
        }

    async def _aggregate(self, *, source_id: str | None) -> list[dict]:
        return [
            *await self._review_packages(source_id),
            *await self._source_changes(source_id),
            *await self._relations(source_id),
            *await self._failed_versions(source_id),
            *await self._due_units(source_id),
        ]

    async def _review_packages(self, source_id: str | None) -> list[dict]:
        statement = select(FeishuReviewPackage).where(
            FeishuReviewPackage.workflow_status.not_in(
                {*TERMINAL_PACKAGE_STATUSES, ReviewPackageStatus.WAITING_SOURCE_CHANGE}
            )
        )
        if source_id:
            statement = statement.where(FeishuReviewPackage.source_id == source_id)
        packages = list(await self.session.scalars(statement))
        quality_service = QualityGateService(self.session)
        unit_service = KnowledgeUnitService(self.session)
        for package in packages:
            if package.quality_computed_at is None:
                await unit_service.ensure_for_package(package)
                await quality_service.evaluate_package(package.package_id)
        return [
            self._item(
                work_id=f"review:{package.package_id}",
                work_type="REVIEW",
                title=package.title_snapshot or "未命名审核包",
                source_id=package.source_id,
                source_title=package.title_snapshot,
                source_path=package.path_snapshot,
                risk=package.risk_level or "MEDIUM",
                status=package.workflow_status,
                assignee_id=package.assignee_id,
                created_at=package.created_at,
                updated_at=package.updated_at,
                due_at=self._package_due_at(package),
                ai_summary=self._review_summary(package),
                suggested_action="复核 AI 建议与证据后完成审核",
                block_reasons=[
                    blocker.get("message")
                    for blocker in ((package.impact_summary or {}).get("blockReasons") or [])
                    if blocker.get("message")
                ],
                navigation={"module": "review", "packageId": package.package_id},
                quality_gate=package.quality_gate_status,
                quality_score=package.quality_score,
            )
            for package in packages
        ]

    async def _source_changes(self, source_id: str | None) -> list[dict]:
        statement = (
            select(FeishuSourceChangeRequest, FeishuReviewItem, FeishuReviewPackage)
            .join(FeishuReviewItem, FeishuReviewItem.review_item_id == FeishuSourceChangeRequest.review_item_id)
            .join(FeishuReviewPackage, FeishuReviewPackage.package_id == FeishuReviewItem.package_id)
            .where(FeishuSourceChangeRequest.status.in_(OPEN_CHANGE_STATUSES))
        )
        if source_id:
            statement = statement.where(FeishuReviewPackage.source_id == source_id)
        rows = (await self.session.execute(statement)).all()
        return [
            self._item(
                work_id=f"source-change:{request.change_request_id}",
                work_type=("USER_FEEDBACK" if package.trigger_type == ReviewTriggerType.FEEDBACK else "SOURCE_CHANGE"),
                title=item.title or package.title_snapshot or "来源修改请求",
                source_id=package.source_id,
                source_title=package.title_snapshot,
                source_path=package.path_snapshot,
                risk=package.risk_level or "MEDIUM",
                status=request.status,
                assignee_id=request.responsible_user_id or package.assignee_id,
                created_at=request.created_at,
                updated_at=request.updated_at,
                due_at=request.created_at + timedelta(days=3),
                ai_summary=request.request_text,
                suggested_action="跟进来源负责人更新并重新核验",
                block_reasons=["等待来源材料修正"],
                navigation={
                    "module": "source-change",
                    "packageId": package.package_id,
                    "changeRequestId": request.change_request_id,
                },
            )
            for request, item, package in rows
        ]

    async def _relations(self, source_id: str | None) -> list[dict]:
        source_version = aliased(FeishuMaterialVersion)
        target_version = aliased(FeishuMaterialVersion)
        source_item = aliased(FeishuSourceItem)
        target_item = aliased(FeishuSourceItem)
        statement = (
            select(FeishuCrossDocumentRelation, source_item, target_item)
            .join(
                source_version,
                source_version.version_id == FeishuCrossDocumentRelation.source_version_id,
            )
            .join(source_item, source_item.item_id == source_version.item_id)
            .join(
                target_version,
                target_version.version_id == FeishuCrossDocumentRelation.target_version_id,
            )
            .join(target_item, target_item.item_id == target_version.item_id)
            .where(FeishuCrossDocumentRelation.status.in_({"open", "pending"}))
        )
        if source_id:
            statement = statement.where(
                or_(source_item.source_id == source_id, target_item.source_id == source_id)
            )
        rows = (await self.session.execute(statement)).all()
        return [
            self._relation_item(relation, source_item, target_item, source_id=source_id)
            for relation, source_item, target_item in rows
        ]

    def _relation_item(
        self,
        relation: FeishuCrossDocumentRelation,
        source_item: FeishuSourceItem,
        target_item: FeishuSourceItem,
        *,
        source_id: str | None,
    ) -> dict:
        # When a source filter matches the target side, make that side the
        # displayed material so the work-item row remains consistent with the
        # selected source.  Without a filter, retain the relation's source side.
        selected_item = (
            target_item
            if source_id and source_item.source_id != source_id and target_item.source_id == source_id
            else source_item
        )
        counterpart = target_item if selected_item is source_item else source_item
        relation_summary = relation.reasoning or f"检测到 {relation.relation_type} 跨文档关系"
        if counterpart.title:
            relation_summary = f"{relation_summary}（关联：{counterpart.title}）"
        return self._item(
            work_id=f"relation:{relation.relation_id}",
            work_type="CONFLICT",
            title=selected_item.title or "跨文档关系",
            source_id=selected_item.source_id,
            source_title=selected_item.title,
            source_path=selected_item.path_text,
            risk="HIGH" if relation.relation_type == "CONFLICT" else "MEDIUM",
            status=relation.status.upper(),
            assignee_id=None,
            created_at=relation.created_at,
            updated_at=relation.updated_at,
            due_at=relation.created_at + timedelta(days=1 if relation.relation_type == "CONFLICT" else 3),
            ai_summary=relation_summary,
            suggested_action="核对两份来源证据并处理关系",
            block_reasons=["未解决的跨文档冲突"] if relation.relation_type == "CONFLICT" else [],
            navigation={"module": "relation", "relationId": relation.relation_id},
        )

    async def _failed_versions(self, source_id: str | None) -> list[dict]:
        statement = (
            select(FeishuMaterialVersion, FeishuSourceItem)
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
            .where(FeishuMaterialVersion.processing_status.in_(FAILED_PROCESSING_STATUSES))
        )
        if source_id:
            statement = statement.where(FeishuSourceItem.source_id == source_id)
        rows = (await self.session.execute(statement)).all()
        return [
            self._item(
                work_id=f"processing:{version.version_id}",
                work_type="PROCESSING_FAILURE",
                title=source_item.title or "资料加工失败",
                source_id=source_item.source_id,
                source_title=source_item.title,
                source_path=source_item.path_text,
                risk="HIGH" if (version.retry_count or 0) >= 3 else "MEDIUM",
                status="RETRY_EXHAUSTED" if (version.retry_count or 0) >= 3 else "RETRYING",
                assignee_id=None,
                created_at=version.created_at,
                updated_at=version.updated_at,
                due_at=version.updated_at + timedelta(minutes=15),
                ai_summary=version.error_message or "资料解析或发布失败",
                suggested_action=(
                    "检查失败原因并人工处理" if (version.retry_count or 0) >= 3 else "系统将按 1、5、15 分钟自动重试"
                ),
                block_reasons=[version.error_message or "资料加工失败"],
                navigation={"module": "material", "versionId": version.version_id},
            )
            for version, source_item in rows
        ]

    async def _due_units(self, source_id: str | None) -> list[dict]:
        now = _db_utc(utc_now_naive())
        statement = (
            select(FeishuKnowledgeUnit, FeishuSourceItem)
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuKnowledgeUnit.item_id)
            .where(
                FeishuKnowledgeUnit.status == "ACTIVE",
                FeishuKnowledgeUnit.publication_state == "INCLUDED",
                FeishuKnowledgeUnit.review_due_at.is_not(None),
                FeishuKnowledgeUnit.review_due_at <= now + timedelta(days=30),
            )
        )
        if source_id:
            statement = statement.where(FeishuSourceItem.source_id == source_id)
        rows = (await self.session.execute(statement)).all()
        return [
            self._item(
                work_id=f"expiry:{unit.unit_id}",
                work_type="EXPIRY_REVIEW",
                title=unit.title,
                source_id=source_item.source_id,
                source_title=source_item.title,
                source_path=source_item.path_text,
                risk="HIGH" if _db_utc(unit.review_due_at) < now else "MEDIUM",
                status="OVERDUE" if _db_utc(unit.review_due_at) < now else "DUE_SOON",
                assignee_id=unit.owner_id,
                created_at=unit.created_at,
                updated_at=unit.updated_at,
                due_at=unit.review_due_at,
                ai_summary="正式知识已到期或将在 30 天内到期，需要确认有效性",
                suggested_action="确认仍然有效、修订或下架",
                block_reasons=[],
                navigation={"module": "knowledge", "unitId": unit.unit_id},
            )
            for unit, source_item in rows
        ]

    @staticmethod
    def _item(
        *,
        work_id: str,
        work_type: str,
        title: str,
        source_id: str,
        source_title: str | None,
        source_path: str | None,
        risk: str,
        status: str,
        assignee_id: str | None,
        created_at,
        updated_at,
        due_at,
        ai_summary: str,
        suggested_action: str,
        block_reasons: list[str],
        navigation: dict,
        quality_gate: str | None = None,
        quality_score: int | None = None,
    ) -> dict:
        now = _db_utc(utc_now_naive())
        normalized_due_at = _db_utc(due_at)
        return {
            "id": work_id,
            "type": work_type,
            "title": title,
            "source": {"sourceId": source_id, "title": source_title, "path": source_path},
            "risk": risk,
            "status": str(status),
            "assigneeId": assignee_id,
            "assigneePool": "ADMIN" if assignee_id is None else None,
            "overdue": bool(normalized_due_at and normalized_due_at < now),
            "dueAt": normalized_due_at.isoformat() if normalized_due_at else None,
            "aiSummary": ai_summary,
            "suggestedAction": suggested_action,
            "blockReasons": block_reasons,
            "navigation": navigation,
            "qualityGate": quality_gate,
            "qualityScore": quality_score,
            "createdAt": _db_utc(created_at).isoformat(),
            "updatedAt": _db_utc(updated_at).isoformat(),
        }

    @staticmethod
    def _matches(
        item: dict,
        *,
        operator_id: str,
        assignee: str | None,
        work_type: str | None,
        risk: str | None,
        status: str | None,
        overdue: bool | None,
    ) -> bool:
        if assignee == "mine" and item["assigneeId"] not in {None, operator_id}:
            return False
        if assignee == "unassigned" and item["assigneeId"] is not None:
            return False
        if assignee and assignee not in {"mine", "unassigned"} and item["assigneeId"] != assignee:
            return False
        if work_type and item["type"] != work_type:
            return False
        if risk and item["risk"] != risk:
            return False
        if status and item["status"] != status:
            return False
        return overdue is None or item["overdue"] is overdue

    @staticmethod
    def _package_due_at(package: FeishuReviewPackage):
        return package.created_at + timedelta(days=RISK_SLA_DAYS.get(package.risk_level or "MEDIUM", 3))

    @staticmethod
    def _review_summary(package: FeishuReviewPackage) -> str:
        impact = package.impact_summary or {}
        affected = int(impact.get("affectedKnowledgeCount") or 0)
        if affected:
            return f"来源变化影响 {affected} 个知识单元，等待审核确认"
        return "审核包等待人工核验 AI 建议、来源证据与质量门禁"

    @staticmethod
    def _risk_rank(risk: str) -> int:
        return {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(risk, 3)
