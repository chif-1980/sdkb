from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import server.routers.feishu_knowledge_router as feishu_router
import yuxi.governance.retry_coordinator as retry_module
from yuxi.governance.retry_coordinator import FeishuRetryCoordinator
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import FeishuMaterialVersion


@pytest.mark.asyncio
async def test_due_failed_version_is_retried_and_enqueued(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            FeishuMaterialVersion(
                version_id="version-1",
                item_id="item-1",
                revision="1",
                content_hash="hash-1",
                processing_status="parse_failed",
                retry_count=0,
                processing_params={"governance_retry_next_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()},
            )
        )
        await session.commit()

        class Manager:
            @asynccontextmanager
            async def get_async_session_context(self):
                yield session

        class FakeReviewService:
            def __init__(self, _session):
                pass

            async def retry(self, _version_id, *, operator_id):
                assert operator_id == "system-retry"
                return SimpleNamespace(processing_status="processing_queued")

            async def mark_processing_queue_failed(self, *_args, **_kwargs):
                raise AssertionError("enqueue should succeed")

        enqueued = []

        async def fake_enqueue(version_id, *, operator_id):
            enqueued.append((version_id, operator_id))

        monkeypatch.setattr(retry_module, "pg_manager", Manager())
        monkeypatch.setattr(feishu_router, "FeishuReviewService", FakeReviewService)
        monkeypatch.setattr(feishu_router, "_enqueue_processing", fake_enqueue)

        result = await FeishuRetryCoordinator(interval_seconds=5).run_once()
        assert result["due"] == 1
        assert result["retried"] == 1
        assert enqueued == [("version-1", "system-retry")]
    await engine.dispose()


def test_retry_coordinator_ignores_missing_schedule_and_exhausted_retries():
    now = datetime.now(UTC)
    missing = FeishuMaterialVersion(
        version_id="missing",
        item_id="item",
        revision="1",
        content_hash="hash",
        processing_status="parse_failed",
        retry_count=0,
        processing_params={},
    )
    exhausted = FeishuMaterialVersion(
        version_id="exhausted",
        item_id="item",
        revision="1",
        content_hash="hash",
        processing_status="parse_failed",
        retry_count=3,
        processing_params={"governance_retry_next_at": (now - timedelta(minutes=1)).isoformat()},
    )

    assert FeishuRetryCoordinator._is_due(missing, now) is False
    assert FeishuRetryCoordinator._is_due(exhausted, now) is False


def test_failure_retry_schedule_uses_one_five_and_fifteen_minutes(monkeypatch):
    now = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(feishu_router, "utc_now", lambda: now)

    scheduled = []
    for retry_count in range(4):
        version = FeishuMaterialVersion(
            version_id=f"version-{retry_count}",
            item_id="item",
            revision="1",
            content_hash="hash",
            processing_status="parse_failed",
            retry_count=retry_count,
            processing_params={},
        )
        feishu_router.FeishuReviewService._schedule_retry(version)
        scheduled.append((version.processing_params or {}).get("governance_retry_next_at"))

    assert scheduled == [
        (now + timedelta(minutes=1)).isoformat(),
        (now + timedelta(minutes=5)).isoformat(),
        (now + timedelta(minutes=15)).isoformat(),
        None,
    ]
