from __future__ import annotations

from dataclasses import dataclass
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_admin_user, get_db
from yuxi.integrations.feishu import FeishuClient, FeishuClientError
from yuxi.integrations.feishu.service import FeishuScanService
from yuxi.knowledge.runtime import knowledge_base
from yuxi.repositories.feishu_knowledge_repository import FeishuKnowledgeRepository as _BaseRepository
from yuxi.services.task_service import TaskContext, tasker
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuSource,
    FeishuSourceItem,
    FeishuSyncRun,
)
from yuxi.storage.postgres.models_business import User
from yuxi.utils.datetime_utils import utc_now

feishu_knowledge = APIRouter(
    prefix="/feishu-knowledge",
    tags=["feishu-knowledge"],
    dependencies=[Depends(get_admin_user)],
)


class RemovalAdapter(Protocol):
    async def remove(self, *, kb_id: str, file_id: str) -> None: ...


class KnowledgeRemovalAdapter:
    async def remove(self, *, kb_id: str, file_id: str) -> None:
        await knowledge_base.delete_file(kb_id, file_id)


class MinioFeishuArchiveAdapter:
    BUCKET = "knowledgebases"
    SAFE_EXTENSIONS = {
        ".bmp",
        ".docx",
        ".gif",
        ".jpeg",
        ".jpg",
        ".md",
        ".pdf",
        ".png",
        ".pptx",
        ".tif",
        ".tiff",
        ".txt",
        ".webp",
        ".xlsx",
    }

    async def archive(
        self,
        *,
        source_id: str,
        item_id: str,
        version_id: str,
        item_type: str,
        title: str,
        content: bytes,
        content_type: str | None,
    ) -> str:
        extension = ".md" if item_type == "page" else Path(title).suffix.lower()
        if extension not in self.SAFE_EXTENSIONS:
            extension = ".bin"
        object_name = f"feishu/{source_id}/{item_id}/{version_id}/source{extension}"
        await get_minio_client().aupload_file(
            self.BUCKET,
            object_name,
            content,
            content_type=content_type,
        )
        return f"minio://{self.BUCKET}/{object_name}"


@dataclass(frozen=True, slots=True)
class PublishResult:
    file_id: str
    chunk_count: int = 0


class PublishAdapter(Protocol):
    async def publish(
        self,
        *,
        kb_id: str,
        object_path: str,
        source_url: str | None,
        wiki_path: str | None,
        version_id: str,
        page_info: dict,
        operator_id: str,
        file_id: str | None = None,
    ) -> PublishResult: ...


class KnowledgePublishAdapter:
    """Calls the existing upload/parse/index service layer without HTTP self-calls."""

    async def publish(
        self,
        *,
        kb_id: str,
        object_path: str,
        source_url: str | None,
        wiki_path: str | None,
        version_id: str,
        page_info: dict,
        operator_id: str,
        file_id: str | None = None,
    ) -> PublishResult:
        citation = {
            "source_url": source_url,
            "wiki_path": wiki_path,
            "material_version": version_id,
            "page_info": page_info,
        }
        params = {"content_type": "file", "feishu": citation}
        if file_id is None:
            file_meta = await knowledge_base.add_file_record(
                kb_id,
                object_path,
                params=params,
                operator_id=operator_id,
            )
            file_id = file_meta["file_id"]
            parsed = await knowledge_base.parse_file(kb_id, file_id, operator_id=operator_id)
            if parsed.get("status") != "parsed":
                raise RuntimeError(f"Feishu material parsing did not complete: {parsed.get('status')}")
        indexed = await knowledge_base.index_file(
            kb_id,
            file_id,
            operator_id=operator_id,
            params=params,
        )
        if indexed.get("status") not in {"indexed", "success"}:
            raise RuntimeError(f"Feishu material indexing did not complete: {indexed.get('status')}")
        return PublishResult(file_id=file_id, chunk_count=int(indexed.get("chunk_count") or 0))


class FeishuKnowledgeRepository(_BaseRepository):
    """Task-4 query and queued-run extension over the scan repository."""

    def __init__(self, session: AsyncSession, *, queued_run_id: str | None = None) -> None:
        super().__init__(session)
        self.queued_run_id = queued_run_id

    async def queue_sync_run(self, *, source_id: str, run_type: str, operator_id: str | None):
        run = FeishuSyncRun(
            run_id=uuid4().hex,
            source_id=source_id,
            run_type=run_type,
            status="queued",
            operator_id=operator_id,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def cancel_queued_run(self, run_id: str) -> None:
        await self.session.execute(
            update(FeishuSyncRun)
            .where(FeishuSyncRun.run_id == run_id, FeishuSyncRun.status == "queued")
            .values(status="cancelled", finished_at=utc_now())
        )

    async def start_sync_run(self, *, source_id: str, run_type: str, operator_id: str | None = None):
        if self.queued_run_id is None:
            return await super().start_sync_run(source_id=source_id, run_type=run_type, operator_id=operator_id)
        result = await self.session.execute(
            select(FeishuSyncRun).where(FeishuSyncRun.run_id == self.queued_run_id).with_for_update()
        )
        run = result.scalar_one_or_none()
        if run is None or run.source_id != source_id or run.run_type != run_type or run.status != "queued":
            raise RuntimeError(f"Queued Feishu sync run is unavailable: {self.queued_run_id}")
        run.status = "running"
        run.started_at = utc_now()
        await self.session.flush()
        return run


class FeishuReviewService:
    APPROVABLE_STATUSES = {"parsed", "awaiting_review"}

    def __init__(self, session: AsyncSession, *, removal_adapter: RemovalAdapter | None = None) -> None:
        self.session = session
        self.removal_adapter = removal_adapter or KnowledgeRemovalAdapter()

    async def approve(self, version_id: str, *, operator_id: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.review_status != "pending" or version.processing_status not in self.APPROVABLE_STATUSES:
                raise ValueError("Only pending parsed material can be approved")
            from_status = version.processing_status
            version.review_status = "approved"
            version.reviewer_id = operator_id
            version.reviewed_at = utc_now()
            version.processing_status = "publish_queued"
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="approved",
                from_status=from_status,
                to_status="publish_queued",
                operator_id=operator_id,
            )
            await self.session.flush()
            return version

    async def reject(self, version_id: str, *, operator_id: str, reason: str) -> FeishuMaterialVersion:
        reason = reason.strip()
        if not reason:
            raise ValueError("Reject reason is required")
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.review_status != "pending":
                raise ValueError("Only pending material can be rejected")
            version.review_status = "rejected"
            version.reviewer_id = operator_id
            version.reviewed_at = utc_now()
            version.review_comment = reason
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="rejected",
                from_status="pending",
                to_status="rejected",
                operator_id=operator_id,
                message=reason,
            )
            await self.session.flush()
            return version

    async def retry(self, version_id: str, *, operator_id: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if not self._is_failed_status(version.processing_status):
                raise ValueError("Only failed material can be retried")
            from_status = version.processing_status
            version.processing_status = "publish_queued" if from_status == "publish_failed" else "processing_queued"
            version.retry_count = (version.retry_count or 0) + 1
            version.error_code = None
            version.error_message = None
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="retry_queued",
                from_status=from_status,
                to_status=version.processing_status,
                operator_id=operator_id,
            )
            await self.session.flush()
            return version

    async def claim_publish(self, version_id: str) -> tuple[FeishuMaterialVersion, FeishuSourceItem, FeishuSource]:
        async with self._transaction():
            version, item, source = await self._get_material(version_id, lock=True)
            if version.processing_status != "publish_queued":
                raise ValueError("Material is not queued for publishing")
            version.processing_status = "publishing"
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="publishing",
                from_status="publish_queued",
                to_status="publishing",
            )
            await self.session.flush()
            return version, item, source

    async def claim_processing(self, version_id: str) -> tuple[FeishuMaterialVersion, FeishuSourceItem, FeishuSource]:
        async with self._transaction():
            version, item, source = await self._get_material(version_id, lock=True)
            if version.processing_status != "processing_queued":
                raise ValueError("Material is not queued for processing")
            version.processing_status = "processing"
            await self.session.flush()
            return version, item, source

    async def mark_processing_succeeded(self, version_id: str, *, file_id: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "processing":
                raise ValueError("Material is not processing")
            version.processing_status = "awaiting_review"
            version.yuxi_file_id = file_id
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="parsed",
                from_status="processing",
                to_status="awaiting_review",
            )
            await self.session.flush()
            return version

    async def mark_processing_failed(self, version_id: str, *, message: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "processing":
                raise ValueError("Material is not processing")
            version.processing_status = "parse_failed"
            version.error_message = message
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="parse_failed",
                from_status="processing",
                to_status="parse_failed",
                message=message,
            )
            await self.session.flush()
            return version

    async def mark_publish_succeeded(
        self, version_id: str, *, yuxi_file_id: str, chunk_count: int = 0
    ) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status not in {"publish_queued", "publishing"}:
                raise ValueError("Material is not queued for publishing")
            old_active_id = item.active_version_id
            if old_active_id and old_active_id != version.version_id:
                old_result = await self.session.execute(
                    select(FeishuMaterialVersion)
                    .where(FeishuMaterialVersion.version_id == old_active_id)
                    .with_for_update()
                )
                old_version = old_result.scalar_one_or_none()
                if old_version is not None:
                    old_version.processing_status = "replaced"
                    old_version.replaced_at = utc_now()
            item.active_version_id = version.version_id
            version.processing_status = "published"
            version.yuxi_file_id = yuxi_file_id
            version.chunk_count = chunk_count
            version.published_at = utc_now()
            version.error_code = None
            version.error_message = None
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="published",
                from_status="publish_queued",
                to_status="published",
                payload_json={"previous_active_version_id": old_active_id},
            )
            await self.session.flush()
            return version

    async def mark_publish_failed(self, version_id: str, *, message: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status not in {"publish_queued", "publishing"}:
                raise ValueError("Material is not queued for publishing")
            version.processing_status = "publish_failed"
            version.error_message = message
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="publish_failed",
                from_status="publish_queued",
                to_status="publish_failed",
                message=message,
            )
            await self.session.flush()
            return version

    async def confirm_removal(self, version_id: str, *, operator_id: str) -> FeishuMaterialVersion:
        version, item, source = await self._claim_removal(version_id, operator_id=operator_id)
        try:
            await self.removal_adapter.remove(kb_id=source.target_kb_id, file_id=version.yuxi_file_id)
        except Exception as exc:
            await self._restore_removal(version_id, message=str(exc))
            raise
        return await self._finish_removal(version_id, operator_id=operator_id)

    async def _claim_removal(
        self, version_id: str, *, operator_id: str
    ) -> tuple[FeishuMaterialVersion, FeishuSourceItem, FeishuSource]:
        async with self._transaction():
            version, item, source = await self._get_material(version_id, lock=True)
            if item.source_validity != "invalid":
                raise ValueError("Material source must be invalid before removal")
            if (
                item.active_version_id != version.version_id
                or version.processing_status != "published"
                or not version.yuxi_file_id
            ):
                raise ValueError("Material is not the active published version")
            version.processing_status = "removal_pending"
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="removal_started",
                from_status="published",
                to_status="removal_pending",
                operator_id=operator_id,
            )
            await self.session.flush()
            return version, item, source

    async def _finish_removal(self, version_id: str, *, operator_id: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "removal_pending" or item.active_version_id != version.version_id:
                raise ValueError("Material removal is no longer pending")
            item.active_version_id = None
            version.processing_status = "removed"
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="removal_confirmed",
                from_status="published",
                to_status="removed",
                operator_id=operator_id,
            )
            await self.session.flush()
            return version

    async def _restore_removal(self, version_id: str, *, message: str) -> None:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "removal_pending":
                return
            version.processing_status = "published"
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="removal_failed",
                from_status="removal_pending",
                to_status="published",
                message=message,
            )
            await self.session.flush()

    async def _get_material(
        self, version_id: str, *, lock: bool = False
    ) -> tuple[FeishuMaterialVersion, FeishuSourceItem, FeishuSource]:
        statement = (
            select(FeishuMaterialVersion, FeishuSourceItem, FeishuSource)
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
            .join(FeishuSource, FeishuSource.source_id == FeishuSourceItem.source_id)
            .where(FeishuMaterialVersion.version_id == version_id)
        )
        if lock:
            statement = statement.with_for_update()
        result = await self.session.execute(statement)
        row = result.one_or_none()
        if row is None:
            raise LookupError(f"Feishu material not found: {version_id}")
        return row

    def _append_event(
        self,
        *,
        source_id: str,
        item_id: str,
        version_id: str,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        operator_id: str | None = None,
        message: str | None = None,
        payload_json: dict | None = None,
    ) -> None:
        self.session.add(
            FeishuProcessingEvent(
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
        )

    @staticmethod
    def _is_failed_status(value: str) -> bool:
        return value == "failed" or value.endswith("_failed") or value.startswith("error_")

    @asynccontextmanager
    async def _transaction(self):
        transaction = self.session.begin_nested() if self.session.in_transaction() else self.session.begin()
        async with transaction:
            yield


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    wiki_root_token: str = Field(min_length=1, max_length=255)
    wiki_root_url: str | None = Field(default=None, max_length=1024)
    target_kb_id: str = Field(min_length=1, max_length=80)
    credential_env_name: str = Field(default="FEISHU_ACCESS_TOKEN", min_length=1, max_length=255)
    enabled: bool = True

    @field_validator("name", "wiki_root_token", "target_kb_id", "credential_env_name")
    @classmethod
    def identifiers_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class ScanRequest(BaseModel):
    mode: Literal["full", "incremental"]


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class BatchActionRequest(BaseModel):
    action: Literal["approve", "reject", "retry", "confirm_removal"]
    version_ids: list[str] = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("version_ids")
    @classmethod
    def version_ids_must_be_unique_and_non_blank(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("version_ids must not contain blank values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("version_ids must be unique")
        return normalized

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def reject_requires_reason(self):
        if self.action == "reject" and not self.reason:
            raise ValueError("reason is required for reject")
        return self


def _iso(value):
    return value.isoformat() if value is not None else None


def _source_dict(source: FeishuSource) -> dict:
    return {
        "source_id": source.source_id,
        "name": source.name,
        "wiki_root_token": source.wiki_root_token,
        "wiki_root_url": source.wiki_root_url,
        "target_kb_id": source.target_kb_id,
        "credential_env_name": source.credential_env_name,
        "enabled": source.enabled,
        "created_at": _iso(getattr(source, "created_at", None)),
        "updated_at": _iso(getattr(source, "updated_at", None)),
    }


def _run_dict(run: FeishuSyncRun) -> dict:
    return {
        "run_id": run.run_id,
        "source_id": run.source_id,
        "run_type": run.run_type,
        "status": run.status,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "scanned_count": run.scanned_count or 0,
        "new_count": run.new_count or 0,
        "changed_count": run.changed_count or 0,
        "unchanged_count": run.unchanged_count or 0,
        "unsupported_count": run.unsupported_count or 0,
        "failed_count": run.failed_count or 0,
        "invalidated_count": run.invalidated_count or 0,
        "error_summary": run.error_summary,
    }


def _material_dict(version: FeishuMaterialVersion, item: FeishuSourceItem, source: FeishuSource) -> dict:
    return {
        "version_id": version.version_id,
        "item_id": item.item_id,
        "source_id": item.source_id,
        "title": item.title,
        "item_type": item.item_type,
        "source_validity": item.source_validity,
        "active": item.active_version_id == version.version_id,
        "source_url": item.source_url,
        "wiki_path": item.path_text,
        "target_kb_id": source.target_kb_id,
        "revision": version.revision,
        "content_hash": version.content_hash,
        "source_object_path": version.source_object_path,
        "parsed_object_path": version.parsed_object_path,
        "processing_status": version.processing_status,
        "processing_params": version.processing_params or {},
        "error_code": version.error_code,
        "error_message": version.error_message,
        "review_status": version.review_status,
        "reviewer_id": version.reviewer_id,
        "reviewed_at": _iso(version.reviewed_at),
        "review_comment": version.review_comment,
        "retry_count": version.retry_count or 0,
        "yuxi_file_id": version.yuxi_file_id,
        "chunk_count": version.chunk_count or 0,
        "token_count": version.token_count or 0,
        "published_at": _iso(version.published_at),
        "replaced_at": _iso(version.replaced_at),
        "created_at": _iso(version.created_at),
        "updated_at": _iso(version.updated_at),
    }


def _event_dict(event: FeishuProcessingEvent) -> dict:
    return {
        "id": event.id,
        "source_id": event.source_id,
        "item_id": event.item_id,
        "version_id": event.version_id,
        "event_type": event.event_type,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "operator_id": event.operator_id,
        "message": event.message,
        "payload": event.payload_json or {},
        "created_at": _iso(event.created_at),
    }


@feishu_knowledge.get("/sources")
async def list_sources(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(FeishuSource).order_by(FeishuSource.created_at.desc()))
    return {"items": [_source_dict(source) for source in result.scalars()]}


@feishu_knowledge.post("/sources", status_code=201)
async def create_source(
    payload: SourceCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    source = await FeishuKnowledgeRepository(db).get_or_create_source(
        source_id=uuid4().hex,
        name=payload.name.strip(),
        wiki_root_token=payload.wiki_root_token.strip(),
        wiki_root_url=payload.wiki_root_url,
        target_kb_id=payload.target_kb_id.strip(),
        credential_env_name=payload.credential_env_name.strip(),
        enabled=payload.enabled,
        created_by=current_user.uid,
    )
    return _source_dict(source)


@feishu_knowledge.post("/sources/{source_id}/check")
async def check_source(source_id: str, db: AsyncSession = Depends(get_db)):
    source = await FeishuKnowledgeRepository(db).get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Feishu source not found")
    try:
        client = FeishuClient(credential_env_name=source.credential_env_name)
    except FeishuClientError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        node = await client.get_node(source.wiki_root_token)
    except FeishuClientError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await client.aclose()
    return {"status": "ok", "source_id": source_id, "root_title": node.title}


@feishu_knowledge.post("/sources/{source_id}/scan", status_code=202)
async def scan_source(
    source_id: str,
    payload: ScanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    repository = FeishuKnowledgeRepository(db)
    source = await repository.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Feishu source not found")
    run = await repository.queue_sync_run(
        source_id=source_id,
        run_type=payload.mode,
        operator_id=current_user.uid,
    )

    async def run_scan(context: TaskContext):
        async with pg_manager.get_async_session_context() as session:
            worker_repository = FeishuKnowledgeRepository(session, queued_run_id=run.run_id)
            worker_source = await worker_repository.get_source(source_id)
            if worker_source is None:
                raise LookupError(f"Feishu source not found: {source_id}")
            client = FeishuClient(credential_env_name=worker_source.credential_env_name)
            try:
                result = await FeishuScanService(
                    repository=worker_repository,
                    client=client,
                    archive_adapter=MinioFeishuArchiveAdapter(),
                ).scan(
                    source_id=source_id,
                    mode=payload.mode,
                    operator_id=current_user.uid,
                )
                await context.set_result({"run_id": result.run_id, "status": result.status})
                if result.status != "succeeded":
                    raise RuntimeError(result.error_summary or "Feishu scan failed")
                return {"run_id": result.run_id, "status": result.status}
            finally:
                await client.aclose()

    task, created = await tasker.enqueue_unique_by_payload(
        name=f"Feishu scan ({source.name})",
        task_type="feishu_scan",
        payload={"source_id": source_id, "run_id": run.run_id, "mode": payload.mode},
        payload_match={"source_id": source_id},
        statuses={"pending", "running"},
        coroutine=run_scan,
    )
    run_id = run.run_id
    if not created:
        await repository.cancel_queued_run(run.run_id)
        run_id = task.payload.get("run_id") or run_id
    return {"task_id": task.id, "run_id": run_id, "status": "queued", "created": created}


@feishu_knowledge.get("/sources/{source_id}/runs")
async def list_source_runs(source_id: str, db: AsyncSession = Depends(get_db)):
    if await FeishuKnowledgeRepository(db).get_source(source_id) is None:
        raise HTTPException(status_code=404, detail="Feishu source not found")
    result = await db.execute(
        select(FeishuSyncRun).where(FeishuSyncRun.source_id == source_id).order_by(FeishuSyncRun.started_at.desc())
    )
    return {"items": [_run_dict(run) for run in result.scalars()]}


@feishu_knowledge.get("/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(FeishuSyncRun).where(FeishuSyncRun.run_id == run_id))
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Feishu sync run not found")
    return _run_dict(run)


@feishu_knowledge.get("/sources/{source_id}/materials")
async def list_materials(
    source_id: str,
    processing_status: Annotated[str | None, Query()] = None,
    review_status: Annotated[str | None, Query()] = None,
    source_validity: Annotated[str | None, Query()] = None,
    item_type: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_db),
):
    if await FeishuKnowledgeRepository(db).get_source(source_id) is None:
        raise HTTPException(status_code=404, detail="Feishu source not found")
    statement = (
        select(FeishuMaterialVersion, FeishuSourceItem, FeishuSource)
        .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
        .join(FeishuSource, FeishuSource.source_id == FeishuSourceItem.source_id)
        .where(FeishuSourceItem.source_id == source_id)
        .order_by(FeishuMaterialVersion.created_at.desc())
    )
    if processing_status:
        statement = statement.where(FeishuMaterialVersion.processing_status == processing_status)
    if review_status:
        statement = statement.where(FeishuMaterialVersion.review_status == review_status)
    if source_validity:
        statement = statement.where(FeishuSourceItem.source_validity == source_validity)
    if item_type:
        statement = statement.where(FeishuSourceItem.item_type == item_type)
    rows = (await db.execute(statement)).all()
    return {"items": [_material_dict(version, item, source) for version, item, source in rows]}


@feishu_knowledge.get("/materials/{version_id}")
async def get_material(version_id: str, db: AsyncSession = Depends(get_db)):
    try:
        version, item, source = await FeishuReviewService(db)._get_material(version_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _material_dict(version, item, source)


@feishu_knowledge.get("/materials/{version_id}/events")
async def list_material_events(version_id: str, db: AsyncSession = Depends(get_db)):
    material_result = await db.execute(
        select(FeishuMaterialVersion.version_id).where(FeishuMaterialVersion.version_id == version_id)
    )
    if material_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Feishu material not found")
    result = await db.execute(
        select(FeishuProcessingEvent)
        .where(FeishuProcessingEvent.version_id == version_id)
        .order_by(FeishuProcessingEvent.created_at.asc())
    )
    return {"items": [_event_dict(event) for event in result.scalars()]}


async def _run_publish_worker(
    version_id: str,
    *,
    operator_id: str,
    publish_adapter: PublishAdapter | None = None,
) -> dict:
    adapter = publish_adapter or KnowledgePublishAdapter()
    try:
        async with pg_manager.get_async_session_context() as session:
            version, item, source = await FeishuReviewService(session).claim_publish(version_id)
            object_path = version.source_object_path or (version.processing_params or {}).get("object_path")
            if not object_path:
                raise RuntimeError("Feishu material has no archived source object")
            publish_args = {
                "kb_id": source.target_kb_id,
                "object_path": object_path,
                "source_url": item.source_url,
                "wiki_path": item.path_text,
                "version_id": version.version_id,
                "page_info": {"item_type": item.item_type, "title": item.title},
                "operator_id": operator_id,
                "file_id": version.yuxi_file_id,
            }
        result = await adapter.publish(**publish_args)
    except Exception as exc:
        async with pg_manager.get_async_session_context() as session:
            service = FeishuReviewService(session)
            try:
                await service.mark_publish_failed(version_id, message=str(exc))
            except (LookupError, ValueError):
                pass
        raise

    async with pg_manager.get_async_session_context() as session:
        material = await FeishuReviewService(session).mark_publish_succeeded(
            version_id,
            yuxi_file_id=result.file_id,
            chunk_count=result.chunk_count,
        )
    return {"version_id": material.version_id, "status": material.processing_status, "file_id": result.file_id}


async def _run_processing_worker(version_id: str, *, operator_id: str) -> dict:
    try:
        async with pg_manager.get_async_session_context() as session:
            version, item, source = await FeishuReviewService(session).claim_processing(version_id)
            object_path = version.source_object_path or (version.processing_params or {}).get("object_path")
            if not object_path:
                raise RuntimeError("Feishu material has no archived source object")
            params = {
                "content_type": "file",
                "feishu": {
                    "source_url": item.source_url,
                    "wiki_path": item.path_text,
                    "material_version": version.version_id,
                    "page_info": {"item_type": item.item_type, "title": item.title},
                },
            }
            kb_id = source.target_kb_id
            existing_file_id = version.yuxi_file_id
        if existing_file_id is None:
            file_meta = await knowledge_base.add_file_record(kb_id, object_path, params=params, operator_id=operator_id)
            existing_file_id = file_meta["file_id"]
        parsed = await knowledge_base.parse_file(kb_id, existing_file_id, operator_id=operator_id)
        if parsed.get("status") != "parsed":
            raise RuntimeError(f"Feishu material parsing did not complete: {parsed.get('status')}")
    except Exception as exc:
        async with pg_manager.get_async_session_context() as session:
            service = FeishuReviewService(session)
            try:
                await service.mark_processing_failed(version_id, message=str(exc))
            except (LookupError, ValueError):
                pass
        raise

    async with pg_manager.get_async_session_context() as session:
        material = await FeishuReviewService(session).mark_processing_succeeded(version_id, file_id=existing_file_id)
    return {"version_id": material.version_id, "status": material.processing_status, "file_id": existing_file_id}


async def _enqueue_publish(version_id: str, *, operator_id: str):
    async def run_publish(context: TaskContext):
        result = await _run_publish_worker(version_id, operator_id=operator_id)
        await context.set_result(result)
        return result

    return await tasker.enqueue(
        name=f"Publish Feishu material ({version_id})",
        task_type="feishu_publish",
        payload={"version_id": version_id},
        coroutine=run_publish,
    )


async def _enqueue_processing(version_id: str, *, operator_id: str):
    async def run_processing(context: TaskContext):
        result = await _run_processing_worker(version_id, operator_id=operator_id)
        await context.set_result(result)
        return result

    return await tasker.enqueue(
        name=f"Process Feishu material ({version_id})",
        task_type="feishu_process",
        payload={"version_id": version_id},
        coroutine=run_processing,
    )


def _raise_action_error(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@feishu_knowledge.post("/materials/{version_id}/approve", status_code=202)
async def approve_material(
    version_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        material = await FeishuReviewService(db).approve(version_id, operator_id=current_user.uid)
        task = await _enqueue_publish(version_id, operator_id=current_user.uid)
    except (LookupError, ValueError) as exc:
        _raise_action_error(exc)
    except Exception as exc:
        _raise_action_error(exc)
    return {"version_id": material.version_id, "status": material.processing_status, "task_id": task.id}


@feishu_knowledge.post("/materials/{version_id}/reject")
async def reject_material(
    version_id: str,
    payload: RejectRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        material = await FeishuReviewService(db).reject(
            version_id,
            operator_id=current_user.uid,
            reason=payload.reason,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"version_id": material.version_id, "status": material.review_status}


@feishu_knowledge.post("/materials/{version_id}/retry", status_code=202)
async def retry_material(
    version_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        material = await FeishuReviewService(db).retry(version_id, operator_id=current_user.uid)
        if material.processing_status == "publish_queued":
            task = await _enqueue_publish(version_id, operator_id=current_user.uid)
        else:
            task = await _enqueue_processing(version_id, operator_id=current_user.uid)
    except (LookupError, ValueError) as exc:
        _raise_action_error(exc)
    except Exception as exc:
        _raise_action_error(exc)
    return {"version_id": material.version_id, "status": material.processing_status, "task_id": task.id}


async def _apply_action(
    version_id: str,
    action: str,
    *,
    db: AsyncSession,
    operator_id: str,
    reason: str | None,
) -> dict:
    user = SimpleNamespace(uid=operator_id)
    if action == "approve":
        return await approve_material(version_id, db=db, current_user=user)
    if action == "reject":
        if not reason:
            raise HTTPException(status_code=422, detail="reason is required for reject")
        return await reject_material(version_id, RejectRequest(reason=reason), db=db, current_user=user)
    if action == "retry":
        return await retry_material(version_id, db=db, current_user=user)
    if action == "confirm_removal":
        return await confirm_removal(version_id, db=db, current_user=user)
    raise HTTPException(status_code=422, detail=f"Unsupported action: {action}")


@feishu_knowledge.post("/materials/batch-action", status_code=202)
async def batch_action(
    payload: BatchActionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    items = []
    for version_id in payload.version_ids:
        try:
            result = await _apply_action(
                version_id,
                payload.action,
                db=db,
                operator_id=current_user.uid,
                reason=payload.reason,
            )
            items.append({**result, "ok": True})
        except HTTPException as exc:
            items.append(
                {
                    "version_id": version_id,
                    "ok": False,
                    "status_code": exc.status_code,
                    "error": str(exc.detail),
                }
            )
        except Exception as exc:
            items.append({"version_id": version_id, "ok": False, "status_code": 500, "error": str(exc)})
    succeeded = sum(bool(item["ok"]) for item in items)
    return {"succeeded": succeeded, "failed": len(items) - succeeded, "items": items}


@feishu_knowledge.post("/materials/{version_id}/confirm-removal", status_code=202)
async def confirm_removal(
    version_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    try:
        material = await FeishuReviewService(db).confirm_removal(version_id, operator_id=current_user.uid)
    except (LookupError, ValueError) as exc:
        _raise_action_error(exc)
    except Exception as exc:
        _raise_action_error(exc)
    return {"version_id": material.version_id, "status": material.processing_status}


__all__ = ["feishu_knowledge"]
