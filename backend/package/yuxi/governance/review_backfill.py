from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from hashlib import sha256

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.governance.domain import (
    ReviewAction,
    ReviewDecision,
    ReviewItemStatus,
    ReviewOutcome,
    ReviewPackageStatus,
    ReviewSubjectType,
    ReviewTriggerType,
    ReviewType,
    SourceChangeRequestStatus,
)
from yuxi.governance.source_change_service import SourceChangeService
from yuxi.governance.notification_service import NotificationService
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuGovernanceReview,
    FeishuMaterialVersion,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
)


TEXT_RELATION_TYPES = {"EXACT_DUPLICATE", "OVERLAP"}
BACKFILL_ADVISORY_LOCK_ID = 0x594B425245564945


async def _acquire_backfill_lock(session: AsyncSession) -> None:
    """Serialize the idempotent backfill when PostgreSQL workers finish together."""

    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": BACKFILL_ADVISORY_LOCK_ID},
    )


async def invalidate_unsubstantiated_text_relations(session: AsyncSession) -> int:
    """Retire old overlap alerts that were created from titles or paths only."""

    relations = list(
        await session.scalars(
            select(FeishuCrossDocumentRelation).where(
                FeishuCrossDocumentRelation.status == "open",
                FeishuCrossDocumentRelation.relation_type.in_(TEXT_RELATION_TYPES),
            )
        )
    )
    if not relations:
        return 0
    invalidated_ids: set[str] = set()
    for relation in relations:
        same_content = [str(item) for item in (relation.same_content or [])]
        has_text_evidence = any(item.startswith("正文") for item in same_content)
        if has_text_evidence:
            continue
        relation.status = "invalidated"
        relation.human_decision = "NO_TEXT_EVIDENCE"
        relation.human_comment = "旧关系仅由标题或目录相似产生，没有可核对的正文证据"
        invalidated_ids.add(relation.relation_id)

    if invalidated_ids:
        review_items = list(await session.scalars(select(FeishuReviewItem)))
        for item in review_items:
            retained_ids = [
                relation_id for relation_id in (item.relation_ids or []) if relation_id not in invalidated_ids
            ]
            if retained_ids != (item.relation_ids or []):
                item.relation_ids = retained_ids
                if not retained_ids:
                    item.problem_tags = [
                        tag for tag in (item.problem_tags or []) if tag not in {"DUPLICATE", "OVERLAP"}
                    ]
        await session.flush()
    return len(invalidated_ids)


def stable_review_id(prefix: str, value: str) -> str:
    return f"{prefix}-{sha256(value.encode('utf-8')).hexdigest()[:32]}"


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _review_type(
    review: FeishuGovernanceReview,
    item: FeishuSourceItem,
    *,
    has_conflict: bool = False,
) -> str:
    tags = set(_string_list(review.problem_tags))
    if has_conflict or "CONFLICT" in tags:
        return ReviewType.CONFLICT
    if "OUTDATED" in tags:
        return ReviewType.STALE
    if review.action == ReviewAction.CREATE:
        return ReviewType.NEW
    if review.action == ReviewAction.UPDATE:
        return ReviewType.UPDATE
    if item.active_version_id:
        if item.active_version_id == review.version_id:
            return ReviewType.STALE
        return ReviewType.UPDATE
    return ReviewType.NEW


def _package_status(status: str | None) -> str:
    return {
        "changes_requested": ReviewPackageStatus.WAITING_SOURCE_CHANGE,
        "resolved": ReviewPackageStatus.COMPLETED,
        "rejected": ReviewPackageStatus.COMPLETED,
    }.get(status or "", ReviewPackageStatus.OPEN)


def _item_status(status: str | None) -> str:
    return {
        "changes_requested": ReviewItemStatus.WAITING_SOURCE_CHANGE,
        "resolved": ReviewItemStatus.DECIDED,
        "rejected": ReviewItemStatus.DECIDED,
    }.get(status or "", ReviewItemStatus.PENDING)


def _outcome(review: FeishuGovernanceReview) -> str | None:
    if review.decision == ReviewDecision.REQUEST_CHANGES:
        return ReviewOutcome.REQUEST_SOURCE_CHANGE
    if review.action == ReviewAction.KEEP_CURRENT:
        return ReviewOutcome.KEEP_CURRENT
    if review.action == ReviewAction.ARCHIVE:
        return ReviewOutcome.ARCHIVE
    if review.decision == ReviewDecision.REJECT:
        return ReviewOutcome.EXCLUDE
    if review.decision != ReviewDecision.PUBLISH:
        return None
    return {
        ReviewAction.CREATE: ReviewOutcome.PUBLISH,
        ReviewAction.UPDATE: ReviewOutcome.ADOPT_NEW_VERSION,
        ReviewAction.SPLIT_BY_SCOPE: ReviewOutcome.SPLIT_SCOPE,
    }.get(review.action, ReviewOutcome.PUBLISH)


async def backfill_legacy_governance_reviews(
    session: AsyncSession,
    *,
    version_ids: Iterable[str] | None = None,
) -> dict[str, int]:
    """Idempotently migrate legacy one-version reviews into P0 review packages."""

    await _acquire_backfill_lock(session)
    await invalidate_unsubstantiated_text_relations(session)

    target_version_ids = {str(version_id) for version_id in (version_ids or []) if version_id}
    legacy_statement = (
        select(FeishuGovernanceReview, FeishuMaterialVersion, FeishuSourceItem)
        .join(FeishuMaterialVersion, FeishuMaterialVersion.version_id == FeishuGovernanceReview.version_id)
        .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
        .order_by(FeishuGovernanceReview.created_at.asc())
    )
    if target_version_ids:
        legacy_statement = legacy_statement.where(FeishuMaterialVersion.version_id.in_(target_version_ids))
    legacy_rows = await session.execute(legacy_statement)
    rows = list(legacy_rows.all())
    existing_version_ids = {version.version_id for _, version, _ in rows}
    pending_statement = (
        select(FeishuMaterialVersion, FeishuSourceItem)
        .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
        .outerjoin(FeishuGovernanceReview, FeishuGovernanceReview.version_id == FeishuMaterialVersion.version_id)
        .where(
            FeishuGovernanceReview.id.is_(None),
            FeishuMaterialVersion.processing_status.in_({"parsed", "awaiting_review"}),
            FeishuMaterialVersion.review_status.in_({"pending", "changes_requested"}),
        )
        .order_by(FeishuMaterialVersion.created_at.asc())
    )
    if target_version_ids:
        pending_statement = pending_statement.where(FeishuMaterialVersion.version_id.in_(target_version_ids))
    pending_rows = (await session.execute(pending_statement)).all()
    for version, source_item in pending_rows:
        if version.version_id in existing_version_ids:
            continue
        review = FeishuGovernanceReview(
            review_id=stable_review_id("review", version.version_id),
            version_id=version.version_id,
            status="changes_requested" if version.review_status == "changes_requested" else "pending",
            assignee_id=version.reviewer_id,
            problem_tags=[],
            decision_comment=version.review_comment,
            applicability_scope=dict((version.processing_params or {}).get("applicability_scope") or {}),
            created_at=version.created_at,
            updated_at=version.updated_at,
        )
        session.add(review)
        rows.append((review, version, source_item))
        existing_version_ids.add(version.version_id)
    if pending_rows:
        await session.flush()
    rows.sort(key=lambda row: row[0].created_at)
    if not rows:
        return {"packages_created": 0, "items_created": 0, "change_requests_created": 0}

    row_version_ids = {version.version_id for _, version, _ in rows}
    relation_rows = (
        await session.execute(
            select(
                FeishuCrossDocumentRelation.source_version_id,
                FeishuCrossDocumentRelation.target_version_id,
                FeishuCrossDocumentRelation.relation_id,
                FeishuCrossDocumentRelation.relation_type,
            ).where(
                FeishuCrossDocumentRelation.status != "invalidated",
                or_(
                    FeishuCrossDocumentRelation.source_version_id.in_(row_version_ids),
                    FeishuCrossDocumentRelation.target_version_id.in_(row_version_ids),
                ),
            )
        )
    ).all()
    relation_ids_by_version: dict[str, set[str]] = defaultdict(set)
    relation_types_by_version: dict[str, set[str]] = defaultdict(set)
    for source_version_id, target_version_id, relation_id, relation_type in relation_rows:
        if source_version_id in row_version_ids:
            relation_ids_by_version[source_version_id].add(relation_id)
            relation_types_by_version[source_version_id].add(relation_type)
        if target_version_id in row_version_ids:
            relation_ids_by_version[target_version_id].add(relation_id)
            relation_types_by_version[target_version_id].add(relation_type)

    packages_by_key = {
        package.package_key: package for package in (await session.scalars(select(FeishuReviewPackage))).all()
    }
    items_by_key = {
        (item.package_id, item.candidate_key): item for item in (await session.scalars(select(FeishuReviewItem))).all()
    }
    change_request_keys = set(
        (
            await session.execute(
                select(FeishuSourceChangeRequest.review_item_id, FeishuSourceChangeRequest.round_number)
            )
        ).all()
    )

    counts = {"packages_created": 0, "items_created": 0, "change_requests_created": 0}
    for review, version, source_item in rows:
        has_conflict = "CONFLICT" in relation_types_by_version[version.version_id]
        review_type = _review_type(review, source_item, has_conflict=has_conflict)
        problem_tags = _string_list(review.problem_tags)
        if has_conflict and "CONFLICT" not in problem_tags:
            problem_tags.append("CONFLICT")
        package_key = f"legacy-review:{review.review_id}"
        package = packages_by_key.get(package_key)
        if package is None:
            workflow_status = _package_status(review.status)
            package = FeishuReviewPackage(
                package_id=stable_review_id("review-package", review.review_id),
                package_key=package_key,
                source_id=source_item.source_id,
                source_item_id=source_item.item_id,
                source_version_id=version.version_id,
                trigger_type=ReviewTriggerType.SOURCE_VERSION,
                title_snapshot=source_item.title,
                path_snapshot=source_item.path_text,
                source_url_snapshot=source_item.source_url,
                workflow_status=workflow_status,
                assignee_id=review.assignee_id,
                risk_level="HIGH" if review_type == ReviewType.CONFLICT else "MEDIUM",
                draft_json={},
                lock_version=1,
                created_at=review.created_at,
                updated_at=review.updated_at,
                completed_at=(review.decided_at or review.updated_at)
                if workflow_status == ReviewPackageStatus.COMPLETED
                else None,
            )
            session.add(package)
            # ReviewPackage and ReviewItem are connected by external business
            # identifiers rather than an ORM relationship. Flush the parent
            # explicitly so query-triggered autoflush cannot insert the child
            # first and violate the PostgreSQL foreign key.
            await session.flush([package])
            packages_by_key[package_key] = package
            counts["packages_created"] += 1
            await NotificationService(session).notify_admins(
                object_type="REVIEW_PACKAGE",
                object_id=package.package_id,
                assignee_id=package.assignee_id,
                event_key="review-package-created",
                title="新增知识审核待办",
                body=f"{source_item.title or '未命名资料'} 已进入知识审核，请复核处理。",
                feishu=package.risk_level == "HIGH" or bool(package.assignee_id),
            )

        candidate_key = f"legacy-review:{review.review_id}"
        review_item_id = stable_review_id("review-item", review.review_id)
        item_key = (package.package_id, candidate_key)
        existing_item = items_by_key.get(item_key)
        if existing_item is None:
            existing_item = FeishuReviewItem(
                review_item_id=review_item_id,
                package_id=package.package_id,
                candidate_key=candidate_key,
                review_type=review_type,
                subject_type=ReviewSubjectType.MATERIAL_VERSION,
                subject_id=version.version_id,
                title=source_item.title,
                summary=review.decision_comment or "由旧审核任务迁移",
                subject_locator_json={},
                evidence_json={
                    "legacy_migrated": True,
                    "legacy_review_id": review.review_id,
                    "legacy_relation_scope": "DOCUMENT",
                },
                relation_ids=sorted(relation_ids_by_version[version.version_id]),
                problem_tags=problem_tags,
                applicability_scope=dict(review.applicability_scope or {}),
                item_status=_item_status(review.status),
                outcome=_outcome(review),
                internal_action=review.action,
                decision_comment=review.decision_comment,
                decision_payload={
                    "legacy_migrated": True,
                    "legacy_decision": review.decision,
                    "legacy_action": review.action,
                },
                decided_by=review.decided_by,
                decided_at=review.decided_at,
                created_at=review.created_at,
                updated_at=review.updated_at,
            )
            session.add(existing_item)
            items_by_key[item_key] = existing_item
            counts["items_created"] += 1
        elif existing_item.item_status in {"PENDING", "WAITING_BUSINESS_CONFIRMATION"}:
            existing_item.relation_ids = sorted(
                set(existing_item.relation_ids or []) | relation_ids_by_version[version.version_id]
            )
            existing_item.problem_tags = sorted(set(existing_item.problem_tags or []) | set(problem_tags))
            if (existing_item.evidence_json or {}).get("legacy_migrated"):
                existing_item.review_type = review_type
            if review_type == ReviewType.CONFLICT:
                package.risk_level = "HIGH"

        await SourceChangeService(session).link_reopened_review_item(existing_item)

        review_item_id = existing_item.review_item_id
        change_request_key = (review_item_id, 1)
        if review.status == "changes_requested" and change_request_key not in change_request_keys:
            session.add(
                FeishuSourceChangeRequest(
                    change_request_id=stable_review_id("change-request", f"{review.review_id}:1"),
                    review_item_id=review_item_id,
                    source_item_id=source_item.item_id,
                    requested_version_id=version.version_id,
                    source_url=source_item.source_url,
                    status=SourceChangeRequestStatus.OPEN,
                    request_text=review.decision_comment or "旧审核任务要求修改飞书原文",
                    round_number=1,
                    created_by=review.decided_by or version.reviewer_id,
                    created_at=review.decided_at or review.updated_at,
                    updated_at=review.updated_at,
                )
            )
            change_request_keys.add(change_request_key)
            counts["change_requests_created"] += 1

    await session.flush()
    return counts
