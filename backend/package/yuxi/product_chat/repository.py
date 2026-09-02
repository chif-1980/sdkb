"""Persistence boundary for enterprise assistant conversations and evidence."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuKnowledgeSourceFragment,
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
    KnowledgeChunk,
)
from yuxi.storage.postgres.models_product import (
    CitationKind,
    ConversationStatus,
    MessageCitation,
    MessageRole,
    ProductConversation,
    ProductMessage,
)
from yuxi.utils.datetime_utils import utc_now_naive

if TYPE_CHECKING:
    from yuxi.product_chat.answer_service import GroundedAnswer


class ProductChatNotFoundError(Exception):
    """Stable not-found error that does not reveal conversation ownership."""

    code = "CONVERSATION_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__(self.code)


class ProductMessageNotFoundError(Exception):
    """Stable not-found error for messages outside the current user's scope."""

    code = "MESSAGE_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__(self.code)


class ProductChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_conversations(self, owner_user_id: int) -> list[ProductConversation]:
        result = await self.session.execute(
            select(ProductConversation)
            .where(ProductConversation.owner_user_id == owner_user_id)
            .order_by(ProductConversation.updated_at.desc())
        )
        return list(result.scalars().all())

    async def create_conversation(self, owner_user_id: int, title: str) -> ProductConversation:
        conversation = ProductConversation(
            owner_user_id=owner_user_id,
            title=(title or "").strip() or None,
            status=ConversationStatus.ACTIVE,
        )
        try:
            self.session.add(conversation)
            await self.session.commit()
            await self.session.refresh(conversation)
            return conversation
        except Exception:
            await self.session.rollback()
            raise

    async def require_conversation(self, conversation_id: str, owner_user_id: int) -> ProductConversation:
        result = await self.session.execute(
            select(ProductConversation).where(
                ProductConversation.conversation_id == conversation_id,
                ProductConversation.owner_user_id == owner_user_id,
                ProductConversation.status == ConversationStatus.ACTIVE,
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            raise ProductChatNotFoundError
        return conversation

    async def require_viewable_conversation(
        self,
        conversation_id: str,
        owner_user_id: int,
    ) -> ProductConversation:
        """Load a conversation that the owner may open, including archived history."""
        result = await self.session.execute(
            select(ProductConversation).where(
                ProductConversation.conversation_id == conversation_id,
                ProductConversation.owner_user_id == owner_user_id,
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            raise ProductChatNotFoundError
        return conversation

    async def get_message_counts(self, conversation_ids: list[str]) -> dict[str, int]:
        if not conversation_ids:
            return {}
        result = await self.session.execute(
            select(ProductMessage.conversation_id, func.count(ProductMessage.id))
            .where(ProductMessage.conversation_id.in_(conversation_ids))
            .group_by(ProductMessage.conversation_id)
        )
        return {conversation_id: count for conversation_id, count in result.all()}

    async def list_messages_with_citations(
        self,
        conversation_id: str,
    ) -> list[tuple[ProductMessage, list[MessageCitation]]]:
        messages = list(
            (
                await self.session.execute(
                    select(ProductMessage)
                    .where(ProductMessage.conversation_id == conversation_id)
                    .order_by(ProductMessage.created_at, ProductMessage.id)
                )
            )
            .scalars()
            .all()
        )
        if not messages:
            return []

        citations = list(
            (
                await self.session.execute(
                    select(MessageCitation)
                    .where(MessageCitation.message_id.in_([message.message_id for message in messages]))
                    .order_by(MessageCitation.id)
                )
            )
            .scalars()
            .all()
        )
        citations_by_message: dict[str, list[MessageCitation]] = {}
        for citation in citations:
            citations_by_message.setdefault(citation.message_id, []).append(citation)
        return [(message, citations_by_message.get(message.message_id, [])) for message in messages]

    async def list_recent_messages(
        self,
        conversation_id: str,
        owner_user_id: int,
        *,
        limit: int,
    ) -> list[ProductMessage]:
        messages = list(
            (
                await self.session.execute(
                    select(ProductMessage)
                    .join(
                        ProductConversation,
                        ProductMessage.conversation_id == ProductConversation.conversation_id,
                    )
                    .where(
                        ProductMessage.conversation_id == conversation_id,
                        ProductConversation.owner_user_id == owner_user_id,
                        ProductConversation.status == ConversationStatus.ACTIVE,
                    )
                    .order_by(ProductMessage.created_at.desc(), ProductMessage.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        messages.reverse()
        return messages

    async def get_owned_citation(
        self,
        citation_id: str,
        owner_user_id: int,
    ) -> MessageCitation | None:
        result = await self.session.execute(
            select(MessageCitation)
            .join(
                ProductMessage,
                MessageCitation.message_id == ProductMessage.message_id,
            )
            .join(
                ProductConversation,
                ProductMessage.conversation_id == ProductConversation.conversation_id,
            )
            .where(
                MessageCitation.citation_id == citation_id,
                ProductConversation.owner_user_id == owner_user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_citation_material(
        self,
        citation: MessageCitation,
    ) -> tuple[
        FeishuSource | None,
        FeishuSourceItem | None,
        FeishuMaterialVersion | None,
    ]:
        source = await self.session.scalar(select(FeishuSource).where(FeishuSource.source_id == citation.source_id))
        item = await self.session.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == citation.item_id))
        version = await self.session.scalar(
            select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == citation.version_id)
        )
        return source, item, version

    async def archive_conversation(self, conversation_id: str, owner_user_id: int) -> None:
        try:
            result = await self.session.execute(
                update(ProductConversation)
                .where(
                    ProductConversation.conversation_id == conversation_id,
                    ProductConversation.owner_user_id == owner_user_id,
                    ProductConversation.status == ConversationStatus.ACTIVE,
                )
                .values(status=ConversationStatus.ARCHIVED, updated_at=utc_now_naive())
            )
            if result.rowcount != 1:
                await self.session.rollback()
                raise ProductChatNotFoundError
            await self.session.commit()
        except ProductChatNotFoundError:
            raise
        except Exception:
            await self.session.rollback()
            raise

    async def restore_conversation(self, conversation_id: str, owner_user_id: int) -> None:
        try:
            result = await self.session.execute(
                update(ProductConversation)
                .where(
                    ProductConversation.conversation_id == conversation_id,
                    ProductConversation.owner_user_id == owner_user_id,
                    ProductConversation.status == ConversationStatus.ARCHIVED,
                )
                .values(status=ConversationStatus.ACTIVE, updated_at=utc_now_naive())
            )
            if result.rowcount != 1:
                await self.session.rollback()
                raise ProductChatNotFoundError
            await self.session.commit()
        except ProductChatNotFoundError:
            raise
        except Exception:
            await self.session.rollback()
            raise

    async def set_message_feedback(
        self,
        message_id: str,
        owner_user_id: int,
        rating: str | None,
        *,
        reason_type: str | None = None,
        reason_text: str | None = None,
    ) -> ProductMessage:
        result = await self.session.execute(
            select(ProductMessage)
            .join(
                ProductConversation,
                ProductMessage.conversation_id == ProductConversation.conversation_id,
            )
            .where(
                ProductMessage.message_id == message_id,
                ProductMessage.role == MessageRole.ASSISTANT,
                ProductConversation.owner_user_id == owner_user_id,
                ProductConversation.status == ConversationStatus.ACTIVE,
            )
        )
        message = result.scalar_one_or_none()
        if message is None:
            raise ProductMessageNotFoundError
        message.feedback_rating = rating
        message.feedback_reason_type = reason_type if rating == "DISLIKE" else None
        message.feedback_reason_text = reason_text.strip() if rating == "DISLIKE" and reason_text else None
        await self.session.flush()
        return message

    async def append_exchange(
        self,
        conversation: ProductConversation,
        owner_user_id: int,
        user_content: str,
        answer: GroundedAnswer,
    ) -> tuple[ProductMessage, ProductMessage, list[MessageCitation]]:
        result = await self.session.execute(
            select(ProductConversation)
            .where(
                ProductConversation.conversation_id == conversation.conversation_id,
                ProductConversation.owner_user_id == owner_user_id,
                ProductConversation.status == ConversationStatus.ACTIVE,
            )
            .with_for_update()
        )
        active_conversation = result.scalar_one_or_none()
        if active_conversation is None:
            raise ProductChatNotFoundError

        user_message = ProductMessage(
            conversation_id=active_conversation.conversation_id,
            role=MessageRole.USER,
            content=user_content,
        )
        assistant_message = ProductMessage(
            conversation_id=active_conversation.conversation_id,
            role=MessageRole.ASSISTANT,
            content=answer.content,
            answer_status=answer.status,
            model_version=answer.model_version,
            prompt_version=answer.prompt_version,
        )
        now = utc_now_naive()
        if not (active_conversation.title or "").strip():
            active_conversation.title = user_content.strip()[:30] or None
        active_conversation.updated_at = now
        self.session.add_all([user_message, assistant_message])
        await self.session.flush()
        citations = [
            MessageCitation(
                message_id=assistant_message.message_id,
                kind=CitationKind.ENTERPRISE_EVIDENCE,
                source_id=citation.source_id,
                item_id=citation.item_id,
                version_id=citation.version_id,
                yuxi_file_id=citation.yuxi_file_id,
                chunk_id=citation.chunk_id,
                title=citation.title,
                source_url=citation.source_url,
                path_text=citation.path_text,
                locator=citation.locator,
                excerpt=citation.excerpt,
                media_type=getattr(citation, "media_type", None),
                image_url=getattr(citation, "image_url", None),
                preview_url=getattr(citation, "preview_url", None),
                image_alt=getattr(citation, "image_alt", None),
                source_version_at=(
                    citation.source_version_at.astimezone(UTC).replace(tzinfo=None)
                    if citation.source_version_at is not None and citation.source_version_at.tzinfo is not None
                    else citation.source_version_at
                ),
            )
            for citation in answer.citations
        ]
        self.session.add_all(citations)
        await self.session.flush()
        if citations:
            citations = list(
                (
                    await self.session.execute(
                        select(MessageCitation)
                        .where(MessageCitation.message_id == assistant_message.message_id)
                        .order_by(MessageCitation.id)
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
        return user_message, assistant_message, citations

    async def get_published_evidence(
        self,
        source_id: str,
        file_ids: list[str] | tuple[str, ...],
    ) -> dict[str, tuple[FeishuSourceItem, FeishuMaterialVersion]]:
        if not file_ids:
            return {}
        result = await self.session.execute(
            select(FeishuSourceItem, FeishuMaterialVersion)
            .join(
                FeishuMaterialVersion,
                FeishuSourceItem.active_version_id == FeishuMaterialVersion.version_id,
            )
            .join(FeishuSource, FeishuSource.source_id == FeishuSourceItem.source_id)
            .where(
                FeishuSource.source_id == source_id,
                FeishuSource.enabled.is_(True),
                FeishuSourceItem.source_id == source_id,
                FeishuSourceItem.source_validity == "valid",
                FeishuSourceItem.publication_status == "ACTIVE",
                FeishuMaterialVersion.processing_status == "published",
                FeishuMaterialVersion.review_status == "approved",
                FeishuMaterialVersion.published_at.is_not(None),
                FeishuMaterialVersion.yuxi_file_id.is_not(None),
                FeishuMaterialVersion.yuxi_file_id.in_(file_ids),
                ~exists().where(
                    FeishuCrossDocumentRelation.status == "open",
                    FeishuCrossDocumentRelation.relation_type == "CONFLICT",
                    or_(
                        FeishuCrossDocumentRelation.source_version_id == FeishuMaterialVersion.version_id,
                        FeishuCrossDocumentRelation.target_version_id == FeishuMaterialVersion.version_id,
                    ),
                ),
            )
        )
        published: dict[str, tuple[FeishuSourceItem, FeishuMaterialVersion]] = {}
        ambiguous_file_ids: set[str] = set()
        for item, version in result.all():
            file_id = version.yuxi_file_id
            if file_id is None or file_id in ambiguous_file_ids:
                continue
            if file_id in published:
                published.pop(file_id)
                ambiguous_file_ids.add(file_id)
                continue
            published[file_id] = (item, version)
        return published

    async def get_chunk_governance(self, chunk_ids: list[str] | tuple[str, ...]) -> dict[str, dict]:
        """Resolve retrieval chunks to governed logical knowledge without exposing draft content."""
        normalized_ids = list(dict.fromkeys(chunk_id for chunk_id in chunk_ids if chunk_id))
        if not normalized_ids:
            return {}
        chunks = list(
            await self.session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.chunk_id.in_(normalized_ids)))
        )
        segment_ids_by_chunk: dict[str, tuple[str, ...]] = {}
        all_segment_ids: set[str] = set()
        for chunk in chunks:
            tags = chunk.tags if isinstance(chunk.tags, dict) else {}
            raw_segment_ids = tags.get("source_segment_ids")
            segment_ids = tuple(
                str(segment_id)
                for segment_id in (raw_segment_ids if isinstance(raw_segment_ids, list) else [])
                if isinstance(segment_id, str) and segment_id
            )
            segment_ids_by_chunk[chunk.chunk_id] = segment_ids
            all_segment_ids.update(segment_ids)

        reference_filter = FeishuKnowledgeSourceFragment.chunk_id.in_(normalized_ids)
        if all_segment_ids:
            reference_filter = or_(
                reference_filter,
                FeishuKnowledgeSourceFragment.segment_id.in_(all_segment_ids),
            )
        references = list(
            await self.session.scalars(
                select(FeishuKnowledgeSourceFragment).where(
                    FeishuKnowledgeSourceFragment.status == "ACTIVE",
                    reference_filter,
                )
            )
        )
        by_segment: dict[str, list[FeishuKnowledgeSourceFragment]] = {}
        by_chunk: dict[str, list[FeishuKnowledgeSourceFragment]] = {}
        for reference in references:
            by_chunk.setdefault(reference.chunk_id, []).append(reference)
            if reference.segment_id:
                by_segment.setdefault(reference.segment_id, []).append(reference)

        result: dict[str, dict] = {}
        for chunk in chunks:
            chunk_references = list(by_chunk.get(chunk.chunk_id, []))
            for segment_id in segment_ids_by_chunk.get(chunk.chunk_id, ()):
                chunk_references.extend(by_segment.get(segment_id, []))
            logical_ids = tuple(dict.fromkeys(reference.logical_knowledge_id for reference in chunk_references))
            roles = {str(reference.source_role) for reference in chunk_references}
            role = "PRIMARY" if "PRIMARY" in roles else "ALIAS" if "ALIAS" in roles else None
            tags = chunk.tags if isinstance(chunk.tags, dict) else {}
            result[chunk.chunk_id] = {
                "logical_knowledge_ids": logical_ids,
                "source_segment_ids": tuple(
                    dict.fromkeys(
                        [
                            *segment_ids_by_chunk.get(chunk.chunk_id, ()),
                            *[reference.segment_id for reference in chunk_references if reference.segment_id],
                        ]
                    )
                ),
                "source_role": role,
                "locator": dict(tags.get("locator") or {}) if isinstance(tags.get("locator"), dict) else {},
                "title_path": list(tags.get("title_path") or []) if isinstance(tags.get("title_path"), list) else [],
            }
        return result
