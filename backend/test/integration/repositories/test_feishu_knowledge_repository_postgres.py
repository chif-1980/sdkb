from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from yuxi.repositories.feishu_knowledge_repository import (
    ConcurrentSyncRunError,
    FeishuKnowledgeRepository,
)
from yuxi.storage.postgres.models_knowledge import FeishuSource

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def postgres_source_context():
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        pytest.skip("POSTGRES_URL is not configured for the PostgreSQL repository integration tests.")
    engine = create_async_engine(postgres_url, poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    source_id = f"task3-upsert-{uuid4().hex}"
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TEMPORARY TABLE feishu_sources (
                        id SERIAL PRIMARY KEY,
                        source_id VARCHAR(64) NOT NULL UNIQUE,
                        name VARCHAR(255) NOT NULL,
                        wiki_root_token VARCHAR(255) NOT NULL,
                        wiki_root_url VARCHAR(1024),
                        target_kb_id VARCHAR(80) NOT NULL,
                        credential_env_name VARCHAR(255) NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        created_by VARCHAR(64),
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    ) ON COMMIT PRESERVE ROWS
                    """
                )
            )
        async with factory() as session:
            yield session, factory, source_id
    finally:
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
            await connection.execute(text(f'SET LOCAL search_path TO "{schema_name}"'))
            await connection.execute(
                text(
                    """
                    CREATE TABLE feishu_sources (
                        id SERIAL PRIMARY KEY,
                        source_id VARCHAR(64) NOT NULL UNIQUE,
                        name VARCHAR(255) NOT NULL,
                        wiki_root_token VARCHAR(255) NOT NULL,
                        wiki_root_url VARCHAR(1024),
                        target_kb_id VARCHAR(80) NOT NULL,
                        credential_env_name VARCHAR(255) NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        created_by VARCHAR(64),
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE feishu_sync_runs (
                        id SERIAL PRIMARY KEY,
                        run_id VARCHAR(64) NOT NULL UNIQUE,
                        source_id VARCHAR(64) NOT NULL REFERENCES feishu_sources(source_id) ON DELETE CASCADE,
                        run_type VARCHAR(32) NOT NULL,
                        status VARCHAR(32) NOT NULL DEFAULT 'running',
                        started_at TIMESTAMPTZ DEFAULT NOW(),
                        finished_at TIMESTAMPTZ,
                        operator_id VARCHAR(64),
                        scanned_count INTEGER DEFAULT 0,
                        new_count INTEGER DEFAULT 0,
                        changed_count INTEGER DEFAULT 0,
                        unchanged_count INTEGER DEFAULT 0,
                        unsupported_count INTEGER DEFAULT 0,
                        failed_count INTEGER DEFAULT 0,
                        invalidated_count INTEGER DEFAULT 0,
                        error_summary TEXT
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO feishu_sources (
                        source_id, name, wiki_root_token, target_kb_id, credential_env_name
                    ) VALUES
                        ('source-1', 'Source', 'root', 'kb-1', 'FEISHU_ACCESS_TOKEN'),
                        ('source-2', 'Other Source', 'other-root', 'kb-2', 'FEISHU_ACCESS_TOKEN')
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO feishu_sync_runs (run_id, source_id, run_type, status)
                    VALUES
                        ('run-first', 'source-1', 'full', 'queued'),
                        ('run-second', 'source-1', 'full', 'queued'),
                        ('run-other', 'source-2', 'full', 'queued')
                    """
                )
            )
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
