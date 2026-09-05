from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from server.routers import product_chat_router


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_solution_context_replays_multi_step_resume_chain(monkeypatch):
    runs = {
        "RUN-2": {
            "run_type": "resume",
            "created_by_run_id": "RUN-1",
            "input_content": '"预算为 30 万"',
        },
        "RUN-1": {
            "run_type": "resume",
            "created_by_run_id": "RUN-0",
            "input_content": '"客户是轨交集团"',
        },
        "RUN-0": {
            "run_type": "chat",
            "conversation_thread_id": "product-CONV-1",
            "request_id": "REQ-1",
            "input_content": "设计智慧运维方案",
            "input_metadata": {
                "agent_invocation_meta": {
                    "product_conversation_id": "CONV-1",
                    "product_attachment_ids": ["ATT-1"],
                },
            },
        },
    }

    async def fake_get_agent_run_view(*, run_id, current_uid, db):
        return {"run": runs[run_id]}

    monkeypatch.setattr(product_chat_router.pg_manager, "get_async_session_context", lambda: _SessionContext())
    monkeypatch.setattr(product_chat_router, "get_agent_run_view", fake_get_agent_run_view)

    conversation_id, request = await product_chat_router._solution_context_for_run(
        run_id="RUN-2",
        current_user=SimpleNamespace(uid="USER-1"),
    )

    assert conversation_id == "CONV-1"
    assert request.request_id == "REQ-1"
    assert request.attachment_ids == ["ATT-1"]
    assert request.content == "设计智慧运维方案\n\n补充信息：\n客户是轨交集团\n预算为 30 万"


def test_solution_progress_events_follow_agent_order_and_do_not_pollute_answer_body():
    state: dict[str, object] = {}

    first = product_chat_router._solution_progress_events(
        {"stage": "UNDERSTANDING", "message": "正在分析需求并规划方案"},
        state,
    )
    assert first == [
        ("progress", {"stage": "UNDERSTANDING", "message": "正在分析需求并规划方案"}),
    ]

    next_events = product_chat_router._solution_progress_events(
        {"stage": "RETRIEVING", "message": "正在检索并展开正式知识"},
        state,
    )
    assert next_events == [
        ("progress", {"stage": "RETRIEVING", "message": "正在检索并展开正式知识"}),
    ]

    # The agent can return to an earlier kind of action.  Preserve that real
    # order, while collapsing only an adjacent exact duplicate.
    assert product_chat_router._solution_progress_events(
        {"stage": "CAPABILITY_MATCHING", "message": "正在匹配企业能力边界"},
        state,
    ) == [
        ("progress", {"stage": "CAPABILITY_MATCHING", "message": "正在匹配企业能力边界"}),
    ]
    assert product_chat_router._solution_progress_events(
        {"stage": "VERIFYING", "message": "正在核验高风险事实与冲突"},
        state,
    ) == [
        ("progress", {"stage": "VERIFYING", "message": "正在核验高风险事实与冲突"}),
    ]
    assert product_chat_router._solution_progress_events(
        {"stage": "RETRIEVING", "message": "正在补充正式知识与会话附件"},
        state,
    ) == [
        ("progress", {"stage": "RETRIEVING", "message": "正在补充正式知识与会话附件"}),
    ]
    assert product_chat_router._solution_progress_events(
        {"stage": "RETRIEVING", "message": "正在补充正式知识与会话附件"},
        state,
    ) == []


def test_agent_progress_uses_runtime_actions_instead_of_prompt_keywords():
    prompt_only = (
        'event: messages\ndata: {"payload":{"chunk":{"status":"loading",'
        '"message":"客户需求包含总体架构和质量审核"}}}\n\n'
    )
    assert product_chat_router._agent_progress(prompt_only) is None

    tool_event = {
        "payload": {
            "chunk": {
                "status": "loading",
                "stream_event": {
                    "type": "tool_call",
                    "name": "query_kb",
                    "tool_call_id": "CALL-1",
                },
            }
        }
    }
    progress = product_chat_router._agent_progress(
        f"event: messages\ndata: {json.dumps(tool_event, ensure_ascii=False)}\n\n"
    )
    assert progress == {
        "stage": "RETRIEVING",
        "message": "正在检索正式知识",
        "delta": "",
    }


def test_solution_safe_stream_delta_only_exposes_blueprint_text():
    state: dict[str, object] = {}

    assert product_chat_router._solution_safe_stream_delta(
        '{"executive_summary":"这是一个', state
    ) == "这是一个"
    assert product_chat_router._solution_safe_stream_delta(
        '可落地方案\\n，包含正式依据"}', state
    ) == "可落地方案\n，包含正式依据"
    assert product_chat_router._solution_safe_stream_delta(
        ',"tool_args":"secret"}', state
    ) == ""


def test_solution_stream_chunks_split_aggregated_body_without_losing_text():
    content = (
        "这是一个较长的方案摘要，用于验证单个运行事件中的聚合正文会被拆成多次可见更新，"
        "而不是一次性刷出。还要保留段落顺序、中文标点和原始内容，确保浏览器可以逐段渲染。"
    )

    chunks = product_chat_router._solution_stream_chunks(content)

    assert len(chunks) > 1
    assert "".join(chunks) == content
    assert all(len(chunk) <= product_chat_router._SOLUTION_STREAM_CHUNK_MAX_CHARS for chunk in chunks)


@pytest.mark.asyncio
async def test_solution_stream_separates_runtime_progress_from_answer_deltas(monkeypatch):
    async def fake_create_solution_run(*, conversation_id, request, current_user):
        return {"run_id": "RUN-1", "status": "running"}

    async def fake_stream_agent_run_events(**kwargs):
        # Plain payload text is not treated as a runtime action.
        yield 'event: messages\ndata: {"payload":{"architecture":"已完成方案设计"}}\n\n'
        payload = {
            "payload": {
                "chunk": {
                    "stream_event": {
                        "type": "message_delta",
                        "content": '{"executive_summary":"可落地方案',
                    }
                }
            }
        }
        yield f"event: messages\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield 'event: messages\ndata: {"payload":{"requirement":"旧的需求事件"}}\n\n'
        yield 'event: end\ndata: {"payload":{"status":"completed"}}\n\n'

    class FakeResponse:
        assistant_message = type("Assistant", (), {"solution_draft": {"id": "DRAFT-1"}})()

        def model_dump(self, **kwargs):
            return {"assistantMessage": {"solutionDraft": {"id": "DRAFT-1"}}}

    async def fake_project_solution_run(**kwargs):
        return FakeResponse()

    monkeypatch.setattr(product_chat_router, "_create_solution_run", fake_create_solution_run)
    monkeypatch.setattr(product_chat_router, "stream_agent_run_events", fake_stream_agent_run_events)
    monkeypatch.setattr(product_chat_router, "_project_solution_run", fake_project_solution_run)

    response = await product_chat_router.stream_message(
        conversation_id="CONV-1",
        request=product_chat_router.SendMessageRequest.model_validate(
            {"content": "设计方案", "skillId": "SOLUTION_DRAFT"}
        ),
        current_user=type("User", (), {"uid": "USER-1"})(),
    )
    body = "".join([
        chunk.decode() if isinstance(chunk, bytes) else chunk
        async for chunk in response.body_iterator
    ])

    # Only actual Blueprint text is emitted as answer content. Runtime stages
    # stay in progress events so the browser can render a ChatGPT-like,
    # collapsible execution process without synthetic prose in the answer.
    assert body.count("event: delta") == 1
    assert "可落地方案" in body
    assert "已接收需求，正在拆解客户场景与交付目标。" not in body
    assert "方案架构骨架已形成，正在核对高风险事实。" not in body
    assert 'executive_summary' not in body
    assert "旧的需求事件" not in body
    assert body.index("event: delta") < body.index("event: complete")
