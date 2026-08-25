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
    ReviewType,
    SourceChangeRequestStatus,
)
from yuxi.governance.review_backfill import backfill_legacy_governance_reviews, stable_review_id
from yuxi.governance.schemas import (
    ReviewItemDecisionRequest,
    ReviewPackageResolveRequest,
)
from yuxi.governance.source_change_service import SourceChangeService
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuGovernanceReview,
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSource,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
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
REJECT_CANDIDATE_OUTCOMES = {ReviewOutcome.EXCLUDE, ReviewOutcome.KEEP_CURRENT}
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
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        await backfill_legacy_governance_reviews(self.session)
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
        filtered_packages = []
        for package in packages:
            items = items_by_package[package.package_id]
            if review_types and not any(item.review_type in review_types for item in items):
                continue
            if problem_tag and not any(problem_tag in (item.problem_tags or []) for item in items):
                continue
            filtered_packages.append(package)

        total = len(filtered_packages)
        offset = (page - 1) * page_size
        paged_packages = filtered_packages[offset : offset + page_size]
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
        items = await self._load_items(package.package_id)
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
        has_update_item = any(item.review_type == ReviewType.UPDATE for item in items)
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
            **self._package_summary(package, items),
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
            "items": [self._item_dict(item) for item in items],
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
    ) -> dict:
        package = await self._load_package(package_id, lock=True)
        self._assert_not_terminal(package)
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
                "reject_candidates": [],
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
                await self._resolve_item_relations(item, operator_id=operator_id, now=now)

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
                },
            )

        previous_status = package.workflow_status
        package.workflow_status = self._aggregate_status(items)
        package.completed_at = now if package.workflow_status == ReviewPackageStatus.COMPLETED else None
        package.draft_json = {}
        package.lock_version += 1

        publish_version_ids: set[str] = set()
        reject_candidates: dict[str, str] = {}
        if package.workflow_status == ReviewPackageStatus.COMPLETED and package.source_version_id:
            publish_items = [item for item in items if item.outcome in PUBLISH_OUTCOMES]
            rejected_items = [item for item in items if item.outcome in REJECT_CANDIDATE_OUTCOMES]
            if publish_items and rejected_items:
                raise ValueError("One source-version package cannot both publish and reject the same material")
            if publish_items:
                publish_version_ids.add(package.source_version_id)
            elif rejected_items:
                reject_item = rejected_items[0]
                reject_candidates[package.source_version_id] = reject_item.decision_comment or self._outcome_label(
                    reject_item.outcome
                )

        if package.workflow_status == ReviewPackageStatus.COMPLETED and previous_status != package.workflow_status:
            self._append_event(package, "review_package_completed", operator_id=operator_id)
        await self.session.flush()
        return {
            "package_id": package.package_id,
            "workflow_status": package.workflow_status,
            "lock_version": package.lock_version,
            "publish_version_ids": sorted(publish_version_ids),
            "reject_candidates": [
                {"version_id": version_id, "reason": reason} for version_id, reason in reject_candidates.items()
            ],
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
        for relation in relations:
            relation.status = "resolved"
            relation.human_decision = item.internal_action or item.outcome
            relation.human_comment = item.decision_comment
            relation.resolved_by = operator_id
            relation.resolved_at = now

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
            legacy_review.decision = ReviewDecision.REQUEST_CHANGES
            legacy_review.status = "changes_requested"
            legacy_review.decided_by = operator_id
            legacy_review.decided_at = now
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
        return {
            "mine": sum(
                package.workflow_status not in {ReviewPackageStatus.COMPLETED, ReviewPackageStatus.INVALIDATED}
                and package.assignee_id in {None, operator_id}
                for package in packages
            ),
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
        type_counts = Counter(item.review_type for item in items)
        return {
            "package_id": package.package_id,
            "source_version_id": package.source_version_id,
            "title": package.title_snapshot or "未命名审核包",
            "wiki_path": package.path_snapshot,
            "workflow_status": package.workflow_status,
            "assignee_id": package.assignee_id,
            "risk_level": package.risk_level,
            "item_count": len(items),
            "pending_item_count": sum(item.item_status == ReviewItemStatus.PENDING for item in items),
            "review_type_counts": dict(type_counts),
            "created_at": _iso(package.created_at),
            "updated_at": _iso(package.updated_at),
        }

    @staticmethod
    def _item_dict(item: FeishuReviewItem) -> dict:
        return {
            "review_item_id": item.review_item_id,
            "review_type": item.review_type,
            "subject_type": item.subject_type,
            "subject_id": item.subject_id,
            "title": item.title,
            "summary": item.summary,
            "subject_locator": item.subject_locator_json or {},
            "evidence": item.evidence_json or {},
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
        }

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
