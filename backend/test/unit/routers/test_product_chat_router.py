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


@pytest.mark.asyncio
async def test_solution_context_humanizes_legacy_resume_option_ids(monkeypatch):
    """Old resume rows without display metadata must not leak option ids."""
    runs = {
        "RUN-1": {
            "run_type": "resume",
            "created_by_run_id": "RUN-0",
            "input_content": '"CONFIRMED"',
        },
        "RUN-0": {
            "run_type": "chat",
            "conversation_thread_id": "product-CONV-1",
            "request_id": "REQ-1",
            "input_content": "设计投标方案",
            "input_metadata": {
                "agent_invocation_meta": {
                    "product_conversation_id": "CONV-1",
                },
            },
        },
    }

    async def fake_get_agent_run_view(*, run_id, current_uid, db):
        return {"run": runs[run_id]}

    monkeypatch.setattr(product_chat_router.pg_manager, "get_async_session_context", lambda: _SessionContext())
    monkeypatch.setattr(product_chat_router, "get_agent_run_view", fake_get_agent_run_view)

    _conversation_id, request = await product_chat_router._solution_context_for_run(
        run_id="RUN-1",
        current_user=SimpleNamespace(uid="USER-1"),
    )

    assert request.content == "设计投标方案\n\n补充信息：\n已确定"


def test_resume_display_answer_humanizes_case_variants_without_rewriting_prose():
    confirmed = product_chat_router.ResumeRunRequest.model_validate(
        {"answer": "CONFIRMED", "questionId": "SCOPE"}
    )
    assert product_chat_router._resume_display_answer(confirmed, []) == "已确定"

    prose = product_chat_router.ResumeRunRequest.model_validate(
        {"answer": "please use admin for this area", "questionId": "SCOPE"}
    )
    assert product_chat_router._resume_display_answer(prose, []) == "please use admin for this area"

    replayed = product_chat_router._resume_answer_text('{"SCOPE":"CONFIRMED","DEPLOYMENT":"PRIVATE"}')
    assert replayed == "SCOPE：已确定\nDEPLOYMENT：PRIVATE"


def test_legacy_draft_open_questions_are_replayed_as_a_question_batch():
    questions = product_chat_router._draft_questions_for_resume(SimpleNamespace(
        payload={
            "openQuestions": [
                "本次是展示型小程序，还是必须支持完整线上销售？",
                {"id": "DEPLOYMENT", "question": "方案采用哪种部署方式？"},
            ]
        }
    ))

    assert [item["questionId"] for item in questions] == ["OPEN_QUESTION_1", "DEPLOYMENT"]
    assert questions[0]["options"]
    assert questions[1]["options"]
    assert [item["position"] for item in questions] == [1, 2]


def test_blocked_legacy_draft_is_repaired_when_parser_derives_questions():
    """A blocked quality state must not hide a newly recoverable question batch."""
    draft = SimpleNamespace(
        status="BLOCKED",
        payload={"openQuestions": ["商城经营模式是什么？"]},
    )
    payload = product_chat_router.extract_solution_payload({
        "title": "商城方案",
        "executive_summary": "方案摘要",
        "sections": [],
        "open_questions": ["商城经营模式是什么？"],
    })

    assert payload.clarification_questions
    assert product_chat_router._blocked_draft_needs_payload_repair(draft, payload)


def test_blocked_draft_repair_does_not_resurrect_resolved_questions():
    draft = SimpleNamespace(
        status="BLOCKED",
        payload={
            "clarificationQuestionsResolved": True,
            "openQuestions": ["商城经营模式是什么？"],
        },
    )
    payload = product_chat_router.extract_solution_payload({
        "open_questions": ["商城经营模式是什么？"],
    })

    assert payload.clarification_questions
    assert not product_chat_router._blocked_draft_needs_payload_repair(draft, payload)


@pytest.mark.asyncio
async def test_completed_solution_run_continues_as_new_run(monkeypatch):
    class FakeDb:
        async def scalar(self, _query):
            return None

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeRunRepository:
        def __init__(self, _db):
            pass

        async def get_run_for_user(self, run_id, uid):
            assert run_id == "RUN-BLOCKED"
            assert uid == "USER-1"
            return SimpleNamespace(status="completed", agent_slug="solution-draft")

    async def fake_get_agent_run_view(*, run_id, current_uid, db):
        return {
            "run": {
                "agent_slug": "solution-draft",
                "input_metadata": {
                    "agent_invocation_meta": {"skill_id": "SOLUTION_DRAFT"},
                },
            }
        }

    async def fake_solution_context_for_run(*, run_id, current_user):
        return "CONV-1", product_chat_router.SendMessageRequest.model_validate(
            {
                "content": "设计投标方案",
                "skillId": "SOLUTION_DRAFT",
                "attachmentIds": ["ATT-1"],
                "requestId": "old-request",
            }
        )

    captured: dict[str, object] = {}

    async def fake_create_solution_run(*, conversation_id, request, current_user):
        captured.update(conversation_id=conversation_id, request=request)
        return {
            "run_id": "RUN-CONTINUED",
            "thread_id": "product-CONV-1",
            "status": "pending",
            "request_id": request.request_id,
        }

    monkeypatch.setattr(product_chat_router.pg_manager, "get_async_session_context", lambda: FakeSessionContext())
    monkeypatch.setattr(product_chat_router, "AgentRunRepository", FakeRunRepository)
    monkeypatch.setattr(product_chat_router, "get_agent_run_view", fake_get_agent_run_view)
    monkeypatch.setattr(product_chat_router, "_solution_context_for_run", fake_solution_context_for_run)
    monkeypatch.setattr(product_chat_router, "_create_solution_run", fake_create_solution_run)

    response = await product_chat_router.resume_product_chat_run(
        "RUN-BLOCKED",
        product_chat_router.ResumeRunRequest.model_validate(
            {
                "answer": "confirmed",
                "questionId": "SOLUTION_CONTEXT",
                "requestId": "resume-1",
            }
        ),
        SimpleNamespace(uid="USER-1"),
    )

    continued = captured["request"]
    assert response["run"]["runId"] == "RUN-CONTINUED"
    assert response["run"]["resumedFromRunId"] == "RUN-BLOCKED"
    assert continued.content == "设计投标方案\n\n补充信息：\n已确定"
    assert continued.request_id == "resume-1"
    assert continued.attachment_ids == ["ATT-1"]


@pytest.mark.asyncio
async def test_completed_solution_run_requires_the_whole_persisted_question_batch(monkeypatch):
    questions = [
        {
            "id": "SCOPE",
            "question": "本次必须支持完整线上销售吗？",
            "type": "SINGLE_CHOICE",
            "options": [{"id": "full", "label": "完整线上销售"}],
            "allowSkip": True,
        },
        {
            "id": "ADMIN",
            "question": "是否需要配套运营管理端？",
            "type": "SINGLE_CHOICE",
            "options": [{"id": "admin", "label": "需要运营管理端"}],
            "allowSkip": True,
        },
    ]

    class FakeDb:
        async def scalar(self, _query):
            return SimpleNamespace(payload={"clarificationQuestions": questions})

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeRunRepository:
        def __init__(self, _db):
            pass

        async def get_run_for_user(self, run_id, uid):
            return SimpleNamespace(status="completed", agent_slug="solution-draft")

    async def fake_get_agent_run_view(**_kwargs):
        return {
            "run": {
                "agent_slug": "solution-draft",
                "input_metadata": {
                    "agent_invocation_meta": {"skill_id": "SOLUTION_DRAFT"},
                },
            }
        }

    async def must_not_continue(**_kwargs):
        raise AssertionError("partial answers must not create a new solution run")

    monkeypatch.setattr(product_chat_router.pg_manager, "get_async_session_context", lambda: FakeSessionContext())
    monkeypatch.setattr(product_chat_router, "AgentRunRepository", FakeRunRepository)
    monkeypatch.setattr(product_chat_router, "get_agent_run_view", fake_get_agent_run_view)
    monkeypatch.setattr(product_chat_router, "_solution_context_for_run", must_not_continue)

    with pytest.raises(product_chat_router.HTTPException) as exc_info:
        await product_chat_router.resume_product_chat_run(
            "RUN-BLOCKED",
            product_chat_router.ResumeRunRequest.model_validate({
                "answer": {"SCOPE": "full"},
                "requestId": "resume-partial",
            }),
            SimpleNamespace(uid="USER-1"),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "code": "QUESTIONS_INCOMPLETE",
        "message": "请先完成全部待确认问题，再继续生成方案",
        "missingQuestionIds": ["ADMIN"],
        "runId": "RUN-BLOCKED",
    }


@pytest.mark.asyncio
async def test_completed_solution_run_continues_once_after_a_complete_question_batch(monkeypatch):
    questions = [
        {
            "id": "SCOPE",
            "question": "本次必须支持完整线上销售吗？",
            "type": "SINGLE_CHOICE",
            "options": [{"id": "full", "label": "完整线上销售"}],
            "allowSkip": True,
        },
        {
            "id": "ADMIN",
            "question": "是否需要配套运营管理端？",
            "type": "SINGLE_CHOICE",
            "options": [{"id": "admin", "label": "需要运营管理端"}],
            "allowSkip": True,
        },
    ]

    class FakeDb:
        async def scalar(self, _query):
            return SimpleNamespace(payload={"clarificationQuestions": questions})

    class FakeSessionContext:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeRunRepository:
        def __init__(self, _db):
            pass

        async def get_run_for_user(self, run_id, uid):
            return SimpleNamespace(status="completed", agent_slug="solution-draft")

    async def fake_get_agent_run_view(**_kwargs):
        return {
            "run": {
                "agent_slug": "solution-draft",
                "input_metadata": {
                    "agent_invocation_meta": {"skill_id": "SOLUTION_DRAFT"},
                },
            }
        }

    async def fake_solution_context_for_run(**_kwargs):
        return "CONV-1", product_chat_router.SendMessageRequest.model_validate({
            "content": "设计微信商城方案",
            "skillId": "SOLUTION_DRAFT",
            "attachmentIds": [],
        })

    captured: dict[str, object] = {}

    async def fake_create_solution_run(*, conversation_id, request, current_user):
        captured.update(conversation_id=conversation_id, request=request)
        return {
            "run_id": "RUN-CONTINUED",
            "thread_id": "product-CONV-1",
            "status": "pending",
            "request_id": request.request_id,
        }

    monkeypatch.setattr(product_chat_router.pg_manager, "get_async_session_context", lambda: FakeSessionContext())
    monkeypatch.setattr(product_chat_router, "AgentRunRepository", FakeRunRepository)
    monkeypatch.setattr(product_chat_router, "get_agent_run_view", fake_get_agent_run_view)
    monkeypatch.setattr(product_chat_router, "_solution_context_for_run", fake_solution_context_for_run)
    monkeypatch.setattr(product_chat_router, "_create_solution_run", fake_create_solution_run)

    response = await product_chat_router.resume_product_chat_run(
        "RUN-BLOCKED",
        product_chat_router.ResumeRunRequest.model_validate({
            "answer": {
                "SCOPE": "full",
                "ADMIN": {"value": "admin", "action": "answer"},
            },
            "requestId": "resume-complete",
        }),
        SimpleNamespace(uid="USER-1"),
    )

    continued = captured["request"]
    assert response["run"]["runId"] == "RUN-CONTINUED"
    assert continued.content == (
        "设计微信商城方案\n\n"
        "补充信息：\n"
        "以下信息已确认，请勿重复询问：\n"
        "本次必须支持完整线上销售吗？：完整线上销售\n"
        "是否需要配套运营管理端？：需要运营管理端"
    )
    assert "\nconfirmed" not in continued.content
    assert continued.request_id == "resume-complete"


def test_resume_request_answer_represents_skip_and_structured_values():
    skip = product_chat_router.ResumeRunRequest.model_validate(
        {"answer": "", "questionId": "SOLUTION_CONTEXT", "action": "skip"}
    )
    assert product_chat_router._resume_request_answer(skip) == "（用户暂不确定）"

    structured = product_chat_router.ResumeRunRequest.model_validate(
        {"answer": ["轨交", "私有化部署"], "questionId": "SOLUTION_CONTEXT"}
    )
    assert product_chat_router._resume_request_answer(structured) == "轨交、私有化部署"


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


def test_agent_progress_preserves_all_interrupt_questions_for_batch_resume():
    raw = {
        "payload": {
            "chunk": {
                "questions": [
                    {
                        "question_id": "SCOPE",
                        "question": "首期范围？",
                        "options": [{"value": "MVP", "label": "MVP"}],
                    },
                    {
                        "question_id": "DEPLOYMENT",
                        "question": "部署方式？",
                        "options": [{"value": "PRIVATE", "label": "私有化"}],
                    },
                ]
            }
        }
    }
    progress = product_chat_router._agent_progress(
        f"event: interrupt\ndata: {json.dumps(raw, ensure_ascii=False)}\n\n"
    )
    assert progress is not None
    interrupt = progress["interrupt"]
    assert interrupt["questionId"] == "SCOPE"
    assert interrupt["total"] == 2
    assert [item["questionId"] for item in interrupt["questions"]] == ["SCOPE", "DEPLOYMENT"]


def test_agent_progress_parses_real_run_event_envelope_for_interrupt_batch():
    """Redis/SSE interrupt events are wrapped as envelope -> payload -> chunk."""
    raw = {
        "schema_version": 1,
        "run_id": "RUN-INTERRUPTED",
        "event": "interrupt",
        "payload": {
            "reason": "ask_user_question_required",
            "chunk": {
                "status": "ask_user_question_required",
                "questions": [
                    {
                        "question_id": "SCOPE",
                        "question": "首期范围？",
                        "options": [
                            {"value": "mvp", "label": "先做 MVP"},
                            {"value": "full", "label": "完整建设"},
                        ],
                        "allow_other": True,
                    },
                    {
                        "question_id": "DEPLOYMENT",
                        "question": "部署方式？",
                        "options": [{"value": "private", "label": "私有化部署"}],
                        "allow_skip": False,
                    },
                ],
            },
        },
    }

    progress = product_chat_router._agent_progress(
        f"event: interrupt\ndata: {json.dumps(raw, ensure_ascii=False)}\n\n"
    )

    assert progress is not None
    interrupt = progress["interrupt"]
    assert interrupt["questionId"] == "SCOPE"
    assert interrupt["questions"][0]["options"] == [
        {"id": "mvp", "label": "先做 MVP"},
        {"id": "full", "label": "完整建设"},
    ]
    assert interrupt["questions"][1]["allowSkip"] is False


def test_agent_progress_maps_allow_skip_independently_from_allow_other():
    raw = {
        "payload": {
            "chunk": {
                "questions": [
                    {
                        "question_id": "REQUIRED",
                        "question": "必答问题",
                        "options": ["A"],
                        "allow_other": False,
                        "allow_skip": True,
                    },
                    {
                        "question_id": "NO_SKIP",
                        "question": "不可跳过问题",
                        "options": ["B"],
                        "allow_other": True,
                        "allow_skip": False,
                    },
                ]
            }
        }
    }
    progress = product_chat_router._agent_progress(
        f"event: interrupt\ndata: {json.dumps(raw, ensure_ascii=False)}\n\n"
    )
    assert progress is not None
    questions = progress["interrupt"]["questions"]
    assert questions[0]["allowSkip"] is True
    assert questions[1]["allowSkip"] is False


@pytest.mark.asyncio
async def test_interrupt_questions_prefers_latest_complete_batch_over_stale_snapshot(monkeypatch):
    """A replayed retry must not resurrect the first/stale question only."""
    events = [
        'event: interrupt\ndata: ' + json.dumps({
            "payload": {"chunk": {"questions": [
                {"question_id": "SCOPE", "question": "首期范围？"},
                {"question_id": "DEPLOYMENT", "question": "部署方式？"},
            ]}},
        }, ensure_ascii=False) + '\n\n',
        # A later retry may contain a truncated single-question copy.  Keep
        # the complete two-question batch instead of replacing it.
        'event: interrupt\ndata: ' + json.dumps({
            "payload": {"chunk": {"questions": [
                {"question_id": "SCOPE", "question": "首期范围？"},
            ]}},
        }, ensure_ascii=False) + '\n\n',
        # A same-size later batch is the authoritative current snapshot.
        'event: interrupt\ndata: ' + json.dumps({
            "payload": {"chunk": {"questions": [
                {"question_id": "SCOPE", "question": "首期范围（请确认）？"},
                {"question_id": "DEPLOYMENT", "question": "部署方式？"},
            ]}},
        }, ensure_ascii=False) + '\n\n',
    ]

    async def fake_stream_agent_run_events(**kwargs):
        for event in events:
            yield event

    monkeypatch.setattr(product_chat_router, "stream_agent_run_events", fake_stream_agent_run_events)

    questions = await product_chat_router._interrupt_questions_for_run(
        "RUN-INTERRUPTED",
        SimpleNamespace(uid="USER-1"),
        {
            # The persisted DTO can still contain the old first question.
            "interrupt": {"questionId": "SCOPE", "question": "首期范围？"},
        },
    )

    assert [item["questionId"] for item in questions] == ["SCOPE", "DEPLOYMENT"]
    assert questions[0]["question"] == "首期范围（请确认）？"
    assert [item["position"] for item in questions] == [1, 2]
    assert [item["total"] for item in questions] == [2, 2]


@pytest.mark.asyncio
async def test_interrupt_questions_deduplicates_same_question_id_and_text(monkeypatch):
    async def fake_stream_agent_run_events(**kwargs):
        yield (
            'event: interrupt\ndata: '
            + json.dumps({
                "payload": {"chunk": {"questions": [
                    {"question_id": "SCOPE", "question": "首期范围？"},
                    {"question_id": "SCOPE", "question": "首期范围？"},
                    {"question_id": "DEPLOYMENT", "question": "部署方式？"},
                ]}},
            }, ensure_ascii=False)
            + '\n\n'
        )

    monkeypatch.setattr(product_chat_router, "stream_agent_run_events", fake_stream_agent_run_events)

    questions = await product_chat_router._interrupt_questions_for_run(
        "RUN-INTERRUPTED",
        SimpleNamespace(uid="USER-1"),
        {},
    )

    assert [item["questionId"] for item in questions] == ["SCOPE", "DEPLOYMENT"]


@pytest.mark.asyncio
async def test_interrupt_questions_falls_back_to_persisted_snapshot_when_redis_is_empty(monkeypatch):
    async def empty_stream(**kwargs):
        del kwargs
        if False:
            yield ""

    monkeypatch.setattr(product_chat_router, "stream_agent_run_events", empty_stream)

    questions = await product_chat_router._interrupt_questions_for_run(
        "RUN-INTERRUPTED",
        SimpleNamespace(uid="USER-1"),
        {
            "interrupt": {
                "source": "ask_user_question",
                "questions": [
                    {"question_id": "SCOPE", "question": "首期范围？"},
                    {"question_id": "DEPLOYMENT", "question": "部署方式？"},
                ],
            }
        },
    )

    assert [item["questionId"] for item in questions] == ["SCOPE", "DEPLOYMENT"]
    partial = product_chat_router.ResumeRunRequest.model_validate({"answer": {"SCOPE": "MVP"}})
    with pytest.raises(product_chat_router.HTTPException) as exc_info:
        product_chat_router._validated_resume_answer(partial, questions, run_id="RUN-INTERRUPTED")
    assert exc_info.value.detail["code"] == "QUESTIONS_INCOMPLETE"
    assert exc_info.value.detail["missingQuestionIds"] == ["DEPLOYMENT"]


@pytest.mark.asyncio
async def test_product_run_dto_and_sse_restore_persisted_interrupt_when_redis_is_empty(monkeypatch):
    snapshot = {
        "source": "ask_user_question",
        "questions": [
            {"question_id": "SCOPE", "question": "首期范围？"},
            {"question_id": "DEPLOYMENT", "question": "部署方式？"},
        ],
    }

    async def fake_get_agent_run_view(**kwargs):
        assert kwargs["run_id"] == "RUN-INTERRUPTED"
        return {
            "run": {
                "id": "RUN-INTERRUPTED",
                "conversation_thread_id": "product-CONV-1",
                "status": "interrupted",
                "request_id": "REQ-1",
                "execution_trace": {},
                "interrupt": snapshot,
            }
        }

    async def empty_stream(**kwargs):
        del kwargs
        if False:
            yield ""

    monkeypatch.setattr(product_chat_router.pg_manager, "get_async_session_context", lambda: _SessionContext())
    monkeypatch.setattr(product_chat_router, "get_agent_run_view", fake_get_agent_run_view)
    monkeypatch.setattr(product_chat_router, "stream_agent_run_events", empty_stream)

    current_user = SimpleNamespace(uid="USER-1")
    dto = await product_chat_router.get_product_chat_run("RUN-INTERRUPTED", current_user)
    assert dto["run"]["interrupt"] == snapshot

    response = await product_chat_router.stream_product_chat_run_events("RUN-INTERRUPTED", current_user)
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert "event: interrupt" in body
    assert '"question_id": "SCOPE"' in body
    assert '"question_id": "DEPLOYMENT"' in body
    assert "event: draft" not in body


def test_validated_resume_answer_requires_all_interrupt_questions():
    questions = [
        {"questionId": "SCOPE", "allowSkip": True},
        {"questionId": "DEPLOYMENT", "allowSkip": True},
    ]
    partial = product_chat_router.ResumeRunRequest.model_validate({"answer": {"SCOPE": "MVP"}})
    with pytest.raises(Exception) as exc_info:
        product_chat_router._validated_resume_answer(partial, questions)
    error = exc_info.value
    assert getattr(error, "status_code", None) == 400
    assert error.detail["code"] == "QUESTIONS_INCOMPLETE"
    assert error.detail["missingQuestionIds"] == ["DEPLOYMENT"]

    complete = product_chat_router.ResumeRunRequest.model_validate({
        "answer": {"SCOPE": "MVP", "DEPLOYMENT": {"value": "PRIVATE", "action": "answer"}},
    })
    assert product_chat_router._validated_resume_answer(complete, questions) == {
        "SCOPE": "MVP",
        "DEPLOYMENT": "PRIVATE",
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
