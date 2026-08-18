from __future__ import annotations

from difflib import SequenceMatcher
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.governance.domain import CrossDocumentRelationType
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuGovernanceReview,
    FeishuMaterialVersion,
    FeishuSourceItem,
)


def _tokens(value: str | None) -> set[str]:
    return {token for token in (value or "").lower().replace("/", " ").split() if token}


def _scope(version: FeishuMaterialVersion) -> dict:
    return dict((version.processing_params or {}).get("applicability_scope") or {})


def _facts(version: FeishuMaterialVersion) -> dict[str, str]:
    raw = (version.processing_params or {}).get("comparison_facts") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value is not None}


class CrossDocumentComparisonService:
    """Create deterministic, auditable comparison evidence after parsing.

    The comparison is intentionally conservative. It only compares the current
    version with the same item's earlier versions and a bounded set of related
    items. A future LLM comparator can replace ``_classify`` without changing
    the persisted relation contract.
    """

    CANDIDATE_STATUSES = {"parsed", "awaiting_review", "published"}
    MAX_CANDIDATES = 30

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def compare_version(self, version_id: str) -> list[FeishuCrossDocumentRelation]:
        current_row = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuMaterialVersion.version_id == version_id)
            )
        ).one_or_none()
        if current_row is None:
            raise LookupError(f"Material version not found: {version_id}")
        current, current_item = current_row

        candidate_rows = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(
                    FeishuSourceItem.source_id == current_item.source_id,
                    FeishuMaterialVersion.version_id != version_id,
                    FeishuMaterialVersion.processing_status.in_(self.CANDIDATE_STATUSES),
                    or_(
                        FeishuMaterialVersion.item_id == current.item_id,
                        FeishuSourceItem.title.is_not(None),
                    ),
                )
                .order_by(FeishuMaterialVersion.created_at.desc())
                .limit(self.MAX_CANDIDATES)
            )
        ).all()

        review = await self._ensure_review(current.version_id)
        _ = review
        relations: list[FeishuCrossDocumentRelation] = []
        for candidate, candidate_item in candidate_rows:
            evidence = self._classify(current, current_item, candidate, candidate_item)
            if evidence is None:
                continue
            relation = await self._upsert_relation(current, candidate, evidence)
            relations.append(relation)
        await self.session.flush()
        return relations

    async def has_open_conflict(self, version_id: str) -> bool:
        relation = await self.session.scalar(
            select(FeishuCrossDocumentRelation.id)
            .where(
                FeishuCrossDocumentRelation.status == "open",
                FeishuCrossDocumentRelation.relation_type == CrossDocumentRelationType.CONFLICT,
                or_(
                    FeishuCrossDocumentRelation.source_version_id == version_id,
                    FeishuCrossDocumentRelation.target_version_id == version_id,
                ),
            )
            .limit(1)
        )
        return relation is not None

    async def _ensure_review(self, version_id: str) -> FeishuGovernanceReview:
        review = await self.session.scalar(
            select(FeishuGovernanceReview).where(FeishuGovernanceReview.version_id == version_id)
        )
        if review is not None:
            return review
        review = FeishuGovernanceReview(
            review_id=f"review-{version_id}"[:64],
            version_id=version_id,
            status="pending",
        )
        self.session.add(review)
        await self.session.flush()
        return review

    async def _upsert_relation(
        self,
        current: FeishuMaterialVersion,
        candidate: FeishuMaterialVersion,
        evidence: dict,
    ) -> FeishuCrossDocumentRelation:
        version_ids = sorted((current.version_id, candidate.version_id))
        comparison_key = f"{version_ids[0]}:{version_ids[1]}"
        relation = await self.session.scalar(
            select(FeishuCrossDocumentRelation).where(
                FeishuCrossDocumentRelation.comparison_key == comparison_key
            )
        )
        if relation is None:
            relation = FeishuCrossDocumentRelation(
                relation_id=f"relation-{uuid4().hex}",
                comparison_key=comparison_key,
                source_version_id=current.version_id,
                target_version_id=candidate.version_id,
                relation_type=evidence["relation_type"],
                similarity=evidence["similarity"],
                confidence=evidence["confidence"],
                same_content=evidence["same_content"],
                different_content=evidence["different_content"],
                scope_difference=evidence["scope_difference"],
                reasoning=evidence["reasoning"],
                status="open",
            )
            self.session.add(relation)
        elif relation.status == "open":
            relation.relation_type = evidence["relation_type"]
            relation.similarity = evidence["similarity"]
            relation.confidence = evidence["confidence"]
            relation.same_content = evidence["same_content"]
            relation.different_content = evidence["different_content"]
            relation.scope_difference = evidence["scope_difference"]
            relation.reasoning = evidence["reasoning"]
        return relation

    @staticmethod
    def _classify(
        current: FeishuMaterialVersion,
        current_item: FeishuSourceItem,
        candidate: FeishuMaterialVersion,
        candidate_item: FeishuSourceItem,
    ) -> dict | None:
        same_item = current.item_id == candidate.item_id
        title_similarity = SequenceMatcher(
            None,
            (current_item.title or "").lower(),
            (candidate_item.title or "").lower(),
        ).ratio()
        path_similarity = SequenceMatcher(
            None,
            (current_item.path_text or "").lower(),
            (candidate_item.path_text or "").lower(),
        ).ratio()
        similarity = 1.0 if current.content_hash == candidate.content_hash else max(title_similarity, path_similarity)
        if current.content_hash == candidate.content_hash:
            relation_type = CrossDocumentRelationType.EXACT_DUPLICATE
        elif same_item:
            relation_type = CrossDocumentRelationType.OVERLAP
        elif similarity < 0.45:
            return None
        else:
            relation_type = CrossDocumentRelationType.OVERLAP

        current_scope = _scope(current)
        candidate_scope = _scope(candidate)
        scope_difference = {
            key: {"current": current_scope.get(key), "candidate": candidate_scope.get(key)}
            for key in set(current_scope) | set(candidate_scope)
            if current_scope.get(key) != candidate_scope.get(key)
        }
        current_facts = _facts(current)
        candidate_facts = _facts(candidate)
        different_content = [
            {"field": key, "current": current_facts[key], "candidate": candidate_facts[key]}
            for key in sorted(set(current_facts) & set(candidate_facts))
            if current_facts[key] != candidate_facts[key]
        ]
        if different_content and not scope_difference:
            relation_type = CrossDocumentRelationType.CONFLICT
        elif scope_difference and relation_type != CrossDocumentRelationType.EXACT_DUPLICATE:
            relation_type = CrossDocumentRelationType.CONDITIONAL_VARIANT

        same_content = []
        if _tokens(current_item.title) & _tokens(candidate_item.title):
            same_content.append("标题包含相同产品或主题词")
        if same_item:
            same_content.append("同一飞书资料的不同版本")
        if current.content_hash == candidate.content_hash:
            same_content.append("内容哈希一致")
        if not same_content:
            same_content.append("目录和标题语义相近")
        if same_item and current.revision != candidate.revision:
            different_content.append(
                {"field": "revision", "current": current.revision, "candidate": candidate.revision}
            )
        return {
            "relation_type": relation_type,
            "similarity": round(similarity, 4),
            "confidence": (
                0.99
                if current.content_hash == candidate.content_hash
                else round(min(0.95, 0.55 + similarity * 0.4), 4)
            ),
            "same_content": same_content,
            "different_content": different_content,
            "scope_difference": scope_difference,
            "reasoning": "相同内容哈希直接判定为重复；其余关系依据同一资料版本、标题/目录相似度和适用范围字段生成。",
        }
