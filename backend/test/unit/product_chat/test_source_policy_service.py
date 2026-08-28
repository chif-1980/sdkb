from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.product_chat.auth_service import ProductAuthError
from yuxi.product_chat.source_policy_service import ProductKnowledgeScope, ProductSourcePolicyService
from yuxi.storage.postgres.models_business import Base, Department, User
from yuxi.storage.postgres.models_knowledge import (
    FeishuCrossDocumentRelation,
    FeishuMaterialVersion,
    FeishuSource,
    FeishuSourceItem,
    KnowledgeBase,
)
from yuxi.storage.postgres.models_product import (
    FeishuDepartmentBinding,
    FeishuUserDepartmentMembership,
)


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def product_source_env(monkeypatch):
    monkeypatch.setenv("PRODUCT_FEISHU_SOURCE_ID", "source-1")


def _user(uid="uid-1", role="user", department_id=7):
    return SimpleNamespace(
        to_dict=lambda: {"uid": uid, "role": role, "department_id": department_id},
    )


class _PolicyManager:
    def __init__(self, accessible):
        self.accessible = accessible

    async def check_policy_accessible(self, user, kb_id):
        return self.accessible


class _FailingPolicyManager:
    async def check_policy_accessible(self, user, kb_id):
        raise ProductAuthError("AUTH_SERVICE_UNAVAILABLE", 503)


class _DepartmentPolicyManager:
    async def check_policy_accessible(self, user, kb_id):
        return kb_id == "kb-1" and user.get("department_id") == 8


async def test_resolve_scope_rejects_a_missing_source(db_session):
    with pytest.raises(ProductAuthError) as exc_info:
        await ProductSourcePolicyService(
            db=db_session,
            knowledge_base=_PolicyManager(True),
        ).resolve_scope(_user())

    assert exc_info.value.code == "PRODUCT_SOURCE_UNAVAILABLE"
    assert exc_info.value.status_code == 503


async def test_resolve_scope_accepts_any_current_feishu_department_membership(db_session):
    user = User(
        username="Employee",
        uid="employee-001",
        password_hash="not-used",
        role="user",
        department=Department(id=7, name="Engineering"),
    )
    product = Department(id=8, name="Product")
    db_session.add_all(
        [
            user,
            product,
            FeishuSource(
                source_id="source-1",
                name="Wiki",
                wiki_root_token="root",
                target_kb_id="kb-1",
                credential_env_name="FEISHU_TOKEN",
                enabled=True,
            ),
        ]
    )
    await db_session.flush()
    department_binding = FeishuDepartmentBinding(
        tenant_key="tenant-a",
        feishu_department_id="od_product",
        department_id=product.id,
        display_name="Product",
    )
    db_session.add(department_binding)
    await db_session.flush()
    db_session.add(
        FeishuUserDepartmentMembership(
            user_id=user.id,
            department_binding_id=department_binding.id,
            position=1,
        )
    )
    await db_session.commit()

    scope = await ProductSourcePolicyService(
        db=db_session,
        knowledge_base=_DepartmentPolicyManager(),
    ).resolve_scope(user)

    assert scope.kb_id == "kb-1"


async def test_resolve_scope_keeps_user_policy_without_a_primary_department(db_session):
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
    await db_session.commit()

    scope = await ProductSourcePolicyService(
        db=db_session,
        knowledge_base=_PolicyManager(True),
    ).resolve_scope(_user(department_id=None))

    assert scope.kb_id == "kb-1"


async def test_resolve_scope_rejects_a_disabled_source(db_session):
    db_session.add(
        FeishuSource(
            source_id="source-1",
            name="Wiki",
            wiki_root_token="root",
            target_kb_id="kb-1",
            credential_env_name="FEISHU_TOKEN",
            enabled=False,
        )
    )
    await db_session.commit()

    with pytest.raises(ProductAuthError) as exc_info:
        await ProductSourcePolicyService(
            db=db_session,
            knowledge_base=_PolicyManager(True),
        ).resolve_scope(_user())

    assert exc_info.value.code == "PRODUCT_SOURCE_UNAVAILABLE"
    assert exc_info.value.status_code == 503


async def test_resolve_scope_only_returns_current_published_approved_versions(db_session):
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
    db_session.add(
        KnowledgeBase(
            kb_id="kb-1",
            name="Product",
            kb_type="milvus",
            share_config={"access_level": "user", "user_uids": ["uid-1"]},
        )
    )

    def add_item(item_id, *, validity="valid", active_version_id=None):
        item = FeishuSourceItem(
            item_id=item_id,
            source_id="source-1",
            item_key=f"key-{item_id}",
            item_type="page",
            source_validity=validity,
            active_version_id=active_version_id,
        )
        db_session.add(item)
        return item

    now = datetime.now(UTC)
    current = add_item("item-current", active_version_id="version-current")
    current_version = FeishuMaterialVersion(
        version_id="version-current",
        item_id=current.item_id,
        revision="2",
        content_hash="current",
        processing_status="published",
        review_status="approved",
        published_at=now,
        yuxi_file_id="file-current",
    )
    old_version = FeishuMaterialVersion(
        version_id="version-old",
        item_id=current.item_id,
        revision="1",
        content_hash="old",
        processing_status="published",
        review_status="approved",
        published_at=now,
        yuxi_file_id="file-old",
    )
    pending_item = add_item("item-pending", active_version_id="version-pending")
    pending_version = FeishuMaterialVersion(
        version_id="version-pending",
        item_id=pending_item.item_id,
        revision="1",
        content_hash="pending",
        processing_status="published",
        review_status="pending",
        published_at=now,
        yuxi_file_id="file-pending",
    )
    failed_item = add_item("item-failed", active_version_id="version-failed")
    failed_version = FeishuMaterialVersion(
        version_id="version-failed",
        item_id=failed_item.item_id,
        revision="1",
        content_hash="failed",
        processing_status="error_indexing",
        review_status="approved",
        published_at=now,
        yuxi_file_id="file-failed",
    )
    invalid_item = add_item("item-invalid", validity="invalid", active_version_id="version-invalid")
    invalid_version = FeishuMaterialVersion(
        version_id="version-invalid",
        item_id=invalid_item.item_id,
        revision="1",
        content_hash="invalid",
        processing_status="published",
        review_status="approved",
        published_at=now,
        yuxi_file_id="file-invalid",
    )
    conflicted_item = add_item("item-conflicted", active_version_id="version-conflicted")
    conflicted_version = FeishuMaterialVersion(
        version_id="version-conflicted",
        item_id=conflicted_item.item_id,
        revision="1",
        content_hash="conflicted",
        processing_status="published",
        review_status="approved",
        published_at=now,
        yuxi_file_id="file-conflicted",
    )
    open_conflict = FeishuCrossDocumentRelation(
        relation_id="relation-open-conflict",
        comparison_key="version-conflicted:version-pending",
        source_version_id=conflicted_version.version_id,
        target_version_id=pending_version.version_id,
        relation_type="CONFLICT",
        status="open",
    )
    db_session.add_all(
        [
            current_version,
            old_version,
            pending_version,
            failed_version,
            invalid_version,
            conflicted_version,
            open_conflict,
        ]
    )
    await db_session.commit()

    service = ProductSourcePolicyService(db=db_session, knowledge_base=_PolicyManager(True))
    assert await service.resolve_scope(_user()) == ProductKnowledgeScope(
        source_id="source-1",
        kb_id="kb-1",
        allowed_file_ids=("file-current",),
    )


async def test_superadmin_still_requires_product_knowledge_policy(db_session):
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
    db_session.add(
        KnowledgeBase(
            kb_id="kb-1",
            name="Product",
            kb_type="milvus",
            share_config={"access_level": "user", "user_uids": ["other-user"]},
        )
    )
    await db_session.commit()

    with pytest.raises(ProductAuthError, match="KNOWLEDGE_ACCESS_DENIED"):
        await ProductSourcePolicyService(
            db=db_session,
            knowledge_base=_PolicyManager(False),
        ).resolve_scope(_user(role="superadmin"))


async def test_resolve_scope_returns_an_empty_whitelist_when_no_files_are_published(db_session):
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
    await db_session.commit()

    scope = await ProductSourcePolicyService(
        db=db_session,
        knowledge_base=_PolicyManager(True),
    ).resolve_scope(_user())

    assert scope == ProductKnowledgeScope(
        source_id="source-1",
        kb_id="kb-1",
        allowed_file_ids=(),
    )


async def test_policy_errors_are_exposed_as_access_denied_without_retrieval(db_session):
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
    db_session.add(
        KnowledgeBase(
            kb_id="kb-1",
            name="Product",
            kb_type="milvus",
            share_config={"access_level": "user", "user_uids": ["other-user"]},
        )
    )
    await db_session.commit()
    with pytest.raises(ProductAuthError) as exc_info:
        await ProductSourcePolicyService(
            db=db_session,
            knowledge_base=_PolicyManager(False),
        ).resolve_scope(_user())

    assert exc_info.value.code == "KNOWLEDGE_ACCESS_DENIED"


async def test_policy_manager_auth_errors_are_normalized_to_access_denied(db_session):
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
    await db_session.commit()

    with pytest.raises(ProductAuthError) as exc_info:
        await ProductSourcePolicyService(
            db=db_session,
            knowledge_base=_FailingPolicyManager(),
        ).resolve_scope(_user())

    assert exc_info.value.code == "KNOWLEDGE_ACCESS_DENIED"
    assert exc_info.value.status_code == 403
