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
    assert comparisons[0]["different_content"][0]["field"] == "GPU 数量"


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
