from datetime import UTC, datetime, timedelta

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
from yuxi.governance.content_quality import assess_content
from yuxi.governance.review_backfill import invalidate_unsubstantiated_text_relations
from yuxi.governance.schemas import ReviewResolveRequest
from yuxi.governance.service import GovernanceService
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuGovernanceReview,
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuReviewItem,
    FeishuReviewPackage,
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


async def test_content_quality_marks_title_only_material_as_missing():
    assert assess_content(content="# 产品手册", title="产品手册")["has_body"] is False
    assert assess_content(content="# 产品手册\n\n部署步骤如下。", title="产品手册")["has_body"] is True


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
    assert reviews[0]["target_kb_id"] == "kb-1"
    assert reviews[0]["yuxi_file_id"] == "file-candidate"
    assert reviews[0]["chunk_count"] == 0
    assert reviews[0]["token_count"] == 0
    assert comparisons[0]["relation_type"] == "CONFLICT"
    assert comparisons[0]["source_title"] == "金融行业解决方案"
    assert comparisons[0]["target_title"] == "Q900 部署指南"
    assert comparisons[0]["source_processing_status"] == "awaiting_review"
    assert comparisons[0]["source_review_status"] == "pending"
    assert comparisons[0]["target_processing_status"] == "published"
    assert comparisons[0]["target_review_status"] == "approved"
    assert comparisons[0]["different_content"][0]["field"] == "GPU 数量"

    relations = await service.list_relations("source-1")
    assert relations[0]["source_processing_status"] == "awaiting_review"
    assert relations[0]["target_processing_status"] == "published"


async def test_comparison_status_separates_all_relations_from_actionable_issues(governance_session):
    governance_session.add(
        FeishuCrossDocumentRelation(
            relation_id="relation-overlap",
            comparison_key="version-current:version-candidate:overlap",
            source_version_id="version-current",
            target_version_id="version-candidate",
            relation_type="OVERLAP",
            status="open",
        )
    )
    await governance_session.commit()

    status = await GovernanceService(governance_session).get_comparison_status("source-1")

    assert status["relation_count"] == 2
    assert status["issue_count"] == 1


async def test_metadata_only_overlap_is_invalidated(governance_session):
    relation = FeishuCrossDocumentRelation(
        relation_id="relation-metadata-only",
        comparison_key="version-current:version-candidate:metadata-only",
        source_version_id="version-current",
        target_version_id="version-candidate",
        relation_type="OVERLAP",
        same_content=["目录和标题语义相近"],
        reasoning="标题和目录相似",
        status="open",
    )
    governance_session.add(relation)
    await governance_session.commit()

    invalidated = await invalidate_unsubstantiated_text_relations(governance_session)
    await governance_session.commit()

    assert invalidated == 1
    assert relation.status == "invalidated"
    assert relation.human_decision == "NO_TEXT_EVIDENCE"


async def test_exact_duplicate_with_body_evidence_remains_open(governance_session):
    relation = FeishuCrossDocumentRelation(
        relation_id="relation-body-duplicate",
        comparison_key="version-current:version-candidate:body-duplicate",
        source_version_id="version-current",
        target_version_id="version-candidate",
        relation_type="EXACT_DUPLICATE",
        same_content=["内容哈希一致", "正文局部相似度 100%"],
        reasoning="两边已解析正文完全一致",
        status="open",
    )
    governance_session.add(relation)
    await governance_session.commit()

    invalidated = await invalidate_unsubstantiated_text_relations(governance_session)
    await governance_session.commit()

    assert invalidated == 0
    assert relation.status == "open"


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
    assert items[0]["pending_update"] is None


async def test_formal_knowledge_exposes_open_update_package_for_each_unit(governance_session):
    detected_at = datetime(2026, 8, 28, 8, 43, 16, tzinfo=UTC)
    source_item = await governance_session.scalar(
        select(FeishuSourceItem).where(FeishuSourceItem.item_id == "item-current")
    )
    source_item.source_updated_at = detected_at
    governance_session.add_all(
        [
            FeishuMaterialVersion(
                version_id="version-pending-update",
                item_id="item-current",
                revision="13",
                content_hash="pending-update-hash",
                processing_status="awaiting_review",
                review_status="pending",
                yuxi_file_id="file-pending-update",
                created_at=detected_at,
            ),
            FeishuReviewPackage(
                package_id="package-pending-update",
                package_key="SOURCE_VERSION:version-pending-update",
                source_id="source-1",
                source_item_id="item-current",
                source_version_id="version-pending-update",
                trigger_type="SOURCE_VERSION",
                title_snapshot="Q900 部署指南",
                workflow_status="OPEN",
                created_at=detected_at,
            ),
            FeishuReviewItem(
                review_item_id="review-item-pending-update",
                package_id="package-pending-update",
                candidate_key="UPDATE:version-pending-update",
                review_type="UPDATE",
                subject_type="MATERIAL_VERSION",
                subject_id="version-pending-update",
                title="版本 1 更新为版本 13",
                item_status="PENDING",
            ),
            FeishuKnowledgeUnit(
                unit_id="unit-update-1",
                unit_key="section:update-1",
                lineage_key="section:update-1",
                version_id="version-current",
                item_id="item-current",
                unit_index=0,
                unit_type="SECTION",
                title="更新测试单元一",
                content="第一条正式知识。",
                content_hash="unit-update-1-hash",
                recommended_outcome="PUBLISH",
                recommendation_reason="内容完整。",
                publication_state="INCLUDED",
            ),
            FeishuKnowledgeUnit(
                unit_id="unit-update-2",
                unit_key="section:update-2",
                lineage_key="section:update-2",
                version_id="version-current",
                item_id="item-current",
                unit_index=1,
                unit_type="SECTION",
                title="更新测试单元二",
                content="第二条正式知识。",
                content_hash="unit-update-2-hash",
                recommended_outcome="PUBLISH",
                recommendation_reason="内容完整。",
                publication_state="INCLUDED",
            ),
        ]
    )
    await governance_session.commit()

    items = await GovernanceService(governance_session).list_formal_knowledge("source-1")

    assert len(items) == 2
    assert {item["revision"] for item in items} == {"1"}
    assert {item["pending_update"]["version_id"] for item in items} == {"version-pending-update"}
    assert {item["pending_update"]["revision"] for item in items} == {"13"}
    assert {item["pending_update"]["review_package_id"] for item in items} == {
        "package-pending-update"
    }
    assert all(item["pending_update"]["detected_at"].startswith("2026-08-28T08:43:16") for item in items)


async def test_formal_knowledge_prefers_included_units_and_keeps_source_trace(governance_session):
    governance_session.add_all(
        [
            FeishuKnowledgeUnit(
                unit_id="unit-included",
                unit_key="section:deployment",
                lineage_key="section:deployment",
                version_id="version-current",
                item_id="item-current",
                unit_index=0,
                unit_type="SECTION",
                title="部署前置条件",
                content="生产环境至少需要八核处理器。",
                content_hash="unit-included-hash",
                source_segment_ids=["segment-1", "segment-2"],
                locator_json={"page": 3},
                recommended_outcome="PUBLISH",
                recommendation_reason="内容完整且没有冲突。",
                publication_state="INCLUDED",
            ),
            FeishuKnowledgeUnit(
                unit_id="unit-excluded",
                unit_key="section:marketing",
                lineage_key="section:marketing",
                version_id="version-current",
                item_id="item-current",
                unit_index=1,
                unit_type="SECTION",
                title="宣传口号",
                content="行业领先。",
                content_hash="unit-excluded-hash",
                source_segment_ids=["segment-3"],
                recommended_outcome="EXCLUDE",
                recommendation_reason="不属于可复用知识。",
                publication_state="EXCLUDED",
            ),
        ]
    )
    await governance_session.commit()

    items = await GovernanceService(governance_session).list_formal_knowledge("source-1")

    assert len(items) == 1
    assert items[0]["knowledge_level"] == "UNIT"
    assert items[0]["knowledge_id"] == "item-current:section:deployment"
    assert items[0]["unit_id"] == "unit-included"
    assert items[0]["title"] == "部署前置条件"
    assert items[0]["source_item_id"] == "item-current"
    assert items[0]["source_title"] == "Q900 部署指南"
    assert items[0]["source_segment_count"] == 2
    assert items[0]["source_locator"] == {"page": 3}


async def test_formal_knowledge_reports_reminder_and_offline_state_without_hiding_management_record(
    governance_session,
):
    now = datetime.now(UTC)
    unit = FeishuKnowledgeUnit(
        unit_id="unit-lifecycle",
        unit_key="section:lifecycle",
        lineage_key="section:lifecycle",
        version_id="version-current",
        item_id="item-current",
        unit_index=0,
        unit_type="SECTION",
        title="生命周期说明",
        content="知识有效期和复核日期只用于提醒。",
        content_hash="unit-lifecycle-hash",
        source_segment_ids=["segment-lifecycle"],
        recommended_outcome="PUBLISH",
        recommendation_reason="内容完整。",
        publication_state="INCLUDED",
        lifecycle_status="ACTIVE",
        valid_until=now - timedelta(days=1),
        review_due_at=now - timedelta(days=2),
    )
    governance_session.add(unit)
    await governance_session.commit()

    items = await GovernanceService(governance_session).list_formal_knowledge("source-1")
    assert items[0]["lifecycle_status"] == "EXPIRED"
    assert items[0]["stored_lifecycle_status"] == "ACTIVE"
    assert items[0]["index_status"] == "INDEXED"

    unit.valid_until = now + timedelta(days=30)
    await governance_session.commit()
    items = await GovernanceService(governance_session).list_formal_knowledge("source-1")
    assert items[0]["lifecycle_status"] == "REVIEW_DUE"
    assert items[0]["index_status"] == "INDEXED"

    source_item = await governance_session.scalar(
        select(FeishuSourceItem).where(FeishuSourceItem.item_id == "item-current")
    )
    current_version = await governance_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-current")
    )
    source_item.publication_status = "OFFLINE"
    current_version.yuxi_file_id = None
    await governance_session.commit()

    items = await GovernanceService(governance_session).list_formal_knowledge("source-1")
    assert len(items) == 1
    assert items[0]["source_publication_status"] == "OFFLINE"
    assert items[0]["lifecycle_status"] == "OFFLINE"
    assert items[0]["index_status"] == "OFFLINE"


async def test_formal_knowledge_versions_mark_only_archived_approved_history_as_rollback_available(
    governance_session,
):
    current = await governance_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-current")
    )
    current.source_object_path = "source-1/item-current/version-current.docx"
    governance_session.add_all(
        [
            FeishuMaterialVersion(
                version_id="version-history-ready",
                item_id="item-current",
                revision="0",
                content_hash="history-ready-hash",
                source_object_path="source-1/item-current/version-history-ready.docx",
                processing_status="replaced",
                review_status="approved",
                yuxi_file_id="file-history-ready",
                published_at=datetime.now(UTC) - timedelta(days=30),
            ),
            FeishuMaterialVersion(
                version_id="version-history-no-archive",
                item_id="item-current",
                revision="-1",
                content_hash="history-no-archive-hash",
                processing_status="replaced",
                review_status="approved",
                published_at=datetime.now(UTC) - timedelta(days=60),
            ),
        ]
    )
    await governance_session.commit()

    versions = await GovernanceService(governance_session).list_knowledge_versions("item-current")
    by_id = {version["version_id"]: version for version in versions}
    assert by_id["version-current"]["active"] is True
    assert by_id["version-current"]["rollback_available"] is False
    assert by_id["version-history-ready"]["rollback_available"] is True
    assert by_id["version-history-no-archive"]["rollback_available"] is False


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
        yuxi_file_id="file-new",
    )
    governance_session.add_all([current_item, current])
    await governance_session.commit()

    async def content_loader(file_id: str) -> str:
        if file_id == "file-candidate":
            return "金融行业解决方案\n本方案用于银行业务流程管理和风险控制。"
        assert file_id in {"file-current", "file-new"}
        return "公司简介\n善达信息专注企业数字化服务，为客户提供知识管理和智能助手产品。"

    service = CrossDocumentComparisonService(
        governance_session,
        content_loader=content_loader,
    )
    first = await service.compare_version("version-new")
    second = await service.compare_version("version-new")
    await governance_session.commit()

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].relation_id == second[0].relation_id
    assert (
        await governance_session.scalar(
            select(FeishuGovernanceReview).where(FeishuGovernanceReview.version_id == "version-new")
        )
        is not None
    )
    assert (
        await governance_session.scalar(
            select(func.count())
            .select_from(FeishuCrossDocumentRelation)
            .where(FeishuCrossDocumentRelation.comparison_key == "version-current:version-new")
        )
        == 1
    )


async def test_comparison_includes_published_knowledge_beyond_metadata_candidate_limit(
    governance_session,
    monkeypatch,
):
    monkeypatch.setattr(
        CrossDocumentComparisonService,
        "MAX_CANDIDATE_SCAN",
        CrossDocumentComparisonService.MAX_CANDIDATES,
    )
    now = datetime.now(UTC)
    pending_item = FeishuSourceItem(
        item_id="item-pending-review",
        source_id="source-1",
        item_key="page:pending-review",
        item_type="docx",
        title="数据治理审批规范",
        path_text="制度中心 / 数据治理",
        source_validity="valid",
    )
    pending_version = FeishuMaterialVersion(
        version_id="version-pending-review",
        item_id=pending_item.item_id,
        revision="1",
        content_hash="pending-review-hash",
        processing_status="awaiting_review",
        review_status="pending",
        yuxi_file_id="file-pending-review",
    )
    published_item = FeishuSourceItem(
        item_id="item-published-reference",
        source_id="source-1",
        item_key="page:published-reference",
        item_type="pdf",
        title="采购管理制度附件",
        path_text="采购中心 / 制度附件",
        source_validity="valid",
        active_version_id="version-published-reference",
    )
    published_version = FeishuMaterialVersion(
        version_id="version-published-reference",
        item_id=published_item.item_id,
        revision="8",
        content_hash="published-reference-hash",
        processing_status="published",
        review_status="approved",
        yuxi_file_id="file-published-reference",
        published_at=now - timedelta(days=30),
        created_at=now - timedelta(days=30),
    )
    distractors = []
    for index in range(CrossDocumentComparisonService.MAX_CANDIDATES):
        item = FeishuSourceItem(
            item_id=f"item-metadata-distractor-{index}",
            source_id="source-1",
            item_key=f"page:metadata-distractor-{index}",
            item_type="docx",
            title=pending_item.title,
            path_text=pending_item.path_text,
            source_validity="valid",
        )
        version = FeishuMaterialVersion(
            version_id=f"version-metadata-distractor-{index}",
            item_id=item.item_id,
            revision="1",
            content_hash=f"metadata-distractor-hash-{index}",
            processing_status="awaiting_review",
            review_status="pending",
            yuxi_file_id=f"file-metadata-distractor-{index}",
            created_at=now - timedelta(minutes=index),
        )
        distractors.extend([item, version])
    governance_session.add_all(
        [pending_item, pending_version, published_item, published_version, *distractors]
    )
    await governance_session.commit()

    shared_passage = "正式制度规定，所有采购项目必须完成供应商资质核验、报价复核和审批留痕后方可执行。"

    async def content_loader(file_id: str) -> str:
        if file_id in {"file-pending-review", "file-published-reference"}:
            return shared_passage
        return f"{file_id} 仅用于说明其他数据治理流程，与采购制度内容无关。"

    relations = await CrossDocumentComparisonService(
        governance_session,
        content_loader=content_loader,
    ).compare_version(pending_version.version_id)

    assert any(
        {relation.source_version_id, relation.target_version_id}
        == {pending_version.version_id, published_version.version_id}
        for relation in relations
    )


async def test_comparison_invalidates_same_source_item_history_relation(governance_session):
    historical_version = FeishuMaterialVersion(
        version_id="version-current-history",
        item_id="item-current",
        revision="0",
        content_hash="current-hash",
        processing_status="awaiting_review",
        review_status="pending",
        yuxi_file_id="file-current-history",
    )
    same_item_relation = FeishuCrossDocumentRelation(
        relation_id="relation-same-source-history",
        comparison_key="version-current-history:version-current",
        source_version_id="version-current-history",
        target_version_id="version-current",
        relation_type="EXACT_DUPLICATE",
        similarity=1.0,
        confidence=0.99,
        same_content=["同一飞书资料的不同版本", "内容哈希一致", "正文局部相似度 100%"],
        status="open",
    )
    governance_session.add_all([historical_version, same_item_relation])
    await governance_session.commit()

    async def content_loader(file_id: str) -> str:
        if file_id in {"file-current", "file-current-history"}:
            return "相同的正式知识正文内容，用于验证同一资料历史版本不进入跨文档检查。"
        return "金融行业解决方案，与当前产品资料无关。"

    relations = await CrossDocumentComparisonService(
        governance_session,
        content_loader=content_loader,
    ).compare_version("version-current")

    assert all(
        {relation.source_version_id, relation.target_version_id}
        != {"version-current", "version-current-history"}
        for relation in relations
    )
    assert same_item_relation.status == "invalidated"
    assert same_item_relation.human_decision == "SAME_SOURCE_HISTORY"


async def test_comparison_excludes_invalid_source_and_invalidates_open_relation(governance_session):
    candidate_item = await governance_session.scalar(
        select(FeishuSourceItem).where(FeishuSourceItem.item_id == "item-candidate")
    )
    relation = await governance_session.scalar(
        select(FeishuCrossDocumentRelation).where(FeishuCrossDocumentRelation.relation_id == "relation-1")
    )
    candidate_item.source_validity = "invalid"
    await governance_session.commit()

    async def content_loader(file_id: str) -> str:
        return f"{file_id} 的正文内容"

    result = await CrossDocumentComparisonService(
        governance_session,
        content_loader=content_loader,
    ).compare_source("source-1")

    assert result["total"] == 1
    assert relation.status == "invalidated"
    assert relation.human_decision == "SOURCE_INVALIDATED"
    assert relation.human_comment == "关联的飞书资料已失效，不再参与跨文档检查"


async def test_comparison_does_not_treat_title_or_path_similarity_as_content_overlap(
    governance_session,
):
    current = await governance_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-current")
    )
    candidate = await governance_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-candidate")
    )
    current_item = await governance_session.scalar(
        select(FeishuSourceItem).where(FeishuSourceItem.item_id == current.item_id)
    )
    candidate_item = await governance_session.scalar(
        select(FeishuSourceItem).where(FeishuSourceItem.item_id == candidate.item_id)
    )
    candidate_item.title = "Q900 部署指南副本"
    candidate_item.path_text = "产品资料 / Q900 / 副本"

    evidence = CrossDocumentComparisonService._classify(
        current,
        current_item,
        candidate,
        candidate_item,
        current_content="",
        candidate_content="",
    )

    assert evidence is None


async def test_comparison_uses_matching_body_passages_as_overlap_evidence(governance_session):
    current = await governance_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-current")
    )
    candidate = await governance_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-candidate")
    )
    current_item = await governance_session.scalar(
        select(FeishuSourceItem).where(FeishuSourceItem.item_id == current.item_id)
    )
    candidate_item = await governance_session.scalar(
        select(FeishuSourceItem).where(FeishuSourceItem.item_id == candidate.item_id)
    )
    shared = "善达信息专注企业数字化服务，为客户提供知识管理、智能助手和持续运营服务。"

    evidence = CrossDocumentComparisonService._classify(
        current,
        current_item,
        candidate,
        candidate_item,
        current_content=f"产品手册\n{shared}\n部署要求另见附件。",
        candidate_content=f"解决方案\n{shared}\n本方案适用于制造行业。",
    )

    assert evidence is not None
    assert evidence["relation_type"] == "OVERLAP"
    assert evidence["similarity"] == 1.0
    assert "正文局部相似度 100%" in evidence["same_content"]


async def test_open_conflict_is_detected_for_publish_guard(governance_session):
    assert await GovernanceService(governance_session).has_open_conflict("version-candidate") is True
