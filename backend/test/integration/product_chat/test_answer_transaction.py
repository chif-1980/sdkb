import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.product_chat.repository import ProductChatNotFoundError, ProductChatRepository
from yuxi.product_chat.source_policy_service import ProductKnowledgeScope
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
)
from yuxi.storage.postgres.models_product import MessageCitation, MessageRole, ProductConversation, ProductMessage


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


def _citation(**overrides):
    values = {
        "evidence_id": "E1",
        "source_id": "source-1",
        "item_id": "item-1",
        "version_id": "version-1",
        "yuxi_file_id": "file-1",
        "title": "产品手册",
        "source_url": "https://example.test/item-1",
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
        source_url=f"https://example.test/{item_id}",
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


async def test_repository_lists_only_owned_active_conversations_in_recent_order(db_session):
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


async def test_repository_hides_wrong_owner_and_archived_conversations(db_session):
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
    assert await repository.list_conversations(7) == []


async def test_append_exchange_commits_messages_citations_title_and_timestamp_together(db_session):
    repository = ProductChatRepository(db_session)
    conversation = await repository.create_conversation(7, "  ")
    previous_updated_at = conversation.updated_at
    question = "   这是一个用于生成会话标题的首问内容，超过三十个字符后应截断。   "

    user_message, assistant_message = await repository.append_exchange(
        conversation,
        7,
        question,
        _answer(),
    )

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
    assert citations[0].message_id == assistant_message.message_id
    assert citations[0].message_id != user_message.message_id
    assert citations[0].excerpt == "支持私有部署。"
    assert conversation.title == question.strip()[:30]
    assert conversation.updated_at >= previous_updated_at


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


async def test_append_exchange_rolls_back_every_row_when_a_citation_fails(db_session):
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
    _add_material(db_session, item_id="draft", file_id="file-draft", processing_status="parsed")
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
            assert_read_transactions_released(1)
            return [{"content": "支持私有部署。", "metadata": {"file_id": "file-1", "chunk_index": 0}}]

        async def get_database_info(self, kb_id):
            assert_read_transactions_released(2)
            return {"llm_model_spec": "provider:model-1"}

    class Model:
        model_name = "model-1"

        async def call(self, prompt, stream=False):
            assert_read_transactions_released(2)
            return SimpleNamespace(
                content=json.dumps(
                    {"status": "SUPPORTED", "answer": "支持私有部署。", "citation_ids": ["E1"]},
                    ensure_ascii=False,
                )
            )

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
            return SimpleNamespace(
                content=json.dumps(
                    {"status": "SUPPORTED", "answer": "支持私有部署。", "citation_ids": ["E1"]},
                    ensure_ascii=False,
                )
            )

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

    messages = (await db_session.execute(select(ProductMessage).order_by(ProductMessage.id))).scalars().all()
    citations = (await db_session.execute(select(MessageCitation))).scalars().all()
    assert [message.role for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert len(citations) == 1
    assert model.calls == 2
