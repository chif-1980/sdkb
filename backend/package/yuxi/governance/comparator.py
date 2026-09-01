from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
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
    FeishuSourceSegment,
    KnowledgeChunk,
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

    The first stage is deliberately cheap: exact hashes, metadata similarity,
    and chunk text shingles narrow the candidate set before a later AI judge is
    introduced. Relations are persisted with a stable pair key so reruns are
    safe.
    """

    CANDIDATE_STATUSES = {"parsed", "awaiting_review", "published"}
    MAX_CANDIDATE_SCAN = 200
    MAX_CANDIDATES = 20
    MIN_SIMILARITY = 0.42
    MIN_CONTENT_OVERLAP = 0.72

    def __init__(
        self,
        session: AsyncSession,
        *,
        content_loader: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self.session = session
        self.content_loader = content_loader
        self._content_cache: dict[str, str] = {}

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

        recent_candidate_rows = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(
                    FeishuSourceItem.source_id == current_item.source_id,
                    FeishuMaterialVersion.version_id != version_id,
                    FeishuMaterialVersion.processing_status.in_(self.CANDIDATE_STATUSES),
                )
                .order_by(FeishuMaterialVersion.created_at.desc())
                .limit(self.MAX_CANDIDATE_SCAN)
            )
        ).all()
        published_candidate_rows = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(
                    FeishuSourceItem.source_id == current_item.source_id,
                    FeishuMaterialVersion.version_id != version_id,
                    FeishuMaterialVersion.processing_status == "published",
                    FeishuSourceItem.active_version_id == FeishuMaterialVersion.version_id,
                )
            )
        ).all()
        candidate_rows_by_version_id = {
            candidate.version_id: (candidate, item) for candidate, item in recent_candidate_rows
        }
        candidate_rows_by_version_id.update(
            {candidate.version_id: (candidate, item) for candidate, item in published_candidate_rows}
        )
        candidate_rows = list(candidate_rows_by_version_id.values())

        content_by_file_id = await self._load_contents(
            [current.yuxi_file_id, *(candidate.yuxi_file_id for candidate, _ in candidate_rows)]
        )
        current_content = content_by_file_id.get(current.yuxi_file_id or "", "")
        content_similarity_by_version_id = {
            candidate.version_id: self._content_similarity(
                current_content,
                content_by_file_id.get(candidate.yuxi_file_id or "", ""),
            )
            for candidate, _ in candidate_rows
        }

        # Preserve same-document history, then rank by parsed body before using
        # title and path as tie-breakers. Metadata alone must not crowd relevant
        # published knowledge out of the comparison set.
        ranked_candidates = sorted(
            candidate_rows,
            key=lambda row: (
                current.item_id == row[0].item_id or current.content_hash == row[0].content_hash,
                content_similarity_by_version_id[row[0].version_id],
                self._candidate_similarity(
                    current,
                    current_item,
                    row[0],
                    row[1],
                    "",
                    "",
                ),
            ),
            reverse=True,
        )[: self.MAX_CANDIDATES]

        review = await self._ensure_review(current.version_id)
        _ = review
        relations: list[FeishuCrossDocumentRelation] = []
        for candidate, candidate_item in ranked_candidates:
            candidate_content = content_by_file_id.get(candidate.yuxi_file_id or "", "")
            evidence = self._classify(
                current,
                current_item,
                candidate,
                candidate_item,
                current_content=current_content,
                candidate_content=candidate_content,
            )
            if evidence is None:
                continue
            relation = await self._upsert_relation(current, candidate, evidence)
            relations.append(relation)
        await self.session.flush()
        return relations

    async def compare_source(
        self,
        source_id: str,
        *,
        version_ids: list[str] | None = None,
        progress_callback: Callable[[int, int, int], Awaitable[None]] | None = None,
    ) -> dict[str, int]:
        """Compare all eligible versions for a source, reporting batch progress."""
        statement = (
            select(FeishuMaterialVersion.version_id)
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
            .where(
                FeishuSourceItem.source_id == source_id,
                FeishuMaterialVersion.processing_status.in_(self.CANDIDATE_STATUSES),
            )
            .order_by(FeishuMaterialVersion.created_at.asc())
        )
        if version_ids:
            statement = statement.where(FeishuMaterialVersion.version_id.in_(version_ids))
        ids = list(await self.session.scalars(statement))
        total = len(ids)
        compared = 0
        relation_count = 0
        for version_id in ids:
            relation_count += len(await self.compare_version(version_id))
            compared += 1
            if progress_callback is not None:
                await progress_callback(compared, total, relation_count)
        return {"total": total, "compared": compared, "relations": relation_count}

    async def _load_contents(self, file_ids: list[str | None]) -> dict[str, str]:
        normalized_ids = list(dict.fromkeys(file_id for file_id in file_ids if file_id))
        if not normalized_ids:
            return {}
        segment_result = await self.session.execute(
            select(FeishuSourceSegment.yuxi_file_id, FeishuSourceSegment.content)
            .where(
                FeishuSourceSegment.yuxi_file_id.in_(normalized_ids),
                FeishuSourceSegment.status == "ACTIVE",
            )
            .order_by(FeishuSourceSegment.yuxi_file_id.asc(), FeishuSourceSegment.segment_index.asc())
        )
        segment_contents: dict[str, list[str]] = {}
        for file_id, content in segment_result.all():
            segment_contents.setdefault(str(file_id), []).append(content or "")
        loaded = {file_id: "\n\n".join(parts) for file_id, parts in segment_contents.items()}
        fallback_ids = [file_id for file_id in normalized_ids if not loaded.get(file_id)]
        if not fallback_ids:
            return loaded
        result = await self.session.execute(
            select(KnowledgeChunk.file_id, KnowledgeChunk.content)
            .where(KnowledgeChunk.file_id.in_(fallback_ids))
            .order_by(KnowledgeChunk.file_id.asc(), KnowledgeChunk.chunk_index.asc())
        )
        contents: dict[str, list[str]] = {}
        for file_id, content in result.all():
            contents.setdefault(str(file_id), []).append(content or "")
        loaded.update({file_id: "\n\n".join(parts) for file_id, parts in contents.items()})
        if self.content_loader is not None:
            for file_id in fallback_ids:
                if loaded.get(file_id):
                    continue
                if file_id not in self._content_cache:
                    self._content_cache[file_id] = (await self.content_loader(file_id)) or ""
                if self._content_cache[file_id]:
                    loaded[file_id] = self._content_cache[file_id]
        return loaded

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
            select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.comparison_key == comparison_key)
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
        elif relation.status == "open" or relation.human_decision == "NO_TEXT_EVIDENCE":
            relation.relation_type = evidence["relation_type"]
            relation.similarity = evidence["similarity"]
            relation.confidence = evidence["confidence"]
            relation.same_content = evidence["same_content"]
            relation.different_content = evidence["different_content"]
            relation.scope_difference = evidence["scope_difference"]
            relation.reasoning = evidence["reasoning"]
            relation.status = "open"
            relation.human_decision = None
            relation.human_comment = None
            relation.resolved_by = None
            relation.resolved_at = None
        return relation

    @staticmethod
    def _classify(
        current: FeishuMaterialVersion,
        current_item: FeishuSourceItem,
        candidate: FeishuMaterialVersion,
        candidate_item: FeishuSourceItem,
        *,
        current_content: str = "",
        candidate_content: str = "",
    ) -> dict | None:
        same_item = current.item_id == candidate.item_id
        body_similarity = CrossDocumentComparisonService._local_content_similarity(
            current_content,
            candidate_content,
        )
        bodies_available = bool(current_content.strip() and candidate_content.strip())
        if bodies_available and current.content_hash == candidate.content_hash:
            relation_type = CrossDocumentRelationType.EXACT_DUPLICATE
            similarity = 1.0
        elif not bodies_available or body_similarity < CrossDocumentComparisonService.MIN_CONTENT_OVERLAP:
            return None
        else:
            relation_type = CrossDocumentRelationType.OVERLAP
            similarity = body_similarity

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
        if bodies_available and current.content_hash == candidate.content_hash:
            same_content.append("内容哈希一致")
        same_content.append(f"正文局部相似度 {body_similarity:.0%}")
        if same_item and current.revision != candidate.revision:
            different_content.append(
                {"field": "revision", "current": current.revision, "candidate": candidate.revision}
            )
        return {
            "relation_type": relation_type,
            "similarity": round(similarity, 4),
            "confidence": (
                0.99
                if bodies_available and current.content_hash == candidate.content_hash
                else round(min(0.95, 0.5 + body_similarity * 0.45), 4)
            ),
            "same_content": same_content,
            "different_content": different_content,
            "scope_difference": scope_difference,
            "reasoning": (
                "标题和目录只用于筛选候选资料；关系类型由两边已解析正文的局部相似段落、适用范围和结构化事实共同确定。"
            ),
        }

    @classmethod
    def _candidate_similarity(
        cls,
        current: FeishuMaterialVersion,
        current_item: FeishuSourceItem,
        candidate: FeishuMaterialVersion,
        candidate_item: FeishuSourceItem,
        current_content: str,
        candidate_content: str,
    ) -> float:
        if current.content_hash == candidate.content_hash:
            return 1.0
        if current.item_id == candidate.item_id:
            return 0.99
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
        content_similarity = cls._content_similarity(current_content, candidate_content)
        return max(title_similarity, path_similarity, content_similarity)

    @staticmethod
    def _content_similarity(current_content: str, candidate_content: str) -> float:
        if not current_content or not candidate_content:
            return 0.0
        current_features = _text_features(current_content)
        candidate_features = _text_features(candidate_content)
        if not current_features or not candidate_features:
            return 0.0
        return len(current_features & candidate_features) / len(current_features | candidate_features)

    @classmethod
    def _local_content_similarity(cls, current_content: str, candidate_content: str) -> float:
        if not current_content or not candidate_content:
            return 0.0
        global_similarity = cls._content_similarity(current_content, candidate_content)
        best = global_similarity
        current_passages = [_normalized_text(item) for item in _text_passages(current_content)]
        candidate_passages = [_normalized_text(item) for item in _text_passages(candidate_content)]
        for current_normalized in current_passages:
            if len(current_normalized) < 30:
                continue
            for candidate_normalized in candidate_passages:
                if len(candidate_normalized) < 30:
                    continue
                shorter, longer = sorted((len(current_normalized), len(candidate_normalized)))
                if shorter / longer < 0.55:
                    continue
                ratio = SequenceMatcher(
                    None,
                    current_normalized,
                    candidate_normalized,
                    autojunk=False,
                ).ratio()
                best = max(best, ratio)
                if best >= 0.995:
                    return 1.0
        return round(best, 4)


def _text_features(value: str) -> set[str]:
    normalized = re.sub(r"\s+", "", value.lower())[:200_000]
    if not normalized:
        return set()
    features: set[str] = set(re.findall(r"[a-z0-9]+", normalized))
    chinese = "".join(re.findall(r"[\u3400-\u9fff]", normalized))
    features.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {feature for feature in features if feature}


def _normalized_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", value.lower())[:4000]


def _text_passages(value: str, *, limit: int = 100) -> list[str]:
    passages: list[str] = []
    for block in re.split(r"\n+|(?<=[。！？!?])", value):
        cleaned = block.strip()
        if not cleaned:
            continue
        if len(cleaned) <= 1200:
            passages.append(cleaned)
            continue
        for start in range(0, len(cleaned), 800):
            passages.append(cleaned[start : start + 1000])
            if len(passages) >= limit:
                return passages
        if len(passages) >= limit:
            return passages
    return passages[:limit]
