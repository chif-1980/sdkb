from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.integrations.feishu.client import FeishuNotFoundError, FeishuPermissionError
from yuxi.integrations.feishu.schemas import FeishuAttachment, FeishuDownload, FeishuNode, FeishuPageContent
from yuxi.integrations.feishu.service import FeishuScanService
from yuxi.repositories.feishu_knowledge_repository import FeishuKnowledgeRepository
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuSourceItem,
    FeishuSyncRun,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class FakeFeishuClient:
    def __init__(
        self,
        *,
        nodes: dict[str, FeishuNode],
        children: dict[str, list[FeishuNode]] | None = None,
        page_attachments: dict[str, list[FeishuAttachment]] | None = None,
        page_contents: dict[str, bytes] | None = None,
        page_revisions: dict[str, str | None] | None = None,
        contents: dict[str, bytes] | None = None,
        error_at: tuple[str, str] | None = None,
        not_found_tokens: set[str] | None = None,
    ) -> None:
        self.nodes = nodes
        self.children = children or {}
        self.page_attachments = page_attachments or {}
        self.page_contents = page_contents or {}
        self.page_revisions = page_revisions or {}
        self.contents = contents or {}
        self.error_at = error_at
        self.not_found_tokens = not_found_tokens or set()
        self.get_calls: list[str] = []
        self.children_calls: list[str] = []
        self.list_nodes_calls: list[tuple[str, str | None]] = []
        self.attachment_calls: list[str] = []
        self.download_calls: list[tuple[str, str]] = []
        self.page_calls: list[str] = []

    def _raise_if_requested(self, operation: str, token: str) -> None:
        if self.error_at == (operation, token):
            raise FeishuPermissionError("permission denied")

    async def get_node(self, node_token: str) -> FeishuNode:
        self.get_calls.append(node_token)
        self._raise_if_requested("get", node_token)
        if node_token in self.not_found_tokens:
            raise FeishuNotFoundError("not found")
        return self.nodes[node_token]

    async def list_children(self, parent_node_token: str) -> list[FeishuNode]:
        self.children_calls.append(parent_node_token)
        self._raise_if_requested("children", parent_node_token)
        return self.children.get(parent_node_token, [])

    async def list_nodes(self, space_id: str, parent_node_token: str | None = None) -> list[FeishuNode]:
        self.list_nodes_calls.append((space_id, parent_node_token))
        self._raise_if_requested("nodes", parent_node_token or "<top-level>")
        return self.children.get(parent_node_token or "<top-level>", [])

    async def list_attachments(self, folder_token: str) -> list[FeishuAttachment]:
        raise AssertionError("page attachments must be discovered through get_wiki_document")

    async def get_wiki_document(self, node: FeishuNode) -> FeishuPageContent:
        token = node.obj_token or node.node_token
        self.page_calls.append(token)
        self._raise_if_requested("page", token)
        return FeishuPageContent(
            content=self.page_contents[token],
            attachments=self.page_attachments.get(token, []),
            revision=self.page_revisions.get(token, "1"),
        )

    async def download(self, file_token: str, *, download_type: str = "file") -> FeishuDownload:
        self.download_calls.append((file_token, download_type))
        self._raise_if_requested("download", file_token)
        return FeishuDownload(file_token=file_token, content=self.contents[file_token])


def node(
    token: str,
    title: str,
    *,
    parent: str | None = None,
    updated_at: str | None = "2026-08-13T00:00:00Z",
    has_child: bool = False,
    obj_type: str = "docx",
    obj_token: str | None = None,
) -> FeishuNode:
    return FeishuNode(
        space_id="space-1",
        node_token=token,
        obj_token=obj_token or f"obj-{token}",
        obj_type=obj_type,
        title=title,
        parent_node_token=parent,
        has_child=has_child,
        source_updated_at=updated_at,
    )


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


@pytest_asyncio.fixture()
async def repository(session):
    repository = FeishuKnowledgeRepository(session)
    await repository.get_or_create_source(
        source_id="source-1",
        name="Engineering Wiki",
        wiki_root_token="root",
        wiki_root_url="https://example.feishu.cn/wiki/root",
        target_kb_id="kb-1",
        credential_env_name="FEISHU_ACCESS_TOKEN",
    )
    return repository


async def _items(session) -> list[FeishuSourceItem]:
    result = await session.execute(select(FeishuSourceItem).order_by(FeishuSourceItem.path_text))
    return list(result.scalars())


async def _version_count(session, item_id: str) -> int:
    return await session.scalar(
        select(func.count()).select_from(FeishuMaterialVersion).where(FeishuMaterialVersion.item_id == item_id)
    )


async def test_scan_recurses_from_configured_root_and_builds_page_paths(repository, session):
    root = node("root", "Root", has_child=True)
    child = node("child", "Child", parent="root", has_child=True)
    grandchild = node("grandchild", "Grandchild", parent="child")
    fake = FakeFeishuClient(
        nodes={item.node_token: item for item in (root, child, grandchild)},
        children={"root": [child], "child": [grandchild]},
        page_contents={"obj-root": b"root", "obj-child": b"child", "obj-grandchild": b"grandchild"},
    )

    result = await FeishuScanService(repository=repository, client=fake).scan(source_id="source-1", mode="full")

    items = await _items(session)
    assert result.status == "succeeded"
    assert result.new_count == 3
    assert fake.get_calls == ["root"]
    assert fake.children_calls == ["root", "child", "grandchild"]
    assert [item.path_text for item in items] == ["Root", "Root / Child", "Root / Child / Grandchild"]


async def test_incremental_scan_requires_a_successful_full_run(repository, session):
    fake = FakeFeishuClient(nodes={"root": node("root", "Root")}, page_contents={"obj-root": b"root"})

    with pytest.raises(ValueError, match="successful full"):
        await FeishuScanService(repository=repository, client=fake).scan(
            source_id="source-1",
            mode="incremental",
        )

    assert await session.scalar(select(func.count()).select_from(FeishuSyncRun)) == 0


async def test_space_scan_recurses_all_top_level_nodes(repository, session):
    root = node("root", "首页", has_child=True)
    sibling = node("sibling", "产品手册", has_child=True)
    child = node("child", "部署文档", parent="sibling")
    fake = FakeFeishuClient(
        nodes={item.node_token: item for item in (root, sibling, child)},
        children={"<top-level>": [root, sibling], "root": [], "sibling": [child], "child": []},
        page_contents={"obj-root": b"root", "obj-sibling": b"sibling", "obj-child": b"child"},
    )
    source = await repository.get_or_create_source(
        source_id="space-source",
        name="Entire Wiki",
        wiki_root_token="root",
        wiki_root_url="https://example.feishu.cn/wiki/root",
        target_kb_id="kb-1",
        credential_env_name="FEISHU_ACCESS_TOKEN",
        scan_scope="space",
    )

    result = await FeishuScanService(repository=repository, client=fake).scan(
        source_id=source.source_id,
        mode="full",
    )

    items = list(
        (
            await session.execute(
                select(FeishuSourceItem).where(FeishuSourceItem.source_id == "space-source").order_by(
                    FeishuSourceItem.path_text
                )
            )
        ).scalars()
    )
    assert result.status == "succeeded"
    assert result.new_count == 3
    assert [item.path_text for item in items] == ["产品手册", "产品手册 / 部署文档", "首页"]
    assert fake.list_nodes_calls == [("space-1", None)]


async def test_page_body_and_attachments_are_independent_source_items(repository, session):
    root = node("root", "Root")
    attachment = FeishuAttachment(file_token="pdf-1", name="Guide.PDF", revision="7", download_type="media")
    fake = FakeFeishuClient(
        nodes={"root": root},
        page_attachments={"obj-root": [attachment]},
        page_contents={"obj-root": b"page body"},
        contents={"pdf-1": b"pdf body"},
    )

    result = await FeishuScanService(repository=repository, client=fake).scan(source_id="source-1", mode="full")

    items = await _items(session)
    assert result.new_count == 2
    assert {(item.item_type, item.title) for item in items} == {("page", "Root"), ("attachment", "Guide.PDF")}
    assert len({item.item_id for item in items}) == 2
    assert fake.download_calls == [("pdf-1", "media")]


async def test_wiki_file_nodes_download_supported_formats_and_skip_media(repository, session):
    root = node("root", "Root", has_child=True)
    names = ["guide.pdf", "slides.pptx", "data.xls", "poster.png", "photo.jpg", "demo.mp4"]
    files = [
        node(
            f"file-{index}",
            name,
            parent="root",
            obj_type="file",
            obj_token=f"drive-file-{index}",
        )
        for index, name in enumerate(names)
    ]
    fake = FakeFeishuClient(
        nodes={item.node_token: item for item in [root, *files]},
        children={"root": files, **{item.node_token: [] for item in files}},
        page_contents={"obj-root": b"page body"},
        contents={
            item.obj_token: item.title.encode()
            for item in files
            if item.title != "demo.mp4" and item.obj_token is not None
        },
    )

    result = await FeishuScanService(repository=repository, client=fake).scan(
        source_id="source-1",
        mode="full",
    )

    items = await _items(session)
    assert result.status == "succeeded"
    assert result.scanned_count == 7
    assert result.new_count == 6
    assert result.unsupported_count == 1
    assert {(item.title, item.item_type) for item in items if item.title != "Root"} == {
        ("guide.pdf", "attachment"),
        ("slides.pptx", "attachment"),
        ("data.xls", "attachment"),
        ("poster.png", "attachment"),
        ("photo.jpg", "attachment"),
        ("demo.mp4", "video"),
    }
    assert fake.page_calls == ["obj-root"]
    assert fake.download_calls == [
        ("drive-file-0", "file"),
        ("drive-file-1", "file"),
        ("drive-file-2", "file"),
        ("drive-file-3", "file"),
        ("drive-file-4", "file"),
    ]


async def test_unknown_wiki_node_is_unsupported_but_its_children_are_scanned(repository, session):
    root = node("root", "Root", has_child=True)
    sheet = node("sheet", "Native Sheet", parent="root", has_child=True, obj_type="sheet")
    child = node("child", "Child", parent="sheet")
    fake = FakeFeishuClient(
        nodes={item.node_token: item for item in (root, sheet, child)},
        children={"root": [sheet], "sheet": [child], "child": []},
        page_contents={"obj-root": b"root", "obj-child": b"child"},
    )

    result = await FeishuScanService(repository=repository, client=fake).scan(
        source_id="source-1",
        mode="full",
    )

    items = {item.title: item for item in await _items(session)}
    assert result.status == "succeeded"
    assert result.scanned_count == 3
    assert result.new_count == 2
    assert result.unsupported_count == 1
    assert items["Native Sheet"].item_type == "unsupported"
    assert items["Child"].path_text == "Root / Native Sheet / Child"


async def test_scan_archives_page_and_attachment_bytes_with_source_metadata(repository, session):
    archived = []

    class ArchiveAdapter:
        async def archive(self, **kwargs):
            archived.append(kwargs)
            extension = ".md" if kwargs["item_type"] == "page" else ".pdf"
            return (
                f"minio://knowledgebases/feishu/{kwargs['source_id']}/{kwargs['item_id']}/"
                f"{kwargs['version_id']}/source{extension}"
            )

    root = node("root", "../../Untrusted title")
    attachment = FeishuAttachment(file_token="pdf-1", name="../../Guide.PDF", revision="7")
    fake = FakeFeishuClient(
        nodes={"root": root},
        page_attachments={"obj-root": [attachment]},
        page_contents={"obj-root": b"# page"},
        contents={"pdf-1": b"%PDF"},
    )

    result = await FeishuScanService(
        repository=repository,
        client=fake,
        archive_adapter=ArchiveAdapter(),
    ).scan(source_id="source-1", mode="full")

    versions = list((await session.execute(select(FeishuMaterialVersion).order_by(FeishuMaterialVersion.id))).scalars())
    assert result.status == "succeeded"
    assert {version.sync_run_id for version in versions} == {result.run_id}
    assert [entry["content"] for entry in archived] == [b"# page", b"%PDF"]
    assert [entry["content_type"] for entry in archived] == ["text/markdown", "application/pdf"]
    assert all(version.source_object_path for version in versions)
    assert [version.processing_params["object_path"] for version in versions] == [
        version.source_object_path for version in versions
    ]
    for version in versions:
        assert version.processing_params["material_version"] == version.version_id
        assert version.processing_params["source_url"] == "https://example.feishu.cn/wiki/root"
        assert version.processing_params["wiki_path"].startswith("../../Untrusted title")
        assert version.processing_params["item_type"] in {"page", "attachment"}
        assert version.processing_params["title"]


async def test_scan_does_not_archive_unsupported_media(repository):
    archived = []

    class ArchiveAdapter:
        async def archive(self, **kwargs):
            archived.append(kwargs)
            return "unexpected"

    root = node("root", "Root")
    audio = FeishuAttachment(file_token="audio-1", name="call.mp3")
    fake = FakeFeishuClient(
        nodes={"root": root},
        page_attachments={"obj-root": [audio]},
        page_contents={"obj-root": b"page"},
    )

    await FeishuScanService(repository=repository, client=fake, archive_adapter=ArchiveAdapter()).scan(
        source_id="source-1", mode="full"
    )

    assert [entry["item_type"] for entry in archived] == ["page"]
    assert fake.download_calls == []


async def test_incremental_scan_creates_version_only_for_changed_item(repository, session):
    root = node("root", "Root")
    pdf = FeishuAttachment(file_token="pdf-1", name="guide.pdf", revision="pdf-1")
    video = FeishuAttachment(file_token="video-1", name="demo.mp4", revision="video-1")
    fake = FakeFeishuClient(
        nodes={"root": root},
        page_attachments={"obj-root": [pdf, video]},
        page_contents={"obj-root": b"page-v1"},
        page_revisions={"obj-root": "page-1"},
        contents={"pdf-1": b"pdf-v1"},
    )
    service = FeishuScanService(repository=repository, client=fake)
    await service.scan(source_id="source-1", mode="full")
    fake.nodes["root"] = replace(root, title="Root renamed")
    fake.page_attachments["obj-root"] = [replace(pdf, revision="pdf-2"), video]
    fake.page_contents["obj-root"] = b"page changed without revision"
    fake.contents["pdf-1"] = b"pdf-v2"
    fake.download_calls.clear()

    result = await service.scan(source_id="source-1", mode="incremental")

    items = {item.title: item for item in await _items(session)}
    assert result.changed_count == 1
    assert result.unchanged_count == 1
    assert result.unsupported_count == 1
    assert fake.download_calls == [("pdf-1", "file")]
    assert await _version_count(session, items["Root renamed"].item_id) == 1
    assert await _version_count(session, items["guide.pdf"].item_id) == 2
    assert await _version_count(session, items["demo.mp4"].item_id) == 1


async def test_version_fallback_prefers_update_time_then_content_hash(repository, session):
    root = node("root", "Root", updated_at="2026-08-13T00:00:00Z")
    without_metadata = FeishuAttachment(file_token="text-1", name="notes.txt", revision=None, source_updated_at=None)
    fake = FakeFeishuClient(
        nodes={"root": root},
        page_attachments={"obj-root": [without_metadata]},
        page_contents={"obj-root": b"page-v1"},
        page_revisions={"obj-root": None},
        contents={"text-1": b"same text"},
    )
    service = FeishuScanService(repository=repository, client=fake)
    await service.scan(source_id="source-1", mode="full")
    fake.page_contents["obj-root"] = b"changed but same timestamp"
    fake.download_calls.clear()

    unchanged = await service.scan(source_id="source-1", mode="incremental")
    assert unchanged.unchanged_count == 2
    assert fake.download_calls == [("text-1", "file")]

    fake.nodes["root"] = replace(root, source_updated_at="2026-08-14T00:00:00Z")
    changed = await service.scan(source_id="source-1", mode="incremental")
    items = {item.title: item for item in await _items(session)}
    assert changed.changed_count == 1
    assert changed.unchanged_count == 1
    assert await _version_count(session, items["Root"].item_id) == 2
    assert await _version_count(session, items["notes.txt"].item_id) == 1


async def test_equivalent_update_time_formats_do_not_create_a_version(repository, session):
    root = node("root", "Root", updated_at="2026-08-13T00:00:00Z")
    fake = FakeFeishuClient(
        nodes={"root": root},
        page_contents={"obj-root": b"page-v1"},
        page_revisions={"obj-root": None},
    )
    service = FeishuScanService(repository=repository, client=fake)
    await service.scan(source_id="source-1", mode="full")
    fake.nodes["root"] = replace(root, source_updated_at="2026-08-13T00:00:00+00:00")
    fake.download_calls.clear()

    result = await service.scan(source_id="source-1", mode="incremental")

    assert result.unchanged_count == 1
    assert fake.download_calls == []


async def test_page_version_uses_docx_revision(repository, session):
    root = node("root", "Root")
    fake = FakeFeishuClient(
        nodes={"root": root},
        page_contents={"obj-root": b"same page"},
        page_revisions={"obj-root": "41"},
    )
    service = FeishuScanService(repository=repository, client=fake)
    await service.scan(source_id="source-1", mode="full")
    fake.page_revisions["obj-root"] = "42"

    result = await service.scan(source_id="source-1", mode="incremental")
    item = (await _items(session))[0]
    versions = list(
        (
            await session.execute(
                select(FeishuMaterialVersion)
                .where(FeishuMaterialVersion.item_id == item.item_id)
                .order_by(FeishuMaterialVersion.id)
            )
        ).scalars()
    )

    assert result.changed_count == 1
    assert [version.revision for version in versions] == ["41", "42"]


async def test_attachment_extensions_and_media_statuses_are_classified(repository, session):
    root = node("root", "Root")
    names = ["a.pdf", "b.docx", "c.pptx", "d.xlsx", "e.txt", "f.JPEG", "g.mp3", "h.mov"]
    attachments = [FeishuAttachment(file_token=f"file-{index}", name=name) for index, name in enumerate(names)]
    contents = {"obj-root": b"page"} | {
        item.file_token: item.name.encode()
        for item in attachments
        if item.name.lower().endswith(("pdf", "docx", "pptx", "xlsx", "txt", "jpeg"))
    }
    fake = FakeFeishuClient(
        nodes={"root": root},
        page_attachments={"obj-root": attachments},
        page_contents={"obj-root": b"page"},
        contents={key: value for key, value in contents.items() if key != "obj-root"},
    )

    result = await FeishuScanService(repository=repository, client=fake).scan(source_id="source-1", mode="full")

    rows = (
        await session.execute(
            select(FeishuSourceItem.title, FeishuSourceItem.item_type, FeishuMaterialVersion.processing_status)
            .join(FeishuMaterialVersion, FeishuMaterialVersion.item_id == FeishuSourceItem.item_id)
            .where(FeishuSourceItem.item_type != "page")
        )
    ).all()
    assert result.status == "succeeded"
    assert result.new_count == 7
    assert result.unsupported_count == 2
    assert {(item_type, status) for _, item_type, status in rows} == {
        ("attachment", "discovered"),
        ("audio", "unsupported"),
        ("video", "unsupported"),
    }
    assert "file-6" not in fake.download_calls
    assert "file-7" not in fake.download_calls


@pytest.mark.parametrize(
    ("error_at", "contents"),
    [
        (("get", "root"), {}),
        (("page", "obj-root"), {}),
        (("children", "root"), {"obj-root": b"page"}),
    ],
)
async def test_partial_or_permission_error_never_invalidates_unseen_items(repository, session, error_at, contents):
    stale, _ = await repository.upsert_source_item(
        source_id="source-1",
        item_key="attachment:stale",
        item_type="attachment",
        title="stale.pdf",
        parent_item_key="page:space-1:root",
        path_text="Root / stale.pdf",
        source_url=None,
        source_updated_at=None,
    )
    root = node("root", "Root")
    fake = FakeFeishuClient(nodes={"root": root}, page_contents={"obj-root": b"page"}, error_at=error_at)

    result = await FeishuScanService(repository=repository, client=fake).scan(source_id="source-1", mode="full")
    await session.refresh(stale)
    run = await session.scalar(select(FeishuSyncRun).order_by(FeishuSyncRun.id.desc()).limit(1))
    failure_events = list(
        (
            await session.execute(
                select(FeishuProcessingEvent).where(FeishuProcessingEvent.event_type == "scan_failed")
            )
        ).scalars()
    )

    assert result.status == "failed"
    assert result.failed_count == 1
    assert result.invalidated_count == 0
    assert stale.source_validity == "valid"
    assert run.status == "failed"
    assert run.invalidated_count == 0
    assert len(failure_events) == 1
    assert (failure_events[0].from_status, failure_events[0].to_status) == ("running", "failed")
    assert failure_events[0].message == run.error_summary == result.error_summary
    assert failure_events[0].payload_json == {"run_id": run.run_id}


async def test_complete_scan_invalidates_unseen_item_without_changing_active_version(repository, session):
    stale, _ = await repository.upsert_source_item(
        source_id="source-1",
        item_key="attachment:stale",
        item_type="attachment",
        title="stale.pdf",
        parent_item_key="page:space-1:root",
        path_text="Root / stale.pdf",
        source_url=None,
        source_updated_at=None,
    )
    stale.active_version_id = "published-version"
    await session.flush()
    root = node("root", "Root")
    fake = FakeFeishuClient(nodes={"root": root}, page_contents={"obj-root": b"page"})

    result = await FeishuScanService(repository=repository, client=fake).scan(source_id="source-1", mode="full")
    await session.refresh(stale)

    assert result.status == "succeeded"
    assert result.invalidated_count == 1
    assert stale.source_validity == "invalid"
    assert stale.active_version_id == "published-version"


@pytest.mark.parametrize("probe_result", ["permission", "still-readable"])
async def test_filtered_child_page_never_invalidates_existing_subtree(repository, session, probe_result):
    root = node("root", "Root", has_child=True)
    child = node("child", "Child", parent="root")
    attachment = FeishuAttachment(file_token="child-file", name="child.pdf")
    fake = FakeFeishuClient(
        nodes={"root": root, "child": child},
        children={"root": [child]},
        page_attachments={"obj-child": [attachment]},
        page_contents={"obj-root": b"root", "obj-child": b"child"},
        contents={"child-file": b"attachment"},
    )
    service = FeishuScanService(repository=repository, client=fake)
    await service.scan(source_id="source-1", mode="full")
    fake.children["root"] = []
    if probe_result == "permission":
        fake.error_at = ("get", "child")

    result = await service.scan(source_id="source-1", mode="full")
    items = {item.title: item for item in await _items(session)}

    assert result.status == "failed"
    assert result.invalidated_count == 0
    assert items["Child"].source_validity == "valid"
    assert items["child.pdf"].source_validity == "valid"


async def test_not_found_child_page_allows_subtree_invalidation(repository, session):
    root = node("root", "Root", has_child=True)
    child = node("child", "Child", parent="root")
    attachment = FeishuAttachment(file_token="child-file", name="child.pdf")
    fake = FakeFeishuClient(
        nodes={"root": root, "child": child},
        children={"root": [child]},
        page_attachments={"obj-child": [attachment]},
        page_contents={"obj-root": b"root", "obj-child": b"child"},
        contents={"child-file": b"attachment"},
    )
    service = FeishuScanService(repository=repository, client=fake)
    await service.scan(source_id="source-1", mode="full")
    fake.children["root"] = []
    fake.not_found_tokens.add("child")

    result = await service.scan(source_id="source-1", mode="full")
    items = {item.title: item for item in await _items(session)}

    assert result.status == "succeeded"
    assert result.invalidated_count == 2
    assert items["Child"].source_validity == "invalid"
    assert items["child.pdf"].source_validity == "invalid"


async def test_child_page_moved_to_drive_allows_subtree_invalidation(repository, session):
    root = node("root", "Root", has_child=True)
    child = node("child", "Child", parent="root")
    attachment = FeishuAttachment(file_token="child-file", name="child.pdf")
    fake = FakeFeishuClient(
        nodes={"root": root, "child": child},
        children={"root": [child]},
        page_attachments={"obj-child": [attachment]},
        page_contents={"obj-root": b"root", "obj-child": b"child"},
        contents={"child-file": b"attachment"},
    )
    service = FeishuScanService(repository=repository, client=fake)
    await service.scan(source_id="source-1", mode="full")
    fake.children["root"] = []
    fake.nodes["child"] = replace(child, space_id="", parent_node_token="drive-folder")

    result = await service.scan(source_id="source-1", mode="full")
    items = {item.title: item for item in await _items(session)}

    assert result.status == "succeeded"
    assert result.invalidated_count == 2
    assert items["Child"].source_validity == "invalid"
    assert items["child.pdf"].source_validity == "invalid"


async def test_child_page_moved_outside_configured_root_allows_subtree_invalidation(repository, session):
    root = node("root", "Root", has_child=True)
    child = node("child", "Child", parent="root")
    outside = node("outside", "Outside")
    fake = FakeFeishuClient(
        nodes={"root": root, "child": child, "outside": outside},
        children={"root": [child]},
        page_contents={"obj-root": b"root", "obj-child": b"child"},
    )
    service = FeishuScanService(repository=repository, client=fake)
    await service.scan(source_id="source-1", mode="full")
    fake.children["root"] = []
    fake.nodes["child"] = replace(child, parent_node_token="outside")

    result = await service.scan(source_id="source-1", mode="full")
    items = {item.title: item for item in await _items(session)}

    assert result.status == "succeeded"
    assert result.invalidated_count == 1
    assert items["Child"].source_validity == "invalid"


async def test_unreadable_parent_never_invalidates_existing_subtree(repository, session):
    root = node("root", "Root", has_child=True)
    child = node("child", "Child", parent="root")
    outside = node("outside", "Outside")
    fake = FakeFeishuClient(
        nodes={"root": root, "child": child, "outside": outside},
        children={"root": [child]},
        page_contents={"obj-root": b"root", "obj-child": b"child"},
    )
    service = FeishuScanService(repository=repository, client=fake)
    await service.scan(source_id="source-1", mode="full")
    fake.children["root"] = []
    fake.nodes["child"] = replace(child, parent_node_token="outside")
    fake.error_at = ("get", "outside")

    result = await service.scan(source_id="source-1", mode="full")
    items = {item.title: item for item in await _items(session)}

    assert result.status == "failed"
    assert result.invalidated_count == 0
    assert items["Child"].source_validity == "valid"


async def test_parent_cycle_never_invalidates_existing_subtree(repository, session):
    root = node("root", "Root", has_child=True)
    child = node("child", "Child", parent="root")
    outside = node("outside", "Outside", parent="child")
    fake = FakeFeishuClient(
        nodes={"root": root, "child": child, "outside": outside},
        children={"root": [child]},
        page_contents={"obj-root": b"root", "obj-child": b"child"},
    )
    service = FeishuScanService(repository=repository, client=fake)
    await service.scan(source_id="source-1", mode="full")
    fake.children["root"] = []
    fake.nodes["child"] = replace(child, parent_node_token="outside")

    result = await service.scan(source_id="source-1", mode="full")
    items = {item.title: item for item in await _items(session)}

    assert result.status == "failed"
    assert result.invalidated_count == 0
    assert items["Child"].source_validity == "valid"


async def test_invalidation_rolls_back_when_successful_run_update_loses_a_race(repository, session, monkeypatch):
    stale, _ = await repository.upsert_source_item(
        source_id="source-1",
        item_key="attachment:stale",
        item_type="attachment",
        title="stale.pdf",
        parent_item_key="page:space-1:root",
        path_text="Root / stale.pdf",
        source_url=None,
        source_updated_at=None,
    )
    root = node("root", "Root")
    fake = FakeFeishuClient(nodes={"root": root}, page_contents={"obj-root": b"page"})
    finish_sync_run = repository._finish_sync_run

    async def reject_successful_finish(**values):
        if values["status"] == "succeeded":
            return False
        return await finish_sync_run(**values)

    monkeypatch.setattr(repository, "_finish_sync_run", reject_successful_finish)

    result = await FeishuScanService(repository=repository, client=fake).scan(source_id="source-1", mode="full")
    await session.refresh(stale)

    assert result.status == "failed"
    assert result.invalidated_count == 0
    assert stale.source_validity == "valid"
