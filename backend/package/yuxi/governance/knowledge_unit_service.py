from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.governance.domain import (
    ProblemTag,
    ReviewItemStatus,
    ReviewOutcome,
    ReviewPackageStatus,
    ReviewSubjectType,
    ReviewTriggerType,
    ReviewType,
)
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSourceItem,
    FeishuSourceSegment,
)


MAX_UNIT_TOKENS = 1_200
NON_KNOWLEDGE_TITLES = {
    "目录",
    "内容目录",
    "章节目录",
    "contents",
    "content",
    "封面",
    "结束",
    "谢谢",
    "感谢观看",
    "thank you",
    "thanks",
}
RELATION_TAGS = {
    "EXACT_DUPLICATE": ProblemTag.DUPLICATE,
    "OVERLAP": ProblemTag.OVERLAP,
    "CONFLICT": ProblemTag.CONFLICT,
    "INSUFFICIENT": ProblemTag.INSUFFICIENT_EVIDENCE,
}


def _hash(*parts: str, length: int = 40) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _normalized(value: str | None) -> str:
    text = re.sub(r"[`*_>#|\-—–·•]+", "", value or "")
    return re.sub(r"\s+", "", text).lower()


def _content_hash(value: str) -> str:
    return hashlib.sha256(_normalized(value).encode("utf-8")).hexdigest()


def _token_count(segments: Sequence[FeishuSourceSegment]) -> int:
    return sum(int(segment.token_count or 0) for segment in segments)


def _unit_type(segment: FeishuSourceSegment) -> str:
    locator = dict(segment.locator_json or {})
    if locator.get("slide"):
        return "SLIDE"
    if locator.get("sheet") or segment.segment_type == "table":
        return "TABLE"
    if segment.segment_type == "ocr":
        return "OCR"
    if segment.segment_type == "qa":
        return "QA"
    if locator.get("page"):
        return "PAGE"
    return "SECTION"


def _structural_anchor(segment: FeishuSourceSegment) -> dict[str, Any]:
    locator = dict(segment.locator_json or {})
    anchor = {"unit_type": _unit_type(segment)}
    for key in ("slide", "sheet", "page"):
        if locator.get(key) is not None:
            anchor[key] = locator[key]
    if not any(key in anchor for key in ("slide", "sheet", "page")):
        anchor["title_path"] = [str(value) for value in (segment.title_path or []) if value]
    if anchor["unit_type"] in {"OCR", "QA"} and not any(key in anchor for key in ("slide", "sheet", "page")):
        anchor["segment_type"] = segment.segment_type
    return anchor


def _locator_for(segments: Sequence[FeishuSourceSegment]) -> dict[str, Any]:
    first = dict(segments[0].locator_json or {})
    result = {
        key: first[key]
        for key in ("page", "page_count", "slide", "sheet", "row_start", "row_end")
        if first.get(key) is not None
    }
    result["source_segment_ids"] = [segment.segment_id for segment in segments]
    return result


def _unit_title(segments: Sequence[FeishuSourceSegment], unit_type: str, unit_index: int) -> str:
    title_path = [str(value).strip() for value in (segments[0].title_path or []) if str(value).strip()]
    if title_path:
        return title_path[-1][:512]
    locator = dict(segments[0].locator_json or {})
    if unit_type == "SLIDE" and locator.get("slide"):
        return f"第 {locator['slide']} 页幻灯片"
    if unit_type == "TABLE" and locator.get("sheet"):
        return f"工作表 {locator['sheet']}"
    if locator.get("page"):
        return f"第 {locator['page']} 页"
    for line in segments[0].content.splitlines():
        cleaned = re.sub(r"^#{1,6}\s+", "", line).strip()
        if cleaned:
            return cleaned[:80]
    return f"知识单元 {unit_index + 1}"


def _is_non_knowledge(title: str, content: str) -> bool:
    normalized_title = _normalized(title)
    normalized_content = _normalized(content)
    if normalized_title in {_normalized(value) for value in NON_KNOWLEDGE_TITLES} and len(normalized_content) <= 240:
        return True
    return len(normalized_content) < 24


@dataclass(slots=True)
class KnowledgeUnitDraft:
    unit_key: str
    lineage_key: str
    unit_index: int
    unit_type: str
    title: str
    content: str
    content_hash: str
    source_segment_ids: list[str]
    locator: dict[str, Any]


def build_knowledge_unit_drafts(
    segments: Sequence[FeishuSourceSegment],
    *,
    item_id: str,
) -> list[KnowledgeUnitDraft]:
    groups: list[list[FeishuSourceSegment]] = []
    current: list[FeishuSourceSegment] = []
    current_anchor = ""

    def flush() -> None:
        if current:
            groups.append(list(current))
            current.clear()

    for segment in sorted(segments, key=lambda value: value.segment_index):
        anchor = json.dumps(_structural_anchor(segment), ensure_ascii=False, sort_keys=True)
        unit_type = _unit_type(segment)
        structured = unit_type in {"SLIDE", "TABLE", "OCR", "QA", "PAGE"}
        fixed_page_group = unit_type in {"SLIDE", "PAGE"}
        can_append = (
            bool(current)
            and anchor == current_anchor
            and (fixed_page_group or _token_count([*current, segment]) <= MAX_UNIT_TOKENS)
            and (_unit_type(current[0]) == _unit_type(segment))
        )
        if current and not can_append:
            flush()
        if not current:
            current_anchor = anchor
        current.append(segment)
        if structured and not fixed_page_group and _token_count(current) >= MAX_UNIT_TOKENS:
            flush()
    flush()

    occurrences: defaultdict[str, int] = defaultdict(int)
    drafts: list[KnowledgeUnitDraft] = []
    for index, group in enumerate(groups):
        anchor = json.dumps(_structural_anchor(group[0]), ensure_ascii=False, sort_keys=True)
        occurrences[anchor] += 1
        occurrence = occurrences[anchor]
        unit_type = _unit_type(group[0])
        content = "\n\n".join(segment.content.strip() for segment in group if segment.content.strip()).strip()
        if not content:
            continue
        drafts.append(
            KnowledgeUnitDraft(
                unit_key=_hash(anchor, str(occurrence), length=48),
                lineage_key=_hash(item_id, anchor, str(occurrence), length=48),
                unit_index=len(drafts),
                unit_type=unit_type,
                title=_unit_title(group, unit_type, index),
                content=content,
                content_hash=_content_hash(content),
                source_segment_ids=[segment.segment_id for segment in group],
                locator=_locator_for(group),
            )
        )
    return drafts


class KnowledgeUnitService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def ensure_for_version(self, version_id: str) -> list[FeishuKnowledgeUnit]:
        package = await self.session.scalar(
            select(FeishuReviewPackage).where(FeishuReviewPackage.source_version_id == version_id)
        )
        if package is None:
            return []
        return await self.ensure_for_package(package)

    async def ensure_for_package(self, package: FeishuReviewPackage) -> list[FeishuKnowledgeUnit]:
        if package.trigger_type == ReviewTriggerType.FEEDBACK:
            return await self._active_units(package.source_version_id)
        if not package.source_version_id or package.workflow_status in {
            ReviewPackageStatus.COMPLETED,
            ReviewPackageStatus.INVALIDATED,
        }:
            return await self._active_units(package.source_version_id)
        row = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuMaterialVersion.version_id == package.source_version_id)
            )
        ).one_or_none()
        if row is None:
            return []
        version, source_item = row
        segments = list(
            await self.session.scalars(
                select(FeishuSourceSegment)
                .where(
                    FeishuSourceSegment.version_id == version.version_id,
                    FeishuSourceSegment.status == "ACTIVE",
                )
                .order_by(FeishuSourceSegment.segment_index.asc())
            )
        )
        if not segments:
            return []
        units = await self._upsert_units(version, source_item, segments)
        comparison_status = str(
            ((version.processing_params or {}).get("comparison") or {}).get("status") or "not_started"
        )
        await self._sync_review_items(package, units, comparison_status=comparison_status)
        await self.session.flush()
        return units

    async def _upsert_units(
        self,
        version: FeishuMaterialVersion,
        source_item: FeishuSourceItem,
        segments: list[FeishuSourceSegment],
    ) -> list[FeishuKnowledgeUnit]:
        drafts = build_knowledge_unit_drafts(segments, item_id=source_item.item_id)
        previous = await self._previous_units(version, source_item)
        previous_by_lineage = {unit.lineage_key: unit for unit in previous}
        previous_by_hash: defaultdict[str, list[FeishuKnowledgeUnit]] = defaultdict(list)
        for unit in previous:
            previous_by_hash[unit.content_hash].append(unit)

        existing = {
            unit.unit_key: unit
            for unit in await self.session.scalars(
                select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.version_id == version.version_id)
            )
        }
        active_keys: set[str] = set()
        result: list[FeishuKnowledgeUnit] = []
        for draft in drafts:
            active_keys.add(draft.unit_key)
            previous_unit = previous_by_lineage.get(draft.lineage_key)
            if previous_unit is None and len(previous_by_hash[draft.content_hash]) == 1:
                previous_unit = previous_by_hash[draft.content_hash][0]
                draft.lineage_key = previous_unit.lineage_key
            change_type = "NEW"
            if previous_unit is not None:
                change_type = "UNCHANGED" if previous_unit.content_hash == draft.content_hash else "UPDATED"

            recommended_outcome, reason, confidence, manual_required = self._recommendation(
                draft,
                previous_unit=previous_unit,
                change_type=change_type,
            )
            unit = existing.get(draft.unit_key)
            if unit is None:
                unit = FeishuKnowledgeUnit(
                    unit_id=f"unit-{_hash(version.version_id, draft.unit_key)}",
                    unit_key=draft.unit_key,
                    version_id=version.version_id,
                    item_id=source_item.item_id,
                    publication_state="PENDING",
                )
                self.session.add(unit)
            unit.lineage_key = draft.lineage_key
            unit.unit_index = draft.unit_index
            unit.unit_type = draft.unit_type
            unit.title = draft.title
            unit.content = draft.content
            unit.content_hash = draft.content_hash
            unit.source_segment_ids = draft.source_segment_ids
            unit.locator_json = draft.locator
            unit.change_type = change_type
            unit.previous_unit_id = previous_unit.unit_id if previous_unit else None
            unit.recommended_outcome = recommended_outcome
            unit.recommendation_reason = reason
            unit.recommendation_confidence = confidence
            unit.manual_review_required = manual_required
            unit.status = "ACTIVE"
            result.append(unit)

        for key, unit in existing.items():
            if key not in active_keys:
                unit.status = "OBSOLETE"
        await self.session.flush()
        return result

    async def _sync_review_items(
        self,
        package: FeishuReviewPackage,
        units: list[FeishuKnowledgeUnit],
        *,
        comparison_status: str,
    ) -> None:
        items = list(
            await self.session.scalars(
                select(FeishuReviewItem)
                .where(FeishuReviewItem.package_id == package.package_id)
                .order_by(FeishuReviewItem.created_at.asc())
            )
        )
        unit_items = {item.subject_id: item for item in items if item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT}
        material_items = [item for item in items if item.subject_type == ReviewSubjectType.MATERIAL_VERSION]
        base_item = next(
            (
                item
                for item in material_items
                if item.item_status in {ReviewItemStatus.PENDING, ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION}
            ),
            material_items[0] if material_items else None,
        )
        if base_item is None and not unit_items:
            return
        if base_item and base_item.item_status == ReviewItemStatus.WAITING_SOURCE_CHANGE:
            return

        previous_unit_ids = [unit.previous_unit_id for unit in units if unit.previous_unit_id]
        previous_units = {
            unit.unit_id: unit
            for unit in await self.session.scalars(
                select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.unit_id.in_(previous_unit_ids))
            )
        }
        relations_by_unit = await self._relations_by_unit(package.source_version_id, units)
        for unit in units:
            relations = relations_by_unit.get(unit.unit_id, [])
            relation_types = {relation.relation_type for relation in relations}
            problem_tags = [RELATION_TAGS[value] for value in relation_types if value in RELATION_TAGS]
            review_type = self._unit_review_type(base_item, unit, relation_types)
            recommended_outcome, reason, confidence, manual_required = self._recommendation_for_review_type(
                unit,
                review_type=review_type,
                relation_types=relation_types,
            )
            if (
                comparison_status != "completed"
                and not relation_types
                and unit.change_type in {"NEW", "UPDATED"}
                and not _is_non_knowledge(unit.title, unit.content)
            ):
                reason = (
                    "跨文档检查失败，请人工核对后处理。"
                    if comparison_status == "failed"
                    else "跨文档检查尚未完成，完成后系统会自动刷新建议。"
                )
                confidence = min(confidence, 0.5)
                manual_required = True
            unit.recommended_outcome = recommended_outcome
            unit.recommendation_reason = reason
            unit.recommendation_confidence = confidence
            unit.manual_review_required = manual_required

            item = unit_items.get(unit.unit_id)
            if item is None:
                item = FeishuReviewItem(
                    review_item_id=f"review-item-{_hash(package.package_id, unit.unit_id)}",
                    package_id=package.package_id,
                    candidate_key=f"unit:{unit.unit_id}",
                    subject_type=ReviewSubjectType.KNOWLEDGE_UNIT,
                    subject_id=unit.unit_id,
                    item_status=ReviewItemStatus.PENDING,
                    applicability_scope={},
                    decision_payload={},
                )
                self.session.add(item)
            if item.item_status not in {ReviewItemStatus.PENDING, ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION}:
                continue
            item.review_type = review_type
            item.title = unit.title
            item.summary = reason
            item.subject_locator_json = dict(unit.locator_json or {})
            previous_unit = previous_units.get(unit.previous_unit_id)
            item.evidence_json = {
                "knowledge_unit": True,
                "unit_type": unit.unit_type,
                "content": unit.content,
                "source_segment_ids": list(unit.source_segment_ids or []),
                "content_hash": unit.content_hash,
                "change_type": unit.change_type,
                "previous_unit_id": unit.previous_unit_id,
                "previous_content": previous_unit.content if previous_unit else None,
                "previous_title": previous_unit.title if previous_unit else None,
                "recommended_outcome": recommended_outcome,
                "recommendation_reason": reason,
                "recommendation_confidence": confidence,
                "manual_review_required": manual_required,
                "comparison_status": comparison_status,
            }
            item.relation_ids = [relation.relation_id for relation in relations]
            item.problem_tags = [str(tag) for tag in problem_tags]

        if base_item and base_item.item_status in {
            ReviewItemStatus.PENDING,
            ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION,
        }:
            base_item.item_status = ReviewItemStatus.INVALIDATED
            base_item.decision_comment = "已拆分为知识单元级审核"
            base_item.decision_payload = {
                **dict(base_item.decision_payload or {}),
                "replaced_by_knowledge_units": True,
                "knowledge_unit_count": len(units),
            }
        package.risk_level = "HIGH" if any(unit.manual_review_required for unit in units) else "LOW"

    async def apply_decision(self, item: FeishuReviewItem, outcome: str) -> None:
        if item.subject_type != ReviewSubjectType.KNOWLEDGE_UNIT:
            return
        unit = await self.session.scalar(
            select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.unit_id == item.subject_id).with_for_update()
        )
        if unit is None:
            raise LookupError(f"Knowledge unit not found: {item.subject_id}")
        if outcome in {ReviewOutcome.PUBLISH, ReviewOutcome.ADOPT_NEW_VERSION, ReviewOutcome.SPLIT_SCOPE}:
            target_state = "INCLUDED"
        elif outcome in {
            ReviewOutcome.EXCLUDE,
            ReviewOutcome.KEEP_CURRENT,
            ReviewOutcome.ARCHIVE,
            ReviewOutcome.DISMISS,
        }:
            target_state = "EXCLUDED"
        elif outcome == ReviewOutcome.CONFIRM_VALID:
            target_state = "INCLUDED"
        else:
            return
        segments = list(
            await self.session.scalars(
                select(FeishuSourceSegment)
                .where(FeishuSourceSegment.segment_id.in_(list(unit.source_segment_ids or [])))
                .with_for_update()
            )
        )
        for segment in segments:
            if segment.publication_state == "PENDING":
                segment.publication_state = target_state
        states = {segment.publication_state for segment in segments}
        if "INCLUDED" in states:
            unit.publication_state = "INCLUDED"
        elif states and states <= {"ALIAS"}:
            unit.publication_state = "ALIAS"
        else:
            unit.publication_state = target_state

    async def _active_units(self, version_id: str | None) -> list[FeishuKnowledgeUnit]:
        if not version_id:
            return []
        return list(
            await self.session.scalars(
                select(FeishuKnowledgeUnit)
                .where(FeishuKnowledgeUnit.version_id == version_id, FeishuKnowledgeUnit.status == "ACTIVE")
                .order_by(FeishuKnowledgeUnit.unit_index.asc())
            )
        )

    async def _previous_units(
        self,
        version: FeishuMaterialVersion,
        source_item: FeishuSourceItem,
    ) -> list[FeishuKnowledgeUnit]:
        if source_item.active_version_id and source_item.active_version_id != version.version_id:
            active = await self._active_units(source_item.active_version_id)
            if active:
                return active
        previous_version_id = await self.session.scalar(
            select(FeishuMaterialVersion.version_id)
            .where(
                FeishuMaterialVersion.item_id == source_item.item_id,
                FeishuMaterialVersion.version_id != version.version_id,
                FeishuMaterialVersion.created_at < version.created_at,
            )
            .order_by(FeishuMaterialVersion.created_at.desc())
            .limit(1)
        )
        return await self._active_units(previous_version_id)

    async def _relations_by_unit(
        self,
        version_id: str | None,
        units: list[FeishuKnowledgeUnit],
    ) -> dict[str, list[FeishuCrossDocumentRelation]]:
        if not version_id or not units:
            return {}
        relations = list(
            await self.session.scalars(
                select(FeishuCrossDocumentRelation).where(
                    FeishuCrossDocumentRelation.status == "open",
                    or_(
                        FeishuCrossDocumentRelation.source_version_id == version_id,
                        FeishuCrossDocumentRelation.target_version_id == version_id,
                    ),
                )
            )
        )
        result: defaultdict[str, list[FeishuCrossDocumentRelation]] = defaultdict(list)
        for relation in relations:
            evidence = self._relation_evidence(relation)
            best_unit = max(units, key=lambda unit: self._evidence_score(unit.content, evidence), default=None)
            if best_unit is not None:
                result[best_unit.unit_id].append(relation)
        return dict(result)

    @staticmethod
    def _relation_evidence(relation: FeishuCrossDocumentRelation) -> str:
        values: list[str] = []
        for item in [*(relation.same_content or []), *(relation.different_content or [])]:
            if isinstance(item, dict):
                values.extend(str(value) for value in item.values() if value)
            elif item:
                values.append(str(item))
        return "\n".join(values)

    @staticmethod
    def _evidence_score(content: str, evidence: str) -> float:
        source = _normalized(content)[:6_000]
        target = _normalized(evidence)[:6_000]
        if not source or not target:
            return 0.0
        if source in target or target in source:
            return 1.0
        return SequenceMatcher(None, source, target, autojunk=False).ratio()

    @staticmethod
    def _unit_review_type(
        base_item: FeishuReviewItem | None,
        unit: FeishuKnowledgeUnit,
        relation_types: set[str],
    ) -> str:
        if "CONFLICT" in relation_types:
            return ReviewType.CONFLICT
        if base_item and base_item.review_type == ReviewType.STALE:
            return ReviewType.STALE
        if unit.change_type == "NEW" and (base_item is None or base_item.review_type == ReviewType.NEW):
            return ReviewType.NEW
        return ReviewType.UPDATE

    @staticmethod
    def _recommendation(
        draft: KnowledgeUnitDraft,
        *,
        previous_unit: FeishuKnowledgeUnit | None,
        change_type: str,
    ) -> tuple[str, str, float, bool]:
        if _is_non_knowledge(draft.title, draft.content):
            outcome = ReviewOutcome.KEEP_CURRENT if previous_unit else ReviewOutcome.EXCLUDE
            return outcome, "内容属于目录、封面或无法独立成义的短文本，建议不纳入知识库。", 0.98, False
        if change_type == "UNCHANGED" and previous_unit is not None:
            if previous_unit.publication_state in {"EXCLUDED", "ALIAS"}:
                return ReviewOutcome.KEEP_CURRENT, "内容与上一版本一致，沿用原处理结果。", 1.0, False
            return ReviewOutcome.ADOPT_NEW_VERSION, "内容与上一版本一致，无需逐项复核。", 1.0, False
        if change_type == "UPDATED":
            return ReviewOutcome.ADOPT_NEW_VERSION, "该知识单元内容已变化，请核对差异后采用新版。", 0.9, True
        return ReviewOutcome.PUBLISH, "未发现冲突或解析异常，建议纳入知识库。", 0.92, False

    @staticmethod
    def _recommendation_for_review_type(
        unit: FeishuKnowledgeUnit,
        *,
        review_type: str,
        relation_types: set[str],
    ) -> tuple[str, str, float, bool]:
        if "CONFLICT" in relation_types:
            return ReviewOutcome.KEEP_CURRENT, "发现相同条件下的结论冲突，需要人工选择保留或采用的版本。", 1.0, True
        if "EXACT_DUPLICATE" in relation_types:
            return ReviewOutcome.KEEP_CURRENT, "发现完全重复内容，请确认使用已有知识或调整规范来源。", 0.98, True
        if "OVERLAP" in relation_types:
            return ReviewOutcome.KEEP_CURRENT, "发现部分内容重叠，需要核对重叠范围后决定。", 0.9, True
        if "INSUFFICIENT" in relation_types:
            return ReviewOutcome.KEEP_CURRENT, "跨文档证据不足，需要人工核对。", 0.7, True
        if review_type == ReviewType.STALE:
            return ReviewOutcome.CONFIRM_VALID, "内容未发生实质变化，建议确认仍然有效。", 0.98, False
        if _is_non_knowledge(unit.title, unit.content):
            outcome = (
                ReviewOutcome.KEEP_CURRENT
                if review_type == ReviewType.UPDATE and unit.change_type != "NEW"
                else ReviewOutcome.EXCLUDE
            )
            return outcome, "内容属于目录、封面或无法独立成义的短文本，建议不纳入知识库。", 0.98, False
        if review_type == ReviewType.UPDATE:
            if unit.change_type == "UNCHANGED":
                return ReviewOutcome.ADOPT_NEW_VERSION, "内容与上一版本一致，无需逐项复核。", 1.0, False
            return ReviewOutcome.ADOPT_NEW_VERSION, "该知识单元内容已变化，请核对差异后采用新版。", 0.9, True
        return ReviewOutcome.PUBLISH, "未发现冲突或解析异常，建议纳入知识库。", 0.92, False
