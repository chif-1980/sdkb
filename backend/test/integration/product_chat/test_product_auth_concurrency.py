from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from yuxi.product_chat.auth_service import ProductAuthService
from yuxi.storage.postgres.models_business import Base, Department, User
from yuxi.storage.postgres.models_product import FeishuUserBinding

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _profile() -> dict[str, str]:
    return {
        "open_id": "ou_concurrent_employee",
        "user_id": "concurrent-employee-001",
        "union_id": "on_concurrent_employee",
        "tenant_key": "tenant-a",
        "name": "Concurrent Employee",
        "avatar_url": "https://img.example/concurrent-avatar.png",
    }


async def test_concurrent_identical_first_binding_returns_the_winner_for_both_requests():
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        pytest.skip("POSTGRES_URL is not configured for the PostgreSQL auth integration test.")

    schema_name = f"product_auth_binding_{uuid4().hex}"
    engine = create_async_engine(postgres_url, pool_size=4, max_overflow=0)
    schema_engine = engine.execution_options(schema_translate_map={None: schema_name})
    setup_factory = async_sessionmaker(schema_engine, expire_on_commit=False)
    barrier = asyncio.Barrier(2)

    class RacingSession(AsyncSession):
        async def commit(self) -> None:
            if any(isinstance(record, FeishuUserBinding) for record in self.new):
                await asyncio.wait_for(barrier.wait(), timeout=5)
            await super().commit()

    racing_factory = async_sessionmaker(
        schema_engine,
        class_=RacingSession,
        expire_on_commit=False,
    )
    schema_created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
        async with schema_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        Department.__table__,
                        User.__table__,
                        FeishuUserBinding.__table__,
                    ],
                )
            )

        async with setup_factory() as setup_session:
            user = User(
                username="Concurrent Employee",
                uid="concurrent-employee-001",
                password_hash="not-used-by-product-auth",
                role="user",
                department=Department(name="Concurrent Auth Department"),
            )
            setup_session.add(user)
            await setup_session.commit()
            user_id = user.id

        async with racing_factory() as first_session, racing_factory() as second_session:
            results = await asyncio.gather(
                ProductAuthService(db=first_session, redis_client=None).resolve_bound_user(_profile()),
                ProductAuthService(db=second_session, redis_client=None).resolve_bound_user(_profile()),
                return_exceptions=True,
            )

        assert all(isinstance(result, User) for result in results)
        assert [result.id for result in results if isinstance(result, User)] == [user_id, user_id]
    finally:
        try:
            if schema_created:
                async with engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        finally:
            await engine.dispose()
