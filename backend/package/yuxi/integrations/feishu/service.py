from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from yuxi.integrations.feishu.client import FeishuClient
from yuxi.integrations.feishu.schemas import FeishuAttachment, FeishuNode
from yuxi.repositories.feishu_knowledge_repository import FeishuKnowledgeRepository
from yuxi.storage.postgres.models_knowledge import FeishuMaterialVersion, FeishuSource
from yuxi.utils.datetime_utils import coerce_any_to_utc_datetime, utc_now

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
}
AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav", ".wma"}
VIDEO_EXTENSIONS = {".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"}


@dataclass(frozen=True, slots=True)
class FeishuScanResult:
    run_id: str
    status: str
    scanned_count: int = 0
    new_count: int = 0
    changed_count: int = 0
    unchanged_count: int = 0
    unsupported_count: int = 0
    failed_count: int = 0
    invalidated_count: int = 0
    error_summary: str | None = None


@dataclass(slots=True)
class _ScanCounts:
    scanned: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    unsupported: int = 0


class FeishuScanService:
    def __init__(self, *, repository: FeishuKnowledgeRepository, client: FeishuClient) -> None:
        self.repository = repository
        self.client = client

    async def scan(
        self,
        *,
        source_id: str,
        mode: str,
        operator_id: str | None = None,
    ) -> FeishuScanResult:
        if mode not in {"full", "incremental"}:
            raise ValueError("mode must be 'full' or 'incremental'")
        source = await self.repository.get_source(source_id)
        if source is None:
            raise LookupError(f"Feishu source not found: {source_id}")
        run = await self.repository.start_sync_run(
            source_id=source_id,
            run_type=mode,
            operator_id=operator_id,
        )
        run_id = run.run_id
        counts = _ScanCounts()
        seen_item_keys: set[str] = set()
        seen_at = utc_now()
        try:
            root = await self.client.get_node(source.wiki_root_token)
            await self._scan_node(
                source=source,
                node=root,
                parent_item_key=None,
                parent_path=None,
                counts=counts,
                seen_item_keys=seen_item_keys,
                seen_at=seen_at,
                visited=set(),
            )
            invalidated_count = await self.repository.complete_successful_scan(
                run_id=run_id,
                source_id=source_id,
                seen_item_keys=seen_item_keys,
                seen_at=seen_at,
                scanned_count=counts.scanned,
                new_count=counts.new,
                changed_count=counts.changed,
                unchanged_count=counts.unchanged,
                unsupported_count=counts.unsupported,
            )
        except Exception as exc:
            error_summary = f"{type(exc).__name__}: {exc}"
            result = FeishuScanResult(
                run_id=run_id,
                status="failed",
                scanned_count=counts.scanned,
                new_count=counts.new,
                changed_count=counts.changed,
                unchanged_count=counts.unchanged,
                unsupported_count=counts.unsupported,
                failed_count=1,
                invalidated_count=0,
                error_summary=error_summary,
            )
            await self._finish_run(result)
            return result

        result = FeishuScanResult(
            run_id=run_id,
            status="succeeded",
            scanned_count=counts.scanned,
            new_count=counts.new,
            changed_count=counts.changed,
            unchanged_count=counts.unchanged,
            unsupported_count=counts.unsupported,
            invalidated_count=invalidated_count,
        )
        return result

    async def _scan_node(
        self,
        *,
        source: FeishuSource,
        node: FeishuNode,
        parent_item_key: str | None,
        parent_path: str | None,
        counts: _ScanCounts,
        seen_item_keys: set[str],
        seen_at: datetime,
        visited: set[str],
    ) -> None:
        if node.node_token in visited:
            return
        visited.add(node.node_token)
        title = node.title or node.node_token
        path_text = f"{parent_path} / {title}" if parent_path else title
        page_key = f"page:{node.space_id}:{node.node_token}"
        page_content = await self.client.get_wiki_document(node)
        await self._record_material(
            source=source,
            item_key=page_key,
            item_type="page",
            title=title,
            parent_item_key=parent_item_key,
            path_text=path_text,
            source_url=self._page_url(source.wiki_root_url, node.node_token),
            token=node.obj_token or node.node_token,
            revision=node.revision,
            source_updated_at=node.source_updated_at,
            supported=True,
            counts=counts,
            seen_item_keys=seen_item_keys,
            seen_at=seen_at,
            content=page_content.content,
        )
        for attachment in page_content.attachments:
            await self._record_attachment(
                source=source,
                attachment=attachment,
                parent_item_key=page_key,
                parent_path=path_text,
                counts=counts,
                seen_item_keys=seen_item_keys,
                seen_at=seen_at,
            )
        children = await self.client.list_children(node.node_token)
        for child in children:
            await self._scan_node(
                source=source,
                node=child,
                parent_item_key=page_key,
                parent_path=path_text,
                counts=counts,
                seen_item_keys=seen_item_keys,
                seen_at=seen_at,
                visited=visited,
            )

    async def _record_attachment(
        self,
        *,
        source: FeishuSource,
        attachment: FeishuAttachment,
        parent_item_key: str,
        parent_path: str,
        counts: _ScanCounts,
        seen_item_keys: set[str],
        seen_at: datetime,
    ) -> None:
        item_type, supported = self._classify_attachment(attachment.name, attachment.file_type)
        await self._record_material(
            source=source,
            item_key=f"attachment:{attachment.file_token}",
            item_type=item_type,
            title=attachment.name,
            parent_item_key=parent_item_key,
            path_text=f"{parent_path} / {attachment.name}",
            source_url=None,
            token=attachment.file_token,
            revision=attachment.revision,
            source_updated_at=attachment.source_updated_at,
            supported=supported,
            counts=counts,
            seen_item_keys=seen_item_keys,
            seen_at=seen_at,
        )

    async def _record_material(
        self,
        *,
        source: FeishuSource,
        item_key: str,
        item_type: str,
        title: str,
        parent_item_key: str | None,
        path_text: str,
        source_url: str | None,
        token: str,
        revision: str | None,
        source_updated_at: str | None,
        supported: bool,
        counts: _ScanCounts,
        seen_item_keys: set[str],
        seen_at: datetime,
        content: bytes | None = None,
    ) -> None:
        updated_at = coerce_any_to_utc_datetime(source_updated_at)
        normalized_updated_at = updated_at.isoformat() if updated_at is not None else None
        item, item_created = await self.repository.upsert_source_item(
            source_id=source.source_id,
            item_key=item_key,
            item_type=item_type,
            title=title,
            parent_item_key=parent_item_key,
            path_text=path_text,
            source_url=source_url,
            source_updated_at=updated_at,
            seen_at=seen_at,
        )
        seen_item_keys.add(item_key)
        counts.scanned += 1
        current = await self.repository.find_current_version(item.item_id)
        if not supported:
            counts.unsupported += 1
            if self._metadata_matches(current, revision, normalized_updated_at):
                return
            content_hash = sha256(f"unsupported:{token}:{revision}:{normalized_updated_at}".encode()).hexdigest()
            await self.repository.create_material_version(
                item_id=item.item_id,
                revision=self._version_revision(revision, normalized_updated_at, content_hash),
                content_hash=content_hash,
                processing_status="unsupported",
                processing_params=self._processing_params(normalized_updated_at),
            )
            return

        if self._metadata_matches(current, revision, normalized_updated_at):
            counts.unchanged += 1
            return
        if content is None:
            download = await self.client.download(token)
            content = download.content
        content_hash = sha256(content).hexdigest()
        if (
            current is not None
            and revision is None
            and normalized_updated_at is None
            and current.content_hash == content_hash
        ):
            counts.unchanged += 1
            return
        _, version_created = await self.repository.create_material_version(
            item_id=item.item_id,
            revision=self._version_revision(revision, normalized_updated_at, content_hash),
            content_hash=content_hash,
            processing_status="discovered",
            processing_params=self._processing_params(normalized_updated_at),
        )
        if not version_created:
            counts.unchanged += 1
        elif item_created:
            counts.new += 1
        else:
            counts.changed += 1

    async def _finish_run(self, result: FeishuScanResult) -> None:
        updated = await self.repository.finish_sync_run(
            run_id=result.run_id,
            status=result.status,
            scanned_count=result.scanned_count,
            new_count=result.new_count,
            changed_count=result.changed_count,
            unchanged_count=result.unchanged_count,
            unsupported_count=result.unsupported_count,
            failed_count=result.failed_count,
            invalidated_count=result.invalidated_count,
            error_summary=result.error_summary,
        )
        if not updated:
            raise RuntimeError(f"Feishu sync run is no longer running: {result.run_id}")

    @staticmethod
    def _metadata_matches(
        current: FeishuMaterialVersion | None,
        revision: str | None,
        source_updated_at: str | None,
    ) -> bool:
        if current is None:
            return False
        if revision is not None:
            return current.revision == revision
        if source_updated_at is not None:
            current_updated_at = (current.processing_params or {}).get("source_updated_at")
            return current_updated_at == source_updated_at
        return False

    @staticmethod
    def _version_revision(revision: str | None, source_updated_at: str | None, content_hash: str) -> str:
        if revision is not None:
            return revision
        if source_updated_at is not None:
            return f"updated:{source_updated_at}"
        return f"hash:{content_hash}"

    @staticmethod
    def _processing_params(source_updated_at: str | None) -> dict[str, str]:
        return {"source_updated_at": source_updated_at} if source_updated_at is not None else {}

    @staticmethod
    def _classify_attachment(name: str, file_type: str | None = None) -> tuple[str, bool]:
        if file_type and file_type.lower() == "image":
            return "attachment", True
        extension = Path(name).suffix.lower()
        if extension in AUDIO_EXTENSIONS:
            return "audio", False
        if extension in VIDEO_EXTENSIONS:
            return "video", False
        return "attachment", extension in SUPPORTED_EXTENSIONS

    @staticmethod
    def _page_url(root_url: str | None, node_token: str) -> str | None:
        if root_url is None:
            return None
        prefix, separator, _ = root_url.rpartition("/")
        return f"{prefix}/{node_token}" if separator else root_url
