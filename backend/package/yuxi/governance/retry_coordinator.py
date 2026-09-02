from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from yuxi.governance.domain import ReviewItemStatus, ReviewOutcome, ReviewPackageStatus, ReviewSubjectType
from yuxi.governance.knowledge_unit_service import KnowledgeUnitService
from yuxi.governance.notification_service import NotificationService, auto_close_enabled
from yuxi.governance.quality_gate_service import QualityGateService
from yuxi.governance.review_package_service import ReviewPackageService
from yuxi.governance.schemas import ReviewItemDecisionRequest, ReviewPackageResolveRequest
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSourceItem,
)
from yuxi.utils.datetime_utils import coerce_any_to_utc_datetime, utc_now
from yuxi.utils.logging_config import logger


class FeishuRetryCoordinator:
    """Resume failed Feishu processing without making GET endpoints executable."""

    RETRYABLE_STATUSES = {"parse_failed", "publish_failed"}
    MAX_RETRIES = 3
    CLAIM_TIMEOUT = timedelta(hours=1)

    def __init__(self, *, interval_seconds: float | None = None) -> None:
        configured = interval_seconds
        if configured is None:
            configured = float(os.getenv("YUXI_GOVERNANCE_RETRY_INTERVAL_SECONDS", "30"))
        self.interval_seconds = max(5.0, configured)
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def run_once(self, *, now: datetime | None = None) -> dict[str, int]:
        current_time = self._as_utc(now or utc_now())
        due = await self._claim_due_versions(current_time)
        retried = 0
        failed = 0
        for version_id, claim_token in due:
            try:
                if await self._retry_and_enqueue(version_id, claim_token):
                    retried += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("Governance retry failed for {}: {}", version_id, exc)

        notified = await self._notify_due_reviews(current_time)
        notification_activated = await self._activate_suppressed_notifications()
        notification_retried = await self._retry_failed_notifications(current_time)
        quality_evaluated, auto_closed = await self._evaluate_and_auto_close_reviews()
        return {
            "due": len(due),
            "retried": retried,
            "failed": failed,
            "expiry_notified": notified,
            "notification_activated": notification_activated,
            "notification_retried": notification_retried,
            "quality_evaluated": quality_evaluated,
            "auto_closed": auto_closed,
        }

    async def _claim_due_versions(self, now: datetime) -> list[tuple[str, str]]:
        """Atomically lease due rows so multiple API instances do not duplicate retries."""
        stale_before = now - self.CLAIM_TIMEOUT
        claimed: list[tuple[str, str]] = []
        async with pg_manager.get_async_session_context() as session:
            statement = (
                select(FeishuMaterialVersion)
                .where(
                    FeishuMaterialVersion.processing_status.in_(self.RETRYABLE_STATUSES),
                    FeishuMaterialVersion.retry_count < self.MAX_RETRIES,
                    or_(
                        FeishuMaterialVersion.retry_claimed_at.is_(None),
                        FeishuMaterialVersion.retry_claimed_at < stale_before,
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            versions = list(await session.scalars(statement))
            for version in versions:
                if not self._is_due(version, now):
                    continue
                token = f"retry_{uuid.uuid4().hex}"
                version.retry_claimed_at = now
                version.retry_claim_token = token
                claimed.append((version.version_id, token))
            await session.commit()
        return claimed

    async def _retry_and_enqueue(self, version_id: str, claim_token: str) -> bool:
        # Import at call time: the router owns the existing task enqueue functions.
        from server.routers.feishu_knowledge_router import (
            FeishuReviewService,
            _enqueue_processing,
            _enqueue_publish,
        )

        async with pg_manager.get_async_session_context() as session:
            version = await session.scalar(
                select(FeishuMaterialVersion)
                .where(FeishuMaterialVersion.version_id == version_id)
                .with_for_update()
            )
            if version is None or version.retry_claim_token != claim_token:
                return False
            material = await FeishuReviewService(session).retry(version_id, operator_id="system-retry")
            version.retry_claimed_at = None
            version.retry_claim_token = None
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
                failed_version = await session.scalar(
                    select(FeishuMaterialVersion)
                    .where(FeishuMaterialVersion.version_id == version_id)
                    .with_for_update()
                )
                if failed_version is not None:
                    failed_version.retry_claimed_at = None
                    failed_version.retry_claim_token = None
                await session.commit()
            raise
        return True

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

    async def _retry_failed_notifications(self, now: datetime) -> int:
        async with pg_manager.get_async_session_context() as session:
            return await NotificationService(session).retry_failed_feishu(now=now)

    async def _activate_suppressed_notifications(self) -> int:
        async with pg_manager.get_async_session_context() as session:
            activated = await NotificationService(session).activate_suppressed()
            await session.commit()
            return activated

    async def _evaluate_and_auto_close_reviews(self, *, limit: int = 50) -> tuple[int, int]:
        async with pg_manager.get_async_session_context() as session:
            package_ids = list(
                await session.scalars(
                    select(FeishuReviewPackage.package_id)
                    .where(FeishuReviewPackage.workflow_status == ReviewPackageStatus.OPEN)
                    .order_by(FeishuReviewPackage.quality_computed_at.asc().nullsfirst())
                    .limit(limit)
                )
            )

        evaluated = 0
        closed = 0
        for package_id in package_ids:
            try:
                async with pg_manager.get_async_session_context() as session:
                    package = await session.scalar(
                        select(FeishuReviewPackage)
                        .where(FeishuReviewPackage.package_id == package_id)
                        .with_for_update()
                    )
                    if package is None or package.workflow_status != ReviewPackageStatus.OPEN:
                        continue
                    await KnowledgeUnitService(session).ensure_for_package(package)
                    quality = await QualityGateService(session).evaluate_package(package.package_id)
                    evaluated += 1
                    if not auto_close_enabled() or not quality["autoCloseEligible"]:
                        await session.commit()
                        continue

                    items = list(
                        await session.scalars(
                            select(FeishuReviewItem).where(
                                FeishuReviewItem.package_id == package.package_id,
                                FeishuReviewItem.item_status == ReviewItemStatus.PENDING,
                                FeishuReviewItem.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT,
                            )
                        )
                    )
                    request_hash = hashlib.sha256(
                        f"{package.package_id}:{package.lock_version}".encode()
                    ).hexdigest()[:32]
                    result = await ReviewPackageService(session).resolve(
                        package.package_id,
                        ReviewPackageResolveRequest(
                            request_id=f"auto-close-{request_hash}",
                            lock_version=package.lock_version,
                            decisions=[
                                ReviewItemDecisionRequest(
                                    review_item_id=item.review_item_id,
                                    outcome=ReviewOutcome.KEEP_CURRENT,
                                    decision_comment="系统确认无业务变化，自动保留当前正式知识。",
                                )
                                for item in items
                            ],
                        ),
                        operator_id="system-governance-auto-close",
                        automated=True,
                    )
                    from server.routers.feishu_knowledge_router import FeishuReviewService

                    review_service = FeishuReviewService(session)
                    for candidate in result["reject_candidates"]:
                        await review_service.reject(
                            candidate["version_id"],
                            operator_id="system-governance-auto-close",
                            reason=candidate["reason"],
                        )
                    await NotificationService(session).notify_admins(
                        object_type="REVIEW_PACKAGE",
                        object_id=package.package_id,
                        assignee_id=package.assignee_id,
                        event_key="review-package-auto-closed",
                        title="无业务变化审核包已自动关闭",
                        body=f"{package.title_snapshot or '未命名资料'} 未发现正文、图片、版式或知识单元变化。",
                    )
                    await session.commit()
                    closed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Governance auto-close failed for {}: {}", package_id, exc)
        return evaluated, closed

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
