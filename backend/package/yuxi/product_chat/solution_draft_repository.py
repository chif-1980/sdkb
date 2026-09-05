"""Persistence for solution-draft projections and immutable versions."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from yuxi.product_chat.solution_draft import SolutionDraftPayload, parse_solution_draft
from yuxi.storage.postgres.models_product import (
    ProductConversation,
    ProductMessage,
    SolutionDraft,
    SolutionDraftVersion,
)
from yuxi.utils.datetime_utils import utc_now_naive


class SolutionDraftNotFoundError(LookupError):
    pass


class SolutionDraftRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_source_run(self, source_run_id: str) -> SolutionDraft | None:
        return await self.db.scalar(select(SolutionDraft).where(SolutionDraft.source_run_id == source_run_id))

    async def get_for_user(self, draft_id: str, user_id: int) -> SolutionDraft | None:
        return await self.db.scalar(
            select(SolutionDraft)
            .join(ProductConversation, ProductConversation.conversation_id == SolutionDraft.conversation_id)
            .where(SolutionDraft.id == draft_id, ProductConversation.owner_user_id == user_id)
        )

    async def list_versions(self, draft_id: str) -> list[SolutionDraftVersion]:
        result = await self.db.execute(
            select(SolutionDraftVersion)
            .where(SolutionDraftVersion.draft_id == draft_id)
            .order_by(SolutionDraftVersion.version.asc())
        )
        return list(result.scalars().all())

    async def create_from_run(
        self,
        *,
        conversation_id: str,
        source_run_id: str,
        payload: SolutionDraftPayload,
    ) -> SolutionDraft:
        existing = await self.get_by_source_run(source_run_id)
        if existing:
            return existing
        data = payload.as_json()
        draft = SolutionDraft(
            id=f"SD-{uuid4().hex}",
            conversation_id=conversation_id,
            source_run_id=source_run_id,
            current_version=1,
            status=payload.quality.status.value if payload.quality else "BLOCKED",
            title=payload.title,
            customer_context=payload.customer_context,
            executive_summary=payload.executive_summary,
            payload=data,
        )
        try:
            # The source run is unique.  Keep the insert and its first version
            # in a savepoint so a concurrent projection can safely lose the
            # race and then return the already-created draft without poisoning
            # the caller's outer transaction.
            async with self.db.begin_nested():
                self.db.add(draft)
                await self.db.flush()
                self.db.add(SolutionDraftVersion(draft_id=draft.id, version=1, payload=data))
                await self.db.flush()
        except IntegrityError:
            existing = await self.get_by_source_run(source_run_id)
            if existing:
                return existing
            raise
        return draft

    async def refresh_blocked_from_payload(
        self,
        *,
        draft_id: str,
        user_id: int,
        payload: SolutionDraftPayload,
    ) -> SolutionDraft:
        """Repair a legacy blocked projection without changing its run id.

        Older projections could be marked ``BLOCKED`` when the runtime emitted
        a richer risk object than the original schema accepted.  Once the
        parser is upgraded, re-projecting the same completed run should append
        an immutable version instead of creating a duplicate draft.
        """
        draft = await self.db.scalar(
            select(SolutionDraft)
            .join(ProductConversation, ProductConversation.conversation_id == SolutionDraft.conversation_id)
            .where(
                SolutionDraft.id == draft_id,
                ProductConversation.owner_user_id == user_id,
            )
            .with_for_update()
        )
        if not draft:
            raise SolutionDraftNotFoundError(draft_id)
        if str(draft.status or "").upper() != "BLOCKED":
            return draft

        data = payload.as_json()
        next_version = int(draft.current_version or 0) + 1
        draft.current_version = next_version
        draft.status = payload.quality.status.value if payload.quality else "BLOCKED"
        draft.title = payload.title
        draft.customer_context = payload.customer_context
        draft.executive_summary = payload.executive_summary
        draft.payload = data
        draft.updated_at = utc_now_naive()
        self.db.add(
            SolutionDraftVersion(
                draft_id=draft.id,
                version=next_version,
                payload=data,
            )
        )
        await self.db.flush()
        return draft

    async def update_for_user(
        self,
        *,
        draft_id: str,
        user_id: int,
        patch: dict,
    ) -> SolutionDraft:
        draft = await self.get_for_user(draft_id, user_id)
        if not draft:
            raise SolutionDraftNotFoundError(draft_id)
        merged = dict(draft.payload or {})
        merged.update(patch)
        payload = parse_solution_draft(merged)
        data = payload.as_json()
        next_version = int(draft.current_version or 0) + 1
        draft.current_version = next_version
        draft.status = payload.quality.status.value if payload.quality else "BLOCKED"
        draft.title = payload.title
        draft.customer_context = payload.customer_context
        draft.executive_summary = payload.executive_summary
        draft.payload = data
        draft.updated_at = utc_now_naive()
        self.db.add(
            SolutionDraftVersion(
                draft_id=draft.id,
                version=next_version,
                payload=data,
                editor_user_id=user_id,
            )
        )
        await self.db.flush()
        return draft

    async def attach_to_message(self, message_id: str, draft_id: str) -> None:
        message = await self.db.scalar(select(ProductMessage).where(ProductMessage.message_id == message_id))
        if message:
            message.solution_draft_id = draft_id
            await self.db.flush()


def serialize_solution_draft(
    draft: SolutionDraft,
    versions: list[SolutionDraftVersion] | None = None,
) -> dict:
    payload = dict(draft.payload or {})
    payload.update({
        "id": draft.id,
        "conversationId": draft.conversation_id,
        "sourceRunId": draft.source_run_id,
        "currentVersion": draft.current_version,
        "status": draft.status,
        "createdAt": draft.created_at.isoformat() if draft.created_at else None,
        "updatedAt": draft.updated_at.isoformat() if draft.updated_at else None,
    })
    if versions is not None:
        payload["versions"] = [
            {
                "version": version.version,
                "payload": version.payload or {},
                "createdAt": version.created_at.isoformat() if version.created_at else None,
            }
            for version in versions
        ]
    return payload
