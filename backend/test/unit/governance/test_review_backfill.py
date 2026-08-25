from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.governance.domain import (
    ReviewItemStatus,
    ReviewOutcome,
    ReviewPackageStatus,
    ReviewSubjectType,
    ReviewTriggerType,
    ReviewType,
    SourceChangeRequestStatus,
)
from yuxi.governance.review_backfill import backfill_legacy_governance_reviews
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuGovernanceReview,
    FeishuMaterialVersion,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSource,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def legacy_review_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        now = datetime.now(UTC)
        source = FeishuSource(
            source_id="source-1",
            name="SD 知识库",
            wiki_root_token="root",
            target_kb_id="kb-1",
            credential_env_name="FEISHU_USER_OAUTH",
        )
        conflict_item = FeishuSourceItem(
            item_id="item-conflict",
            source_id="source-1",
            item_key="page:conflict",
            item_type="docx",
            title="冲突资料",
            path_text="产品资料 / 冲突资料",
            source_url="https://quickdone.feishu.cn/wiki/conflict",
            source_validity="valid",
        )
        update_item = FeishuSourceItem(
            item_id="item-update",
            source_id="source-1",
            item_key="page:update",
            item_type="pdf",
            title="待修改资料",
            source_url="https://quickdone.feishu.cn/wiki/update",
            source_validity="valid",
            active_version_id="version-old",
        )
        new_item = FeishuSourceItem(
            item_id="item-new",
            source_id="source-1",
            item_key="page:new",
            item_type="pptx",
            title="新增资料",
            source_validity="valid",
        )
        current_item = FeishuSourceItem(
            item_id="item-current",
            source_id="source-1",
            item_key="page:current",
            item_type="docx",
            title="当前正式资料",
            source_validity="valid",
            active_version_id="version-current",
        )
        versions = [
            FeishuMaterialVersion(
                version_id="version-conflict",
                item_id="item-conflict",
                revision="1",
                content_hash="hash-conflict",
                processing_status="awaiting_review",
                review_status="pending",
            ),
            FeishuMaterialVersion(
                version_id="version-update",
                item_id="item-update",
                revision="2",
                content_hash="hash-update",
                processing_status="awaiting_review",
                review_status="changes_requested",
                reviewer_id="reviewer-a",
            ),
            FeishuMaterialVersion(
                version_id="version-new",
                item_id="item-new",
                revision="1",
                content_hash="hash-new",
                processing_status="published",
                review_status="approved",
            ),
            FeishuMaterialVersion(
                version_id="version-current",
                item_id="item-current",
                revision="1",
                content_hash="hash-current",
                processing_status="published",
                review_status="approved",
            ),
        ]
        reviews = [
            FeishuGovernanceReview(
                review_id="review-conflict",
                version_id="version-conflict",
                status="pending",
                assignee_id="reviewer-b",
                problem_tags=["CONFLICT"],
                applicability_scope={"product": "Q900"},
                created_at=now,
                updated_at=now,
            ),
            FeishuGovernanceReview(
                review_id="review-update",
                version_id="version-update",
                status="changes_requested",
                decision="REQUEST_CHANGES",
                action="MARK_INSUFFICIENT",
                problem_tags=["MISSING_SCOPE"],
                decision_comment="请在飞书原文补充产品版本",
                applicability_scope={"product": "Q900"},
                decided_by="reviewer-a",
                decided_at=now,
                created_at=now,
                updated_at=now,
            ),
            FeishuGovernanceReview(
                review_id="review-new",
                version_id="version-new",
                status="resolved",
                decision="PUBLISH",
                action="CREATE",
                problem_tags=[],
                applicability_scope={},
                decided_by="reviewer-a",
                decided_at=now,
                created_at=now,
                updated_at=now,
            ),
            FeishuGovernanceReview(
                review_id="review-current",
                version_id="version-current",
                status="pending",
                problem_tags=[],
                applicability_scope={},
                created_at=now,
                updated_at=now,
            ),
        ]
        relation = FeishuCrossDocumentRelation(
            relation_id="relation-conflict",
            comparison_key="version-conflict:version-update",
            source_version_id="version-conflict",
            target_version_id="version-update",
            relation_type="CONFLICT",
            status="open",
        )
        session.add_all(
            [source, conflict_item, update_item, new_item, current_item, *versions, *reviews, relation]
        )
        await session.commit()
        # SQLite disables foreign-key enforcement by default, which previously
        # hid an ordering bug that failed against the real PostgreSQL schema.
        await session.execute(text("PRAGMA foreign_keys=ON"))
        yield session
    await engine.dispose()


async def test_p0_review_tables_are_registered_with_expected_constraints():
    assert {
        "feishu_review_packages",
        "feishu_review_items",
        "feishu_source_change_requests",
    } <= set(Base.metadata.tables)

    package_table = Base.metadata.tables["feishu_review_packages"]
    item_table = Base.metadata.tables["feishu_review_items"]
    change_table = Base.metadata.tables["feishu_source_change_requests"]

    assert {"package_id", "package_key", "workflow_status", "draft_json", "lock_version"} <= {
        column.name for column in package_table.columns
    }
    assert {"candidate_key", "review_type", "item_status", "outcome", "internal_action"} <= {
        column.name for column in item_table.columns
    }
    assert {"review_item_id", "status", "round_number", "received_version_id"} <= {
        column.name for column in change_table.columns
    }


async def test_backfill_migrates_legacy_reviews_and_is_idempotent(legacy_review_session):
    first = await backfill_legacy_governance_reviews(legacy_review_session)
    await legacy_review_session.commit()
    second = await backfill_legacy_governance_reviews(legacy_review_session)
    await legacy_review_session.commit()

    assert first == {"packages_created": 4, "items_created": 4, "change_requests_created": 1}
    assert second == {"packages_created": 0, "items_created": 0, "change_requests_created": 0}
    assert await legacy_review_session.scalar(select(func.count()).select_from(FeishuReviewPackage)) == 4
    assert await legacy_review_session.scalar(select(func.count()).select_from(FeishuReviewItem)) == 4
    assert await legacy_review_session.scalar(select(func.count()).select_from(FeishuSourceChangeRequest)) == 1


async def test_backfill_can_limit_worker_work_to_one_version(legacy_review_session):
    first = await backfill_legacy_governance_reviews(
        legacy_review_session,
        version_ids=["version-conflict"],
    )
    await legacy_review_session.commit()

    assert first == {"packages_created": 1, "items_created": 1, "change_requests_created": 0}
    assert await legacy_review_session.scalar(select(func.count()).select_from(FeishuReviewPackage)) == 1
    assert await legacy_review_session.scalar(select(func.count()).select_from(FeishuReviewItem)) == 1

    second = await backfill_legacy_governance_reviews(legacy_review_session)
    await legacy_review_session.commit()
    assert second == {"packages_created": 3, "items_created": 3, "change_requests_created": 1}


async def test_backfill_preserves_status_evidence_and_change_request(legacy_review_session):
    await backfill_legacy_governance_reviews(legacy_review_session)
    await legacy_review_session.commit()

    stored_packages = (await legacy_review_session.scalars(select(FeishuReviewPackage))).all()
    packages = {package.source_version_id: package for package in stored_packages}
    items = {item.subject_id: item for item in (await legacy_review_session.scalars(select(FeishuReviewItem))).all()}
    change_request = await legacy_review_session.scalar(select(FeishuSourceChangeRequest))

    assert packages["version-conflict"].workflow_status == ReviewPackageStatus.OPEN
    assert packages["version-conflict"].trigger_type == ReviewTriggerType.SOURCE_VERSION
    assert packages["version-conflict"].risk_level == "HIGH"
    assert packages["version-update"].workflow_status == ReviewPackageStatus.WAITING_SOURCE_CHANGE
    assert packages["version-new"].workflow_status == ReviewPackageStatus.COMPLETED

    assert items["version-conflict"].review_type == ReviewType.CONFLICT
    assert items["version-conflict"].subject_type == ReviewSubjectType.MATERIAL_VERSION
    assert items["version-conflict"].item_status == ReviewItemStatus.PENDING
    assert items["version-conflict"].relation_ids == ["relation-conflict"]
    assert items["version-update"].review_type == ReviewType.CONFLICT
    assert items["version-update"].outcome == ReviewOutcome.REQUEST_SOURCE_CHANGE
    assert items["version-update"].item_status == ReviewItemStatus.WAITING_SOURCE_CHANGE
    assert items["version-new"].review_type == ReviewType.NEW
    assert items["version-new"].outcome == ReviewOutcome.PUBLISH
    assert items["version-new"].item_status == ReviewItemStatus.DECIDED
    assert items["version-current"].review_type == ReviewType.STALE

    assert change_request.status == SourceChangeRequestStatus.OPEN
    assert change_request.source_item_id == "item-update"
    assert change_request.requested_version_id == "version-update"
    assert change_request.request_text == "请在飞书原文补充产品版本"
    assert change_request.round_number == 1


async def test_backfill_repairs_pending_current_version_misclassified_as_update(legacy_review_session):
    await backfill_legacy_governance_reviews(legacy_review_session)
    current_item = await legacy_review_session.scalar(
        select(FeishuReviewItem).where(FeishuReviewItem.subject_id == "version-current")
    )
    current_item.review_type = ReviewType.UPDATE
    await legacy_review_session.commit()

    result = await backfill_legacy_governance_reviews(legacy_review_session)
    await legacy_review_session.commit()

    assert result == {"packages_created": 0, "items_created": 0, "change_requests_created": 0}
    assert current_item.review_type == ReviewType.STALE
