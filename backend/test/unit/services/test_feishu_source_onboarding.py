from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.services import feishu_source_onboarding as service

pytestmark = pytest.mark.asyncio


async def test_create_uses_platform_model_and_does_not_publish_or_share(monkeypatch):
    monkeypatch.setattr(service.config, "embed_model", "provider:embedding")
    monkeypatch.setattr(service.model_cache, "get_model_info", lambda spec: SimpleNamespace(model_type="embedding"))
    create = AsyncMock(return_value={"kb_id": "auto-kb"})
    monkeypatch.setattr(service.knowledge_base, "create_database", create)
    result = await service.create_feishu_target("source-1", SimpleNamespace(uid="admin", department_id=1))
    assert result == "auto-kb"
    kwargs = create.call_args.kwargs
    assert kwargs["embedding_model_spec"] == "provider:embedding"
    assert kwargs["feishu_source_id"] == "source-1"
    assert kwargs["feishu_name_pending"] is True
    assert kwargs["share_config"] == {"access_level": "user", "user_uids": ["admin"], "department_ids": []}


async def test_missing_default_model_creates_nothing(monkeypatch):
    monkeypatch.setattr(service.model_cache, "get_model_info", lambda spec: None)
    create = AsyncMock()
    monkeypatch.setattr(service.knowledge_base, "create_database", create)
    with pytest.raises(ValueError, match="默认索引模型不可用"):
        await service.create_feishu_target("source-1", SimpleNamespace(uid="admin", department_id=1))
    create.assert_not_awaited()


@pytest.mark.parametrize("existing_name", [False, True])
async def test_authorized_title_names_only_pending_target_once(monkeypatch, existing_name):
    source = SimpleNamespace(source_id="source-1", target_kb_id="auto-kb")
    target = SimpleNamespace(name="待授权", description="说明", additional_params={
        "feishu_source_id": "source-1", "feishu_name_pending": True,
    })
    monkeypatch.setattr(service.KnowledgeBaseRepository, "get_by_kb_id", AsyncMock(return_value=target))
    monkeypatch.setattr(service.knowledge_base, "database_name_exists", AsyncMock(return_value=existing_name))

    async def update(kb_id, name, description, **kwargs):
        target.name = name
        target.additional_params.update(kwargs["additional_params"])

    rename = AsyncMock(side_effect=update)
    monkeypatch.setattr(service.knowledge_base, "update_database", rename)
    await service.name_feishu_target(source, "公司制度")
    assert target.name == ("公司制度（source-1）" if existing_name else "公司制度")
    await service.name_feishu_target(source, "后来的飞书名称")
    rename.assert_awaited_once()


@pytest.mark.parametrize("metadata", [{}, {"feishu_source_id": "another-source", "feishu_name_pending": True}])
async def test_existing_target_is_never_renamed(monkeypatch, metadata):
    target = SimpleNamespace(additional_params=metadata)
    monkeypatch.setattr(service.KnowledgeBaseRepository, "get_by_kb_id", AsyncMock(return_value=target))
    rename = AsyncMock()
    monkeypatch.setattr(service.knowledge_base, "update_database", rename)
    await service.name_feishu_target(SimpleNamespace(source_id="source-1", target_kb_id="existing-kb"), "公司制度")
    rename.assert_not_awaited()
