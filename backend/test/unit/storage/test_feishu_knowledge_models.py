from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuProcessingEvent,
    FeishuSource,
    FeishuSourceItem,
    FeishuSyncRun,
)


def test_feishu_models_expose_expected_tables_and_version_identity():
    assert {
        FeishuSource.__tablename__,
        FeishuSyncRun.__tablename__,
        FeishuSourceItem.__tablename__,
        FeishuMaterialVersion.__tablename__,
        FeishuProcessingEvent.__tablename__,
    } == {
        "feishu_sources",
        "feishu_sync_runs",
        "feishu_source_items",
        "feishu_material_versions",
        "feishu_processing_events",
    }
    columns = FeishuMaterialVersion.__table__.columns
    assert {"item_id", "revision", "content_hash", "review_status", "published_at"} <= set(columns.keys())
    constraints = FeishuMaterialVersion.__table__.constraints
    assert any(
        {"item_id", "revision", "content_hash"} <= {column.name for column in c.columns}
        for c in constraints
        if hasattr(c, "columns")
    )


def test_feishu_source_item_item_key_is_unique_and_has_active_version_pointer():
    constraints = FeishuSourceItem.__table__.constraints
    assert any(
        {"item_key"} == {column.name for column in c.columns}
        for c in constraints
        if hasattr(c, "columns")
    )
    assert "active_version_id" in FeishuSourceItem.__table__.columns


def test_feishu_source_item_indexes_have_unique_names():
    index_names = [index.name for index in FeishuSourceItem.__table__.indexes]

    assert len(index_names) == len(set(index_names))


def test_feishu_events_are_append_only_records_and_media_statuses_are_supported():
    event_columns = FeishuProcessingEvent.__table__.columns
    assert {"event_type", "from_status", "to_status", "payload_json", "created_at"} <= set(event_columns.keys())
    item = FeishuSourceItem(item_id="item-1", source_id="source-1", item_key="video:file-1", item_type="video")
    version = FeishuMaterialVersion(
        version_id="version-1", item_id="item-1", revision="1", content_hash="hash", processing_status="unsupported"
    )
    assert item.item_type == "video"
    assert version.processing_status == "unsupported"
