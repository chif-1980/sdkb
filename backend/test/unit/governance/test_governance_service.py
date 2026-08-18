from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.governance.domain import (
    CrossDocumentRelationType,
    KnowledgeSourceRole,
    ProblemTag,
    ReviewAction,
    ReviewDecision,
)
from yuxi.governance.comparator import CrossDocumentComparisonService
from yuxi.governance.schemas import ReviewResolveRequest
from yuxi.governance.service import GovernanceService
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuGovernanceReview,
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def governance_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        source = FeishuSource(
            source_id="source-1",
            name="SD 知识库",
            wiki_root_token="root",
            target_kb_id="kb-1",
            credential_env_name="FEISHU_USER_OAUTH",
        )
        current_item = FeishuSourceItem(
            item_id="item-current",
            source_id="source-1",
            item_key="page:current",
            item_type="docx",
            title="Q900 部署指南",
            path_text="产品资料 / Q900",
            source_validity="valid",
            active_version_id="version-current",
        )
        candidate_item = FeishuSourceItem(
            item_id="item-candidate",
            source_id="source-1",
            item_key="page:candidate",
            item_type="pdf",
            title="金融行业解决方案",
            path_text="行业方案 / 金融",
            source_validity="valid",
        )
        current = FeishuMaterialVersion(
            version_id="version-current",
            item_id="item-current",
            revision="1",
            content_hash="current-hash",
            processing_status="published",
            review_status="approved",
            yuxi_file_id="file-current",
            chunk_count=8,
            published_at=datetime.now(UTC),
        )
        candidate = FeishuMaterialVersion(
            version_id="version-candidate",
            item_id="item-candidate",
            revision="2",
            content_hash="candidate-hash",
            processing_status="awaiting_review",
            review_status="pending",
            yuxi_file_id="file-candidate",
        )
        relation = FeishuCrossDocumentRelation(
            relation_id="relation-1",
            comparison_key="version-candidate:version-current:candidate-hash:current-hash",
            source_version_id="version-candidate",
            target_version_id="version-current",
            relation_type="CONFLICT",
            similarity=0.88,
            confidence=0.94,
            same_content=["产品、版本和部署模式一致"],
            different_content=[{"field": "GPU 数量", "source": "6", "target": "4"}],
            scope_difference={},
            reasoning="相同条件下结论冲突",
            status="open",
        )
        session.add_all([source, current_item, candidate_item, current, candidate, relation])
        await session.commit()
        yield session
    await engine.dispose()


async def test_governance_domain_defines_stable_public_types():
    assert [item.value for item in ReviewDecision] == [
        "PUBLISH",
        "REQUEST_CHANGES",
        "REJECT",
        "TRANSFER",
    ]
    assert ReviewAction.MARK_DUPLICATE.value == "MARK_DUPLICATE"
    assert ProblemTag.CONFLICT.value == "CONFLICT"
    assert CrossDocumentRelationType.CONDITIONAL_VARIANT.value == "CONDITIONAL_VARIANT"
    assert KnowledgeSourceRole.ALIAS.value == "ALIAS"


async def test_review_request_requires_transfer_assignee_comment_and_valid_publish_action():
    with pytest.raises(ValidationError, match="assignee_id is required"):
        ReviewResolveRequest(decision="TRANSFER", action="KEEP_CURRENT")

    with pytest.raises(ValidationError, match="decision_comment is required"):
        ReviewResolveRequest(decision="REQUEST_CHANGES", action="MARK_INSUFFICIENT")

    with pytest.raises(ValidationError, match="publish requires"):
        ReviewResolveRequest(decision="PUBLISH", action="MARK_DUPLICATE")


async def test_review_list_and_comparison_return_real_persisted_evidence(governance_session):
    service = GovernanceService(governance_session)

    reviews = await service.list_reviews("source-1")
    comparisons = await service.list_review_comparisons(reviews[0]["review_id"])

    assert reviews[0]["version_id"] == "version-candidate"
    assert reviews[0]["problem_tags"] == ["CONFLICT"]
    assert reviews[0]["risk_level"] == "HIGH"
    assert reviews[0]["comparison_count"] == 1
    assert comparisons[0]["relation_type"] == "CONFLICT"
    assert comparisons[0]["source_title"] == "金融行业解决方案"
    assert comparisons[0]["target_title"] == "Q900 部署指南"
    assert comparisons[0]["different_content"][0]["field"] == "GPU 数量"


async def test_request_changes_persists_scope_closes_relation_and_removes_pending_review(governance_session):
    service = GovernanceService(governance_session)
    review, version, item = await service.prepare_resolution("version-candidate", operator_id="reviewer-a")
    payload = ReviewResolveRequest(
        decision="REQUEST_CHANGES",
        action="MARK_INSUFFICIENT",
        problem_tags=["CONFLICT", "MISSING_SCOPE"],
        decision_comment="请补充生效版本后重新扫描",
        applicability_scope={"industry": "金融", "product": "Q900"},
    )

    await service.record_resolution(review, version, item, payload, operator_id="reviewer-a")
    await governance_session.commit()

    assert review.status == "changes_requested"
    assert version.review_status == "changes_requested"
    assert version.processing_params["applicability_scope"] == {"industry": "金融", "product": "Q900"}
    relation = await governance_session.get(FeishuCrossDocumentRelation, 1)
    assert relation.status == "resolved"
    assert relation.human_decision == "MARK_INSUFFICIENT"
    assert await service.list_reviews("source-1", status="pending") == []


async def test_transfer_keeps_task_open_and_only_new_assignee_can_resolve(governance_session):
    service = GovernanceService(governance_session)
    review, version, item = await service.prepare_resolution("version-candidate", operator_id="reviewer-a")
    payload = ReviewResolveRequest(
        decision="TRANSFER",
        action="KEEP_CURRENT",
        assignee_id="reviewer-b",
        decision_comment="转交产品负责人",
    )
    await service.record_resolution(review, version, item, payload, operator_id="reviewer-a")
    await governance_session.commit()

    assert review.status == "pending"
    assert review.assignee_id == "reviewer-b"
    with pytest.raises(PermissionError, match="assigned to another reviewer"):
        await service.prepare_resolution(review.review_id, operator_id="reviewer-a")

    same_review, _, _ = await service.prepare_resolution(review.review_id, operator_id="reviewer-b")
    assert same_review.review_id == review.review_id


async def test_formal_knowledge_only_returns_active_published_approved_version(governance_session):
    items = await GovernanceService(governance_session).list_formal_knowledge("source-1")

    assert len(items) == 1
    assert items[0]["knowledge_id"] == "item-current"
    assert items[0]["current_version_id"] == "version-current"
    assert items[0]["index_status"] == "INDEXED"
    assert items[0]["source_role"] == "PRIMARY"


async def test_comparison_is_idempotent_and_ensures_review_task(governance_session):
    current_item = FeishuSourceItem(
        item_id="item-new",
        source_id="source-1",
        item_key="page:new",
        item_type="docx",
        title="Q900 部署指南副本",
        path_text="产品资料 / Q900 / 副本",
        source_validity="valid",
    )
    current = FeishuMaterialVersion(
        version_id="version-new",
        item_id="item-new",
        revision="3",
        content_hash="current-hash",
        processing_status="awaiting_review",
        review_status="pending",
    )
    governance_session.add_all([current_item, current])
    await governance_session.commit()

    service = CrossDocumentComparisonService(governance_session)
    first = await service.compare_version("version-new")
    second = await service.compare_version("version-new")
    await governance_session.commit()

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].relation_id == second[0].relation_id
    assert await governance_session.scalar(
        select(FeishuGovernanceReview).where(FeishuGovernanceReview.version_id == "version-new")
    ) is not None
    assert await governance_session.scalar(
        select(func.count()).select_from(FeishuCrossDocumentRelation).where(
            FeishuCrossDocumentRelation.comparison_key == "version-current:version-new"
        )
    ) == 1


async def test_open_conflict_is_detected_for_publish_guard(governance_session):
    assert await GovernanceService(governance_session).has_open_conflict("version-candidate") is True
