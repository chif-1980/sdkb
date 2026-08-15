"""Current-policy and material validation for persisted product citations."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.product_chat.repository import ProductChatRepository
from yuxi.storage.postgres.models_product import MessageCitation


class CitationResolutionError(Exception):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        self.code = code
        self.status_code = status_code
        self.message = message
        super().__init__(code)


class CitationService:
    def __init__(
        self,
        db: AsyncSession,
        knowledge_base_manager: Any | None = None,
    ) -> None:
        self._repository = ProductChatRepository(db)
        if knowledge_base_manager is None:
            from yuxi.knowledge.runtime import knowledge_base

            knowledge_base_manager = knowledge_base
        self._knowledge_base = knowledge_base_manager

    async def resolve(
        self,
        citation_id: str,
        user: Any,
    ) -> MessageCitation:
        citation = await self._repository.get_owned_citation(citation_id, user.id)
        if citation is None:
            raise CitationResolutionError(
                "CITATION_NOT_FOUND",
                404,
                "引用不存在",
            )

        source, item, version = await self._repository.get_citation_material(citation)
        if source is None or not source.enabled:
            raise CitationResolutionError(
                "CITATION_GONE",
                410,
                "引用资料已失效",
            )

        try:
            user_dict = user if isinstance(user, dict) else user.to_dict()
            accessible = await self._knowledge_base.check_policy_accessible(
                user_dict,
                source.target_kb_id,
            )
        except Exception as exc:
            raise CitationResolutionError(
                "CITATION_ACCESS_DENIED",
                403,
                "当前无权访问该引用",
            ) from exc
        if accessible is not True:
            raise CitationResolutionError(
                "CITATION_ACCESS_DENIED",
                403,
                "当前无权访问该引用",
            )

        material_is_current = (
            item is not None
            and item.source_id == citation.source_id
            and item.source_validity == "valid"
            and item.active_version_id == citation.version_id
            and version is not None
            and version.item_id == citation.item_id
            and version.processing_status == "published"
            and version.review_status == "approved"
            and version.published_at is not None
            and version.yuxi_file_id == citation.yuxi_file_id
        )
        if not material_is_current:
            raise CitationResolutionError(
                "CITATION_GONE",
                410,
                "引用资料已失效",
            )
        return citation
