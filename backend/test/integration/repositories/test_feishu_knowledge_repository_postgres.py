from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.feishu_knowledge_repository import (
    ConcurrentSyncRunError,
    FeishuKnowledgeRepository,
)
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import FeishuSource, FeishuSyncRun

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def postgres_source_context():
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        pytest.skip("POSTGRES_URL is not configured for the PostgreSQL repository integration tests.")
    engine = create_async_engine(postgres_url)
    schema_name = f"task3_upsert_{uuid4().hex}"
    schema_engine = engine.execution_options(schema_translate_map={None: schema_name})
    factory = async_sessionmaker(schema_engine, expire_on_commit=False)
    source_id = f"task3-upsert-{uuid4().hex}"
    schema_created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
        # Build the table from the real model so this fixture cannot drift from the
        # columns the repository actually reads and writes.
        async with schema_engine.begin() as connection:
            await connection.run_sync(lambda c: FeishuSource.__table__.create(c))
        async with factory() as session:
            yield session, factory, source_id
    finally:
        if schema_created:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await engine.dispose()


@pytest_asyncio.fixture()
async def postgres_mutex_context():
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        pytest.skip("POSTGRES_URL is not configured for the PostgreSQL repository integration tests.")
    engine = create_async_engine(postgres_url, pool_size=2, max_overflow=0)
    schema_name = f"task4_mutex_{uuid4().hex}"
    schema_engine = engine.execution_options(schema_translate_map={None: schema_name})
    factory = async_sessionmaker(schema_engine, expire_on_commit=False)
    schema_created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
        # Same reason as above: model-derived DDL keeps the fixture in step with
        # scan_scope / impact_summary and any future column.
        async with schema_engine.begin() as connection:
            await connection.run_sync(
                lambda c: Base.metadata.create_all(c, tables=[FeishuSource.__table__, FeishuSyncRun.__table__])
            )
        async with factory() as session:
            # 用 ORM 插入，才能应用模型上只有 Python 端默认值的列
            # （enabled / scan_scope / impact_summary）。
            session.add_all(
                [
                    FeishuSource(
                        source_id="source-1",
                        name="Source",
                        wiki_root_token="root",
                        target_kb_id="kb-1",
                        credential_env_name="FEISHU_ACCESS_TOKEN",
                    ),
                    FeishuSource(
                        source_id="source-2",
                        name="Other Source",
                        wiki_root_token="other-root",
                        target_kb_id="kb-2",
                        credential_env_name="FEISHU_ACCESS_TOKEN",
                    ),
                    FeishuSyncRun(run_id="run-first", source_id="source-1", run_type="full", status="queued"),
                    FeishuSyncRun(run_id="run-second", source_id="source-1", run_type="full", status="queued"),
                    FeishuSyncRun(run_id="run-other", source_id="source-2", run_type="full", status="queued"),
                ]
            )
            await session.commit()
        yield factory, schema_name
    finally:
        if schema_created:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await engine.dispose()


async def test_postgres_source_upsert_refreshes_source_loaded_in_same_session(postgres_source_context):
    session, factory, source_id = postgres_source_context
    repository = FeishuKnowledgeRepository(session)
    created = await repository.get_or_create_source(
        source_id=source_id,
        name="Engineering Wiki",
        wiki_root_token="old-root",
        wiki_root_url="https://example.feishu.cn/wiki/old-root",
        target_kb_id="kb-old",
        credential_env_name="FEISHU_ACCESS_TOKEN",
        enabled=True,
    )
    loaded = await repository.get_source(source_id)

    updated = await repository.get_or_create_source(
        source_id=source_id,
        name="Updated Wiki",
        wiki_root_token="new-root",
        wiki_root_url="https://example.feishu.cn/wiki/new-root",
        target_kb_id="kb-new",
        credential_env_name="FEISHU_ACCESS_TOKEN",
        enabled=False,
    )

    async with factory() as verification_session:
        persisted = (
            await verification_session.execute(select(FeishuSource).where(FeishuSource.source_id == source_id))
        ).scalar_one()
    assert persisted.name == "Updated Wiki"
    assert persisted.wiki_root_token == "new-root"
    assert persisted.wiki_root_url == "https://example.feishu.cn/wiki/new-root"
    assert persisted.target_kb_id == "kb-new"
    assert persisted.enabled is False
    assert loaded is created
    assert updated is loaded
    assert updated.name == "Updated Wiki"
    assert updated.wiki_root_token == "new-root"
    assert updated.wiki_root_url == "https://example.feishu.cn/wiki/new-root"
    assert updated.target_kb_id == "kb-new"
    assert updated.enabled is False


async def test_postgres_source_upsert_advances_persisted_updated_at(postgres_source_context):
    session, factory, source_id = postgres_source_context
    repository = FeishuKnowledgeRepository(session)
    source = await repository.get_or_create_source(
        source_id=source_id,
        name="Engineering Wiki",
        wiki_root_token="old-root",
        wiki_root_url=None,
        target_kb_id="kb-old",
        credential_env_name="FEISHU_ACCESS_TOKEN",
    )
    previous_updated_at = source.updated_at
    await asyncio.sleep(0.01)

    await repository.get_or_create_source(
        source_id=source_id,
        name="Updated Wiki",
        wiki_root_token="new-root",
        wiki_root_url=None,
        target_kb_id="kb-new",
        credential_env_name="FEISHU_ACCESS_TOKEN",
        enabled=False,
    )

    async with factory() as verification_session:
        persisted_updated_at = await verification_session.scalar(
            select(FeishuSource.updated_at).where(FeishuSource.source_id == source_id)
        )
    assert persisted_updated_at is not None
    assert previous_updated_at is not None
    assert persisted_updated_at > previous_updated_at


async def test_postgres_claim_queued_run_serializes_same_source(postgres_mutex_context):
    factory, schema_name = postgres_mutex_context
    first_claimed = asyncio.Event()
    release_first = asyncio.Event()

    async with factory() as first_session, factory() as second_session:

        async def claim_first():
            async with first_session.begin():
                run = await FeishuKnowledgeRepository(first_session).claim_queued_sync_run(
                    run_id="run-first",
                    source_id="source-1",
                    run_type="full",
                    operator_id="admin-1",
                )
                first_claimed.set()
                await release_first.wait()
                return run.run_id

        async def claim_second():
            await first_claimed.wait()
            async with second_session.begin():
                return await FeishuKnowledgeRepository(second_session).claim_queued_sync_run(
                    run_id="run-second",
                    source_id="source-1",
                    run_type="full",
                    operator_id="admin-2",
                )

        first_task = asyncio.create_task(claim_first())
        first_claim_waiter = asyncio.create_task(first_claimed.wait())
        second_task = None
        try:
            done, _ = await asyncio.wait(
                {first_task, first_claim_waiter},
                timeout=2,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if first_task in done:
                await first_task
            assert first_claim_waiter in done, "First PostgreSQL claim did not acquire the source lock"

            second_task = asyncio.create_task(claim_second())
            await asyncio.sleep(0.05)
            assert second_task.done() is False

            release_first.set()
            assert await asyncio.wait_for(first_task, timeout=2) == "run-first"
            with pytest.raises(ConcurrentSyncRunError):
                await asyncio.wait_for(second_task, timeout=2)
        finally:
            release_first.set()
            first_claim_waiter.cancel()
            await asyncio.gather(
                first_task,
                *([second_task] if second_task is not None else []),
                return_exceptions=True,
            )


async def test_postgres_claim_queued_run_does_not_block_different_source(postgres_mutex_context):
    factory, _ = postgres_mutex_context
    first_claimed = asyncio.Event()
    release_first = asyncio.Event()

    async with factory() as first_session, factory() as other_session:

        async def claim_first():
            async with first_session.begin():
                run = await FeishuKnowledgeRepository(first_session).claim_queued_sync_run(
                    run_id="run-first",
                    source_id="source-1",
                    run_type="full",
                    operator_id="admin-1",
                )
                first_claimed.set()
                await release_first.wait()
                return run.run_id

        first_task = asyncio.create_task(claim_first())
        other_task = None
        try:
            await asyncio.wait_for(first_claimed.wait(), timeout=2)
            other_task = asyncio.create_task(
                FeishuKnowledgeRepository(other_session).claim_queued_sync_run(
                    run_id="run-other",
                    source_id="source-2",
                    run_type="full",
                    operator_id="admin-2",
                )
            )

            other = await asyncio.wait_for(other_task, timeout=2)
            assert other.run_id == "run-other"
            assert first_task.done() is False

            release_first.set()
            assert await asyncio.wait_for(first_task, timeout=2) == "run-first"
        finally:
            release_first.set()
            await asyncio.gather(
                first_task,
                *([other_task] if other_task is not None else []),
                return_exceptions=True,
            )
