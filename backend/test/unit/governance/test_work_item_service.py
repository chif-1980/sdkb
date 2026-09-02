from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.governance.work_item_service import WorkItemService
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
)


def test_work_item_filters_support_owner_type_risk_status_and_overdue():
    now = datetime.now(UTC)
    item = WorkItemService._item(
        work_id="review:1",
        work_type="REVIEW",
        title="资料审核",
        source_id="source-1",
        source_title="资料",
        source_path="资料/正文",
        risk="HIGH",
        status="OPEN",
        assignee_id="admin-a",
        created_at=now - timedelta(days=2),
        updated_at=now,
        due_at=now - timedelta(minutes=1),
        ai_summary="需要复核",
        suggested_action="审核",
        block_reasons=[],
        navigation={"module": "review"},
    )

    assert WorkItemService._matches(
        item,
        operator_id="admin-a",
        assignee="mine",
        work_type="REVIEW",
        risk="HIGH",
        status="OPEN",
        overdue=True,
    )
    assert not WorkItemService._matches(
        item,
        operator_id="admin-b",
        assignee="mine",
        work_type=None,
        risk=None,
        status=None,
        overdue=None,
    )


@pytest.mark.asyncio
async def test_relation_filter_matches_target_source_and_displays_that_material():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session:
        session.add_all(
            [
                FeishuSource(
                    source_id="source-a",
                    name="资料 A",
                    wiki_root_token="root-a",
                    target_kb_id="kb-a",
                    credential_env_name="FEISHU_A",
                ),
                FeishuSource(
                    source_id="source-b",
                    name="资料 B",
                    wiki_root_token="root-b",
                    target_kb_id="kb-b",
                    credential_env_name="FEISHU_B",
                ),
                FeishuSourceItem(
                    item_id="item-a",
                    source_id="source-a",
                    item_key="item-a",
                    item_type="attachment",
                    title="来源一",
                    path_text="目录/来源一",
                ),
                FeishuSourceItem(
                    item_id="item-b",
                    source_id="source-b",
                    item_key="item-b",
                    item_type="attachment",
                    title="来源二",
                    path_text="目录/来源二",
                ),
                FeishuMaterialVersion(
                    version_id="version-a",
                    item_id="item-a",
                    revision="1",
                    content_hash="hash-a",
                ),
                FeishuMaterialVersion(
                    version_id="version-b",
                    item_id="item-b",
                    revision="1",
                    content_hash="hash-b",
                ),
                FeishuCrossDocumentRelation(
                    relation_id="relation-1",
                    comparison_key="version-a:version-b",
                    source_version_id="version-a",
                    target_version_id="version-b",
                    relation_type="CONFLICT",
                    status="open",
                    reasoning="两份资料的有效期不一致",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        await session.commit()

        items = await WorkItemService(session)._relations("source-b")

    await engine.dispose()
    assert len(items) == 1
    assert items[0]["source"]["sourceId"] == "source-b"
    assert items[0]["source"]["title"] == "来源二"
    assert "来源一" in items[0]["aiSummary"]
