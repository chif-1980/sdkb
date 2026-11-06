"""Feedback governance workflow against real PostgreSQL constraints.

Covers the acceptance boundaries that the sequence-only test does not:
concurrent transitions, rejection and re-submission, feedback without any
knowledge citation, the legacy feedback store, and cross-scope isolation.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.dashboard_router import dashboard
from server.routers.product_chat_router import product_chat
from server.utils.auth_middleware import get_db
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    Base,
    Conversation,
    Department,
    Message,
    MessageFeedback,
    RolePermission,
    User,
)
from yuxi.storage.postgres.models_knowledge import (
    FeishuKnowledgeUnit,
    FeishuMaterialVersion,
    FeishuNotificationDelivery,
    FeishuReviewItem,
    FeishuReviewPackage,
    FeishuSource,
    FeishuSourceChangeRequest,
    FeishuSourceItem,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeFile,
)
from yuxi.storage.postgres.models_product import MessageCitation, ProductConversation, ProductMessage
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def workflow(monkeypatch):
    """Isolated schema with two departments, a product answer and a legacy answer."""
    schema = "test_feedback_flow_" + uuid4().hex
    engine = create_async_engine(os.environ["POSTGRES_URL"], execution_options={"schema_translate_map": {None: schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:

            async def add(row):
                db.add(row)
                await db.flush()
                return row

            home = await add(Department(name="反馈治理甲"))
            other = await add(Department(name="反馈治理乙"))
            owner = await add(
                User(username="提出人", uid="owner", role="user", password_hash="x", department_id=home.id)
            )
            processor = await add(
                User(username="处理人", uid="processor", role="superadmin", password_hash="x", department_id=home.id)
            )
            reviewer = await add(
                User(username="复核人", uid="reviewer", role="superadmin", password_hash="x", department_id=home.id)
            )
            # Administrator without an explicit role keeps the legacy grants: it can
            # read feedback only inside the configured scope and cannot manage it.
            scoped = await add(
                User(username="范围管理员", uid="scoped", role="admin", password_hash="x", department_id=home.id)
            )
            outsider = await add(
                User(username="外部人员", uid="outsider", role="user", password_hash="x", department_id=other.id)
            )
            await add(RolePermission(role="admin", permission="feedback.view", scope="department"))

            await add(KnowledgeBase(kb_id="kb", name="test", kb_type="milvus"))
            await add(KnowledgeFile(file_id="file", kb_id="kb", filename="test.txt"))
            await add(
                FeishuSource(
                    source_id="source",
                    name="测试",
                    wiki_root_token="root",
                    target_kb_id="kb",
                    credential_env_name="unused",
                )
            )
            item = await add(
                FeishuSourceItem(
                    item_id="item",
                    source_id="source",
                    item_key="item",
                    item_type="docx",
                    title="语音机器人",
                    source_url="https://example.test/source",
                    source_validity="valid",
                    publication_status="ACTIVE",
                )
            )
            await add(
                FeishuMaterialVersion(
                    version_id="version",
                    item_id="item",
                    revision="1",
                    content_hash="hash",
                    processing_status="published",
                    review_status="approved",
                    yuxi_file_id="file",
                    published_at=datetime.now(UTC),
                )
            )
            item.active_version_id = "version"
            await add(
                FeishuKnowledgeUnit(
                    unit_id="unit",
                    unit_key="unit",
                    lineage_key="unit",
                    version_id="version",
                    item_id="item",
                    unit_index=0,
                    unit_type="SECTION",
                    title="语音机器人",
                    content="test",
                    content_hash="hash",
                    source_segment_ids=["segment"],
                    recommended_outcome="PUBLISH",
                    recommendation_reason="test",
                    publication_state="INCLUDED",
                    lifecycle_status="ACTIVE",
                    status="ACTIVE",
                )
            )
            await add(
                KnowledgeChunk(
                    chunk_id="chunk",
                    file_id="file",
                    kb_id="kb",
                    chunk_index=0,
                    content="test",
                    tags={"source_segment_ids": ["segment"]},
                )
            )

            # One answer with a governed citation and one answer without any citation.
            cited = await add(ProductConversation(owner_user_id=owner.id, title="介绍下语音机器人"))
            cited_message = await add(
                ProductMessage(
                    conversation_id=cited.conversation_id,
                    role="ASSISTANT",
                    content="机器人回答",
                    answer_status="SUPPORTED",
                )
            )
            await add(
                MessageCitation(
                    message_id=cited_message.message_id,
                    source_id="source",
                    item_id="item",
                    version_id="version",
                    yuxi_file_id="file",
                    chunk_id="chunk",
                    title="语音机器人",
                    source_url="https://example.test/source",
                    locator="第一段",
                    excerpt="test",
                )
            )
            bare = await add(ProductConversation(owner_user_id=owner.id, title="闲聊"))
            bare_message = await add(
                ProductMessage(
                    conversation_id=bare.conversation_id,
                    role="ASSISTANT",
                    content="没有引用的回答",
                    answer_status="INSUFFICIENT",
                )
            )
            # Legacy store feedback listed by the same dashboard page.
            thread = await add(Conversation(thread_id="thread-legacy", uid="owner", agent_id="test", title="旧会话"))
            legacy_message = await add(Message(conversation_id=thread.id, role="assistant", content="旧回答"))
            legacy_feedback = await add(MessageFeedback(message_id=legacy_message.id, uid="owner", rating="dislike"))
            await db.commit()

        @asynccontextmanager
        async def session_context():
            async with factory() as db:
                async with db.begin():
                    yield db

        async def get_session():
            async with session_context() as db:
                yield db

        monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)
        monkeypatch.setenv("YUXI_GOVERNANCE_AUTOMATION_MODE", "observe")
        app = FastAPI()
        app.include_router(product_chat, prefix="/api")
        app.include_router(dashboard, prefix="/api")
        app.dependency_overrides[get_db] = get_session

        def headers(user):
            return {"Authorization": f"Bearer {AuthUtils.create_access_token({'sub': str(user.id)})}"}

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            yield {
                "client": client,
                "factory": factory,
                "users": {
                    "owner": owner,
                    "processor": processor,
                    "reviewer": reviewer,
                    "scoped": scoped,
                    "outsider": outsider,
                },
                "departments": {"home": home, "other": other},
                "product_keys": {
                    "cited": f"product:{cited_message.message_id}",
                    "bare": f"product:{bare_message.message_id}",
                },
                "legacy_key": str(legacy_feedback.id),
                "owner_cookie": AuthUtils.create_access_token(
                    {"sub": str(owner.id), "token_kind": "enterprise_assistant"}
                ),
                "headers": headers,
            }
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()


async def submit_feedback(workflow, message_key: str, *, reason_type: str = "OTHER", text_: str = "回答不准"):
    """Dislike the message through the real assistant endpoint."""
    message_id = message_key.removeprefix("product:")
    response = await workflow["client"].put(
        f"/api/chat/messages/{message_id}/feedback",
        json={"rating": "DISLIKE", "reasonType": reason_type, "reasonText": text_},
        cookies={"enterprise_assistant_session": workflow["owner_cookie"]},
    )
    assert response.status_code == 200, response.text
    return response


async def act(workflow, key: str, action: str, version: int, *, actor="processor", note="处理说明", **extra):
    payload = {"version": version, "action": action, "note": note, **extra}
    return await workflow["client"].patch(
        f"/api/dashboard/feedbacks/{key}/workflow",
        json=payload,
        headers=workflow["headers"](workflow["users"][actor]),
    )


async def test_reject_returns_to_processing_and_processor_can_resubmit(workflow):
    """驳回不是关闭；退回后原处理人可再次提交，且复核仍须由他人完成。"""
    key = workflow["product_keys"]["cited"]
    await submit_feedback(workflow, key)

    version = 0
    for action in ("ASSIGN", "START", "SUBMIT_REVIEW"):
        response = await act(workflow, key, action, version, assigneeId=workflow["users"]["processor"].id)
        assert response.status_code == 200, response.text
        version = response.json()["version"]
    assert response.json()["status"] == "IN_REVIEW"

    # 处理人不能自我复核，也不能自己关闭。
    assert (await act(workflow, key, "APPROVE", version, actor="processor")).status_code == 403
    assert (await act(workflow, key, "CLOSE", version, actor="processor")).status_code == 409

    rejected = await act(workflow, key, "REJECT", version, actor="reviewer", note="引用与原文不一致，请重核")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "PROCESSING"
    assert rejected.json()["reviewerId"] == workflow["users"]["reviewer"].id
    version = rejected.json()["version"]

    # 退回后仍停留在原负责人名下，可重新提交复核。
    resubmitted = await act(workflow, key, "SUBMIT_REVIEW", version, note="已按原文重新核对")
    assert resubmitted.status_code == 200, resubmitted.text
    assert resubmitted.json()["status"] == "IN_REVIEW"
    version = resubmitted.json()["version"]

    approved = await act(workflow, key, "APPROVE", version, actor="reviewer", note="本次一致")
    assert approved.status_code == 200, approved.text
    version = approved.json()["version"]
    closed = await act(workflow, key, "CLOSE", version, note="确认关闭")
    assert closed.status_code == 200 and closed.json()["status"] == "CLOSED"

    history = closed.json()["history"]
    assert [event["action"] for event in history] == [
        "ASSIGN", "START", "SUBMIT_REVIEW", "REJECT", "SUBMIT_REVIEW", "APPROVE", "CLOSE",
    ]
    assert closed.json()["version"] == 7


async def test_reopen_is_rejected_while_the_case_is_still_processing(workflow):
    key = workflow["product_keys"]["cited"]
    await submit_feedback(workflow, key)
    version = 0
    for action in ("ASSIGN", "START"):
        response = await act(workflow, key, action, version, assigneeId=workflow["users"]["processor"].id)
        assert response.status_code == 200, response.text
        version = response.json()["version"]

    reopened = await act(workflow, key, "REOPEN", version, note="尝试重开")
    assert reopened.status_code == 409
    assert "仍在处理中" in reopened.text


async def test_concurrent_transitions_leave_exactly_one_winner(workflow):
    """同一版本的两个并发操作只能有一个生效，另一个必须收到版本冲突。"""
    key = workflow["product_keys"]["cited"]
    await submit_feedback(workflow, key)
    assignee = workflow["users"]["processor"].id

    async def assign():
        return await act(workflow, key, "ASSIGN", 0, assigneeId=assignee, note="并发指派")

    first, second = await asyncio.gather(assign(), assign())
    assert sorted([first.status_code, second.status_code]) == [200, 409], (first.text, second.text)

    winner = first if first.status_code == 200 else second
    assert winner.json()["version"] == 1
    assert len(winner.json()["history"]) == 1

    latest = await workflow["client"].get(
        f"/api/dashboard/feedbacks/{key}/workflow", headers=workflow["headers"](workflow["users"]["processor"])
    )
    assert latest.status_code == 200
    assert latest.json()["version"] == 1
    assert [event["action"] for event in latest.json()["history"]] == ["ASSIGN"]


async def test_simultaneous_first_assignment_creates_one_case(workflow):
    """工单首次创建也是并发点：两个会话都读到“无工单”时，锁必须让后到者拿到 409 而不是唯一键错误。"""
    from types import SimpleNamespace

    from yuxi.services.feedback_governance import transition_feedback
    from yuxi.storage.postgres.models_business import FeedbackCase

    key = workflow["product_keys"]["cited"]
    await submit_feedback(workflow, key)
    admin_id = workflow["users"]["processor"].id
    factory = workflow["factory"]

    def patch():
        return SimpleNamespace(version=0, action="ASSIGN", assigneeId=admin_id, note="并发首次指派")

    outcomes: dict[str, str] = {}

    async def attempt(name, started=None, release=None):
        async with factory() as session:
            user = await session.get(User, admin_id)
            try:
                await transition_feedback(session, key, user, patch())
                if started is not None:
                    started.set()
                    # 保持未提交，确保第二个会话在第一个事务内也尝试创建工单。
                    await release.wait()
                await session.commit()
                outcomes[name] = "ok"
            except Exception as exc:  # noqa: BLE001 -- 断言最终类型而非吞掉错误
                await session.rollback()
                outcomes[name] = type(exc).__name__

    started, release = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(attempt("first", started, release))
    await started.wait()
    second = asyncio.create_task(attempt("second"))
    await asyncio.sleep(0.5)
    release.set()
    await asyncio.gather(first, second)

    assert sorted(outcomes.values()) == ["HTTPException", "ok"], outcomes
    async with factory() as db:
        cases = list(await db.scalars(select(FeedbackCase).where(FeedbackCase.feedback_key == key)))
    assert [(case.status, case.version) for case in cases] == [("ASSIGNED", 1)]


async def test_feedback_without_knowledge_citations_is_still_governable(workflow):
    """没有引用、无法关联正式知识的反馈也必须能走完指派到关闭。"""
    key = workflow["product_keys"]["bare"]
    await submit_feedback(workflow, key, reason_type="MISSING_SOURCE", text_="这里查不到资料")

    response = await workflow["client"].get(
        "/api/dashboard/feedbacks", headers=workflow["headers"](workflow["users"]["processor"])
    )
    assert response.status_code == 200
    listed = {item["id"]: item for item in response.json()}
    assert key in listed and listed[key]["governance"]["status"] == "OPEN"

    version = 0
    for action in ("ASSIGN", "START", "SUBMIT_REVIEW"):
        step = await act(workflow, key, action, version, assigneeId=workflow["users"]["processor"].id)
        assert step.status_code == 200, step.text
        version = step.json()["version"]
    for action, actor in (("APPROVE", "reviewer"), ("CLOSE", "processor")):
        step = await act(workflow, key, action, version, actor=actor, note="补齐资料后确认")
        assert step.status_code == 200, step.text
        version = step.json()["version"]
    assert step.json()["status"] == "CLOSED"

    # 没有引用就没有可关联的知识单元，因此不得凭空生成审核包或修改请求。
    async with workflow["factory"]() as db:
        for model in (FeishuReviewPackage, FeishuReviewItem, FeishuSourceChangeRequest, FeishuNotificationDelivery):
            assert await db.scalar(select(func.count()).select_from(model)) == 0, model.__name__


async def test_legacy_feedback_key_is_governable(workflow):
    """旧版反馈以整数主键治理，与助手反馈共用同一套状态机。"""
    key = workflow["legacy_key"]
    response = await act(workflow, key, "ASSIGN", 0, assigneeId=workflow["users"]["processor"].id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ASSIGNED"
    assert response.json()["version"] == 1

    # 旧版反馈没有引用信息，工单仍按同一规则推进。
    version = response.json()["version"]
    for action in ("START", "SUBMIT_REVIEW"):
        response = await act(workflow, key, action, version)
        assert response.status_code == 200, response.text
        version = response.json()["version"]
    response = await act(workflow, key, "APPROVE", version, actor="reviewer", note="复核通过")
    assert response.status_code == 200, response.text
    response = await act(workflow, key, "CLOSE", response.json()["version"], note="关闭")
    assert response.status_code == 200 and response.json()["status"] == "CLOSED"


async def test_department_scoped_administrator_cannot_cross_the_scope(workflow):
    """范围管理员只能看到本部门反馈，且没有处理权限时不得推进工单。"""
    await submit_feedback(workflow, workflow["product_keys"]["cited"])
    scoped = workflow["users"]["scoped"]

    listed = await workflow["client"].get("/api/dashboard/feedbacks", headers=workflow["headers"](scoped))
    assert listed.status_code == 200, listed.text
    # 本部门的两条反馈（助手侧引用反馈与旧版反馈）都在范围内，且提交人信息只暴露本人。
    assert {item["uid"] for item in listed.json()} == {"owner"}
    assert {str(item["id"]) for item in listed.json()} == {workflow["product_keys"]["cited"], workflow["legacy_key"]}

    denied = await act(
        workflow,
        workflow["product_keys"]["cited"],
        "ASSIGN",
        0,
        actor="scoped",
        assigneeId=workflow["users"]["processor"].id,
    )
    assert denied.status_code == 403

    # 提交人本人既看不到管理端列表，也不能处理自己的反馈。
    assert (
        await workflow["client"].get(
            "/api/dashboard/feedbacks", headers=workflow["headers"](workflow["users"]["outsider"])
        )
    ).status_code == 403
    assert (
        await act(workflow, workflow["product_keys"]["cited"], "ASSIGN", 0, actor="outsider", assigneeId=scoped.id)
    ).status_code == 403


async def test_assignment_rejects_a_user_without_feedback_permission(workflow):
    """指派对象必须同时具备反馈查看与处理权限，否则不能成为负责人。"""
    await submit_feedback(workflow, workflow["product_keys"]["cited"])
    response = await act(
        workflow, workflow["product_keys"]["cited"], "ASSIGN", 0, assigneeId=workflow["users"]["outsider"].id
    )
    assert response.status_code == 422
    assert "权限" in response.text

    # 指派失败不应留下半成品工单。
    latest = await workflow["client"].get(
        f"/api/dashboard/feedbacks/{workflow['product_keys']['cited']}/workflow",
        headers=workflow["headers"](workflow["users"]["processor"]),
    )
    assert latest.status_code == 200
    assert latest.json()["status"] == "OPEN"
    assert latest.json()["version"] == 0
    assert latest.json()["history"] == []


async def test_every_action_is_blocked_after_the_user_changes_the_feedback(workflow):
    """原反馈变化后，除重新打开外的所有操作都必须拒绝，避免复核旧内容。"""
    key = workflow["product_keys"]["cited"]
    await submit_feedback(workflow, key)
    version = 0
    for action in ("ASSIGN", "START", "SUBMIT_REVIEW"):
        response = await act(workflow, key, action, version, assigneeId=workflow["users"]["processor"].id)
        assert response.status_code == 200, response.text
        version = response.json()["version"]

    async with workflow["factory"]() as db:
        stored = await db.scalar(
            select(ProductMessage).where(
                ProductMessage.message_id == workflow["product_keys"]["cited"].removeprefix("product:")
            )
        )
        stored.feedback_reason_text = "用户补充：其实是另一台设备"
        await db.commit()

    for action, actor in (
        ("APPROVE", "reviewer"),
        ("REJECT", "reviewer"),
        ("ASSIGN", "processor"),
        ("START", "processor"),
        ("SUBMIT_REVIEW", "processor"),
    ):
        blocked = await act(workflow, key, action, version, actor=actor, assigneeId=workflow["users"]["processor"].id)
        assert blocked.status_code == 409, f"{action}: {blocked.text}"

    latest = await workflow["client"].get(
        f"/api/dashboard/feedbacks/{key}/workflow", headers=workflow["headers"](workflow["users"]["reviewer"])
    )
    assert latest.json()["sourceChanged"] is True
    assert latest.json()["version"] == version
    assert latest.json()["status"] == "IN_REVIEW"

    reopened = await act(workflow, key, "REOPEN", version, actor="reviewer", note="按用户补充重新处理")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "OPEN"
    assert reopened.json()["sourceChanged"] is False
    assert reopened.json()["assigneeId"] is None
    assert reopened.json()["resolution"] is None


async def test_closed_case_only_reopens_when_the_feedback_content_actually_changes(workflow):
    """已关闭工单：内容未变不应反复重开，内容变了必须提示并重新处理。"""
    key = workflow["product_keys"]["cited"]
    await submit_feedback(workflow, key, text_="原文引用错了")
    version = 0
    for action in ("ASSIGN", "START", "SUBMIT_REVIEW"):
        response = await act(workflow, key, action, version, assigneeId=workflow["users"]["processor"].id)
        assert response.status_code == 200, response.text
        version = response.json()["version"]
    for action, actor in (("APPROVE", "reviewer"), ("CLOSE", "processor")):
        response = await act(workflow, key, action, version, actor=actor, note="已修正引用")
        assert response.status_code == 200, response.text
        version = response.json()["version"]
    assert response.json()["status"] == "CLOSED"

    async def reword(text):
        async with workflow["factory"]() as db:
            stored = await db.scalar(
                select(ProductMessage).where(ProductMessage.message_id == key.removeprefix("product:"))
            )
            stored.feedback_reason_text = text
            await db.commit()

    # 撤回后又提交完全相同的内容：没有新信息，不应反复触发重开。
    await reword(None)
    await reword("原文引用错了")
    unchanged = await workflow["client"].get(
        f"/api/dashboard/feedbacks/{key}/workflow", headers=workflow["headers"](workflow["users"]["processor"])
    )
    assert unchanged.json()["status"] == "CLOSED"
    assert unchanged.json()["sourceChanged"] is False

    # 内容确实变了：必须标记为已变化并拒绝继续处理，只能重新打开。
    await reword("原文引用错了，而且缺少版本号")
    changed = await workflow["client"].get(
        f"/api/dashboard/feedbacks/{key}/workflow", headers=workflow["headers"](workflow["users"]["processor"])
    )
    assert changed.json()["status"] == "CLOSED"
    assert changed.json()["sourceChanged"] is True
    blocked = await act(workflow, key, "ASSIGN", version, assigneeId=workflow["users"]["processor"].id)
    assert blocked.status_code == 409

    reopened = await act(workflow, key, "REOPEN", version, note="用户补充了缺失版本号")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "OPEN"
    assert reopened.json()["sourceChanged"] is False
    assert reopened.json()["version"] == version + 1
