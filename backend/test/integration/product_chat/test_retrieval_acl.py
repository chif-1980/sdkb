"""Product retrieval ACL integration contracts using isolated in-memory dependencies."""

from types import SimpleNamespace

import pytest

from yuxi.knowledge.manager import KnowledgeBaseManager
from yuxi.product_chat.auth_service import ProductAuthError
from yuxi.product_chat.source_policy_service import ProductSourcePolicyService
from yuxi.repositories.feishu_knowledge_repository import FeishuKnowledgeRepository
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository


pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_policy_failure_stops_model_and_retrieval_chain(monkeypatch):
    calls = []
    monkeypatch.setenv("PRODUCT_FEISHU_SOURCE_ID", "source-1")

    async def get_source(self, source_id):
        return SimpleNamespace(source_id=source_id, target_kb_id="kb-1", enabled=True)

    class _RaisingPolicyManager:
        async def check_policy_accessible(self, user, kb_id):
            calls.append("policy")
            raise RuntimeError("policy backend unavailable")

    async def list_files(*args, **kwargs):
        calls.append("published-files")
        return ["file-current"]

    monkeypatch.setattr(FeishuKnowledgeRepository, "get_source", get_source)
    monkeypatch.setattr(ProductSourcePolicyService, "_list_published_file_ids", list_files)

    async def invoke_product_query():
        service = ProductSourcePolicyService(db=object(), knowledge_base=_RaisingPolicyManager())
        user = SimpleNamespace(to_dict=lambda: {"uid": "uid-1", "role": "user", "department_id": 7})
        scope = await service.resolve_scope(user)
        calls.append(("model", scope))

    with pytest.raises(ProductAuthError) as exc_info:
        await invoke_product_query()

    assert exc_info.value.code == "KNOWLEDGE_ACCESS_DENIED"
    assert calls == ["policy"]


@pytest.mark.asyncio
async def test_strict_policy_access_does_not_bypass_for_superadmin_or_creator(monkeypatch):
    kb = SimpleNamespace(
        kb_id="kb-1",
        created_by="creator-1",
        share_config={"access_level": "user", "user_uids": ["allowed-user"]},
    )

    async def get_by_kb_id(self, kb_id):
        return kb if kb_id == "kb-1" else None

    monkeypatch.setattr(KnowledgeBaseRepository, "get_by_kb_id", get_by_kb_id)
    manager = object.__new__(KnowledgeBaseManager)

    assert (
        await manager.check_policy_accessible(
            {"uid": "creator-1", "role": "superadmin", "department_id": 7},
            "kb-1",
        )
        is False
    )
