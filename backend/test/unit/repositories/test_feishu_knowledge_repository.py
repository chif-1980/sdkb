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
    FeishuSyncRun,
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
    assert "updated_at =" in sql


async def test_start_sync_run_rejects_a_second_running_scan(repository):
    run = await repository.start_sync_run(source_id="source-1", run_type="full", operator_id="admin-1")

    with pytest.raises(ConcurrentSyncRunError):
        await repository.start_sync_run(source_id="source-1", run_type="incremental", operator_id="admin-2")

    assert run.status == "running"


async def test_fail_sync_run_persists_terminal_state_and_event(repository, session):
    run = FeishuSyncRun(
        run_id="queued-run",
        source_id="source-1",
        run_type="full",
        status="queued",
        operator_id="admin-1",
    )
    session.add(run)
    await session.commit()

    updated = await repository.fail_sync_run(
        run_id=run.run_id,
        source_id="source-1",
        error_summary="RuntimeError: credential unavailable",
        operator_id="admin-1",
    )
    await session.commit()

    event = (await session.execute(select(FeishuProcessingEvent))).scalar_one()
    assert updated is True
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.failed_count == 1
    assert run.error_summary == "RuntimeError: credential unavailable"
    assert (event.event_type, event.from_status, event.to_status) == ("scan_failed", "queued", "failed")
    assert event.message == run.error_summary
    assert event.payload_json == {"run_id": run.run_id}


async def test_claim_queued_sync_run_rejects_same_source_and_allows_different_source(repository, session):
    await repository.get_or_create_source(
        source_id="source-2",
        name="Product Wiki",
        wiki_root_token="root-2",
        wiki_root_url=None,
        target_kb_id="kb-2",
        credential_env_name="FEISHU_ACCESS_TOKEN",
    )
    runs = [
        FeishuSyncRun(run_id="source-1-first", source_id="source-1", run_type="full", status="queued"),
        FeishuSyncRun(run_id="source-1-second", source_id="source-1", run_type="full", status="queued"),
        FeishuSyncRun(run_id="source-2-first", source_id="source-2", run_type="full", status="queued"),
    ]
    session.add_all(runs)
    await session.commit()

    first = await repository.claim_queued_sync_run(
        run_id="source-1-first",
        source_id="source-1",
        run_type="full",
        operator_id="admin-1",
    )
    with pytest.raises(ConcurrentSyncRunError):
        await repository.claim_queued_sync_run(
            run_id="source-1-second",
            source_id="source-1",
            run_type="full",
            operator_id="admin-2",
        )
    other_source = await repository.claim_queued_sync_run(
        run_id="source-2-first",
        source_id="source-2",
        run_type="full",
        operator_id="admin-2",
    )

    assert first.status == "running"
    assert runs[1].status == "queued"
    assert other_source.status == "running"


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


async def test_queue_archived_versions_for_processing_is_atomic_and_filters_unsupported(repository, session):
    page, _ = await repository.upsert_source_item(
        source_id="source-1",
        item_key="page:space-1:root",
        item_type="page",
        title="Root",
        parent_item_key=None,
        path_text="Root",
        source_url="https://example.feishu.cn/wiki/root",
        source_updated_at=None,
    )
    attachment, _ = await repository.upsert_source_item(
        source_id="source-1",
        item_key="attachment:file-1",
        item_type="attachment",
        title="Guide.pdf",
        parent_item_key=page.item_key,
        path_text="Root / Guide.pdf",
        source_url=None,
        source_updated_at=None,
    )
    audio, _ = await repository.upsert_source_item(
        source_id="source-1",
        item_key="audio:file-2",
        item_type="audio",
        title="Call.mp3",
        parent_item_key=page.item_key,
        path_text="Root / Call.mp3",
        source_url=None,
        source_updated_at=None,
    )
    page_version, _ = await repository.create_material_version(
        item_id=page.item_id,
        revision="page-1",
        content_hash="page-hash",
        processing_status="discovered",
        processing_params={},
    )
    attachment_version, _ = await repository.create_material_version(
        item_id=attachment.item_id,
        revision="attachment-1",
        content_hash="attachment-hash",
        processing_status="discovered",
        processing_params={},
    )
    audio_version, _ = await repository.create_material_version(
        item_id=audio.item_id,
        revision="audio-1",
        content_hash="audio-hash",
        processing_status="discovered",
        processing_params={},
    )
    page_version.source_object_path = "minio://knowledgebases/page.md"
    audio_version.source_object_path = "minio://knowledgebases/call.mp3"
    await session.commit()

    claimed = await repository.queue_archived_versions_for_processing(
        source_id="source-1",
        operator_id="admin",
    )
    claimed_again = await repository.queue_archived_versions_for_processing(
        source_id="source-1",
        operator_id="admin",
    )

    assert claimed == [page_version.version_id]
    assert claimed_again == []
    assert page_version.processing_status == "processing_queued"
    assert attachment_version.processing_status == "discovered"
    assert audio_version.processing_status == "discovered"
    events = list((await session.execute(select(FeishuProcessingEvent))).scalars())
    assert [(event.version_id, event.event_type, event.from_status, event.to_status) for event in events] == [
        (page_version.version_id, "processing_queued", "discovered", "processing_queued")
    ]


async def test_startup_reconciliation_recovers_interrupted_states_once(repository, session):
    transitions = [
        ("processing-queued", "processing_queued", "parse_failed"),
        ("processing", "processing", "parse_failed"),
        ("publish-queued", "publish_queued", "publish_failed"),
        ("publishing", "publishing", "publish_failed"),
        ("removal", "removal_pending", "removal_failed"),
    ]
    versions = []
    for index, (name, status, _) in enumerate(transitions):
        item = FeishuSourceItem(
            item_id=f"item-{name}",
            source_id="source-1",
            item_key=f"page:space-1:{name}",
            item_type="page",
            title=name,
            source_validity="valid",
            active_version_id=f"version-{name}" if status == "removal_pending" else None,
        )
        version = FeishuMaterialVersion(
            version_id=f"version-{name}",
            item_id=item.item_id,
            revision=str(index),
            content_hash=f"hash-{name}",
            processing_status=status,
        )
        session.add_all([item, version])
        versions.append(version)
    runs = [
        FeishuSyncRun(run_id="run-queued", source_id="source-1", run_type="full", status="queued"),
        FeishuSyncRun(run_id="run-running", source_id="source-1", run_type="incremental", status="running"),
        FeishuSyncRun(run_id="run-succeeded", source_id="source-1", run_type="full", status="succeeded"),
    ]
    session.add_all(runs)
    await session.commit()

    first = await repository.reconcile_interrupted_work()
    event_count = len(list((await session.execute(select(FeishuProcessingEvent))).scalars()))
    second = await repository.reconcile_interrupted_work()

    assert first == {"sync_runs": 2, "material_versions": 5}
    assert second == {"sync_runs": 0, "material_versions": 0}
    assert [(version.version_id, version.processing_status) for version in versions] == [
        (f"version-{name}", expected) for name, _, expected in transitions
    ]
    assert runs[0].status == runs[1].status == "failed"
    assert runs[0].finished_at is not None and runs[1].finished_at is not None
    assert runs[2].status == "succeeded"
    removal_item = await session.get(FeishuSourceItem, 5)
    assert removal_item.active_version_id == "version-removal"
    events = list((await session.execute(select(FeishuProcessingEvent))).scalars())
    assert len(events) == event_count == 7
    assert {event.event_type for event in events} == {"startup_reconciled"}
    assert {(event.from_status, event.to_status) for event in events} == {
        ("queued", "failed"),
        ("running", "failed"),
        ("processing_queued", "parse_failed"),
        ("processing", "parse_failed"),
        ("publish_queued", "publish_failed"),
        ("publishing", "publish_failed"),
        ("removal_pending", "removal_failed"),
    }


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
