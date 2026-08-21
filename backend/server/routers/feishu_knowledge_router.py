from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_admin_user, get_db
from yuxi.integrations.feishu import (
    FeishuClient,
    FeishuClientError,
    FeishuPermissionError,
    FeishuUserOAuthError,
    FeishuUserOAuthService,
    create_user_authorized_feishu_client,
)
from yuxi.integrations.feishu.schemas import FeishuNode
from yuxi.integrations.feishu.service import FeishuScanService
from yuxi.governance.comparator import CrossDocumentComparisonService
from yuxi.governance.content_quality import assess_content
from yuxi.knowledge.runtime import knowledge_base
from yuxi.repositories.feishu_knowledge_repository import (
    FeishuKnowledgeRepository as _BaseRepository,
    FeishuSourceSummary,
)
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
GLOBAL_FEISHU_CREDENTIAL_MARKER = "GLOBAL_FEISHU_APP"


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


@dataclass(frozen=True, slots=True)
class PublishSwitchResult:
    material: FeishuMaterialVersion
    replaced_file_id: str | None


def _feishu_source_path(*, wiki_path: str | None, title: str | None, item_type: str) -> str:
    raw_path = wiki_path or title or "Untitled"
    parts = []
    for raw_part in raw_path.split(" / "):
        part = "".join("_" if ord(char) < 32 else char for char in raw_part.strip())
        part = part.replace("\\", "_").replace("/", "_").replace(":", "_").lstrip(".")
        parts.append(part or "Untitled")
    if item_type == "page" and not parts[-1].lower().endswith(".md"):
        parts[-1] += ".md"
    display_path = "/".join(parts)
    if len(display_path) <= 512:
        return display_path
    filename = parts[-1]
    suffix = Path(filename).suffix
    if suffix and len(suffix) < 512:
        return f"{filename[: 512 - len(suffix)]}{suffix}"
    return filename[:512]


def _cancellation_message(exc: asyncio.CancelledError) -> str:
    return str(exc) or "Task was cancelled"


def _note_cancellation_recovery_failure(
    exc: asyncio.CancelledError,
    *,
    operation: str,
    recovery_exc: BaseException,
) -> None:
    exc.add_note(f"{operation} failed: {type(recovery_exc).__name__}: {recovery_exc}")


class PublishAdapter(Protocol):
    async def prepare_file(
        self,
        *,
        kb_id: str,
        object_path: str,
        source_url: str | None,
        wiki_path: str | None,
        version_id: str,
        content_hash: str,
        page_info: dict,
        operator_id: str,
    ) -> str: ...

    async def publish(
        self,
        *,
        kb_id: str,
        object_path: str,
        source_url: str | None,
        wiki_path: str | None,
        version_id: str,
        content_hash: str,
        page_info: dict,
        operator_id: str,
        file_id: str | None = None,
        parse_before_index: bool = False,
    ) -> PublishResult: ...


class KnowledgePublishAdapter:
    """Calls the existing upload/parse/index service layer without HTTP self-calls."""

    @staticmethod
    def _params(
        *,
        object_path: str,
        source_url: str | None,
        wiki_path: str | None,
        version_id: str,
        content_hash: str,
        page_info: dict,
    ) -> dict:
        citation = {
            "source_url": source_url,
            "wiki_path": wiki_path,
            "material_version": version_id,
            "page_info": page_info,
        }
        params = {
            "content_type": "file",
            "source_path": _feishu_source_path(
                wiki_path=wiki_path,
                title=page_info.get("title"),
                item_type=page_info.get("item_type", "page"),
            ),
            "content_hashes": {object_path: content_hash},
            "feishu": citation,
        }
        return params

    async def prepare_file(
        self,
        *,
        kb_id: str,
        object_path: str,
        source_url: str | None,
        wiki_path: str | None,
        version_id: str,
        content_hash: str,
        page_info: dict,
        operator_id: str,
    ) -> str:
        params = self._params(
            object_path=object_path,
            source_url=source_url,
            wiki_path=wiki_path,
            version_id=version_id,
            content_hash=content_hash,
            page_info=page_info,
        )
        file_meta = await knowledge_base.add_file_record(
            kb_id,
            object_path,
            params=params,
            operator_id=operator_id,
        )
        return file_meta["file_id"]

    async def publish(
        self,
        *,
        kb_id: str,
        object_path: str,
        source_url: str | None,
        wiki_path: str | None,
        version_id: str,
        content_hash: str,
        page_info: dict,
        operator_id: str,
        file_id: str | None = None,
        parse_before_index: bool = False,
    ) -> PublishResult:
        params = self._params(
            object_path=object_path,
            source_url=source_url,
            wiki_path=wiki_path,
            version_id=version_id,
            content_hash=content_hash,
            page_info=page_info,
        )
        if file_id is None:
            file_id = await self.prepare_file(
                kb_id=kb_id,
                object_path=object_path,
                source_url=source_url,
                wiki_path=wiki_path,
                version_id=version_id,
                content_hash=content_hash,
                page_info=page_info,
                operator_id=operator_id,
            )
            parse_before_index = True
        if parse_before_index:
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
        return await self.claim_queued_sync_run(
            run_id=self.queued_run_id,
            source_id=source_id,
            run_type=run_type,
            operator_id=operator_id,
        )


class FeishuReviewService:
    APPROVABLE_STATUSES = {"parsed", "awaiting_review"}
    RETRYABLE_STATUSES = {"parse_failed", "publish_failed"}

    def __init__(self, session: AsyncSession, *, removal_adapter: RemovalAdapter | None = None) -> None:
        self.session = session
        self.removal_adapter = removal_adapter or KnowledgeRemovalAdapter()

    async def approve(self, version_id: str, *, operator_id: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.review_status != "pending" or version.processing_status not in self.APPROVABLE_STATUSES:
                raise ValueError("Only pending parsed material can be approved")
            quality = (version.processing_params or {}).get("content_quality") or {}
            if not quality.get("checked"):
                raise ValueError("正文检查尚未完成，不能发布")
            if not quality.get("has_body"):
                raise ValueError("资料只有标题、没有可审核正文，不能发布")
            if await CrossDocumentComparisonService(self.session).has_open_conflict(version.version_id):
                raise ValueError("Material has unresolved cross-document conflicts")
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
            if version.processing_status not in self.RETRYABLE_STATUSES:
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
            processing_params = dict(version.processing_params or {})
            processing_params["comparison"] = {
                **(processing_params.get("comparison") or {}),
                "status": "queued",
                "candidate_count": 0,
                "relation_count": 0,
                "queued_at": utc_now().isoformat(),
                "error": None,
            }
            version.processing_params = processing_params
            await self.session.flush()
            return version

    async def remember_processing_file(self, version_id: str, *, file_id: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, _, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "processing":
                raise ValueError("Material is not processing")
            if version.yuxi_file_id not in {None, file_id}:
                raise ValueError("Material already references a different file")
            version.yuxi_file_id = file_id
            await self.session.flush()
            return version

    async def remember_publish_candidate(self, version_id: str, *, file_id: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, _, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "publishing":
                raise ValueError("Material is not publishing")
            version.yuxi_file_id = file_id
            version.processing_params = {
                **(version.processing_params or {}),
                "publish_candidate_needs_parse": True,
            }
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

    async def mark_processing_queue_failed(self, version_id: str, *, message: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "processing_queued":
                raise ValueError("Material is not queued for processing")
            version.processing_status = "parse_failed"
            version.error_message = message
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="processing_enqueue_failed",
                from_status="processing_queued",
                to_status="parse_failed",
                message=message,
            )
            await self.session.flush()
            return version

    async def mark_publish_succeeded(
        self, version_id: str, *, yuxi_file_id: str, chunk_count: int = 0
    ) -> PublishSwitchResult:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status not in {"publish_queued", "publishing"}:
                raise ValueError("Material is not queued for publishing")
            item = await self.session.scalar(
                select(FeishuSourceItem).where(FeishuSourceItem.item_id == version.item_id).with_for_update()
            )
            if item is None:
                raise LookupError(f"Feishu source item not found: {version.item_id}")
            old_active_id = item.active_version_id
            old_version = None
            if old_active_id and old_active_id != version.version_id:
                old_result = await self.session.execute(
                    select(FeishuMaterialVersion)
                    .where(FeishuMaterialVersion.version_id == old_active_id)
                    .with_for_update()
                )
                old_version = old_result.scalar_one_or_none()
            if old_version is not None and old_version.id > version.id:
                version.processing_status = "replaced"
                version.yuxi_file_id = yuxi_file_id
                version.chunk_count = chunk_count
                version.replaced_at = utc_now()
                version.error_code = None
                version.error_message = None
                processing_params = dict(version.processing_params or {})
                processing_params.pop("publish_candidate_needs_parse", None)
                version.processing_params = processing_params
                self._append_event(
                    source_id=item.source_id,
                    item_id=item.item_id,
                    version_id=version.version_id,
                    event_type="publish_obsolete",
                    from_status="publishing",
                    to_status="replaced",
                    payload_json={"active_version_id": old_active_id},
                )
                await self.session.flush()
                cleanup_file_id = None if old_version.yuxi_file_id == yuxi_file_id else yuxi_file_id
                return PublishSwitchResult(material=version, replaced_file_id=cleanup_file_id)
            cleanup_file_id = None
            if old_version is not None:
                if old_version.yuxi_file_id != yuxi_file_id:
                    cleanup_file_id = old_version.yuxi_file_id
                old_version.processing_status = "replaced"
                old_version.replaced_at = utc_now()
            item.active_version_id = version.version_id
            version.processing_status = "published"
            version.yuxi_file_id = yuxi_file_id
            version.chunk_count = chunk_count
            version.published_at = utc_now()
            version.error_code = None
            version.error_message = None
            processing_params = dict(version.processing_params or {})
            processing_params.pop("publish_candidate_needs_parse", None)
            version.processing_params = processing_params
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
            return PublishSwitchResult(material=version, replaced_file_id=cleanup_file_id)

    async def mark_replacement_cleanup_failed(self, version_id: str, *, message: str) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "published" or item.active_version_id != version.version_id:
                raise ValueError("Material is not the active published version")
            version.error_message = message
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="replacement_cleanup_failed",
                from_status="published",
                to_status="published",
                message=message,
            )
            await self.session.flush()
            return version

    async def mark_publish_obsolete_cleanup_failed(
        self,
        version_id: str,
        *,
        message: str,
    ) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "replaced":
                raise ValueError("Material is not an obsolete published version")
            version.error_message = message
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="publish_obsolete_cleanup_failed",
                from_status="replaced",
                to_status="replaced",
                message=message,
            )
            await self.session.flush()
            return version

    async def mark_publish_failed(
        self,
        version_id: str,
        *,
        message: str,
        yuxi_file_id: str | None = None,
        clear_file_id: bool = False,
        candidate_needs_parse: bool = False,
    ) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status not in {"publish_queued", "publishing"}:
                raise ValueError("Material is not queued for publishing")
            from_status = version.processing_status
            version.processing_status = "publish_failed"
            version.error_message = message
            if clear_file_id:
                version.yuxi_file_id = None
            elif yuxi_file_id is not None:
                version.yuxi_file_id = yuxi_file_id
            processing_params = dict(version.processing_params or {})
            if candidate_needs_parse:
                processing_params["publish_candidate_needs_parse"] = True
            else:
                processing_params.pop("publish_candidate_needs_parse", None)
            version.processing_params = processing_params
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="publish_failed",
                from_status=from_status,
                to_status="publish_failed",
                message=message,
            )
            await self.session.flush()
            return version

    async def confirm_removal(self, version_id: str, *, operator_id: str) -> FeishuMaterialVersion:
        version, item, source = await self._claim_removal(version_id, operator_id=operator_id)
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise
        external_file_already_missing = False
        try:
            await self.removal_adapter.remove(kb_id=source.target_kb_id, file_id=version.yuxi_file_id)
        except FileNotFoundError:
            external_file_already_missing = True
        except Exception as exc:
            await self._restore_removal(version_id, message=str(exc))
            raise
        try:
            return await self._finish_removal(
                version_id,
                operator_id=operator_id,
                external_file_already_missing=external_file_already_missing,
            )
        except Exception as exc:
            await self.session.rollback()
            try:
                async with pg_manager.get_async_session_context() as recovery_session:
                    await FeishuReviewService(recovery_session)._restore_removal(
                        version_id,
                        message=str(exc),
                    )
                    await recovery_session.commit()
            except Exception as recovery_exc:
                raise RuntimeError(f"{exc}; removal recovery failed: {recovery_exc}") from exc
            raise

    async def _claim_removal(
        self, version_id: str, *, operator_id: str
    ) -> tuple[FeishuMaterialVersion, FeishuSourceItem, FeishuSource]:
        async with self._transaction():
            version, item, source = await self._get_material(version_id, lock=True)
            if item.source_validity != "invalid":
                raise ValueError("Material source must be invalid before removal")
            if (
                item.active_version_id != version.version_id
                or version.processing_status
                not in {
                    "published",
                    "removal_failed",
                }
                or not version.yuxi_file_id
            ):
                raise ValueError("Material is not the active published version")
            from_status = version.processing_status
            version.processing_status = "removal_pending"
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="removal_started",
                from_status=from_status,
                to_status="removal_pending",
                operator_id=operator_id,
            )
            await self.session.flush()
            return version, item, source

    async def _finish_removal(
        self,
        version_id: str,
        *,
        operator_id: str,
        external_file_already_missing: bool = False,
    ) -> FeishuMaterialVersion:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "removal_pending" or item.active_version_id != version.version_id:
                raise ValueError("Material removal is no longer pending")
            item.active_version_id = None
            version.processing_status = "removed"
            version.yuxi_file_id = None
            version.error_message = None
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="removal_confirmed",
                from_status="removal_pending",
                to_status="removed",
                operator_id=operator_id,
                payload_json={"external_file_already_missing": external_file_already_missing},
            )
            await self.session.flush()
            return version

    async def _restore_removal(self, version_id: str, *, message: str) -> None:
        async with self._transaction():
            version, item, _ = await self._get_material(version_id, lock=True)
            if version.processing_status != "removal_pending":
                return
            version.processing_status = "removal_failed"
            version.error_message = message
            self._append_event(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="removal_failed",
                from_status="removal_pending",
                to_status="removal_failed",
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

    @asynccontextmanager
    async def _transaction(self):
        transaction = self.session.begin_nested() if self.session.in_transaction() else self.session.begin()
        async with transaction:
            yield


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    wiki_root_token: str = Field(min_length=1, max_length=255)
    wiki_root_url: str | None = Field(default=None, max_length=1024)
    scan_scope: Literal["root", "space"] = "root"
    target_kb_id: str = Field(min_length=1, max_length=80)
    enabled: bool = True

    @field_validator("name", "wiki_root_token", "target_kb_id")
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


def _source_dict(source: FeishuSource, summary: FeishuSourceSummary | None = None) -> dict:
    data = {
        "source_id": source.source_id,
        "name": source.name,
        "wiki_root_token": source.wiki_root_token,
        "wiki_root_url": source.wiki_root_url,
        "scan_scope": getattr(source, "scan_scope", "root") or "root",
        "target_kb_id": source.target_kb_id,
        "enabled": source.enabled,
        "created_at": _iso(getattr(source, "created_at", None)),
        "updated_at": _iso(getattr(source, "updated_at", None)),
    }
    if summary is not None:
        data.update(
            {
                "last_full_sync_at": _iso(summary.last_full_sync_at),
                "last_incremental_sync_at": _iso(summary.last_incremental_sync_at),
                "total_count": summary.total_count,
                "awaiting_review_count": summary.awaiting_review_count,
                "failed_count": summary.failed_count,
                "source_invalid_count": summary.source_invalid_count,
            }
        )
    return data


def _run_dict(run: FeishuSyncRun) -> dict:
    return {
        "run_id": run.run_id,
        "source_id": run.source_id,
        "run_type": run.run_type,
        "status": run.status,
        "operator_id": run.operator_id,
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
    content_quality = (version.processing_params or {}).get("content_quality") or {}
    is_directory = item.item_type == "directory" or content_quality.get("classification") == "directory"
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
        "sync_run_id": version.sync_run_id,
        "source_updated_at": _iso(item.source_updated_at),
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
        "content_quality": content_quality,
        "is_directory": is_directory,
        "content_missing": bool(
            not is_directory and content_quality.get("checked") and not content_quality.get("has_body")
        ),
        "content_check_pending": bool(not is_directory and not content_quality.get("checked")),
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
    repository = FeishuKnowledgeRepository(db)
    items = []
    for source in result.scalars():
        summary = await repository.get_source_summary(source.source_id)
        items.append(_source_dict(source, summary))
    return {"items": items}


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
        scan_scope=payload.scan_scope,
        target_kb_id=payload.target_kb_id.strip(),
        credential_env_name=GLOBAL_FEISHU_CREDENTIAL_MARKER,
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
        client = create_user_authorized_feishu_client(source_id)
    except (FeishuClientError, FeishuUserOAuthError) as exc:
        raise HTTPException(status_code=getattr(exc, "status_code", None) or 422, detail=str(exc)) from exc
    try:
        node = await client.get_node(source.wiki_root_token)
    except FeishuUserOAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except FeishuClientError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await client.aclose()
    return {"status": "ok", "source_id": source_id, "root_title": node.title}


SPACE_PERMISSION_DETAIL = {
    "code": "FEISHU_SPACE_PERMISSION_DENIED",
    "message": "当前应用没有读取整个知识空间的权限，请在飞书开放平台开通后重试",
}


def _tree_node_dict(source: FeishuSource, node: FeishuNode, children: list[dict]) -> dict:
    return {
        "title": node.title or node.node_token,
        "node_token": node.node_token,
        "obj_token": node.obj_token,
        "obj_type": node.obj_type,
        "parent_node_token": node.parent_node_token,
        "has_child": bool(node.has_child),
        "source_updated_at": node.source_updated_at,
        "url": FeishuScanService._page_url(source.wiki_root_url, node.node_token),
        "children": children,
    }


async def _read_node_tree(
    *,
    client: FeishuClient,
    source: FeishuSource,
    node: FeishuNode,
    visited: set[str],
) -> dict | None:
    if node.node_token in visited:
        return None
    visited.add(node.node_token)
    children: list[dict] = []
    if node.has_child:
        for child in await client.list_nodes(node.space_id, node.node_token):
            child_tree = await _read_node_tree(client=client, source=source, node=child, visited=visited)
            if child_tree is not None:
                children.append(child_tree)
    return _tree_node_dict(source, node, children)


@feishu_knowledge.get("/sources/{source_id}/tree")
async def get_source_tree(source_id: str, db: AsyncSession = Depends(get_db)):
    source = await FeishuKnowledgeRepository(db).get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Feishu source not found")
    client = None
    try:
        client = create_user_authorized_feishu_client(source_id)
        root = await client.get_node(source.wiki_root_token)
        scan_scope = getattr(source, "scan_scope", "root") or "root"
        if scan_scope == "space":
            top_nodes = await client.list_nodes(root.space_id)
            if not top_nodes:
                top_nodes = [root]
        else:
            top_nodes = [root]
        nodes = []
        visited: set[str] = set()
        for node in top_nodes:
            tree = await _read_node_tree(client=client, source=source, node=node, visited=visited)
            if tree is not None:
                nodes.append(tree)
        return {"scope": scan_scope, "space_id": root.space_id, "nodes": nodes}
    except FeishuUserOAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except FeishuPermissionError as exc:
        raise HTTPException(status_code=424, detail=SPACE_PERMISSION_DETAIL) from exc
    except FeishuClientError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if client is not None:
            await client.aclose()


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
    oauth_status = await FeishuUserOAuthService(db=db).get_authorization_status(source_id)
    if not oauth_status["authorized"]:
        raise HTTPException(
            status_code=424,
            detail={
                "code": "FEISHU_USER_AUTHORIZATION_REQUIRED",
                "message": "请先完成飞书用户授权，再启动知识扫描",
            },
        )
    run = await repository.queue_sync_run(
        source_id=source_id,
        run_type=payload.mode,
        operator_id=current_user.uid,
    )
    await db.commit()

    async def run_scan(context: TaskContext):
        queued_version_ids = []
        enqueued_version_ids = set()
        try:
            async with pg_manager.get_async_session_context() as session:
                worker_repository = FeishuKnowledgeRepository(session, queued_run_id=run.run_id)
                client = None
                try:
                    try:
                        if hasattr(context, "raise_if_cancelled"):
                            await context.raise_if_cancelled()
                        worker_source = await worker_repository.get_source(source_id)
                        if worker_source is None:
                            raise LookupError(f"Feishu source not found: {source_id}")
                        client = create_user_authorized_feishu_client(source_id)
                        result = await FeishuScanService(
                            repository=worker_repository,
                            client=client,
                            archive_adapter=MinioFeishuArchiveAdapter(),
                        ).scan(
                            source_id=source_id,
                            mode=payload.mode,
                            operator_id=current_user.uid,
                        )
                    except Exception as exc:
                        await worker_repository.fail_sync_run(
                            run_id=run.run_id,
                            source_id=source_id,
                            error_summary=f"{type(exc).__name__}: {exc}",
                            operator_id=current_user.uid,
                        )
                        await session.commit()
                        raise
                    try:
                        await session.commit()
                    except Exception as commit_exc:
                        await session.rollback()
                        error_summary = result.error_summary or f"{type(commit_exc).__name__}: {commit_exc}"
                        async with pg_manager.get_async_session_context() as recovery_session:
                            await FeishuKnowledgeRepository(recovery_session).fail_sync_run(
                                run_id=run.run_id,
                                source_id=source_id,
                                error_summary=error_summary,
                                operator_id=current_user.uid,
                            )
                            await recovery_session.commit()
                        raise
                    if result.status != "succeeded":
                        await context.set_result({"run_id": result.run_id, "status": result.status})
                        raise RuntimeError(result.error_summary or "Feishu scan failed")
                    if hasattr(context, "raise_if_cancelled"):
                        await context.raise_if_cancelled()
                    claimed_version_ids = await worker_repository.queue_archived_versions_for_processing(
                        source_id=source_id,
                        operator_id=current_user.uid,
                    )
                    await session.commit()
                    queued_version_ids = claimed_version_ids
                    await context.set_result({"run_id": result.run_id, "status": result.status})
                    enqueue_errors = []
                    for version_id in queued_version_ids:
                        try:
                            await _enqueue_processing(version_id, operator_id=current_user.uid)
                            enqueued_version_ids.add(version_id)
                        except Exception as exc:
                            enqueue_errors.append(exc)
                            await FeishuReviewService(session).mark_processing_queue_failed(
                                version_id,
                                message=str(exc),
                            )
                            await session.commit()
                    if enqueue_errors:
                        raise enqueue_errors[0]
                    return {"run_id": result.run_id, "status": result.status}
                finally:
                    if client is not None:
                        await client.aclose()
        except asyncio.CancelledError as exc:
            remaining_version_ids = []
            try:
                async with pg_manager.get_async_session_context() as recovery_session:
                    recovery_repository = FeishuKnowledgeRepository(recovery_session)
                    status = await recovery_repository.get_sync_run_status(run.run_id)
                    if status == "succeeded":
                        if not queued_version_ids:
                            claimed_version_ids = await recovery_repository.queue_archived_versions_for_processing(
                                source_id=source_id,
                                operator_id=current_user.uid,
                            )
                        else:
                            claimed_version_ids = queued_version_ids
                    else:
                        await recovery_repository.fail_sync_run(
                            run_id=run.run_id,
                            source_id=source_id,
                            error_summary=f"CancelledError: {_cancellation_message(exc)}",
                            operator_id=current_user.uid,
                        )
                    await recovery_session.commit()
                    if status == "succeeded":
                        queued_version_ids = claimed_version_ids
                        remaining_version_ids = [
                            version_id for version_id in queued_version_ids if version_id not in enqueued_version_ids
                        ]
            except (Exception, asyncio.CancelledError) as recovery_exc:
                _note_cancellation_recovery_failure(
                    exc,
                    operation="scan cancellation recovery",
                    recovery_exc=recovery_exc,
                )
            for version_id in remaining_version_ids:
                try:
                    await _enqueue_processing(version_id, operator_id=current_user.uid)
                    enqueued_version_ids.add(version_id)
                except (Exception, asyncio.CancelledError) as enqueue_exc:
                    if isinstance(enqueue_exc, asyncio.CancelledError):
                        _note_cancellation_recovery_failure(
                            exc,
                            operation=f"processing enqueue recovery for {version_id}",
                            recovery_exc=enqueue_exc,
                        )
                    try:
                        async with pg_manager.get_async_session_context() as recovery_session:
                            await FeishuReviewService(recovery_session).mark_processing_queue_failed(
                                version_id,
                                message=_cancellation_message(enqueue_exc)
                                if isinstance(enqueue_exc, asyncio.CancelledError)
                                else str(enqueue_exc),
                            )
                            await recovery_session.commit()
                    except (Exception, asyncio.CancelledError) as recovery_exc:
                        _note_cancellation_recovery_failure(
                            exc,
                            operation=f"processing enqueue failure recovery for {version_id}",
                            recovery_exc=recovery_exc,
                        )
            raise

    try:
        task, created = await tasker.enqueue_unique_by_payload(
            name=f"Feishu scan ({source.name})",
            task_type="feishu_scan",
            payload={"source_id": source_id, "run_id": run.run_id, "mode": payload.mode},
            payload_match={"source_id": source_id},
            statuses={"pending", "running"},
            coroutine=run_scan,
        )
        if not created:
            existing_run_id = task.payload.get("run_id")
            existing_run_status = await repository.get_sync_run_status(existing_run_id) if existing_run_id else None
            if existing_run_status not in {"queued", "running"}:
                task, created = await tasker.enqueue_unique_by_payload(
                    name=f"Feishu scan ({source.name})",
                    task_type="feishu_scan",
                    payload={"source_id": source_id, "run_id": run.run_id, "mode": payload.mode},
                    payload_match={"source_id": source_id},
                    statuses={"pending"},
                    coroutine=run_scan,
                )
    except Exception:
        await repository.cancel_queued_run(run.run_id)
        await db.commit()
        raise
    run_id = run.run_id
    if not created:
        await repository.cancel_queued_run(run.run_id)
        await db.commit()
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
    directory: Annotated[str | None, Query()] = None,
    updated_from: Annotated[datetime | None, Query()] = None,
    updated_to: Annotated[datetime | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
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
    if directory:
        statement = statement.where(
            or_(
                FeishuSourceItem.path_text == directory,
                FeishuSourceItem.path_text.startswith(f"{directory} /", autoescape=True),
            )
        )
    if updated_from and updated_to and updated_from > updated_to:
        raise HTTPException(status_code=422, detail="updated_from must not be later than updated_to")
    if updated_from:
        statement = statement.where(FeishuSourceItem.source_updated_at >= updated_from)
    if updated_to:
        statement = statement.where(FeishuSourceItem.source_updated_at <= updated_to)
    if run_id:
        statement = statement.where(FeishuMaterialVersion.sync_run_id == run_id)
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
    context: TaskContext | None = None,
) -> dict:
    adapter = publish_adapter or KnowledgePublishAdapter()
    candidate_file_id = None
    publish_args = None
    try:
        async with pg_manager.get_async_session_context() as session:
            version, item, source = await FeishuReviewService(session).claim_publish(version_id)
            object_path = version.source_object_path or (version.processing_params or {}).get("object_path")
            if not object_path:
                raise RuntimeError("Feishu material has no archived source object")
            publish_file_id = version.yuxi_file_id
            shared_active_file = False
            if item.active_version_id and item.active_version_id != version.version_id:
                active_file_id = await session.scalar(
                    select(FeishuMaterialVersion.yuxi_file_id).where(
                        FeishuMaterialVersion.version_id == item.active_version_id
                    )
                )
                if active_file_id and active_file_id == publish_file_id:
                    publish_file_id = None
                    shared_active_file = True
            candidate_needs_parse = bool((version.processing_params or {}).get("publish_candidate_needs_parse"))
            if candidate_needs_parse:
                candidate_file_id = publish_file_id
            publish_args = {
                "kb_id": source.target_kb_id,
                "object_path": object_path,
                "source_url": item.source_url,
                "wiki_path": item.path_text,
                "version_id": version.version_id,
                "content_hash": version.content_hash,
                "page_info": {"item_type": item.item_type, "title": item.title},
                "operator_id": operator_id,
                "file_id": publish_file_id,
                "parse_before_index": candidate_needs_parse,
            }
        if context is not None:
            await context.raise_if_cancelled()
        if shared_active_file:
            candidate_file_id = await adapter.prepare_file(
                **{key: value for key, value in publish_args.items() if key not in {"file_id", "parse_before_index"}}
            )
            async with pg_manager.get_async_session_context() as session:
                await FeishuReviewService(session).remember_publish_candidate(
                    version_id,
                    file_id=candidate_file_id,
                )
                await session.commit()
            publish_args["file_id"] = candidate_file_id
            publish_args["parse_before_index"] = True
        result = await adapter.publish(**publish_args)
    except asyncio.CancelledError as exc:
        recovery_message = _cancellation_message(exc)
        retained_file_id = None
        cleanup_succeeded = False
        if candidate_file_id is not None and publish_args is not None:
            try:
                await knowledge_base.delete_file(publish_args["kb_id"], candidate_file_id)
                cleanup_succeeded = True
            except (Exception, asyncio.CancelledError) as cleanup_exc:
                cleanup_message = (
                    _cancellation_message(cleanup_exc)
                    if isinstance(cleanup_exc, asyncio.CancelledError)
                    else str(cleanup_exc)
                )
                recovery_message = f"{recovery_message}; new file cleanup failed: {cleanup_message}"
                retained_file_id = candidate_file_id
        try:
            async with pg_manager.get_async_session_context() as session:
                service = FeishuReviewService(session)
                if candidate_file_id is None:
                    await service.mark_publish_failed(version_id, message=recovery_message)
                else:
                    await service.mark_publish_failed(
                        version_id,
                        message=recovery_message,
                        yuxi_file_id=retained_file_id,
                        clear_file_id=cleanup_succeeded,
                        candidate_needs_parse=retained_file_id is not None,
                    )
                await session.commit()
        except (Exception, asyncio.CancelledError) as recovery_exc:
            _note_cancellation_recovery_failure(
                exc,
                operation="publish cancellation recovery",
                recovery_exc=recovery_exc,
            )
        raise
    except Exception as exc:
        recovery_message = str(exc)
        retained_file_id = None
        cleanup_succeeded = False
        cleanup_cancellation = None
        if candidate_file_id is not None and publish_args is not None:
            try:
                await knowledge_base.delete_file(publish_args["kb_id"], candidate_file_id)
                cleanup_succeeded = True
            except asyncio.CancelledError as cleanup_exc:
                cleanup_cancellation = cleanup_exc
                recovery_message = (
                    f"{recovery_message}; new file cleanup cancelled: {_cancellation_message(cleanup_exc)}"
                )
                retained_file_id = candidate_file_id
            except Exception as cleanup_exc:
                recovery_message = f"{recovery_message}; new file cleanup failed: {cleanup_exc}"
                retained_file_id = candidate_file_id
        async with pg_manager.get_async_session_context() as session:
            service = FeishuReviewService(session)
            try:
                if candidate_file_id is None:
                    await service.mark_publish_failed(version_id, message=recovery_message)
                else:
                    await service.mark_publish_failed(
                        version_id,
                        message=recovery_message,
                        yuxi_file_id=retained_file_id,
                        clear_file_id=cleanup_succeeded,
                        candidate_needs_parse=retained_file_id is not None,
                    )
            except (LookupError, ValueError):
                pass
        if cleanup_cancellation is not None:
            raise cleanup_cancellation
        if retained_file_id is not None:
            raise RuntimeError(recovery_message) from exc
        raise

    try:
        async with pg_manager.get_async_session_context() as session:
            switch = await FeishuReviewService(session).mark_publish_succeeded(
                version_id,
                yuxi_file_id=result.file_id,
                chunk_count=result.chunk_count,
            )
            await session.commit()
    except (Exception, asyncio.CancelledError) as exc:
        if isinstance(exc, asyncio.CancelledError):
            try:
                async with pg_manager.get_async_session_context() as status_session:
                    processing_status = await status_session.scalar(
                        select(FeishuMaterialVersion.processing_status).where(
                            FeishuMaterialVersion.version_id == version_id
                        )
                    )
            except (Exception, asyncio.CancelledError) as recovery_exc:
                _note_cancellation_recovery_failure(
                    exc,
                    operation="publish activation status recovery",
                    recovery_exc=recovery_exc,
                )
                raise exc
            if processing_status in {"published", "replaced"}:
                raise
        recovery_message = str(exc)
        retained_file_id = None
        cleanup_cancellation = None
        created_file_id = candidate_file_id
        if created_file_id is None and result.file_id != publish_args["file_id"]:
            created_file_id = result.file_id
        if created_file_id is not None:
            try:
                await knowledge_base.delete_file(publish_args["kb_id"], created_file_id)
            except asyncio.CancelledError as cleanup_exc:
                cleanup_cancellation = cleanup_exc
                recovery_message = f"{exc}; new file cleanup cancelled: {_cancellation_message(cleanup_exc)}"
                retained_file_id = created_file_id
            except Exception as cleanup_exc:
                recovery_message = f"{exc}; new file cleanup failed: {cleanup_exc}"
                retained_file_id = created_file_id
        try:
            async with pg_manager.get_async_session_context() as recovery_session:
                await FeishuReviewService(recovery_session).mark_publish_failed(
                    version_id,
                    message=recovery_message,
                    yuxi_file_id=retained_file_id,
                    clear_file_id=created_file_id is not None and retained_file_id is None,
                    candidate_needs_parse=False,
                )
                await recovery_session.commit()
        except (Exception, asyncio.CancelledError) as recovery_exc:
            cancellation = exc if isinstance(exc, asyncio.CancelledError) else cleanup_cancellation
            if cancellation is not None:
                _note_cancellation_recovery_failure(
                    cancellation,
                    operation=f"{recovery_message}; publish recovery",
                    recovery_exc=recovery_exc,
                )
                raise cancellation
            raise RuntimeError(f"{recovery_message}; publish recovery failed: {recovery_exc}") from exc
        if isinstance(exc, asyncio.CancelledError):
            raise
        if cleanup_cancellation is not None:
            raise cleanup_cancellation
        if retained_file_id is not None:
            raise RuntimeError(recovery_message) from exc
        raise
    material = switch.material
    if switch.replaced_file_id:
        try:
            await knowledge_base.delete_file(publish_args["kb_id"], switch.replaced_file_id)
        except (Exception, asyncio.CancelledError) as exc:
            message = _cancellation_message(exc) if isinstance(exc, asyncio.CancelledError) else str(exc)
            try:
                async with pg_manager.get_async_session_context() as session:
                    service = FeishuReviewService(session)
                    if material.processing_status == "replaced":
                        await service.mark_publish_obsolete_cleanup_failed(
                            version_id,
                            message=message,
                        )
                    else:
                        await service.mark_replacement_cleanup_failed(
                            version_id,
                            message=message,
                        )
                    await session.commit()
            except (Exception, asyncio.CancelledError) as recovery_exc:
                if isinstance(exc, asyncio.CancelledError):
                    _note_cancellation_recovery_failure(
                        exc,
                        operation="published file cleanup recovery",
                        recovery_exc=recovery_exc,
                    )
                    raise exc
                raise
            if isinstance(exc, asyncio.CancelledError):
                raise
    return {"version_id": material.version_id, "status": material.processing_status, "file_id": result.file_id}


async def _run_processing_worker(
    version_id: str,
    *,
    operator_id: str,
    context: TaskContext | None = None,
) -> dict:
    try:
        async with pg_manager.get_async_session_context() as session:
            version, item, source = await FeishuReviewService(session).claim_processing(version_id)
            object_path = version.source_object_path or (version.processing_params or {}).get("object_path")
            if not object_path:
                raise RuntimeError("Feishu material has no archived source object")
            params = {
                "content_type": "file",
                "source_path": _feishu_source_path(
                    wiki_path=item.path_text,
                    title=item.title,
                    item_type=item.item_type,
                ),
                "content_hashes": {object_path: version.content_hash},
                "feishu": {
                    "source_url": item.source_url,
                    "wiki_path": item.path_text,
                    "material_version": version.version_id,
                    "page_info": {"item_type": item.item_type, "title": item.title},
                },
            }
            kb_id = source.target_kb_id
            existing_file_id = version.yuxi_file_id
        if context is not None:
            await context.raise_if_cancelled()
        if existing_file_id is None:
            file_meta = await knowledge_base.add_file_record(kb_id, object_path, params=params, operator_id=operator_id)
            existing_file_id = file_meta["file_id"]
            async with pg_manager.get_async_session_context() as session:
                await FeishuReviewService(session).remember_processing_file(
                    version_id,
                    file_id=existing_file_id,
                )
        parsed = await knowledge_base.parse_file(kb_id, existing_file_id, operator_id=operator_id)
        if parsed.get("status") != "parsed":
            raise RuntimeError(f"Feishu material parsing did not complete: {parsed.get('status')}")
        try:
            content_info = await knowledge_base.get_file_content(kb_id, existing_file_id)
            quality = assess_content(
                content=content_info.get("content") if isinstance(content_info, dict) else None,
                title=item.title,
            )
        except Exception as exc:
            quality = {
                "checked": False,
                "has_body": False,
                "body_length": 0,
                "reason": f"无法读取解析正文：{exc}",
            }
        async with pg_manager.get_async_session_context() as quality_session:
            quality_version = await quality_session.scalar(
                select(FeishuMaterialVersion)
                .where(FeishuMaterialVersion.version_id == version_id)
                .with_for_update()
            )
            if quality_version is not None:
                quality_params = dict(quality_version.processing_params or {})
                quality_params["content_quality"] = quality
                quality_version.processing_params = quality_params
                await quality_session.flush()
    except asyncio.CancelledError as exc:
        try:
            async with pg_manager.get_async_session_context() as session:
                try:
                    await FeishuReviewService(session).mark_processing_failed(
                        version_id,
                        message=_cancellation_message(exc),
                    )
                    await session.commit()
                except (LookupError, ValueError):
                    pass
        except (Exception, asyncio.CancelledError) as recovery_exc:
            _note_cancellation_recovery_failure(
                exc,
                operation="processing cancellation recovery",
                recovery_exc=recovery_exc,
            )
        raise
    except Exception as exc:
        async with pg_manager.get_async_session_context() as session:
            service = FeishuReviewService(session)
            try:
                await service.mark_processing_failed(version_id, message=str(exc))
            except (LookupError, ValueError):
                pass
        raise

    try:
        async with pg_manager.get_async_session_context() as session:
            material = await FeishuReviewService(session).mark_processing_succeeded(
                version_id,
                file_id=existing_file_id,
            )
    except asyncio.CancelledError as exc:
        try:
            async with pg_manager.get_async_session_context() as session:
                try:
                    await FeishuReviewService(session).mark_processing_failed(
                        version_id,
                        message=_cancellation_message(exc),
                    )
                    await session.commit()
                except (LookupError, ValueError):
                    pass
        except (Exception, asyncio.CancelledError) as recovery_exc:
            _note_cancellation_recovery_failure(
                exc,
                operation="processing finalization cancellation recovery",
                recovery_exc=recovery_exc,
            )
        raise
    try:
        await _enqueue_comparison(material.version_id, operator_id=operator_id)
    except Exception as exc:
        # 解析结果仍然可供人工审核，比较失败只记录状态，不回滚解析结果。
        async with pg_manager.get_async_session_context() as session:
            await _update_comparison_state(session, material.version_id, "failed", error=str(exc))
    return {"version_id": material.version_id, "status": material.processing_status, "file_id": existing_file_id}


async def _update_comparison_state(
    session: AsyncSession,
    version_id: str,
    status: str,
    *,
    relation_count: int | None = None,
    candidate_count: int | None = None,
    error: str | None = None,
) -> None:
    version = await session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == version_id).with_for_update()
    )
    if version is None:
        raise LookupError(f"Material version not found: {version_id}")
    params = dict(version.processing_params or {})
    comparison = dict(params.get("comparison") or {})
    comparison["status"] = status
    if relation_count is not None:
        comparison["relation_count"] = relation_count
    if candidate_count is not None:
        comparison["candidate_count"] = candidate_count
    if error is not None:
        comparison["error"] = error
    elif status != "failed":
        comparison["error"] = None
    comparison["updated_at"] = utc_now().isoformat()
    params["comparison"] = comparison
    version.processing_params = params
    await session.flush()


async def _run_comparison_worker(
    version_id: str,
    *,
    context: TaskContext | None = None,
) -> dict:
    async with pg_manager.get_async_session_context() as session:
        await _update_comparison_state(session, version_id, "running")
    try:
        if context is not None:
            await context.raise_if_cancelled()
        async with pg_manager.get_async_session_context() as session:
            relations = await CrossDocumentComparisonService(session).compare_version(version_id)
            relation_count = len(relations)
        async with pg_manager.get_async_session_context() as session:
            await _update_comparison_state(session, version_id, "completed", relation_count=relation_count)
        return {"version_id": version_id, "status": "completed", "relation_count": relation_count}
    except asyncio.CancelledError:
        async with pg_manager.get_async_session_context() as session:
            await _update_comparison_state(session, version_id, "queued", error="任务已取消")
        raise
    except Exception as exc:
        async with pg_manager.get_async_session_context() as session:
            await _update_comparison_state(session, version_id, "failed", error=str(exc))
        raise


async def _enqueue_comparison(version_id: str, *, operator_id: str | None = None):
    async def run_comparison(context: TaskContext):
        result = await _run_comparison_worker(version_id, context=context)
        await context.set_result(result)
        return result

    task, _ = await tasker.enqueue_unique_by_payload(
        name=f"Compare Feishu material ({version_id})",
        task_type="feishu_compare",
        payload={"version_id": version_id, "operator_id": operator_id},
        payload_match={"version_id": version_id},
        statuses={"pending", "running"},
        coroutine=run_comparison,
    )
    return task


async def _run_comparison_backfill_worker(
    source_id: str,
    *,
    context: TaskContext,
) -> dict:
    async with pg_manager.get_async_session_context() as session:
        statement = (
            select(FeishuMaterialVersion.version_id)
            .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
            .where(
                FeishuSourceItem.source_id == source_id,
                FeishuMaterialVersion.processing_status.in_(CrossDocumentComparisonService.CANDIDATE_STATUSES),
            )
            .order_by(FeishuMaterialVersion.created_at.asc())
        )
        version_ids = list(await session.scalars(statement))
    total = len(version_ids)
    compared = 0
    relations = 0
    for version_id in version_ids:
        await context.raise_if_cancelled()
        async with pg_manager.get_async_session_context() as session:
            await _update_comparison_state(session, version_id, "running")
            try:
                found = await CrossDocumentComparisonService(session).compare_version(version_id)
                found_count = len(found)
                await _update_comparison_state(session, version_id, "completed", relation_count=found_count)
            except asyncio.CancelledError:
                await _update_comparison_state(session, version_id, "queued", error="任务已取消")
                raise
            except Exception as exc:
                await _update_comparison_state(session, version_id, "failed", error=str(exc))
                found_count = 0
        compared += 1
        relations += found_count
        await context.set_progress(
            (compared / total * 100) if total else 100,
            f"跨文档检查 {compared}/{total}，发现 {relations} 条关系",
        )
    return {"source_id": source_id, "total": total, "compared": compared, "relations": relations}


async def _enqueue_comparison_backfill(source_id: str, *, operator_id: str):
    async def run_backfill(context: TaskContext):
        result = await _run_comparison_backfill_worker(source_id, context=context)
        await context.set_result(result)
        return result

    task, created = await tasker.enqueue_unique_by_payload(
        name=f"Backfill Feishu comparisons ({source_id})",
        task_type="feishu_compare_backfill",
        payload={"source_id": source_id, "operator_id": operator_id},
        payload_match={"source_id": source_id},
        statuses={"pending", "running"},
        coroutine=run_backfill,
    )
    return task, created


async def _enqueue_publish(version_id: str, *, operator_id: str):
    async def run_publish(context: TaskContext):
        result = await _run_publish_worker(version_id, operator_id=operator_id, context=context)
        await context.set_result(result)
        return result

    task, _ = await tasker.enqueue_unique_by_payload(
        name=f"Publish Feishu material ({version_id})",
        task_type="feishu_publish",
        payload={"version_id": version_id},
        payload_match={"version_id": version_id},
        statuses={"pending"},
        coroutine=run_publish,
    )
    return task


async def _enqueue_processing(version_id: str, *, operator_id: str):
    async def run_processing(context: TaskContext):
        result = await _run_processing_worker(version_id, operator_id=operator_id, context=context)
        await context.set_result(result)
        return result

    task, _ = await tasker.enqueue_unique_by_payload(
        name=f"Process Feishu material ({version_id})",
        task_type="feishu_process",
        payload={"version_id": version_id},
        payload_match={"version_id": version_id},
        statuses={"pending"},
        coroutine=run_processing,
    )
    return task


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
        await db.commit()
        try:
            task = await _enqueue_publish(version_id, operator_id=current_user.uid)
        except Exception as exc:
            await FeishuReviewService(db).mark_publish_failed(version_id, message=str(exc))
            await db.commit()
            raise
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
        await db.commit()
        if material.processing_status == "publish_queued":
            try:
                task = await _enqueue_publish(version_id, operator_id=current_user.uid)
            except Exception as exc:
                await FeishuReviewService(db).mark_publish_failed(version_id, message=str(exc))
                await db.commit()
                raise
        else:
            try:
                task = await _enqueue_processing(version_id, operator_id=current_user.uid)
            except Exception as exc:
                await FeishuReviewService(db).mark_processing_queue_failed(version_id, message=str(exc))
                await db.commit()
                raise
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
