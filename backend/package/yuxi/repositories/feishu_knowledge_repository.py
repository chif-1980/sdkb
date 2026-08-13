from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuSource,
    FeishuSourceItem,
    FeishuSyncRun,
)
from yuxi.utils.datetime_utils import utc_now


class ConcurrentSyncRunError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FeishuSourceSummary:
    total_count: int
    valid_count: int
    invalid_count: int
    unsupported_count: int


class FeishuKnowledgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_source(self, source_id: str) -> FeishuSource | None:
        async with self._read_transaction():
            result = await self.session.execute(select(FeishuSource).where(FeishuSource.source_id == source_id))
            return result.scalar_one_or_none()

    async def get_or_create_source(
        self,
        *,
        source_id: str,
        name: str,
        wiki_root_token: str,
        wiki_root_url: str | None,
        target_kb_id: str,
        credential_env_name: str,
        enabled: bool = True,
        created_by: str | None = None,
    ) -> FeishuSource:
        async with self._write_transaction():
            result = await self.session.execute(
                select(FeishuSource).where(FeishuSource.source_id == source_id).with_for_update()
            )
            source = result.scalar_one_or_none()
            if source is None:
                source = FeishuSource(source_id=source_id, created_by=created_by)
                self.session.add(source)
            source.name = name
            source.wiki_root_token = wiki_root_token
            source.wiki_root_url = wiki_root_url
            source.target_kb_id = target_kb_id
            source.credential_env_name = credential_env_name
            source.enabled = enabled
            await self.session.flush()
            return source

    async def start_sync_run(
        self,
        *,
        source_id: str,
        run_type: str,
        operator_id: str | None = None,
    ) -> FeishuSyncRun:
        run: FeishuSyncRun | None = None
        has_running_run = False
        async with self._write_transaction():
            source_result = await self.session.execute(
                select(FeishuSource).where(FeishuSource.source_id == source_id).with_for_update()
            )
            if source_result.scalar_one_or_none() is None:
                raise LookupError(f"Feishu source not found: {source_id}")
            running_result = await self.session.execute(
                select(FeishuSyncRun.run_id).where(
                    FeishuSyncRun.source_id == source_id,
                    FeishuSyncRun.status == "running",
                )
            )
            if running_result.scalar_one_or_none() is not None:
                has_running_run = True
            else:
                run = FeishuSyncRun(
                    run_id=uuid4().hex,
                    source_id=source_id,
                    run_type=run_type,
                    status="running",
                    operator_id=operator_id,
                )
                self.session.add(run)
                await self.session.flush()
        if has_running_run:
            raise ConcurrentSyncRunError(f"A Feishu sync run is already active for source: {source_id}")
        if run is None:
            raise RuntimeError("Feishu sync run was not created")
        return run

    async def finish_sync_run(
        self,
        *,
        run_id: str,
        status: str,
        scanned_count: int,
        new_count: int,
        changed_count: int,
        unchanged_count: int,
        unsupported_count: int,
        failed_count: int,
        invalidated_count: int,
        error_summary: str | None = None,
    ) -> bool:
        async with self._write_transaction():
            return await self._finish_sync_run(
                run_id=run_id,
                status=status,
                scanned_count=scanned_count,
                new_count=new_count,
                changed_count=changed_count,
                unchanged_count=unchanged_count,
                unsupported_count=unsupported_count,
                failed_count=failed_count,
                invalidated_count=invalidated_count,
                error_summary=error_summary,
            )

    async def complete_successful_scan(
        self,
        *,
        run_id: str,
        source_id: str,
        seen_item_keys: set[str],
        seen_at: datetime,
        scanned_count: int,
        new_count: int,
        changed_count: int,
        unchanged_count: int,
        unsupported_count: int,
    ) -> int:
        async with self._write_transaction():
            await self._mark_seen_items(source_id=source_id, item_keys=seen_item_keys, seen_at=seen_at)
            invalidated_count = await self._mark_source_invalid(
                source_id=source_id,
                seen_item_keys=seen_item_keys,
            )
            updated = await self._finish_sync_run(
                run_id=run_id,
                status="succeeded",
                scanned_count=scanned_count,
                new_count=new_count,
                changed_count=changed_count,
                unchanged_count=unchanged_count,
                unsupported_count=unsupported_count,
                failed_count=0,
                invalidated_count=invalidated_count,
            )
            if not updated:
                raise ConcurrentSyncRunError(f"Feishu sync run is no longer running: {run_id}")
            return invalidated_count

    async def upsert_source_item(
        self,
        *,
        source_id: str,
        item_key: str,
        item_type: str,
        title: str | None,
        parent_item_key: str | None,
        path_text: str | None,
        source_url: str | None,
        source_updated_at: datetime | None,
        seen_at: datetime | None = None,
    ) -> tuple[FeishuSourceItem, bool]:
        async with self._write_transaction():
            await self.session.execute(
                select(FeishuSource.source_id).where(FeishuSource.source_id == source_id).with_for_update()
            )
            result = await self.session.execute(
                select(FeishuSourceItem).where(FeishuSourceItem.item_key == item_key).with_for_update()
            )
            item = result.scalar_one_or_none()
            created = item is None
            if item is None:
                item = FeishuSourceItem(item_id=uuid4().hex, source_id=source_id, item_key=item_key)
                self.session.add(item)
            elif item.source_id != source_id:
                raise ValueError(f"Feishu item key belongs to a different source: {item_key}")
            item.item_type = item_type
            item.title = title
            item.parent_item_key = parent_item_key
            item.path_text = path_text
            item.source_url = source_url
            item.source_updated_at = source_updated_at
            item.last_seen_at = seen_at or utc_now()
            item.source_validity = "valid"
            await self.session.flush()
            return item, created

    async def find_current_version(self, item_id: str) -> FeishuMaterialVersion | None:
        async with self._read_transaction():
            result = await self.session.execute(
                select(FeishuMaterialVersion)
                .where(FeishuMaterialVersion.item_id == item_id)
                .order_by(FeishuMaterialVersion.id.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def create_material_version(
        self,
        *,
        item_id: str,
        revision: str,
        content_hash: str,
        processing_status: str,
        processing_params: dict | None,
    ) -> tuple[FeishuMaterialVersion, bool]:
        async with self._write_transaction():
            item_result = await self.session.execute(
                select(FeishuSourceItem.item_id).where(FeishuSourceItem.item_id == item_id).with_for_update()
            )
            if item_result.scalar_one_or_none() is None:
                raise LookupError(f"Feishu source item not found: {item_id}")
            result = await self.session.execute(
                select(FeishuMaterialVersion).where(
                    FeishuMaterialVersion.item_id == item_id,
                    FeishuMaterialVersion.revision == revision,
                    FeishuMaterialVersion.content_hash == content_hash,
                )
            )
            version = result.scalar_one_or_none()
            if version is not None:
                return version, False
            version = FeishuMaterialVersion(
                version_id=uuid4().hex,
                item_id=item_id,
                revision=revision,
                content_hash=content_hash,
                processing_status=processing_status,
                processing_params=processing_params or {},
            )
            self.session.add(version)
            await self.session.flush()
            return version, True

    async def mark_seen_items(self, *, source_id: str, item_keys: set[str], seen_at: datetime) -> int:
        async with self._write_transaction():
            return await self._mark_seen_items(source_id=source_id, item_keys=item_keys, seen_at=seen_at)

    async def mark_source_invalid(self, *, source_id: str, seen_item_keys: set[str]) -> int:
        async with self._write_transaction():
            return await self._mark_source_invalid(source_id=source_id, seen_item_keys=seen_item_keys)

    async def append_event(
        self,
        *,
        source_id: str,
        event_type: str,
        item_id: str | None = None,
        version_id: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        operator_id: str | None = None,
        message: str | None = None,
        payload_json: dict | None = None,
    ) -> FeishuProcessingEvent:
        async with self._write_transaction():
            event = FeishuProcessingEvent(
                source_id=source_id,
                item_id=item_id,
                version_id=version_id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                operator_id=operator_id,
                message=message,
                payload_json=payload_json or {},
            )
            self.session.add(event)
            await self.session.flush()
            return event

    async def get_source_summary(self, source_id: str) -> FeishuSourceSummary:
        latest_version_ids = (
            select(
                FeishuMaterialVersion.item_id,
                func.max(FeishuMaterialVersion.id).label("version_id"),
            )
            .group_by(FeishuMaterialVersion.item_id)
            .subquery()
        )
        async with self._read_transaction():
            result = await self.session.execute(
                select(
                    func.count(FeishuSourceItem.id),
                    func.sum(case((FeishuSourceItem.source_validity == "valid", 1), else_=0)),
                    func.sum(case((FeishuSourceItem.source_validity == "invalid", 1), else_=0)),
                    func.sum(case((FeishuMaterialVersion.processing_status == "unsupported", 1), else_=0)),
                )
                .select_from(FeishuSourceItem)
                .outerjoin(
                    latest_version_ids,
                    latest_version_ids.c.item_id == FeishuSourceItem.item_id,
                )
                .outerjoin(
                    FeishuMaterialVersion,
                    FeishuMaterialVersion.id == latest_version_ids.c.version_id,
                )
                .where(FeishuSourceItem.source_id == source_id)
            )
        total, valid, invalid, unsupported = result.one()
        return FeishuSourceSummary(
            total_count=total or 0,
            valid_count=valid or 0,
            invalid_count=invalid or 0,
            unsupported_count=unsupported or 0,
        )

    async def _finish_sync_run(
        self,
        *,
        run_id: str,
        status: str,
        scanned_count: int,
        new_count: int,
        changed_count: int,
        unchanged_count: int,
        unsupported_count: int,
        failed_count: int,
        invalidated_count: int,
        error_summary: str | None = None,
    ) -> bool:
        statement = (
            update(FeishuSyncRun)
            .where(FeishuSyncRun.run_id == run_id, FeishuSyncRun.status == "running")
            .values(
                status=status,
                finished_at=utc_now(),
                scanned_count=scanned_count,
                new_count=new_count,
                changed_count=changed_count,
                unchanged_count=unchanged_count,
                unsupported_count=unsupported_count,
                failed_count=failed_count,
                invalidated_count=invalidated_count,
                error_summary=error_summary,
            )
            .execution_options(synchronize_session="fetch")
        )
        result = await self.session.execute(statement)
        return result.rowcount == 1

    async def _mark_seen_items(self, *, source_id: str, item_keys: set[str], seen_at: datetime) -> int:
        if not item_keys:
            return 0
        statement = (
            update(FeishuSourceItem)
            .where(
                FeishuSourceItem.source_id == source_id,
                FeishuSourceItem.item_key.in_(item_keys),
            )
            .values(last_seen_at=seen_at, source_validity="valid")
            .execution_options(synchronize_session="fetch")
        )
        result = await self.session.execute(statement)
        return result.rowcount

    async def _mark_source_invalid(self, *, source_id: str, seen_item_keys: set[str]) -> int:
        conditions = [
            FeishuSourceItem.source_id == source_id,
            FeishuSourceItem.source_validity == "valid",
        ]
        if seen_item_keys:
            conditions.append(FeishuSourceItem.item_key.not_in(seen_item_keys))
        statement = (
            update(FeishuSourceItem)
            .where(*conditions)
            .values(source_validity="invalid")
            .execution_options(synchronize_session="fetch")
        )
        result = await self.session.execute(statement)
        return result.rowcount

    @asynccontextmanager
    async def _write_transaction(self):
        transaction = self.session.begin_nested() if self.session.in_transaction() else self.session.begin()
        async with transaction:
            yield

    @asynccontextmanager
    async def _read_transaction(self):
        transaction = self.session.begin_nested() if self.session.in_transaction() else self.session.begin()
        async with transaction:
            yield
