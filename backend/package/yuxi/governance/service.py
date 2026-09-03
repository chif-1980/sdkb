from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuxi.governance.comparator import CrossDocumentComparisonService
from yuxi.governance.content_quality import assess_content
from yuxi.governance.domain import (
    CrossDocumentRelationType,
    ProblemTag,
    ReviewDecision,
)
from yuxi.governance.schemas import ReviewResolveRequest
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuGovernanceReview,
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSource,
    FeishuSourceItem,
)
from yuxi.utils.datetime_utils import coerce_datetime


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _relation_problem_tag(relation_type: str) -> str | None:
    return {
        CrossDocumentRelationType.EXACT_DUPLICATE: ProblemTag.DUPLICATE,
        CrossDocumentRelationType.OVERLAP: ProblemTag.OVERLAP,
        CrossDocumentRelationType.CONFLICT: ProblemTag.CONFLICT,
        CrossDocumentRelationType.INSUFFICIENT: ProblemTag.INSUFFICIENT_EVIDENCE,
    }.get(relation_type)


class GovernanceService:
    TERMINAL_REVIEW_STATUSES = {"resolved", "rejected", "changes_requested"}

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def has_open_conflict(self, version_id: str) -> bool:
        return await CrossDocumentComparisonService(self.session).has_open_conflict(version_id)

    @staticmethod
    def content_publish_block_reason(version: FeishuMaterialVersion) -> str | None:
        params = version.processing_params or {}
        if params.get("skip_reason") == "directory":
            return "目录节点仅用于组织下级内容，无需发布"
        quality = params.get("content_quality") or {}
        if not quality.get("checked"):
            return "正文检查尚未完成，不能发布"
        if not quality.get("has_body"):
            return "资料只有标题、没有可审核正文，不能发布"
        return None

    async def ensure_content_quality(
        self,
        version: FeishuMaterialVersion,
        *,
        target_kb_id: str,
        title: str | None = None,
    ) -> dict:
        """Backfill content quality for historical parsed materials on first review."""

        params = dict(version.processing_params or {})
        existing = params.get("content_quality")
        if isinstance(existing, dict) and existing.get("checked"):
            return existing

        quality = None
        if version.yuxi_file_id and target_kb_id:
            try:
                from yuxi.knowledge.runtime import knowledge_base

                content_info = await knowledge_base.get_file_content(target_kb_id, version.yuxi_file_id)
                quality = assess_content(
                    content=content_info.get("content") if isinstance(content_info, dict) else None,
                    title=title,
                )
            except Exception as exc:  # pragma: no cover - connector failures are handled conservatively
                quality = {
                    "checked": False,
                    "has_body": False,
                    "body_length": 0,
                    "reason": f"无法读取解析正文：{exc}",
                }
        if quality is None:
            quality = {
                "checked": False,
                "has_body": False,
                "body_length": 0,
                "reason": "没有可读取的解析正文",
            }
        params["content_quality"] = quality
        version.processing_params = params
        await self.session.flush()
        return quality

    async def list_reviews(
        self,
        source_id: str,
        *,
        status: str | None = None,
        problem_tag: str | None = None,
    ) -> list[dict]:
        statement = (
            select(FeishuMaterialVersion, FeishuSourceItem, FeishuSource, FeishuGovernanceReview)
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
            .join(FeishuSource, FeishuSource.source_id == FeishuSourceItem.source_id)
            .outerjoin(FeishuGovernanceReview, FeishuGovernanceReview.version_id == FeishuMaterialVersion.version_id)
            .where(
                FeishuSourceItem.source_id == source_id,
                FeishuMaterialVersion.processing_status.in_({"parsed", "awaiting_review"}),
                FeishuMaterialVersion.review_status.in_({"pending", "changes_requested"}),
            )
            .order_by(FeishuMaterialVersion.created_at.desc())
        )
        if status:
            statement = statement.where(func.coalesce(FeishuGovernanceReview.status, "pending") == status)
        rows = (await self.session.execute(statement)).all()
        reviews = []
        for version, item, source, review in rows:
            await self.ensure_content_quality(version, target_kb_id=source.target_kb_id, title=item.title)
            relations = await self._relations_for_version(version.version_id)
            review_data = self._review_dict(version, item, source, review, relations)
            if problem_tag and problem_tag not in review_data["problem_tags"]:
                continue
            reviews.append(review_data)
        return reviews

    async def get_review(self, review_id: str) -> dict:
        version, item, source, review = await self._get_review_row(review_id)
        await self.ensure_content_quality(version, target_kb_id=source.target_kb_id, title=item.title)
        relations = await self._relations_for_version(version.version_id)
        return self._review_dict(version, item, source, review, relations)

    async def list_review_comparisons(self, review_id: str) -> list[dict]:
        version, _, _, _ = await self._get_review_row(review_id)
        relations = await self._relations_for_version(version.version_id)
        return [await self._relation_dict(relation, version.version_id) for relation in relations]

    async def list_relations(
        self,
        source_id: str,
        *,
        relation_type: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        source_version = aliased(FeishuMaterialVersion)
        target_version = aliased(FeishuMaterialVersion)
        source_item = aliased(FeishuSourceItem)
        target_item = aliased(FeishuSourceItem)
        statement = (
            select(
                FeishuCrossDocumentRelation,
                source_version,
                source_item,
                target_version,
                target_item,
            )
            .join(source_version, source_version.version_id == FeishuCrossDocumentRelation.source_version_id)
            .join(source_item, source_item.item_id == source_version.item_id)
            .join(target_version, target_version.version_id == FeishuCrossDocumentRelation.target_version_id)
            .join(target_item, target_item.item_id == target_version.item_id)
            .where(
                FeishuCrossDocumentRelation.status != "invalidated",
                or_(source_item.source_id == source_id, target_item.source_id == source_id),
            )
            .order_by(FeishuCrossDocumentRelation.created_at.desc())
        )
        if relation_type:
            statement = statement.where(FeishuCrossDocumentRelation.relation_type == relation_type)
        if status:
            statement = statement.where(FeishuCrossDocumentRelation.status == status)
        else:
            statement = statement.where(FeishuCrossDocumentRelation.status != "invalidated")
        rows = (await self.session.execute(statement)).all()
        return [
            self._relation_row_dict(
                relation,
                source_version_record,
                source_item_record,
                target_version_record,
                target_item_record,
            )
            for relation, source_version_record, source_item_record, target_version_record, target_item_record in rows
        ]

    async def get_comparison_status(self, source_id: str) -> dict:
        rows = (
            await self.session.execute(
                select(FeishuMaterialVersion.processing_params)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(
                    FeishuSourceItem.source_id == source_id,
                    FeishuMaterialVersion.processing_status.in_(CrossDocumentComparisonService.CANDIDATE_STATUSES),
                )
            )
        ).all()
        counts = {"not_started": 0, "queued": 0, "running": 0, "completed": 0, "failed": 0}
        for (processing_params,) in rows:
            comparison = (processing_params or {}).get("comparison") or {}
            status = comparison.get("status") or "not_started"
            if status not in counts:
                status = "not_started"
            counts[status] += 1
        source_version = aliased(FeishuMaterialVersion)
        target_version = aliased(FeishuMaterialVersion)
        source_item = aliased(FeishuSourceItem)
        target_item = aliased(FeishuSourceItem)
        relation_count_statement = (
            select(func.count(FeishuCrossDocumentRelation.id))
            .join(source_version, source_version.version_id == FeishuCrossDocumentRelation.source_version_id)
            .join(source_item, source_item.item_id == source_version.item_id)
            .join(target_version, target_version.version_id == FeishuCrossDocumentRelation.target_version_id)
            .join(target_item, target_item.item_id == target_version.item_id)
            .where(
                FeishuCrossDocumentRelation.status != "invalidated",
                or_(source_item.source_id == source_id, target_item.source_id == source_id),
            )
        )
        relation_count = await self.session.scalar(relation_count_statement)
        issue_count = await self.session.scalar(
            relation_count_statement.where(
                FeishuCrossDocumentRelation.status == "open",
                FeishuCrossDocumentRelation.relation_type.in_(
                    {
                        CrossDocumentRelationType.CONFLICT,
                        CrossDocumentRelationType.INSUFFICIENT,
                    }
                ),
            )
        )
        total = len(rows)
        if counts["running"]:
            status = "running"
        elif counts["queued"]:
            status = "queued"
        elif counts["failed"] and counts["completed"] == 0:
            status = "failed"
        elif counts["not_started"]:
            status = "not_started"
        elif counts["failed"]:
            status = "failed"
        else:
            status = "completed" if total else "not_started"
        return {
            "status": status,
            "total": total,
            "completed": counts["completed"],
            "queued": counts["queued"],
            "running": counts["running"],
            "failed": counts["failed"],
            "not_started": counts["not_started"],
            "relation_count": int(relation_count or 0),
            "issue_count": int(issue_count or 0),
        }

    async def list_formal_knowledge(self, source_id: str) -> list[dict]:
        statement = (
            select(FeishuSourceItem, FeishuMaterialVersion, FeishuSource)
            .join(FeishuMaterialVersion, FeishuMaterialVersion.version_id == FeishuSourceItem.active_version_id)
            .join(FeishuSource, FeishuSource.source_id == FeishuSourceItem.source_id)
            .where(
                FeishuSourceItem.source_id == source_id,
                FeishuSourceItem.source_validity == "valid",
                FeishuMaterialVersion.review_status == "approved",
                FeishuMaterialVersion.published_at.is_not(None),
            )
            .order_by(FeishuMaterialVersion.published_at.desc())
        )
        rows = (await self.session.execute(statement)).all()
        version_ids = [version.version_id for _, version, _ in rows]
        item_ids = [item.item_id for item, _, _ in rows]
        pending_updates_by_item: dict[str, dict] = {}
        if item_ids:
            pending_update_rows = (
                await self.session.execute(
                    select(FeishuReviewPackage, FeishuMaterialVersion)
                    .join(
                        FeishuMaterialVersion,
                        FeishuMaterialVersion.version_id == FeishuReviewPackage.source_version_id,
                    )
                    .join(FeishuReviewItem, FeishuReviewItem.package_id == FeishuReviewPackage.package_id)
                    .where(
                        FeishuReviewPackage.source_item_id.in_(item_ids),
                        FeishuReviewPackage.workflow_status.not_in({"COMPLETED", "INVALIDATED"}),
                        FeishuMaterialVersion.version_id.not_in(version_ids),
                        FeishuMaterialVersion.review_status.in_({"pending", "changes_requested"}),
                        FeishuReviewItem.review_type == "UPDATE",
                        FeishuReviewItem.item_status.in_(
                            {
                                "PENDING",
                                "WAITING_SOURCE_CHANGE",
                                "WAITING_BUSINESS_CONFIRMATION",
                                "SOURCE_UPDATED",
                            }
                        ),
                    )
                    .order_by(FeishuReviewPackage.created_at.desc())
                )
            ).unique().all()
            source_updated_at_by_item = {item.item_id: item.source_updated_at for item, _, _ in rows}
            for package, pending_version in pending_update_rows:
                pending_updates_by_item.setdefault(
                    package.source_item_id,
                    {
                        "version_id": pending_version.version_id,
                        "revision": pending_version.revision,
                        "detected_at": _iso(pending_version.created_at),
                        "source_updated_at": _iso(source_updated_at_by_item.get(package.source_item_id)),
                        "review_package_id": package.package_id,
                    },
                )
        units_by_version: dict[str, list[FeishuKnowledgeUnit]] = {}
        if version_ids:
            units = list(
                await self.session.scalars(
                    select(FeishuKnowledgeUnit)
                    .where(
                        FeishuKnowledgeUnit.version_id.in_(version_ids),
                        FeishuKnowledgeUnit.status == "ACTIVE",
                        FeishuKnowledgeUnit.publication_state == "INCLUDED",
                    )
                    .order_by(FeishuKnowledgeUnit.version_id.asc(), FeishuKnowledgeUnit.unit_index.asc())
                )
            )
            for unit in units:
                units_by_version.setdefault(unit.version_id, []).append(unit)

        result: list[dict] = []
        for item, version, source in rows:
            units = units_by_version.get(version.version_id, [])
            if units:
                for unit in units:
                    lifecycle_status = self._unit_lifecycle_status(unit, item.publication_status)
                    indexed = (
                        item.publication_status == "ACTIVE"
                        and unit.lifecycle_status == "ACTIVE"
                        and bool(version.yuxi_file_id)
                    )
                    result.append(
                        {
                            "knowledge_id": unit.matched_logical_knowledge_id or f"{item.item_id}:{unit.lineage_key}",
                            "knowledge_level": "UNIT",
                            "unit_id": unit.unit_id,
                            "unit_type": unit.unit_type,
                            "unit_index": unit.unit_index,
                            "title": unit.title or "未命名知识单元",
                            "current_version_id": version.version_id,
                            "revision": version.revision,
                            "source_id": item.source_id,
                            "source_item_id": item.item_id,
                            "source_title": item.title or "未命名原始材料",
                            "source_url": item.source_url,
                            "wiki_path": item.path_text,
                            "source_role": "PRIMARY",
                            "source_segment_ids": list(unit.source_segment_ids or []),
                            "source_segment_count": len(unit.source_segment_ids or []),
                            "source_locator": dict(unit.locator_json or {}),
                            "applicability_scope": dict(unit.applicability_scope or {})
                            or (version.processing_params or {}).get("applicability_scope", {}),
                            "index_status": "INDEXED" if indexed else "OFFLINE",
                            "lifecycle_status": lifecycle_status,
                            "stored_lifecycle_status": unit.lifecycle_status,
                            "source_publication_status": item.publication_status,
                            "owner_id": unit.owner_id,
                            "owner_name": unit.owner_name,
                            "valid_from": _iso(unit.valid_from),
                            "valid_until": _iso(unit.valid_until),
                            "review_due_at": _iso(unit.review_due_at),
                            "lifecycle_note": unit.lifecycle_note,
                            "lifecycle_updated_by": unit.lifecycle_updated_by,
                            "lifecycle_updated_at": _iso(unit.lifecycle_updated_at),
                            "yuxi_file_id": version.yuxi_file_id,
                            "chunk_count": version.chunk_count or 0,
                            "published_at": _iso(version.published_at),
                            "updated_at": _iso(unit.updated_at or version.updated_at),
                            "target_kb_id": source.target_kb_id,
                            "pending_update": pending_updates_by_item.get(item.item_id),
                        }
                    )
                continue

            # 兼容尚未拆分知识单元的历史正式资料，避免升级后旧知识从列表消失。
            result.append(
                {
                    "knowledge_id": item.item_id,
                    "knowledge_level": "MATERIAL",
                    "unit_id": None,
                    "unit_type": None,
                    "title": item.title or "未命名知识",
                    "current_version_id": version.version_id,
                    "revision": version.revision,
                    "source_id": item.source_id,
                    "source_item_id": item.item_id,
                    "source_title": item.title or "未命名原始材料",
                    "source_url": item.source_url,
                    "wiki_path": item.path_text,
                    "source_role": "PRIMARY",
                    "source_segment_ids": [],
                    "source_segment_count": 0,
                    "source_locator": {},
                    "applicability_scope": (version.processing_params or {}).get("applicability_scope", {}),
                    "index_status": (
                        "INDEXED" if item.publication_status == "ACTIVE" and version.yuxi_file_id else "OFFLINE"
                    ),
                    "lifecycle_status": "ACTIVE" if item.publication_status == "ACTIVE" else "OFFLINE",
                    "stored_lifecycle_status": "ACTIVE",
                    "source_publication_status": item.publication_status,
                    "yuxi_file_id": version.yuxi_file_id,
                    "chunk_count": version.chunk_count or 0,
                    "published_at": _iso(version.published_at),
                    "updated_at": _iso(version.updated_at),
                    "target_kb_id": source.target_kb_id,
                    "pending_update": pending_updates_by_item.get(item.item_id),
                }
            )
        return result

    @staticmethod
    def _unit_lifecycle_status(unit: FeishuKnowledgeUnit, source_status: str | None) -> str:
        if source_status != "ACTIVE" or unit.lifecycle_status == "OFFLINE":
            return "OFFLINE"
        now = datetime.now(UTC)
        valid_until = coerce_datetime(unit.valid_until)
        if valid_until and valid_until <= now:
            return "EXPIRED"
        review_due_at = coerce_datetime(unit.review_due_at)
        if review_due_at and review_due_at <= now:
            return "REVIEW_DUE"
        return "ACTIVE"

    async def list_knowledge_versions(self, knowledge_id: str) -> list[dict]:
        item = await self.session.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == knowledge_id))
        if item is None:
            raise LookupError(f"Formal knowledge not found: {knowledge_id}")
        versions = (
            await self.session.scalars(
                select(FeishuMaterialVersion)
                .where(FeishuMaterialVersion.item_id == knowledge_id)
                .order_by(FeishuMaterialVersion.created_at.desc())
            )
        ).all()
        return [
            {
                "version_id": version.version_id,
                "revision": version.revision,
                "processing_status": version.processing_status,
                "review_status": version.review_status,
                "active": item.active_version_id == version.version_id,
                "rollback_available": (
                    item.active_version_id != version.version_id
                    and version.review_status == "approved"
                    and version.published_at is not None
                    and bool(version.source_object_path)
                ),
                "yuxi_file_id": version.yuxi_file_id,
                "published_at": _iso(version.published_at),
                "created_at": _iso(version.created_at),
            }
            for version in versions
        ]

    async def list_knowledge_relations(self, knowledge_id: str) -> list[dict]:
        version_ids = list(
            await self.session.scalars(
                select(FeishuMaterialVersion.version_id).where(FeishuMaterialVersion.item_id == knowledge_id)
            )
        )
        if not version_ids:
            raise LookupError(f"Formal knowledge not found: {knowledge_id}")
        relations = (
            await self.session.scalars(
                select(FeishuCrossDocumentRelation)
                .where(
                    FeishuCrossDocumentRelation.status != "invalidated",
                    or_(
                        FeishuCrossDocumentRelation.source_version_id.in_(version_ids),
                        FeishuCrossDocumentRelation.target_version_id.in_(version_ids),
                    ),
                )
                .order_by(FeishuCrossDocumentRelation.created_at.desc())
            )
        ).all()
        return [await self._relation_dict(relation, version_ids[0]) for relation in relations]

    async def prepare_resolution(
        self,
        review_id: str,
        *,
        operator_id: str,
    ) -> tuple[FeishuGovernanceReview, FeishuMaterialVersion, FeishuSourceItem]:
        version, item, _, review = await self._get_review_row(review_id, lock=True)
        if review is None:
            review = FeishuGovernanceReview(
                review_id=f"review-{uuid4().hex}",
                version_id=version.version_id,
                status="pending",
            )
            self.session.add(review)
            await self.session.flush()
        if review.status in self.TERMINAL_REVIEW_STATUSES:
            raise ValueError("Review task is already completed")
        if review.assignee_id and review.assignee_id != operator_id:
            raise PermissionError("Review task is assigned to another reviewer")
        return review, version, item

    async def record_resolution(
        self,
        review: FeishuGovernanceReview,
        version: FeishuMaterialVersion,
        item: FeishuSourceItem,
        payload: ReviewResolveRequest,
        *,
        operator_id: str,
    ) -> None:
        now = datetime.now(UTC)
        review.decision = payload.decision
        review.action = payload.action
        review.problem_tags = [tag.value for tag in payload.problem_tags]
        review.decision_comment = payload.decision_comment
        review.applicability_scope = payload.applicability_scope.model_dump(mode="json", exclude_none=True)

        if payload.decision == ReviewDecision.TRANSFER:
            review.status = "pending"
            review.assignee_id = payload.assignee_id
            review.decided_by = None
            review.decided_at = None
            self._append_event(
                version,
                item,
                event_type="review_transferred",
                operator_id=operator_id,
                message=payload.decision_comment,
                payload={"assignee_id": payload.assignee_id},
            )
        elif payload.decision == ReviewDecision.REQUEST_CHANGES:
            review.status = "changes_requested"
            review.decided_by = operator_id
            review.decided_at = now
            version.review_status = "changes_requested"
            version.reviewer_id = operator_id
            version.reviewed_at = now
            version.review_comment = payload.decision_comment
            self._append_event(
                version,
                item,
                event_type="changes_requested",
                operator_id=operator_id,
                message=payload.decision_comment,
            )
        else:
            review.status = "rejected" if payload.decision == ReviewDecision.REJECT else "resolved"
            review.decided_by = operator_id
            review.decided_at = now

        processing_params = dict(version.processing_params or {})
        processing_params["applicability_scope"] = review.applicability_scope
        version.processing_params = processing_params
        await self._resolve_related_comparisons(version.version_id, payload, operator_id=operator_id, now=now)
        await self.session.flush()

    async def _get_review_row(self, review_id: str, *, lock: bool = False):
        statement = (
            select(FeishuMaterialVersion, FeishuSourceItem, FeishuSource, FeishuGovernanceReview)
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
            .join(FeishuSource, FeishuSource.source_id == FeishuSourceItem.source_id)
            .outerjoin(FeishuGovernanceReview, FeishuGovernanceReview.version_id == FeishuMaterialVersion.version_id)
            .where(
                or_(
                    FeishuGovernanceReview.review_id == review_id,
                    FeishuMaterialVersion.version_id == review_id,
                )
            )
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            raise LookupError(f"Review task not found: {review_id}")
        return row

    async def _relations_for_version(self, version_id: str) -> list[FeishuCrossDocumentRelation]:
        return list(
            await self.session.scalars(
                select(FeishuCrossDocumentRelation)
                .where(
                    FeishuCrossDocumentRelation.status != "invalidated",
                    or_(
                        FeishuCrossDocumentRelation.source_version_id == version_id,
                        FeishuCrossDocumentRelation.target_version_id == version_id,
                    ),
                )
                .order_by(FeishuCrossDocumentRelation.created_at.desc())
            )
        )

    async def _relation_dict(self, relation: FeishuCrossDocumentRelation, current_version_id: str) -> dict:
        source_version, source_item = await self._version_item(relation.source_version_id)
        target_version, target_item = await self._version_item(relation.target_version_id)
        data = self._relation_row_dict(relation, source_version, source_item, target_version, target_item)
        data["current_side"] = "source" if relation.source_version_id == current_version_id else "target"
        data["source_revision"] = source_version.revision
        data["target_revision"] = target_version.revision
        return data

    async def _version_item(self, version_id: str) -> tuple[FeishuMaterialVersion, FeishuSourceItem]:
        row = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuMaterialVersion.version_id == version_id)
            )
        ).one()
        return row

    def _relation_row_dict(
        self,
        relation: FeishuCrossDocumentRelation,
        source_version: FeishuMaterialVersion,
        source_item: FeishuSourceItem,
        target_version: FeishuMaterialVersion,
        target_item: FeishuSourceItem,
    ) -> dict:
        return {
            "relation_id": relation.relation_id,
            "source_version_id": relation.source_version_id,
            "target_version_id": relation.target_version_id,
            "source_title": source_item.title or "未命名资料",
            "target_title": target_item.title or "未命名资料",
            "source_url": source_item.source_url,
            "target_url": target_item.source_url,
            "source_path": source_item.path_text,
            "target_path": target_item.path_text,
            "source_processing_status": source_version.processing_status,
            "source_review_status": source_version.review_status,
            "target_processing_status": target_version.processing_status,
            "target_review_status": target_version.review_status,
            "relation_type": relation.relation_type,
            "similarity": relation.similarity,
            "confidence": relation.confidence,
            "same_content": relation.same_content or [],
            "different_content": relation.different_content or [],
            "scope_difference": relation.scope_difference or {},
            "reasoning": relation.reasoning,
            "status": relation.status,
            "human_decision": relation.human_decision,
            "human_comment": relation.human_comment,
            "resolved_by": relation.resolved_by,
            "resolved_at": _iso(relation.resolved_at),
            "created_at": _iso(relation.created_at),
        }

    def _review_dict(
        self,
        version: FeishuMaterialVersion,
        item: FeishuSourceItem,
        source: FeishuSource,
        review: FeishuGovernanceReview | None,
        relations: list[FeishuCrossDocumentRelation],
    ) -> dict:
        relation_types = {relation.relation_type for relation in relations if relation.status == "open"}
        derived_tags = {_relation_problem_tag(relation_type) for relation_type in relation_types}
        problem_tags = set(review.problem_tags if review else []) | {
            str(tag) for tag in derived_tags if tag is not None
        }
        risk_level = "HIGH" if CrossDocumentRelationType.CONFLICT in relation_types else "MEDIUM"
        quality = (version.processing_params or {}).get("content_quality") or {}
        is_directory = item.item_type == "directory" or quality.get("classification") == "directory"
        content_missing = not is_directory and quality.get("checked") and not quality.get("has_body")
        content_unchecked = not is_directory and not quality.get("checked")
        if content_missing:
            problem_tags.add(ProblemTag.CONTENT_MISSING)
            risk_level = "HIGH"
        elif content_unchecked:
            risk_level = "HIGH"
        if not relation_types or relation_types == {CrossDocumentRelationType.EXACT_DUPLICATE}:
            risk_level = "HIGH" if content_missing or content_unchecked else "LOW"
        return {
            "review_id": review.review_id if review else version.version_id,
            "version_id": version.version_id,
            "item_id": item.item_id,
            "source_id": item.source_id,
            "target_kb_id": source.target_kb_id,
            "title": item.title or "未命名素材",
            "item_type": item.item_type,
            "wiki_path": item.path_text,
            "source_url": item.source_url,
            "revision": version.revision,
            "processing_status": version.processing_status,
            "review_status": version.review_status,
            "status": review.status if review else "pending",
            "yuxi_file_id": version.yuxi_file_id,
            "chunk_count": version.chunk_count or 0,
            "token_count": version.token_count or 0,
            "content_quality": quality,
            "is_directory": is_directory,
            "content_missing": bool(content_missing),
            "content_check_pending": bool(content_unchecked),
            "assignee_id": review.assignee_id if review else None,
            "last_decision": review.decision if review else None,
            "last_action": review.action if review else None,
            "decision_comment": review.decision_comment if review else version.review_comment,
            "problem_tags": sorted(str(tag) for tag in problem_tags),
            "applicability_scope": (
                review.applicability_scope
                if review
                else (version.processing_params or {}).get("applicability_scope", {})
            ),
            "relation_types": sorted(relation_types),
            "comparison_count": len(relations),
            "comparison_status": (
                ((version.processing_params or {}).get("comparison") or {}).get("status") or "not_started"
            ),
            "risk_level": risk_level,
            "source_updated_at": _iso(item.source_updated_at),
            "created_at": _iso(version.created_at),
            "updated_at": _iso(version.updated_at),
        }

    async def _resolve_related_comparisons(
        self,
        version_id: str,
        payload: ReviewResolveRequest,
        *,
        operator_id: str,
        now: datetime,
    ) -> None:
        if payload.decision == ReviewDecision.TRANSFER:
            return
        relations = await self._relations_for_version(version_id)
        for relation in relations:
            if relation.status != "open":
                continue
            relation.status = "resolved"
            relation.human_decision = payload.action
            relation.human_comment = payload.decision_comment
            relation.resolved_by = operator_id
            relation.resolved_at = now

    def _append_event(
        self,
        version: FeishuMaterialVersion,
        item: FeishuSourceItem,
        *,
        event_type: str,
        operator_id: str,
        message: str | None,
        payload: dict | None = None,
    ) -> None:
        self.session.add(
            FeishuProcessingEvent(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type=event_type,
                from_status=version.review_status,
                to_status=version.review_status,
                operator_id=operator_id,
                message=message,
                payload_json=payload or {},
            )
        )
