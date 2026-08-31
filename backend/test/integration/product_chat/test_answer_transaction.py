import asyncio
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from yuxi.product_chat.repository import ProductChatNotFoundError, ProductChatRepository
from yuxi.product_chat.source_policy_service import ProductKnowledgeScope
from yuxi.storage.postgres.models_business import Base, Department, User
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
)
from yuxi.storage.postgres.models_product import (
    ConversationStatus,
    MessageCitation,
    MessageRole,
    ProductConversation,
    ProductMessage,
)


pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture()
async def postgres_answer_context():
    postgres_url = os.getenv("TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("TEST_POSTGRES_URL is not configured for PostgreSQL answer transaction tests.")
    try:
        parsed_url = make_url(postgres_url)
    except ArgumentError:
        pytest.fail("TEST_POSTGRES_URL must be a valid postgresql+asyncpg URL.")
    if parsed_url.drivername != "postgresql+asyncpg":
        pytest.fail("TEST_POSTGRES_URL must use the postgresql+asyncpg scheme.")

    schema_name = f"product_answer_{uuid4().hex}"
    engine = create_async_engine(
        postgres_url,
        isolation_level="READ COMMITTED",
        pool_size=5,
        max_overflow=0,
    )
    schema_engine = engine.execution_options(schema_translate_map={None: schema_name})
    factory = async_sessionmaker(schema_engine, expire_on_commit=False)
    schema_created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
        async with schema_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        Department.__table__,
                        User.__table__,
                        ProductConversation.__table__,
                        ProductMessage.__table__,
                        MessageCitation.__table__,
                    ],
                )
            )
        yield schema_engine, factory
    finally:
        try:
            if schema_created:
                async with engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        finally:
            await engine.dispose()


async def _seed_postgres_conversation(factory):
    async with factory() as session:
        user = User(
            username="Answer Transaction User",
            uid="answer-transaction-user",
            password_hash="not-used-by-product-chat",
            role="user",
        )
        session.add(user)
        await session.flush()
        conversation = ProductConversation(owner_user_id=user.id, title="")
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        return user.id, conversation


async def _assert_postgres_backend_is_blocked(factory, *, blocked_pid: int, blocker_pid: int) -> None:
    statement = text("SELECT CAST(:blocker_pid AS integer) = ANY(pg_blocking_pids(CAST(:blocked_pid AS integer)))")
    try:
        async with asyncio.timeout(5), factory() as observer:
            while not await observer.scalar(
                statement,
                {"blocked_pid": blocked_pid, "blocker_pid": blocker_pid},
            ):
                pass
    except TimeoutError:
        pytest.fail("PostgreSQL did not report the expected blocked backend before timeout.")


def _citation(**overrides):
    values = {
        "evidence_id": "E1",
        "source_id": "source-1",
        "item_id": "item-1",
        "version_id": "version-1",
        "yuxi_file_id": "file-1",
        "chunk_id": "chunk-1",
        "title": "产品手册",
        "source_url": "https://quickdone.feishu.cn/wiki/item-1",
        "path_text": "产品 / 手册",
        "locator": "第1段",
        "excerpt": "支持私有部署。",
        "source_version_at": datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _answer(*, citations=(_citation(),)):
    return SimpleNamespace(
        status="SUPPORTED",
        content="该产品支持私有部署。",
        citations=tuple(citations),
        model_version="model-1",
        prompt_version="enterprise-grounded-v1",
    )


def _postgres_answer():
    return _answer()


def _add_material(
    db_session,
    *,
    item_id,
    file_id,
    source_id="source-1",
    validity="valid",
    processing_status="published",
    review_status="approved",
    published_at=datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
    active=True,
):
    version_id = f"version-{item_id}"
    item = FeishuSourceItem(
        item_id=item_id,
        source_id=source_id,
        item_key=f"key-{item_id}",
        item_type="page",
        title=f"Title {item_id}",
        path_text="产品 / 手册",
        source_url=f"https://quickdone.feishu.cn/wiki/{item_id}",
        source_validity=validity,
        active_version_id=version_id if active else f"other-{version_id}",
    )
    version = FeishuMaterialVersion(
        version_id=version_id,
        item_id=item_id,
        revision="1",
        content_hash=f"hash-{item_id}",
        processing_status=processing_status,
        review_status=review_status,
        published_at=published_at,
        yuxi_file_id=file_id,
    )
    db_session.add_all([item, version])
    return item, version


async def test_repository_lists_owned_conversations_in_recent_order(db_session):
    repository = ProductChatRepository(db_session)
    older = await repository.create_conversation(7, " Older ")
    newer = await repository.create_conversation(7, "Newer")
    await repository.create_conversation(8, "Other owner")
    older.updated_at = datetime(2026, 8, 15, 8, 0)
    newer.updated_at = datetime(2026, 8, 16, 8, 0)
    await db_session.commit()

    conversations = await repository.list_conversations(7)

    assert [conversation.conversation_id for conversation in conversations] == [
        newer.conversation_id,
        older.conversation_id,
    ]
    assert older.title == "Older"


async def test_repository_hides_wrong_owner_but_keeps_archived_conversations_viewable(db_session):
    repository = ProductChatRepository(db_session)
    conversation = await repository.create_conversation(7, "Owned")

    with pytest.raises(ProductChatNotFoundError) as exc_info:
        await repository.require_conversation(conversation.conversation_id, 8)
    assert exc_info.value.code == "CONVERSATION_NOT_FOUND"
    assert exc_info.value.status_code == 404

    await repository.archive_conversation(conversation.conversation_id, 7)

    with pytest.raises(ProductChatNotFoundError):
        await repository.require_conversation(conversation.conversation_id, 7)
    with pytest.raises(ProductChatNotFoundError):
        await repository.archive_conversation(conversation.conversation_id, 7)
    listed = await repository.list_conversations(7)
    assert [item.conversation_id for item in listed] == [conversation.conversation_id]
    assert listed[0].status == ConversationStatus.ARCHIVED

    viewable = await repository.require_viewable_conversation(conversation.conversation_id, 7)
    assert viewable.status == ConversationStatus.ARCHIVED

    await repository.restore_conversation(conversation.conversation_id, 7)
    assert (await repository.list_conversations(7))[0].status == ConversationStatus.ACTIVE


async def test_append_exchange_stages_messages_citations_title_and_timestamp_for_caller_commit(db_session):
    repository = ProductChatRepository(db_session)
    conversation = await repository.create_conversation(7, "  ")
    previous_updated_at = conversation.updated_at
    question = "   这是一个用于生成会话标题的首问内容，超过三十个字符后应截断。   "

    user_message, assistant_message, stored_citations = await repository.append_exchange(
        conversation,
        7,
        question,
        _answer(),
    )
    await db_session.commit()

    messages = (
        (
            await db_session.execute(
                select(ProductMessage)
                .where(ProductMessage.conversation_id == conversation.conversation_id)
                .order_by(ProductMessage.id)
            )
        )
        .scalars()
        .all()
    )
    citations = (await db_session.execute(select(MessageCitation))).scalars().all()
    assert messages == [user_message, assistant_message]
    assert [message.role for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert user_message.answer_status is None
    assert assistant_message.answer_status == "SUPPORTED"
    assert assistant_message.model_version == "model-1"
    assert assistant_message.prompt_version == "enterprise-grounded-v1"
    assert len(citations) == 1
    assert citations == stored_citations
    assert citations[0].message_id == assistant_message.message_id
    assert citations[0].message_id != user_message.message_id
    assert citations[0].chunk_id == "chunk-1"
    assert citations[0].excerpt == "支持私有部署。"
    assert conversation.title == question.strip()[:30]
    assert conversation.updated_at >= previous_updated_at


async def test_append_exchange_persists_image_evidence_fields(db_session):
    repository = ProductChatRepository(db_session)
    conversation = await repository.create_conversation(7, "")
    citation = _citation(
        media_type="IMAGE",
        image_url="/minio/public/docs/architecture.png",
        preview_url="/minio/public/docs/previews/architecture.webp",
        image_alt="系统架构图",
    )

    _, assistant_message, _ = await repository.append_exchange(
        conversation,
        7,
        "展示系统架构图",
        _answer(citations=(citation,)),
    )
    await db_session.commit()
    _, stored = (await repository.list_messages_with_citations(conversation.conversation_id))[-1]

    assert stored[0].message_id == assistant_message.message_id
    assert stored[0].media_type == "IMAGE"
    assert stored[0].image_url == "/minio/public/docs/architecture.png"
    assert stored[0].preview_url == "/minio/public/docs/previews/architecture.webp"
    assert stored[0].image_alt == "系统架构图"


async def test_append_exchange_rejects_conversation_archived_after_require(db_session):
    repository = ProductChatRepository(db_session)
    conversation = await repository.create_conversation(7, "Owned")
    await repository.require_conversation(conversation.conversation_id, 7)

    other_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with other_factory() as other_session:
        await ProductChatRepository(other_session).archive_conversation(conversation.conversation_id, 7)

    with pytest.raises(ProductChatNotFoundError):
        await repository.append_exchange(
            conversation=conversation,
            owner_user_id=7,
            user_content="归档后不应写入",
            answer=_answer(),
        )

    assert await db_session.scalar(select(func.count()).select_from(ProductMessage)) == 0


async def test_append_exchange_rechecks_owner_inside_write_transaction(db_session):
    repository = ProductChatRepository(db_session)
    conversation = await repository.create_conversation(7, "Owned")

    with pytest.raises(ProductChatNotFoundError):
        await repository.append_exchange(
            conversation=conversation,
            owner_user_id=8,
            user_content="其他用户不应写入",
            answer=_answer(),
        )

    assert await db_session.scalar(select(func.count()).select_from(ProductMessage)) == 0


async def test_postgres_archive_lock_wins_before_append(postgres_answer_context):
    _, factory = postgres_answer_context
    owner_user_id, conversation = await _seed_postgres_conversation(factory)
    archive_locked = asyncio.Event()
    release_archive = asyncio.Event()

    async with factory() as archive_session, factory() as append_session:
        archive_pid = await archive_session.scalar(text("SELECT pg_backend_pid()"))
        append_pid = await append_session.scalar(text("SELECT pg_backend_pid()"))
        assert archive_pid is not None
        assert append_pid is not None

        async def archive_first() -> None:
            try:
                result = await archive_session.execute(
                    update(ProductConversation)
                    .where(
                        ProductConversation.conversation_id == conversation.conversation_id,
                        ProductConversation.status == ConversationStatus.ACTIVE,
                    )
                    .values(status=ConversationStatus.ARCHIVED)
                )
                assert result.rowcount == 1
                archive_locked.set()
                await release_archive.wait()
                await archive_session.commit()
            except Exception:
                await archive_session.rollback()
                raise

        archive_task = asyncio.create_task(archive_first())
        append_task = None
        try:
            await asyncio.wait_for(archive_locked.wait(), timeout=2)
            append_task = asyncio.create_task(
                ProductChatRepository(append_session).append_exchange(
                    conversation=conversation,
                    owner_user_id=owner_user_id,
                    user_content="归档竞争不应写入",
                    answer=_postgres_answer(),
                )
            )
            await _assert_postgres_backend_is_blocked(
                factory,
                blocked_pid=append_pid,
                blocker_pid=archive_pid,
            )
            release_archive.set()
            await asyncio.wait_for(archive_task, timeout=2)
            with pytest.raises(ProductChatNotFoundError):
                await asyncio.wait_for(append_task, timeout=2)
        finally:
            release_archive.set()
            await asyncio.gather(
                archive_task,
                *([append_task] if append_task is not None else []),
                return_exceptions=True,
            )

    async with factory() as verification_session:
        stored_status = await verification_session.scalar(
            select(ProductConversation.status).where(
                ProductConversation.conversation_id == conversation.conversation_id
            )
        )
        message_count = await verification_session.scalar(select(func.count()).select_from(ProductMessage))
    assert stored_status == ConversationStatus.ARCHIVED
    assert message_count == 0


async def test_postgres_append_lock_wins_before_archive(postgres_answer_context):
    schema_engine, factory = postgres_answer_context
    owner_user_id, conversation = await _seed_postgres_conversation(factory)
    append_selected = asyncio.Event()
    release_append = asyncio.Event()
    archive_update_started = asyncio.Event()

    class PausingAppendSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            selects_conversation = getattr(statement, "is_select", False) and any(
                description.get("entity") is ProductConversation
                for description in getattr(statement, "column_descriptions", ())
            )
            if selects_conversation:
                append_selected.set()
                await release_append.wait()
            return result

    class SignalingArchiveSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            updates_conversation = (
                getattr(statement, "is_update", False)
                and getattr(getattr(statement, "table", None), "name", None) == ProductConversation.__tablename__
            )
            if updates_conversation:
                archive_update_started.set()
            return await super().execute(statement, *args, **kwargs)

    append_factory = async_sessionmaker(
        schema_engine,
        class_=PausingAppendSession,
        expire_on_commit=False,
    )
    archive_factory = async_sessionmaker(
        schema_engine,
        class_=SignalingArchiveSession,
        expire_on_commit=False,
    )
    async with append_factory() as append_session, archive_factory() as archive_session:
        append_pid = await append_session.scalar(text("SELECT pg_backend_pid()"))
        archive_pid = await archive_session.scalar(text("SELECT pg_backend_pid()"))
        assert append_pid is not None
        assert archive_pid is not None

        async def append_and_commit():
            exchange = await ProductChatRepository(append_session).append_exchange(
                conversation=conversation,
                owner_user_id=owner_user_id,
                user_content="追加先取得锁",
                answer=_postgres_answer(),
            )
            await append_session.commit()
            return exchange

        append_task = asyncio.create_task(append_and_commit())
        archive_task = None
        try:
            await asyncio.wait_for(append_selected.wait(), timeout=2)
            archive_task = asyncio.create_task(
                ProductChatRepository(archive_session).archive_conversation(
                    conversation.conversation_id,
                    owner_user_id,
                )
            )
            await asyncio.wait_for(archive_update_started.wait(), timeout=2)
            await _assert_postgres_backend_is_blocked(
                factory,
                blocked_pid=archive_pid,
                blocker_pid=append_pid,
            )
            release_append.set()
            user_message, assistant_message, _ = await asyncio.wait_for(append_task, timeout=2)
            await asyncio.wait_for(archive_task, timeout=2)
            assert [user_message.role, assistant_message.role] == [
                MessageRole.USER,
                MessageRole.ASSISTANT,
            ]
        finally:
            release_append.set()
            await asyncio.gather(
                append_task,
                *([archive_task] if archive_task is not None else []),
                return_exceptions=True,
            )

    async with factory() as verification_session:
        stored_status = await verification_session.scalar(
            select(ProductConversation.status).where(
                ProductConversation.conversation_id == conversation.conversation_id
            )
        )
        message_count = await verification_session.scalar(select(func.count()).select_from(ProductMessage))
        source_version_at = await verification_session.scalar(select(MessageCitation.source_version_at))
    assert stored_status == ConversationStatus.ARCHIVED
    assert message_count == 2
    assert source_version_at == datetime(2026, 8, 16, 8, 0)


async def test_caller_rollback_removes_every_exchange_row_when_a_citation_fails(db_session):
    repository = ProductChatRepository(db_session)
    conversation = await repository.create_conversation(7, "")
    conversation_id = conversation.conversation_id

    with pytest.raises(Exception):
        await repository.append_exchange(
            conversation,
            7,
            "首问",
            _answer(citations=(_citation(source_url=None),)),
        )
    await db_session.rollback()

    message_count = await db_session.scalar(select(func.count()).select_from(ProductMessage))
    citation_count = await db_session.scalar(select(func.count()).select_from(MessageCitation))
    stored = await db_session.scalar(
        select(ProductConversation).where(ProductConversation.conversation_id == conversation_id)
    )
    assert message_count == 0
    assert citation_count == 0
    assert stored.title is None


async def test_repository_revalidates_only_active_formal_material_versions(db_session):
    db_session.add_all(
        [
            FeishuSource(
                source_id="source-1",
                name="Wiki",
                wiki_root_token="root",
                target_kb_id="kb-1",
                credential_env_name="FEISHU_TOKEN",
                enabled=True,
            ),
            FeishuSource(
                source_id="source-2",
                name="Other Wiki",
                wiki_root_token="root-2",
                target_kb_id="kb-2",
                credential_env_name="FEISHU_TOKEN_2",
                enabled=True,
            ),
        ]
    )
    current = _add_material(db_session, item_id="current", file_id="file-current")
    _add_material(db_session, item_id="inactive", file_id="file-inactive", active=False)
    _add_material(db_session, item_id="invalid", file_id="file-invalid", validity="invalid")
    draft = _add_material(db_session, item_id="draft", file_id="file-draft", processing_status="parsed")
    conflicted = _add_material(db_session, item_id="conflicted", file_id="file-conflicted")
    db_session.add(
        FeishuCrossDocumentRelation(
            relation_id="relation-open-conflict",
            comparison_key="version-conflicted:version-draft",
            source_version_id=conflicted[1].version_id,
            target_version_id=draft[1].version_id,
            relation_type="CONFLICT",
            status="open",
        )
    )
    _add_material(db_session, item_id="pending", file_id="file-pending", review_status="pending")
    _add_material(db_session, item_id="unpublished", file_id="file-unpublished", published_at=None)
    _add_material(db_session, item_id="other-source", file_id="file-other", source_id="source-2")
    await db_session.commit()

    repository = ProductChatRepository(db_session)
    result = await repository.get_published_evidence(
        "source-1",
        [
            "file-current",
            "file-inactive",
            "file-invalid",
            "file-draft",
            "file-conflicted",
            "file-pending",
            "file-unpublished",
            "file-other",
        ],
    )

    assert result == {"file-current": current}


async def test_repository_rejects_evidence_when_source_is_disabled(db_session):
    db_session.add(
        FeishuSource(
            source_id="source-disabled",
            name="Disabled Wiki",
            wiki_root_token="root-disabled",
            target_kb_id="kb-disabled",
            credential_env_name="FEISHU_TOKEN_DISABLED",
            enabled=False,
        )
    )
    _add_material(
        db_session,
        item_id="disabled-source-item",
        file_id="file-disabled-source",
        source_id="source-disabled",
    )
    await db_session.commit()

    result = await ProductChatRepository(db_session).get_published_evidence(
        "source-disabled",
        ["file-disabled-source"],
    )

    assert result == {}


async def test_repository_rejects_ambiguous_formal_versions_sharing_a_file_id(db_session):
    db_session.add(
        FeishuSource(
            source_id="source-1",
            name="Wiki",
            wiki_root_token="root",
            target_kb_id="kb-1",
            credential_env_name="FEISHU_TOKEN",
            enabled=True,
        )
    )
    _add_material(db_session, item_id="first", file_id="file-shared")
    _add_material(db_session, item_id="second", file_id="file-shared")
    await db_session.commit()

    result = await ProductChatRepository(db_session).get_published_evidence(
        "source-1",
        ["file-shared", "file-shared"],
    )

    assert result == {}


async def test_answer_service_owns_short_read_transactions_and_preserves_caller_writes(
    tmp_path,
    monkeypatch,
):
    from yuxi.product_chat.answer_service import AnswerService

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'answer-transactions.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as seed_session:
        seed_session.add(
            FeishuSource(
                source_id="source-1",
                name="Wiki",
                wiki_root_token="root",
                target_kb_id="kb-1",
                credential_env_name="FEISHU_TOKEN",
                enabled=True,
            )
        )
        _add_material(seed_session, item_id="item-1", file_id="file-1")
        await seed_session.commit()

    read_sessions = []

    def read_session_factory():
        session = factory()
        read_sessions.append(session)
        return session

    def assert_read_transactions_released(expected_session_count):
        assert len(read_sessions) == expected_session_count
        assert all(not session.in_transaction() for session in read_sessions)

    class Knowledge:
        async def check_policy_accessible(self, user, kb_id):
            return True

        async def aquery(self, question, kb_id, **kwargs):
            assert_read_transactions_released(2)
            return [{"content": "支持私有部署。", "metadata": {"file_id": "file-1", "chunk_index": 0}}]

        async def get_database_info(self, kb_id):
            assert_read_transactions_released(3)
            return {"llm_model_spec": "provider:model-1"}

    class Model:
        model_name = "model-1"

        async def call(self, prompt, stream=False):
            assert stream is True
            assert_read_transactions_released(3)

            async def chunks():
                yield SimpleNamespace(
                    content=json.dumps(
                        {"status": "SUPPORTED", "answer": "支持私有部署。", "citation_ids": ["E1"]},
                        ensure_ascii=False,
                    )
                )

            return chunks()

    monkeypatch.setenv("PRODUCT_FEISHU_SOURCE_ID", "source-1")
    caller_session = factory()
    unrelated = ProductConversation(owner_user_id=99, title="Unrelated")
    caller_session.add(unrelated)
    await caller_session.flush()
    assert caller_session.in_transaction()

    try:
        result = await AnswerService(
            db=caller_session,
            read_session_factory=read_session_factory,
            knowledge_base=Knowledge(),
            model_selector=lambda model_spec: Model(),
        ).answer("是否支持私有部署？", {"id": 7}, "conversation-1")

        assert result.status == "SUPPORTED"
        assert caller_session.in_transaction()
        assert await caller_session.get(ProductConversation, unrelated.id) is unrelated
        async with factory() as observer:
            assert await observer.get(ProductConversation, unrelated.id) is None
    finally:
        await caller_session.rollback()
        await caller_session.close()
        await engine.dispose()


async def test_model_failure_writes_nothing_and_successful_retry_appends_once(db_session):
    from yuxi.product_chat.answer_service import AnswerService

    db_session.add(
        FeishuSource(
            source_id="source-1",
            name="Wiki",
            wiki_root_token="root",
            target_kb_id="kb-1",
            credential_env_name="FEISHU_TOKEN",
            enabled=True,
        )
    )
    _add_material(db_session, item_id="item-1", file_id="file-1")
    await db_session.commit()
    repository = ProductChatRepository(db_session)
    conversation = await repository.create_conversation(7, "")

    class Policy:
        async def resolve_scope(self, user):
            return ProductKnowledgeScope("source-1", "kb-1", ("file-1",))

    class Knowledge:
        async def aquery(self, question, kb_id, **kwargs):
            return [{"content": "支持私有部署。", "metadata": {"file_id": "file-1", "chunk_index": 0}}]

        async def get_database_info(self, kb_id):
            return {"llm_model_spec": "provider:model-1"}

    class FlakyModel:
        model_name = "model-1"

        def __init__(self):
            self.calls = 0

        async def call(self, prompt, stream=False):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("model unavailable")

            async def chunks():
                yield SimpleNamespace(
                    content=json.dumps(
                        {"status": "SUPPORTED", "answer": "支持私有部署。", "citation_ids": ["E1"]},
                        ensure_ascii=False,
                    )
                )

            return chunks()

    model = FlakyModel()
    answer_service = AnswerService(
        db=db_session,
        repository=repository,
        policy_service=Policy(),
        knowledge_base=Knowledge(),
        model_selector=lambda model_spec: model,
    )

    with pytest.raises(RuntimeError, match="model unavailable"):
        await answer_service.answer("是否支持私有部署？", object(), conversation.conversation_id)
    assert await db_session.scalar(select(func.count()).select_from(ProductMessage)) == 0

    answer = await answer_service.answer("是否支持私有部署？", object(), conversation.conversation_id)
    await repository.append_exchange(conversation, 7, "是否支持私有部署？", answer)
    await db_session.commit()

    messages = (await db_session.execute(select(ProductMessage).order_by(ProductMessage.id))).scalars().all()
    citations = (await db_session.execute(select(MessageCitation))).scalars().all()
    assert [message.role for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert len(citations) == 1
    assert model.calls == 2
