from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.governance.domain import CrossDocumentRelationType, ReviewSubjectType
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSourceItem,
    FeishuSourceSegment,
)
from yuxi.utils.datetime_utils import utc_now_naive


QUALITY_DIMENSION_MAX = {
    "traceability": 30,
    "completeness": 25,
    "consistency": 20,
    "timeliness": 15,
    "governance": 10,
}
FAILED_PROCESSING_STATUSES = {"parse_failed", "publish_failed"}
OPEN_RELATION_STATUSES = {"open", "pending"}
IMAGE_SEGMENT_TYPES = {"image", "figure", "ocr", "media"}


def _db_utc(value):
    """Database timestamps are UTC-naive in legacy tables."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def score_quality_dimensions(
    *,
    unit_count: int,
    traceable_unit_count: int,
    content_unit_count: int,
    parsing_complete: bool,
    open_relation_count: int,
    open_conflict_count: int,
    source_valid: bool,
    overdue_unit_count: int,
    assignee_present: bool,
    owned_unit_count: int,
    source_metadata_present: bool,
) -> dict[str, dict]:
    """Calculate the five documented quality dimensions without database access."""
    denominator = max(unit_count, 1)
    traceability = round(QUALITY_DIMENSION_MAX["traceability"] * traceable_unit_count / denominator)
    completeness = (
        round(QUALITY_DIMENSION_MAX["completeness"] * content_unit_count / denominator) if parsing_complete else 0
    )
    consistency = 0 if open_conflict_count else (10 if open_relation_count else 20)
    timeliness = 0 if not source_valid else (8 if overdue_unit_count else 15)
    governance = (4 if assignee_present else 0) + round(4 * owned_unit_count / denominator)
    if source_metadata_present:
        governance += 2
    return {
        "traceability": {
            "label": "证据与可追溯性",
            "score": traceability,
            "maxScore": QUALITY_DIMENSION_MAX["traceability"],
        },
        "completeness": {
            "label": "内容完整性",
            "score": completeness,
            "maxScore": QUALITY_DIMENSION_MAX["completeness"],
        },
        "consistency": {
            "label": "一致性与重复冲突",
            "score": consistency,
            "maxScore": QUALITY_DIMENSION_MAX["consistency"],
        },
        "timeliness": {
            "label": "时效性与有效期",
            "score": timeliness,
            "maxScore": QUALITY_DIMENSION_MAX["timeliness"],
        },
        "governance": {
            "label": "治理元数据",
            "score": governance,
            "maxScore": QUALITY_DIMENSION_MAX["governance"],
        },
    }


def gate_status(score: int, blockers: list[dict]) -> str:
    if blockers:
        return "BLOCKED"
    if score >= 80:
        return "RECOMMENDED"
    if score >= 60:
        return "REVIEW"
    return "RETURN"


class QualityGateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def evaluate_package(self, package_id: str) -> dict:
        package = await self.session.scalar(
            select(FeishuReviewPackage).where(FeishuReviewPackage.package_id == package_id)
        )
        if package is None:
            raise LookupError(f"Review package not found: {package_id}")

        review_items = list(
            await self.session.scalars(
                select(FeishuReviewItem).where(FeishuReviewItem.package_id == package.package_id)
            )
        )
        unit_ids = [item.subject_id for item in review_items if item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT]
        units = (
            list(
                await self.session.scalars(
                    select(FeishuKnowledgeUnit).where(
                        FeishuKnowledgeUnit.unit_id.in_(unit_ids),
                        FeishuKnowledgeUnit.status == "ACTIVE",
                    )
                )
            )
            if unit_ids
            else []
        )
        version, source_item = await self._source_context(package)
        relations = await self._open_relations(package.source_version_id, review_items)
        previous_version = await self._previous_version(version, source_item)
        current_segments = await self._segments(version.version_id if version else None)
        previous_segments = await self._segments(previous_version.version_id if previous_version else None)

        result = self._evaluate(
            package=package,
            version=version,
            source_item=source_item,
            units=units,
            review_items=review_items,
            relations=relations,
            current_segments=current_segments,
            previous_segments=previous_segments,
            previous_version=previous_version,
        )
        package.quality_gate_status = result["qualityGate"]["status"]
        package.quality_score = result["qualityScore"]
        package.quality_dimensions = result["qualityDimensions"]
        package.impact_summary = result["impactSummary"]
        package.auto_close_eligible = result["autoCloseEligible"]
        package.quality_computed_at = utc_now_naive()
        await self.session.flush()
        result["qualityComputedAt"] = package.quality_computed_at.isoformat()
        return result

    async def assert_version_publishable(self, version_id: str) -> None:
        row = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuMaterialVersion.version_id == version_id)
            )
        ).one_or_none()
        if row is None:
            raise LookupError(f"Feishu material not found: {version_id}")
        version, source_item = row
        units = list(
            await self.session.scalars(
                select(FeishuKnowledgeUnit).where(
                    FeishuKnowledgeUnit.version_id == version_id,
                    FeishuKnowledgeUnit.status == "ACTIVE",
                    FeishuKnowledgeUnit.publication_state.in_({"PENDING", "INCLUDED"}),
                )
            )
        )
        relations = list(
            await self.session.scalars(
                select(FeishuCrossDocumentRelation).where(
                    or_(
                        FeishuCrossDocumentRelation.source_version_id == version_id,
                        FeishuCrossDocumentRelation.target_version_id == version_id,
                    ),
                    FeishuCrossDocumentRelation.status.in_(OPEN_RELATION_STATUSES),
                )
            )
        )
        blockers = self._hard_blockers(version, source_item, units, relations)
        if blockers:
            raise ValueError("质量门禁未通过：" + "；".join(blocker["message"] for blocker in blockers))

    def _evaluate(
        self,
        *,
        package: FeishuReviewPackage,
        version: FeishuMaterialVersion | None,
        source_item: FeishuSourceItem | None,
        units: list[FeishuKnowledgeUnit],
        review_items: list[FeishuReviewItem],
        relations: list[FeishuCrossDocumentRelation],
        current_segments: list[FeishuSourceSegment],
        previous_segments: list[FeishuSourceSegment],
        previous_version: FeishuMaterialVersion | None,
    ) -> dict:
        units = [unit for unit in units if unit.publication_state in {"PENDING", "INCLUDED"}]
        blockers = self._hard_blockers(version, source_item, units, relations)
        now = _db_utc(utc_now_naive())
        traceable_count = sum(bool(unit.source_segment_ids and unit.locator_json) for unit in units)
        content_count = sum(bool((unit.content or "").strip()) for unit in units)
        overdue_count = sum(bool(unit.review_due_at and _db_utc(unit.review_due_at) < now) for unit in units)
        open_conflicts = [
            relation for relation in relations if relation.relation_type == CrossDocumentRelationType.CONFLICT
        ]
        quality = (version.processing_params or {}).get("content_quality") or {} if version else {}
        parsing_complete = bool(
            version
            and version.processing_status not in FAILED_PROCESSING_STATUSES
            and quality.get("checked")
            and quality.get("has_body")
        )
        dimensions = score_quality_dimensions(
            unit_count=len(units),
            traceable_unit_count=traceable_count,
            content_unit_count=content_count,
            parsing_complete=parsing_complete,
            open_relation_count=len(relations),
            open_conflict_count=len(open_conflicts),
            source_valid=bool(source_item and source_item.source_validity == "valid"),
            overdue_unit_count=overdue_count,
            assignee_present=bool(package.assignee_id),
            owned_unit_count=sum(bool(unit.owner_id or unit.owner_name) for unit in units),
            source_metadata_present=bool(package.title_snapshot and package.path_snapshot),
        )
        score = sum(int(dimension["score"]) for dimension in dimensions.values())
        change_counts = Counter((unit.change_type or "UNKNOWN").upper() for unit in units)
        text_changed = self._content_signature(current_segments) != self._content_signature(previous_segments)
        image_changed = self._image_signature(current_segments) != self._image_signature(previous_segments)
        layout_changed = self._layout_signature(current_segments) != self._layout_signature(previous_segments)
        has_previous_evidence = bool(previous_version and previous_segments and current_segments)
        all_units_unchanged = bool(units) and all((unit.change_type or "").upper() == "UNCHANGED" for unit in units)
        auto_close_eligible = bool(
            has_previous_evidence
            and parsing_complete
            and not blockers
            and not relations
            and all_units_unchanged
            and not text_changed
            and not image_changed
            and not layout_changed
        )
        impact = {
            "knowledgeUnits": {
                "new": change_counts.get("NEW", 0),
                "modified": change_counts.get("MODIFIED", 0) + change_counts.get("UPDATED", 0),
                "deleted": change_counts.get("DELETED", 0),
                "unchanged": change_counts.get("UNCHANGED", 0),
            },
            "openRelationCount": len(relations),
            "openRelationTypes": dict(Counter(relation.relation_type for relation in relations)),
            "textChanged": text_changed if has_previous_evidence else None,
            "imageChanged": image_changed if has_previous_evidence else None,
            "layoutChanged": layout_changed if has_previous_evidence else None,
            "affectedKnowledgeCount": sum(count for change, count in change_counts.items() if change != "UNCHANGED"),
            "parsingComplete": parsing_complete,
            "previousVersionId": previous_version.version_id if previous_version else None,
            "blockReasons": blockers,
        }
        return {
            "qualityGate": {"status": gate_status(score, blockers), "blockers": blockers},
            "qualityScore": score,
            "qualityDimensions": dimensions,
            "impactSummary": impact,
            "autoCloseEligible": auto_close_eligible,
        }

    @staticmethod
    def _hard_blockers(
        version: FeishuMaterialVersion | None,
        source_item: FeishuSourceItem | None,
        units: list[FeishuKnowledgeUnit],
        relations: list[FeishuCrossDocumentRelation],
    ) -> list[dict]:
        blockers: list[dict] = []
        quality = (version.processing_params or {}).get("content_quality") or {} if version else {}
        if (
            version is None
            or version.processing_status in FAILED_PROCESSING_STATUSES
            or not quality.get("checked")
            or not quality.get("has_body")
            or any(not (unit.content or "").strip() for unit in units)
        ):
            blockers.append({"code": "CONTENT_INCOMPLETE", "message": "正文为空或解析不完整"})
        if units and any(not unit.source_segment_ids or not unit.locator_json for unit in units):
            blockers.append({"code": "SOURCE_LOCATION_MISSING", "message": "知识单元缺少可追溯来源位置"})
        if any(
            relation.relation_type == CrossDocumentRelationType.CONFLICT and relation.status in OPEN_RELATION_STATUSES
            for relation in relations
        ):
            blockers.append({"code": "OPEN_CONFLICT", "message": "存在未解决的跨文档冲突"})
        if source_item is None or source_item.source_validity != "valid":
            blockers.append({"code": "SOURCE_INVALID", "message": "来源已经失效或无法核验"})
        return blockers

    async def _source_context(
        self, package: FeishuReviewPackage
    ) -> tuple[FeishuMaterialVersion | None, FeishuSourceItem | None]:
        version = None
        if package.source_version_id:
            version = await self.session.scalar(
                select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == package.source_version_id)
            )
        source_item = None
        item_id = package.source_item_id or (version.item_id if version else None)
        if item_id:
            source_item = await self.session.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == item_id))
        return version, source_item

    async def _previous_version(
        self,
        version: FeishuMaterialVersion | None,
        source_item: FeishuSourceItem | None,
    ) -> FeishuMaterialVersion | None:
        if version is None or source_item is None:
            return None
        if source_item.active_version_id and source_item.active_version_id != version.version_id:
            return await self.session.scalar(
                select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == source_item.active_version_id)
            )
        return await self.session.scalar(
            select(FeishuMaterialVersion)
            .where(
                FeishuMaterialVersion.item_id == version.item_id,
                FeishuMaterialVersion.version_id != version.version_id,
                FeishuMaterialVersion.published_at.is_not(None),
            )
            .order_by(
                FeishuMaterialVersion.published_at.desc().nullslast(),
                FeishuMaterialVersion.created_at.desc(),
            )
            .limit(1)
        )

    async def _open_relations(
        self,
        version_id: str | None,
        review_items: list[FeishuReviewItem],
    ) -> list[FeishuCrossDocumentRelation]:
        relation_ids = {relation_id for item in review_items for relation_id in (item.relation_ids or [])}
        clauses = []
        if version_id:
            clauses.extend(
                [
                    FeishuCrossDocumentRelation.source_version_id == version_id,
                    FeishuCrossDocumentRelation.target_version_id == version_id,
                ]
            )
        if relation_ids:
            clauses.append(FeishuCrossDocumentRelation.relation_id.in_(relation_ids))
        if not clauses:
            return []
        return list(
            await self.session.scalars(
                select(FeishuCrossDocumentRelation).where(
                    or_(*clauses),
                    FeishuCrossDocumentRelation.status.in_(OPEN_RELATION_STATUSES),
                )
            )
        )

    async def _segments(self, version_id: str | None) -> list[FeishuSourceSegment]:
        if not version_id:
            return []
        return list(
            await self.session.scalars(
                select(FeishuSourceSegment)
                .where(
                    FeishuSourceSegment.version_id == version_id,
                    FeishuSourceSegment.status == "ACTIVE",
                )
                .order_by(FeishuSourceSegment.segment_index)
            )
        )

    @staticmethod
    def _signature(payload: list) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _content_signature(cls, segments: list[FeishuSourceSegment]) -> str:
        return cls._signature(
            [segment.content_hash for segment in segments if segment.segment_type.lower() not in IMAGE_SEGMENT_TYPES]
        )

    @classmethod
    def _image_signature(cls, segments: list[FeishuSourceSegment]) -> str:
        return cls._signature(
            [
                (segment.content_hash, segment.locator_json or {})
                for segment in segments
                if segment.segment_type.lower() in IMAGE_SEGMENT_TYPES or "![" in (segment.content or "")
            ]
        )

    @classmethod
    def _layout_signature(cls, segments: list[FeishuSourceSegment]) -> str:
        layout_keys = {"page", "page_number", "slide", "slide_number", "sheet", "row", "row_start", "row_end"}
        return cls._signature(
            [
                (
                    segment.segment_type,
                    segment.title_path or [],
                    {key: value for key, value in (segment.locator_json or {}).items() if key in layout_keys},
                )
                for segment in segments
            ]
        )
