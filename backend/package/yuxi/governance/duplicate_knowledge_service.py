from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.governance.domain import (
    CrossDocumentRelationType,
    DuplicateResolutionStrategy,
    KnowledgeSourceRole,
    ReviewAction,
    ReviewDecision,
    ReviewItemStatus,
    ReviewOutcome,
    ReviewPackageStatus,
    ReviewSubjectType,
)
from yuxi.governance.knowledge_unit_service import KnowledgeUnitService
from yuxi.governance.review_backfill import backfill_legacy_governance_reviews
from yuxi.governance.schemas import DuplicateRelationResolutionRequest
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuDuplicateRelationDecision,
    FeishuGovernanceReview,
    FeishuKnowledgeSourceFragment,
    FeishuKnowledgeUnit,
    FeishuLogicalKnowledge,
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSourceItem,
    FeishuSourceSegment,
    KnowledgeChunk,
)
from yuxi.utils.datetime_utils import utc_now_naive


SUPPORTED_RELATION_TYPES = {
    CrossDocumentRelationType.EXACT_DUPLICATE,
    CrossDocumentRelationType.OVERLAP,
}
MIN_FRAGMENT_LENGTH = 24
MAX_FRAGMENT_MATCHES = 24


@dataclass(frozen=True, slots=True)
class ParsedFragment:
    chunk_id: str
    file_id: str
    chunk_index: int
    content: str
    segment_id: str | None = None
    locator_json: dict = field(default_factory=dict)


def _stable_id(prefix: str, *parts: str) -> str:
    raw = ":".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _normalize_content(content: str) -> str:
    normalized = re.sub(r"[`*_>#\-—–·•|]+", "", content or "")
    normalized = re.sub(r"\s+", "", normalized).lower()
    return normalized[:8000]


def _shingles(content: str, size: int = 3) -> set[str]:
    if len(content) < size:
        return {content} if content else set()
    return {content[index : index + size] for index in range(len(content) - size + 1)}


def _content_similarity(source: str, target: str) -> float:
    if not source or not target:
        return 0.0
    if source == target:
        return 1.0
    source_shingles = _shingles(source)
    target_shingles = _shingles(target)
    union = source_shingles | target_shingles
    jaccard = len(source_shingles & target_shingles) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, source, target, autojunk=False).ratio()
    return round(max(jaccard, sequence), 4)


def _content_hash(content: str) -> str:
    return hashlib.sha256(_normalize_content(content).encode("utf-8")).hexdigest()


def _excerpt(content: str, limit: int = 420) -> str:
    cleaned = re.sub(r"\n{3,}", "\n\n", (content or "").strip())
    return cleaned if len(cleaned) <= limit else f"{cleaned[:limit].rstrip()}…"


def _passages(content: str, *, limit: int = 160) -> list[str]:
    passages: list[str] = []
    for block in re.split(r"\n+|(?<=[。！？!?])", content or ""):
        cleaned = block.strip()
        if _is_media_reference(cleaned):
            continue
        if len(_normalize_content(cleaned)) < MIN_FRAGMENT_LENGTH:
            continue
        if len(cleaned) <= 1200:
            passages.append(cleaned)
        else:
            passages.extend(cleaned[start : start + 1000] for start in range(0, len(cleaned), 800))
        if len(passages) >= limit:
            break
    return passages[:limit]


def _is_media_reference(content: str) -> bool:
    match = re.fullmatch(r"!?\[([^\]]+)\]\(([^)]+)\)", content or "")
    if match is None:
        return False
    label, target = match.groups()
    return bool(
        "/kb-images/" in target
        or re.search(r"\.(?:png|jpe?g|gif|webp|svg)(?:$|[?#])", label, re.IGNORECASE)
        or re.search(r"\.(?:png|jpe?g|gif|webp|svg)(?:$|[?#])", target, re.IGNORECASE)
    )


def _best_passage_match(source: str, target: str) -> tuple[float, str, str]:
    best = (0.0, "", "")
    for source_passage in _passages(source):
        source_normalized = _normalize_content(source_passage)
        for target_passage in _passages(target):
            target_normalized = _normalize_content(target_passage)
            similarity = _content_similarity(source_normalized, target_normalized)
            if similarity > best[0]:
                best = (similarity, source_passage, target_passage)
            if similarity >= 0.995:
                return 1.0, source_passage, target_passage
    return best


def _parsed_fragments(file_id: str, content: str) -> list[ParsedFragment]:
    return [
        ParsedFragment(
            chunk_id=_stable_id("parsed-fragment", file_id, str(index), _content_hash(passage)),
            file_id=file_id,
            chunk_index=index,
            content=passage,
        )
        for index, passage in enumerate(_passages(content, limit=400))
    ]


def _fragment_title(content: str, fallback_title: str, chunk_index: int) -> str:
    lines = [re.sub(r"^#+\s*", "", line).strip() for line in (content or "").splitlines() if line.strip()]
    for line in lines:
        if "公司简介" in line:
            return "公司简介"
    if lines and len(lines[0]) <= 60:
        return lines[0]
    return f"{fallback_title or '未命名资料'} · 片段 {chunk_index + 1}"


class DuplicateKnowledgeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        content_loader: Callable[[str], Awaitable[str | None]] | None = None,
    ):
        self.session = session
        self.content_loader = content_loader

    async def get_relation_candidates(self, relation_id: str) -> dict:
        relation, versions, items, chunks = await self._load_context(relation_id)
        matches = self._match_fragments(relation, versions, chunks)
        decision = await self.session.scalar(
            select(FeishuDuplicateRelationDecision).where(
                FeishuDuplicateRelationDecision.relation_id == relation.relation_id
            )
        )
        response = self._candidate_response(relation, versions, items, matches, decision)
        if decision and decision.strategy != DuplicateResolutionStrategy.KEEP_SEPARATE:
            alias_version_id = (
                relation.target_version_id
                if decision.primary_version_id == relation.source_version_id
                else relation.source_version_id
            )
            response["review_automation"] = await self._review_automation_summary(
                alias_version_id,
                relation_id=relation.relation_id,
            )
        return response

    async def resolve_relation(
        self,
        relation_id: str,
        payload: DuplicateRelationResolutionRequest,
        *,
        operator_id: str,
    ) -> dict:
        relation = await self.session.scalar(
            select(FeishuCrossDocumentRelation)
            .where(FeishuCrossDocumentRelation.relation_id == relation_id)
            .with_for_update()
        )
        if relation is None:
            raise LookupError(f"Cross-document relation not found: {relation_id}")
        existing = await self.session.scalar(
            select(FeishuDuplicateRelationDecision).where(
                FeishuDuplicateRelationDecision.relation_id == relation.relation_id
            )
        )
        if existing is not None:
            if existing.request_id != payload.request_id:
                raise ValueError("Duplicate relation has already been resolved")
            response = await self.get_relation_candidates(relation_id)
            response["idempotent_replay"] = True
            return response

        relation, versions, items, chunks = await self._load_context(relation_id, relation=relation)
        matches = self._match_fragments(relation, versions, chunks)
        if payload.strategy != DuplicateResolutionStrategy.KEEP_SEPARATE and not matches:
            raise ValueError("没有找到可关联的重复内容片段")

        primary_version_id = None
        logical_knowledge_ids: list[str] = []
        review_automation = None
        if payload.strategy != DuplicateResolutionStrategy.KEEP_SEPARATE:
            primary_version_id = (
                relation.source_version_id
                if payload.strategy == DuplicateResolutionStrategy.USE_SOURCE
                else relation.target_version_id
            )
            for match in matches:
                logical_knowledge = await self._link_fragment_pair(
                    relation,
                    match,
                    primary_version_id=primary_version_id,
                    versions=versions,
                    items=items,
                    chunks=chunks,
                    operator_id=operator_id,
                )
                if logical_knowledge.logical_knowledge_id not in logical_knowledge_ids:
                    logical_knowledge_ids.append(logical_knowledge.logical_knowledge_id)

        decision = FeishuDuplicateRelationDecision(
            decision_id=_stable_id("duplicate-decision", relation.relation_id),
            relation_id=relation.relation_id,
            request_id=payload.request_id,
            strategy=payload.strategy,
            primary_version_id=primary_version_id,
            logical_knowledge_ids=logical_knowledge_ids,
            fragment_match_ids=[match["match_id"] for match in matches],
            comment=payload.comment,
            decided_by=operator_id,
        )
        self.session.add(decision)
        relation.status = "resolved"
        relation.human_decision = (
            "KEEP_SEPARATE"
            if payload.strategy == DuplicateResolutionStrategy.KEEP_SEPARATE
            else ReviewAction.MARK_DUPLICATE
        )
        relation.human_comment = payload.comment
        relation.resolved_by = operator_id
        relation.resolved_at = utc_now_naive()
        if primary_version_id is not None:
            alias_version_id = (
                relation.target_version_id
                if primary_version_id == relation.source_version_id
                else relation.source_version_id
            )
            review_automation = await self._close_duplicate_review_items(
                alias_version_id,
                relation_id=relation.relation_id,
                matches=matches,
                operator_id=operator_id,
                now=relation.resolved_at,
            )
        # Persist the relation decision in both related review packages so the
        # business audit trail remains visible regardless of which document
        # the operator opened first.  The relation decision table remains the
        # source of truth; these events are the human-readable audit entries.
        related_packages = list(
            await self.session.scalars(
                select(FeishuReviewPackage).where(
                    FeishuReviewPackage.source_version_id.in_(
                        {relation.source_version_id, relation.target_version_id}
                    )
                )
            )
        )
        strategy_label = {
            DuplicateResolutionStrategy.USE_SOURCE: "保留来源一",
            DuplicateResolutionStrategy.USE_TARGET: "保留来源二",
            DuplicateResolutionStrategy.KEEP_SEPARATE: "分别保留",
        }[payload.strategy]
        for package in related_packages:
            self.session.add(
                FeishuProcessingEvent(
                    source_id=package.source_id,
                    item_id=package.source_item_id,
                    version_id=package.source_version_id,
                    event_type="cross_document_relation_resolved",
                    operator_id=operator_id,
                    message=f"跨文档关系已处理：{strategy_label}",
                    payload_json={
                        "package_id": package.package_id,
                        "relation_id": relation.relation_id,
                        "strategy": payload.strategy,
                        "primary_version_id": primary_version_id,
                        "fragment_match_count": len(matches),
                        "review_automation": review_automation,
                    },
                )
            )
        await self.session.flush()

        response = self._candidate_response(relation, versions, items, matches, decision)
        if review_automation is not None:
            response["review_automation"] = review_automation
        response["idempotent_replay"] = False
        return response

    async def _close_duplicate_review_items(
        self,
        alias_version_id: str,
        *,
        relation_id: str,
        matches: list[dict],
        operator_id: str,
        now: datetime,
    ) -> dict:
        relation = await self.session.scalar(
            select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.relation_id == relation_id)
        )
        if relation is None:
            raise LookupError(f"Cross-document relation not found: {relation_id}")
        if alias_version_id == relation.source_version_id:
            alias_segment_ids = {match["source_segment_id"] for match in matches if match.get("source_segment_id")}
        elif alias_version_id == relation.target_version_id:
            alias_segment_ids = {match["target_segment_id"] for match in matches if match.get("target_segment_id")}
        else:
            raise ValueError("规范来源与重复来源不属于当前跨文档关系")
        if not alias_segment_ids:
            return await self._review_automation_summary(alias_version_id, relation_id=relation_id)

        await backfill_legacy_governance_reviews(self.session, version_ids=[alias_version_id])
        packages = list(
            await self.session.scalars(
                select(FeishuReviewPackage)
                .where(
                    FeishuReviewPackage.source_version_id == alias_version_id,
                    FeishuReviewPackage.workflow_status.not_in(
                        {ReviewPackageStatus.COMPLETED, ReviewPackageStatus.INVALIDATED}
                    ),
                )
                .with_for_update()
            )
        )
        auto_decided_count = 0
        for package in packages:
            await KnowledgeUnitService(self.session).ensure_for_package(package)
            review_items = list(
                await self.session.scalars(
                    select(FeishuReviewItem).where(FeishuReviewItem.package_id == package.package_id).with_for_update()
                )
            )
            unit_ids = [
                item.subject_id for item in review_items if item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT
            ]
            units = {
                unit.unit_id: unit
                for unit in await self.session.scalars(
                    select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.unit_id.in_(unit_ids)).with_for_update()
                )
            }
            related_relation_ids = {
                str(linked_id)
                for item in review_items
                for linked_id in (item.relation_ids or [])
                if str(linked_id) != relation_id
            }
            open_relation_ids = (
                set(
                    await self.session.scalars(
                        select(FeishuCrossDocumentRelation.relation_id).where(
                            FeishuCrossDocumentRelation.relation_id.in_(related_relation_ids),
                            FeishuCrossDocumentRelation.status == "open",
                        )
                    )
                )
                if related_relation_ids
                else set()
            )
            package_decided_count = 0
            for item in review_items:
                if (
                    item.subject_type != ReviewSubjectType.KNOWLEDGE_UNIT
                    or item.item_status
                    not in {ReviewItemStatus.PENDING, ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION}
                    or open_relation_ids.intersection(str(value) for value in (item.relation_ids or []))
                ):
                    continue
                unit = units.get(item.subject_id)
                source_segment_ids = {str(value) for value in (unit.source_segment_ids or [])} if unit else set()
                if not source_segment_ids or not source_segment_ids.issubset(alias_segment_ids):
                    continue
                item.item_status = ReviewItemStatus.DECIDED
                item.outcome = ReviewOutcome.DUPLICATE_SOURCE
                item.internal_action = ReviewAction.MARK_DUPLICATE
                item.decision_comment = "已作为规范知识的重复来源保留，无需再次审批。"
                item.decision_payload = {
                    "request_id": f"duplicate:{relation_id}",
                    "outcome": ReviewOutcome.DUPLICATE_SOURCE,
                    "duplicate_relation_id": relation_id,
                    "automated": True,
                }
                item.decided_by = operator_id
                item.decided_at = now
                unit.publication_state = "ALIAS"
                package_decided_count += 1
            if not package_decided_count:
                continue
            auto_decided_count += package_decided_count
            previous_status = package.workflow_status
            package.workflow_status = self._aggregate_package_status(review_items)
            package.completed_at = now if package.workflow_status == ReviewPackageStatus.COMPLETED else None
            package.lock_version += 1
            self.session.add(
                FeishuProcessingEvent(
                    source_id=package.source_id,
                    item_id=package.source_item_id,
                    version_id=package.source_version_id,
                    event_type="duplicate_review_items_auto_decided",
                    operator_id=operator_id,
                    message=f"已自动处理 {package_decided_count} 个重复来源知识单元",
                    payload_json={
                        "package_id": package.package_id,
                        "relation_id": relation_id,
                        "review_item_count": package_decided_count,
                    },
                )
            )
            if package.workflow_status == ReviewPackageStatus.COMPLETED and previous_status != package.workflow_status:
                self.session.add(
                    FeishuProcessingEvent(
                        source_id=package.source_id,
                        item_id=package.source_item_id,
                        version_id=package.source_version_id,
                        event_type="review_package_completed",
                        operator_id=operator_id,
                        message="全部知识单元均已作为重复来源保留，无需再次审批。",
                        payload_json={"package_id": package.package_id, "relation_id": relation_id},
                    )
                )

        summary = await self._review_automation_summary(alias_version_id, relation_id=relation_id)
        if summary["remaining_unit_count"] == 0 and summary["open_package_count"] == 0 and auto_decided_count:
            version = await self.session.scalar(
                select(FeishuMaterialVersion)
                .where(FeishuMaterialVersion.version_id == alias_version_id)
                .with_for_update()
            )
            source_item = (
                await self.session.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == version.item_id))
                if version
                else None
            )
            if (
                version
                and source_item
                and source_item.active_version_id != version.version_id
                and version.processing_status in {"parsed", "awaiting_review"}
            ):
                version.processing_status = "skipped"
                version.review_status = "not_required"
                version.reviewer_id = operator_id
                version.reviewed_at = now
                version.review_comment = "全部知识单元均已作为其他规范知识的重复来源保留。"
                legacy_review = await self.session.scalar(
                    select(FeishuGovernanceReview).where(FeishuGovernanceReview.version_id == alias_version_id)
                )
                if legacy_review is not None:
                    legacy_review.status = "resolved"
                    legacy_review.decision = ReviewDecision.REJECT
                    legacy_review.action = ReviewAction.MARK_DUPLICATE
                    legacy_review.decision_comment = version.review_comment
                    legacy_review.decided_by = operator_id
                    legacy_review.decided_at = now
        await self.session.flush()
        return await self._review_automation_summary(alias_version_id, relation_id=relation_id)

    async def _review_automation_summary(self, alias_version_id: str, *, relation_id: str) -> dict:
        packages = list(
            await self.session.scalars(
                select(FeishuReviewPackage).where(FeishuReviewPackage.source_version_id == alias_version_id)
            )
        )
        package_ids = [package.package_id for package in packages]
        review_items = (
            list(
                await self.session.scalars(select(FeishuReviewItem).where(FeishuReviewItem.package_id.in_(package_ids)))
            )
            if package_ids
            else []
        )
        duplicate_items = [
            item for item in review_items if (item.decision_payload or {}).get("duplicate_relation_id") == relation_id
        ]
        remaining = sum(
            item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT
            and item.item_status
            in {
                ReviewItemStatus.PENDING,
                ReviewItemStatus.WAITING_SOURCE_CHANGE,
                ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION,
            }
            for item in review_items
        )
        package_statuses = {package.package_id: package.workflow_status for package in packages}
        completed_package_ids = {
            item.package_id
            for item in duplicate_items
            if package_statuses.get(item.package_id) == ReviewPackageStatus.COMPLETED
        }
        version = await self.session.scalar(
            select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == alias_version_id)
        )
        return {
            "alias_version_id": alias_version_id,
            "auto_decided_unit_count": len(duplicate_items),
            "remaining_unit_count": remaining,
            "completed_package_count": len(completed_package_ids),
            "open_package_count": sum(
                status not in {ReviewPackageStatus.COMPLETED, ReviewPackageStatus.INVALIDATED}
                for status in package_statuses.values()
            ),
            "source_review_closed": bool(
                version and version.processing_status == "skipped" and version.review_status == "not_required"
            ),
        }

    @staticmethod
    def _aggregate_package_status(items: list[FeishuReviewItem]) -> str:
        statuses = {item.item_status for item in items}
        if ReviewItemStatus.WAITING_SOURCE_CHANGE in statuses:
            return ReviewPackageStatus.WAITING_SOURCE_CHANGE
        if ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION in statuses:
            return ReviewPackageStatus.WAITING_BUSINESS_CONFIRMATION
        if statuses and statuses <= {
            ReviewItemStatus.DECIDED,
            ReviewItemStatus.SOURCE_UPDATED,
            ReviewItemStatus.INVALIDATED,
        }:
            return ReviewPackageStatus.COMPLETED
        return ReviewPackageStatus.OPEN

    async def _load_context(
        self,
        relation_id: str,
        *,
        relation: FeishuCrossDocumentRelation | None = None,
    ) -> tuple[
        FeishuCrossDocumentRelation,
        dict[str, FeishuMaterialVersion],
        dict[str, FeishuSourceItem],
        dict[str, KnowledgeChunk | ParsedFragment],
    ]:
        if relation is None:
            relation = await self.session.scalar(
                select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.relation_id == relation_id)
            )
        if relation is None:
            raise LookupError(f"Cross-document relation not found: {relation_id}")
        if relation.relation_type not in SUPPORTED_RELATION_TYPES:
            raise ValueError("当前关系不是可治理的重复或重叠内容")

        version_ids = [relation.source_version_id, relation.target_version_id]
        versions = {
            version.version_id: version
            for version in await self.session.scalars(
                select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id.in_(version_ids))
            )
        }
        if len(versions) != 2:
            raise LookupError("Cross-document relation source version is missing")
        item_ids = [version.item_id for version in versions.values()]
        items = {
            item.item_id: item
            for item in await self.session.scalars(
                select(FeishuSourceItem).where(FeishuSourceItem.item_id.in_(item_ids))
            )
        }
        file_ids = [version.yuxi_file_id for version in versions.values() if version.yuxi_file_id]
        chunks: dict[str, KnowledgeChunk | ParsedFragment] = {
            segment.segment_id: ParsedFragment(
                chunk_id=segment.segment_id,
                file_id=segment.yuxi_file_id,
                chunk_index=segment.segment_index,
                content=segment.content,
                segment_id=segment.segment_id,
                locator_json=dict(segment.locator_json or {}),
            )
            for segment in await self.session.scalars(
                select(FeishuSourceSegment)
                .where(
                    FeishuSourceSegment.version_id.in_(version_ids),
                    FeishuSourceSegment.status == "ACTIVE",
                )
                .order_by(FeishuSourceSegment.yuxi_file_id.asc(), FeishuSourceSegment.segment_index.asc())
            )
        }
        segmented_file_ids = {chunk.file_id for chunk in chunks.values()}
        fallback_file_ids = [file_id for file_id in file_ids if file_id not in segmented_file_ids]
        chunks.update(
            {
                chunk.chunk_id: chunk
                for chunk in await self.session.scalars(
                    select(KnowledgeChunk)
                    .where(KnowledgeChunk.file_id.in_(fallback_file_ids))
                    .order_by(KnowledgeChunk.file_id.asc(), KnowledgeChunk.chunk_index.asc())
                )
            }
        )
        if self.content_loader is not None:
            loaded_file_ids = {chunk.file_id for chunk in chunks.values()}
            for file_id in file_ids:
                if file_id in loaded_file_ids:
                    continue
                content = await self.content_loader(file_id)
                for fragment in _parsed_fragments(file_id, content or ""):
                    chunks[fragment.chunk_id] = fragment
        return relation, versions, items, chunks

    @staticmethod
    def _match_fragments(
        relation: FeishuCrossDocumentRelation,
        versions: dict[str, FeishuMaterialVersion],
        chunks: dict[str, KnowledgeChunk | ParsedFragment],
    ) -> list[dict]:
        source_file_id = versions[relation.source_version_id].yuxi_file_id
        target_file_id = versions[relation.target_version_id].yuxi_file_id
        source_chunks = [chunk for chunk in chunks.values() if chunk.file_id == source_file_id]
        target_chunks = [chunk for chunk in chunks.values() if chunk.file_id == target_file_id]
        threshold = 0.94 if relation.relation_type == CrossDocumentRelationType.EXACT_DUPLICATE else 0.72
        candidates = []
        for source_chunk in source_chunks:
            source_normalized = _normalize_content(source_chunk.content)
            if len(source_normalized) < MIN_FRAGMENT_LENGTH:
                continue
            for target_chunk in target_chunks:
                target_normalized = _normalize_content(target_chunk.content)
                if len(target_normalized) < MIN_FRAGMENT_LENGTH:
                    continue
                similarity = _content_similarity(source_normalized, target_normalized)
                passage_similarity, source_overlap, target_overlap = _best_passage_match(
                    source_chunk.content,
                    target_chunk.content,
                )
                similarity = max(similarity, passage_similarity)
                if similarity < threshold:
                    continue
                candidates.append(
                    (
                        similarity,
                        source_chunk,
                        target_chunk,
                        source_overlap or source_chunk.content,
                        target_overlap or target_chunk.content,
                    )
                )

        matches = []
        used_source_ids: set[str] = set()
        used_target_ids: set[str] = set()
        for similarity, source_chunk, target_chunk, source_overlap, target_overlap in sorted(
            candidates,
            key=lambda item: item[0],
            reverse=True,
        ):
            if source_chunk.chunk_id in used_source_ids or target_chunk.chunk_id in used_target_ids:
                continue
            used_source_ids.add(source_chunk.chunk_id)
            used_target_ids.add(target_chunk.chunk_id)
            matches.append(
                {
                    "match_id": _stable_id(
                        "fragment-match",
                        relation.relation_id,
                        source_chunk.chunk_id,
                        target_chunk.chunk_id,
                    ),
                    "source_chunk_id": source_chunk.chunk_id,
                    "source_chunk_index": source_chunk.chunk_index,
                    "source_segment_id": getattr(source_chunk, "segment_id", None),
                    "source_locator": dict(getattr(source_chunk, "locator_json", {}) or {}),
                    "source_excerpt": _excerpt(source_chunk.content),
                    "source_overlap_excerpt": _excerpt(source_overlap),
                    "target_chunk_id": target_chunk.chunk_id,
                    "target_chunk_index": target_chunk.chunk_index,
                    "target_segment_id": getattr(target_chunk, "segment_id", None),
                    "target_locator": dict(getattr(target_chunk, "locator_json", {}) or {}),
                    "target_excerpt": _excerpt(target_chunk.content),
                    "target_overlap_excerpt": _excerpt(target_overlap),
                    "similarity": similarity,
                }
            )
            if len(matches) >= MAX_FRAGMENT_MATCHES:
                break
        return matches

    async def _link_fragment_pair(
        self,
        relation: FeishuCrossDocumentRelation,
        match: dict,
        *,
        primary_version_id: str,
        versions: dict[str, FeishuMaterialVersion],
        items: dict[str, FeishuSourceItem],
        chunks: dict[str, KnowledgeChunk | ParsedFragment],
        operator_id: str,
    ) -> FeishuLogicalKnowledge:
        source_is_primary = primary_version_id == relation.source_version_id
        primary_version = versions[primary_version_id]
        alias_version_id = relation.target_version_id if source_is_primary else relation.source_version_id
        alias_version = versions[alias_version_id]
        primary_chunk_id = match["source_chunk_id"] if source_is_primary else match["target_chunk_id"]
        alias_chunk_id = match["target_chunk_id"] if source_is_primary else match["source_chunk_id"]
        primary_chunk = chunks[primary_chunk_id]
        alias_chunk = chunks[alias_chunk_id]
        primary_item = items[primary_version.item_id]

        existing_refs = list(
            await self.session.scalars(
                select(FeishuKnowledgeSourceFragment).where(
                    FeishuKnowledgeSourceFragment.status == "ACTIVE",
                    (
                        (FeishuKnowledgeSourceFragment.version_id == primary_version.version_id)
                        & (FeishuKnowledgeSourceFragment.chunk_id == primary_chunk.chunk_id)
                    )
                    | (
                        (FeishuKnowledgeSourceFragment.version_id == alias_version.version_id)
                        & (FeishuKnowledgeSourceFragment.chunk_id == alias_chunk.chunk_id)
                    ),
                )
            )
        )
        primary_ref = next(
            (
                ref
                for ref in existing_refs
                if ref.version_id == primary_version.version_id and ref.chunk_id == primary_chunk.chunk_id
            ),
            None,
        )
        group_ids = {ref.logical_knowledge_id for ref in existing_refs}
        logical_knowledge_id = primary_ref.logical_knowledge_id if primary_ref else next(iter(group_ids), None)
        if logical_knowledge_id is None:
            logical_knowledge_id = _stable_id(
                "logical-knowledge",
                relation.relation_id,
                match["match_id"],
            )
            logical_knowledge = FeishuLogicalKnowledge(
                logical_knowledge_id=logical_knowledge_id,
                source_id=primary_item.source_id,
                title=_fragment_title(
                    primary_chunk.content, primary_item.title or "未命名资料", primary_chunk.chunk_index
                ),
                status="ACTIVE",
                created_by=operator_id,
                updated_by=operator_id,
            )
            self.session.add(logical_knowledge)
            await self.session.flush()
        else:
            logical_knowledge = await self.session.scalar(
                select(FeishuLogicalKnowledge).where(
                    FeishuLogicalKnowledge.logical_knowledge_id == logical_knowledge_id
                )
            )
            if logical_knowledge is None:
                raise LookupError(f"Logical knowledge not found: {logical_knowledge_id}")

        for merged_id in sorted(group_ids - {logical_knowledge.logical_knowledge_id}):
            await self._merge_group(
                merged_id,
                logical_knowledge.logical_knowledge_id,
                operator_id=operator_id,
            )

        primary_source = await self._upsert_source_fragment(
            logical_knowledge,
            relation,
            primary_version,
            primary_chunk,
            role=KnowledgeSourceRole.PRIMARY,
        )
        await self._upsert_source_fragment(
            logical_knowledge,
            relation,
            alias_version,
            alias_chunk,
            role=KnowledgeSourceRole.ALIAS,
        )
        await self._set_segment_publication_state(primary_chunk, "INCLUDED")
        await self._set_segment_publication_state(alias_chunk, "ALIAS")
        active_sources = list(
            await self.session.scalars(
                select(FeishuKnowledgeSourceFragment).where(
                    FeishuKnowledgeSourceFragment.logical_knowledge_id == logical_knowledge.logical_knowledge_id,
                    FeishuKnowledgeSourceFragment.status == "ACTIVE",
                )
            )
        )
        for source in active_sources:
            source.source_role = (
                KnowledgeSourceRole.PRIMARY
                if source.source_ref_id == primary_source.source_ref_id
                else KnowledgeSourceRole.ALIAS
            )
        logical_knowledge.primary_source_ref_id = primary_source.source_ref_id
        logical_knowledge.updated_by = operator_id
        logical_knowledge.updated_at = utc_now_naive()
        await self.session.flush()
        return logical_knowledge

    async def _set_segment_publication_state(
        self,
        chunk: KnowledgeChunk | ParsedFragment,
        state: str,
    ) -> None:
        segment_id = getattr(chunk, "segment_id", None)
        if not segment_id:
            return
        segment = await self.session.scalar(
            select(FeishuSourceSegment).where(FeishuSourceSegment.segment_id == segment_id)
        )
        if segment is not None:
            segment.publication_state = state

    async def _upsert_source_fragment(
        self,
        logical_knowledge: FeishuLogicalKnowledge,
        relation: FeishuCrossDocumentRelation,
        version: FeishuMaterialVersion,
        chunk: KnowledgeChunk | ParsedFragment,
        *,
        role: str,
    ) -> FeishuKnowledgeSourceFragment:
        source = await self.session.scalar(
            select(FeishuKnowledgeSourceFragment).where(
                FeishuKnowledgeSourceFragment.logical_knowledge_id == logical_knowledge.logical_knowledge_id,
                FeishuKnowledgeSourceFragment.version_id == version.version_id,
                FeishuKnowledgeSourceFragment.chunk_id == chunk.chunk_id,
            )
        )
        if source is None:
            segment_id = getattr(chunk, "segment_id", None)
            locator = dict(getattr(chunk, "locator_json", {}) or {})
            if not locator:
                locator = {"chunk_index": chunk.chunk_index}
            source = FeishuKnowledgeSourceFragment(
                source_ref_id=_stable_id(
                    "knowledge-source",
                    logical_knowledge.logical_knowledge_id,
                    version.version_id,
                    chunk.chunk_id,
                ),
                logical_knowledge_id=logical_knowledge.logical_knowledge_id,
                relation_id=relation.relation_id,
                version_id=version.version_id,
                yuxi_file_id=version.yuxi_file_id,
                chunk_id=chunk.chunk_id,
                segment_id=segment_id,
                chunk_index=chunk.chunk_index,
                content_hash=_content_hash(chunk.content),
                content_snapshot=chunk.content,
                locator_json=locator,
                source_role=role,
                status="ACTIVE",
            )
            self.session.add(source)
        else:
            source.source_role = role
            source.status = "ACTIVE"
            source.content_hash = _content_hash(chunk.content)
            source.content_snapshot = chunk.content
            source.segment_id = getattr(chunk, "segment_id", None)
            source.locator_json = dict(getattr(chunk, "locator_json", {}) or source.locator_json or {})
            source.updated_at = utc_now_naive()
        await self.session.flush()
        return source

    async def _merge_group(self, source_group_id: str, target_group_id: str, *, operator_id: str) -> None:
        if source_group_id == target_group_id:
            return
        source_group = await self.session.scalar(
            select(FeishuLogicalKnowledge).where(FeishuLogicalKnowledge.logical_knowledge_id == source_group_id)
        )
        if source_group is None or source_group.status == "MERGED":
            return
        refs = list(
            await self.session.scalars(
                select(FeishuKnowledgeSourceFragment).where(
                    FeishuKnowledgeSourceFragment.logical_knowledge_id == source_group_id
                )
            )
        )
        existing_target_keys = {
            (ref.version_id, ref.chunk_id)
            for ref in await self.session.scalars(
                select(FeishuKnowledgeSourceFragment).where(
                    FeishuKnowledgeSourceFragment.logical_knowledge_id == target_group_id
                )
            )
        }
        for ref in refs:
            if (ref.version_id, ref.chunk_id) in existing_target_keys:
                ref.status = "MERGED"
                continue
            ref.logical_knowledge_id = target_group_id
            ref.source_role = KnowledgeSourceRole.ALIAS
        source_group.status = "MERGED"
        source_group.merged_into_id = target_group_id
        source_group.primary_source_ref_id = None
        source_group.updated_by = operator_id
        source_group.updated_at = utc_now_naive()

    @staticmethod
    def _candidate_response(
        relation: FeishuCrossDocumentRelation,
        versions: dict[str, FeishuMaterialVersion],
        items: dict[str, FeishuSourceItem],
        matches: list[dict],
        decision: FeishuDuplicateRelationDecision | None,
    ) -> dict:
        source_version = versions[relation.source_version_id]
        target_version = versions[relation.target_version_id]
        source_item = items[source_version.item_id]
        target_item = items[target_version.item_id]
        return {
            "relation_id": relation.relation_id,
            "relation_type": relation.relation_type,
            "status": relation.status,
            "source": {
                "version_id": source_version.version_id,
                "revision": source_version.revision,
                "title": source_item.title or "未命名资料",
                "path": source_item.path_text,
            },
            "target": {
                "version_id": target_version.version_id,
                "revision": target_version.revision,
                "title": target_item.title or "未命名资料",
                "path": target_item.path_text,
            },
            "fragment_matches": matches,
            "decision": {
                "strategy": decision.strategy,
                "primary_version_id": decision.primary_version_id,
                "logical_knowledge_ids": decision.logical_knowledge_ids or [],
                "fragment_match_ids": decision.fragment_match_ids or [],
                "comment": decision.comment,
                "decided_by": decision.decided_by,
                "created_at": decision.created_at.isoformat() if decision.created_at else None,
            }
            if decision
            else None,
        }
