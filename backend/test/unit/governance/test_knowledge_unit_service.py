from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.governance.domain import ReviewOutcome
from yuxi.governance.knowledge_unit_service import KnowledgeUnitService, build_knowledge_unit_drafts
from yuxi.governance.review_package_service import ReviewPackageService
from yuxi.governance.schemas import (
    ReviewItemDecisionRequest,
    ReviewPackageBulkExcludeRequest,
    ReviewPackageResolveRequest,
)
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuMaterialVersion,
    FeishuKnowledgeUnit,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSource,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
    FeishuSourceSegment,
)


def _segment(
    index: int,
    content: str,
    *,
    slide: int | None = None,
    title: str = "产品能力",
) -> FeishuSourceSegment:
    return FeishuSourceSegment(
        segment_id=f"segment-{index}",
        segment_key=f"segment-key-{index}",
        version_id="version-1",
        item_id="item-1",
        segment_index=index,
        segment_type="slide" if slide is not None else "text",
        title_path=[title],
        locator_json={"slide": slide} if slide is not None else {},
        content=content,
        content_hash=f"hash-{index}",
        token_count=20,
        publication_state="PENDING",
        status="ACTIVE",
    )


def test_same_slide_segments_form_one_unit_and_slide_boundary_splits_units():
    segments = [
        _segment(0, "第一段介绍产品的核心能力。", slide=1),
        _segment(1, "第二段补充产品的适用场景。", slide=1, title="适用场景"),
        _segment(2, "第二页说明产品的部署要求。", slide=2, title="部署要求"),
    ]

    drafts = build_knowledge_unit_drafts(segments, item_id="item-1")

    assert len(drafts) == 2
    assert drafts[0].source_segment_ids == ["segment-0", "segment-1"]
    assert drafts[1].source_segment_ids == ["segment-2"]
    assert drafts[0].locator["slide"] == 1
    assert drafts[1].locator["slide"] == 2


def test_every_active_source_segment_is_assigned_once():
    segments = [
        _segment(0, "第一章包含完整的准备工作说明。", title="准备工作"),
        _segment(1, "第二章包含完整的实施步骤说明。", title="实施步骤"),
        _segment(2, "第三章包含完整的验收检查说明。", title="验收检查"),
    ]

    drafts = build_knowledge_unit_drafts(segments, item_id="item-1")
    assigned = [segment_id for draft in drafts for segment_id in draft.source_segment_ids]

    assert sorted(assigned) == ["segment-0", "segment-1", "segment-2"]
    assert len(assigned) == len(set(assigned))


def test_cover_or_directory_unit_is_recommended_for_exclusion():
    draft = build_knowledge_unit_drafts(
        [_segment(0, "目录\n产品介绍\n实施方案", slide=1, title="目录")],
        item_id="item-1",
    )[0]

    outcome, _, confidence, manual_required = KnowledgeUnitService._recommendation(
        draft,
        previous_unit=None,
        change_type="NEW",
    )

    assert outcome == ReviewOutcome.EXCLUDE
    assert confidence == 0.98
    assert manual_required is False


@pytest.fixture
async def unit_review_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
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
            source_validity="valid",
        )
        version = FeishuMaterialVersion(
            version_id="version-1",
            item_id="item-1",
            revision="1",
            content_hash="hash-1",
            processing_status="awaiting_review",
            processing_params={
                "comparison": {"status": "completed"},
                "content_quality": {"checked": True, "has_body": True, "body_length": 120},
            },
            review_status="pending",
            yuxi_file_id="file-1",
        )
        package = FeishuReviewPackage(
            package_id="package-1",
            package_key="package-1",
            source_id="source-1",
            source_item_id="item-1",
            source_version_id="version-1",
            trigger_type="SOURCE_VERSION",
            title_snapshot="部署指南",
            workflow_status="OPEN",
            risk_level="LOW",
            lock_version=1,
            created_at=now,
            updated_at=now,
        )
        material_item = FeishuReviewItem(
            review_item_id="review-material-1",
            package_id="package-1",
            candidate_key="material:version-1",
            review_type="NEW",
            subject_type="MATERIAL_VERSION",
            subject_id="version-1",
            title="部署指南",
            item_status="PENDING",
            created_at=now,
            updated_at=now,
        )
        segments = [
            FeishuSourceSegment(
                segment_id="segment-install",
                segment_key="install",
                version_id="version-1",
                item_id="item-1",
                yuxi_file_id="file-1",
                segment_index=0,
                segment_type="text",
                title_path=["安装步骤"],
                locator_json={"page": 1},
                content="安装前需要准备管理员账号和服务地址，然后按步骤完成部署。",
                content_hash="segment-install-hash",
                token_count=30,
                publication_state="PENDING",
                status="ACTIVE",
            ),
            FeishuSourceSegment(
                segment_id="segment-check",
                segment_key="check",
                version_id="version-1",
                item_id="item-1",
                yuxi_file_id="file-1",
                segment_index=1,
                segment_type="text",
                title_path=["验收检查"],
                locator_json={"page": 2},
                content="部署完成后检查登录、检索和权限隔离功能是否符合验收要求。",
                content_hash="segment-check-hash",
                token_count=28,
                publication_state="PENDING",
                status="ACTIVE",
            ),
        ]
        session.add_all([source, source_item, version, package, material_item, *segments])
        await session.commit()
        yield session, package, segments
    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_converts_material_review_to_idempotent_unit_items(unit_review_session):
    session, package, _ = unit_review_session
    service = KnowledgeUnitService(session)

    first = await service.ensure_for_package(package)
    second = await service.ensure_for_package(package)
    detail = await ReviewPackageService(session).get_package(package.package_id)
    stored_items = list(
        await session.scalars(select(FeishuReviewItem).where(FeishuReviewItem.package_id == package.package_id))
    )

    assert [unit.unit_id for unit in first] == [unit.unit_id for unit in second]
    assert len(detail["items"]) == detail["knowledge_unit_count"] == 2
    assert detail["safe_recommendation_count"] == 2
    assert all(item["knowledge_unit"] for item in detail["items"])
    assert all(item["content"] for item in detail["items"])
    invalidated_materials = [
        item for item in stored_items if item.subject_type == "MATERIAL_VERSION" and item.item_status == "INVALIDATED"
    ]
    assert len(invalidated_materials) == 1


@pytest.mark.asyncio
async def test_resolved_relation_remains_visible_without_affecting_review_advice(unit_review_session):
    session, package, _ = unit_review_session
    material_item = await session.scalar(
        select(FeishuReviewItem).where(FeishuReviewItem.review_item_id == "review-material-1")
    )
    material_item.relation_ids = ["relation-resolved"]
    session.add_all(
        [
            FeishuSourceItem(
                item_id="item-published",
                source_id="source-1",
                item_key="page:item-published",
                item_type="docx",
                title="已发布部署说明",
                source_validity="valid",
            ),
            FeishuMaterialVersion(
                version_id="version-published",
                item_id="item-published",
                revision="1",
                content_hash="hash-published",
                processing_status="published",
                processing_params={},
                review_status="published",
                yuxi_file_id="file-published",
            ),
            FeishuCrossDocumentRelation(
                relation_id="relation-resolved",
                comparison_key="version-1:version-published",
                source_version_id="version-1",
                target_version_id="version-published",
                relation_type="CONFLICT",
                similarity=0.9,
                confidence=0.9,
                same_content=["安装前需要准备管理员账号和服务地址"],
                different_content=[{"current": "按步骤完成部署", "candidate": "直接完成部署"}],
                status="resolved",
                human_decision="KEEP_CURRENT",
            ),
        ]
    )
    await session.flush()

    detail = await ReviewPackageService(session).get_package(package.package_id)

    related_items = [item for item in detail["items"] if "relation-resolved" in item["relation_ids"]]
    assert len(related_items) == 1
    assert related_items[0]["title"] == "安装步骤"
    assert related_items[0]["review_type"] == "NEW"
    assert related_items[0]["problem_tags"] == []
    assert related_items[0]["recommended_outcome"] == "PUBLISH"
    assert related_items[0]["manual_review_required"] is False
    assert [relation["relation_id"] for relation in detail["relations"]] == ["relation-resolved"]


@pytest.mark.asyncio
async def test_mixed_unit_decisions_publish_only_included_segments(unit_review_session):
    session, package, segments = unit_review_session
    await KnowledgeUnitService(session).ensure_for_package(package)
    unit_items = list(
        await session.scalars(
            select(FeishuReviewItem)
            .where(
                FeishuReviewItem.package_id == package.package_id,
                FeishuReviewItem.subject_type == "KNOWLEDGE_UNIT",
            )
            .order_by(FeishuReviewItem.title.asc())
        )
    )
    payload = ReviewPackageResolveRequest(
        request_id="mixed-unit-review",
        lock_version=package.lock_version,
        decisions=[
            ReviewItemDecisionRequest(
                review_item_id=unit_items[0].review_item_id,
                outcome="PUBLISH",
            ),
            ReviewItemDecisionRequest(
                review_item_id=unit_items[1].review_item_id,
                outcome="EXCLUDE",
            ),
        ],
    )

    result = await ReviewPackageService(session).resolve(
        package.package_id,
        payload,
        operator_id="admin-a",
    )

    assert result["workflow_status"] == "COMPLETED"
    assert result["publish_version_ids"] == []
    assert result["unit_publish_version_ids"] == ["version-1"]
    assert result["resolved_unit_count"] == 2
    assert result["remaining_unit_count"] == 0
    assert result["included_unit_count"] == 1
    assert result["excluded_unit_count"] == 1
    assert result["reject_candidates"] == []
    assert sorted(segment.publication_state for segment in segments) == ["EXCLUDED", "INCLUDED"]


@pytest.mark.parametrize(
    ("outcomes", "completion_result"),
    [
        (("PUBLISH", "PUBLISH"), "all_included"),
        (("PUBLISH", "EXCLUDE"), "partial"),
        (("EXCLUDE", "EXCLUDE"), "all_excluded"),
    ],
)
@pytest.mark.asyncio
async def test_completed_packages_can_be_filtered_by_unit_result(
    unit_review_session,
    outcomes,
    completion_result,
):
    session, package, _ = unit_review_session
    service = ReviewPackageService(session)
    await KnowledgeUnitService(session).ensure_for_package(package)
    unit_items = list(
        await session.scalars(
            select(FeishuReviewItem)
            .where(
                FeishuReviewItem.package_id == package.package_id,
                FeishuReviewItem.subject_type == "KNOWLEDGE_UNIT",
            )
            .order_by(FeishuReviewItem.title.asc())
        )
    )
    await service.resolve(
        package.package_id,
        ReviewPackageResolveRequest(
            request_id=f"complete-as-{completion_result}",
            lock_version=package.lock_version,
            decisions=[
                ReviewItemDecisionRequest(
                    review_item_id=item.review_item_id,
                    outcome=outcome,
                )
                for item, outcome in zip(unit_items, outcomes, strict=True)
            ],
        ),
        operator_id="admin-a",
    )

    matched = await service.list_packages(
        "source-1",
        operator_id="admin-a",
        view="all",
        workflow_statuses=["COMPLETED"],
        completion_result=completion_result,
    )
    unmatched_result = next(
        value for value in ("all_included", "partial", "all_excluded") if value != completion_result
    )
    unmatched = await service.list_packages(
        "source-1",
        operator_id="admin-a",
        view="all",
        workflow_statuses=["COMPLETED"],
        completion_result=unmatched_result,
    )

    assert [item["package_id"] for item in matched["items"]] == [package.package_id]
    assert matched["items"][0]["completion_result"] == completion_result
    assert unmatched["items"] == []


@pytest.mark.asyncio
async def test_single_unit_publish_keeps_package_open_and_reports_progress(unit_review_session):
    session, package, segments = unit_review_session
    await KnowledgeUnitService(session).ensure_for_package(package)
    unit_items = list(
        await session.scalars(
            select(FeishuReviewItem)
            .where(
                FeishuReviewItem.package_id == package.package_id,
                FeishuReviewItem.subject_type == "KNOWLEDGE_UNIT",
            )
            .order_by(FeishuReviewItem.title.asc())
        )
    )

    result = await ReviewPackageService(session).resolve(
        package.package_id,
        ReviewPackageResolveRequest(
            request_id="single-unit-publish",
            lock_version=package.lock_version,
            decisions=[
                ReviewItemDecisionRequest(
                    review_item_id=unit_items[0].review_item_id,
                    outcome="PUBLISH",
                )
            ],
        ),
        operator_id="admin-a",
    )

    assert result["workflow_status"] == "OPEN"
    assert result["publish_version_ids"] == []
    assert result["unit_publish_version_ids"] == ["version-1"]
    assert result["resolved_unit_count"] == 1
    assert result["remaining_unit_count"] == 1
    assert result["included_unit_count"] == 1
    assert result["affected_unit_titles"] == [unit_items[0].title]
    assert sorted(segment.publication_state for segment in segments) == ["INCLUDED", "PENDING"]


@pytest.mark.asyncio
async def test_all_excluded_units_reject_material_and_preserve_alias_segment(unit_review_session):
    session, package, segments = unit_review_session
    segments[1].publication_state = "ALIAS"
    await KnowledgeUnitService(session).ensure_for_package(package)
    unit_items = list(
        await session.scalars(
            select(FeishuReviewItem).where(
                FeishuReviewItem.package_id == package.package_id,
                FeishuReviewItem.subject_type == "KNOWLEDGE_UNIT",
            )
        )
    )
    payload = ReviewPackageResolveRequest(
        request_id="exclude-all-unit-review",
        lock_version=package.lock_version,
        decisions=[
            ReviewItemDecisionRequest(
                review_item_id=item.review_item_id,
                outcome="EXCLUDE",
            )
            for item in unit_items
        ],
    )

    result = await ReviewPackageService(session).resolve(
        package.package_id,
        payload,
        operator_id="admin-a",
    )

    assert result["publish_version_ids"] == []
    assert result["reject_candidates"] == [{"version_id": "version-1", "reason": "不纳入知识库"}]
    assert segments[0].publication_state == "EXCLUDED"
    assert segments[1].publication_state == "ALIAS"


@pytest.mark.asyncio
async def test_bulk_exclude_closes_source_changes_and_finalizes_units_atomically(unit_review_session):
    session, package, segments = unit_review_session
    service = ReviewPackageService(session)
    await KnowledgeUnitService(session).ensure_for_package(package)
    unit_items = list(
        await session.scalars(
            select(FeishuReviewItem).where(
                FeishuReviewItem.package_id == package.package_id,
                FeishuReviewItem.subject_type == "KNOWLEDGE_UNIT",
            )
        )
    )
    await service.resolve(
        package.package_id,
        ReviewPackageResolveRequest(
            request_id="request-source-changes",
            lock_version=package.lock_version,
            decisions=[
                ReviewItemDecisionRequest(
                    review_item_id=item.review_item_id,
                    outcome="REQUEST_SOURCE_CHANGE",
                    decision_comment="请补充原始资料",
                )
                for item in unit_items
            ],
        ),
        operator_id="admin-a",
    )
    request = ReviewPackageBulkExcludeRequest(
        request_id="request-bulk-exclude",
        lock_version=package.lock_version,
        review_item_ids=[item.review_item_id for item in unit_items],
        decision_comment="结束资料修改任务并批量不纳入知识库",
    )

    result = await service.bulk_exclude(package.package_id, request, operator_id="admin-a")
    replayed = await service.bulk_exclude(package.package_id, request, operator_id="admin-a")
    change_requests = list(await session.scalars(select(FeishuSourceChangeRequest)))
    knowledge_units = list(await session.scalars(select(FeishuKnowledgeUnit)))

    assert result["workflow_status"] == "COMPLETED"
    assert result["closed_change_request_count"] == 2
    assert result["remaining_unit_count"] == 0
    assert result["excluded_unit_count"] == 2
    assert result["reject_candidates"] == [
        {
            "version_id": "version-1",
            "reason": "结束资料修改任务并批量不纳入知识库",
        }
    ]
    assert replayed["idempotent_replay"] is True
    assert all(request.status == "CANCELLED" for request in change_requests)
    assert all(item.item_status == "DECIDED" and item.outcome == "EXCLUDE" for item in unit_items)
    assert all(unit.publication_state == "EXCLUDED" for unit in knowledge_units)
    assert all(segment.publication_state == "EXCLUDED" for segment in segments)


@pytest.mark.asyncio
async def test_excluded_unit_can_be_reopened_once_as_a_new_review(unit_review_session):
    session, package, segments = unit_review_session
    service = ReviewPackageService(session)
    await KnowledgeUnitService(session).ensure_for_package(package)
    original = await session.scalar(
        select(FeishuReviewItem)
        .where(
            FeishuReviewItem.package_id == package.package_id,
            FeishuReviewItem.subject_type == "KNOWLEDGE_UNIT",
        )
        .order_by(FeishuReviewItem.title.asc())
    )
    await service.resolve(
        package.package_id,
        ReviewPackageResolveRequest(
            request_id="exclude-before-reopen",
            lock_version=package.lock_version,
            decisions=[
                ReviewItemDecisionRequest(
                    review_item_id=original.review_item_id,
                    outcome="EXCLUDE",
                )
            ],
        ),
        operator_id="admin-a",
    )

    reopened = await service.reopen_excluded_item(original.review_item_id, operator_id="admin-a")
    replayed = await service.reopen_excluded_item(original.review_item_id, operator_id="admin-a")
    original_detail = await service.get_package(package.package_id)
    reopened_detail = await service.get_package(reopened["package_id"])
    stored_reopened_items = list(
        await session.scalars(
            select(FeishuReviewItem).where(FeishuReviewItem.reopened_from_item_id == original.review_item_id)
        )
    )
    unit = await session.scalar(select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.unit_id == original.subject_id))
    unit_segments = [segment for segment in segments if segment.segment_id in unit.source_segment_ids]

    assert reopened["idempotent_replay"] is False
    assert replayed == {**reopened, "idempotent_replay": True}
    assert len(stored_reopened_items) == 1
    assert reopened_detail["trigger_type"] == "FEEDBACK"
    assert reopened_detail["knowledge_unit_count"] == 1
    assert reopened_detail["items"][0]["reopened_from_item_id"] == original.review_item_id
    assert reopened_detail["items"][0]["item_status"] == "PENDING"
    assert reopened_detail["items"][0]["manual_review_required"] is True
    original_item = next(item for item in original_detail["items"] if item["review_item_id"] == original.review_item_id)
    assert original_item["can_reopen_exclusion"] is False
    assert original_item["reopened_by_item_id"] == reopened["review_item_id"]
    assert unit.publication_state == "PENDING"
    assert all(segment.publication_state == "PENDING" for segment in unit_segments)


@pytest.mark.asyncio
async def test_pending_comparison_keeps_new_units_out_of_safe_batch(unit_review_session):
    session, package, _ = unit_review_session
    version = await session.scalar(select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-1"))
    version.processing_params = {"comparison": {"status": "running"}}

    detail = await ReviewPackageService(session).get_package(package.package_id)

    assert detail["attention_unit_count"] == 2
    assert detail["safe_recommendation_count"] == 0
    assert all(item["manual_review_required"] for item in detail["items"])
    assert all(item["comparison_status"] == "running" for item in detail["items"])


@pytest.mark.asyncio
async def test_adopt_new_version_supersedes_published_conflicting_unit(unit_review_session):
    session, package, segments = unit_review_session
    await KnowledgeUnitService(session).ensure_for_package(package)
    candidate_unit = await session.scalar(
        select(FeishuKnowledgeUnit)
        .where(FeishuKnowledgeUnit.version_id == "version-1")
        .order_by(FeishuKnowledgeUnit.unit_index.asc())
    )
    counterpart_item = FeishuSourceItem(
        item_id="item-published",
        source_id="source-1",
        item_key="page:item-published",
        item_type="docx",
        title="已发布部署说明",
        source_validity="valid",
    )
    counterpart_version = FeishuMaterialVersion(
        version_id="version-published",
        item_id="item-published",
        revision="1",
        content_hash="hash-published",
        processing_status="published",
        processing_params={},
        review_status="published",
        yuxi_file_id="file-published",
    )
    counterpart_package = FeishuReviewPackage(
        package_id="package-published",
        package_key="package-published",
        source_id="source-1",
        source_item_id="item-published",
        source_version_id="version-published",
        trigger_type="SOURCE_VERSION",
        title_snapshot="已发布部署说明",
        workflow_status="OPEN",
        risk_level="HIGH",
        lock_version=1,
        quality_gate_status="BLOCKED",
        quality_score=40,
        quality_dimensions={"consistency": {"score": 0}},
        impact_summary={"blockReasons": [{"code": "OPEN_CONFLICT"}]},
        auto_close_eligible=True,
        quality_computed_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    counterpart_review_item = FeishuReviewItem(
        review_item_id="review-published",
        package_id="package-published",
        candidate_key="unit:unit-published",
        review_type="CONFLICT",
        subject_type="KNOWLEDGE_UNIT",
        subject_id="unit-published",
        title="安装步骤",
        item_status="PENDING",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    counterpart_segment = FeishuSourceSegment(
        segment_id="segment-published",
        segment_key="published",
        version_id="version-published",
        item_id="item-published",
        yuxi_file_id="file-published",
        segment_index=0,
        segment_type="text",
        title_path=["安装步骤"],
        locator_json={"page": 1},
        content="安装前需要准备管理员账号和服务地址，然后按步骤完成部署。",
        content_hash="published-segment-hash",
        token_count=30,
        publication_state="INCLUDED",
        status="ACTIVE",
    )
    counterpart_unit = FeishuKnowledgeUnit(
        unit_id="unit-published",
        unit_key="published-unit",
        lineage_key="published-lineage",
        version_id="version-published",
        item_id="item-published",
        unit_index=0,
        unit_type="SECTION",
        title="安装步骤",
        content=counterpart_segment.content,
        content_hash="published-unit-hash",
        source_segment_ids=[counterpart_segment.segment_id],
        locator_json={"page": 1},
        change_type="NEW",
        recommended_outcome="PUBLISH",
        recommendation_reason="已发布",
        publication_state="INCLUDED",
        applicability_scope={"product": "Q900", "product_version": "V1"},
    )
    relation = FeishuCrossDocumentRelation(
        relation_id="relation-adopt",
        comparison_key="version-1:version-published:adopt",
        source_version_id="version-1",
        target_version_id="version-published",
        relation_type="CONFLICT",
        same_content=["安装前需要准备管理员账号和服务地址"],
        different_content=[{"field": "部署方式", "current": "按步骤完成部署", "candidate": "直接完成部署"}],
        status="open",
    )
    session.add_all(
        [
            counterpart_item,
            counterpart_version,
            counterpart_package,
            counterpart_review_item,
            counterpart_segment,
            counterpart_unit,
            relation,
        ]
    )
    await session.flush()
    detail = await ReviewPackageService(session).get_package(package.package_id)
    conflict_item = next(item for item in detail["items"] if item["subject_id"] == candidate_unit.unit_id)
    conflict_item_record = await session.scalar(
        select(FeishuReviewItem).where(FeishuReviewItem.review_item_id == conflict_item["review_item_id"])
    )
    result = await ReviewPackageService(session).resolve(
        package.package_id,
        ReviewPackageResolveRequest(
            request_id="adopt-conflict",
            lock_version=package.lock_version,
            decisions=[
                ReviewItemDecisionRequest(
                    review_item_id=conflict_item_record.review_item_id,
                    outcome="ADOPT_NEW_VERSION",
                    problem_tags=["CONFLICT"],
                    decision_comment="采用新版并替代旧结论",
                )
            ],
        ),
        operator_id="admin-a",
    )

    assert result["counterpart_actions"][0]["unit_id"] == "unit-published"
    assert relation.status == "resolved"
    assert counterpart_unit.publication_state == "EXCLUDED"
    assert counterpart_unit.lifecycle_status == "OFFLINE"
    assert counterpart_segment.publication_state == "EXCLUDED"
    assert counterpart_review_item.item_status == "DECIDED"
    assert counterpart_review_item.outcome == "KEEP_CURRENT"
    assert counterpart_package.workflow_status == "COMPLETED"
    assert counterpart_package.quality_gate_status is None
    assert counterpart_package.quality_score is None
    assert counterpart_package.quality_dimensions == {}
    assert counterpart_package.impact_summary == {}
    assert counterpart_package.auto_close_eligible is False
    assert counterpart_package.quality_computed_at is None
    assert all(segment.publication_state != "EXCLUDED" for segment in segments)


@pytest.mark.asyncio
async def test_split_scope_persists_candidate_scope_and_keeps_counterpart(unit_review_session):
    session, package, _ = unit_review_session
    await KnowledgeUnitService(session).ensure_for_package(package)
    candidate_unit = await session.scalar(
        select(FeishuKnowledgeUnit)
        .where(FeishuKnowledgeUnit.version_id == "version-1")
        .order_by(FeishuKnowledgeUnit.unit_index.asc())
    )
    counterpart = FeishuKnowledgeUnit(
        unit_id="unit-scope-counterpart",
        unit_key="scope-counterpart",
        lineage_key="scope-counterpart-lineage",
        version_id="version-scope-counterpart",
        item_id="item-scope-counterpart",
        unit_index=0,
        unit_type="SECTION",
        title=candidate_unit.title,
        content=candidate_unit.content,
        content_hash="scope-counterpart-hash",
        source_segment_ids=[],
        locator_json={"page": 1},
        change_type="NEW",
        recommended_outcome="PUBLISH",
        recommendation_reason="已发布",
        publication_state="INCLUDED",
        applicability_scope={"product": "Q900", "product_version": "V1"},
    )
    relation = FeishuCrossDocumentRelation(
        relation_id="relation-scope",
        comparison_key="version-1:version-scope-counterpart",
        source_version_id="version-1",
        target_version_id="version-scope-counterpart",
        relation_type="CONFLICT",
        same_content=[candidate_unit.content[:20]],
        different_content=[{"field": "产品版本", "current": "V1", "candidate": "V2"}],
        status="open",
    )
    session.add_all([counterpart, relation])
    await session.flush()
    detail = await ReviewPackageService(session).get_package(package.package_id)
    item = next(item for item in detail["items"] if item["subject_id"] == candidate_unit.unit_id)
    result = await ReviewPackageService(session).resolve(
        package.package_id,
        ReviewPackageResolveRequest(
            request_id="split-conflict",
            lock_version=package.lock_version,
            decisions=[
                ReviewItemDecisionRequest(
                    review_item_id=item["review_item_id"],
                    outcome="SPLIT_SCOPE",
                    problem_tags=["CONFLICT"],
                    applicability_scope={"product": "Q900", "product_version": "V2"},
                )
            ],
        ),
        operator_id="admin-a",
    )

    assert result["counterpart_actions"] == []
    assert relation.status == "resolved"
    assert candidate_unit.applicability_scope == {"product": "Q900", "product_version": "V2"}
    assert counterpart.publication_state == "INCLUDED"


@pytest.mark.asyncio
async def test_split_scope_rejects_overlapping_counterpart_scope(unit_review_session):
    session, package, _ = unit_review_session
    await KnowledgeUnitService(session).ensure_for_package(package)
    candidate_unit = await session.scalar(
        select(FeishuKnowledgeUnit)
        .where(FeishuKnowledgeUnit.version_id == "version-1")
        .order_by(FeishuKnowledgeUnit.unit_index.asc())
    )
    counterpart = FeishuKnowledgeUnit(
        unit_id="unit-overlap-counterpart",
        unit_key="overlap-counterpart",
        lineage_key="overlap-counterpart-lineage",
        version_id="version-overlap-counterpart",
        item_id="item-overlap-counterpart",
        unit_index=0,
        unit_type="SECTION",
        title=candidate_unit.title,
        content=candidate_unit.content,
        content_hash="overlap-counterpart-hash",
        source_segment_ids=[],
        locator_json={"page": 1},
        change_type="NEW",
        recommended_outcome="PUBLISH",
        recommendation_reason="已发布",
        publication_state="INCLUDED",
        applicability_scope={"product": "Q900", "product_version": "V1"},
    )
    relation = FeishuCrossDocumentRelation(
        relation_id="relation-overlap",
        comparison_key="version-1:version-overlap-counterpart",
        source_version_id="version-1",
        target_version_id="version-overlap-counterpart",
        relation_type="CONFLICT",
        same_content=[candidate_unit.content[:20]],
        different_content=[{"field": "价格", "current": "45万", "candidate": "30万"}],
        status="open",
    )
    session.add_all([counterpart, relation])
    await session.flush()
    detail = await ReviewPackageService(session).get_package(package.package_id)
    item = next(item for item in detail["items"] if item["subject_id"] == candidate_unit.unit_id)

    with pytest.raises(ValueError, match="适用范围.*重叠"):
        await ReviewPackageService(session).resolve(
            package.package_id,
            ReviewPackageResolveRequest(
                request_id="split-overlap-conflict",
                lock_version=package.lock_version,
                decisions=[
                    ReviewItemDecisionRequest(
                        review_item_id=item["review_item_id"],
                        outcome="SPLIT_SCOPE",
                        problem_tags=["CONFLICT"],
                        applicability_scope={"product": "Q900", "product_version": "V1"},
                    )
                ],
            ),
            operator_id="admin-a",
        )

    assert relation.status == "open"
    assert candidate_unit.publication_state == "PENDING"


@pytest.mark.asyncio
async def test_keep_current_rejects_when_counterpart_is_only_a_candidate(unit_review_session):
    session, package, _ = unit_review_session
    await KnowledgeUnitService(session).ensure_for_package(package)
    candidate_unit = await session.scalar(
        select(FeishuKnowledgeUnit)
        .where(FeishuKnowledgeUnit.version_id == "version-1")
        .order_by(FeishuKnowledgeUnit.unit_index.asc())
    )
    counterpart = FeishuKnowledgeUnit(
        unit_id="unit-pending-counterpart",
        unit_key="pending-counterpart",
        lineage_key="pending-counterpart-lineage",
        version_id="version-pending-counterpart",
        item_id="item-pending-counterpart",
        unit_index=0,
        unit_type="SECTION",
        title=candidate_unit.title,
        content=candidate_unit.content,
        content_hash="pending-counterpart-hash",
        source_segment_ids=[],
        locator_json={"page": 1},
        change_type="NEW",
        recommended_outcome="PUBLISH",
        recommendation_reason="待审核",
        publication_state="PENDING",
    )
    relation = FeishuCrossDocumentRelation(
        relation_id="relation-pending",
        comparison_key="version-1:version-pending-counterpart",
        source_version_id="version-1",
        target_version_id="version-pending-counterpart",
        relation_type="CONFLICT",
        same_content=[candidate_unit.content[:20]],
        different_content=[{"field": "价格", "current": "45万", "candidate": "30万"}],
        status="open",
    )
    session.add_all([counterpart, relation])
    await session.flush()
    detail = await ReviewPackageService(session).get_package(package.package_id)
    item = next(item for item in detail["items"] if item["subject_id"] == candidate_unit.unit_id)

    with pytest.raises(ValueError, match="另一侧尚未发布"):
        await ReviewPackageService(session).resolve(
            package.package_id,
            ReviewPackageResolveRequest(
                request_id="keep-pending-conflict",
                lock_version=package.lock_version,
                decisions=[
                    ReviewItemDecisionRequest(
                        review_item_id=item["review_item_id"],
                        outcome="KEEP_CURRENT",
                        problem_tags=["CONFLICT"],
                        decision_comment="等待正式版本确认",
                    )
                ],
            ),
            operator_id="admin-a",
        )
