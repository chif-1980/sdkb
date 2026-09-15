"""Link-based onboarding uses existing sources and read-only connection checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from server.routers import feishu_knowledge_router as router


def test_link_resolves_node_and_strips_share_parameters():
    payload = router.SourceCreate(
        wiki_root_url=" https://team.feishu.cn/wiki/Abc123?from=copy#section ",
        target_kb_id="kb-1",
    )
    assert payload.wiki_root_token == "Abc123"
    assert payload.wiki_root_url == "https://team.feishu.cn/wiki/Abc123"
    assert payload.scan_scope == "root"
    assert payload.name == "飞书知识库"


@pytest.mark.parametrize("url", [
    "http://team.feishu.cn/wiki/Abc123",
    "https://team.feishu.cn.evil.example/wiki/Abc123",
    "https://evil.example/wiki/Abc123",
    "https://team.feishu.cn/docx/Abc123",
    "https://team.feishu.cn/wiki/",
    "https://user:password@team.feishu.cn/wiki/Abc123",
    "https://team.feishu.cn:8443/wiki/Abc123",
    "https://team.feishu.cn/wiki/../admin",
    "https://team.feishu.cn/wiki/" + "A" * 256,
])
def test_invalid_links_are_rejected_before_any_network_access(url):
    with pytest.raises(ValidationError):
        router.SourceCreate(wiki_root_url=url, target_kb_id="kb-1")


def test_link_and_legacy_token_cannot_disagree():
    with pytest.raises(ValidationError, match="不一致"):
        router.SourceCreate(wiki_root_url="https://team.feishu.cn/wiki/Abc123",
                            wiki_root_token="Different", target_kb_id="kb-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("databases", [[], [{"kb_id": "kb-1", "kb_type": "notion"}]])
async def test_missing_or_read_only_target_creates_no_source(monkeypatch, databases):
    monkeypatch.setattr(router.knowledge_base, "get_databases_by_uid",
                        AsyncMock(return_value={"databases": databases}))
    create = AsyncMock()
    monkeypatch.setattr(router.FeishuKnowledgeRepository, "get_or_create_source", create)
    with pytest.raises(HTTPException) as failure:
        await router.create_source(
            router.SourceCreate(wiki_root_url="https://team.feishu.cn/wiki/Abc123", target_kb_id="kb-1"),
            db=SimpleNamespace(), current_user=SimpleNamespace(uid="admin"),
        )
    assert failure.value.status_code == 422
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,denied", [("root", False), ("space", False), ("space", True)])
async def test_connection_checks_selected_scope_and_always_closes_client(monkeypatch, scope, denied):
    name_target = AsyncMock()
    monkeypatch.setattr(router, "name_feishu_target", name_target)
    source = SimpleNamespace(wiki_root_token="Abc123", scan_scope=scope, name="飞书知识库")
    db = SimpleNamespace(flush=AsyncMock())
    monkeypatch.setattr(router.FeishuKnowledgeRepository, "get_source", AsyncMock(return_value=source))
    client = SimpleNamespace(
        get_node=AsyncMock(return_value=SimpleNamespace(title="制度目录", space_id="space-1")),
        list_nodes=AsyncMock(), aclose=AsyncMock(),
    )
    if denied:
        client.list_nodes.side_effect = router.FeishuPermissionError("denied")
    monkeypatch.setattr(router, "create_user_authorized_feishu_client", lambda _: client)
    if denied:
        with pytest.raises(HTTPException) as failure:
            await router.check_source("source-1", db=db)
        assert failure.value.detail["code"] == "FEISHU_SPACE_PERMISSION_DENIED"
        db.flush.assert_not_awaited()
        name_target.assert_not_awaited()
    else:
        result = await router.check_source("source-1", db=db)
        assert result["root_title"] == "制度目录"
        assert result["scan_scope"] == scope
        assert source.name == "制度目录"
        db.flush.assert_awaited_once()
        name_target.assert_awaited_once_with(source, "制度目录")
    assert client.list_nodes.await_count == (1 if scope == "space" else 0)
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_automatic_target_binds_source_or_cleans_up_on_save_failure(monkeypatch, fails):
    user = SimpleNamespace(uid="admin", department_id=1)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    create_target = AsyncMock(return_value="auto-kb")
    delete_target = AsyncMock()
    monkeypatch.setattr(router, "create_feishu_target", create_target)
    monkeypatch.setattr(router.knowledge_base, "delete_database", delete_target)

    async def save(**kwargs):
        if fails:
            raise RuntimeError("save failed")
        return SimpleNamespace(**kwargs, created_at=None, updated_at=None)

    monkeypatch.setattr(router.FeishuKnowledgeRepository, "get_or_create_source", AsyncMock(side_effect=save))
    payload = router.SourceCreate(wiki_root_url="https://team.feishu.cn/wiki/Abc123")
    if fails:
        with pytest.raises(RuntimeError, match="save failed"):
            await router.create_source(payload, db=db, current_user=user)
        db.rollback.assert_awaited_once()
        delete_target.assert_awaited_once_with("auto-kb")
    else:
        result = await router.create_source(payload, db=db, current_user=user)
        assert result["target_kb_id"] == "auto-kb"
        create_target.assert_awaited_once_with(result["source_id"], user)
        db.commit.assert_awaited_once()
        delete_target.assert_not_awaited()
