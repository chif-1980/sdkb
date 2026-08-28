"""Product knowledge-source policy resolution for the enterprise assistant."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.product_chat.auth_service import ProductAuthError
from yuxi.repositories.feishu_knowledge_repository import FeishuKnowledgeRepository
from yuxi.storage.postgres.models_product import (
    FeishuDepartmentBinding,
    FeishuUserDepartmentMembership,
)

PRODUCT_SOURCE_ENV = "PRODUCT_FEISHU_SOURCE_ID"


@dataclass(frozen=True, slots=True)
class ProductKnowledgeScope:
    source_id: str
    kb_id: str
    allowed_file_ids: tuple[str, ...]


class ProductSourcePolicyService:
    """Resolve the immutable source/file scope used by product retrieval."""

    def __init__(
        self,
        *,
        db: AsyncSession,
        knowledge_base: Any | None = None,
        knowledge_base_manager: Any | None = None,
    ) -> None:
        self._db = db
        self._knowledge_base = knowledge_base_manager or knowledge_base

    async def resolve_scope(self, user: Any) -> ProductKnowledgeScope:
        source_id = os.environ.get(PRODUCT_SOURCE_ENV, "").strip()
        if not source_id:
            raise ProductAuthError("PRODUCT_SOURCE_UNAVAILABLE", 503)

        repository = FeishuKnowledgeRepository(self._db)
        source = await repository.get_source(source_id)
        if source is None or not source.enabled or not source.target_kb_id:
            raise ProductAuthError("PRODUCT_SOURCE_UNAVAILABLE", 503)

        user_dict = user if isinstance(user, dict) else user.to_dict()
        try:
            if not await self._policy_accessible(user, user_dict, source.target_kb_id):
                raise ProductAuthError("KNOWLEDGE_ACCESS_DENIED", 403)
        except Exception as exc:
            raise ProductAuthError("KNOWLEDGE_ACCESS_DENIED", 403) from exc

        allowed_file_ids = tuple(sorted(set(await self._list_published_file_ids(repository, source_id))))
        return ProductKnowledgeScope(
            source_id=source.source_id,
            kb_id=source.target_kb_id,
            allowed_file_ids=allowed_file_ids,
        )

    async def _policy_accessible(self, user: Any, user_dict: dict[str, Any], kb_id: str) -> bool:
        policy_manager = self._policy_manager()
        if await policy_manager.check_policy_accessible(user_dict, kb_id):
            return True

        primary_department_id = user_dict.get("department_id")
        department_ids: list[int] = []
        user_id = getattr(user, "id", None)
        if user_id is not None:
            result = await self._db.execute(
                select(FeishuDepartmentBinding.department_id)
                .join(
                    FeishuUserDepartmentMembership,
                    FeishuUserDepartmentMembership.department_binding_id == FeishuDepartmentBinding.id,
                )
                .where(FeishuUserDepartmentMembership.user_id == user_id)
                .order_by(FeishuUserDepartmentMembership.position)
            )
            department_ids.extend(result.scalars().all())

        for department_id in dict.fromkeys(department_ids):
            if department_id == primary_department_id:
                continue
            candidate = dict(user_dict)
            candidate["department_id"] = department_id
            if await policy_manager.check_policy_accessible(candidate, kb_id):
                return True
        return False

    async def _list_published_file_ids(self, repository: FeishuKnowledgeRepository, source_id: str) -> list[str]:
        return await repository.list_published_file_ids(source_id)

    def _policy_manager(self) -> Any:
        if self._knowledge_base is not None:
            return self._knowledge_base
        from yuxi.knowledge.runtime import knowledge_base

        return knowledge_base
