from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import case, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
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
    awaiting_review_count: int
    failed_count: int
    source_invalid_count: int
    last_full_sync_at: datetime | None
    last_incremental_sync_at: datetime | None


class FeishuKnowledgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_source(self, source_id: str) -> FeishuSource | None:
        async with self._read_transaction():
            result = await self.session.execute(select(FeishuSource).where(FeishuSource.source_id == source_id))
            return result.scalar_one_or_none()

    async def has_successful_full_scan(self, source_id: str) -> bool:
        async with self._read_transaction():
            result = await self.session.execute(
                select(FeishuSyncRun.run_id)
                .where(
                    FeishuSyncRun.source_id == source_id,
                    FeishuSyncRun.run_type == "full",
                    FeishuSyncRun.status == "succeeded",
                )
                .limit(1)
            )
            return result.scalar_one_or_none() is not None

    async def get_sync_run_status(self, run_id: str) -> str | None:
        async with self._read_transaction():
            result = await self.session.execute(select(FeishuSyncRun.status).where(FeishuSyncRun.run_id == run_id))
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
        scan_scope: str | None = None,
    ) -> FeishuSource:
        if scan_scope is not None and scan_scope not in {"root", "space"}:
            raise ValueError("scan_scope must be 'root' or 'space'")
        async with self._write_transaction():
            if self.session.get_bind().dialect.name == "postgresql":
                result = await self.session.execute(
                    self._build_postgres_source_upsert(
                        source_id=source_id,
                        name=name,
                        wiki_root_token=wiki_root_token,
                        wiki_root_url=wiki_root_url,
                        target_kb_id=target_kb_id,
                        credential_env_name=credential_env_name,
                        enabled=enabled,
                        created_by=created_by,
                        scan_scope=scan_scope,
                    ).returning(FeishuSource),
                    execution_options={"populate_existing": True},
                )
                return result.scalar_one()
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
            if scan_scope is not None:
                source.scan_scope = scan_scope
            await self.session.flush()
            return source

    @staticmethod
    def _build_postgres_source_upsert(
        *,
        source_id: str,
        name: str,
        wiki_root_token: str,
        wiki_root_url: str | None,
        target_kb_id: str,
        credential_env_name: str,
        enabled: bool,
        created_by: str | None,
        scan_scope: str | None = None,
    ):
        statement = postgres_insert(FeishuSource).values(
            source_id=source_id,
            name=name,
            wiki_root_token=wiki_root_token,
            wiki_root_url=wiki_root_url,
            target_kb_id=target_kb_id,
            credential_env_name=credential_env_name,
            enabled=enabled,
            created_by=created_by,
            scan_scope=scan_scope or "root",
        )
        set_values = {
            "name": statement.excluded.name,
            "wiki_root_token": statement.excluded.wiki_root_token,
            "wiki_root_url": statement.excluded.wiki_root_url,
            "target_kb_id": statement.excluded.target_kb_id,
            "credential_env_name": statement.excluded.credential_env_name,
            "enabled": statement.excluded.enabled,
            "updated_at": utc_now(),
        }
        if scan_scope is not None:
            set_values["scan_scope"] = statement.excluded.scan_scope
        return statement.on_conflict_do_update(
            index_elements=[FeishuSource.source_id],
            set_=set_values,
        )

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

    async def claim_queued_sync_run(
        self,
        *,
        run_id: str,
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
            run_result = await self.session.execute(
                select(FeishuSyncRun).where(FeishuSyncRun.run_id == run_id).with_for_update()
            )
            run = run_result.scalar_one_or_none()
            if run is None or run.source_id != source_id or run.run_type != run_type or run.status != "queued":
                raise RuntimeError(f"Queued Feishu sync run is unavailable: {run_id}")
            running_result = await self.session.execute(
                select(FeishuSyncRun.run_id).where(
                    FeishuSyncRun.source_id == source_id,
                    FeishuSyncRun.status == "running",
                    FeishuSyncRun.run_id != run_id,
                )
            )
            if running_result.scalar_one_or_none() is not None:
                has_running_run = True
            else:
                run.status = "running"
                run.started_at = utc_now()
                run.operator_id = operator_id
                await self.session.flush()
        if has_running_run:
            raise ConcurrentSyncRunError(f"A Feishu sync run is already active for source: {source_id}")
        return run

    async def fail_sync_run(
        self,
        *,
        run_id: str,
        source_id: str,
        error_summary: str,
        operator_id: str | None = None,
    ) -> bool:
        async with self._write_transaction():
            result = await self.session.execute(
                select(FeishuSyncRun)
                .where(FeishuSyncRun.run_id == run_id, FeishuSyncRun.source_id == source_id)
                .with_for_update()
            )
            run = result.scalar_one_or_none()
            if run is None or run.status not in {"queued", "running"}:
                return False
            from_status = run.status
            run.status = "failed"
            run.finished_at = utc_now()
            run.failed_count = max(run.failed_count or 0, 1)
            run.error_summary = error_summary
            self.session.add(
                FeishuProcessingEvent(
                    source_id=source_id,
                    event_type="scan_failed",
                    from_status=from_status,
                    to_status="failed",
                    operator_id=operator_id,
                    message=error_summary,
                    payload_json={"run_id": run_id},
                )
            )
            await self.session.flush()
            return True

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
        impact_summary: dict | None = None,
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
                impact_summary=impact_summary,
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
        impact_summary: dict | None = None,
    ) -> int:
        async with self._write_transaction():
            await self._mark_seen_items(source_id=source_id, item_keys=seen_item_keys, seen_at=seen_at)
            invalidated_count = await self._mark_source_invalid(
                source_id=source_id,
                seen_item_keys=seen_item_keys,
            )
            completed_impact = dict(impact_summary or {})
            completed_impact["deleted"] = invalidated_count
            completed_impact["affectedKnowledgeCount"] = (
                int(completed_impact.get("new") or 0)
                + int(completed_impact.get("modified") or 0)
                + invalidated_count
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
                impact_summary=completed_impact,
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

    async def list_published_file_ids(self, source_id: str) -> list[str]:
        """Return file IDs for the source's active, published, approved material versions."""
        statement = (
            select(FeishuMaterialVersion.yuxi_file_id)
            .join(
                FeishuSourceItem,
                FeishuMaterialVersion.version_id == FeishuSourceItem.active_version_id,
            )
            .where(
                FeishuSourceItem.source_id == source_id,
                FeishuSourceItem.source_validity == "valid",
                FeishuMaterialVersion.processing_status == "published",
                FeishuMaterialVersion.review_status == "approved",
                FeishuMaterialVersion.published_at.is_not(None),
                FeishuMaterialVersion.yuxi_file_id.is_not(None),
                ~exists().where(
                    FeishuCrossDocumentRelation.status == "open",
                    FeishuCrossDocumentRelation.relation_type == "CONFLICT",
                    or_(
                        FeishuCrossDocumentRelation.source_version_id == FeishuMaterialVersion.version_id,
                        FeishuCrossDocumentRelation.target_version_id == FeishuMaterialVersion.version_id,
                    ),
                ),
            )
            .distinct()
            .order_by(FeishuMaterialVersion.yuxi_file_id)
        )
        async with self._read_transaction():
            result = await self.session.execute(statement)
            return [file_id for file_id in result.scalars().all() if file_id]

    async def create_material_version(
        self,
        *,
        item_id: str,
        revision: str,
        content_hash: str,
        processing_status: str,
        processing_params: dict | None,
        sync_run_id: str | None = None,
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
                sync_run_id=sync_run_id,
                revision=revision,
                content_hash=content_hash,
                processing_status=processing_status,
                processing_params=processing_params or {},
            )
            self.session.add(version)
            await self.session.flush()
            return version, True

    async def queue_archived_versions_for_processing(
        self,
        *,
        source_id: str,
        operator_id: str | None,
    ) -> list[str]:
        queued_version_ids: list[str] = []
        async with self._write_transaction():
            candidates = await self.session.execute(
                select(FeishuMaterialVersion.version_id, FeishuSourceItem.item_id)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(
                    FeishuSourceItem.source_id == source_id,
                    FeishuSourceItem.item_type.in_({"page", "attachment"}),
                    FeishuMaterialVersion.processing_status == "discovered",
                    FeishuMaterialVersion.source_object_path.is_not(None),
                )
                .order_by(FeishuMaterialVersion.id)
            )
            for version_id, item_id in candidates:
                result = await self.session.execute(
                    update(FeishuMaterialVersion)
                    .where(
                        FeishuMaterialVersion.version_id == version_id,
                        FeishuMaterialVersion.processing_status == "discovered",
                    )
                    .values(processing_status="processing_queued")
                )
                if result.rowcount != 1:
                    continue
                queued_version_ids.append(version_id)
                self.session.add(
                    FeishuProcessingEvent(
                        source_id=source_id,
                        item_id=item_id,
                        version_id=version_id,
                        event_type="processing_queued",
                        from_status="discovered",
                        to_status="processing_queued",
                        operator_id=operator_id,
                    )
                )
            await self.session.flush()
        return queued_version_ids

    async def reconcile_interrupted_work(self) -> dict[str, int]:
        reconciled_runs = 0
        reconciled_versions = 0
        interrupted_message = "Interrupted by service restart"
        material_transitions = {
            "processing_queued": "parse_failed",
            "processing": "parse_failed",
            "publish_queued": "publish_failed",
            "publishing": "publish_failed",
            "removal_pending": "removal_failed",
        }
        async with self._write_transaction():
            run_result = await self.session.execute(
                select(FeishuSyncRun).where(FeishuSyncRun.status.in_({"queued", "running"})).with_for_update()
            )
            for run in run_result.scalars():
                from_status = run.status
                run.status = "failed"
                run.finished_at = utc_now()
                run.error_summary = interrupted_message
                run.failed_count = max(run.failed_count or 0, 1)
                self.session.add(
                    FeishuProcessingEvent(
                        source_id=run.source_id,
                        event_type="startup_reconciled",
                        from_status=from_status,
                        to_status="failed",
                        message=interrupted_message,
                        payload_json={"run_id": run.run_id},
                    )
                )
                reconciled_runs += 1

            version_result = await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuMaterialVersion.processing_status.in_(material_transitions))
                .with_for_update()
            )
            for version, item in version_result:
                from_status = version.processing_status
                to_status = material_transitions[from_status]
                version.processing_status = to_status
                if to_status.endswith("_failed"):
                    version.error_message = interrupted_message
                self.session.add(
                    FeishuProcessingEvent(
                        source_id=item.source_id,
                        item_id=item.item_id,
                        version_id=version.version_id,
                        event_type="startup_reconciled",
                        from_status=from_status,
                        to_status=to_status,
                        message=interrupted_message,
                    )
                )
                reconciled_versions += 1
            await self.session.flush()
        return {"sync_runs": reconciled_runs, "material_versions": reconciled_versions}

    async def mark_seen_items(self, *, source_id: str, item_keys: set[str], seen_at: datetime) -> int:
        async with self._write_transaction():
            return await self._mark_seen_items(source_id=source_id, item_keys=item_keys, seen_at=seen_at)

    async def list_unseen_valid_page_roots(
        self,
        *,
        source_id: str,
        seen_item_keys: set[str],
    ) -> list[FeishuSourceItem]:
        conditions = [
            FeishuSourceItem.source_id == source_id,
            FeishuSourceItem.source_validity == "valid",
            FeishuSourceItem.item_type == "page",
        ]
        if seen_item_keys:
            conditions.append(FeishuSourceItem.item_key.not_in(seen_item_keys))
        async with self._read_transaction():
            result = await self.session.execute(select(FeishuSourceItem).where(*conditions))
            unseen_pages = list(result.scalars())
        unseen_keys = {item.item_key for item in unseen_pages}
        return [item for item in unseen_pages if item.parent_item_key not in unseen_keys]

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
                    func.sum(case((FeishuMaterialVersion.processing_status == "awaiting_review", 1), else_=0)),
                    func.sum(
                        case(
                            (
                                FeishuMaterialVersion.processing_status.in_(
                                    {"parse_failed", "publish_failed", "removal_failed"}
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
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
            run_result = await self.session.execute(
                select(
                    func.max(case((FeishuSyncRun.run_type == "full", FeishuSyncRun.started_at), else_=None)),
                    func.max(case((FeishuSyncRun.run_type == "incremental", FeishuSyncRun.started_at), else_=None)),
                ).where(FeishuSyncRun.source_id == source_id)
            )
        total, valid, invalid, unsupported, awaiting_review, failed = result.one()
        last_full_sync_at, last_incremental_sync_at = run_result.one()
        return FeishuSourceSummary(
            total_count=total or 0,
            valid_count=valid or 0,
            invalid_count=invalid or 0,
            unsupported_count=unsupported or 0,
            awaiting_review_count=awaiting_review or 0,
            failed_count=failed or 0,
            source_invalid_count=invalid or 0,
            last_full_sync_at=last_full_sync_at,
            last_incremental_sync_at=last_incremental_sync_at,
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
        impact_summary: dict | None = None,
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
                impact_summary=impact_summary or {},
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
