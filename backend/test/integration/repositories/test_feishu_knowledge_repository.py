from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from yuxi.repositories.feishu_knowledge_repository import FeishuKnowledgeRepository
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
