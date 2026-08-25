from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.governance.domain import (
    ReviewItemStatus,
    ReviewPackageStatus,
    ReviewSubjectType,
    ReviewTriggerType,
    ReviewType,
    SourceChangeRequestStatus,
)
from yuxi.governance.review_backfill import backfill_legacy_governance_reviews
from yuxi.governance.review_package_service import ReviewPackageService
from yuxi.governance.schemas import ReviewItemDecisionRequest, ReviewPackageResolveRequest
from yuxi.governance.source_change_service import SourceChangeService
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSource,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def source_change_session():
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
        source_item = FeishuSourceItem(
            item_id="item-1",
            source_id="source-1",
            item_key="page:item-1",
            item_type="docx",
            title="部署指南",
            source_url="https://quickdone.feishu.cn/wiki/item-1",
            source_validity="valid",
            active_version_id="version-published",
        )
        versions = [
            FeishuMaterialVersion(
                version_id="version-published",
                item_id="item-1",
                revision="1",
                content_hash="hash-published",
                processing_status="published",
                review_status="approved",
            ),
            FeishuMaterialVersion(
                version_id="version-requested",
                item_id="item-1",
                revision="2",
                content_hash="hash-requested",
                processing_status="replaced",
                review_status="changes_requested",
            ),
            FeishuMaterialVersion(
                version_id="version-received",
                item_id="item-1",
                revision="3",
                content_hash="hash-received",
                processing_status="discovered",
                review_status="pending",
            ),
        ]
        package = FeishuReviewPackage(
            package_id="package-old",
            package_key="package-old",
            source_id="source-1",
            source_item_id="item-1",
            source_version_id="version-requested",
            trigger_type=ReviewTriggerType.SOURCE_VERSION,
            title_snapshot="部署指南",
            source_url_snapshot=source_item.source_url,
            workflow_status=ReviewPackageStatus.WAITING_SOURCE_CHANGE,
            lock_version=1,
            created_at=now,
            updated_at=now,
        )
        review_item = FeishuReviewItem(
            review_item_id="review-item-old",
            package_id="package-old",
            candidate_key="candidate-old",
            review_type=ReviewType.UPDATE,
            subject_type=ReviewSubjectType.MATERIAL_VERSION,
            subject_id="version-requested",
            title="部署指南",
            item_status=ReviewItemStatus.WAITING_SOURCE_CHANGE,
            created_at=now,
            updated_at=now,
        )
        change_request = FeishuSourceChangeRequest(
            change_request_id="change-request-old",
            review_item_id="review-item-old",
            source_item_id="item-1",
            requested_version_id="version-requested",
            source_url=source_item.source_url,
            status=SourceChangeRequestStatus.OPEN,
            request_text="请补充部署版本",
            round_number=1,
            created_by="admin-a",
            created_at=now,
            updated_at=now,
        )
        session.add_all([source, source_item, *versions, package, review_item, change_request])
        await session.commit()
        yield session
    await engine.dispose()


async def test_changed_hash_closes_old_review_stage_and_is_idempotent(source_change_session):
    service = SourceChangeService(source_change_session)

    first = await service.register_new_material_version("version-received")
    await source_change_session.commit()
    second = await service.register_new_material_version("version-received")
    await source_change_session.commit()

    change_request = await source_change_session.scalar(select(FeishuSourceChangeRequest))
    review_item = await source_change_session.scalar(select(FeishuReviewItem))
    package = await source_change_session.scalar(select(FeishuReviewPackage))
    source_item = await source_change_session.scalar(select(FeishuSourceItem))
    event_count = await source_change_session.scalar(
        select(func.count())
        .select_from(FeishuProcessingEvent)
        .where(FeishuProcessingEvent.event_type == "source_change_version_received")
    )

    assert first["received_count"] == 1
    assert second["received_count"] == 0
    assert change_request.status == SourceChangeRequestStatus.NEW_VERSION_RECEIVED
    assert change_request.received_version_id == "version-received"
    assert review_item.item_status == ReviewItemStatus.SOURCE_UPDATED
    assert package.workflow_status == ReviewPackageStatus.COMPLETED
    assert package.lock_version == 2
    assert source_item.active_version_id == "version-published"
    assert event_count == 1


async def test_same_hash_does_not_reopen_review(source_change_session):
    version = await source_change_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-received")
    )
    version.content_hash = "hash-requested"
    await source_change_session.commit()

    result = await SourceChangeService(source_change_session).register_new_material_version("version-received")
    await source_change_session.commit()

    change_request = await source_change_session.scalar(select(FeishuSourceChangeRequest))
    review_item = await source_change_session.scalar(select(FeishuReviewItem))
    assert result["received_count"] == 0
    assert change_request.status == SourceChangeRequestStatus.OPEN
    assert change_request.received_version_id is None
    assert review_item.item_status == ReviewItemStatus.WAITING_SOURCE_CHANGE


async def test_parsed_new_version_reopens_review_and_next_decision_fulfills_old_request(source_change_session):
    await SourceChangeService(source_change_session).register_new_material_version("version-received")
    version = await source_change_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-received")
    )
    version.processing_status = "awaiting_review"
    await source_change_session.flush()

    await backfill_legacy_governance_reviews(source_change_session)
    new_item = await source_change_session.scalar(
        select(FeishuReviewItem).where(FeishuReviewItem.subject_id == "version-received")
    )
    new_package = await source_change_session.scalar(
        select(FeishuReviewPackage).where(FeishuReviewPackage.package_id == new_item.package_id)
    )

    assert new_item.reopened_from_item_id == "review-item-old"
    assert new_package.workflow_status == ReviewPackageStatus.OPEN
    assert (
        await source_change_session.scalar(
            select(func.count())
            .select_from(FeishuProcessingEvent)
            .where(FeishuProcessingEvent.event_type == "review_item_reopened")
        )
        == 1
    )

    payload = ReviewPackageResolveRequest(
        request_id="request-second-round",
        lock_version=new_package.lock_version,
        decisions=[
            ReviewItemDecisionRequest(
                review_item_id=new_item.review_item_id,
                outcome="REQUEST_SOURCE_CHANGE",
                problem_tags=["MISSING_SCOPE"],
                decision_comment="仍需补充部署模式",
                applicability_scope={},
            )
        ],
    )
    await ReviewPackageService(source_change_session).resolve(
        new_package.package_id,
        payload,
        operator_id="admin-a",
    )
    await source_change_session.commit()

    old_request = await source_change_session.scalar(
        select(FeishuSourceChangeRequest).where(
            FeishuSourceChangeRequest.change_request_id == "change-request-old"
        )
    )
    new_request = await source_change_session.scalar(
        select(FeishuSourceChangeRequest).where(
            FeishuSourceChangeRequest.review_item_id == new_item.review_item_id
        )
    )
    assert old_request.status == SourceChangeRequestStatus.FULFILLED
    assert old_request.resolved_at is not None
    assert new_request.status == SourceChangeRequestStatus.OPEN
    assert new_request.round_number == 2
    assert new_item.item_status == ReviewItemStatus.WAITING_SOURCE_CHANGE
    assert new_package.workflow_status == ReviewPackageStatus.WAITING_SOURCE_CHANGE
