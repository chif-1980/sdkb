"""Offline end-to-end coverage for the Feishu knowledge pipeline."""

from __future__ import annotations

import json
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers import feishu_knowledge_router as router_module
from server.routers.feishu_knowledge_router import FeishuReviewService
import yuxi.integrations.feishu.client as feishu_client_module
from yuxi.integrations.feishu import FeishuClient
from yuxi.integrations.feishu.service import FeishuScanService
from yuxi.repositories.feishu_knowledge_repository import FeishuKnowledgeRepository
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import FeishuMaterialVersion, FeishuProcessingEvent, FeishuSourceItem

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]

SOURCE_ID = "source-offline"
TARGET_KB_ID = "kb-offline"
OFFLINE_APP_ID = "cli_offline_app"
OFFLINE_APP_SECRET = "offline-app-secret"
OFFLINE_TOKEN = "offline-tenant-token"
AUTH_PATH = "/open-apis/auth/v3/tenant_access_token/internal"


async def _no_sleep(_delay: float) -> None:
    return None


class FakeFeishuTransport:
    def __init__(self) -> None:
        self.auth_calls = 0
        self.permission_denied = False
        self.fail_children_for: str | None = None
        self.rate_limit_once = False
        self.rate_limit_attempts = 0
        self.page_revisions = {"root": 1, "child": 1, "grandchild": 1}
        self.page_contents = {
            "root": "# Quickdone launch handbook v1",
            "child": "# Product operations\nOperations details",
            "grandchild": "# Support playbook\nSupport details",
        }
        self.page_titles = {
            "root": "Quickdone Handbook",
            "child": "Product Operations",
            "grandchild": "Support Playbook",
        }
        self.children = {"root": ["child"], "child": ["grandchild"], "grandchild": []}
        self.attachments = {
            "pdf": ("Guide.pdf", b"PDF onboarding guide", "application/pdf"),
            "txt": ("Notes.txt", b"Release notes", "text/plain"),
            "png": ("Diagram.png", b"PNG architecture diagram", "image/png"),
            "audio": ("Meeting.mp3", b"audio", "audio/mpeg"),
            "video": ("Demo.mp4", b"video", "video/mp4"),
        }

    def node_payload(self, token: str) -> dict:
        return {
            "space_id": "space-offline",
            "node_token": token,
            "obj_token": f"doc-{token}",
            "obj_type": "docx",
            "title": self.page_titles[token],
            "parent_node_token": {"root": None, "child": "root", "grandchild": "child"}[token],
            "has_child": bool(self.children[token]),
            "obj_edit_time": str(self.page_revisions[token]),
        }

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == AUTH_PATH:
            assert request.method == "POST"
            assert request.headers.get("authorization") is None
            assert request.read()
            assert request.content
            payload = json.loads(request.content)
            assert payload == {"app_id": OFFLINE_APP_ID, "app_secret": OFFLINE_APP_SECRET}
            self.auth_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": OFFLINE_TOKEN, "expire": 7200},
            )

        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {OFFLINE_TOKEN}"
        path = request.url.path
        if path == "/open-apis/wiki/v2/spaces/get_node":
            token = request.url.params["token"]
            if self.permission_denied:
                return httpx.Response(403, json={"token": OFFLINE_TOKEN})
            if self.rate_limit_once and token == "root":
                self.rate_limit_attempts += 1
                if self.rate_limit_attempts == 1:
                    return httpx.Response(
                        429,
                        headers={"retry-after": "0", "x-request-id": "retry-offline"},
                        json={"token": OFFLINE_TOKEN},
                    )
                self.rate_limit_once = False
            return httpx.Response(200, json={"code": 0, "data": {"node": self.node_payload(token)}})
        if path == "/open-apis/wiki/v2/spaces/space-offline/nodes":
            parent = request.url.params["parent_node_token"]
            if parent == self.fail_children_for:
                return httpx.Response(503, headers={"x-request-id": "partial-offline"})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [self.node_payload(token) for token in self.children[parent]],
                        "has_more": False,
                    },
                },
            )
        if path.endswith("/raw_content"):
            page = path.split("/")[-2].removeprefix("doc-")
            return httpx.Response(200, json={"code": 0, "data": {"content": self.page_contents[page]}})
        if path.endswith("/blocks"):
            page = path.split("/")[-2].removeprefix("doc-")
            items = []
            if page == "root":
                items = [
                    {"block_id": token, "file": {"token": token, "name": name}}
                    for token, (name, _content, _content_type) in self.attachments.items()
                ]
            return httpx.Response(200, json={"code": 0, "data": {"items": items, "has_more": False}})
        if path.startswith("/open-apis/docx/v1/documents/"):
            page = path.rsplit("/", 1)[-1].removeprefix("doc-")
            return httpx.Response(
                200,
                json={"code": 0, "data": {"document": {"revision_id": self.page_revisions[page]}}},
            )
        if path.startswith("/open-apis/drive/v1/medias/") and path.endswith("/download"):
            token = path.split("/")[-2]
            name, content, content_type = self.attachments[token]
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": content_type, "content-disposition": f'attachment; filename="{name}"'},
            )
        raise AssertionError(f"Unexpected fake Feishu request: {request.url}")


class LocalMinio:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def archive(self, **kwargs) -> str:
        extension = ".md" if kwargs["item_type"] == "page" else Path(kwargs["title"]).suffix.lower()
        object_path = (
            f"minio://knowledgebases/feishu/{kwargs['source_id']}/{kwargs['item_id']}/"
            f"{kwargs['version_id']}/source{extension}"
        )
        self.objects[object_path] = kwargs["content"]
        return object_path


class LocalMilvus:
    def __init__(self, minio: LocalMinio) -> None:
        self.minio = minio
        self.files: dict[str, dict] = {}
        self.vectors: dict[str, dict] = {}
        self.fail_next_index = False
        self._next_file = 1

    async def add_file_record(self, kb_id, object_path, *, params, operator_id):
        assert kb_id == TARGET_KB_ID
        assert operator_id == "admin-offline"
        file_id = f"file-{self._next_file}"
        self._next_file += 1
        self.files[file_id] = {"object_path": object_path, "params": params, "parsed_content": None}
        return {"file_id": file_id}

    async def parse_file(self, kb_id, file_id, *, operator_id):
        assert kb_id == TARGET_KB_ID
        record = self.files[file_id]
        record["parsed_content"] = self.minio.objects[record["object_path"]].decode("utf-8", errors="replace")
        return {"status": "parsed"}

    async def get_file_content(self, kb_id, file_id):
        assert kb_id == TARGET_KB_ID
        return {"content": self.files[file_id]["parsed_content"] or ""}

    async def index_file(self, kb_id, file_id, *, operator_id, params):
        assert kb_id == TARGET_KB_ID
        if self.fail_next_index:
            self.fail_next_index = False
            raise RuntimeError("forced local Milvus indexing failure")
        record = self.files[file_id]
        self.vectors[file_id] = {"content": record["parsed_content"], "feishu": params["feishu"]}
        return {"status": "indexed", "chunk_count": 1}

    async def delete_file(self, kb_id, file_id):
        assert kb_id == TARGET_KB_ID
        self.files.pop(file_id, None)
        self.vectors.pop(file_id, None)

    def search(self, query: str) -> dict | None:
        for vector in self.vectors.values():
            if query.casefold() not in vector["content"].casefold():
                continue
            citation = vector["feishu"]
            return {
                "content": vector["content"].lstrip("# "),
                "source_url": citation["source_url"],
                "wiki_path": citation["wiki_path"],
                "title": citation["page_info"]["title"],
                "item_type": citation["page_info"]["item_type"],
                "material_version": citation["material_version"],
            }
        return None


class OfflinePipeline:
    def __init__(self, session_factory, transport: FakeFeishuTransport, minio: LocalMinio, milvus: LocalMilvus):
        self.session_factory = session_factory
        self.transport = transport
        self.minio = minio
        self.milvus = milvus

    async def scan(self, mode: str):
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(self.transport))
        client = FeishuClient(
            client=http_client,
            environ={"FEISHU_APP_ID": OFFLINE_APP_ID, "FEISHU_APP_SECRET": OFFLINE_APP_SECRET},
            sleep=_no_sleep,
        )
        try:
            async with self.session_factory() as session:
                result = await FeishuScanService(
                    repository=FeishuKnowledgeRepository(session),
                    client=client,
                    archive_adapter=self.minio,
                ).scan(source_id=SOURCE_ID, mode=mode, operator_id="admin-offline")
                await session.commit()
                return result
        finally:
            await http_client.aclose()

    async def process_discovered(self) -> list[str]:
        async with self.session_factory() as session:
            version_ids = await FeishuKnowledgeRepository(session).queue_archived_versions_for_processing(
                source_id=SOURCE_ID,
                operator_id="admin-offline",
            )
            await session.commit()
        for version_id in version_ids:
            await router_module._run_processing_worker(version_id, operator_id="admin-offline")
        return version_ids

    async def reject(self, version_id: str, *, reason: str) -> None:
        async with self.session_factory() as session:
            await FeishuReviewService(session).reject(
                version_id,
                operator_id="admin-offline",
                reason=reason,
            )
            await session.commit()

    async def approve_and_publish(self, version_id: str) -> None:
        await self.approve(version_id)
        await router_module._run_publish_worker(version_id, operator_id="admin-offline")

    async def approve(self, version_id: str) -> None:
        async with self.session_factory() as session:
            await FeishuReviewService(session).approve(version_id, operator_id="admin-offline")
            await session.commit()

    async def materials(self) -> list[tuple[FeishuMaterialVersion, FeishuSourceItem]]:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .order_by(FeishuMaterialVersion.id)
            )
            return list(rows)

    async def version_counts_by_title(self) -> dict[str, int]:
        return dict(Counter(item.title for _version, item in await self.materials()))

    async def source_validity_by_title(self) -> dict[str, str]:
        async with self.session_factory() as session:
            result = await session.execute(select(FeishuSourceItem).order_by(FeishuSourceItem.id))
            return {item.title: item.source_validity for item in result.scalars()}

    async def material_versions(self, title: str) -> list[FeishuMaterialVersion]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(FeishuMaterialVersion)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuSourceItem.title == title)
                .order_by(FeishuMaterialVersion.id)
            )
            return list(result.scalars())

    async def latest_material(self, title: str) -> tuple[FeishuMaterialVersion, FeishuSourceItem]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuSourceItem.title == title)
                .order_by(FeishuMaterialVersion.id.desc())
                .limit(1)
            )
            return result.one()

    async def rejection_evidence(self, title: str) -> dict:
        async with self.session_factory() as session:
            version = (
                await session.execute(
                    select(FeishuMaterialVersion)
                    .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                    .where(FeishuSourceItem.title == title)
                )
            ).scalar_one()
            event = (
                await session.execute(
                    select(FeishuProcessingEvent).where(
                        FeishuProcessingEvent.version_id == version.version_id,
                        FeishuProcessingEvent.event_type == "rejected",
                    )
                )
            ).scalar_one_or_none()
            return {
                "version_id": version.version_id,
                "review_status": version.review_status,
                "review_comment": version.review_comment,
                "event": None
                if event is None
                else {
                    "version_id": event.version_id,
                    "event_type": event.event_type,
                    "from_status": event.from_status,
                    "to_status": event.to_status,
                    "operator_id": event.operator_id,
                    "message": event.message,
                },
            }

    async def initial_publish(self) -> dict:
        scan = await self.scan("full")
        await self.process_discovered()
        materials = await self.materials()
        current = [(version, item) for version, item in materials if version.processing_status == "awaiting_review"]
        awaiting_review_count = len(current)
        rejected_version, rejected_item = next(
            (version, item) for version, item in current if item.title == "Notes.txt"
        )
        await self.reject(rejected_version.version_id, reason="Not approved for publication")
        rejection = await self.rejection_evidence(rejected_item.title)
        for version, _item in current:
            if version.version_id != rejected_version.version_id:
                await self.approve_and_publish(version.version_id)
        final_materials = await self.materials()
        material_types = Counter(item.item_type for _version, item in final_materials)
        retrieval = self.milvus.search("launch handbook")
        return {
            "scan": {
                "status": scan.status,
                "scanned_count": scan.scanned_count,
                "new_count": scan.new_count,
                "unsupported_count": scan.unsupported_count,
                "failed_count": scan.failed_count,
            },
            "material_types": dict(material_types),
            "archived_count": len(self.minio.objects),
            "awaiting_review_count": awaiting_review_count,
            "rejected_title": rejected_item.title,
            "rejection": rejection,
            "published_count": sum(version.processing_status == "published" for version, _item in final_materials),
            "retrieval": {key: retrieval[key] for key in ("content", "source_url", "wiki_path", "title", "item_type")},
        }


@pytest.fixture
async def offline_pipeline(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'feishu-e2e.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    minio = LocalMinio()
    milvus = LocalMilvus(minio)

    @asynccontextmanager
    async def session_context():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    monkeypatch.setattr(router_module.pg_manager, "get_async_session_context", session_context)
    monkeypatch.setattr(router_module, "knowledge_base", milvus)
    async with session_factory() as session:
        await FeishuKnowledgeRepository(session).get_or_create_source(
            source_id=SOURCE_ID,
            name="Offline Feishu Wiki",
            wiki_root_token="root",
            wiki_root_url="https://quickdone.test/wiki/root",
            target_kb_id=TARGET_KB_ID,
            credential_env_name="GLOBAL_FEISHU_APP",
            created_by="admin-offline",
        )
        await session.commit()
    try:
        yield OfflinePipeline(session_factory, FakeFeishuTransport(), minio, milvus)
    finally:
        await engine.dispose()


async def test_full_scan_review_publish_and_retrieve_source(offline_pipeline):
    result = await offline_pipeline.initial_publish()

    assert offline_pipeline.transport.auth_calls == 1
    assert result["scan"] == {
        "status": "succeeded",
        "scanned_count": 8,
        "new_count": 6,
        "unsupported_count": 2,
        "failed_count": 0,
    }
    assert result["material_types"] == {
        "page": 3,
        "attachment": 3,
        "audio": 1,
        "video": 1,
    }
    assert result["archived_count"] == 6
    assert result["awaiting_review_count"] == 6
    assert result["rejected_title"] == "Notes.txt"
    rejection = result["rejection"]
    assert rejection["review_status"] == "rejected"
    assert rejection["review_comment"] == "Not approved for publication"
    assert rejection["event"] == {
        "version_id": rejection["version_id"],
        "event_type": "rejected",
        "from_status": "pending",
        "to_status": "rejected",
        "operator_id": "admin-offline",
        "message": "Not approved for publication",
    }
    assert result["published_count"] == 5
    assert result["retrieval"] == {
        "content": "Quickdone launch handbook v1",
        "source_url": "https://quickdone.test/wiki/root",
        "wiki_path": "Quickdone Handbook",
        "title": "Quickdone Handbook",
        "item_type": "page",
    }


async def test_incremental_scan_keeps_v1_searchable_until_v2_is_atomically_published(offline_pipeline):
    await offline_pipeline.initial_publish()
    before_counts = await offline_pipeline.version_counts_by_title()
    old_version, root_item = await offline_pipeline.latest_material("Quickdone Handbook")

    assert root_item.active_version_id == old_version.version_id
    assert offline_pipeline.milvus.search("handbook v1") is not None

    offline_pipeline.transport.page_revisions["root"] = 2
    offline_pipeline.transport.page_contents["root"] = "# Quickdone launch handbook v2"
    scan = await offline_pipeline.scan("incremental")
    after_counts = await offline_pipeline.version_counts_by_title()

    assert {
        "status": scan.status,
        "scanned_count": scan.scanned_count,
        "new_count": scan.new_count,
        "changed_count": scan.changed_count,
        "unchanged_count": scan.unchanged_count,
        "unsupported_count": scan.unsupported_count,
    } == {
        "status": "succeeded",
        "scanned_count": 8,
        "new_count": 0,
        "changed_count": 1,
        "unchanged_count": 5,
        "unsupported_count": 2,
    }
    assert after_counts == {**before_counts, "Quickdone Handbook": before_counts["Quickdone Handbook"] + 1}

    queued = await offline_pipeline.process_discovered()
    new_version, root_item = await offline_pipeline.latest_material("Quickdone Handbook")

    assert queued == [new_version.version_id]
    assert new_version.processing_status == "awaiting_review"
    assert root_item.active_version_id == old_version.version_id
    assert offline_pipeline.milvus.search("handbook v1") is not None
    assert offline_pipeline.milvus.search("handbook v2") is None

    await offline_pipeline.approve_and_publish(new_version.version_id)
    versions = await offline_pipeline.material_versions("Quickdone Handbook")
    active_version, root_item = await offline_pipeline.latest_material("Quickdone Handbook")

    assert active_version.version_id == new_version.version_id
    assert root_item.active_version_id == new_version.version_id
    assert [(version.revision, version.processing_status) for version in versions] == [
        ("1", "replaced"),
        ("2", "published"),
    ]
    assert offline_pipeline.milvus.search("handbook v1") is None
    assert offline_pipeline.milvus.search("handbook v2")["material_version"] == new_version.version_id


async def test_scan_failures_never_bulk_invalidate_and_rate_limit_retry_hides_token(
    offline_pipeline,
    monkeypatch,
):
    log_messages = []

    class CapturingLogger:
        def info(self, message, *args):
            log_messages.append(message.format(*args))

    monkeypatch.setattr(feishu_client_module, "logger", CapturingLogger())
    initial = await offline_pipeline.scan("full")
    original_validity = await offline_pipeline.source_validity_by_title()

    offline_pipeline.transport.permission_denied = True
    permission_failure = await offline_pipeline.scan("full")
    offline_pipeline.transport.permission_denied = False
    assert await offline_pipeline.source_validity_by_title() == original_validity

    offline_pipeline.transport.fail_children_for = "child"
    partial_failure = await offline_pipeline.scan("full")
    offline_pipeline.transport.fail_children_for = None
    assert await offline_pipeline.source_validity_by_title() == original_validity

    offline_pipeline.transport.rate_limit_once = True
    recovered = await offline_pipeline.scan("full")

    assert initial.unsupported_count == 2
    assert initial.failed_count == 0
    assert permission_failure.status == partial_failure.status == "failed"
    assert permission_failure.failed_count == partial_failure.failed_count == 1
    assert permission_failure.invalidated_count == partial_failure.invalidated_count == 0
    assert await offline_pipeline.source_validity_by_title() == original_validity
    assert set(original_validity.values()) == {"valid"}
    assert recovered.status == "succeeded"
    assert recovered.failed_count == 0
    assert offline_pipeline.transport.rate_limit_attempts == 2
    assert OFFLINE_APP_SECRET not in " ".join(log_messages)
    assert OFFLINE_TOKEN not in " ".join(log_messages)


async def test_index_failure_keeps_the_old_active_version_searchable(offline_pipeline):
    await offline_pipeline.initial_publish()
    old_version, _root_item = await offline_pipeline.latest_material("Quickdone Handbook")

    offline_pipeline.transport.page_revisions["root"] = 2
    offline_pipeline.transport.page_contents["root"] = "# Quickdone launch handbook v2"
    await offline_pipeline.scan("incremental")
    await offline_pipeline.process_discovered()
    new_version, _root_item = await offline_pipeline.latest_material("Quickdone Handbook")
    await offline_pipeline.approve(new_version.version_id)
    offline_pipeline.milvus.fail_next_index = True

    with pytest.raises(RuntimeError, match="forced local Milvus indexing failure"):
        await router_module._run_publish_worker(new_version.version_id, operator_id="admin-offline")

    failed_version, root_item = await offline_pipeline.latest_material("Quickdone Handbook")
    versions = await offline_pipeline.material_versions("Quickdone Handbook")

    assert failed_version.version_id == new_version.version_id
    assert failed_version.processing_status == "publish_failed"
    assert root_item.active_version_id == old_version.version_id
    assert [(version.revision, version.processing_status) for version in versions] == [
        ("1", "published"),
        ("2", "publish_failed"),
    ]
    assert offline_pipeline.milvus.search("handbook v1")["material_version"] == old_version.version_id
    assert offline_pipeline.milvus.search("handbook v2") is None
