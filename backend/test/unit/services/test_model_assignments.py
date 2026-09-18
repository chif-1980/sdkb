from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.routers import system_router
from yuxi.agents.context import BaseContext, prepare_agent_runtime_context
from yuxi.agents.models import system_chat_model_spec
from yuxi.models.providers import assignments


@pytest.mark.asyncio
async def test_invalid_assignment_does_not_partially_save(monkeypatch):
    cfg = SimpleNamespace(update_and_save=lambda _: pytest.fail("invalid update applied"))
    monkeypatch.setattr(system_router, "config", cfg)
    monkeypatch.setattr(assignments.model_cache, "get_model_info", lambda spec: (
        SimpleNamespace(model_type="chat", api_key="test") if spec == "p:chat" else None
    ))
    with pytest.raises(HTTPException) as error:
        await system_router.update_config_batch({"default_model": "p:chat", "reranker": "missing"}, object())
    assert error.value.status_code == 400


@pytest.mark.parametrize("info", [None, SimpleNamespace(model_type="embedding", api_key="key"),
                                 SimpleNamespace(model_type="chat", api_key="")])
def test_invalid_type_disabled_or_missing_key_rejected(monkeypatch, info):
    monkeypatch.setattr(assignments.model_cache, "get_model_info", lambda _: info)
    with pytest.raises(ValueError):
        assignments.validate_model_assignments({"default_model": "provider:model"})


@pytest.mark.asyncio
async def test_runtime_refresh_ignores_old_agent_and_request_model(monkeypatch):
    from yuxi.agents import models

    cfg = SimpleNamespace(default_model="old:model")
    cfg.refresh = lambda: setattr(cfg, "default_model", "new:model")
    monkeypatch.setattr(models, "sys_config", cfg)
    assert system_chat_model_spec() == "new:model"
    context = await prepare_agent_runtime_context(BaseContext(model="old:agent"))
    assert context.model == "new:model"
    assert "model" not in BaseContext.get_configurable_items()


@pytest.mark.asyncio
async def test_valid_assignment_saves_and_returns_config(monkeypatch):
    changes = []
    cfg = SimpleNamespace(update_and_save=lambda items: changes.extend([items, "save"]),
                          dump_config=lambda: {"default_model": "p:chat"})
    monkeypatch.setattr(system_router, "config", cfg)
    monkeypatch.setattr(assignments.model_cache, "get_model_info",
                        lambda _: SimpleNamespace(model_type="chat", api_key="test"))
    assert await system_router.update_config_batch({"default_model": "p:chat"}, object()) == {"default_model": "p:chat"}
    assert changes == [{"default_model": "p:chat"}, "save"]


@pytest.mark.asyncio
async def test_save_failure_returns_error_not_success(monkeypatch):
    def fail(items):
        raise OSError("disk unavailable")
    monkeypatch.setattr(system_router, "config", SimpleNamespace(update_and_save=fail))
    monkeypatch.setattr(assignments.model_cache, "get_model_info",
                        lambda _: SimpleNamespace(model_type="chat", api_key="test"))
    with pytest.raises(HTTPException) as error:
        await system_router.update_config_batch({"default_model": "p:chat"}, object())
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_simple_call_uses_current_fast_model_not_client_override(monkeypatch):
    from server.routers import chat_router
    used = []
    cfg = SimpleNamespace(fast_model="old:fast")
    cfg.refresh = lambda: setattr(cfg, "fast_model", "new:fast")
    async def call(query):
        return SimpleNamespace(content="title")
    monkeypatch.setattr(chat_router, "conf", cfg)
    monkeypatch.setattr(chat_router, "select_model",
                        lambda **kwargs: used.append(kwargs["model_spec"]) or SimpleNamespace(call=call))
    response = await chat_router.call("title", {"model_spec": "old:client"}, object())
    assert response["response"] == "title"
    assert used == ["new:fast"]


@pytest.mark.asyncio
async def test_guard_uses_updated_model_and_enable_flag(monkeypatch):
    from yuxi.utils import guard
    used = []
    cfg = SimpleNamespace(enable_content_guard_llm=True, content_guard_llm_model="p:first", refresh=lambda: None)
    async def call(prompt):
        return SimpleNamespace(content="合规")
    monkeypatch.setattr(guard, "config", cfg)
    monkeypatch.setattr(guard, "select_model",
                        lambda **kwargs: used.append(kwargs["model_spec"]) or SimpleNamespace(call=call))
    instance = guard.ContentGuard()
    assert await instance.check("普通测试内容") is False
    cfg.content_guard_llm_model = "p:second"
    assert await instance.check("普通测试内容") is False
    cfg.enable_content_guard_llm = False
    assert await instance.check("普通测试内容") is False
    assert used == ["p:first", "p:second"]
