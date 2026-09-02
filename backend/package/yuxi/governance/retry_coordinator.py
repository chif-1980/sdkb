from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

from sqlalchemy import select

from yuxi.governance.notification_service import NotificationService
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuSourceItem,
)
from yuxi.utils.datetime_utils import coerce_any_to_utc_datetime, utc_now
from yuxi.utils.logging_config import logger


class FeishuRetryCoordinator:
    """Resume failed Feishu processing without making GET endpoints executable."""

    RETRYABLE_STATUSES = {"parse_failed", "publish_failed"}
    MAX_RETRIES = 3

    def __init__(self, *, interval_seconds: float | None = None) -> None:
        configured = interval_seconds
        if configured is None:
            configured = float(os.getenv("YUXI_GOVERNANCE_RETRY_INTERVAL_SECONDS", "30"))
        self.interval_seconds = max(5.0, configured)
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def run_once(self, *, now: datetime | None = None) -> dict[str, int]:
        current_time = self._as_utc(now or utc_now())
        async with pg_manager.get_async_session_context() as session:
            versions = list(
                await session.scalars(
                    select(FeishuMaterialVersion).where(
                        FeishuMaterialVersion.processing_status.in_(self.RETRYABLE_STATUSES),
                        FeishuMaterialVersion.retry_count < self.MAX_RETRIES,
                    )
                )
            )

        due = [version for version in versions if self._is_due(version, current_time)]
        retried = 0
        failed = 0
        for version in due:
            try:
                await self._retry_and_enqueue(version.version_id)
                retried += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("Governance retry failed for {}: {}", version.version_id, exc)

        notified = await self._notify_due_reviews(current_time)
        return {"due": len(due), "retried": retried, "failed": failed, "expiry_notified": notified}

    async def _retry_and_enqueue(self, version_id: str) -> None:
        # Import at call time: the router owns the existing task enqueue functions.
        from server.routers.feishu_knowledge_router import (
            FeishuReviewService,
            _enqueue_processing,
            _enqueue_publish,
        )

        async with pg_manager.get_async_session_context() as session:
            material = await FeishuReviewService(session).retry(version_id, operator_id="system-retry")
            await session.commit()

        enqueue = _enqueue_publish if material.processing_status == "publish_queued" else _enqueue_processing
        try:
            await enqueue(version_id, operator_id="system-retry")
        except Exception as exc:
            async with pg_manager.get_async_session_context() as session:
                service = FeishuReviewService(session)
                if material.processing_status == "publish_queued":
                    await service.mark_publish_failed(version_id, message=str(exc))
                else:
                    await service.mark_processing_queue_failed(version_id, message=str(exc))
                await session.commit()
            raise

    async def _notify_due_reviews(self, now: datetime) -> int:
        today = now.astimezone(UTC).date().isoformat()
        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(FeishuKnowledgeUnit, FeishuSourceItem)
                    .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuKnowledgeUnit.item_id)
                    .where(
                        FeishuKnowledgeUnit.status == "ACTIVE",
                        FeishuKnowledgeUnit.publication_state == "INCLUDED",
                        FeishuKnowledgeUnit.review_due_at.is_not(None),
                    )
                )
            ).all()
            created = 0
            for unit, item in rows:
                due_at = self._db_datetime(unit.review_due_at)
                if due_at is None or due_at > now:
                    continue
                created += await NotificationService(session).notify_admins(
                    object_type="EXPIRY_REVIEW",
                    object_id=unit.unit_id,
                    assignee_id=unit.owner_id,
                    event_key=f"expiry-review:{today}",
                    title="正式知识需要到期复核",
                    body=f"{item.title or unit.title or '未命名知识'} 已到复核时间，请确认有效性。",
                    feishu=True,
                )
            return created

    @classmethod
    def _is_due(cls, version: FeishuMaterialVersion, now: datetime) -> bool:
        if version.processing_status not in cls.RETRYABLE_STATUSES:
            return False
        if int(version.retry_count or 0) >= cls.MAX_RETRIES:
            return False
        raw = (version.processing_params or {}).get("governance_retry_next_at")
        if not raw:
            return False
        try:
            due_at = coerce_any_to_utc_datetime(raw)
        except (TypeError, ValueError):
            return False
        return bool(due_at and due_at <= cls._as_utc(now))

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _db_datetime(value: datetime | None) -> datetime | None:
        """Legacy DB DateTime columns store UTC as naive values."""
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="feishu-governance-retry-coordinator")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Governance retry coordinator iteration failed: {}", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                continue


retry_coordinator = FeishuRetryCoordinator()
