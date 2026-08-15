"""Persistence boundary for enterprise assistant conversations and evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_knowledge import FeishuMaterialVersion, FeishuSource, FeishuSourceItem
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


class ProductChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_conversations(self, owner_user_id: int) -> list[ProductConversation]:
        result = await self.session.execute(
            select(ProductConversation)
            .where(
                ProductConversation.owner_user_id == owner_user_id,
                ProductConversation.status == ConversationStatus.ACTIVE,
            )
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

    async def append_exchange(
        self,
        conversation: ProductConversation,
        owner_user_id: int,
        user_content: str,
        answer: GroundedAnswer,
    ) -> tuple[ProductMessage, ProductMessage]:
        try:
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
            self.session.add_all(
                [
                    MessageCitation(
                        message_id=assistant_message.message_id,
                        kind=CitationKind.ENTERPRISE_EVIDENCE,
                        source_id=citation.source_id,
                        item_id=citation.item_id,
                        version_id=citation.version_id,
                        yuxi_file_id=citation.yuxi_file_id,
                        title=citation.title,
                        source_url=citation.source_url,
                        path_text=citation.path_text,
                        locator=citation.locator,
                        excerpt=citation.excerpt,
                        source_version_at=citation.source_version_at,
                    )
                    for citation in answer.citations
                ]
            )
            await self.session.commit()
            return user_message, assistant_message
        except Exception:
            await self.session.rollback()
            raise

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
                FeishuMaterialVersion.processing_status == "published",
                FeishuMaterialVersion.review_status == "approved",
                FeishuMaterialVersion.published_at.is_not(None),
                FeishuMaterialVersion.yuxi_file_id.is_not(None),
                FeishuMaterialVersion.yuxi_file_id.in_(file_ids),
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
