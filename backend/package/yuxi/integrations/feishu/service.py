from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import mimetypes
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Protocol

from yuxi.governance.content_quality import assess_content
from yuxi.governance.source_change_service import SourceChangeService
from yuxi.integrations.feishu.client import FeishuClient, FeishuNotFoundError
from yuxi.integrations.feishu.schemas import FeishuAttachment, FeishuNode
from yuxi.repositories.feishu_knowledge_repository import FeishuKnowledgeRepository
from yuxi.storage.postgres.models_knowledge import FeishuMaterialVersion, FeishuSource
from yuxi.utils.datetime_utils import coerce_any_to_utc_datetime, utc_now

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xls",
    ".xlsx",
    ".txt",
    ".md",
    ".markdown",
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


class FeishuArchiveAdapter(Protocol):
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
    ) -> str: ...


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
    impact_summary: dict[str, object] | None = None
    error_summary: str | None = None


@dataclass(slots=True)
class _ScanCounts:
    scanned: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    unsupported: int = 0
    image_changes: int = 0
    layout_check_pending: int = 0


class FeishuScanService:
    def __init__(
        self,
        *,
        repository: FeishuKnowledgeRepository,
        client: FeishuClient,
        archive_adapter: FeishuArchiveAdapter | None = None,
    ) -> None:
        self.repository = repository
        self.client = client
        self.archive_adapter = archive_adapter
        self._progress_callback: Callable[[int, str], Awaitable[None]] | None = None

    async def scan(
        self,
        *,
        source_id: str,
        mode: str,
        operator_id: str | None = None,
        progress_callback: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> FeishuScanResult:
        if mode not in {"full", "incremental"}:
            raise ValueError("mode must be 'full' or 'incremental'")
        source = await self.repository.get_source(source_id)
        if source is None:
            raise LookupError(f"Feishu source not found: {source_id}")
        if mode == "incremental" and not await self.repository.has_successful_full_scan(source_id):
            raise ValueError("Feishu incremental scan requires a successful full scan")
        run = await self.repository.start_sync_run(
            source_id=source_id,
            run_type=mode,
            operator_id=operator_id,
        )
        run_id = run.run_id
        counts = _ScanCounts()
        self._progress_callback = progress_callback
        seen_item_keys: set[str] = set()
        seen_at = utc_now()
        try:
            root = await self.client.get_node(source.wiki_root_token)
            visited: set[str] = set()
            scan_scope = getattr(source, "scan_scope", "root") or "root"
            if scan_scope == "space":
                if not root.space_id:
                    raise RuntimeError("Feishu root node did not include a space ID")
                # The configured node is used to discover the space. The
                # space-level endpoint is authoritative for sibling top-level
                # nodes, so the configured root is not treated as the only
                # branch anymore.
                top_nodes = await self.client.list_nodes(root.space_id)
                if not top_nodes:
                    # Some tenants return no item when the configured node is
                    # the only visible top-level node. Keep it discoverable
                    # while still allowing a permission error to propagate.
                    top_nodes = [root]
                for top_node in top_nodes:
                    await self._scan_node(
                        source=source,
                        node=top_node,
                        parent_item_key=None,
                        parent_path=None,
                        counts=counts,
                        seen_item_keys=seen_item_keys,
                        seen_at=seen_at,
                        visited=visited,
                        run_id=run_id,
                    )
            else:
                await self._scan_node(
                    source=source,
                    node=root,
                    parent_item_key=None,
                    parent_path=None,
                    counts=counts,
                    seen_item_keys=seen_item_keys,
                    seen_at=seen_at,
                    visited=visited,
                    run_id=run_id,
                )
                await self._verify_unseen_page_roots(
                    source_id=source_id,
                    seen_item_keys=seen_item_keys,
                    root=root,
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
                impact_summary=self._impact_summary(counts, invalidated_count=0),
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
                impact_summary=self._impact_summary(counts, invalidated_count=0),
                error_summary=error_summary,
            )
            await self._finish_run(result, source_id=source_id)
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
            impact_summary=self._impact_summary(counts, invalidated_count=invalidated_count),
        )
        return result

    async def _verify_unseen_page_roots(
        self,
        *,
        source_id: str,
        seen_item_keys: set[str],
        root: FeishuNode,
    ) -> None:
        unseen_roots = await self.repository.list_unseen_valid_page_roots(
            source_id=source_id,
            seen_item_keys=seen_item_keys,
        )
        for item in unseen_roots:
            prefix, _, node_token = item.item_key.partition(":")
            if prefix != "page" or ":" not in node_token:
                raise RuntimeError(f"Invalid Feishu page item key: {item.item_key}")
            node_token = node_token.rsplit(":", 1)[-1]
            try:
                node = await self.client.get_node(node_token)
            except FeishuNotFoundError:
                continue
            if node.space_id != root.space_id:
                continue

            visited = {node.node_token}
            while node.parent_node_token:
                parent_token = node.parent_node_token
                if parent_token == root.node_token:
                    raise RuntimeError(f"Feishu page omitted from traversal but remains under root: {node_token}")
                if parent_token in visited:
                    raise RuntimeError(f"Feishu page parent chain contains a cycle: {node_token}")
                visited.add(parent_token)
                node = await self.client.get_node(parent_token)

            if node.node_token == root.node_token:
                raise RuntimeError(f"Feishu page omitted from traversal but remains under root: {node_token}")

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
        run_id: str,
    ) -> None:
        if node.node_token in visited:
            return
        visited.add(node.node_token)
        title = node.title or node.node_token
        path_text = f"{parent_path} / {title}" if parent_path else title
        node_url = self._page_url(source.wiki_root_url, node.node_token)
        node_token = node.obj_token or node.node_token
        if node.obj_type == "docx":
            node_item_key = f"page:{node.space_id}:{node.node_token}"
            page_content = await self.client.get_wiki_document(node)
            content_quality = assess_content(
                content=page_content.content.decode("utf-8", errors="replace"),
                title=title,
            )
            # A Feishu wiki page with children and no body is a navigation
            # heading, not a knowledge source. Pages with body remain normal
            # materials even when they also contain child nodes.
            is_directory = bool(node.has_child and not content_quality["has_body"])
            await self._record_material(
                source=source,
                item_key=node_item_key,
                item_type="directory" if is_directory else "page",
                title=title,
                parent_item_key=parent_item_key,
                path_text=path_text,
                source_url=node_url,
                token=node_token,
                revision=page_content.revision,
                source_updated_at=node.source_updated_at,
                supported=not is_directory,
                counts=counts,
                seen_item_keys=seen_item_keys,
                seen_at=seen_at,
                content=page_content.content,
                run_id=run_id,
            )
            for attachment in page_content.attachments:
                await self._record_attachment(
                    source=source,
                    attachment=attachment,
                    parent_item_key=node_item_key,
                    parent_path=path_text,
                    parent_source_url=node_url,
                    counts=counts,
                    seen_item_keys=seen_item_keys,
                    seen_at=seen_at,
                    run_id=run_id,
                )
        elif node.obj_type == "file":
            node_item_key = f"file:{node.space_id}:{node.node_token}"
            item_type, supported = self._classify_attachment(title, node.obj_type)
            await self._record_material(
                source=source,
                item_key=node_item_key,
                item_type=item_type,
                title=title,
                parent_item_key=parent_item_key,
                path_text=path_text,
                source_url=node_url,
                token=node_token,
                revision=node.revision,
                source_updated_at=node.source_updated_at,
                supported=supported,
                counts=counts,
                seen_item_keys=seen_item_keys,
                seen_at=seen_at,
                run_id=run_id,
            )
        else:
            node_item_key = f"node:{node.space_id}:{node.node_token}"
            await self._record_material(
                source=source,
                item_key=node_item_key,
                item_type="unsupported",
                title=title,
                parent_item_key=parent_item_key,
                path_text=path_text,
                source_url=node_url,
                token=node_token,
                revision=node.revision,
                source_updated_at=node.source_updated_at,
                supported=False,
                counts=counts,
                seen_item_keys=seen_item_keys,
                seen_at=seen_at,
                run_id=run_id,
            )
        children = await self.client.list_children(node.node_token)
        for child in children:
            await self._scan_node(
                source=source,
                node=child,
                parent_item_key=node_item_key,
                parent_path=path_text,
                counts=counts,
                seen_item_keys=seen_item_keys,
                seen_at=seen_at,
                visited=visited,
                run_id=run_id,
            )

    async def _record_attachment(
        self,
        *,
        source: FeishuSource,
        attachment: FeishuAttachment,
        parent_item_key: str,
        parent_path: str,
        parent_source_url: str | None,
        counts: _ScanCounts,
        seen_item_keys: set[str],
        seen_at: datetime,
        run_id: str,
    ) -> None:
        item_type, supported = self._classify_attachment(attachment.name, attachment.file_type)
        await self._record_material(
            source=source,
            item_key=f"attachment:{attachment.file_token}",
            item_type=item_type,
            title=attachment.name,
            parent_item_key=parent_item_key,
            path_text=f"{parent_path} / {attachment.name}",
            source_url=parent_source_url,
            token=attachment.file_token,
            revision=attachment.revision,
            source_updated_at=attachment.source_updated_at,
            supported=supported,
            counts=counts,
            seen_item_keys=seen_item_keys,
            seen_at=seen_at,
            download_type=attachment.download_type,
            run_id=run_id,
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
        download_type: str = "file",
        run_id: str,
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
        if self._progress_callback is not None:
            await self._progress_callback(counts.scanned, title)
        current = await self.repository.find_current_version(item.item_id)
        if not supported:
            if item_type != "directory":
                counts.unsupported += 1
            if self._metadata_matches(current, revision, normalized_updated_at):
                if item_type == "directory" and current is not None:
                    current.processing_status = "skipped"
                    current.review_status = "not_required"
                    current.review_comment = "目录节点仅用于组织下级内容，无需加工或审核"
                    current.processing_params = {
                        **(current.processing_params or {}),
                        "content_quality": {
                            "checked": True,
                            "has_body": False,
                            "body_length": 0,
                            "classification": "directory",
                            "reason": "目录节点仅用于组织下级内容",
                        },
                        "skip_reason": "directory",
                    }
                    await self.repository.session.flush()
                return
            content_hash = sha256(f"unsupported:{token}:{revision}:{normalized_updated_at}".encode()).hexdigest()
            version, _ = await self.repository.create_material_version(
                item_id=item.item_id,
                revision=self._version_revision(revision, normalized_updated_at, content_hash),
                content_hash=content_hash,
                processing_status="skipped" if item_type == "directory" else "unsupported",
                processing_params={
                    **self._processing_params(normalized_updated_at),
                    **(
                        {
                            "content_quality": {
                                "checked": True,
                                "has_body": False,
                                "body_length": 0,
                                "classification": "directory",
                                "reason": "目录节点仅用于组织下级内容",
                            },
                            "skip_reason": "directory",
                        }
                        if item_type == "directory"
                        else {}
                    ),
                },
                sync_run_id=run_id,
            )
            if item_type == "directory":
                version.review_status = "not_required"
                version.review_comment = "目录节点仅用于组织下级内容，无需加工或审核"
                await self.repository.session.flush()
            return

        metadata_matches = self._metadata_matches(current, revision, normalized_updated_at)
        archive_missing = self.archive_adapter is not None and current is not None and not current.source_object_path
        if metadata_matches and not archive_missing:
            counts.unchanged += 1
            return
        content_type = "text/markdown" if item_type == "page" else None
        if content is None:
            download = await self.client.download(token, download_type=download_type)
            content = download.content
            content_type = download.content_type
        content_hash = sha256(content).hexdigest()
        next_revision = self._version_revision(revision, normalized_updated_at, content_hash)
        if current is not None and current.content_hash == content_hash and not archive_missing:
            current.revision = next_revision
            current.processing_params = {
                **(current.processing_params or {}),
                **self._processing_params(normalized_updated_at),
                "source_url": source_url,
                "wiki_path": path_text,
                "item_type": item_type,
                "title": title,
                "download_type": download_type,
            }
            await self.repository.session.flush()
            counts.unchanged += 1
            return
        version, version_created = await self.repository.create_material_version(
            item_id=item.item_id,
            revision=next_revision,
            content_hash=content_hash,
            processing_status="discovered",
            processing_params=self._processing_params(normalized_updated_at),
            sync_run_id=run_id,
        )
        if self.archive_adapter is not None and not version.source_object_path:
            content_type = content_type or mimetypes.guess_type(title)[0]
            object_path = await self.archive_adapter.archive(
                source_id=source.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                item_type=item_type,
                title=title,
                content=content,
                content_type=content_type,
            )
            version.source_object_path = object_path
            version.processing_params = {
                **(version.processing_params or {}),
                "object_path": object_path,
                "content_type": content_type,
                "source_url": source_url,
                "wiki_path": path_text,
                "material_version": version.version_id,
                "item_type": item_type,
                "title": title,
                "download_type": download_type,
            }
            await self.repository.session.flush()
        if version_created:
            suffix = Path(title).suffix.lower()
            is_image = suffix in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
            is_layout_candidate = suffix in {".pdf", ".docx", ".pptx", ".xls", ".xlsx"}
            version.processing_params = {
                **(version.processing_params or {}),
                "scan_impact": {
                    "changeType": "NEW" if item_created else "MODIFIED",
                    "imageChanged": is_image and not item_created,
                    # A scan only proves that the file bytes changed.  The
                    # parser must compare source segments before we can claim
                    # a real layout change (metadata-only updates are common
                    # for Office/PDF files).
                    "layoutChanged": None,
                    "layoutCheckPending": is_layout_candidate and not item_created,
                },
            }
            if not item_created and is_image:
                counts.image_changes += 1
            if not item_created and is_layout_candidate:
                counts.layout_check_pending += 1
            await self.repository.session.flush()
            await SourceChangeService(self.repository.session).register_new_material_version(version.version_id)
        if not version_created:
            counts.unchanged += 1
        elif item_created:
            counts.new += 1
        else:
            counts.changed += 1

    async def _finish_run(self, result: FeishuScanResult, *, source_id: str) -> None:
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
            impact_summary=result.impact_summary,
            error_summary=result.error_summary,
        )
        if not updated:
            raise RuntimeError(f"Feishu sync run is no longer running: {result.run_id}")
        if result.status == "failed":
            await self.repository.append_event(
                source_id=source_id,
                event_type="scan_failed",
                from_status="running",
                to_status="failed",
                message=result.error_summary,
                payload_json={"run_id": result.run_id},
            )

    @staticmethod
    def _impact_summary(counts: _ScanCounts, *, invalidated_count: int) -> dict[str, object]:
        return {
            "new": counts.new,
            "modified": counts.changed,
            "deleted": invalidated_count,
            "imageChanged": counts.image_changes,
            "layoutChanged": 0,
            "layoutCheckPending": counts.layout_check_pending,
            "affectedKnowledgeCount": counts.new + counts.changed + invalidated_count,
            "affectedRelationCount": None,
            "categories": [
                category
                for category, count in (
                    ("新增", counts.new),
                    ("修改", counts.changed),
                    ("删除", invalidated_count),
                    ("图片", counts.image_changes),
                    ("待核对版式", counts.layout_check_pending),
                )
                if count
            ],
        }

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
