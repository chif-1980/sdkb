from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.governance.domain import (
    ReviewItemStatus,
    ReviewPackageStatus,
    ReviewSubjectType,
    SourceChangeRequestStatus,
)
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
)
from yuxi.utils.datetime_utils import utc_now_naive


FINAL_ITEM_STATUSES = {
    ReviewItemStatus.DECIDED,
    ReviewItemStatus.SOURCE_UPDATED,
    ReviewItemStatus.INVALIDATED,
}
ACTIVE_CHANGE_REQUEST_STATUSES = {
    SourceChangeRequestStatus.OPEN,
    SourceChangeRequestStatus.NEW_VERSION_RECEIVED,
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class SourceChangeService:
    """Close the loop between read-only Feishu source changes and review work."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register_new_material_version(
        self,
        version_id: str,
        *,
        operator_id: str | None = None,
    ) -> dict:
        row = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuMaterialVersion.version_id == version_id)
            )
        ).one_or_none()
        if row is None:
            raise LookupError(f"Material version not found: {version_id}")
        version, source_item = row

        requests = list(
            await self.session.scalars(
                select(FeishuSourceChangeRequest)
                .where(
                    FeishuSourceChangeRequest.source_item_id == source_item.item_id,
                    FeishuSourceChangeRequest.status == SourceChangeRequestStatus.OPEN,
                    FeishuSourceChangeRequest.requested_version_id.is_not(None),
                    FeishuSourceChangeRequest.requested_version_id != version.version_id,
                )
                .order_by(FeishuSourceChangeRequest.created_at.asc())
                .with_for_update()
            )
        )
        if not requests:
            return {"version_id": version_id, "received_count": 0, "change_request_ids": []}

        requested_version_ids = {request.requested_version_id for request in requests}
        requested_hashes = dict(
            (
                await self.session.execute(
                    select(FeishuMaterialVersion.version_id, FeishuMaterialVersion.content_hash).where(
                        FeishuMaterialVersion.version_id.in_(requested_version_ids)
                    )
                )
            ).all()
        )
        now = utc_now_naive()
        received_request_ids: list[str] = []
        package_ids: set[str] = set()
        for request in requests:
            requested_hash = requested_hashes.get(request.requested_version_id)
            if requested_hash is None or requested_hash == version.content_hash:
                continue
            item = await self.session.scalar(
                select(FeishuReviewItem)
                .where(FeishuReviewItem.review_item_id == request.review_item_id)
                .with_for_update()
            )
            if item is None:
                continue
            request.status = SourceChangeRequestStatus.NEW_VERSION_RECEIVED
            request.received_version_id = version.version_id
            request.updated_at = now
            if item.item_status == ReviewItemStatus.WAITING_SOURCE_CHANGE:
                item.item_status = ReviewItemStatus.SOURCE_UPDATED
                item.updated_at = now
            package_ids.add(item.package_id)
            received_request_ids.append(request.change_request_id)
            self.session.add(
                FeishuProcessingEvent(
                    source_id=source_item.source_id,
                    item_id=source_item.item_id,
                    version_id=version.version_id,
                    event_type="source_change_version_received",
                    from_status=SourceChangeRequestStatus.OPEN,
                    to_status=SourceChangeRequestStatus.NEW_VERSION_RECEIVED,
                    operator_id=operator_id,
                    message="检测到飞书原文发生实质变化",
                    payload_json={
                        "change_request_id": request.change_request_id,
                        "review_item_id": request.review_item_id,
                        "requested_version_id": request.requested_version_id,
                        "received_version_id": version.version_id,
                    },
                )
            )

        for package_id in package_ids:
            await self._refresh_package_status(package_id, now=now, operator_id=operator_id)
        await self.session.flush()
        return {
            "version_id": version_id,
            "received_count": len(received_request_ids),
            "change_request_ids": received_request_ids,
        }

    async def link_reopened_review_item(self, item: FeishuReviewItem) -> str | None:
        if item.subject_type != ReviewSubjectType.MATERIAL_VERSION or item.reopened_from_item_id:
            return item.reopened_from_item_id
        request = await self.session.scalar(
            select(FeishuSourceChangeRequest)
            .where(
                FeishuSourceChangeRequest.received_version_id == item.subject_id,
                FeishuSourceChangeRequest.status == SourceChangeRequestStatus.NEW_VERSION_RECEIVED,
            )
            .order_by(FeishuSourceChangeRequest.updated_at.desc(), FeishuSourceChangeRequest.id.desc())
            .limit(1)
        )
        if request is None:
            return None
        package = await self.session.scalar(
            select(FeishuReviewPackage).where(FeishuReviewPackage.package_id == item.package_id)
        )
        if package is None:
            return None
        item.reopened_from_item_id = request.review_item_id
        self.session.add(
            FeishuProcessingEvent(
                source_id=package.source_id,
                item_id=package.source_item_id,
                version_id=package.source_version_id,
                event_type="review_item_reopened",
                message="飞书新版本已解析，重新进入审核",
                payload_json={
                    "package_id": package.package_id,
                    "review_item_id": item.review_item_id,
                    "reopened_from_item_id": request.review_item_id,
                    "change_request_id": request.change_request_id,
                },
            )
        )
        await self.session.flush()
        return request.review_item_id

    async def fulfill_received_requests(
        self,
        item: FeishuReviewItem,
        *,
        operator_id: str,
        now: datetime,
    ) -> int:
        if item.subject_type != ReviewSubjectType.MATERIAL_VERSION:
            return 0
        requests = list(
            await self.session.scalars(
                select(FeishuSourceChangeRequest)
                .where(
                    FeishuSourceChangeRequest.received_version_id == item.subject_id,
                    FeishuSourceChangeRequest.status == SourceChangeRequestStatus.NEW_VERSION_RECEIVED,
                )
                .with_for_update()
            )
        )
        if not requests:
            return 0
        package = await self.session.scalar(
            select(FeishuReviewPackage).where(FeishuReviewPackage.package_id == item.package_id)
        )
        if package is None:
            return 0
        for request in requests:
            request.status = SourceChangeRequestStatus.FULFILLED
            request.resolved_at = now
            request.updated_at = now
            self.session.add(
                FeishuProcessingEvent(
                    source_id=package.source_id,
                    item_id=package.source_item_id,
                    version_id=package.source_version_id,
                    event_type="source_change_request_fulfilled",
                    from_status=SourceChangeRequestStatus.NEW_VERSION_RECEIVED,
                    to_status=SourceChangeRequestStatus.FULFILLED,
                    operator_id=operator_id,
                    payload_json={
                        "package_id": package.package_id,
                        "review_item_id": item.review_item_id,
                        "change_request_id": request.change_request_id,
                    },
                )
            )
        await self.session.flush()
        return len(requests)

    async def list_change_requests(
        self,
        source_id: str,
        *,
        status: str | None = None,
        responsible_user_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        conditions = [FeishuReviewPackage.source_id == source_id]
        if status:
            conditions.append(FeishuSourceChangeRequest.status == status)
        if responsible_user_id:
            conditions.append(FeishuSourceChangeRequest.responsible_user_id == responsible_user_id)
        base = (
            select(FeishuSourceChangeRequest, FeishuReviewItem, FeishuReviewPackage)
            .join(FeishuReviewItem, FeishuReviewItem.review_item_id == FeishuSourceChangeRequest.review_item_id)
            .join(FeishuReviewPackage, FeishuReviewPackage.package_id == FeishuReviewItem.package_id)
            .where(*conditions)
        )
        total = int(await self.session.scalar(select(func.count()).select_from(base.subquery())) or 0)
        rows = (
            await self.session.execute(
                base.order_by(FeishuSourceChangeRequest.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return {
            "items": [self._change_request_dict(request, item, package) for request, item, package in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def get_change_request(self, change_request_id: str) -> dict:
        row = (
            await self.session.execute(
                select(FeishuSourceChangeRequest, FeishuReviewItem, FeishuReviewPackage)
                .join(FeishuReviewItem, FeishuReviewItem.review_item_id == FeishuSourceChangeRequest.review_item_id)
                .join(FeishuReviewPackage, FeishuReviewPackage.package_id == FeishuReviewItem.package_id)
                .where(FeishuSourceChangeRequest.change_request_id == change_request_id)
            )
        ).one_or_none()
        if row is None:
            raise LookupError(f"Source change request not found: {change_request_id}")
        return self._change_request_dict(*row)

    async def cancel_change_request(
        self,
        change_request_id: str,
        *,
        operator_id: str,
        reason: str,
    ) -> dict:
        request = await self.session.scalar(
            select(FeishuSourceChangeRequest)
            .where(FeishuSourceChangeRequest.change_request_id == change_request_id)
            .with_for_update()
        )
        if request is None:
            raise LookupError(f"Source change request not found: {change_request_id}")
        if request.status not in ACTIVE_CHANGE_REQUEST_STATUSES:
            raise ValueError("Only open or newly received source-change requests can be cancelled")
        item = await self.session.scalar(
            select(FeishuReviewItem)
            .where(FeishuReviewItem.review_item_id == request.review_item_id)
            .with_for_update()
        )
        if item is None:
            raise LookupError(f"Review item not found: {request.review_item_id}")
        package = await self.session.scalar(
            select(FeishuReviewPackage)
            .where(FeishuReviewPackage.package_id == item.package_id)
            .with_for_update()
        )
        if package is None:
            raise LookupError(f"Review package not found: {item.package_id}")

        previous_status = request.status
        now = utc_now_naive()
        request.status = SourceChangeRequestStatus.CANCELLED
        request.resolved_at = now
        request.updated_at = now
        remaining_active = await self.session.scalar(
            select(func.count())
            .select_from(FeishuSourceChangeRequest)
            .where(
                FeishuSourceChangeRequest.review_item_id == item.review_item_id,
                FeishuSourceChangeRequest.change_request_id != request.change_request_id,
                FeishuSourceChangeRequest.status.in_(ACTIVE_CHANGE_REQUEST_STATUSES),
            )
        )
        if not remaining_active and item.item_status == ReviewItemStatus.WAITING_SOURCE_CHANGE:
            item.item_status = ReviewItemStatus.INVALIDATED
            item.updated_at = now
        await self._refresh_package_status(package.package_id, now=now, operator_id=operator_id)
        self.session.add(
            FeishuProcessingEvent(
                source_id=package.source_id,
                item_id=package.source_item_id,
                version_id=package.source_version_id,
                event_type="source_change_request_cancelled",
                from_status=previous_status,
                to_status=SourceChangeRequestStatus.CANCELLED,
                operator_id=operator_id,
                message=reason,
                payload_json={
                    "package_id": package.package_id,
                    "review_item_id": item.review_item_id,
                    "change_request_id": request.change_request_id,
                },
            )
        )
        await self.session.flush()
        return self._change_request_dict(request, item, package)

    async def _refresh_package_status(
        self,
        package_id: str,
        *,
        now: datetime,
        operator_id: str | None,
    ) -> None:
        package = await self.session.scalar(
            select(FeishuReviewPackage)
            .where(FeishuReviewPackage.package_id == package_id)
            .with_for_update()
        )
        if package is None:
            return
        items = list(
            await self.session.scalars(select(FeishuReviewItem).where(FeishuReviewItem.package_id == package_id))
        )
        statuses = {item.item_status for item in items}
        if ReviewItemStatus.WAITING_SOURCE_CHANGE in statuses:
            workflow_status = ReviewPackageStatus.WAITING_SOURCE_CHANGE
        elif ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION in statuses:
            workflow_status = ReviewPackageStatus.WAITING_BUSINESS_CONFIRMATION
        elif statuses and statuses <= FINAL_ITEM_STATUSES:
            workflow_status = ReviewPackageStatus.COMPLETED
        else:
            workflow_status = ReviewPackageStatus.OPEN
        previous_status = package.workflow_status
        if previous_status == workflow_status:
            return
        package.workflow_status = workflow_status
        package.completed_at = now if workflow_status == ReviewPackageStatus.COMPLETED else None
        package.lock_version += 1
        package.updated_at = now
        if workflow_status == ReviewPackageStatus.COMPLETED:
            self.session.add(
                FeishuProcessingEvent(
                    source_id=package.source_id,
                    item_id=package.source_item_id,
                    version_id=package.source_version_id,
                    event_type="review_package_completed",
                    from_status=previous_status,
                    to_status=workflow_status,
                    operator_id=operator_id,
                    payload_json={"package_id": package.package_id},
                )
            )

    @staticmethod
    def _change_request_dict(
        request: FeishuSourceChangeRequest,
        item: FeishuReviewItem,
        package: FeishuReviewPackage,
    ) -> dict:
        return {
            "change_request_id": request.change_request_id,
            "review_item_id": request.review_item_id,
            "package_id": package.package_id,
            "source_id": package.source_id,
            "source_item_id": request.source_item_id,
            "requested_version_id": request.requested_version_id,
            "received_version_id": request.received_version_id,
            "source_url": request.source_url,
            "status": request.status,
            "request_text": request.request_text,
            "responsible_user_id": request.responsible_user_id,
            "responsible_user_name": request.responsible_user_name,
            "round_number": request.round_number,
            "created_by": request.created_by,
            "created_at": _iso(request.created_at),
            "updated_at": _iso(request.updated_at),
            "resolved_at": _iso(request.resolved_at),
            "review_item_status": item.item_status,
            "review_type": item.review_type,
            "title": item.title or package.title_snapshot,
            "workflow_status": package.workflow_status,
        }
