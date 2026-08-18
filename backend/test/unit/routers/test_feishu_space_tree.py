from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.routers import feishu_knowledge_router as router
from yuxi.integrations.feishu.client import FeishuPermissionError
from yuxi.integrations.feishu.schemas import FeishuNode

pytestmark = pytest.mark.asyncio


def _node(token: str, title: str, *, parent: str | None = None, has_child: bool = False) -> FeishuNode:
    return FeishuNode(
        space_id="space-1",
        node_token=token,
        obj_token=f"obj-{token}",
        obj_type="docx",
        title=title,
        parent_node_token=parent,
        has_child=has_child,
    )


async def test_get_source_tree_lists_space_top_level_nodes_and_children(monkeypatch):
    root = _node("root", "首页", has_child=True)
    sibling = _node("sibling", "产品手册", has_child=True)
    child = _node("child", "部署文档", parent="sibling")
    calls = []

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def get_source(self, source_id):
            return SimpleNamespace(
                source_id=source_id,
                wiki_root_token="root",
                wiki_root_url="https://quickdone.feishu.cn/wiki/root",
                scan_scope="space",
            )

    class FakeClient:
        async def get_node(self, token):
            calls.append(("get_node", token))
            return root

        async def list_nodes(self, space_id, parent_node_token=None):
            calls.append(("list_nodes", space_id, parent_node_token))
            if parent_node_token is None:
                return [root, sibling]
            if parent_node_token == "sibling":
                return [child]
            return []

        async def aclose(self):
            calls.append(("close",))

    monkeypatch.setattr(router, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router, "create_user_authorized_feishu_client", lambda _source_id: FakeClient())

    response = await router.get_source_tree("source-1", db=SimpleNamespace())

    assert response["scope"] == "space"
    assert [node["title"] for node in response["nodes"]] == ["首页", "产品手册"]
    assert response["nodes"][1]["children"][0]["title"] == "部署文档"
    assert calls[-1] == ("close",)


async def test_get_source_tree_does_not_turn_space_permission_failure_into_empty_tree(monkeypatch):
    class FakeRepository:
        def __init__(self, _db):
            pass

        async def get_source(self, source_id):
            return SimpleNamespace(source_id=source_id, wiki_root_token="root", scan_scope="space")

    class FakeClient:
        async def get_node(self, token):
            return _node("root", "首页")

        async def list_nodes(self, space_id, parent_node_token=None):
            raise FeishuPermissionError("permission denied")

        async def aclose(self):
            pass

    monkeypatch.setattr(router, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router, "create_user_authorized_feishu_client", lambda _source_id: FakeClient())

    with pytest.raises(HTTPException) as raised:
        await router.get_source_tree("source-1", db=SimpleNamespace())

    assert raised.value.status_code == 424
    assert raised.value.detail == {
        "code": "FEISHU_SPACE_PERMISSION_DENIED",
        "message": "当前应用没有读取整个知识空间的权限，请在飞书开放平台开通后重试",
    }


async def test_scan_requires_active_user_oauth_before_queueing(monkeypatch):
    queued = []

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def get_source(self, source_id):
            return SimpleNamespace(source_id=source_id)

        async def queue_sync_run(self, **kwargs):
            queued.append(kwargs)

    class FakeOAuthService:
        def __init__(self, **_kwargs):
            pass

        async def get_authorization_status(self, _source_id):
            return {"authorized": False, "status": "not_authorized"}

    monkeypatch.setattr(router, "FeishuKnowledgeRepository", FakeRepository)
    monkeypatch.setattr(router, "FeishuUserOAuthService", FakeOAuthService)

    with pytest.raises(HTTPException) as raised:
        await router.scan_source(
            "source-1",
            router.ScanRequest(mode="full"),
            db=SimpleNamespace(),
            current_user=SimpleNamespace(uid="admin-1"),
        )

    assert raised.value.status_code == 424
    assert raised.value.detail["code"] == "FEISHU_USER_AUTHORIZATION_REQUIRED"
    assert queued == []
