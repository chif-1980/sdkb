import inspect
import os
from collections import Counter
from uuid import uuid4

import pytest
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

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


class _SearchPathBegin:
    def __init__(self, engine, schema_name: str):
        self.context = engine.begin()
        self.schema_name = schema_name

    async def __aenter__(self):
        connection = await self.context.__aenter__()
        await connection.execute(text(f'SET LOCAL search_path TO "{self.schema_name}"'))
        return connection

    async def __aexit__(self, exc_type, exc, tb):
        return await self.context.__aexit__(exc_type, exc, tb)


class _SearchPathEngine:
    def __init__(self, engine, schema_name: str):
        self.engine = engine
        self.schema_name = schema_name

    def begin(self):
        return _SearchPathBegin(self.engine, self.schema_name)


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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_product_schema_is_idempotent_in_real_postgres():
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        pytest.skip("POSTGRES_URL is not configured for the PostgreSQL product schema integration test.")

    schema_name = f"product_schema_{uuid4().hex}"
    engine = create_async_engine(postgres_url)
    manager = PostgresManager()
    original_initialized, original_engine = manager._initialized, manager.async_engine
    schema_created = False

    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True

        manager._initialized = True
        manager.async_engine = _SearchPathEngine(engine, schema_name)
        for _ in range(2):
            await manager.create_tables()
            await manager.ensure_product_schema()

        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: _inspect_product_schema(sync_connection, schema_name)
            )

        expected_columns = {
            "feishu_user_bindings": {
                "id",
                "user_id",
                "feishu_open_id",
                "feishu_user_id",
                "feishu_union_id",
                "tenant_key",
                "display_name",
                "avatar_url",
                "authorization_status",
                "last_login_at",
                "created_at",
                "updated_at",
            },
            "product_conversations": {
                "id",
                "conversation_id",
                "owner_user_id",
                "title",
                "status",
                "created_at",
                "updated_at",
            },
            "product_messages": {
                "id",
                "message_id",
                "conversation_id",
                "role",
                "content",
                "answer_status",
                "model_version",
                "prompt_version",
                "created_at",
            },
            "message_citations": {
                "id",
                "citation_id",
                "message_id",
                "kind",
                "source_id",
                "item_id",
                "version_id",
                "yuxi_file_id",
                "title",
                "source_url",
                "path_text",
                "locator",
                "excerpt",
                "source_version_at",
                "created_at",
            },
        }
        assert schema["table_names"] >= set(expected_columns)
        assert schema["columns"] == expected_columns
        assert schema["locator_type"] == "TEXT"
        assert schema["indexes"]["product_conversations"] >= {
            "ix_product_conversations_owner_status_updated"
        }
        assert schema["indexes"]["product_messages"] >= {"ix_product_messages_conversation_created"}
        assert schema["indexes"]["message_citations"] >= {
            "ix_message_citations_message_id",
            "ix_message_citations_version_id",
        }
        assert schema["unique_columns"] == {
            "feishu_user_bindings": {("feishu_open_id",), ("feishu_user_id",), ("user_id",)},
            "product_conversations": {("conversation_id",)},
            "product_messages": {("message_id",)},
            "message_citations": {("citation_id",)},
        }
        assert schema["foreign_keys"] == {
            "feishu_user_bindings": {("user_id",): ("users", ("id",))},
            "product_conversations": {("owner_user_id",): ("users", ("id",))},
            "product_messages": {("conversation_id",): ("product_conversations", ("conversation_id",))},
            "message_citations": {("message_id",): ("product_messages", ("message_id",))},
        }
        assert "ACTIVE" in schema["checks"]["feishu_user_bindings"]
        assert "REVOKED" in schema["checks"]["feishu_user_bindings"]
        assert "ARCHIVED" in schema["checks"]["product_conversations"]
        for expected in (
            "USER",
            "ASSISTANT",
            "SUPPORTED",
            "INSUFFICIENT",
            "CONFLICTING",
            "MODEL_VERSION IS NULL",
            "PROMPT_VERSION IS NULL",
            "ANSWER_STATUS IS NOT NULL",
        ):
            assert expected in schema["checks"]["product_messages"]
        assert "ENTERPRISE_EVIDENCE" in schema["checks"]["message_citations"]
    finally:
        manager._initialized, manager.async_engine = original_initialized, original_engine
        try:
            if schema_created:
                async with engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        finally:
            await engine.dispose()


def _inspect_product_schema(connection, schema_name: str) -> dict:
    inspector = sqlalchemy_inspect(connection)
    table_names = {
        "feishu_user_bindings",
        "product_conversations",
        "product_messages",
        "message_citations",
    }
    columns = {
        table_name: inspector.get_columns(table_name, schema=schema_name)
        for table_name in table_names
    }
    return {
        "table_names": set(inspector.get_table_names(schema=schema_name)),
        "columns": {
            table_name: {column["name"] for column in table_columns}
            for table_name, table_columns in columns.items()
        },
        "locator_type": str(
            next(column["type"] for column in columns["message_citations"] if column["name"] == "locator")
        ),
        "indexes": {
            table_name: {index["name"] for index in inspector.get_indexes(table_name, schema=schema_name)}
            for table_name in table_names
        },
        "unique_columns": {
            table_name: {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table_name, schema=schema_name)
            }
            for table_name in table_names
        },
        "foreign_keys": {
            table_name: {
                tuple(foreign_key["constrained_columns"]): (
                    foreign_key["referred_table"],
                    tuple(foreign_key["referred_columns"]),
                )
                for foreign_key in inspector.get_foreign_keys(table_name, schema=schema_name)
            }
            for table_name in table_names
        },
        "checks": {
            table_name: "\n".join(
                constraint["sqltext"]
                for constraint in inspector.get_check_constraints(table_name, schema=schema_name)
            ).upper()
            for table_name in table_names
        },
    }
