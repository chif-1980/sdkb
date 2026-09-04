"""Governed material cards and downloads for enterprise-assistant chat."""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.product_chat.citation_service import CitationResolutionError, CitationService
from yuxi.product_chat.repository import ProductChatRepository
from yuxi.product_chat.schemas import ProductMaterialResponse
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
    KnowledgeFile,
)
from yuxi.storage.postgres.models_product import MessageCitation
from yuxi.utils.datetime_utils import format_utc_datetime


@dataclass(frozen=True, slots=True)
class ResolvedProductMaterial:
    response: ProductMaterialResponse
    citation: MessageCitation
    source: FeishuSource
    item: FeishuSourceItem
    version: FeishuMaterialVersion
    knowledge_file: KnowledgeFile


class ProductMaterialService:
    """Expose only current approved/published files already cited to the user."""

    def __init__(self, db: AsyncSession, knowledge_base_manager: Any | None = None) -> None:
        self._repository = ProductChatRepository(db)
        self._citations = CitationService(db, knowledge_base_manager=knowledge_base_manager)

    async def list_from_citations(
        self,
        citations: list[MessageCitation],
        user: Any,
    ) -> list[ProductMaterialResponse]:
        materials: list[ProductMaterialResponse] = []
        seen_file_ids: set[str] = set()
        for candidate in citations:
            if candidate.yuxi_file_id in seen_file_ids:
                continue
            try:
                resolved = await self.resolve(candidate.citation_id, user)
            except CitationResolutionError:
                # A source can be withdrawn or the user's permission can
                # change after an older answer was created. Such a file must
                # disappear from actionable results instead of leaking stale
                # metadata.
                continue
            seen_file_ids.add(resolved.citation.yuxi_file_id)
            materials.append(resolved.response)
        return materials

    async def resolve(self, material_id: str, user: Any) -> ResolvedProductMaterial:
        # The opaque material id is the owned citation id. This prevents a
        # caller from downloading an arbitrary guessed knowledge-file id.
        citation = await self._citations.resolve(material_id, user)
        source, item, version, knowledge_file = await self._repository.get_citation_material_with_file(citation)
        if source is None or item is None or version is None or knowledge_file is None or knowledge_file.is_folder:
            raise CitationResolutionError("MATERIAL_GONE", 410, "资料已失效")

        file_name = self._file_name(knowledge_file.filename, item.title, citation.title)
        updated_at = (
            format_utc_datetime(item.source_updated_at)
            or format_utc_datetime(version.published_at)
            or format_utc_datetime(version.updated_at)
            or format_utc_datetime(citation.source_version_at)
            or ""
        )
        response = ProductMaterialResponse(
            id=citation.citation_id,
            title=(item.title or citation.title or file_name).strip(),
            type=self._material_type(item.title or citation.title or file_name),
            file_name=file_name,
            mime_type=(
                knowledge_file.content_type
                or mimetypes.guess_type(file_name)[0]
                or "application/octet-stream"
            ),
            size_bytes=max(int(knowledge_file.file_size or 0), 0),
            updated_at=updated_at,
            summary=self._summary(citation.excerpt),
            status="PUBLISHED",
            approval_status="APPROVED",
            publication_status="PUBLISHED",
            citation=self._citation_response(citation),
        )
        return ResolvedProductMaterial(
            response=response,
            citation=citation,
            source=source,
            item=item,
            version=version,
            knowledge_file=knowledge_file,
        )

    @staticmethod
    def _citation_response(citation: MessageCitation):
        from yuxi.product_chat.schemas import CitationResponse

        return CitationResponse(
            id=citation.citation_id,
            kind=citation.kind,
            title=citation.title,
            path=citation.path_text,
            locator=citation.locator,
            excerpt=citation.excerpt,
            version_at=format_utc_datetime(citation.source_version_at),
            media_type=citation.media_type,
            image_url=citation.image_url,
            preview_url=citation.preview_url,
            image_alt=citation.image_alt,
        )

    @staticmethod
    def _file_name(stored_name: str | None, item_title: str | None, citation_title: str) -> str:
        candidate = (stored_name or item_title or citation_title or "资料").replace("\\", "/")
        return PurePosixPath(candidate).name or "资料"

    @staticmethod
    def _material_type(title: str) -> str:
        normalized = title.lower()
        if "宣传" in normalized:
            return "宣传手册"
        if "解决方案" in normalized or "方案" in normalized:
            return "解决方案"
        if any(keyword in normalized for keyword in ("产品说明", "产品手册", "白皮书", "产品介绍")):
            return "产品说明"
        return "企业资料"

    @staticmethod
    def _summary(excerpt: str) -> str:
        normalized = re.sub(r"\s+", " ", excerpt or "").strip()
        return normalized if len(normalized) <= 240 else f"{normalized[:237]}…"
