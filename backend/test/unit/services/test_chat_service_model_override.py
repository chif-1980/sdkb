import pytest

from yuxi.services import chat_service as svc


@pytest.mark.parametrize("meta", [{"model_spec": "user-pick"}, {"request_id": "r1"}, None])
def test_legacy_overrides_use_unified_model(monkeypatch, meta):
    monkeypatch.setattr("yuxi.agents.models.system_chat_model_spec", lambda: "unified:chat")
    input_context = {"model": "agent-default"}
    svc._apply_model_override(input_context, meta)
    assert input_context["model"] == "unified:chat"
