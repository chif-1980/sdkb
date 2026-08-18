import pytest

from yuxi.storage.postgres.manager import PostgresManager


class _RecordingConnection:
    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement))


class _RecordingBegin:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return _RecordingBegin(self.connection)


@pytest.mark.asyncio
async def test_ensure_knowledge_schema_creates_feishu_tables_and_indexes_idempotently():
    manager = PostgresManager()
    original_initialized, original_engine = manager._initialized, manager.async_engine
    connection = _RecordingConnection()
    manager._initialized, manager.async_engine = True, _RecordingEngine(connection)
    try:
        await manager.ensure_knowledge_schema()
    finally:
        manager._initialized, manager.async_engine = original_initialized, original_engine

    statements = "\n".join(connection.statements)
    for table in (
        "feishu_sources",
        "feishu_user_oauth_credentials",
        "feishu_sync_runs",
        "feishu_source_items",
        "feishu_material_versions",
        "feishu_processing_events",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in statements
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_feishu_source_items_item_key" in statements
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_feishu_material_versions_identity" in statements
    assert "sync_run_id VARCHAR(64)" in statements
    assert "REFERENCES feishu_sync_runs(run_id) ON DELETE SET NULL" in statements
    assert "ADD COLUMN IF NOT EXISTS sync_run_id VARCHAR(64)" in statements
    assert "fk_feishu_material_versions_sync_run_id" in statements
    assert "CREATE INDEX IF NOT EXISTS ix_feishu_material_versions_sync_run_id" in statements
    feishu_statements = [statement for statement in connection.statements if "feishu_" in statement]
    assert all("REFERENCES knowledge_files" not in statement for statement in feishu_statements)
    assert all("DROP TABLE" not in statement.upper() for statement in feishu_statements)
