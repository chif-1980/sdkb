from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.feishu_knowledge_router import FeishuReviewService
from yuxi.governance.lifecycle_service import KnowledgeLifecycleService
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuSource,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
)

pytestmark = pytest.mark.asyncio


class RecordingRemovalAdapter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def remove(self, *, kb_id: str, file_id: str) -> None:
        self.calls.append((kb_id, file_id))
        if self.error:
            raise self.error


@pytest.fixture
async def lifecycle_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        published_at = datetime.now(UTC)
        session.add_all(
            [
                FeishuSource(
                    source_id="source-1",
                    name="善达知识库",
                    wiki_root_token="root",
                    target_kb_id="kb-1",
                    credential_env_name="FEISHU_USER_OAUTH",
                ),
                FeishuSourceItem(
                    item_id="item-1",
                    source_id="source-1",
                    item_key="page:item-1",
                    item_type="docx",
                    title="部署指南",
                    source_url="https://example.test/item-1",
                    source_validity="valid",
                    active_version_id="version-current",
                    publication_status="ACTIVE",
                ),
                FeishuMaterialVersion(
                    version_id="version-current",
                    item_id="item-1",
                    revision="2",
                    content_hash="current-hash",
                    source_object_path="source-1/item-1/version-current.docx",
                    processing_status="published",
                    review_status="approved",
                    yuxi_file_id="file-current",
                    chunk_count=6,
                    published_at=published_at,
                ),
                FeishuMaterialVersion(
                    version_id="version-history",
                    item_id="item-1",
                    revision="1",
                    content_hash="history-hash",
                    source_object_path="source-1/item-1/version-history.docx",
                    processing_status="replaced",
                    review_status="approved",
                    yuxi_file_id="file-history",
                    chunk_count=4,
                    published_at=published_at,
                ),
                FeishuKnowledgeUnit(
                    unit_id="unit-1",
                    unit_key="section:deployment",
                    lineage_key="section:deployment",
                    version_id="version-current",
                    item_id="item-1",
                    unit_index=0,
                    unit_type="SECTION",
                    title="部署要求",
                    content="生产环境需要八核处理器。",
                    content_hash="unit-hash",
                    source_segment_ids=["segment-1"],
                    recommended_outcome="PUBLISH",
                    recommendation_reason="内容完整。",
                    publication_state="INCLUDED",
                    lifecycle_status="ACTIVE",
                    status="ACTIVE",
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


async def _publish_candidate(session, version_id: str, file_id: str):
    service = FeishuReviewService(session)
    await service.claim_publish(version_id)
    await session.commit()
    await service.remember_publish_candidate(version_id, file_id=file_id)
    await session.commit()
    result = await service.mark_publish_succeeded(version_id, yuxi_file_id=file_id, chunk_count=5)
    await session.commit()
    return result


async def test_unit_can_be_offlined_and_restored_only_after_candidate_index_switch(lifecycle_session):
    lifecycle = KnowledgeLifecycleService(lifecycle_session)

    await lifecycle.queue_unit_transition(
        "unit-1",
        target="OFFLINE",
        reason="内容已过时",
        operator_id="admin-1",
    )
    await lifecycle_session.commit()
    offline_result = await _publish_candidate(lifecycle_session, "version-current", "file-offline")

    unit = await lifecycle_session.scalar(select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.unit_id == "unit-1"))
    assert unit.lifecycle_status == "OFFLINE"
    assert unit.publication_state == "INCLUDED"
    assert unit.lifecycle_note == "内容已过时"
    assert offline_result.replaced_file_id == "file-current"

    await lifecycle.queue_unit_transition(
        "unit-1",
        target="ACTIVE",
        reason="已复核恢复",
        operator_id="admin-2",
    )
    await lifecycle_session.commit()
    restore_result = await _publish_candidate(lifecycle_session, "version-current", "file-restored")

    assert unit.lifecycle_status == "ACTIVE"
    assert unit.publication_state == "INCLUDED"
    assert unit.lifecycle_note == "已复核恢复"
    assert restore_result.replaced_file_id == "file-offline"


async def test_unit_metadata_update_is_recorded_with_before_and_after_values(lifecycle_session):
    lifecycle = KnowledgeLifecycleService(lifecycle_session)
    valid_from = datetime(2026, 9, 1, tzinfo=UTC)
    valid_until = datetime(2026, 12, 31, tzinfo=UTC)
    review_due_at = datetime(2026, 10, 1, tzinfo=UTC)

    await lifecycle.update_unit_metadata(
        "unit-1",
        owner_id="owner-1",
        owner_name="知识负责人",
        valid_from=valid_from,
        valid_until=valid_until,
        review_due_at=review_due_at,
        operator_id="admin-1",
    )
    await lifecycle_session.commit()

    event = await lifecycle_session.scalar(
        select(FeishuProcessingEvent)
        .where(FeishuProcessingEvent.event_type == "knowledge_unit_metadata_updated")
        .order_by(FeishuProcessingEvent.id.desc())
    )
    assert event is not None
    assert event.source_id == "source-1"
    assert event.item_id == "item-1"
    assert event.version_id == "version-current"
    assert event.operator_id == "admin-1"
    assert event.message == "已更新知识单元治理信息"
    assert event.payload_json["unit_id"] == "unit-1"
    assert event.payload_json["changed_fields"] == [
        "owner_id",
        "owner_name",
        "valid_from",
        "valid_until",
        "review_due_at",
    ]
    assert event.payload_json["before"] == {
        "owner_id": None,
        "owner_name": None,
        "valid_from": None,
        "valid_until": None,
        "review_due_at": None,
    }
    assert event.payload_json["after"] == {
        "owner_id": "owner-1",
        "owner_name": "知识负责人",
        "valid_from": "2026-09-01T00:00:00",
        "valid_until": "2026-12-31T00:00:00",
        "review_due_at": "2026-10-01T00:00:00",
    }


async def test_failed_unit_rebuild_keeps_old_index_and_original_lifecycle_state(lifecycle_session):
    lifecycle = KnowledgeLifecycleService(lifecycle_session)
    review = FeishuReviewService(lifecycle_session)

    await lifecycle.queue_unit_transition(
        "unit-1",
        target="OFFLINE",
        reason="准备下架",
        operator_id="admin-1",
    )
    await lifecycle_session.commit()
    await review.claim_publish("version-current")
    await lifecycle_session.commit()
    await review.remember_publish_candidate("version-current", file_id="file-candidate")
    await lifecycle_session.commit()
    await review.mark_publish_failed(
        "version-current",
        message="候选索引构建失败",
        yuxi_file_id="file-candidate",
    )
    await lifecycle_session.commit()

    unit = await lifecycle_session.scalar(select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.unit_id == "unit-1"))
    version = await lifecycle_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-current")
    )
    assert unit.lifecycle_status == "ACTIVE"
    assert unit.publication_state == "INCLUDED"
    assert version.processing_status == "published"
    assert version.yuxi_file_id == "file-current"
    assert "lifecycle_requests" not in (version.processing_params or {})
    assert (version.processing_params or {})["failed_publish_candidate_file_id"] == "file-candidate"


async def test_source_can_be_offlined_and_restored_with_real_index_state(lifecycle_session):
    removal = RecordingRemovalAdapter()
    review = FeishuReviewService(lifecycle_session, removal_adapter=removal)

    item = await review.offline_source("item-1", operator_id="admin-1", reason="整篇内容过时")
    version = await lifecycle_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-current")
    )
    assert removal.calls == [("kb-1", "file-current")]
    assert item.publication_status == "OFFLINE"
    assert version.processing_status == "published"
    assert version.yuxi_file_id is None
    assert version.chunk_count == 0

    queued = await review.queue_source_restore("item-1", operator_id="admin-2", reason="资料重新生效")
    await lifecycle_session.commit()
    assert queued.processing_status == "publish_queued"
    assert item.publication_status == "RESTORE_PENDING"

    await _publish_candidate(lifecycle_session, "version-current", "file-restored")
    assert item.publication_status == "ACTIVE"
    assert version.processing_status == "published"
    assert version.yuxi_file_id == "file-restored"


async def test_source_offline_and_restore_failures_do_not_leave_false_state(lifecycle_session):
    review = FeishuReviewService(
        lifecycle_session,
        removal_adapter=RecordingRemovalAdapter(RuntimeError("向量库删除失败")),
    )

    with pytest.raises(RuntimeError, match="向量库删除失败"):
        await review.offline_source("item-1", operator_id="admin-1", reason="整篇下架")

    item = await lifecycle_session.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == "item-1"))
    version = await lifecycle_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-current")
    )
    assert item.publication_status == "ACTIVE"
    assert version.yuxi_file_id == "file-current"

    item.publication_status = "OFFLINE"
    version.yuxi_file_id = None
    version.chunk_count = 0
    await lifecycle_session.commit()
    await review.queue_source_restore("item-1", operator_id="admin-2", reason="尝试恢复")
    await lifecycle_session.commit()
    await review.claim_publish("version-current")
    await lifecycle_session.commit()
    await review.mark_publish_failed("version-current", message="恢复索引失败")
    await lifecycle_session.commit()

    assert item.publication_status == "OFFLINE"
    assert version.processing_status == "published"
    assert version.yuxi_file_id is None


async def test_history_rollback_switches_only_after_success_and_restores_target_on_failure(lifecycle_session):
    review = FeishuReviewService(lifecycle_session)

    queued = await review.queue_source_rollback(
        "item-1",
        "version-history",
        operator_id="admin-1",
        reason="回滚到已验证版本",
    )
    await lifecycle_session.commit()
    assert queued.processing_status == "publish_queued"
    assert queued.yuxi_file_id is None

    await review.claim_publish("version-history")
    await lifecycle_session.commit()
    await review.remember_publish_candidate("version-history", file_id="file-rollback-candidate")
    await lifecycle_session.commit()
    await review.mark_publish_failed(
        "version-history",
        message="回滚索引失败",
        yuxi_file_id="file-rollback-candidate",
    )
    await lifecycle_session.commit()

    item = await lifecycle_session.scalar(select(FeishuSourceItem).where(FeishuSourceItem.item_id == "item-1"))
    history = await lifecycle_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-history")
    )
    assert item.active_version_id == "version-current"
    assert history.processing_status == "replaced"
    assert history.yuxi_file_id == "file-history"

    await review.queue_source_rollback(
        "item-1",
        "version-history",
        operator_id="admin-1",
        reason="再次回滚",
    )
    await lifecycle_session.commit()
    result = await _publish_candidate(lifecycle_session, "version-history", "file-rollback")

    current = await lifecycle_session.scalar(
        select(FeishuMaterialVersion).where(FeishuMaterialVersion.version_id == "version-current")
    )
    assert item.active_version_id == "version-history"
    assert history.processing_status == "published"
    assert history.yuxi_file_id == "file-rollback"
    assert current.processing_status == "replaced"
    assert result.replaced_file_id == "file-current"


async def test_revision_request_keeps_feishu_as_source_of_truth_and_is_idempotent(lifecycle_session):
    lifecycle = KnowledgeLifecycleService(lifecycle_session)

    first = await lifecycle.create_revision_request(
        "unit-1",
        trigger_type="LIFECYCLE",
        reason="正文参数有误，请修改飞书原文",
        operator_id="admin-1",
    )
    second = await lifecycle.create_revision_request(
        "unit-1",
        trigger_type="LIFECYCLE",
        reason="重复提交不应新建任务",
        operator_id="admin-1",
    )
    await lifecycle_session.commit()

    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert first["change_request_id"] == second["change_request_id"]
    unit = await lifecycle_session.scalar(select(FeishuKnowledgeUnit).where(FeishuKnowledgeUnit.unit_id == "unit-1"))
    change_request = await lifecycle_session.scalar(
        select(FeishuSourceChangeRequest).where(
            FeishuSourceChangeRequest.change_request_id == first["change_request_id"]
        )
    )
    assert unit.content == "生产环境需要八核处理器。"
    assert change_request.source_url == "https://example.test/item-1"
    assert change_request.request_text == "正文参数有误，请修改飞书原文"
