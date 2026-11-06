import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
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
async def source_change_engine(request):
    schema = None
    if getattr(request, "param", "sqlite") == "postgres":
        if not os.environ.get("POSTGRES_URL"):
            raise RuntimeError("PostgreSQL acceptance requires POSTGRES_URL in the Docker test environment")
        schema = "test_publication_" + uuid4().hex
        engine = create_async_engine(
            os.environ["POSTGRES_URL"], execution_options={"schema_translate_map": {None: schema}}
        )
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            if schema:
                await connection.execute(text(f"CREATE SCHEMA {schema}"))
            await connection.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        if schema:
            async with engine.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()


@pytest.fixture
async def source_change_session(source_change_engine):
    engine = source_change_engine
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
        # Flush parents explicitly so PostgreSQL validates the actual publication path.
        for row in [source, source_item, *versions, package, review_item, change_request]:
            session.add(row)
            await session.flush()
        await session.commit()
        yield session


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


async def test_publication_closes_linked_suggestion_only_after_success(source_change_session):
    from server.routers.feishu_knowledge_router import FeishuReviewService
    from yuxi.governance.meeting_publication import complete_meeting_suggestions
    from yuxi.product_chat.meeting_repository import MeetingRepository
    from yuxi.storage.postgres.models_business import User, Department
    from yuxi.storage.postgres.models_product import (
        ProductConversation, ProductMessage, MeetingRecord, ManagedMeetingItem, ManagedMeetingEvent,
    )

    db = source_change_session
    department = Department(name="测试发布")
    db.add(department)
    await db.flush()
    user = User(
        username="publication-test", uid="publication-test", password_hash="unused", department_id=department.id
    )
    db.add(user)
    await db.flush()
    conversation = ProductConversation(owner_user_id=user.id, title="发布会议")
    db.add(conversation)
    await db.flush()
    question = ProductMessage(conversation_id=conversation.conversation_id, role="USER", content="会议")
    answer = ProductMessage(
        conversation_id=conversation.conversation_id, role="ASSISTANT", content="结果", answer_status="SUPPORTED"
    )
    db.add_all([question, answer])
    await db.flush()
    record = MeetingRecord(id="meeting-publish", conversation_id=conversation.conversation_id,
        message_id=answer.message_id, user_message_id=question.message_id, request_id="publish-test",
        state="completed", version=0, input={}, sources=[])
    db.add(record)
    await db.flush()
    await MeetingRepository(db).save_result(record, {"title":"发布会议", "body":"核对规则", "followup":{
        "tasks":[], "knowledgeSuggestions":[{"id":"suggestion-1", "title":"修改规则", "status":"REVIEW_REQUESTED",
            "governance":{"changeRequestId":"change-request-old", "formalPublication":False}}]}})
    request = await db.scalar(select(FeishuSourceChangeRequest))
    request.status = SourceChangeRequestStatus.FULFILLED
    request.received_version_id = "version-received"
    version = await db.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-received")
    )
    version.processing_status = "publish_failed"
    await db.commit()
    assert await complete_meeting_suggestions(db, version.version_id) == 0
    assert record.result["followup"]["knowledgeSuggestions"][0]["status"] == "REVIEW_REQUESTED"
    version.processing_status = "published"
    version.published_at = datetime.now(UTC)
    source_item = await db.scalar(select(FeishuSourceItem))
    source_item.publication_status = "ACTIVE"
    await db.flush()
    # A successful but superseded version cannot close a linked suggestion.
    assert await complete_meeting_suggestions(db, version.version_id) == 0
    source_item.active_version_id = version.version_id
    source_item.publication_status = "INACTIVE"
    await db.flush()
    assert await complete_meeting_suggestions(db, version.version_id) == 0
    source_item.publication_status = "ACTIVE"
    request.status = SourceChangeRequestStatus.NEW_VERSION_RECEIVED
    await db.flush()
    assert await complete_meeting_suggestions(db, version.version_id) == 0
    request.status = SourceChangeRequestStatus.FULFILLED
    source_item.active_version_id = "version-published"
    version.processing_status = "publishing"
    await db.commit()
    await FeishuReviewService(db).mark_publish_succeeded(version.version_id, yuxi_file_id="file-new")
    await db.commit()
    suggestion = record.result["followup"]["knowledgeSuggestions"][0]
    assert suggestion["status"] == "PUBLISHED"
    assert suggestion["governance"]["publishedVersionId"] == version.version_id
    assert suggestion["governance"]["formalPublication"] is True
    projection = await db.scalar(select(ManagedMeetingItem).where(ManagedMeetingItem.kind == "KNOWLEDGE"))
    assert projection.payload["status"] == "PUBLISHED"
    saved_version = record.version
    assert await complete_meeting_suggestions(db, version.version_id) == 0
    assert record.version == saved_version
    events = list(
        await db.scalars(select(ManagedMeetingEvent).where(ManagedMeetingEvent.action == "KNOWLEDGE_PUBLISHED"))
    )
    assert len(events) == 1


async def _published_meeting(db, *, suggestions, meeting_id="meeting-publish", parent_id=None, title="发布会议"):
    """Create one completed meeting snapshot whose knowledge suggestions drive the writeback."""
    from yuxi.product_chat.meeting_repository import MeetingRepository
    from yuxi.storage.postgres.models_business import Department, User
    from yuxi.storage.postgres.models_product import MeetingRecord, ProductConversation, ProductMessage

    department = await db.scalar(select(Department).limit(1))
    if department is None:
        department = Department(name="测试发布")
        db.add(department)
        await db.flush()
    user = await db.scalar(select(User).limit(1))
    if user is None:
        user = User(
            username="publication-test",
            uid="publication-test",
            password_hash="unused",
            department_id=department.id,
        )
        db.add(user)
        await db.flush()
    conversation = await db.scalar(select(ProductConversation).limit(1))
    if conversation is None:
        conversation = ProductConversation(owner_user_id=user.id, title=title)
        db.add(conversation)
        await db.flush()
    question = ProductMessage(conversation_id=conversation.conversation_id, role="USER", content="会议")
    answer = ProductMessage(
        conversation_id=conversation.conversation_id, role="ASSISTANT", content="结果", answer_status="SUPPORTED"
    )
    db.add_all([question, answer])
    await db.flush()
    record = MeetingRecord(
        id=meeting_id,
        conversation_id=conversation.conversation_id,
        message_id=answer.message_id,
        user_message_id=question.message_id,
        request_id=f"request-{meeting_id}",
        state="completed",
        version=0,
        input={"parentId": parent_id} if parent_id else {},
        sources=[],
    )
    db.add(record)
    await db.flush()
    await MeetingRepository(db).save_result(record, {"title": title, "body": "核对规则", "followup": {
        "tasks": [], "knowledgeSuggestions": suggestions}})
    return record


async def _make_version_published(db, version_id, *, publication_status="ACTIVE"):
    version = await db.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == version_id)
    )
    version.processing_status = "published"
    version.published_at = datetime.now(UTC)
    item = await db.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == version.item_id))
    item.active_version_id = version_id
    item.publication_status = publication_status
    await db.flush()
    return version, item


async def test_invalidated_source_never_closes_linked_suggestion(source_change_session):
    """A version published after the Feishu original disappeared must not claim closure."""
    from yuxi.governance.meeting_publication import complete_meeting_suggestions

    db = source_change_session
    record = await _published_meeting(db, suggestions=[{
        "id": "suggestion-1", "title": "修改规则", "status": "REVIEW_REQUESTED",
        "governance": {"changeRequestId": "change-request-old", "formalPublication": False}}])
    request = await db.scalar(select(FeishuSourceChangeRequest))
    request.status = SourceChangeRequestStatus.FULFILLED
    request.received_version_id = "version-received"
    version, item = await _make_version_published(db, "version-received")
    # 扫描把该页面标记失效后，版本仍可能已排入发布队列并成功写入索引。
    item.source_validity = "invalid"
    await db.commit()

    assert await complete_meeting_suggestions(db, version.version_id) == 0
    assert record.result["followup"]["knowledgeSuggestions"][0]["status"] == "REVIEW_REQUESTED"
    assert record.result["followup"]["knowledgeSuggestions"][0]["governance"]["formalPublication"] is False

    # 来源恢复有效后，同一版本的回写才允许生效。
    item.source_validity = "valid"
    await db.flush()
    assert await complete_meeting_suggestions(db, version.version_id) == 1
    assert record.result["followup"]["knowledgeSuggestions"][0]["status"] == "PUBLISHED"


async def test_one_request_closes_every_linked_suggestion_across_meetings(source_change_session):
    """A single fulfilled request may back several suggestions; each must close exactly once."""
    from yuxi.governance.meeting_publication import complete_meeting_suggestions
    from yuxi.storage.postgres.models_product import ManagedMeetingEvent

    db = source_change_session
    first = await _published_meeting(db, meeting_id="meeting-a", suggestions=[
        {"id": "suggestion-1", "title": "修改规则", "status": "REVIEW_REQUESTED",
         "governance": {"changeRequestId": "change-request-old", "formalPublication": False}},
        {"id": "suggestion-2", "title": "补充部署步骤", "status": "REVIEW_REQUESTED",
         "governance": {"changeRequestId": "change-request-old", "formalPublication": False}},
        # 同一会议中未关联该请求的建议必须保持不变。
        {"id": "suggestion-3", "title": "另一条建议", "status": "REVIEW_REQUESTED",
         "governance": {"changeRequestId": "change-request-other", "formalPublication": False}},
    ])
    second = await _published_meeting(db, meeting_id="meeting-b", suggestions=[
        {"id": "suggestion-1", "title": "修改规则", "status": "REVIEW_REQUESTED",
         "governance": {"changeRequestId": "change-request-old", "formalPublication": False}}])
    request = await db.scalar(select(FeishuSourceChangeRequest))
    request.status = SourceChangeRequestStatus.FULFILLED
    request.received_version_id = "version-received"
    version, _ = await _make_version_published(db, "version-received")
    await db.commit()

    assert await complete_meeting_suggestions(db, version.version_id) == 3
    first_items = {item["id"]: item for item in first.result["followup"]["knowledgeSuggestions"]}
    assert first_items["suggestion-1"]["status"] == "PUBLISHED"
    assert first_items["suggestion-2"]["status"] == "PUBLISHED"
    assert first_items["suggestion-3"]["status"] == "REVIEW_REQUESTED"
    assert second.result["followup"]["knowledgeSuggestions"][0]["status"] == "PUBLISHED"
    events = list(
        await db.scalars(select(ManagedMeetingEvent).where(ManagedMeetingEvent.action == "KNOWLEDGE_PUBLISHED"))
    )
    assert sorted(event.meeting_id for event in events) == ["meeting-a", "meeting-b"]
    assert sorted(events[0].detail["itemIds"]) == ["suggestion-1", "suggestion-2"]

    # 重复回调不得新增事件，也不得再次增加版本。
    saved_first, saved_second = first.version, second.version
    assert await complete_meeting_suggestions(db, version.version_id) == 0
    assert (first.version, second.version) == (saved_first, saved_second)


async def test_writeback_failure_rolls_back_publication_and_keeps_suggestion(source_change_session, monkeypatch):
    """如果回写本身失败，发布会整体回滚，建议不会被提前关闭。"""
    from server.routers import feishu_knowledge_router
    from yuxi.governance import meeting_publication
    from yuxi.storage.postgres.models_product import MeetingRecord

    db = source_change_session
    await _published_meeting(db, suggestions=[{
        "id": "suggestion-1", "title": "修改规则", "status": "REVIEW_REQUESTED",
        "governance": {"changeRequestId": "change-request-old", "formalPublication": False}}])
    request = await db.scalar(select(FeishuSourceChangeRequest))
    request.status = SourceChangeRequestStatus.FULFILLED
    request.received_version_id = "version-received"
    version = await db.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-received")
    )
    version.processing_status = "publishing"
    await db.commit()

    async def broken_writeback(*args, **kwargs):
        raise RuntimeError("writeback transaction failed")

    monkeypatch.setattr(meeting_publication, "complete_meeting_suggestions", broken_writeback)

    with pytest.raises(RuntimeError, match="writeback transaction failed"):
        await feishu_knowledge_router.FeishuReviewService(db).mark_publish_succeeded(
            version.version_id, yuxi_file_id="file-new"
        )
    await db.rollback()

    refreshed = await db.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-received")
    )
    item = await db.scalar(select(FeishuSourceItem))
    assert refreshed.processing_status == "publishing"
    assert refreshed.published_at is None
    assert item.active_version_id == "version-published"
    reloaded = await db.scalar(
        select(MeetingRecord).where(MeetingRecord.request_id == "request-meeting-publish")
    )
    assert reloaded.result["followup"]["knowledgeSuggestions"][0]["status"] == "REVIEW_REQUESTED"


async def test_restart_marks_publish_failed_and_retry_closes_only_after_success(source_change_session):
    """服务重启把发布中断标记为失败后，必须再成功发布一次才回写。"""
    from server.routers.feishu_knowledge_router import FeishuReviewService
    from yuxi.governance.meeting_publication import complete_meeting_suggestions
    from yuxi.repositories.feishu_knowledge_repository import FeishuKnowledgeRepository

    db = source_change_session
    record = await _published_meeting(db, suggestions=[{
        "id": "suggestion-1", "title": "修改规则", "status": "REVIEW_REQUESTED",
        "governance": {"changeRequestId": "change-request-old", "formalPublication": False}}])
    request = await db.scalar(select(FeishuSourceChangeRequest))
    request.status = SourceChangeRequestStatus.FULFILLED
    request.received_version_id = "version-received"
    version = await db.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-received")
    )
    version.processing_status = "publish_queued"
    await db.commit()

    reconciled = await FeishuKnowledgeRepository(db).reconcile_interrupted_work()
    await db.commit()
    assert reconciled["material_versions"] == 1
    interrupted = await db.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-received")
    )
    assert interrupted.processing_status == "publish_failed"
    assert await complete_meeting_suggestions(db, version.version_id) == 0
    assert record.result["followup"]["knowledgeSuggestions"][0]["status"] == "REVIEW_REQUESTED"

    retried = await FeishuReviewService(db).retry(version.version_id, operator_id="admin")
    assert retried.processing_status == "publish_queued"
    await db.commit()
    await FeishuReviewService(db).mark_publish_succeeded(version.version_id, yuxi_file_id="file-new")
    await db.commit()

    assert record.result["followup"]["knowledgeSuggestions"][0]["status"] == "PUBLISHED"


async def test_rerun_keeps_request_link_so_publication_still_closes_suggestion(source_change_session):
    """重新分析后同一条建议仍保留修改请求关联，发布会继续命中并回写。"""
    from yuxi.governance.meeting_publication import complete_meeting_suggestions

    db = source_change_session
    first = await _published_meeting(db, suggestions=[{
        "id": "knowledge-1", "title": "修改规则", "reason": "原规则与现状冲突", "sourceRefs": ["S1-P2"],
        "status": "REVIEW_REQUESTED",
        "governance": {"changeRequestId": "change-request-old", "formalPublication": False,
                       "reviewPackageId": "package-old"}}])
    # 同一会议重新分析：模型再次抽出同一条建议（标题与引用一致），合并必须保留人工处置与请求关联。
    rerun = await _published_meeting(db, meeting_id="meeting-rerun", parent_id=first.id, suggestions=[{
        "id": "knowledge-1", "title": "修改规则", "reason": "重新生成的理由", "sourceRefs": ["S1-P2"],
        "status": "PENDING_MAINTAINER", "comparisonStatus": "COVERED", "comparison": "已有知识覆盖"}])

    merged = rerun.result["followup"]["knowledgeSuggestions"]
    assert len(merged) == 1
    assert merged[0]["status"] == "REVIEW_REQUESTED"
    assert merged[0]["governance"]["changeRequestId"] == "change-request-old"
    # 可刷新的比对结论被更新，人工填写的理由与处置不被覆盖。
    assert merged[0]["comparisonStatus"] == "COVERED"
    assert merged[0]["reason"] == "原规则与现状冲突"

    request = await db.scalar(select(FeishuSourceChangeRequest))
    request.status = SourceChangeRequestStatus.FULFILLED
    request.received_version_id = "version-received"
    version, _ = await _make_version_published(db, "version-received")
    await db.commit()

    assert await complete_meeting_suggestions(db, version.version_id) == 1
    assert rerun.result["followup"]["knowledgeSuggestions"][0]["status"] == "PUBLISHED"


async def test_rerun_with_new_evidence_keeps_original_request_link(source_change_session):
    """重新分析换了引用时新增一条建议，但原建议的请求关联仍在，发布后仍会闭环。"""
    from yuxi.governance.meeting_publication import complete_meeting_suggestions

    db = source_change_session
    first = await _published_meeting(db, suggestions=[{
        "id": "knowledge-1", "title": "修改规则", "reason": "原规则与现状冲突", "sourceRefs": ["S1-P2"],
        "status": "REVIEW_REQUESTED",
        "governance": {"changeRequestId": "change-request-old", "formalPublication": False}}])
    rerun = await _published_meeting(db, meeting_id="meeting-rerun", parent_id=first.id, suggestions=[{
        "id": "knowledge-1", "title": "修改规则", "reason": "引用了另一段正文", "sourceRefs": ["S1-P9"],
        "status": "PENDING_MAINTAINER"}])

    merged = rerun.result["followup"]["knowledgeSuggestions"]
    assert len(merged) == 2
    by_status = {item["status"]: item for item in merged}
    assert by_status["REVIEW_REQUESTED"]["governance"]["changeRequestId"] == "change-request-old"
    assert by_status["PENDING_MAINTAINER"]["title"] == "修改规则"

    request = await db.scalar(select(FeishuSourceChangeRequest))
    request.status = SourceChangeRequestStatus.FULFILLED
    request.received_version_id = "version-received"
    version, _ = await _make_version_published(db, "version-received")
    await db.commit()

    # 只关闭带请求关联的那条，新证据产生的建议保持待处理。
    assert await complete_meeting_suggestions(db, version.version_id) == 1
    by_status = {item["status"]: item for item in rerun.result["followup"]["knowledgeSuggestions"]}
    assert by_status["PUBLISHED"]["governance"]["changeRequestId"] == "change-request-old"
    assert "governance" not in by_status["PENDING_MAINTAINER"]


async def test_new_source_suggestion_is_never_matched_by_title(source_change_session):
    """待补回原文的建议只记录人工草稿，发布同名资料也不得被当作闭环。"""
    from yuxi.governance.meeting_publication import complete_meeting_suggestions

    db = source_change_session
    record = await _published_meeting(db, suggestions=[{
        "id": "suggestion-new", "title": "部署版本矩阵", "status": "NEW_SOURCE_DRAFT",
        "draftContent": "待补回：部署版本矩阵",
        "governance": {"nextStep": "请维护人员补回飞书原文后启动扫描审核", "formalPublication": False}}])
    # 另有一条已履行的请求，确保回写函数不会在“无请求”处提前返回。
    other = FeishuSourceChangeRequest(
        change_request_id="change-request-other",
        review_item_id="review-item-old",
        source_item_id="item-1",
        status=SourceChangeRequestStatus.FULFILLED,
        request_text="无关请求",
        round_number=2,
        received_version_id="version-brand-new",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(other)
    brand_new = FeishuSourceItem(
        item_id="item-brand-new",
        source_id="source-1",
        item_key="page:item-brand-new",
        item_type="docx",
        title="部署版本矩阵",  # 与建议标题完全一致
        source_url="https://quickdone.feishu.cn/wiki/item-brand-new",
        source_validity="valid",
        active_version_id="version-brand-new",
    )
    brand_new_version = FeishuMaterialVersion(
        version_id="version-brand-new",
        item_id="item-brand-new",
        revision="1",
        content_hash="hash-brand-new",
        processing_status="published",
        published_at=datetime.now(UTC),
        review_status="approved",
    )
    db.add_all([brand_new, brand_new_version])
    await db.commit()

    assert await complete_meeting_suggestions(db, "version-brand-new") == 0
    suggestion = record.result["followup"]["knowledgeSuggestions"][0]
    assert suggestion["status"] == "NEW_SOURCE_DRAFT"
    assert suggestion["governance"]["formalPublication"] is False
    assert "publishedVersionId" not in suggestion["governance"]
