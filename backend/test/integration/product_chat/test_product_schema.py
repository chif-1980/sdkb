import inspect
from collections import Counter

import pytest

from server.utils.lifespan import lifespan
from yuxi.storage.postgres.manager import PostgresManager
from yuxi.storage.postgres.models_business import Base as BusinessBase


class _RecordingConnection:
    def __init__(self):
        self.run_sync_calls = []
        self.statements: list[str] = []

    async def run_sync(self, operation):
        self.run_sync_calls.append(operation)

    async def execute(self, statement):
        self.statements.append(str(statement))


class _RecordingBegin:
    def __init__(self, connection: _RecordingConnection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingEngine:
    def __init__(self, connection: _RecordingConnection):
        self.connection = connection

    def begin(self):
        return _RecordingBegin(self.connection)


@pytest.mark.asyncio
async def test_product_schema_creation_and_index_ensure_are_idempotent():
    manager = PostgresManager()
    original_initialized, original_engine = manager._initialized, manager.async_engine
    connection = _RecordingConnection()
    manager._initialized, manager.async_engine = True, _RecordingEngine(connection)

    try:
        for _ in range(2):
            await manager.create_tables()
            await manager.ensure_product_schema()
    finally:
        manager._initialized, manager.async_engine = original_initialized, original_engine

    assert len(connection.run_sync_calls) == 4
    assert all(operation.__self__ is BusinessBase.metadata for operation in connection.run_sync_calls)
    assert {
        "feishu_user_bindings",
        "product_conversations",
        "product_messages",
        "message_citations",
    } <= set(BusinessBase.metadata.tables)

    expected_indexes = {
        (
            "CREATE INDEX IF NOT EXISTS ix_product_conversations_owner_status_updated "
            "ON product_conversations (owner_user_id, status, updated_at)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS ix_product_messages_conversation_created "
            "ON product_messages (conversation_id, created_at)"
        ),
        "CREATE INDEX IF NOT EXISTS ix_message_citations_message_id ON message_citations (message_id)",
        "CREATE INDEX IF NOT EXISTS ix_message_citations_version_id ON message_citations (version_id)",
    }
    assert Counter(connection.statements) == Counter({statement: 2 for statement in expected_indexes})

    assert set(BusinessBase.metadata.tables["product_messages"].columns.keys()) == {
        "id",
        "message_id",
        "conversation_id",
        "role",
        "content",
        "answer_status",
        "model_version",
        "prompt_version",
        "created_at",
    }
    assert {index.name for index in BusinessBase.metadata.tables["message_citations"].indexes} == {
        "ix_message_citations_message_id",
        "ix_message_citations_version_id",
    }


def test_lifespan_ensures_product_schema_immediately_after_table_creation():
    source = inspect.getsource(lifespan)

    assert "await pg_manager.create_tables()\n        await pg_manager.ensure_product_schema()" in source
