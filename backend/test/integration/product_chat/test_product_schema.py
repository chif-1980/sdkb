import asyncio
import inspect
import json
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


async def _create_legacy_product_tables(connection, locator_type: str):
    for legacy_table_ddl in (
        """
        CREATE TABLE product_conversations (
            owner_user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """,
        """
        CREATE TABLE product_messages (
            conversation_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        )
        """,
        f"""
        CREATE TABLE message_citations (
            message_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            locator {locator_type} NOT NULL
        )
        """,
    ):
        await connection.execute(text(legacy_table_ddl))


async def _inspect_legacy_product_migration(connection, schema_name: str):
    column = (
        await connection.execute(
            text(
                """
                SELECT data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = :schema_name
                  AND table_name = 'message_citations'
                  AND column_name = 'locator'
                """
            ),
            {"schema_name": schema_name},
        )
    ).one()
    index_names = set(
        (
            await connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = :schema_name"),
                {"schema_name": schema_name},
            )
        )
        .scalars()
        .all()
    )
    return column, index_names


async def _wait_for_table_lock_waiters(connection, schema_name: str, expected_count: int):
    while True:
        waiting_count = (
            await connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_locks AS waiting
                    JOIN pg_class AS relation ON relation.oid = waiting.relation
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = :schema_name
                      AND relation.relname = 'message_citations'
                      AND waiting.mode = 'AccessExclusiveLock'
                      AND NOT waiting.granted
                    """
                ),
                {"schema_name": schema_name},
            )
        ).scalar_one()
        if waiting_count >= expected_count:
            return
        await asyncio.sleep(0.01)


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
        "feishu_department_bindings",
        "feishu_user_department_memberships",
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
    statement_counts = Counter(connection.statements)
    for statement in expected_indexes:
        assert statement_counts[statement] == 2

    migration_statements = [
        statement for statement in connection.statements if "ALTER COLUMN locator TYPE TEXT" in statement
    ]
    assert len(migration_statements) == 2
    assert len(connection.statements) == 2 * (len(expected_indexes) + 1)
    assert connection.statements[0] == connection.statements[5] == migration_statements[0]
    assert all(statement == migration_statements[0] for statement in migration_statements)
    locator_type_check = "data_type IN ('json', 'jsonb')"
    exclusive_lock = "LOCK TABLE message_citations IN ACCESS EXCLUSIVE MODE"
    first_type_check = migration_statements[0].find(locator_type_check)
    exclusive_lock_position = migration_statements[0].find(exclusive_lock)
    second_type_check = migration_statements[0].find(locator_type_check, first_type_check + 1)
    assert migration_statements[0].count(locator_type_check) == 2
    assert first_type_check < exclusive_lock_position < second_type_check
    assert "locator::jsonb #>> '{}'" in migration_statements[0]
    assert "COALESCE(locator::jsonb #>> '{}', 'null')" in migration_statements[0]

    assert set(BusinessBase.metadata.tables["product_messages"].columns.keys()) == {
        "id",
        "message_id",
        "conversation_id",
        "role",
        "content",
        "answer_status",
        "model_version",
        "prompt_version",
        "feedback_rating",
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
            "feishu_department_bindings": {
                "id",
                "tenant_key",
                "feishu_department_id",
                "department_id",
                "display_name",
                "created_at",
                "updated_at",
            },
            "feishu_user_department_memberships": {
                "id",
                "user_id",
                "department_binding_id",
                "position",
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
                "feedback_rating",
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
        assert schema["indexes"]["product_conversations"] >= {"ix_product_conversations_owner_status_updated"}
        assert schema["indexes"]["product_messages"] >= {"ix_product_messages_conversation_created"}
        assert schema["indexes"]["message_citations"] >= {
            "ix_message_citations_message_id",
            "ix_message_citations_version_id",
        }
        assert schema["unique_columns"] == {
            "feishu_user_bindings": {("feishu_open_id",), ("feishu_user_id",), ("user_id",)},
            "feishu_department_bindings": {
                ("department_id",),
                ("tenant_key", "feishu_department_id"),
            },
            "feishu_user_department_memberships": {
                ("user_id", "department_binding_id"),
            },
            "product_conversations": {("conversation_id",)},
            "product_messages": {("message_id",)},
            "message_citations": {("citation_id",)},
        }
        assert schema["foreign_keys"] == {
            "feishu_user_bindings": {("user_id",): ("users", ("id",))},
            "feishu_department_bindings": {("department_id",): ("departments", ("id",))},
            "feishu_user_department_memberships": {
                ("user_id",): ("users", ("id",)),
                ("department_binding_id",): ("feishu_department_bindings", ("id",)),
            },
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
            "FEEDBACK_RATING",
            "LIKE",
            "DISLIKE",
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


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("legacy_locator_type", ["JSON", "JSONB"])
async def test_product_schema_migrates_legacy_locator_to_text(legacy_locator_type):
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        pytest.skip("POSTGRES_URL is not configured for the PostgreSQL product schema integration test.")

    schema_name = f"product_locator_migration_{uuid4().hex}"
    engine = create_async_engine(postgres_url)
    manager = PostgresManager()
    original_initialized, original_engine = manager._initialized, manager.async_engine
    schema_created = False

    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
            await connection.execute(text(f'SET LOCAL search_path TO "{schema_name}"'))
            await _create_legacy_product_tables(connection, legacy_locator_type)
            await connection.execute(
                text(
                    f"""
                    INSERT INTO message_citations (message_id, version_id, locator)
                    VALUES
                        ('object', 'version-1', CAST(:object_locator AS {legacy_locator_type})),
                        ('array', 'version-2', CAST(:array_locator AS {legacy_locator_type})),
                        ('string', 'version-3', CAST(:string_locator AS {legacy_locator_type})),
                        ('null', 'version-4', CAST(:null_locator AS {legacy_locator_type}))
                    """
                ),
                {
                    "object_locator": '{"page":3,"sections":[1,2]}',
                    "array_locator": '[1,2,{"label":"A B"}]',
                    "string_locator": '"section-3"',
                    "null_locator": "null",
                },
            )

        manager._initialized = True
        manager.async_engine = _SearchPathEngine(engine, schema_name)
        for _ in range(2):
            await manager.ensure_product_schema()

        async with engine.connect() as connection:
            column, index_names = await _inspect_legacy_product_migration(connection, schema_name)
            locator_rows = (
                await connection.execute(text(f'SELECT message_id, locator FROM "{schema_name}".message_citations'))
            ).all()

        locator_by_message = {row.message_id: row.locator for row in locator_rows}
        assert column.data_type == "text"
        assert column.is_nullable == "NO"
        assert json.loads(locator_by_message["object"]) == {"page": 3, "sections": [1, 2]}
        assert json.loads(locator_by_message["array"]) == [1, 2, {"label": "A B"}]
        assert locator_by_message["string"] == "section-3"
        assert locator_by_message["null"] == "null"
        assert index_names >= {
            "ix_product_conversations_owner_status_updated",
            "ix_product_messages_conversation_created",
            "ix_message_citations_message_id",
            "ix_message_citations_version_id",
        }
    finally:
        manager._initialized, manager.async_engine = original_initialized, original_engine
        try:
            if schema_created:
                async with engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        finally:
            await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_product_schema_does_not_exclusively_lock_an_existing_text_locator():
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        pytest.skip("POSTGRES_URL is not configured for the PostgreSQL product schema integration test.")

    schema_name = f"product_text_locator_{uuid4().hex}"
    engine = create_async_engine(postgres_url)
    manager = object.__new__(PostgresManager)
    blocker = None
    blocker_transaction = None
    ensure_task = None
    lock_observer_task = None
    schema_created = False

    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
            await connection.execute(text(f'SET LOCAL search_path TO "{schema_name}"'))
            await _create_legacy_product_tables(connection, "TEXT")
            await connection.execute(
                text(
                    """
                    INSERT INTO message_citations (message_id, version_id, locator)
                    VALUES ('string', 'version-1', 'section-3')
                    """
                )
            )

        manager._initialized = True
        manager.async_engine = _SearchPathEngine(engine, schema_name)

        blocker = await engine.connect()
        blocker_transaction = await blocker.begin()
        await blocker.execute(text(f'SET LOCAL search_path TO "{schema_name}"'))
        await blocker.execute(text("LOCK TABLE message_citations IN ACCESS SHARE MODE"))

        ensure_task = asyncio.create_task(manager.ensure_product_schema())
        async with engine.connect() as monitor:
            lock_observer_task = asyncio.create_task(
                _wait_for_table_lock_waiters(monitor, schema_name, expected_count=1)
            )
            completed, _ = await asyncio.wait(
                {ensure_task, lock_observer_task},
                timeout=5,
                return_when=asyncio.FIRST_COMPLETED,
            )
            assert ensure_task in completed
            assert lock_observer_task not in completed
            lock_observer_task.cancel()
            await asyncio.gather(lock_observer_task, return_exceptions=True)

        await ensure_task

        async with engine.connect() as connection:
            column, index_names = await _inspect_legacy_product_migration(connection, schema_name)
            locator = (
                await connection.execute(
                    text(f"SELECT locator FROM \"{schema_name}\".message_citations WHERE message_id = 'string'")
                )
            ).scalar_one()

        assert column.data_type == "text"
        assert column.is_nullable == "NO"
        assert locator == "section-3"
        assert index_names >= {
            "ix_product_conversations_owner_status_updated",
            "ix_product_messages_conversation_created",
            "ix_message_citations_message_id",
            "ix_message_citations_version_id",
        }
    finally:
        for task in (ensure_task, lock_observer_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (ensure_task, lock_observer_task) if task is not None),
            return_exceptions=True,
        )
        if blocker_transaction is not None and blocker_transaction.is_active:
            await blocker_transaction.rollback()
        if blocker is not None:
            await blocker.close()
        try:
            if schema_created:
                async with engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        finally:
            await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_product_schema_concurrent_legacy_migrations_are_serialized():
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        pytest.skip("POSTGRES_URL is not configured for the PostgreSQL product schema integration test.")

    schema_name = f"product_concurrent_{uuid4().hex}"
    engine = create_async_engine(postgres_url)
    manager_one = object.__new__(PostgresManager)
    manager_two = object.__new__(PostgresManager)
    blocker = None
    blocker_transaction = None
    migration_tasks = []
    schema_created = False

    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
            await connection.execute(text(f'SET LOCAL search_path TO "{schema_name}"'))
            await _create_legacy_product_tables(connection, "JSONB")
            await connection.execute(
                text(
                    """
                    INSERT INTO message_citations (message_id, version_id, locator)
                    VALUES ('string', 'version-1', '"section-3"')
                    """
                )
            )

        for manager in (manager_one, manager_two):
            manager._initialized = True
            manager.async_engine = _SearchPathEngine(engine, schema_name)

        blocker = await engine.connect()
        blocker_transaction = await blocker.begin()
        await blocker.execute(text(f'SET LOCAL search_path TO "{schema_name}"'))
        await blocker.execute(text("LOCK TABLE message_citations IN ACCESS EXCLUSIVE MODE"))

        migration_tasks = [
            asyncio.create_task(manager_one.ensure_product_schema()),
            asyncio.create_task(manager_two.ensure_product_schema()),
        ]
        async with engine.connect() as monitor:
            await asyncio.wait_for(
                _wait_for_table_lock_waiters(monitor, schema_name, expected_count=2),
                timeout=5,
            )

        await blocker_transaction.commit()
        await asyncio.wait_for(asyncio.gather(*migration_tasks), timeout=5)

        async with engine.connect() as connection:
            column, index_names = await _inspect_legacy_product_migration(connection, schema_name)
            locator = (
                await connection.execute(
                    text(f"SELECT locator FROM \"{schema_name}\".message_citations WHERE message_id = 'string'")
                )
            ).scalar_one()

        assert column.data_type == "text"
        assert column.is_nullable == "NO"
        assert locator == "section-3"
        assert index_names >= {
            "ix_product_conversations_owner_status_updated",
            "ix_product_messages_conversation_created",
            "ix_message_citations_message_id",
            "ix_message_citations_version_id",
        }
    finally:
        for task in migration_tasks:
            if not task.done():
                task.cancel()
        if migration_tasks:
            await asyncio.gather(*migration_tasks, return_exceptions=True)
        if blocker_transaction is not None and blocker_transaction.is_active:
            await blocker_transaction.rollback()
        if blocker is not None:
            await blocker.close()
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
        "feishu_department_bindings",
        "feishu_user_department_memberships",
        "product_conversations",
        "product_messages",
        "message_citations",
    }
    columns = {table_name: inspector.get_columns(table_name, schema=schema_name) for table_name in table_names}
    return {
        "table_names": set(inspector.get_table_names(schema=schema_name)),
        "columns": {
            table_name: {column["name"] for column in table_columns} for table_name, table_columns in columns.items()
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
                constraint["sqltext"] for constraint in inspector.get_check_constraints(table_name, schema=schema_name)
            ).upper()
            for table_name in table_names
        },
    }
