from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.governance.domain import (
    ProblemTag,
    ReviewItemStatus,
    ReviewOutcome,
    ReviewPackageStatus,
    ReviewSubjectType,
    ReviewTriggerType,
    ReviewType,
    SourceChangeRequestStatus,
)
from yuxi.governance.notification_service import NotificationService
from yuxi.governance.review_backfill import stable_review_id
from yuxi.storage.postgres.models_knowledge import (
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
)
from yuxi.utils.datetime_utils import ensure_utc, utc_now_naive


class KnowledgeLifecycleService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def update_unit_metadata(
        self,
        unit_id: str,
        *,
        owner_id: str | None,
        owner_name: str | None,
        valid_from: datetime | None,
        valid_until: datetime | None,
        review_due_at: datetime | None,
        operator_id: str,
    ) -> FeishuKnowledgeUnit:
        unit = await self._load_unit(unit_id, lock=True)
        version, item = await self._ensure_formal_unit(unit)
        valid_from = self._normalize_datetime(valid_from)
        valid_until = self._normalize_datetime(valid_until)
        review_due_at = self._normalize_datetime(review_due_at)
        if valid_from and valid_until and valid_from > valid_until:
            raise ValueError("valid_from must not be later than valid_until")

        before = {
            "owner_id": unit.owner_id,
            "owner_name": unit.owner_name,
            "valid_from": self._iso_datetime(unit.valid_from),
            "valid_until": self._iso_datetime(unit.valid_until),
            "review_due_at": self._iso_datetime(unit.review_due_at),
        }
        after = {
            "owner_id": self._optional_text(owner_id),
            "owner_name": self._optional_text(owner_name),
            "valid_from": self._iso_datetime(valid_from),
            "valid_until": self._iso_datetime(valid_until),
            "review_due_at": self._iso_datetime(review_due_at),
        }
        changed_fields = [field for field in before if before[field] != after[field]]
        unit.owner_id = self._optional_text(owner_id)
        unit.owner_name = self._optional_text(owner_name)
        unit.valid_from = valid_from
        unit.valid_until = valid_until
        unit.review_due_at = review_due_at
        unit.lifecycle_updated_by = operator_id
        unit.lifecycle_updated_at = utc_now_naive()
        self.session.add(
            FeishuProcessingEvent(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="knowledge_unit_metadata_updated",
                from_status=unit.lifecycle_status,
                to_status=unit.lifecycle_status,
                operator_id=operator_id,
                message=(
                    "已更新知识单元治理信息"
                    if changed_fields
                    else "已确认知识单元治理信息未变化"
                ),
                payload_json={
                    "unit_id": unit.unit_id,
                    "changed_fields": changed_fields,
                    "before": before,
                    "after": after,
                },
            )
        )
        await self.session.flush()
        return unit

    async def queue_unit_transition(
        self,
        unit_id: str,
        *,
        target: str,
        reason: str,
        operator_id: str,
    ) -> dict:
        if target not in {"ACTIVE", "OFFLINE"}:
            raise ValueError("Unsupported knowledge-unit lifecycle target")
        reason = reason.strip()
        if not reason:
            raise ValueError("Lifecycle reason is required")

        unit = await self._load_unit(unit_id, lock=True)
        row = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuMaterialVersion.version_id == unit.version_id)
                .with_for_update()
            )
        ).one()
        version, item = row
        if unit.status != "ACTIVE" or unit.publication_state != "INCLUDED":
            raise ValueError("Only included knowledge units can change lifecycle status")
        if item.active_version_id != version.version_id:
            raise ValueError("Knowledge unit is not part of the active source version")
        if item.publication_status not in {"ACTIVE", "RESTORE_FAILED"}:
            raise ValueError("Source is not currently available for unit lifecycle changes")
        if unit.lifecycle_status == target:
            return {
                "unit_id": unit.unit_id,
                "version_id": version.version_id,
                "target": target,
                "enqueue_required": False,
                "idempotent_replay": True,
            }

        params = dict(version.processing_params or {})
        requests = dict(params.get("lifecycle_requests") or {})
        existing = requests.get(unit.unit_id)
        if isinstance(existing, dict) and existing.get("target") == target:
            return {
                "unit_id": unit.unit_id,
                "version_id": version.version_id,
                "target": target,
                "enqueue_required": version.processing_status != "publishing",
                "idempotent_replay": True,
            }

        revision = int(params.get("unit_publish_requested_revision") or 0) + 1
        params["unit_publish_requested_revision"] = revision
        requests[unit.unit_id] = {
            "target": target,
            "revision": revision,
            "reason": reason,
            "operator_id": operator_id,
            "requested_at": utc_now_naive().isoformat(),
        }
        params["lifecycle_requests"] = requests
        version.processing_params = params
        from_status = version.processing_status
        enqueue_required = from_status != "publishing"
        if enqueue_required:
            version.processing_status = "publish_queued"
        version.error_code = None
        version.error_message = None
        self.session.add(
            FeishuProcessingEvent(
                source_id=item.source_id,
                item_id=item.item_id,
                version_id=version.version_id,
                event_type="knowledge_unit_lifecycle_queued",
                from_status=from_status,
                to_status=version.processing_status,
                operator_id=operator_id,
                message=reason,
                payload_json={"unit_id": unit.unit_id, "target": target, "revision": revision},
            )
        )
        await self.session.flush()
        return {
            "unit_id": unit.unit_id,
            "version_id": version.version_id,
            "target": target,
            "enqueue_required": enqueue_required,
            "idempotent_replay": False,
        }

    async def offline_segment_ids(self, version_id: str, *, claimed_revision: int) -> set[str]:
        units = list(
            await self.session.scalars(
                select(FeishuKnowledgeUnit).where(
                    FeishuKnowledgeUnit.version_id == version_id,
                    FeishuKnowledgeUnit.status == "ACTIVE",
                    FeishuKnowledgeUnit.publication_state == "INCLUDED",
                )
            )
        )
        version = await self.session.scalar(
            select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == version_id)
        )
        requests = dict((version.processing_params or {}).get("lifecycle_requests") or {}) if version else {}
        offline: set[str] = set()
        for unit in units:
            target = unit.lifecycle_status
            request = requests.get(unit.unit_id)
            if isinstance(request, dict) and int(request.get("revision") or 0) <= claimed_revision:
                target = request.get("target") or target
            if target == "OFFLINE":
                offline.update(str(segment_id) for segment_id in (unit.source_segment_ids or []))
        return offline

    async def apply_claimed_transitions(self, version_id: str, *, claimed_revision: int) -> None:
        version = await self.session.scalar(
            select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == version_id).with_for_update()
        )
        if version is None:
            raise LookupError(f"Feishu material not found: {version_id}")
        params = dict(version.processing_params or {})
        requests = dict(params.get("lifecycle_requests") or {})
        applied = {
            unit_id: request
            for unit_id, request in requests.items()
            if isinstance(request, dict) and int(request.get("revision") or 0) <= claimed_revision
        }
        if applied:
            units = list(
                await self.session.scalars(
                    select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.unit_id.in_(applied)).with_for_update()
                )
            )
            now = utc_now_naive()
            for unit in units:
                request = applied[unit.unit_id]
                unit.lifecycle_status = request["target"]
                unit.lifecycle_note = request.get("reason")
                unit.lifecycle_updated_by = request.get("operator_id")
                unit.lifecycle_updated_at = now
        remaining = {unit_id: request for unit_id, request in requests.items() if unit_id not in applied}
        if remaining:
            params["lifecycle_requests"] = remaining
        else:
            params.pop("lifecycle_requests", None)
        version.processing_params = params
        await self.session.flush()

    async def discard_claimed_transitions(self, version_id: str, *, claimed_revision: int) -> None:
        version = await self.session.scalar(
            select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == version_id).with_for_update()
        )
        if version is None:
            return
        params = dict(version.processing_params or {})
        requests = dict(params.get("lifecycle_requests") or {})
        remaining = {
            unit_id: request
            for unit_id, request in requests.items()
            if not isinstance(request, dict) or int(request.get("revision") or 0) > claimed_revision
        }
        if remaining:
            params["lifecycle_requests"] = remaining
        else:
            params.pop("lifecycle_requests", None)
        version.processing_params = params
        await self.session.flush()

    async def create_revision_request(
        self,
        unit_id: str,
        *,
        trigger_type: str,
        reason: str,
        operator_id: str,
    ) -> dict:
        if trigger_type not in {ReviewTriggerType.LIFECYCLE, ReviewTriggerType.FEEDBACK}:
            raise ValueError("Unsupported correction trigger")
        reason = reason.strip()
        if not reason:
            raise ValueError("Revision reason is required")
        unit = await self._load_unit(unit_id, lock=True)
        await self._ensure_formal_unit(unit)
        version, item = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuMaterialVersion.version_id == unit.version_id)
            )
        ).one()

        existing = (
            await self.session.execute(
                select(FeishuReviewPackage, FeishuReviewItem, FeishuSourceChangeRequest)
                .join(FeishuReviewItem, FeishuReviewItem.package_id == FeishuReviewPackage.package_id)
                .join(
                    FeishuSourceChangeRequest,
                    FeishuSourceChangeRequest.review_item_id == FeishuReviewItem.review_item_id,
                )
                .where(
                    FeishuReviewPackage.trigger_type == trigger_type,
                    FeishuReviewPackage.workflow_status == ReviewPackageStatus.WAITING_SOURCE_CHANGE,
                    FeishuReviewItem.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT,
                    FeishuReviewItem.subject_id == unit.unit_id,
                    FeishuSourceChangeRequest.status.in_(
                        {SourceChangeRequestStatus.OPEN, SourceChangeRequestStatus.NEW_VERSION_RECEIVED}
                    ),
                )
            )
        ).one_or_none()
        if existing:
            package, review_item, change_request = existing
            return self._revision_result(package, review_item, change_request, idempotent_replay=True)

        task_number = (
            int(
                await self.session.scalar(
                    select(func.count(FeishuReviewItem.id)).where(
                        FeishuReviewItem.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT,
                        FeishuReviewItem.subject_id == unit.unit_id,
                    )
                )
                or 0
            )
            + 1
        )
        identity = f"{trigger_type}:{unit.unit_id}:{task_number}"
        package = FeishuReviewPackage(
            package_id=stable_review_id("review-package-correction", identity),
            package_key=f"correction:{identity}",
            source_id=item.source_id,
            source_item_id=item.item_id,
            source_version_id=version.version_id,
            trigger_type=trigger_type,
            title_snapshot=item.title,
            path_snapshot=item.path_text,
            source_url_snapshot=item.source_url,
            workflow_status=ReviewPackageStatus.WAITING_SOURCE_CHANGE,
            assignee_id=None if trigger_type == ReviewTriggerType.FEEDBACK else operator_id,
            risk_level="HIGH" if trigger_type == ReviewTriggerType.FEEDBACK else "MEDIUM",
            draft_json={},
            lock_version=1,
        )
        review_item = FeishuReviewItem(
            review_item_id=stable_review_id("review-item-correction", identity),
            package_id=package.package_id,
            candidate_key=f"correction:{unit.unit_id}",
            review_type=ReviewType.STALE,
            subject_type=ReviewSubjectType.KNOWLEDGE_UNIT,
            subject_id=unit.unit_id,
            title=unit.title,
            summary=reason,
            subject_locator_json=dict(unit.locator_json or {}),
            evidence_json={"trigger_type": trigger_type},
            relation_ids=[],
            problem_tags=[ProblemTag.OUTDATED],
            applicability_scope=dict((version.processing_params or {}).get("applicability_scope") or {}),
            item_status=ReviewItemStatus.WAITING_SOURCE_CHANGE,
            outcome=ReviewOutcome.REQUEST_SOURCE_CHANGE,
            decision_comment=reason,
            decision_payload={"trigger_type": trigger_type},
            decided_by=operator_id,
            decided_at=utc_now_naive(),
        )
        change_request = FeishuSourceChangeRequest(
            change_request_id=stable_review_id("change-request", f"{review_item.review_item_id}:1"),
            review_item_id=review_item.review_item_id,
            source_item_id=item.item_id,
            requested_version_id=version.version_id,
            source_url=item.source_url,
            status=SourceChangeRequestStatus.OPEN,
            request_text=reason,
            responsible_user_id=unit.owner_id,
            responsible_user_name=unit.owner_name,
            round_number=1,
            created_by=operator_id,
        )
        self.session.add_all([package, review_item, change_request])
        await self.session.flush()
        await NotificationService(self.session).notify_admins(
            object_type="SOURCE_CHANGE",
            object_id=change_request.change_request_id,
            assignee_id=change_request.responsible_user_id,
            event_key="source-change-created",
            title="知识来源需要复核",
            body=f"{item.title or '未命名资料'}：{reason}",
            feishu=bool(change_request.responsible_user_id),
        )
        return self._revision_result(package, review_item, change_request, idempotent_replay=False)

    async def create_feedback_requests_for_segments(
        self,
        segment_ids: set[str],
        *,
        reason: str,
        operator_id: str,
    ) -> list[dict]:
        if not segment_ids:
            return []
        units = list(
            await self.session.scalars(
                select(FeishuKnowledgeUnit)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuKnowledgeUnit.item_id)
                .where(
                    FeishuKnowledgeUnit.status == "ACTIVE",
                    FeishuKnowledgeUnit.publication_state == "INCLUDED",
                    FeishuKnowledgeUnit.lifecycle_status == "ACTIVE",
                    FeishuSourceItem.active_version_id == FeishuKnowledgeUnit.version_id,
                    FeishuSourceItem.publication_status == "ACTIVE",
                )
            )
        )
        affected = [
            unit
            for unit in units
            if segment_ids.intersection(str(segment_id) for segment_id in (unit.source_segment_ids or []))
        ]
        results = []
        for unit in affected:
            results.append(
                await self.create_revision_request(
                    unit.unit_id,
                    trigger_type=ReviewTriggerType.FEEDBACK,
                    reason=reason,
                    operator_id=operator_id,
                )
            )
        return results

    async def _load_unit(self, unit_id: str, *, lock: bool) -> FeishuKnowledgeUnit:
        statement = select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.unit_id == unit_id)
        if lock:
            statement = statement.with_for_update()
        unit = await self.session.scalar(statement)
        if unit is None:
            raise LookupError(f"Knowledge unit not found: {unit_id}")
        return unit

    async def _ensure_formal_unit(
        self, unit: FeishuKnowledgeUnit
    ) -> tuple[FeishuMaterialVersion, FeishuSourceItem]:
        row = (
            await self.session.execute(
                select(FeishuMaterialVersion, FeishuSourceItem)
                .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                .where(FeishuMaterialVersion.version_id == unit.version_id)
            )
        ).one_or_none()
        if row is None:
            raise LookupError(f"Knowledge unit source version not found: {unit.version_id}")
        version, item = row
        if (
            unit.status != "ACTIVE"
            or unit.publication_state != "INCLUDED"
            or item.active_version_id != version.version_id
            or version.review_status != "approved"
            or version.published_at is None
        ):
            raise ValueError("Knowledge unit is not part of formal knowledge")
        return version, item

    @staticmethod
    def _normalize_datetime(value: datetime | None) -> datetime | None:
        return ensure_utc(value).replace(tzinfo=None) if value else None

    @staticmethod
    def _optional_text(value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @staticmethod
    def _iso_datetime(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _revision_result(package, review_item, change_request, *, idempotent_replay: bool) -> dict:
        return {
            "package_id": package.package_id,
            "review_item_id": review_item.review_item_id,
            "change_request_id": change_request.change_request_id,
            "workflow_status": package.workflow_status,
            "idempotent_replay": idempotent_replay,
        }
