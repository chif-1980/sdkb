from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.feishu_knowledge_repository import (
    ConcurrentSyncRunError,
    FeishuKnowledgeRepository,
)
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuSourceItem,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


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


async def test_get_or_create_source_updates_configuration_without_storing_credentials(repository):
    source = await repository.get_or_create_source(
        source_id="source-1",
        name="Updated Wiki",
        wiki_root_token="new-root",
        wiki_root_url=None,
        target_kb_id="kb-2",
        credential_env_name="FEISHU_TOKEN_NAME",
    )

    assert source.name == "Updated Wiki"
    assert source.wiki_root_token == "new-root"
    assert source.target_kb_id == "kb-2"
    assert source.credential_env_name == "FEISHU_TOKEN_NAME"
    assert not hasattr(source, "credential")


async def test_postgres_source_upsert_is_atomic(repository):
    statement = repository._build_postgres_source_upsert(
        source_id="source-1",
        name="Engineering Wiki",
        wiki_root_token="root",
        wiki_root_url="https://example.feishu.cn/wiki/root",
        target_kb_id="kb-1",
        credential_env_name="FEISHU_ACCESS_TOKEN",
        enabled=True,
        created_by="admin-1",
    )

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "INSERT INTO feishu_sources" in sql
    assert "ON CONFLICT (source_id) DO UPDATE" in sql
    for column in ("name", "wiki_root_token", "wiki_root_url", "target_kb_id", "credential_env_name", "enabled"):
        assert f"{column} = excluded.{column}" in sql


async def test_start_sync_run_rejects_a_second_running_scan(repository):
    run = await repository.start_sync_run(source_id="source-1", run_type="full", operator_id="admin-1")

    with pytest.raises(ConcurrentSyncRunError):
        await repository.start_sync_run(source_id="source-1", run_type="incremental", operator_id="admin-2")

    assert run.status == "running"


async def test_successful_full_scan_is_the_only_incremental_prerequisite(repository):
    failed_full = await repository.start_sync_run(source_id="source-1", run_type="full")
    await repository.finish_sync_run(
        run_id=failed_full.run_id,
        status="failed",
        scanned_count=0,
        new_count=0,
        changed_count=0,
        unchanged_count=0,
        unsupported_count=0,
        failed_count=1,
        invalidated_count=0,
    )
    succeeded_incremental = await repository.start_sync_run(source_id="source-1", run_type="incremental")
    await repository.finish_sync_run(
        run_id=succeeded_incremental.run_id,
        status="succeeded",
        scanned_count=0,
        new_count=0,
        changed_count=0,
        unchanged_count=0,
        unsupported_count=0,
        failed_count=0,
        invalidated_count=0,
    )
    running_full = await repository.start_sync_run(source_id="source-1", run_type="full")

    assert await repository.has_successful_full_scan("source-1") is False

    await repository.finish_sync_run(
        run_id=running_full.run_id,
        status="succeeded",
        scanned_count=0,
        new_count=0,
        changed_count=0,
        unchanged_count=0,
        unsupported_count=0,
        failed_count=0,
        invalidated_count=0,
    )
    assert await repository.has_successful_full_scan("source-1") is True


async def test_upsert_and_version_methods_preserve_active_version(repository):
    first_seen = datetime(2026, 8, 13, 1, tzinfo=UTC)
    item, created = await repository.upsert_source_item(
        source_id="source-1",
        item_key="page:space-1:root",
        item_type="page",
        title="Old title",
        parent_item_key=None,
        path_text="Old title",
        source_url="https://example.feishu.cn/wiki/root",
        source_updated_at=datetime(2026, 8, 12, tzinfo=UTC),
        seen_at=first_seen,
    )
    item.active_version_id = "published-version"
    await repository.session.flush()
    version, version_created = await repository.create_material_version(
        item_id=item.item_id,
        revision="revision-1",
        content_hash="hash-1",
        processing_status="discovered",
        processing_params={"source_updated_at": "2026-08-12T00:00:00Z"},
    )
    duplicate, duplicate_created = await repository.create_material_version(
        item_id=item.item_id,
        revision="revision-1",
        content_hash="hash-1",
        processing_status="discovered",
        processing_params={"source_updated_at": "2026-08-12T00:00:00Z"},
    )

    updated, updated_created = await repository.upsert_source_item(
        source_id="source-1",
        item_key="page:space-1:root",
        item_type="page",
        title="New title",
        parent_item_key=None,
        path_text="New title",
        source_url="https://example.feishu.cn/wiki/root",
        source_updated_at=datetime(2026, 8, 13, tzinfo=UTC),
        seen_at=first_seen + timedelta(days=1),
    )

    assert created is True
    assert updated_created is False
    assert updated.item_id == item.item_id
    assert updated.title == "New title"
    assert updated.active_version_id == "published-version"
    assert version_created is True
    assert duplicate_created is False
    assert duplicate.version_id == version.version_id
    assert await repository.find_current_version(item.item_id) == version


async def test_seen_and_invalidation_updates_are_scoped_and_preserve_active_version(repository, session):
    seen_at = datetime(2026, 8, 13, 2, tzinfo=UTC)
    seen, _ = await repository.upsert_source_item(
        source_id="source-1",
        item_key="attachment:seen",
        item_type="attachment",
        title="seen.pdf",
        parent_item_key="page:space-1:root",
        path_text="Root / seen.pdf",
        source_url=None,
        source_updated_at=None,
        seen_at=seen_at - timedelta(days=1),
    )
    unseen, _ = await repository.upsert_source_item(
        source_id="source-1",
        item_key="attachment:unseen",
        item_type="attachment",
        title="unseen.pdf",
        parent_item_key="page:space-1:root",
        path_text="Root / unseen.pdf",
        source_url=None,
        source_updated_at=None,
        seen_at=seen_at - timedelta(days=1),
    )
    unseen.active_version_id = "published-version"
    await session.flush()

    marked_seen = await repository.mark_seen_items(source_id="source-1", item_keys={seen.item_key}, seen_at=seen_at)
    invalidated = await repository.mark_source_invalid(source_id="source-1", seen_item_keys={seen.item_key})
    await session.refresh(seen)
    await session.refresh(unseen)

    assert marked_seen == 1
    assert invalidated == 1
    assert seen.source_validity == "valid"
    assert seen.last_seen_at.replace(tzinfo=UTC) == seen_at
    assert unseen.source_validity == "invalid"
    assert unseen.active_version_id == "published-version"


async def test_event_summary_and_terminal_run_update(repository, session):
    item, _ = await repository.upsert_source_item(
        source_id="source-1",
        item_key="video:file-1",
        item_type="video",
        title="demo.mp4",
        parent_item_key="page:space-1:root",
        path_text="Root / demo.mp4",
        source_url=None,
        source_updated_at=None,
        seen_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    version, _ = await repository.create_material_version(
        item_id=item.item_id,
        revision="revision-1",
        content_hash="hash-1",
        processing_status="unsupported",
        processing_params={},
    )
    event = await repository.append_event(
        source_id="source-1",
        item_id=item.item_id,
        version_id=version.version_id,
        event_type="material_discovered",
        to_status="unsupported",
        payload_json={"format": "video"},
    )
    run = await repository.start_sync_run(source_id="source-1", run_type="incremental")
    finished = await repository.finish_sync_run(
        run_id=run.run_id,
        status="succeeded",
        scanned_count=1,
        new_count=0,
        changed_count=0,
        unchanged_count=0,
        unsupported_count=1,
        failed_count=0,
        invalidated_count=0,
    )
    stale_update = await repository.finish_sync_run(
        run_id=run.run_id,
        status="failed",
        scanned_count=0,
        new_count=0,
        changed_count=0,
        unchanged_count=0,
        unsupported_count=0,
        failed_count=1,
        invalidated_count=0,
    )
    summary = await repository.get_source_summary("source-1")
    events = list((await session.execute(select(FeishuProcessingEvent))).scalars())
    versions = list((await session.execute(select(FeishuMaterialVersion))).scalars())
    items = list((await session.execute(select(FeishuSourceItem))).scalars())

    assert event in events
    assert finished is True
    assert stale_update is False
    assert run.status == "succeeded"
    assert len(items) == len(versions) == 1
    assert summary.total_count == 1
    assert summary.valid_count == 1
    assert summary.invalid_count == 0
    assert summary.unsupported_count == 1
