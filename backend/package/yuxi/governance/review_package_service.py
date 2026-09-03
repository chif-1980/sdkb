from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.governance.domain import (
    ProblemTag,
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
from yuxi.governance.knowledge_unit_service import KnowledgeUnitService
from yuxi.governance.notification_service import NotificationService
from yuxi.governance.quality_gate_service import QualityGateService
from yuxi.governance.review_backfill import backfill_legacy_governance_reviews, stable_review_id
from yuxi.governance.schemas import (
    ReviewPackageBulkExcludeRequest,
    ReviewItemDecisionRequest,
    ReviewPackageResolveRequest,
)
from yuxi.governance.source_change_service import SourceChangeService
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuGovernanceReview,
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSource,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
    FeishuSourceSegment,
)
from yuxi.utils.datetime_utils import utc_now_naive


OUTCOME_ACTIONS: dict[str, dict[str, str | None]] = {
    ReviewType.NEW: {
        ReviewOutcome.PUBLISH: ReviewAction.CREATE,
        ReviewOutcome.REQUEST_SOURCE_CHANGE: None,
        ReviewOutcome.EXCLUDE: ReviewAction.ARCHIVE,
    },
    ReviewType.UPDATE: {
        ReviewOutcome.ADOPT_NEW_VERSION: ReviewAction.UPDATE,
        ReviewOutcome.KEEP_CURRENT: ReviewAction.KEEP_CURRENT,
        ReviewOutcome.EXCLUDE: ReviewAction.ARCHIVE,
        ReviewOutcome.SPLIT_SCOPE: ReviewAction.SPLIT_BY_SCOPE,
        ReviewOutcome.REQUEST_SOURCE_CHANGE: None,
    },
    ReviewType.CONFLICT: {
        ReviewOutcome.KEEP_CURRENT: ReviewAction.KEEP_CURRENT,
        ReviewOutcome.ADOPT_NEW_VERSION: ReviewAction.UPDATE,
        ReviewOutcome.SPLIT_SCOPE: ReviewAction.SPLIT_BY_SCOPE,
        ReviewOutcome.WAIT_BUSINESS_CONFIRMATION: None,
    },
    ReviewType.STALE: {
        ReviewOutcome.CONFIRM_VALID: ReviewAction.KEEP_CURRENT,
        ReviewOutcome.REQUEST_SOURCE_CHANGE: None,
        ReviewOutcome.REQUEST_SUPPORTING_SOURCE: None,
        ReviewOutcome.DISMISS: None,
    },
}

PROCESSING_ONLY_TAGS = {ProblemTag.PARSE_ERROR, ProblemTag.CONTENT_MISSING}
COMMENT_REQUIRED_OUTCOMES = {
    ReviewOutcome.REQUEST_SOURCE_CHANGE,
    ReviewOutcome.REQUEST_SUPPORTING_SOURCE,
    ReviewOutcome.WAIT_BUSINESS_CONFIRMATION,
}
PUBLISH_OUTCOMES = {
    ReviewOutcome.PUBLISH,
    ReviewOutcome.ADOPT_NEW_VERSION,
    ReviewOutcome.SPLIT_SCOPE,
}
UNIT_PUBLISH_OUTCOMES = PUBLISH_OUTCOMES | {ReviewOutcome.CONFIRM_VALID}
REJECT_CANDIDATE_OUTCOMES = {ReviewOutcome.EXCLUDE, ReviewOutcome.KEEP_CURRENT}
COMPLETION_RESULTS = {"all_included", "partial", "all_excluded", "all_duplicate"}
FINAL_ITEM_STATUSES = {
    ReviewItemStatus.DECIDED,
    ReviewItemStatus.SOURCE_UPDATED,
    ReviewItemStatus.INVALIDATED,
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class ReviewPackageService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_packages(
        self,
        source_id: str,
        *,
        operator_id: str,
        view: str = "mine",
        workflow_statuses: list[str] | None = None,
        review_types: list[str] | None = None,
        problem_tag: str | None = None,
        risk_level: str | None = None,
        completion_result: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        await backfill_legacy_governance_reviews(self.session)
        if completion_result and completion_result not in COMPLETION_RESULTS:
            raise ValueError("completion_result must be all_included, partial, all_excluded or all_duplicate")
        statement = select(FeishuReviewPackage).where(FeishuReviewPackage.source_id == source_id)
        if risk_level:
            statement = statement.where(FeishuReviewPackage.risk_level == risk_level)
        if workflow_statuses:
            statement = statement.where(FeishuReviewPackage.workflow_status.in_(workflow_statuses))
        elif view == "mine":
            statement = statement.where(
                FeishuReviewPackage.workflow_status.not_in(
                    {ReviewPackageStatus.COMPLETED, ReviewPackageStatus.INVALIDATED}
                )
            )

        if view == "mine":
            statement = statement.where(
                or_(FeishuReviewPackage.assignee_id == operator_id, FeishuReviewPackage.assignee_id.is_(None))
            )
        elif view == "transferred_by_me":
            transferred_ids = await self._transferred_package_ids(source_id, operator_id)
            if not transferred_ids:
                return {
                    "items": [],
                    "total": 0,
                    "page": page,
                    "page_size": page_size,
                    "counts": await self._counts(source_id, operator_id),
                }
            statement = statement.where(FeishuReviewPackage.package_id.in_(transferred_ids))
        elif view != "all":
            raise ValueError("view must be mine, all or transferred_by_me")

        packages = list(
            await self.session.scalars(
                statement.order_by(
                    case(
                        (FeishuReviewPackage.risk_level == "HIGH", 0),
                        (FeishuReviewPackage.risk_level == "MEDIUM", 1),
                        else_=2,
                    ),
                    FeishuReviewPackage.created_at.asc(),
                )
            )
        )
        items_by_package = await self._items_by_package([package.package_id for package in packages])
        if completion_result:
            unit_service = KnowledgeUnitService(self.session)
            for package in packages:
                await unit_service.ensure_for_package(package)
            items_by_package = await self._items_by_package([package.package_id for package in packages])
        filtered_packages = []
        for package in packages:
            items = items_by_package[package.package_id]
            if review_types and not any(item.review_type in review_types for item in items):
                continue
            if problem_tag and not any(problem_tag in (item.problem_tags or []) for item in items):
                continue
            if completion_result and self._completion_result(items) != completion_result:
                continue
            filtered_packages.append(package)

        total = len(filtered_packages)
        offset = (page - 1) * page_size
        paged_packages = filtered_packages[offset : offset + page_size]
        unit_service = KnowledgeUnitService(self.session)
        quality_service = QualityGateService(self.session)
        for package in paged_packages:
            await unit_service.ensure_for_package(package)
            if package.quality_computed_at is None:
                await quality_service.evaluate_package(package.package_id)
        if paged_packages:
            page_items = await self._items_by_package([package.package_id for package in paged_packages])
            items_by_package.update(page_items)
        return {
            "items": [
                self._package_summary(package, items_by_package[package.package_id]) for package in paged_packages
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "counts": await self._counts(source_id, operator_id),
        }

    async def get_package(self, package_id: str) -> dict:
        package = await self._load_package(package_id)
        await KnowledgeUnitService(self.session).ensure_for_package(package)
        quality_result = await QualityGateService(self.session).evaluate_package(package.package_id)
        items = await self._load_items(package.package_id)
        display_items = self._display_items(items)
        source_row = None
        if package.source_version_id:
            source_row = (
                await self.session.execute(
                    select(FeishuMaterialVersion, FeishuSourceItem, FeishuSource)
                    .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                    .join(FeishuSource, FeishuSource.source_id == FeishuSourceItem.source_id)
                    .where(FeishuMaterialVersion.version_id == package.source_version_id)
                )
            ).one_or_none()
        item_ids = [item.review_item_id for item in items]
        reopened_by_item_id: dict[str, str] = {}
        if item_ids:
            reopened_items = list(
                await self.session.scalars(
                    select(FeishuReviewItem).where(FeishuReviewItem.reopened_from_item_id.in_(item_ids))
                )
            )
            reopened_by_item_id = {
                item.reopened_from_item_id: item.review_item_id for item in reopened_items if item.reopened_from_item_id
            }
        relation_ids = sorted({relation_id for item in items for relation_id in (item.relation_ids or [])})
        change_requests = []
        if item_ids:
            change_requests = list(
                await self.session.scalars(
                    select(FeishuSourceChangeRequest)
                    .where(FeishuSourceChangeRequest.review_item_id.in_(item_ids))
                    .order_by(FeishuSourceChangeRequest.created_at.desc())
                )
            )
        relations = []
        if relation_ids:
            relations = list(
                await self.session.scalars(
                    select(FeishuCrossDocumentRelation).where(
                        FeishuCrossDocumentRelation.relation_id.in_(relation_ids),
                        FeishuCrossDocumentRelation.status != "invalidated",
                    )
                )
            )
        relation_source_ids = {
            version_id
            for relation in relations
            for version_id in (relation.source_version_id, relation.target_version_id)
        }
        relation_sources = {}
        if relation_source_ids:
            relation_sources = {
                version.version_id: (version, source_item)
                for version, source_item in (
                    await self.session.execute(
                        select(FeishuMaterialVersion, FeishuSourceItem)
                        .join(FeishuSourceItem, FeishuSourceItem.item_id == FeishuMaterialVersion.item_id)
                        .where(FeishuMaterialVersion.version_id.in_(relation_source_ids))
                    )
                ).all()
            }
        event_scope = []
        if package.source_version_id:
            event_scope.append(FeishuProcessingEvent.version_id == package.source_version_id)
        if package.source_item_id:
            event_scope.append(FeishuProcessingEvent.item_id == package.source_item_id)
        events = []
        if event_scope:
            events = list(
                await self.session.scalars(
                    select(FeishuProcessingEvent)
                    .where(
                        FeishuProcessingEvent.source_id == package.source_id,
                        or_(*event_scope),
                    )
                    .order_by(FeishuProcessingEvent.created_at.desc())
                )
            )
        source_version, source_item, source = source_row or (None, None, None)
        previous_version = None
        has_update_item = any(item.review_type == ReviewType.UPDATE for item in display_items)
        if (
            has_update_item
            and source_item
            and source_item.active_version_id
            and source_item.active_version_id != package.source_version_id
        ):
            previous_version = await self.session.scalar(
                select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == source_item.active_version_id)
            )
        elif has_update_item and source_item and package.source_version_id:
            previous_version = await self.session.scalar(
                select(FeishuMaterialVersion)
                .where(
                    FeishuMaterialVersion.item_id == source_item.item_id,
                    FeishuMaterialVersion.version_id != package.source_version_id,
                    FeishuMaterialVersion.yuxi_file_id.is_not(None),
                    FeishuMaterialVersion.published_at.is_not(None),
                )
                .order_by(
                    FeishuMaterialVersion.published_at.desc().nullslast(),
                    FeishuMaterialVersion.created_at.desc(),
                )
                .limit(1)
            )
        return {
            **self._package_summary(package, display_items),
            **quality_result,
            "source_item_id": package.source_item_id,
            "source_version_id": package.source_version_id,
            "source_url": package.source_url_snapshot,
            "target_kb_id": source.target_kb_id if source else None,
            "item_type": source_item.item_type if source_item else None,
            "revision": source_version.revision if source_version else None,
            "yuxi_file_id": source_version.yuxi_file_id if source_version else None,
            "chunk_count": source_version.chunk_count if source_version else 0,
            "token_count": source_version.token_count if source_version else 0,
            "processing_status": source_version.processing_status if source_version else None,
            "review_status": source_version.review_status if source_version else None,
            "content_quality": ((source_version.processing_params or {}).get("content_quality") or {})
            if source_version
            else {},
            "previous_version": {
                "version_id": previous_version.version_id,
                "revision": previous_version.revision,
                "yuxi_file_id": previous_version.yuxi_file_id,
                "chunk_count": previous_version.chunk_count or 0,
                "token_count": previous_version.token_count or 0,
                "published_at": _iso(previous_version.published_at),
            }
            if previous_version
            else None,
            "draft": package.draft_json or {},
            "lock_version": package.lock_version,
            "items": [
                self._item_dict(item, reopened_by_item_id=reopened_by_item_id.get(item.review_item_id))
                for item in display_items
            ],
            "relations": [self._relation_dict(relation, relation_sources) for relation in relations],
            "change_requests": [self._change_request_dict(request) for request in change_requests],
            "events": [self._event_dict(event) for event in events],
        }

    async def save_draft(
        self,
        package_id: str,
        *,
        operator_id: str,
        lock_version: int,
        draft: dict,
    ) -> dict:
        package = await self._load_package(package_id, lock=True)
        self._assert_not_terminal(package)
        self._claim_or_assert_assignee(package, operator_id)
        self._check_lock_version(package, lock_version)
        package.draft_json = draft
        package.lock_version += 1
        self._append_event(package, "review_draft_saved", operator_id=operator_id)
        await self.session.flush()
        return {"package_id": package.package_id, "draft": package.draft_json, "lock_version": package.lock_version}

    async def save_layout_edit(
        self,
        package_id: str,
        *,
        operator_id: str,
        lock_version: int,
        block_id: str,
        page_number: int,
        content: str,
        source_segment_ids: list[str] | None = None,
    ) -> dict:
        """Persist a visual edit as a review draft; the Feishu source stays read-only."""
        package = await self._load_package(package_id, lock=True)
        self._assert_not_terminal(package)
        self._claim_or_assert_assignee(package, operator_id)
        self._check_lock_version(package, lock_version)
        draft = dict(package.draft_json or {})
        edits = dict(draft.get("layout_edits") or {})
        edits[block_id] = {
            "block_id": block_id,
            "page_number": page_number,
            "content": content,
            "source_segment_ids": list(source_segment_ids or []),
            "edited_by": operator_id,
            "edited_at": utc_now_naive().isoformat(),
        }
        draft["layout_edits"] = edits
        package.draft_json = draft
        package.lock_version += 1
        self._append_event(
            package,
            "review_layout_edit_saved",
            operator_id=operator_id,
            payload={"block_id": block_id, "page_number": page_number},
        )
        await self.session.flush()
        return {"package_id": package.package_id, "draft": package.draft_json, "lock_version": package.lock_version}

    async def transfer(
        self,
        package_id: str,
        *,
        operator_id: str,
        lock_version: int,
        assignee_id: str,
        comment: str,
    ) -> dict:
        package = await self._load_package(package_id, lock=True)
        self._assert_not_terminal(package)
        self._claim_or_assert_assignee(package, operator_id)
        self._check_lock_version(package, lock_version)
        assignee = await self.session.scalar(
            select(User).where(
                User.uid == assignee_id,
                User.role.in_({"admin", "superadmin"}),
                User.is_deleted == 0,
            )
        )
        if assignee is None:
            raise ValueError("Assignee is not an active knowledge administrator")
        previous_assignee = package.assignee_id
        package.assignee_id = assignee_id
        package.lock_version += 1
        if package.source_version_id:
            legacy_review = await self.session.scalar(
                select(FeishuGovernanceReview).where(FeishuGovernanceReview.version_id == package.source_version_id)
            )
            if legacy_review is not None:
                legacy_review.assignee_id = assignee_id
                legacy_review.status = "pending"
        self._append_event(
            package,
            "review_package_transferred",
            operator_id=operator_id,
            message=comment,
            payload={"from_assignee_id": previous_assignee, "assignee_id": assignee_id},
        )
        await NotificationService(self.session).notify_admins(
            object_type="REVIEW_PACKAGE",
            object_id=package.package_id,
            assignee_id=assignee_id,
            event_key="review-package-transferred",
            title="审核包已指派给你",
            body=f"{package.title_snapshot or '未命名审核包'}：{comment}",
            feishu=True,
        )
        await self.session.flush()
        return {
            "package_id": package.package_id,
            "source_item_id": package.source_item_id,
            "source_version_id": package.source_version_id,
            "assignee_id": package.assignee_id,
            "lock_version": package.lock_version,
        }

    async def resolve(
        self,
        package_id: str,
        payload: ReviewPackageResolveRequest,
        *,
        operator_id: str,
        automated: bool = False,
    ) -> dict:
        package = await self._load_package(package_id, lock=True)
        self._assert_not_terminal(package)
        if not automated:
            self._claim_or_assert_assignee(package, operator_id)
        items = await self._load_items(package.package_id, lock=True)
        items_by_id = {item.review_item_id: item for item in items}

        replay_items = [items_by_id.get(decision.review_item_id) for decision in payload.decisions]
        if all(
            item is not None and (item.decision_payload or {}).get("request_id") == payload.request_id
            for item in replay_items
        ):
            return {
                "package_id": package.package_id,
                "workflow_status": package.workflow_status,
                "lock_version": package.lock_version,
                "publish_version_ids": [],
                "unit_publish_version_ids": [],
                "reject_candidates": [],
                **self._unit_progress(items),
                "affected_unit_titles": [],
                "counterpart_actions": [],
                "idempotent_replay": True,
            }

        self._check_lock_version(package, payload.lock_version)
        missing_ids = [
            decision.review_item_id for decision in payload.decisions if decision.review_item_id not in items_by_id
        ]
        if missing_ids:
            raise LookupError(f"Review items do not belong to package: {', '.join(missing_ids)}")
        if len(payload.decisions) > 1 and any(
            items_by_id[decision.review_item_id].review_type == ReviewType.CONFLICT for decision in payload.decisions
        ):
            raise ValueError("Conflict review items must be decided individually")

        now = utc_now_naive()
        affected_unit_titles: list[str] = []
        counterpart_actions: list[dict] = []
        unit_publish_requested = False
        layout_edits = dict((package.draft_json or {}).get("layout_edits") or {})
        for decision in payload.decisions:
            item = items_by_id[decision.review_item_id]
            if item.item_status not in {ReviewItemStatus.PENDING, ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION}:
                raise ValueError(f"Review item is not actionable: {item.review_item_id}")
            action = self._validate_decision(item, decision)
            await SourceChangeService(self.session).fulfill_received_requests(
                item,
                operator_id=operator_id,
                now=now,
            )
            scope = decision.applicability_scope.model_dump(mode="json", exclude_none=True)
            item.outcome = decision.outcome
            item.internal_action = action
            item.problem_tags = [tag.value for tag in decision.problem_tags]
            item.applicability_scope = scope
            item.decision_comment = decision.decision_comment
            item.decision_payload = {
                "request_id": payload.request_id,
                "outcome": decision.outcome,
                "problem_tags": item.problem_tags,
                "decision_comment": decision.decision_comment,
                "applicability_scope": scope,
                "layout_edits": layout_edits,
                "automated": automated,
            }
            item.decided_by = operator_id
            item.decided_at = now

            if decision.outcome in {
                ReviewOutcome.REQUEST_SOURCE_CHANGE,
                ReviewOutcome.REQUEST_SUPPORTING_SOURCE,
            }:
                item.item_status = ReviewItemStatus.WAITING_SOURCE_CHANGE
                await self._create_change_request(package, item, decision, operator_id=operator_id, now=now)
            elif decision.outcome == ReviewOutcome.WAIT_BUSINESS_CONFIRMATION:
                item.item_status = ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION
            else:
                item.item_status = ReviewItemStatus.DECIDED
                if item.review_type == ReviewType.CONFLICT:
                    counterpart_actions.extend(
                        await self._resolve_conflict_relations(
                            package,
                            item,
                            decision.outcome,
                            applicability_scope=scope,
                            operator_id=operator_id,
                            now=now,
                        )
                    )
                else:
                    await self._resolve_item_relations(item, operator_id=operator_id, now=now)

            await KnowledgeUnitService(self.session).apply_decision(
                item,
                decision.outcome,
                applicability_scope=scope if decision.outcome == ReviewOutcome.SPLIT_SCOPE else None,
            )
            if item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT:
                affected_unit_titles.append(item.title or "未命名知识单元")
                unit_publish_requested = unit_publish_requested or decision.outcome in UNIT_PUBLISH_OUTCOMES

            await self._update_legacy_review(package, item, operator_id=operator_id, now=now)
            self._append_event(
                package,
                "review_item_decided",
                operator_id=operator_id,
                message=decision.decision_comment,
                payload={
                    "review_item_id": item.review_item_id,
                    "outcome": decision.outcome,
                    "internal_action": action,
                    "request_id": payload.request_id,
                    "automated": automated,
                },
            )

        if any(decision.outcome in UNIT_PUBLISH_OUTCOMES for decision in payload.decisions):
            quality_result = await QualityGateService(self.session).evaluate_package(package.package_id)
            blockers = quality_result["qualityGate"]["blockers"]
            if blockers:
                raise ValueError("质量门禁未通过：" + "；".join(blocker["message"] for blocker in blockers))

        previous_status = package.workflow_status
        package.workflow_status = self._aggregate_status(items)
        package.completed_at = now if package.workflow_status == ReviewPackageStatus.COMPLETED else None
        package.draft_json = {}
        package.lock_version += 1

        publish_version_ids: set[str] = set()
        unit_publish_version_ids: set[str] = set()
        reject_candidates: dict[str, str] = {}
        knowledge_unit_package = any(item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT for item in items)
        if knowledge_unit_package and unit_publish_requested and package.source_version_id:
            unit_publish_version_ids.add(package.source_version_id)
        if package.workflow_status == ReviewPackageStatus.COMPLETED and package.source_version_id:
            publish_items = [item for item in items if item.outcome in UNIT_PUBLISH_OUTCOMES]
            rejected_items = [item for item in items if item.outcome in REJECT_CANDIDATE_OUTCOMES]
            if publish_items and rejected_items and not knowledge_unit_package:
                raise ValueError("One source-version package cannot both publish and reject the same material")
            if publish_items:
                if not knowledge_unit_package:
                    publish_version_ids.add(package.source_version_id)
            elif rejected_items:
                reject_item = rejected_items[0]
                reject_candidates[package.source_version_id] = reject_item.decision_comment or self._outcome_label(
                    reject_item.outcome
                )
            if knowledge_unit_package:
                await self._complete_unit_legacy_review(
                    package,
                    items,
                    publish=bool(publish_items),
                    operator_id=operator_id,
                    now=now,
                )

        if package.workflow_status == ReviewPackageStatus.COMPLETED and previous_status != package.workflow_status:
            self._append_event(package, "review_package_completed", operator_id=operator_id)
        await self.session.flush()
        return {
            "package_id": package.package_id,
            "workflow_status": package.workflow_status,
            "lock_version": package.lock_version,
            "publish_version_ids": sorted(publish_version_ids),
            "unit_publish_version_ids": sorted(unit_publish_version_ids),
            "reject_candidates": [
                {"version_id": version_id, "reason": reason} for version_id, reason in reject_candidates.items()
            ],
            **self._unit_progress(items),
            "affected_unit_titles": affected_unit_titles,
            "counterpart_actions": counterpart_actions,
            "idempotent_replay": False,
        }

    async def bulk_exclude(
        self,
        package_id: str,
        payload: ReviewPackageBulkExcludeRequest,
        *,
        operator_id: str,
    ) -> dict:
        package = await self._load_package(package_id, lock=True)
        self._claim_or_assert_assignee(package, operator_id)
        items = await self._load_items(package.package_id, lock=True)
        items_by_id = {item.review_item_id: item for item in items}
        target_items = [items_by_id.get(item_id) for item_id in payload.review_item_ids]

        if all(
            item is not None
            and item.outcome == ReviewOutcome.EXCLUDE
            and (item.decision_payload or {}).get("request_id") == payload.request_id
            for item in target_items
        ):
            return {
                "package_id": package.package_id,
                "workflow_status": package.workflow_status,
                "lock_version": package.lock_version,
                "publish_version_ids": [],
                "unit_publish_version_ids": [],
                "reject_candidates": [],
                **self._unit_progress(items),
                "affected_unit_titles": [],
                "closed_change_request_count": 0,
                "idempotent_replay": True,
            }

        self._assert_not_terminal(package)
        self._check_lock_version(package, payload.lock_version)
        missing_ids = [item_id for item_id, item in zip(payload.review_item_ids, target_items) if item is None]
        if missing_ids:
            raise LookupError(f"Review items do not belong to package: {', '.join(missing_ids)}")

        eligible_statuses = {
            ReviewItemStatus.PENDING,
            ReviewItemStatus.WAITING_SOURCE_CHANGE,
            ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION,
        }
        for item in target_items:
            cancelled_source_change = (
                item.item_status == ReviewItemStatus.INVALIDATED and item.outcome == ReviewOutcome.REQUEST_SOURCE_CHANGE
            )
            if (
                item.subject_type != ReviewSubjectType.KNOWLEDGE_UNIT
                or ReviewOutcome.EXCLUDE not in OUTCOME_ACTIONS.get(item.review_type, {})
                or (item.item_status not in eligible_statuses and not cancelled_source_change)
            ):
                raise ValueError(f"Review item cannot be batch excluded: {item.review_item_id}")

        now = utc_now_naive()
        active_requests = list(
            await self.session.scalars(
                select(FeishuSourceChangeRequest)
                .where(
                    FeishuSourceChangeRequest.review_item_id.in_(payload.review_item_ids),
                    FeishuSourceChangeRequest.status.in_(
                        {
                            SourceChangeRequestStatus.OPEN,
                            SourceChangeRequestStatus.NEW_VERSION_RECEIVED,
                        }
                    ),
                )
                .with_for_update()
            )
        )
        active_item_ids = {request.review_item_id for request in active_requests}
        waiting_without_request = [
            item.review_item_id
            for item in target_items
            if item.item_status == ReviewItemStatus.WAITING_SOURCE_CHANGE and item.review_item_id not in active_item_ids
        ]
        if waiting_without_request:
            raise ValueError(
                "Waiting review items have no active source-change request: " + ", ".join(waiting_without_request)
            )

        requests_by_item: dict[str, list[str]] = defaultdict(list)
        for request in active_requests:
            previous_status = request.status
            request.status = SourceChangeRequestStatus.CANCELLED
            request.resolved_at = now
            request.updated_at = now
            requests_by_item[request.review_item_id].append(request.change_request_id)
            self.session.add(
                FeishuProcessingEvent(
                    source_id=package.source_id,
                    item_id=package.source_item_id,
                    version_id=package.source_version_id,
                    event_type="source_change_request_cancelled",
                    from_status=previous_status,
                    to_status=SourceChangeRequestStatus.CANCELLED,
                    operator_id=operator_id,
                    message=payload.decision_comment,
                    payload_json={
                        "package_id": package.package_id,
                        "review_item_id": request.review_item_id,
                        "change_request_id": request.change_request_id,
                        "reason": "batch_excluded",
                    },
                )
            )

        affected_unit_titles: list[str] = []
        for item in target_items:
            item.outcome = ReviewOutcome.EXCLUDE
            item.internal_action = OUTCOME_ACTIONS[item.review_type][ReviewOutcome.EXCLUDE]
            item.decision_comment = payload.decision_comment
            item.decision_payload = {
                "request_id": payload.request_id,
                "outcome": ReviewOutcome.EXCLUDE,
                "ended_change_request_ids": requests_by_item[item.review_item_id],
            }
            item.item_status = ReviewItemStatus.DECIDED
            item.decided_by = operator_id
            item.decided_at = now
            item.updated_at = now
            await KnowledgeUnitService(self.session).apply_decision(item, ReviewOutcome.EXCLUDE)
            await self._resolve_item_relations(item, operator_id=operator_id, now=now)
            affected_unit_titles.append(item.title or "未命名知识单元")
            self._append_event(
                package,
                "review_item_decided",
                operator_id=operator_id,
                message=payload.decision_comment,
                payload={
                    "review_item_id": item.review_item_id,
                    "outcome": ReviewOutcome.EXCLUDE,
                    "internal_action": item.internal_action,
                    "request_id": payload.request_id,
                    "ended_change_request_count": len(requests_by_item[item.review_item_id]),
                },
            )

        previous_status = package.workflow_status
        package.workflow_status = self._aggregate_status(items)
        package.completed_at = now if package.workflow_status == ReviewPackageStatus.COMPLETED else None
        package.draft_json = {}
        package.lock_version += 1

        reject_candidates: list[dict[str, str]] = []
        if package.workflow_status == ReviewPackageStatus.COMPLETED and package.source_version_id:
            publish_items = [item for item in items if item.outcome in UNIT_PUBLISH_OUTCOMES]
            rejected_items = [item for item in items if item.outcome in REJECT_CANDIDATE_OUTCOMES]
            await self._complete_unit_legacy_review(
                package,
                items,
                publish=bool(publish_items),
                operator_id=operator_id,
                now=now,
            )
            if not publish_items and rejected_items:
                reject_candidates.append(
                    {
                        "version_id": package.source_version_id,
                        "reason": payload.decision_comment,
                    }
                )
        if package.workflow_status == ReviewPackageStatus.COMPLETED and previous_status != package.workflow_status:
            self._append_event(package, "review_package_completed", operator_id=operator_id)

        await self.session.flush()
        return {
            "package_id": package.package_id,
            "workflow_status": package.workflow_status,
            "lock_version": package.lock_version,
            "publish_version_ids": [],
            "unit_publish_version_ids": [],
            "reject_candidates": reject_candidates,
            **self._unit_progress(items),
            "affected_unit_titles": affected_unit_titles,
            "closed_change_request_count": len(active_requests),
            "idempotent_replay": False,
        }

    async def reopen_excluded_item(self, review_item_id: str, *, operator_id: str) -> dict:
        original = await self.session.scalar(
            select(FeishuReviewItem).where(FeishuReviewItem.review_item_id == review_item_id).with_for_update()
        )
        if original is None:
            raise LookupError(f"Review item not found: {review_item_id}")

        existing = await self.session.scalar(
            select(FeishuReviewItem).where(FeishuReviewItem.reopened_from_item_id == review_item_id)
        )
        if existing is not None:
            existing_package = await self._load_package(existing.package_id)
            return {
                "package_id": existing.package_id,
                "review_item_id": existing.review_item_id,
                "workflow_status": existing_package.workflow_status,
                "idempotent_replay": True,
            }

        if (
            original.subject_type != ReviewSubjectType.KNOWLEDGE_UNIT
            or original.item_status != ReviewItemStatus.DECIDED
            or original.outcome != ReviewOutcome.EXCLUDE
        ):
            raise ValueError("Only excluded knowledge units can be reopened")

        original_package = await self._load_package(original.package_id)
        unit = await self.session.scalar(
            select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.unit_id == original.subject_id).with_for_update()
        )
        if unit is None:
            raise LookupError(f"Knowledge unit not found: {original.subject_id}")
        if unit.publication_state not in {"EXCLUDED", "ALIAS"}:
            raise ValueError("Knowledge unit is no longer excluded")

        package_id = stable_review_id("review-package-reopen", original.review_item_id)
        package = FeishuReviewPackage(
            package_id=package_id,
            package_key=f"reopen-exclusion:{original.review_item_id}",
            source_id=original_package.source_id,
            source_item_id=original_package.source_item_id,
            source_version_id=original_package.source_version_id,
            trigger_type=ReviewTriggerType.FEEDBACK,
            title_snapshot=original_package.title_snapshot,
            path_snapshot=original_package.path_snapshot,
            source_url_snapshot=original_package.source_url_snapshot,
            workflow_status=ReviewPackageStatus.OPEN,
            assignee_id=operator_id,
            risk_level=original_package.risk_level,
            draft_json={},
            lock_version=1,
        )
        self.session.add(package)
        await self.session.flush([package])

        evidence = dict(original.evidence_json or {})
        recommended_outcome = (
            ReviewOutcome.ADOPT_NEW_VERSION if original.review_type == ReviewType.UPDATE else ReviewOutcome.PUBLISH
        )
        evidence.update(
            {
                "reopened_exclusion": True,
                "recommended_outcome": recommended_outcome,
                "recommendation_reason": "已重新申请纳入，请重新审核。",
                "manual_review_required": True,
            }
        )
        reopened = FeishuReviewItem(
            review_item_id=stable_review_id("review-item-reopen", original.review_item_id),
            package_id=package.package_id,
            candidate_key=f"reopen:{original.review_item_id}",
            review_type=original.review_type,
            subject_type=original.subject_type,
            subject_id=original.subject_id,
            title=original.title,
            summary="已重新申请纳入，请重新审核。",
            subject_locator_json=dict(original.subject_locator_json or {}),
            evidence_json=evidence,
            relation_ids=list(original.relation_ids or []),
            problem_tags=list(original.problem_tags or []),
            applicability_scope=dict(original.applicability_scope or {}),
            item_status=ReviewItemStatus.PENDING,
            decision_payload={},
            reopened_from_item_id=original.review_item_id,
        )
        self.session.add(reopened)

        await NotificationService(self.session).notify_admins(
            object_type="REVIEW_PACKAGE",
            object_id=package.package_id,
            assignee_id=operator_id,
            event_key="review-package-reopened",
            title="知识单元重新申请纳入",
            body=f"{package.title_snapshot or '未命名资料'} 已重新进入审核。",
            feishu=True,
        )

        segment_ids = list(unit.source_segment_ids or [])
        if segment_ids:
            segments = list(
                await self.session.scalars(
                    select(FeishuSourceSegment).where(FeishuSourceSegment.segment_id.in_(segment_ids)).with_for_update()
                )
            )
            for segment in segments:
                if segment.publication_state in {"EXCLUDED", "ALIAS"}:
                    segment.publication_state = "PENDING"
        unit.publication_state = "PENDING"

        self._append_event(
            package,
            "review_item_reopened",
            operator_id=operator_id,
            message="已重新申请纳入知识库",
            payload={
                "review_item_id": reopened.review_item_id,
                "reopened_from_item_id": original.review_item_id,
                "reason": "reopen_exclusion",
            },
        )
        await self.session.flush()
        return {
            "package_id": package.package_id,
            "review_item_id": reopened.review_item_id,
            "workflow_status": package.workflow_status,
            "idempotent_replay": False,
        }

    def _validate_decision(self, item: FeishuReviewItem, decision: ReviewItemDecisionRequest) -> str | None:
        actions = OUTCOME_ACTIONS.get(item.review_type)
        if actions is None or decision.outcome not in actions:
            raise ValueError(f"Outcome {decision.outcome} is not allowed for review type {item.review_type}")
        if any(tag in PROCESSING_ONLY_TAGS for tag in decision.problem_tags):
            raise ValueError("Processing failures cannot be submitted as knowledge-review problem tags")
        if decision.outcome in COMMENT_REQUIRED_OUTCOMES and not decision.decision_comment:
            raise ValueError("decision_comment is required for this outcome")
        scope = decision.applicability_scope.model_dump(mode="json", exclude_none=True)
        if decision.outcome == ReviewOutcome.SPLIT_SCOPE and not scope:
            raise ValueError("applicability_scope is required when splitting scope")
        return actions[decision.outcome]

    async def _create_change_request(
        self,
        package: FeishuReviewPackage,
        item: FeishuReviewItem,
        decision: ReviewItemDecisionRequest,
        *,
        operator_id: str,
        now: datetime,
    ) -> None:
        max_round = await self.session.scalar(
            select(func.max(FeishuSourceChangeRequest.round_number)).where(
                or_(
                    FeishuSourceChangeRequest.review_item_id == item.review_item_id,
                    FeishuSourceChangeRequest.received_version_id == item.subject_id,
                )
            )
        )
        round_number = int(max_round or 0) + 1
        request = FeishuSourceChangeRequest(
            change_request_id=stable_review_id("change-request", f"{item.review_item_id}:{round_number}"),
            review_item_id=item.review_item_id,
            source_item_id=package.source_item_id,
            requested_version_id=package.source_version_id,
            source_url=package.source_url_snapshot,
            status=SourceChangeRequestStatus.OPEN,
            request_text=decision.decision_comment or "请修改飞书原文",
            responsible_user_id=decision.responsible_user_id,
            responsible_user_name=decision.responsible_user_name,
            round_number=round_number,
            created_by=operator_id,
            created_at=now,
            updated_at=now,
        )
        self.session.add(request)
        self._append_event(
            package,
            "source_change_requested",
            operator_id=operator_id,
            message=decision.decision_comment,
            payload={
                "review_item_id": item.review_item_id,
                "change_request_id": request.change_request_id,
                "round_number": round_number,
            },
        )

    async def _resolve_item_relations(self, item: FeishuReviewItem, *, operator_id: str, now: datetime) -> None:
        relation_ids = list(item.relation_ids or [])
        if not relation_ids:
            return
        relations = list(
            await self.session.scalars(
                select(FeishuCrossDocumentRelation).where(
                    FeishuCrossDocumentRelation.relation_id.in_(relation_ids),
                    FeishuCrossDocumentRelation.status == "open",
                )
            )
        )
        version_ids: set[str] = set()
        for relation in relations:
            relation.status = "resolved"
            relation.human_decision = item.internal_action or item.outcome
            relation.human_comment = item.decision_comment
            relation.resolved_by = operator_id
            relation.resolved_at = now
            version_ids.update(
                version_id for version_id in (relation.source_version_id, relation.target_version_id) if version_id
            )
        await self._invalidate_quality_cache_for_versions(version_ids)

    async def _resolve_conflict_relations(
        self,
        package: FeishuReviewPackage,
        item: FeishuReviewItem,
        outcome: str,
        *,
        applicability_scope: dict | None = None,
        operator_id: str,
        now: datetime,
    ) -> list[dict]:
        """Apply a conflict decision to both sides before closing the relation.

        A relation is not sufficient as a decision record by itself: adopting a
        candidate must take the conflicting published unit out of retrieval,
        while keeping the current version must leave that published unit intact.
        Scope splits retain both units and persist the candidate's scope on the
        knowledge-unit row.
        """
        relation_ids = list(item.relation_ids or [])
        if not relation_ids:
            return []
        relations = list(
            await self.session.scalars(
                select(FeishuCrossDocumentRelation)
                .where(
                    FeishuCrossDocumentRelation.relation_id.in_(relation_ids),
                    FeishuCrossDocumentRelation.status.in_({"open", "pending"}),
                )
                .with_for_update()
            )
        )
        actions: list[dict] = []
        version_ids: set[str] = set()
        for relation in relations:
            version_ids.update(
                version_id for version_id in (relation.source_version_id, relation.target_version_id) if version_id
            )
            counterpart_version_id = (
                relation.target_version_id
                if relation.source_version_id == package.source_version_id
                else relation.source_version_id
            )
            counterpart = await self._find_counterpart_unit(
                counterpart_version_id,
                relation,
                exclude_unit_id=item.subject_id,
            )
            if outcome == ReviewOutcome.KEEP_CURRENT and counterpart is not None:
                # If both sides are only candidates there is no defensible
                # "current" version to keep; require an explicit adoption.
                if counterpart.publication_state != "INCLUDED":
                    raise ValueError("无法保留当前版本：冲突另一侧尚未发布，请采用新版或等待业务确认")
            elif outcome == ReviewOutcome.SPLIT_SCOPE and counterpart is not None:
                counterpart_scope = await self._counterpart_scope(counterpart)
                if self._scopes_overlap(applicability_scope or {}, counterpart_scope):
                    raise ValueError("适用范围与冲突另一侧重叠，请填写互斥的行业、产品或版本范围")
            elif outcome == ReviewOutcome.ADOPT_NEW_VERSION and counterpart is not None:
                await self._supersede_counterpart_unit(counterpart, operator_id=operator_id, now=now)
                await self._close_counterpart_reviews(counterpart, operator_id=operator_id, now=now)
                actions.append(
                    {
                        "relation_id": relation.relation_id,
                        "unit_id": counterpart.unit_id,
                        "title": counterpart.title,
                        "action": "EXCLUDED",
                        "message": "已将冲突的另一侧知识移出正式检索",
                    }
                )
                self._append_event(
                    package,
                    "conflict_counterpart_superseded",
                    operator_id=operator_id,
                    message="采用新版并移出冲突的另一侧知识",
                    payload={
                        "review_item_id": item.review_item_id,
                        "relation_id": relation.relation_id,
                        "counterpart_unit_id": counterpart.unit_id,
                        "counterpart_version_id": counterpart.version_id,
                    },
                )
            relation.status = "resolved"
            relation.human_decision = outcome
            relation.human_comment = item.decision_comment
            relation.resolved_by = operator_id
            relation.resolved_at = now
        await self._invalidate_quality_cache_for_versions(version_ids)
        return actions

    async def _invalidate_quality_cache_for_versions(self, version_ids: set[str]) -> None:
        """Force quality to be recomputed after a cross-document decision.

        Relation status and counterpart publication state are part of the gate
        calculation.  Clearing the cached result for both sides prevents stale
        BLOCKED/RECOMMENDED badges in list views until the next detail request.
        """
        version_ids = {version_id for version_id in version_ids if version_id}
        if not version_ids:
            return
        packages = list(
            await self.session.scalars(
                select(FeishuReviewPackage)
                .where(FeishuReviewPackage.source_version_id.in_(version_ids))
                .with_for_update()
            )
        )
        for package in packages:
            package.quality_gate_status = None
            package.quality_score = None
            package.quality_dimensions = {}
            package.impact_summary = {}
            package.auto_close_eligible = False
            package.quality_computed_at = None

    async def _counterpart_scope(self, unit: FeishuKnowledgeUnit) -> dict:
        scope = dict(unit.applicability_scope or {})
        if scope:
            return scope
        version = await self.session.scalar(
            select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == unit.version_id)
        )
        return dict((version.processing_params or {}).get("applicability_scope") or {}) if version else {}

    @staticmethod
    def _scopes_overlap(left: dict, right: dict) -> bool:
        """Return true unless at least one shared dimension proves disjointness."""
        left = {key: str(value).strip() for key, value in left.items() if str(value or "").strip()}
        right = {key: str(value).strip() for key, value in right.items() if str(value or "").strip()}
        shared = set(left) & set(right)
        return not any(left[key] != right[key] for key in shared)

    async def _find_counterpart_unit(
        self,
        version_id: str | None,
        relation: FeishuCrossDocumentRelation,
        *,
        exclude_unit_id: str | None = None,
    ) -> FeishuKnowledgeUnit | None:
        if not version_id:
            return None
        units = list(
            await self.session.scalars(
                select(FeishuKnowledgeUnit)
                .where(
                    FeishuKnowledgeUnit.version_id == version_id,
                    FeishuKnowledgeUnit.status == "ACTIVE",
                )
                .with_for_update()
            )
        )
        candidates = [unit for unit in units if unit.unit_id != exclude_unit_id]
        if not candidates:
            return None
        evidence = KnowledgeUnitService._relation_evidence(relation)
        if not evidence:
            return candidates[0]
        state_priority = {"INCLUDED": 2, "PENDING": 1}
        return max(
            candidates,
            key=lambda unit: (
                state_priority.get(unit.publication_state, 0),
                KnowledgeUnitService._evidence_score(unit.content, evidence),
            ),
        )

    async def _supersede_counterpart_unit(
        self,
        unit: FeishuKnowledgeUnit,
        *,
        operator_id: str,
        now: datetime,
    ) -> None:
        unit.publication_state = "EXCLUDED"
        unit.lifecycle_status = "OFFLINE"
        unit.lifecycle_note = "被跨文档冲突裁决替代，已移出正式检索。"
        unit.lifecycle_updated_by = operator_id
        unit.lifecycle_updated_at = now
        segments = list(
            await self.session.scalars(
                select(FeishuSourceSegment)
                .where(FeishuSourceSegment.segment_id.in_(list(unit.source_segment_ids or [])))
                .with_for_update()
            )
        )
        for segment in segments:
            if segment.publication_state in {"INCLUDED", "PENDING", "ALIAS"}:
                segment.publication_state = "EXCLUDED"

    async def _close_counterpart_reviews(
        self,
        unit: FeishuKnowledgeUnit,
        *,
        operator_id: str,
        now: datetime,
    ) -> None:
        """Prevent a superseded candidate review from being published later."""
        items = list(
            await self.session.scalars(
                select(FeishuReviewItem)
                .where(
                    FeishuReviewItem.subject_id == unit.unit_id,
                    FeishuReviewItem.item_status.in_(
                        {
                            ReviewItemStatus.PENDING,
                            ReviewItemStatus.WAITING_SOURCE_CHANGE,
                            ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION,
                        }
                    ),
                )
                .with_for_update()
            )
        )
        if not items:
            return
        package_ids = {item.package_id for item in items}
        for item in items:
            item.item_status = ReviewItemStatus.DECIDED
            item.outcome = ReviewOutcome.KEEP_CURRENT
            item.internal_action = ReviewAction.KEEP_CURRENT
            item.decision_comment = "该知识已被另一版本的冲突裁决替代，不再纳入知识库。"
            item.decision_payload = {
                **dict(item.decision_payload or {}),
                "superseded_by_conflict": True,
                "superseded_unit_id": unit.unit_id,
            }
            item.decided_by = operator_id
            item.decided_at = now
        for package_id in package_ids:
            counterpart_package = await self._load_package(package_id, lock=True)
            counterpart_items = await self._load_items(package_id, lock=True)
            previous_status = counterpart_package.workflow_status
            counterpart_package.workflow_status = self._aggregate_status(counterpart_items)
            counterpart_package.completed_at = (
                now if counterpart_package.workflow_status == ReviewPackageStatus.COMPLETED else None
            )
            counterpart_package.lock_version += 1
            self._append_event(
                counterpart_package,
                "conflict_review_superseded",
                operator_id=operator_id,
                message="该审核项已被另一版本的冲突裁决替代",
                payload={
                    "unit_id": unit.unit_id,
                    "previous_workflow_status": previous_status,
                    "workflow_status": counterpart_package.workflow_status,
                },
            )

    async def _update_legacy_review(
        self,
        package: FeishuReviewPackage,
        item: FeishuReviewItem,
        *,
        operator_id: str,
        now: datetime,
    ) -> None:
        if not package.source_version_id:
            return
        if item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT and item.item_status not in {
            ReviewItemStatus.WAITING_SOURCE_CHANGE,
            ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION,
        }:
            return
        legacy_review = await self.session.scalar(
            select(FeishuGovernanceReview).where(FeishuGovernanceReview.version_id == package.source_version_id)
        )
        version = await self.session.scalar(
            select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == package.source_version_id)
        )
        if legacy_review is None or version is None:
            return
        legacy_review.action = item.internal_action
        legacy_review.problem_tags = list(item.problem_tags or [])
        legacy_review.decision_comment = item.decision_comment
        legacy_review.applicability_scope = dict(item.applicability_scope or {})
        if item.item_status == ReviewItemStatus.WAITING_SOURCE_CHANGE:
            has_included_unit = bool(
                await self.session.scalar(
                    select(func.count(FeishuReviewItem.id)).where(
                        FeishuReviewItem.package_id == package.package_id,
                        FeishuReviewItem.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT,
                        FeishuReviewItem.outcome.in_(UNIT_PUBLISH_OUTCOMES),
                    )
                )
            )
            legacy_review.decision = ReviewDecision.REQUEST_CHANGES
            legacy_review.status = "changes_requested"
            legacy_review.decided_by = operator_id
            legacy_review.decided_at = now
            if not has_included_unit:
                version.review_status = "changes_requested"
            version.reviewer_id = operator_id
            version.reviewed_at = now
            version.review_comment = item.decision_comment
        elif item.item_status == ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION:
            legacy_review.decision = None
            legacy_review.status = "pending"
            legacy_review.decided_by = None
            legacy_review.decided_at = None
        elif item.outcome in PUBLISH_OUTCOMES:
            legacy_review.decision = ReviewDecision.PUBLISH
            legacy_review.status = "resolved"
            legacy_review.decided_by = operator_id
            legacy_review.decided_at = now
        else:
            legacy_review.decision = ReviewDecision.REJECT
            legacy_review.status = "rejected"
            legacy_review.decided_by = operator_id
            legacy_review.decided_at = now

    async def _complete_unit_legacy_review(
        self,
        package: FeishuReviewPackage,
        items: list[FeishuReviewItem],
        *,
        publish: bool,
        operator_id: str,
        now: datetime,
    ) -> None:
        legacy_review = await self.session.scalar(
            select(FeishuGovernanceReview).where(FeishuGovernanceReview.version_id == package.source_version_id)
        )
        if legacy_review is None:
            return
        unit_items = [item for item in items if item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT]
        published_count = sum(item.outcome in PUBLISH_OUTCOMES for item in unit_items)
        excluded_count = sum(item.outcome in REJECT_CANDIDATE_OUTCOMES for item in unit_items)
        duplicate_count = sum(item.outcome == ReviewOutcome.DUPLICATE_SOURCE for item in unit_items)
        legacy_review.decision = ReviewDecision.PUBLISH if publish else ReviewDecision.REJECT
        legacy_review.action = ReviewAction.UPDATE if publish else ReviewAction.ARCHIVE
        legacy_review.problem_tags = sorted({tag for item in unit_items for tag in (item.problem_tags or [])})
        legacy_review.decision_comment = (
            f"知识单元审核完成：纳入 {published_count}，不纳入 {excluded_count}，重复来源 {duplicate_count}"
        )
        legacy_review.status = "resolved" if publish else "rejected"
        legacy_review.decided_by = operator_id
        legacy_review.decided_at = now

    @staticmethod
    def _aggregate_status(items: list[FeishuReviewItem]) -> str:
        statuses = {item.item_status for item in items}
        if ReviewItemStatus.WAITING_SOURCE_CHANGE in statuses:
            return ReviewPackageStatus.WAITING_SOURCE_CHANGE
        if ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION in statuses:
            return ReviewPackageStatus.WAITING_BUSINESS_CONFIRMATION
        if statuses and statuses <= FINAL_ITEM_STATUSES:
            return ReviewPackageStatus.COMPLETED
        return ReviewPackageStatus.OPEN

    async def _load_package(self, package_id: str, *, lock: bool = False) -> FeishuReviewPackage:
        statement = select(FeishuReviewPackage).where(FeishuReviewPackage.package_id == package_id)
        if lock:
            statement = statement.with_for_update()
        package = await self.session.scalar(statement)
        if package is None:
            raise LookupError(f"Review package not found: {package_id}")
        return package

    async def _load_items(self, package_id: str, *, lock: bool = False) -> list[FeishuReviewItem]:
        statement = (
            select(FeishuReviewItem)
            .where(FeishuReviewItem.package_id == package_id)
            .order_by(FeishuReviewItem.created_at.asc())
        )
        if lock:
            statement = statement.with_for_update()
        return list(await self.session.scalars(statement))

    async def _items_by_package(self, package_ids: list[str]) -> dict[str, list[FeishuReviewItem]]:
        grouped: dict[str, list[FeishuReviewItem]] = defaultdict(list)
        if not package_ids:
            return grouped
        items = list(
            await self.session.scalars(
                select(FeishuReviewItem)
                .where(FeishuReviewItem.package_id.in_(package_ids))
                .order_by(FeishuReviewItem.created_at.asc())
            )
        )
        for item in items:
            grouped[item.package_id].append(item)
        return grouped

    async def _counts(self, source_id: str, operator_id: str) -> dict[str, int]:
        packages = list(
            await self.session.scalars(select(FeishuReviewPackage).where(FeishuReviewPackage.source_id == source_id))
        )
        mine_packages = [
            package
            for package in packages
            if package.workflow_status not in {ReviewPackageStatus.COMPLETED, ReviewPackageStatus.INVALIDATED}
            and package.assignee_id in {None, operator_id}
        ]
        source_update_package_ids = {
            package.package_id for package in mine_packages if package.trigger_type == ReviewTriggerType.SOURCE_VERSION
        }
        if source_update_package_ids:
            source_update_package_ids &= set(
                await self.session.scalars(
                    select(FeishuReviewItem.package_id)
                    .where(
                        FeishuReviewItem.package_id.in_(source_update_package_ids),
                        FeishuReviewItem.review_type == ReviewType.UPDATE,
                    )
                    .distinct()
                )
            )
        return {
            "mine": len(mine_packages),
            "source_updates": len(source_update_package_ids),
            "waiting_source_change": sum(
                package.workflow_status == ReviewPackageStatus.WAITING_SOURCE_CHANGE for package in packages
            ),
            "waiting_business_confirmation": sum(
                package.workflow_status == ReviewPackageStatus.WAITING_BUSINESS_CONFIRMATION for package in packages
            ),
            "completed": sum(package.workflow_status == ReviewPackageStatus.COMPLETED for package in packages),
        }

    async def _transferred_package_ids(self, source_id: str, operator_id: str) -> set[str]:
        events = list(
            await self.session.scalars(
                select(FeishuProcessingEvent).where(
                    FeishuProcessingEvent.source_id == source_id,
                    FeishuProcessingEvent.operator_id == operator_id,
                    FeishuProcessingEvent.event_type == "review_package_transferred",
                )
            )
        )
        return {
            str((event.payload_json or {}).get("package_id"))
            for event in events
            if (event.payload_json or {}).get("package_id")
        }

    @staticmethod
    def _claim_or_assert_assignee(package: FeishuReviewPackage, operator_id: str) -> None:
        if package.assignee_id and package.assignee_id != operator_id:
            raise PermissionError("Review package is assigned to another reviewer")
        if package.assignee_id is None:
            package.assignee_id = operator_id

    @staticmethod
    def _assert_not_terminal(package: FeishuReviewPackage) -> None:
        if package.workflow_status in {ReviewPackageStatus.COMPLETED, ReviewPackageStatus.INVALIDATED}:
            raise ValueError("Review package is already completed or invalidated")

    @staticmethod
    def _check_lock_version(package: FeishuReviewPackage, lock_version: int) -> None:
        if package.lock_version != lock_version:
            raise RuntimeError(f"Review package changed; current lock_version is {package.lock_version}")

    def _append_event(
        self,
        package: FeishuReviewPackage,
        event_type: str,
        *,
        operator_id: str,
        message: str | None = None,
        payload: dict | None = None,
    ) -> None:
        event_payload = {"package_id": package.package_id, **(payload or {})}
        self.session.add(
            FeishuProcessingEvent(
                source_id=package.source_id,
                item_id=package.source_item_id,
                version_id=package.source_version_id,
                event_type=event_type,
                operator_id=operator_id,
                message=message,
                payload_json=event_payload,
            )
        )

    @staticmethod
    def _package_summary(package: FeishuReviewPackage, items: list[FeishuReviewItem]) -> dict:
        items = ReviewPackageService._display_items(items)
        type_counts = Counter(item.review_type for item in items)
        knowledge_units = [item for item in items if item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT]
        recommendation_counts = Counter(
            str((item.evidence_json or {}).get("recommended_outcome"))
            for item in knowledge_units
            if (item.evidence_json or {}).get("recommended_outcome")
        )
        actionable_units = [
            item
            for item in knowledge_units
            if item.item_status in {ReviewItemStatus.PENDING, ReviewItemStatus.WAITING_BUSINESS_CONFIRMATION}
        ]
        return {
            "package_id": package.package_id,
            "source_version_id": package.source_version_id,
            "trigger_type": package.trigger_type,
            "title": package.title_snapshot or "未命名审核包",
            "wiki_path": package.path_snapshot,
            "workflow_status": package.workflow_status,
            "assignee_id": package.assignee_id,
            "risk_level": package.risk_level,
            "qualityGate": {
                "status": package.quality_gate_status,
                "blockers": (package.impact_summary or {}).get("blockReasons") or [],
            },
            "qualityScore": package.quality_score,
            "qualityDimensions": package.quality_dimensions or {},
            "impactSummary": package.impact_summary or {},
            "autoCloseEligible": bool(package.auto_close_eligible),
            "qualityComputedAt": _iso(package.quality_computed_at),
            "item_count": len(items),
            "pending_item_count": sum(item.item_status == ReviewItemStatus.PENDING for item in items),
            "review_type_counts": dict(type_counts),
            "knowledge_unit_count": len(knowledge_units),
            "attention_unit_count": sum(
                bool((item.evidence_json or {}).get("manual_review_required")) for item in actionable_units
            ),
            "safe_recommendation_count": sum(
                not bool((item.evidence_json or {}).get("manual_review_required"))
                and (item.evidence_json or {}).get("recommended_outcome") in OUTCOME_ACTIONS.get(item.review_type, {})
                for item in actionable_units
            ),
            "recommendation_counts": dict(recommendation_counts),
            **ReviewPackageService._unit_progress(items),
            "completion_result": ReviewPackageService._completion_result(items),
            "created_at": _iso(package.created_at),
            "updated_at": _iso(package.updated_at),
        }

    @staticmethod
    def _unit_progress(items: list[FeishuReviewItem]) -> dict:
        knowledge_units = [item for item in items if item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT]
        decided = sum(item.item_status in FINAL_ITEM_STATUSES for item in knowledge_units)
        included = sum(item.outcome in UNIT_PUBLISH_OUTCOMES for item in knowledge_units)
        excluded = sum(item.outcome in REJECT_CANDIDATE_OUTCOMES for item in knowledge_units)
        duplicate = sum(item.outcome == ReviewOutcome.DUPLICATE_SOURCE for item in knowledge_units)
        return {
            "resolved_unit_count": decided,
            "decided_unit_count": decided,
            "remaining_unit_count": max(len(knowledge_units) - decided, 0),
            "included_unit_count": included,
            "excluded_unit_count": excluded,
            "duplicate_unit_count": duplicate,
        }

    @staticmethod
    def _completion_result(items: list[FeishuReviewItem]) -> str | None:
        knowledge_units = [item for item in items if item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT]
        if not knowledge_units or any(item.item_status not in FINAL_ITEM_STATUSES for item in knowledge_units):
            return None
        included = sum(item.outcome in UNIT_PUBLISH_OUTCOMES for item in knowledge_units)
        excluded = sum(item.outcome in REJECT_CANDIDATE_OUTCOMES for item in knowledge_units)
        duplicate = sum(item.outcome == ReviewOutcome.DUPLICATE_SOURCE for item in knowledge_units)
        if included == len(knowledge_units):
            return "all_included"
        if excluded == len(knowledge_units):
            return "all_excluded"
        if duplicate == len(knowledge_units):
            return "all_duplicate"
        if sum(bool(count) for count in (included, excluded, duplicate)) > 1:
            return "partial"
        return None

    @staticmethod
    def _item_dict(item: FeishuReviewItem, *, reopened_by_item_id: str | None = None) -> dict:
        evidence = item.evidence_json or {}
        can_reopen_exclusion = (
            item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT
            and item.item_status == ReviewItemStatus.DECIDED
            and item.outcome == ReviewOutcome.EXCLUDE
            and reopened_by_item_id is None
        )
        return {
            "review_item_id": item.review_item_id,
            "review_type": item.review_type,
            "subject_type": item.subject_type,
            "subject_id": item.subject_id,
            "title": item.title,
            "summary": item.summary,
            "subject_locator": item.subject_locator_json or {},
            "evidence": evidence,
            "relation_ids": item.relation_ids or [],
            "problem_tags": item.problem_tags or [],
            "applicability_scope": item.applicability_scope or {},
            "item_status": item.item_status,
            "outcome": item.outcome,
            "internal_action": item.internal_action,
            "decision_comment": item.decision_comment,
            "allowed_outcomes": list(OUTCOME_ACTIONS.get(item.review_type, {})),
            "decided_by": item.decided_by,
            "decided_at": _iso(item.decided_at),
            "reopened_from_item_id": item.reopened_from_item_id,
            "reopened_by_item_id": reopened_by_item_id,
            "can_reopen_exclusion": can_reopen_exclusion,
            "knowledge_unit": bool(evidence.get("knowledge_unit")),
            "unit_type": evidence.get("unit_type"),
            "content": evidence.get("content"),
            "previous_content": evidence.get("previous_content"),
            "source_segment_ids": evidence.get("source_segment_ids") or [],
            "change_type": evidence.get("change_type"),
            "recommended_outcome": evidence.get("recommended_outcome"),
            "recommendation_reason": evidence.get("recommendation_reason"),
            "recommendation_confidence": evidence.get("recommendation_confidence"),
            "manual_review_required": bool(evidence.get("manual_review_required")),
            "comparison_status": evidence.get("comparison_status"),
        }

    @staticmethod
    def _display_items(items: list[FeishuReviewItem]) -> list[FeishuReviewItem]:
        knowledge_units = [item for item in items if item.subject_type == ReviewSubjectType.KNOWLEDGE_UNIT]
        return knowledge_units or items

    @staticmethod
    def _relation_dict(
        relation: FeishuCrossDocumentRelation,
        relation_sources: dict[str, tuple[FeishuMaterialVersion, FeishuSourceItem]],
    ) -> dict:
        source_version, source_item = relation_sources.get(relation.source_version_id, (None, None))
        target_version, target_item = relation_sources.get(relation.target_version_id, (None, None))
        return {
            "relation_id": relation.relation_id,
            "source_version_id": relation.source_version_id,
            "target_version_id": relation.target_version_id,
            "source_title": source_item.title if source_item else "未命名资料",
            "target_title": target_item.title if target_item else "未命名资料",
            "source_path": source_item.path_text if source_item else None,
            "target_path": target_item.path_text if target_item else None,
            "source_url": source_item.source_url if source_item else None,
            "target_url": target_item.source_url if target_item else None,
            "source_revision": source_version.revision if source_version else None,
            "target_revision": target_version.revision if target_version else None,
            "relation_type": relation.relation_type,
            "similarity": relation.similarity,
            "confidence": relation.confidence,
            "same_content": relation.same_content or [],
            "different_content": relation.different_content or [],
            "scope_difference": relation.scope_difference or {},
            "reasoning": relation.reasoning,
            "status": relation.status,
        }

    @staticmethod
    def _change_request_dict(request: FeishuSourceChangeRequest) -> dict:
        return {
            "change_request_id": request.change_request_id,
            "review_item_id": request.review_item_id,
            "status": request.status,
            "request_text": request.request_text,
            "source_url": request.source_url,
            "responsible_user_id": request.responsible_user_id,
            "responsible_user_name": request.responsible_user_name,
            "round_number": request.round_number,
            "requested_version_id": request.requested_version_id,
            "received_version_id": request.received_version_id,
            "created_at": _iso(request.created_at),
            "updated_at": _iso(request.updated_at),
        }

    @staticmethod
    def _event_dict(event: FeishuProcessingEvent) -> dict:
        return {
            "event_type": event.event_type,
            "operator_id": event.operator_id,
            "message": event.message,
            "payload": event.payload_json or {},
            "created_at": _iso(event.created_at),
        }

    @staticmethod
    def _outcome_label(outcome: str) -> str:
        return {
            ReviewOutcome.EXCLUDE: "不纳入知识库",
            ReviewOutcome.KEEP_CURRENT: "保留当前版本",
        }.get(outcome, outcome)
